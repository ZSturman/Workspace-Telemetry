from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from workspace_telemetry.worklog import Evidence, WorklogPipeline


def _activity_event(project_id: str, confidence: float, start: datetime, minutes: int = 10) -> Evidence:
    return Evidence(
        hash=f"activity-{start.isoformat()}",
        kind="activity",
        start=start,
        end=start + timedelta(minutes=minutes),
        app="Safari",
        project_id=project_id,
        confidence=confidence,
        matched_by="app_hint",
    )


def _git_event(project_id: str, confidence: float, start: datetime) -> Evidence:
    return Evidence(
        hash=f"git-{start.isoformat()}",
        kind="git",
        start=start,
        end=start + timedelta(seconds=1),
        repo="/repo",
        project_id=project_id,
        confidence=confidence,
        matched_by="repo_root",
        payload={"branch": "main"},
    )


class WriteActivitySessionTests(unittest.TestCase):
    """A work session with only foreground-window/AFK evidence (no git, AI, or
    filesystem activity) used to crash `_write_activity_session` with
    `ValueError: max() iterable argument is empty`, which surfaced to
    Hammerspoon as a scheduled worklog_sync failure/timeout."""

    def _pipeline(self) -> WorklogPipeline:
        pipeline = WorklogPipeline.__new__(WorklogPipeline)
        pipeline.activity_store = Mock()
        return pipeline

    def test_activity_only_session_does_not_crash(self):
        pipeline = self._pipeline()
        start = datetime(2026, 9, 6, 9, tzinfo=timezone.utc)
        events = [_activity_event("project-1", 0.8, start)]

        pipeline._write_activity_session(events, dry_run=False)

        pipeline.activity_store.upsert.assert_called_once()
        properties = pipeline.activity_store.upsert.call_args.args[0]
        self.assertEqual(properties["project confidence"]["number"], 0.8)

    def test_mixed_session_still_uses_core_evidence_confidence(self):
        pipeline = self._pipeline()
        start = datetime(2026, 9, 6, 9, tzinfo=timezone.utc)
        events = [
            _activity_event("project-1", 0.5, start),
            _git_event("project-1", 0.95, start + timedelta(minutes=1)),
        ]

        pipeline._write_activity_session(events, dry_run=False)

        properties = pipeline.activity_store.upsert.call_args.args[0]
        self.assertEqual(properties["project confidence"]["number"], 0.95)

    def test_session_with_no_project_id_is_skipped(self):
        pipeline = self._pipeline()
        start = datetime(2026, 9, 6, 9, tzinfo=timezone.utc)
        events = [_activity_event("", 0.0, start)]

        pipeline._write_activity_session(events, dry_run=False)

        pipeline.activity_store.upsert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
