from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from workspace_telemetry.config import TelemetrySettings
from workspace_telemetry.models import TimeRange
from workspace_telemetry.state import TelemetryState
from workspace_telemetry.worklog import (
    Evidence, Project, ProjectCatalog, WorklogPipeline, WorklogSynthesizer,
    _classify_next_work, _union_seconds,
)


class WorklogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 8, 22, 9, tzinfo=timezone.utc)

    def test_exact_path_beats_ambiguous_project_name(self) -> None:
        catalog = ProjectCatalog([
            Project("one", "ChewSense", repos=("/work/chew",)),
            Project("two", "Chew Sense"),
        ], {}, {})
        event = catalog.match(Evidence("event", "filesystem", self.start, self.start + timedelta(seconds=1), path="/work/chew/Sources/App.swift"))
        self.assertEqual(event.project_id, "one")
        self.assertEqual(event.matched_by, "repo_root")

    def test_ambiguous_workspace_name_stays_unresolved(self) -> None:
        catalog = ProjectCatalog([Project("one", "Chew Sense"), Project("two", "ChewSense")], {}, {})
        event = catalog.match(Evidence("event", "activity", self.start, self.start + timedelta(minutes=1), workspace="/tmp/ChewSense"))
        self.assertEqual(event.project_id, "")

    def test_filesystem_metadata_is_never_promoted_by_its_parent_directory(self) -> None:
        catalog = ProjectCatalog([Project("one", "ChewSense")], {}, {})
        event = catalog.match(Evidence(
            "event", "filesystem", self.start, self.start + timedelta(seconds=1),
            path="/Projects/PORTFOLIO/Technology/ChewSense/.DS_Store",
            workspace="/Projects/PORTFOLIO/Technology/ChewSense",
        ))
        self.assertEqual(event.project_id, "")

    def test_filesystem_reader_discards_finder_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = f"{directory}/events.jsonl"
            with open(journal, "w", encoding="utf-8") as handle:
                handle.write('{"path":"/Projects/ChewSense/.DS_Store","timestamp":"2026-08-22T09:00:00+00:00"}\n')
            pipeline = object.__new__(WorklogPipeline)
            pipeline.journal_path = Path(journal)
            pipeline.config = {}
            pipeline.settings = TelemetrySettings(notion_api_key="test")
            pipeline.state = TelemetryState(":memory:")
            interval = TimeRange(self.start, self.start + timedelta(hours=1))
            self.assertEqual(pipeline._filesystem_events(interval), [])
            pipeline.state.close()

    def test_union_does_not_inflate_overlapping_active_time(self) -> None:
        self.assertEqual(_union_seconds([(self.start, self.start + timedelta(minutes=10)), (self.start + timedelta(minutes=5), self.start + timedelta(minutes=15))]), 900)

    def test_session_gap_and_ambient_activity(self) -> None:
        pipeline = object.__new__(WorklogPipeline)
        first = Evidence("file", "filesystem", self.start, self.start + timedelta(seconds=1), project_id="one", confidence=.9)
        ambient = Evidence("window", "activity", self.start, self.start + timedelta(minutes=8), app="Code")
        second = Evidence("later", "filesystem", self.start + timedelta(minutes=25), self.start + timedelta(minutes=26), project_id="one", confidence=.9)
        groups = pipeline._sessions([first, ambient, second], None)
        self.assertEqual(len(groups), 2)
        self.assertTrue(any(event.kind == "activity" for event in groups[0]))

    def test_local_checkpoint_deduplicates_event_and_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = TelemetryState(f"{directory}/state.sqlite")
            self.assertTrue(state.record_worklog_event("hash", "git", self.start, self.start, {"safe": True}))
            self.assertFalse(state.record_worklog_event("hash", "git", self.start, self.start, {"safe": True}))
            state.save_worklog_checkpoint("cursor", "7")
            self.assertEqual(state.worklog_checkpoint("cursor"), "7")
            state.close()

    def test_model_validation_drops_unknown_session_types_and_literal_nulls(self) -> None:
        # Unknown types used to discard the whole batch; the local model also writes the string "null" for empty values.
        synthesizer = WorklogSynthesizer(TelemetrySettings(notion_api_key="test"))
        entry = synthesizer._validate({"entries": [{"id": "one", "title": "x", "session_types": ["Invented", "null", "Build"], "what_happened": "x",
                                                    "progress_summary": "null", "problems": "null", "next_step": "No action is needed for the live page.", "event": "x", "confidence": .5,
                                                    "decisions": ["null"]}]}, [{"id": "one"}], {"Build"})["one"]
        self.assertEqual(entry["session_types"], ["Build"])
        self.assertEqual((entry["progress_summary"], entry["problems"], entry["next_step"], entry["decisions"]), ("", None, None, []))
        with self.assertRaises(ValueError):
            synthesizer._validate({"entries": [{"id": "other", "title": "x", "session_types": [], "what_happened": "x", "progress_summary": "x",
                                                "problems": None, "next_step": None, "event": "x", "confidence": .5}]}, [{"id": "one"}], {"Build"})

    def test_local_directory_beats_ambiguous_project_name(self) -> None:
        catalog = ProjectCatalog([
            Project("one", "Chew Sense", local_directory="/work/chew"),
            Project("two", "ChewSense"),
        ], {}, {})
        event = catalog.match(Evidence("event", "filesystem", self.start, self.start + timedelta(seconds=1), path="/work/chew/Sources/App.swift"))
        self.assertEqual(event.project_id, "one")
        self.assertEqual(event.matched_by, "local_directory")

    def test_classify_next_work_only_uses_allowed_options(self) -> None:
        text = "Requires manual XCTest UI permission grant. Update README documentation."
        self.assertEqual(_classify_next_work(text, {"Testing", "Documentation"}), ["Testing", "Documentation"])
        self.assertEqual(_classify_next_work(text, {"Documentation"}), ["Documentation"])
        self.assertEqual(_classify_next_work(text, set()), [])

    def test_sync_next_steps_upserts_then_is_idempotent(self) -> None:
        class FakeNotion:
            def __init__(self) -> None:
                self.updates: list[tuple[str, dict]] = []

            def update_page(self, page_id: str, properties: dict) -> None:
                self.updates.append((page_id, properties))

        with tempfile.TemporaryDirectory() as directory:
            docs = f"{directory}/docs"
            import os
            os.makedirs(docs)
            with open(f"{docs}/NEXT_STEPS.md", "w", encoding="utf-8") as handle:
                handle.write("Requires a one-time XCTest UI automation permission grant.")

            pipeline = object.__new__(WorklogPipeline)
            pipeline.catalog = ProjectCatalog([Project("proj-1", "Sample", local_directory=directory)], {}, {})
            pipeline.notion = FakeNotion()
            pipeline.next_work_options = {"Testing"}
            pipeline.state = TelemetryState(":memory:")

            first = pipeline.sync_next_steps(dry_run=False)
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["action"], "updated")
            self.assertEqual(first[0]["tags"], ["Testing"])
            self.assertEqual(len(pipeline.notion.updates), 1)

            second = pipeline.sync_next_steps(dry_run=False)
            self.assertEqual(second, [])
            self.assertEqual(len(pipeline.notion.updates), 1)
            pipeline.state.close()


if __name__ == "__main__":
    unittest.main()
