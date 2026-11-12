from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from workspace_telemetry.worklog import Evidence, WorklogPipeline


def _git_event(start: datetime) -> Evidence:
    return Evidence(
        hash="git-fixed-hash", kind="git", start=start, end=start + timedelta(seconds=1),
        repo="/repo", payload={"branch": "main", "event_type": "commit", "changed_paths": []},
    )


def _ai_event(start: datetime) -> Evidence:
    return Evidence(
        hash="ai-fixed-hash", kind="ai", start=start, end=start + timedelta(seconds=1),
        workspace="/repo", payload={"tool": "Codex"},
    )


class PersistEvidenceIncrementalWriteTests(unittest.TestCase):
    """Every sync re-collects the whole local day, so `_persist_evidence` must
    only push a git/AI event to Notion the first time it is seen -- otherwise
    every 10-minute cycle re-upserts every such event recorded since midnight,
    a cost that grows across the day and was the main reason worklog_sync
    increasingly timed out later in the day."""

    def _pipeline(self, *, already_known: bool) -> WorklogPipeline:
        pipeline = WorklogPipeline.__new__(WorklogPipeline)
        pipeline.state = Mock()
        # record_worklog_event is INSERT OR IGNORE: True means newly inserted,
        # False means this exact event hash was already recorded.
        pipeline.state.record_worklog_event = Mock(return_value=not already_known)
        pipeline.git_store = Mock()
        pipeline.ai_store = Mock()
        return pipeline

    def test_new_git_and_ai_events_are_written_to_notion(self):
        pipeline = self._pipeline(already_known=False)
        start = datetime(2026, 9, 7, 9, tzinfo=timezone.utc)

        counts = pipeline._persist_evidence([_git_event(start), _ai_event(start)], dry_run=False)

        self.assertEqual(counts, {"activity": 0, "filesystem": 0, "git": 1, "ai": 1})
        pipeline.git_store.upsert.assert_called_once()
        pipeline.ai_store.upsert.assert_called_once()

    def test_already_recorded_git_and_ai_events_are_not_rewritten(self):
        pipeline = self._pipeline(already_known=True)
        start = datetime(2026, 9, 7, 9, tzinfo=timezone.utc)

        counts = pipeline._persist_evidence([_git_event(start), _ai_event(start)], dry_run=False)

        self.assertEqual(counts, {"activity": 0, "filesystem": 0, "git": 1, "ai": 1})
        pipeline.git_store.upsert.assert_not_called()
        pipeline.ai_store.upsert.assert_not_called()

    def test_dry_run_never_writes_and_never_touches_state(self):
        pipeline = self._pipeline(already_known=False)
        start = datetime(2026, 9, 7, 9, tzinfo=timezone.utc)

        pipeline._persist_evidence([_git_event(start), _ai_event(start)], dry_run=True)

        pipeline.state.record_worklog_event.assert_not_called()
        pipeline.git_store.upsert.assert_not_called()
        pipeline.ai_store.upsert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
