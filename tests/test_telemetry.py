from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from workspace_telemetry.aggregation import aggregate
from workspace_telemetry.config import PrivacyRules
from workspace_telemetry.models import ActivityEvent, ActivityWatchSnapshot, TimeRange


class ProductivityTelemetryTests(unittest.TestCase):
    def test_productivity_is_classified_without_external_mode_state(self):
        start = datetime(2026, 7, 19, 9, tzinfo=timezone.utc)
        period = TimeRange(start, start + timedelta(minutes=30))
        snapshot = ActivityWatchSnapshot(True, events=[ActivityEvent("event", "window", "window", start, start + timedelta(minutes=30), app="Code", category="coding")], has_window_bucket=True, has_afk_bucket=True)
        metrics = aggregate(period, snapshot, [], [], PrivacyRules())
        self.assertEqual(metrics.on_task_minutes, 30)
        self.assertNotIn("focus", " ".join(metrics.as_dict()).casefold())
        self.assertIn("productivityScore", metrics.as_dict())


if __name__ == "__main__": unittest.main()
