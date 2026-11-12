"""Richer Work Log evidence: full commit messages, assistant results, notes, grounding, and the synthesis cache."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from workspace_telemetry.config import TelemetrySettings
from workspace_telemetry.models import TimeRange
from workspace_telemetry.state import TelemetryState
from workspace_telemetry.worklog import (
    Evidence, Project, ProjectCatalog, WorklogPipeline, WorklogSynthesizer, _batches, _file_areas, _grounded, _read_claude, _read_codex,
)

START = datetime(2026, 10, 9, 15, tzinfo=timezone.utc)


def pipeline(projects: list[Project] | None = None) -> WorklogPipeline:
    instance = object.__new__(WorklogPipeline)
    instance.settings = TelemetrySettings(notion_api_key="test")
    instance.catalog = ProjectCatalog(projects or [Project("p1", "Hue Light Control", local_directory="/w/Hue")], {}, {})
    instance.state = TelemetryState(":memory:")
    return instance


def write_lines(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


class GitEvidenceTests(unittest.TestCase):
    def test_full_message_and_every_changed_file_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = lambda *args: subprocess.run(["git", "-C", directory, *args], check=True, capture_output=True)
            run("init", "-q")
            run("config", "user.email", "t@example.com"); run("config", "user.name", "T")
            for name in ("encoder.cpp", "display.cpp"):
                (Path(directory) / name).write_text("x", encoding="utf-8")
            run("add", "-A")
            run("commit", "-q", "-m", "Debounce the rotary encoder\n\nFast turns skipped steps, so readings are now debounced.")
            instance = pipeline([Project("p1", "Hue", repos=(directory,))])
            events = instance._git_events(TimeRange(datetime.now(timezone.utc) - timedelta(hours=1), datetime.now(timezone.utc) + timedelta(hours=1)), [])
            commit = next(event for event in events if event.payload.get("event_type") == "commit")
            self.assertEqual(commit.summary, "Debounce the rotary encoder")
            self.assertIn("Fast turns skipped steps", commit.payload["body"])
            self.assertEqual(sorted(commit.payload["changed_paths"]), ["display.cpp", "encoder.cpp"])
            self.assertEqual(commit.payload["changed_count"], 2)
            instance.state.close()


class TranscriptTests(unittest.TestCase):
    def test_codex_keeps_the_real_request_and_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            write_lines(path, [
                {"type": "session_meta", "payload": {"cwd": "/w/Hue"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Make the dial switch rooms on long press"}]}},
                {"type": "response_item", "timestamp": "2026-10-09T15:10:00Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Long press now cycles rooms; the OLED shows the room name."}]}},
                {"type": "event_msg", "timestamp": "2026-10-09T15:20:00Z", "payload": {"type": "agent_message", "message": "You've hit your usage limit."}},
            ])
            cwd, prompt, response, when = _read_codex(path)
            self.assertEqual((cwd, prompt), ("/w/Hue", "Make the dial switch rooms on long press"))
            self.assertEqual(response, "Long press now cycles rooms; the OLED shows the room name.")
            self.assertEqual(when, "2026-10-09T15:10:00Z")

    def test_claude_skips_side_threads_and_injected_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            write_lines(path, [
                {"type": "user", "cwd": "/w/Hue", "message": {"content": "<command-name>/clear</command-name>"}},
                {"type": "user", "cwd": "/w/Hue", "message": {"content": "Add a diagnostics screen to the firmware"}},
                {"type": "assistant", "isSidechain": True, "timestamp": "2026-10-09T15:30:00Z", "message": {"content": [{"type": "text", "text": "side work"}]}},
                {"type": "assistant", "timestamp": "2026-10-09T15:25:00Z", "message": {"content": [{"type": "text", "text": "Added a diagnostics screen showing Wi-Fi and bridge status."}]}},
            ])
            self.assertEqual(_read_claude(path), ("/w/Hue", "Add a diagnostics screen to the firmware",
                                                  "Added a diagnostics screen showing Wi-Fi and bridge status.", "2026-10-09T15:25:00Z"))


class PacketTests(unittest.TestCase):
    def events(self) -> list[Evidence]:
        return [
            Evidence("a", "activity", START, START + timedelta(minutes=30), app="Xcode", workspace="BetterWorkspace.xcodeproj", project_id="p1"),
            Evidence("f", "filesystem", START, START, path="/w/Hue/src/encoder.cpp", project_id="p1"),
            Evidence("g", "git", START, START, repo="/w/Hue", project_id="p1", summary="Debounce the rotary encoder",
                     payload={"event_type": "commit", "body": "Fast turns skipped steps.", "changed_paths": ["src/encoder.cpp"]}),
            Evidence("c", "ai", START, START, project_id="p1", summary="Add diagnostics",
                     payload={"tool": "Claude Code", "intent": "Add diagnostics", "outcome": "Added a diagnostics screen. It shows bridge status."}),
            Evidence("w", "ai", START, START, project_id="p1", summary="Codex activity", payload={"tool": "Codex", "intent": "", "outcome": ""}),
        ]

    def test_packet_carries_the_work_and_not_the_apps(self) -> None:
        instance = pipeline()
        packet = instance._evidence_packet(self.events(), "p1", START, "h1", ["Fixed the skipping dial before the demo."])
        text = json.dumps(packet)
        self.assertNotIn("Xcode", text)
        self.assertNotIn("BetterWorkspace", text)
        self.assertEqual(packet["commits"][0]["message"], "Fast turns skipped steps.")
        self.assertEqual(packet["assistant_sessions"], [{"tool": "Claude Code", "asked": "Add diagnostics", "result": "Added a diagnostics screen. It shows bridge status."}])
        self.assertEqual(packet["files_changed"], {"count": 1, "names": ["src/encoder.cpp"]})
        self.assertEqual(packet["your_notes"], ["Fixed the skipping dial before the demo."])
        instance.state.close()

    def test_fallback_summary_lists_work_not_apps(self) -> None:
        instance = pipeline()
        props = instance._worklog_properties(self.events(), "p1", START, "h1", {"Build"}, None, [])
        happened = props["What Happened"]["rich_text"][0]["text"]["content"]
        self.assertIn("Commits: Debounce the rotary encoder", happened)
        self.assertIn("Assistant work: Added a diagnostics screen", happened)
        self.assertIn("Changed: src/encoder.cpp", happened)
        self.assertNotIn("Xcode", happened)
        instance.state.close()

    def test_model_summary_keeps_grounded_points_and_the_notes_verbatim(self) -> None:
        instance = pipeline()
        semantic = {"session_types": ["Build"], "what_happened": "Debounced the rotary encoder and added a diagnostics screen.",
                    "accomplishments": ["Debounced the rotary encoder"], "decisions": ["Debounce readings because fast turns skipped steps"],
                    "title": "Encoder debounce", "progress_summary": "x", "event": "x", "confidence": .8}
        props = instance._worklog_properties(self.events(), "p1", START, "h1", {"Build"}, semantic, ["Fixed the skipping dial before the demo."])
        happened = "".join(part["text"]["content"] for part in props["What Happened"]["rich_text"])
        self.assertIn("Done: Debounced the rotary encoder", happened)
        self.assertIn("Decided: Debounce readings because fast turns skipped steps", happened)
        self.assertIn("In your words: Fixed the skipping dial before the demo.", happened)
        instance.state.close()

    def test_session_notes_are_read_for_the_day_and_project_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "work-sessions.jsonl"
            write_lines(inbox, [
                {"timestamp": "2026-10-09T09:00:00-07:00", "project_ids": ["p1"], "summary": "Fixed the skipping dial before the demo."},
                {"timestamp": "2026-10-09T10:00:00-07:00", "project_ids": ["p1"], "summary": "Made progress"},
                {"timestamp": "2026-10-08T10:00:00-07:00", "project_ids": ["p1"], "summary": "Yesterday's note about the case design."},
            ])
            instance = pipeline()
            instance.settings = TelemetrySettings(notion_api_key="test", timezone="America/Los_Angeles") if "timezone" in TelemetrySettings.__dataclass_fields__ else instance.settings
            instance.session_inbox_path = inbox
            day = datetime(2026, 10, 9, tzinfo=instance.settings.zoneinfo())
            self.assertEqual(instance._session_notes(day), {"p1": ["Fixed the skipping dial before the demo."]})
            instance.state.close()


class FileAreaTests(unittest.TestCase):
    def test_few_files_are_listed_and_directories_dropped(self) -> None:
        self.assertEqual(_file_areas(["src", "src/encoder.cpp", "README.md", "src/__pycache__/x.cpython-314.pyc", "tests/__pycache__"]),
                         ["src/encoder.cpp", "README.md"])

    def test_many_files_become_areas_with_counts(self) -> None:
        names = [f"docs/evidence/audit/shot{i}.png" for i in range(30)] + ["src/encoder.cpp"]
        self.assertEqual(_file_areas(names), ["docs/evidence/audit (30 files)", "src/encoder.cpp"])


class SynthesisTests(unittest.TestCase):
    def test_ungrounded_points_and_activity_only_summaries_are_dropped(self) -> None:
        packet = {"id": "h1", "commits": [{"subject": "Debounce the rotary encoder", "message": "Fast turns skipped steps.", "files": []}]}
        synthesizer = WorklogSynthesizer(TelemetrySettings(notion_api_key="test"))
        entry = synthesizer._ground({"what_happened": "Used Xcode and Finder and changed 4 files.", "accomplishments": ["Debounced the rotary encoder", "Shipped to the App Store"],
                                     "decisions": ["Rewrote the app in Rust for speed"]}, packet)
        self.assertEqual(entry["what_happened"], "")
        self.assertEqual(entry["accomplishments"], ["Debounced the rotary encoder"])
        self.assertEqual(entry["decisions"], [])
        self.assertTrue(_grounded("debounced encoder", "Debounce the rotary encoder"))

    def test_unchanged_packets_are_not_sent_to_the_model_again(self) -> None:
        state = TelemetryState(":memory:")
        synthesizer = WorklogSynthesizer(TelemetrySettings(notion_api_key="test"), cache=state)
        calls = []
        entry = {"title": "t", "session_types": ["Build"], "what_happened": "Debounced the rotary encoder.", "accomplishments": [], "decisions": [],
                 "progress_summary": "p", "problems": None, "next_step": None, "event": "e", "confidence": .7}
        synthesizer._request = lambda packets, allowed, deadline=None: calls.append([p["id"] for p in packets]) or {p["id"]: dict(entry) for p in packets}
        packets = [{"id": "h1", "commits": [{"subject": "Debounce the rotary encoder"}]}]
        first = synthesizer.synthesize(packets, {"Build"})
        second = synthesizer.synthesize(packets, {"Build"})
        self.assertEqual(calls, [["h1"]])
        self.assertEqual(first, second)
        changed = [{"id": "h1", "commits": [{"subject": "Debounce the rotary encoder"}, {"subject": "Show the room name"}]}]
        synthesizer.synthesize(changed, {"Build"})
        self.assertEqual(len(calls), 2)
        state.close()

    def test_richest_packets_go_first_and_the_time_budget_is_respected(self) -> None:
        synthesizer = WorklogSynthesizer(TelemetrySettings(notion_api_key="test"), budget_seconds=0)
        calls = []
        synthesizer._request = lambda packets, allowed, deadline=None: calls.append([p["id"] for p in packets]) or {}
        packets = [{"id": "files"}, {"id": "notes", "your_notes": ["Fixed the dial."]}, {"id": "commits", "commits": [{"subject": "x"}]}]
        synthesizer.synthesize(packets, {"Build"})
        self.assertEqual(calls, [["notes", "commits", "files"]])  # the first batch always runs
        calls.clear()
        synthesizer.synthesize([{"id": str(i)} for i in range(7)], {"Build"})
        self.assertEqual(calls, [["0", "1", "2"]])  # out of time after the first batch; the rest wait for the next pass

    def test_no_repair_attempt_after_the_deadline(self) -> None:
        import time as clock
        from unittest import mock
        synthesizer = WorklogSynthesizer(TelemetrySettings(notion_api_key="test", lm_studio_timeout_seconds=1))
        with mock.patch("workspace_telemetry.worklog.urlopen", side_effect=OSError("busy")) as opened:
            self.assertEqual(synthesizer._request([{"id": "h1"}], {"Build"}, deadline=clock.monotonic() - 1), {})
            self.assertEqual(opened.call_count, 1)
            synthesizer._request([{"id": "h1"}], {"Build"}, deadline=clock.monotonic() + 60)
            self.assertEqual(opened.call_count, 3)

    def test_batches_stay_small(self) -> None:
        packets = [{"id": str(i), "text": "x" * 3000} for i in range(5)]
        self.assertEqual([len(batch) for batch in _batches(packets)], [2, 2, 1])
        self.assertEqual([len(batch) for batch in _batches([{"id": str(i)} for i in range(7)])], [3, 3, 1])


if __name__ == "__main__":
    unittest.main()
