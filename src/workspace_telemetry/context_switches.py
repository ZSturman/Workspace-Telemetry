"""Read-only Context Switches source adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .notion_api import number_value, plain_text

from .models import ContextSwitch, TimeRange


def fetch_context_switches(notion: Any, database_id: str, time_range: TimeRange) -> tuple[list[ContextSwitch], list[str]]:
    """Read source pages only; this module deliberately has no write operations."""

    try:
        pages = notion.query_database(
            database_id,
            filter_payload={
                "and": [
                    {"property": "Switched At", "date": {"on_or_after": time_range.start.isoformat()}},
                    {"property": "Switched At", "date": {"before": time_range.end.isoformat()}},
                ]
            },
            sorts=[{"property": "Switched At", "direction": "ascending"}],
        )
    except Exception as exc:
        return [], [f"Context Switches fetch failed: {exc}"]

    entries: dict[str, ContextSwitch] = {}
    errors: list[str] = []
    for page in pages:
        item = _normalize(page)
        if item is None:
            errors.append(f"Ignored malformed Context Switch page {page.get('id', '')}")
            continue
        key = item.external_id or item.source_hash
        current = entries.get(key)
        if current is None or item.seconds_in_previous_app > current.seconds_in_previous_app:
            entries[key] = item
    return sorted(entries.values(), key=lambda item: (item.switched_at, item.external_id)), errors


def _normalize(page: dict[str, Any]) -> ContextSwitch | None:
    timestamp = _parse_datetime(plain_text(page, "Switched At"))
    external_id = plain_text(page, "External ID").strip()
    source_hash = plain_text(page, "Source Hash").strip()
    if timestamp is None or not (external_id or source_hash):
        return None
    return ContextSwitch(
        external_id=external_id or source_hash,
        source_hash=source_hash,
        switched_at=timestamp,
        from_app=plain_text(page, "From App").strip() or "Unknown",
        to_app=plain_text(page, "To App").strip() or "Unknown",
        from_category=plain_text(page, "From Category").strip().casefold() or "other",
        to_category=plain_text(page, "To Category").strip().casefold() or "other",
        seconds_in_previous_app=max(0.0, number_value(page, "Seconds in Previous App") or 0.0),
        friction_weight=max(0.0, number_value(page, "Friction Weight") or 0.0),
    )


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
