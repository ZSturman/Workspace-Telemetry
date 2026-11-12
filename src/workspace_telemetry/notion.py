"""Telemetry Summaries-only persistence adapter."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .notion_api import (
    date_property,
    multi_select_property,
    number_property,
    relation_property,
    rich_text_property,
    select_property,
    title_property,
)

from .models import TelemetryRecord


REQUIRED_PROPERTIES = {
    "Name": "title",
    "Period Start": "date",
    "Period End": "date",
    "Summary": "rich_text",
    "Raw Data": "rich_text",
    "Source Hash": "rich_text",
    "Status": "select",
    "Display Data": "rich_text",
    "Focus Minutes": "number",
    "Focus Score": "number",
}


class TelemetrySummariesStore:
    def __init__(self, notion: Any, database_id: str) -> None:
        self.notion = notion
        self.database_id = database_id
        self.data_source_id = notion.data_source_id(database_id)
        self._validated = False

    def validate_schema(self) -> None:
        if self._validated:
            return
        properties = self.notion.get_data_source(self.data_source_id).get("properties", {})
        invalid = [
            f"{name} (expected {kind}, found {(properties.get(name) or {}).get('type', 'missing')})"
            for name, kind in REQUIRED_PROPERTIES.items()
            if properties.get(name, {}).get("type") != kind
        ]
        if invalid:
            raise RuntimeError("Telemetry Summaries schema is incompatible: " + ", ".join(invalid))
        self._validated = True

    def migrate_schema(self, *, dry_run: bool = False) -> dict[str, Any]:
        properties = self.notion.get_data_source(self.data_source_id).get("properties", {})
        existing = (properties.get("Display Data") or {}).get("type")
        if existing == "rich_text":
            self._validated = True
            return {"changed": False, "property": "Display Data", "type": "rich_text"}
        if existing:
            raise RuntimeError(f"Display Data exists with incompatible type {existing}.")
        if not dry_run:
            self.notion.update_database(self.database_id, {"Display Data": {"rich_text": {}}})
            self._validated = False
            self.validate_schema()
        return {"changed": True, "dryRun": dry_run, "property": "Display Data", "type": "rich_text"}

    def upsert(self, record: TelemetryRecord, *, dry_run: bool = False) -> tuple[str, str]:
        self.validate_schema()
        title = summary_title(record)
        properties = record_properties(record, title)
        existing = self._find_by_title(title)
        if dry_run:
            return (str(existing[0].get("id") or "") if existing else "", "would-update" if existing else "would-create")
        if existing:
            page_id = str(existing[0].get("id") or "")
            self.notion.update_page(page_id, properties)
            return page_id, "updated"
        page = self.notion.create_page(self.data_source_id, properties)
        return str(page.get("id") or ""), "created"

    def upsert_comparison(
        self,
        *,
        title: str,
        start: str,
        end: str,
        source_hash: str,
        raw_data: dict[str, Any],
        summary: str,
        dry_run: bool = False,
    ) -> tuple[str, str]:
        self.validate_schema()
        properties = {
            "Name": title_property(title),
            "Period Start": date_property(start),
            "Period End": date_property(end),
            "Summary": rich_text_property(summary),
            "Raw Data": rich_text_property(json.dumps(raw_data, sort_keys=True, separators=(",", ":"))),
            "Source Hash": rich_text_property(source_hash),
            "Status": select_property("Generated"),
            "Summary Version": number_property(1),
        }
        existing = self._find_by_title(title)
        if dry_run:
            return "", "would-update" if existing else "would-create"
        if existing:
            page_id = str(existing[0].get("id") or "")
            self.notion.update_page(page_id, properties)
            return page_id, "updated"
        page = self.notion.create_page(self.data_source_id, properties)
        return str(page.get("id") or ""), "created"

    def _find_by_title(self, title: str) -> list[dict[str, Any]]:
        """Defend against Notion API variants that ignore a title filter."""

        filtered = self.notion.query_database(
            self.data_source_id,
            filter_payload={"property": "Name", "title": {"equals": title}},
            page_size=10,
        )
        matches = [page for page in filtered if _page_text(page, "Name") == title]
        if matches:
            return matches
        # The legacy endpoint used by the existing kiosk client has occasionally
        # returned unfiltered pages for title equality. A local exact check avoids
        # overwriting a different strategy's deterministic row.
        pages = self.notion.query_data_source(self.data_source_id)
        return [page for page in pages if _page_text(page, "Name") == title]


def summary_title(record: TelemetryRecord) -> str:
    if record.strategy.id == "rolling-24h-human-v2":
        return "Telemetry | rolling | rolling-24h-human-v2"
    start = record.time_range.start.strftime("%Y-%m-%d %H:%M %z")
    return f"Telemetry | {record.strategy.cadence} | {start} | {record.strategy.id}"


def record_properties(record: TelemetryRecord, title: str) -> dict[str, Any]:
    metrics = record.metrics
    content = record.generation.content
    raw_data = dict(record.raw_data)
    raw_data["summaryKey"] = record.key
    raw_data["status"] = record.status
    raw_data["metrics"] = metrics.as_dict()
    raw_data["generation"] = {
        "usedLmStudio": record.generation.used_lm_studio,
        "endpoint": record.generation.endpoint,
        "model": record.generation.model,
        "error": record.generation.error,
    }
    properties: dict[str, Any] = {
        "Name": title_property(title),
        "Period Start": date_property(record.time_range.start.isoformat()),
        "Period End": date_property(record.time_range.end.isoformat()),
        "Active Minutes": number_property(metrics.active_minutes),
        "Idle Minutes": number_property(metrics.idle_minutes),
        "Focus Minutes": number_property(metrics.on_task_minutes),
        "Focus Score": number_property(metrics.productivity_score),
        "Friction Score": number_property(metrics.friction_score),
        "Context Switches": number_property(metrics.context_switches),
        "Source Event Count": number_property(metrics.source_event_count),
        "Applications": multi_select_property(list(metrics.applications)[:100]),
        "Activity Categories": multi_select_property(list(metrics.categories)[:100]),
        "Websites": multi_select_property(list(metrics.domains)[:100]),
        "ActivityWatch Buckets": multi_select_property(list(metrics.activitywatch_buckets)[:100]),
        "Projects": multi_select_property(list(dict.fromkeys([*metrics.projects, *content.projects]))[:100]),
        "Topics": multi_select_property(list(content.topics)[:100]),
        "People / Organizations": multi_select_property(list(content.people_organizations)[:100]),
        "Dominant App": select_property(metrics.dominant_app),
        "Dominant Category": select_property(metrics.dominant_category),
        "Primary Activity": select_property(content.primary_activity or metrics.dominant_category),
        "Summary": rich_text_property(content.recap or content.summary),
        "Evidence": rich_text_property(" ".join(filter(None, (
            content.on_task_assessment, content.switching_assessment, content.distraction_assessment,
        ))) or " ".join(content.observations)),
        "Work Completed": rich_text_property(content.work_completed),
        "Work In Progress": rich_text_property(content.progress_assessment or content.work_in_progress),
        "Next Likely Action": rich_text_property(content.next_likely_action),
        "Confidence": number_property(round(content.confidence, 3)),
        "Context Switch IDs": rich_text_property(",".join(metrics.context_switch_ids)),
        "Source Hash": rich_text_property(record.source_hash),
        "Display Data": rich_text_property(json.dumps(_display_data(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False)),
        "Raw Data": rich_text_property(json.dumps(raw_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)),
        "Status": select_property(record.status),
        "Summary Version": number_property(record.strategy.version),
        "Generated At": date_property(datetime.now(record.time_range.start.tzinfo).isoformat()),
    }
    if record.previous_page_id:
        properties["Previous Summary"] = relation_property([record.previous_page_id])
    return properties


def _display_data(record: TelemetryRecord) -> dict[str, Any]:
    """Versioned, bounded JSON that is the kiosk's only detailed data source."""

    metrics = record.metrics
    content = record.generation.content
    active = max(metrics.active_seconds, 1.0)
    prompt_coverage = metrics.as_prompt_dict()["coverage"]

    def ranked(values: dict[str, float], limit: int = 8) -> list[dict[str, Any]]:
        return [
            {
                "name": str(name)[:120],
                "minutes": round(seconds / 60, 1),
                "activeShare": round(seconds / active * 100, 1),
            }
            for name, seconds in sorted(values.items(), key=lambda item: (-item[1], item[0].casefold()))[:limit]
            if name and seconds >= 0
        ]

    return {
        "schemaVersion": 1,
        "kind": (
            "recent" if record.strategy.id == "interval-production-v2" else
            "day" if record.strategy.id == "rolling-24h-human-v2" else "week"
        ),
        "strategy": record.strategy.id,
        "period": {
            "start": record.time_range.start.isoformat(),
            "end": record.time_range.end.isoformat(),
        },
        "sourceStatus": record.status,
        "metrics": {
            "activeMinutes": metrics.active_minutes,
            "onTaskMinutes": metrics.on_task_minutes,
            "neutralMinutes": metrics.neutral_minutes,
            "distractionMinutes": metrics.distraction_minutes,
            "idleMinutes": metrics.idle_minutes,
            "excludedMinutes": metrics.excluded_minutes,
            "sessions": metrics.session_count,
            "contextSwitches": metrics.context_switches,
            "switchesPerActiveHour": round(metrics.switches_per_active_hour, 1),
            "medianDwellSeconds": round(metrics.median_dwell_seconds, 1),
            "shortDwellVisits": metrics.short_dwell_visits,
            "bounceBacks": metrics.bounce_backs,
            "productiveShare": round(metrics.productive_share, 1),
            "distractionShare": round(metrics.distraction_share, 1),
            "stabilityScore": round(metrics.stability_score, 1),
            "onTaskScore": None if metrics.on_task_score is None else round(metrics.on_task_score, 1),
            "scoreLabel": metrics.score_label,
            "scoreReason": metrics.score_reason[:240],
        },
        "apps": ranked(metrics.applications),
        "domains": ranked(metrics.domains),
        "projects": ranked(metrics.projects),
        "transitions": [
            {"from": source[:120], "to": target[:120], "count": count}
            for source, target, count in metrics.top_transitions[:5]
        ],
        "narrative": {
            "headline": (content.headline or content.summary)[:180],
            "recap": (content.recap or content.summary)[:900],
            "onTaskAssessment": content.on_task_assessment[:500],
            "switchingAssessment": content.switching_assessment[:500],
            "distractionAssessment": content.distraction_assessment[:500],
            "progressAssessment": (content.progress_assessment or content.work_in_progress)[:700],
            "confidence": round(content.confidence, 3),
        },
        "coverage": {
            "activityWatch": bool(prompt_coverage["activityWatch"]),
            "foregroundWindows": bool(prompt_coverage["foregroundWindows"]),
            "afk": bool(prompt_coverage["afk"]),
            "contextSwitches": bool(prompt_coverage["contextSwitches"]),
            "limitations": [str(item)[:180] for item in prompt_coverage["limitations"][:6]],
        },
    }


def _page_text(page: dict[str, Any], property_name: str) -> str:
    prop = page.get("properties", {}).get(property_name, {})
    values = prop.get("title") or prop.get("rich_text") or []
    return "".join(
        str(item.get("plain_text") or (item.get("text") or {}).get("content") or "")
        for item in values
        if isinstance(item, dict)
    )
