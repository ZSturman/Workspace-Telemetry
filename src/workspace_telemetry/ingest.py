"""Stable append-only producer ingress for Workspace Telemetry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


DATA_DIRECTORY = Path.home() / "Library/Application Support/WorkspaceTelemetry"
INBOX_DIRECTORY = DATA_DIRECTORY / "inbox"
LEDGER_PATH = DATA_DIRECTORY / "ledger.jsonl"
DEDUP_PATH = DATA_DIRECTORY / "ingest-dedup.json"


def _event_id(event: dict[str, Any], source: str) -> str:
    supplied = str(event.get("event_id") or event.get("id") or "").strip()
    if supplied:
        return f"{source}:{supplied}"
    encoded = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"{source}:sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _load_seen() -> set[str]:
    try:
        value = json.loads(DEDUP_PATH.read_text(encoding="utf-8"))
        return set(value.get("eventIds") or []) if isinstance(value, dict) else set()
    except (OSError, ValueError):
        return set()


def ingest_jsonl(input_path: Path, source: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Ingest valid JSON objects once. Invalid rows are reported, never guessed."""

    if not input_path.is_absolute():
        raise ValueError("--input must be an absolute path")
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    seen, accepted, duplicate, invalid, envelopes = _load_seen(), 0, 0, 0, []
    for line_number, raw in enumerate(input_path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if not isinstance(event, dict):
            invalid += 1
            continue
        event_id = _event_id(event, source)
        if event_id in seen:
            duplicate += 1
            continue
        seen.add(event_id)
        envelopes.append({"event_id": event_id, "source": source, "payload": event, "input_line": line_number})
        accepted += 1
    if not dry_run and envelopes:
        DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
        with LEDGER_PATH.open("a", encoding="utf-8") as target:
            for envelope in envelopes:
                target.write(json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n")
        DEDUP_PATH.write_text(json.dumps({"eventIds": sorted(seen)}, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "source": source, "input": str(input_path), "accepted": accepted, "duplicates": duplicate, "invalid": invalid, "dryRun": dry_run}


def export_events(
    *, source: str, project_name: str = "", limit: int = 20, ledger_path: Path = LEDGER_PATH
) -> dict[str, Any]:
    """Return a bounded, read-only projection of normalized producer events.

    Consumers use this export instead of opening Telemetry's private stores.
    The projection intentionally excludes arbitrary producer metadata, which
    may carry information unnecessary for task planning.
    """

    if not ledger_path.exists():
        return {"ok": True, "source": source, "projectName": project_name, "events": []}
    requested_name = project_name.casefold().strip()
    matches: list[dict[str, Any]] = []
    with ledger_path.open(encoding="utf-8") as source_file:
        for raw in source_file:
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(envelope, dict) or envelope.get("source") != source:
                continue
            payload = envelope.get("payload")
            if not isinstance(payload, dict):
                continue
            event_project = str(payload.get("project_name") or "").strip()
            if requested_name and event_project.casefold() != requested_name:
                continue
            matches.append(
                {
                    "event_id": str(envelope.get("event_id") or ""),
                    "timestamp": str(payload.get("ts_iso") or payload.get("timestamp") or ""),
                    "type": str(payload.get("type") or payload.get("kind") or ""),
                    "path": str(payload.get("path") or ""),
                    "project_id": str(payload.get("project_id") or ""),
                    "project_name": event_project,
                    "meaningful": bool(payload.get("meaningful", True)),
                    "raw_event_count": int(payload.get("raw_event_count") or 1),
                }
            )
    bounded = max(1, min(limit, 100))
    return {
        "ok": True,
        "source": source,
        "projectName": project_name,
        "events": list(reversed(matches[-bounded:])),
    }
