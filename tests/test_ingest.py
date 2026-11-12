from __future__ import annotations

import json
from pathlib import Path

from workspace_telemetry import ingest
from workspace_telemetry import migration, social_export


def test_ingest_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "DATA_DIRECTORY", tmp_path)
    monkeypatch.setattr(ingest, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(ingest, "DEDUP_PATH", tmp_path / "dedup.json")
    source = tmp_path / "events.jsonl"
    source.write_text(json.dumps({"event_id": "one", "timestamp": "2026-09-12T12:00:00+00:00", "kind": "git"}) + "\n")
    assert ingest.ingest_jsonl(source, "workspace-watcher")["accepted"] == 1
    assert ingest.ingest_jsonl(source, "workspace-watcher")["duplicates"] == 1


def test_export_events_is_a_bounded_read_only_workspace_watcher_projection(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"event_id": "workspace-watcher:one", "source": "workspace-watcher", "payload": {"event_id": "one", "ts_iso": "2026-09-12T12:00:00+00:00", "type": "modified", "path": "Projects/Example/a.py", "project_id": "project-1", "project_name": "Example", "meaningful": True, "metadata": {"secret": "not exported"}}},
        {"event_id": "workspace-watcher:two", "source": "workspace-watcher", "payload": {"event_id": "two", "ts_iso": "2026-09-12T12:01:00+00:00", "type": "created", "path": "Projects/Other/b.py", "project_name": "Other", "meaningful": True}},
        {"event_id": "workspace-watcher:three", "source": "workspace-watcher", "payload": {"event_id": "three", "ts_iso": "2026-09-12T12:02:00+00:00", "type": "created", "path": "Projects/Example/c.py", "project_id": "project-1", "project_name": "Example", "meaningful": True}},
    ]
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = ingest.export_events(source="workspace-watcher", project_name="Example", limit=1, ledger_path=ledger)

    assert result["ok"] is True
    assert result["events"] == [{"event_id": "workspace-watcher:three", "timestamp": "2026-09-12T12:02:00+00:00", "type": "created", "path": "Projects/Example/c.py", "project_id": "project-1", "project_name": "Example", "meaningful": True, "raw_event_count": 1}]


def test_legacy_migration_keeps_archive_and_promotes_active_state(tmp_path, monkeypatch):
    source = tmp_path / "KioskTelemetry"
    source.mkdir()
    (source / "telemetry.sqlite").write_bytes(b"sqlite-state")
    monkeypatch.setattr(migration, "DATA_DIRECTORY", tmp_path / "WorkspaceTelemetry")
    result = migration.migrate_legacy(source)
    assert result["promoted"] == ["telemetry.sqlite"]
    assert (tmp_path / "WorkspaceTelemetry/legacy-kiosk-archive/telemetry.sqlite").read_bytes() == b"sqlite-state"
    assert (tmp_path / "WorkspaceTelemetry/telemetry.sqlite").read_bytes() == b"sqlite-state"
    assert migration.migrate_legacy(source)["copied"] == []


def test_social_export_redacts_secrets(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps({"event_id": "one", "source": "work-sessions", "payload": {"timestamp": "2099-01-01T00:00:00+00:00", "kind": "session", "summary": "token=private-value", "active_seconds": 360}}) + "\n")
    monkeypatch.setattr(social_export, "LEDGER_PATH", ledger)
    record = social_export.export_social_evidence(lookback_days=99999, limit=2)["records"][0]
    assert "private-value" not in record["facts"]
