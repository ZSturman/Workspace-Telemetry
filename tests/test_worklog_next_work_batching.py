from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from workspace_telemetry.worklog import MAX_NEXT_WORK_ANALYSES_PER_SYNC, WorklogPipeline


def _project_page(index: int) -> dict:
    return {
        "id": f"project-{index}",
        "properties": {
            "title": {"type": "title", "title": [{"plain_text": f"Project {index}"}]},
            "Next Work": {"type": "multi_select", "multi_select": []},
        },
    }


class AnalyzeNextWorkBatchingTests(unittest.TestCase):
    """A ~230-project "Needs Next Work Analysis" backlog made analyze_next_work
    iterate every checked project on every scheduled worklog_sync, each doing a
    sequential local-LM call, which routinely blew past Hammerspoon's 120s
    worklog timeout before a single project got unchecked -- so the backlog
    never drained and every sync timed out. analyze_next_work must cap how
    many projects it processes per call."""

    def _pipeline(self, checked_count: int) -> WorklogPipeline:
        pipeline = WorklogPipeline.__new__(WorklogPipeline)
        pipeline.settings = Mock(zoneinfo=Mock(return_value=None))
        pipeline.settings.projects_database_id = "projects-db"
        pipeline.work_store = Mock(source_id="work-log-db")
        notion = Mock()
        notion.data_source_id = Mock(return_value="projects-source")
        notion.query_data_source = Mock(side_effect=lambda source_id, **kwargs: (
            [_project_page(index) for index in range(checked_count)] if source_id == "projects-source" else []
        ))
        notion.update_page = Mock()
        pipeline.notion = notion
        return pipeline

    def test_large_backlog_is_capped_per_sync_call(self):
        pipeline = self._pipeline(checked_count=231)
        with patch("workspace_telemetry.worklog.NextWorkAnalyzer") as analyzer_cls:
            analyzer_cls.return_value.analyze = Mock(return_value="Some concrete next step.")
            results = pipeline.analyze_next_work(dry_run=False)

        self.assertEqual(len(results), MAX_NEXT_WORK_ANALYSES_PER_SYNC)
        self.assertEqual(analyzer_cls.return_value.analyze.call_count, MAX_NEXT_WORK_ANALYSES_PER_SYNC)
        self.assertEqual(pipeline.notion.update_page.call_count, MAX_NEXT_WORK_ANALYSES_PER_SYNC)

    def test_small_backlog_is_not_padded_or_truncated(self):
        pipeline = self._pipeline(checked_count=3)
        with patch("workspace_telemetry.worklog.NextWorkAnalyzer") as analyzer_cls:
            analyzer_cls.return_value.analyze = Mock(return_value="Some concrete next step.")
            results = pipeline.analyze_next_work(dry_run=False)

        self.assertEqual(len(results), 3)


if __name__ == "__main__":
    unittest.main()
