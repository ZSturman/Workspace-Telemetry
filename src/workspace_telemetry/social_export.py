"""Redacted public evidence export consumed by Social Drafts."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .ingest import LEDGER_PATH
from .worklog import SENSITIVE


def _clean(value: Any, limit: int = 1400) -> str:
    value = SENSITIVE.sub("[REDACTED]", str(value or ""))
    return value.replace(str(Path.home()), "~")[:limit]


def _stamp(event: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "ts_iso", "ended_at", "endedAt", "start", "started_at"):
        raw = event.get(key)
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        except ValueError:
            pass
    if isinstance(event.get("ts"), (float, int)):
        return datetime.fromtimestamp(float(event["ts"]), tz=UTC)
    return None


def export_social_evidence(*, lookback_days: int, limit: int) -> dict[str, Any]:
    now = datetime.now(UTC)
    since = now - timedelta(days=max(1, lookback_days))
    records: list[dict[str, Any]] = []
    malformed = 0
    if LEDGER_PATH.exists():
        for raw in reversed(LEDGER_PATH.read_text(encoding="utf-8").splitlines()):
            try:
                envelope = json.loads(raw)
                event = envelope["payload"]
            except (ValueError, KeyError, TypeError):
                malformed += 1
                continue
            stamp = _stamp(event)
            if not stamp or stamp < since:
                continue
            kind = str(event.get("kind") or event.get("event_type") or envelope.get("source") or "activity")
            if kind == "task-lifecycle":
                kind = "session"
            records.append({
                "id": str(envelope.get("event_id")), "kind": kind, "timestamp": stamp.isoformat(),
                "project_key": str(event.get("project_id") or event.get("repo") or event.get("path") or "unassigned"),
                "project": _clean(event.get("project_name") or event.get("repo") or event.get("path") or "Unassigned", 180),
                "project_ids": [str(value) for value in event.get("project_ids", []) if str(value)],
                "facts": _clean(event.get("summary") or event.get("message") or event.get("path") or kind, 700),
                "detail": _clean(event.get("detail") or "", 800),
                "active_seconds": max(0, int(event.get("active_seconds") or 0)),
            })
            if len(records) >= max(1, limit):
                break
    session_path = Path.home() / "Library/Application Support/WorkspaceTelemetry/work-sessions/current-session.json"
    try:
        current_session = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current_session = {}
    return {"ok": True, "records": list(reversed(records)), "metadata": {"since": since.isoformat(), "malformed": malformed, "active_session": current_session.get("status") in ("active", "paused")}}
