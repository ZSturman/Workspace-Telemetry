from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from workspace_telemetry.models import (
    GenerationResult,
    Strategy,
    SummaryContent,
    TelemetryMetrics,
    TelemetryRecord,
    TimeRange,
)
from workspace_telemetry.notion import REQUIRED_PROPERTIES, TelemetrySummariesStore, record_properties

# The Telemetry Summaries database's actual live property names, captured
# 2026-09-06. Names here get renamed occasionally (e.g. "On Task Minutes"/
# "Productivity Score" became "Focus Minutes"/"Focus Score"); when that
# happens, `record_properties` silently keeps sending the old name and Notion
# rejects the write with a 400 deep inside a scheduled run, which
# `validate_schema` did not catch because it only checks REQUIRED_PROPERTIES.
# `record_properties` must only ever emit keys from this set. Update this
# fixture (and REQUIRED_PROPERTIES for anything that should fail fast) when
# the live database's schema is intentionally changed.
KNOWN_SCHEMA_PROPERTIES = {
    "Name", "Period Start", "Period End", "Active Minutes", "Idle Minutes",
    "Focus Minutes", "Focus Score", "Friction Score", "Context Switches",
    "Source Event Count", "Applications", "Activity Categories", "Websites",
    "ActivityWatch Buckets", "Projects", "Topics", "People / Organizations",
    "Dominant App", "Dominant Category", "Primary Activity", "Summary",
    "Evidence", "Work Completed", "Work In Progress", "Next Likely Action",
    "Confidence", "Context Switch IDs", "Source Hash", "Display Data",
    "Raw Data", "Status", "Summary Version", "Generated At", "Previous Summary",
}


def _make_record(previous_page_id: str = "") -> TelemetryRecord:
    start = datetime(2026, 9, 6, tzinfo=timezone.utc)
    return TelemetryRecord(
        key="interval-production-v2:test",
        time_range=TimeRange(start, start + timedelta(minutes=30)),
        strategy=Strategy(id="interval-production-v2", cadence="interval", source_mode="activitywatch", context_size="small"),
        source_hash="deadbeef",
        metrics=TelemetryMetrics(on_task_seconds=600, active_seconds=1200),
        generation=GenerationResult(content=SummaryContent(summary="Did work.", primary_activity="Building"), used_lm_studio=False),
        status="Generated",
        raw_data={},
        previous_page_id=previous_page_id,
    )


class RecordPropertiesSchemaTests(unittest.TestCase):
    """Guards against Notion property names drifting away from the live schema."""

    def test_record_properties_only_uses_known_live_schema_names(self):
        properties = record_properties(_make_record(), "Telemetry | interval | test")
        unknown = set(properties) - KNOWN_SCHEMA_PROPERTIES
        self.assertEqual(unknown, set(), f"record_properties emitted names not in the live Notion schema: {unknown}")

    def test_record_properties_uses_current_focus_names_not_retired_ones(self):
        properties = record_properties(_make_record(), "Telemetry | interval | test")
        self.assertIn("Focus Minutes", properties)
        self.assertIn("Focus Score", properties)
        self.assertNotIn("On Task Minutes", properties)
        self.assertNotIn("Productivity Score", properties)

    def test_required_properties_includes_focus_fields_so_validation_fails_fast(self):
        self.assertEqual(REQUIRED_PROPERTIES.get("Focus Minutes"), "number")
        self.assertEqual(REQUIRED_PROPERTIES.get("Focus Score"), "number")


class ValidateSchemaTests(unittest.TestCase):
    def _store_with_schema(self, properties_by_name: dict[str, str]) -> TelemetrySummariesStore:
        notion = Mock()
        notion.data_source_id = Mock(return_value="data-source-1")
        notion.get_data_source = Mock(return_value={
            "properties": {name: {"type": kind} for name, kind in properties_by_name.items()}
        })
        return TelemetrySummariesStore(notion, "database-1")

    def test_validate_schema_passes_when_focus_properties_exist(self):
        schema = {name: "number" for name in ("Focus Minutes", "Focus Score")}
        schema.update({name: kind for name, kind in REQUIRED_PROPERTIES.items()})
        store = self._store_with_schema(schema)
        store.validate_schema()  # must not raise

    def test_validate_schema_raises_a_clear_error_when_a_property_is_renamed_away(self):
        schema = {name: kind for name, kind in REQUIRED_PROPERTIES.items() if name != "Focus Score"}
        store = self._store_with_schema(schema)
        with self.assertRaises(RuntimeError) as failure:
            store.validate_schema()
        self.assertIn("Focus Score", str(failure.exception))

    def test_upsert_sends_only_known_schema_properties_to_notion(self):
        schema = {name: kind for name, kind in REQUIRED_PROPERTIES.items()}
        store = self._store_with_schema(schema)
        store.notion.query_database = Mock(return_value=[])
        store.notion.query_data_source = Mock(return_value=[])
        store.notion.create_page = Mock(return_value={"id": "page-1"})

        page_id, action = store.upsert(_make_record())

        self.assertEqual(action, "created")
        self.assertEqual(page_id, "page-1")
        sent_properties = store.notion.create_page.call_args.args[1]
        self.assertNotIn("On Task Minutes", sent_properties)
        self.assertNotIn("Productivity Score", sent_properties)
        self.assertIn("Focus Minutes", sent_properties)
        self.assertIn("Focus Score", sent_properties)


if __name__ == "__main__":
    unittest.main()
