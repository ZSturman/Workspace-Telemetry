from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from workspace_telemetry.state import TelemetryState
from workspace_telemetry.worklog import WorklogPipeline

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


def _record(state: TelemetryState, kind: str, age_days: float, index: int, payload_size: int = 10) -> None:
    start = NOW - timedelta(days=age_days)
    state.record_worklog_event(f"{kind}-{age_days}-{index}", kind, start, start + timedelta(seconds=1), {"path": "x" * payload_size})


def _kinds(state: TelemetryState) -> dict[str, int]:
    return dict(state.connection.execute("SELECT kind, count(*) FROM worklog_events GROUP BY kind").fetchall())


class WorklogRetentionTests(unittest.TestCase):
    """Raw evidence is pruned by kind; git/AI rows gate Notion writes and are kept."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "telemetry.sqlite"
        self.state = TelemetryState(self.path)
        for kind in ("filesystem", "activity", "afk", "terminal", "git", "ai"):
            for age in (1, 20, 120):
                _record(self.state, kind, age, 0)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_prune_applies_windows_by_kind(self):
        removed = self.state.prune_worklog_events(NOW)
        self.assertEqual(removed, {"filesystem": 2, "activity": 1, "afk": 1, "terminal": 1})
        self.assertEqual(_kinds(self.state), {"filesystem": 1, "activity": 2, "afk": 2, "terminal": 2, "git": 3, "ai": 3})

    def test_dry_run_counts_without_deleting(self):
        removed = self.state.prune_worklog_events(NOW, dry_run=True)
        self.assertEqual(removed["filesystem"], 2)
        self.assertEqual(sum(_kinds(self.state).values()), 18)

    def test_pruned_hash_can_be_recorded_again(self):
        self.state.prune_worklog_events(NOW)
        start = NOW - timedelta(days=20)
        self.assertTrue(self.state.record_worklog_event("filesystem-20-0", "filesystem", start, start, {}))

    def test_compact_then_prune_returns_space_to_the_filesystem(self):
        for index in range(3000):
            _record(self.state, "filesystem", 30, index, payload_size=600)
        self.state.compact()
        self.assertEqual(self.state.connection.execute("PRAGMA auto_vacuum").fetchone()[0], 2)
        before = self.path.stat().st_size
        self.state.prune_worklog_events(NOW)
        self.state.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assertLess(self.path.stat().st_size, before / 2)
        self.assertEqual(self.state.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class DailyPruneGateTests(unittest.TestCase):
    def _pipeline(self, checkpoint: str) -> WorklogPipeline:
        pipeline = WorklogPipeline.__new__(WorklogPipeline)
        pipeline.settings = Mock()
        pipeline.state = Mock()
        pipeline.state.worklog_checkpoint = Mock(return_value=checkpoint)
        pipeline.state.prune_worklog_events = Mock(return_value={"filesystem": 5})
        return pipeline

    def test_prunes_once_per_local_day(self):
        with patch("workspace_telemetry.worklog._now", return_value=NOW):
            pipeline = self._pipeline(checkpoint="2026-10-20")
            self.assertEqual(pipeline._prune_evidence_daily(dry_run=False), {"filesystem": 5})
            pipeline.state.save_worklog_checkpoint.assert_called_once_with("worklog_prune_day", "2026-10-21")
            already = self._pipeline(checkpoint="2026-10-21")
            self.assertEqual(already._prune_evidence_daily(dry_run=False), {})
            already.state.prune_worklog_events.assert_not_called()

    def test_dry_run_and_errors_do_not_prune_or_fail(self):
        with patch("workspace_telemetry.worklog._now", return_value=NOW):
            pipeline = self._pipeline(checkpoint="")
            self.assertEqual(pipeline._prune_evidence_daily(dry_run=True), {})
            pipeline.state.prune_worklog_events.assert_not_called()
            pipeline.state.prune_worklog_events.side_effect = RuntimeError("database is locked")
            self.assertEqual(pipeline._prune_evidence_daily(dry_run=False), {"error": "database is locked"})
            pipeline.state.save_worklog_checkpoint.assert_not_called()


class FilesystemNoiseTests(unittest.TestCase):
    def test_generated_output_and_service_logs_are_ignored(self):
        from workspace_telemetry import worklog
        for path in ("/Users/u/Workspace/Projects/App/.build/out/a.o", "/Users/u/Workspace/Logs/workspace-watcher/core.db",
                     "/Users/u/Workspace/Projects/Cam/Scripts/fixtures/output/hls/index1.ts", "/p/pkg/__pycache__/m.pyc"):
            self.assertTrue(any(token in path for token in worklog.DEFAULT_IGNORES), path)
        self.assertFalse(any(token in "/Users/u/Workspace/Projects/App/Sources/main.swift" for token in worklog.DEFAULT_IGNORES))


if __name__ == "__main__":
    unittest.main()
