"""Non-destructive import of the retired KioskTelemetry state directory."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .ingest import DATA_DIRECTORY


LEGACY_FILES = (
    "telemetry.sqlite", "fs-events.jsonl", "terminal-events.jsonl", "watch-roots.json",
    "work-sessions/project-map.json", "work-sessions/projects-cache.json",
    "work-sessions/current-session.json", "work-sessions/pending-operations.json",
    "work-sessions/session-telemetry.jsonl",
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migrate_legacy(source: Path, *, dry_run: bool = False) -> dict[str, Any]:
    if not source.is_absolute():
        raise ValueError("--source must be an absolute path")
    if not source.is_dir():
        raise FileNotFoundError(source)
    target_root = DATA_DIRECTORY / "legacy-kiosk-archive"
    copied, already_present, missing, promoted = [], [], [], []
    for relative in LEGACY_FILES:
        old = source / relative
        if not old.exists():
            missing.append(relative)
            continue
        new = target_root / relative
        if new.exists() and _digest(old) == _digest(new):
            already_present.append(relative)
            continue
        copied.append(relative)
        if not dry_run:
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new)
    # The archive is immutable provenance; the same files seed active state on
    # first migration so current checkpoint/session history is not discarded.
    for relative in LEGACY_FILES:
        archived = target_root / relative
        active = DATA_DIRECTORY / relative
        if not archived.exists() or active.exists():
            continue
        promoted.append(relative)
        if not dry_run:
            active.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archived, active)
    manifest: dict[str, Any] = {"source": str(source), "copied": copied, "alreadyPresent": already_present, "promoted": promoted, "missing": missing}
    if not dry_run:
        target_root.mkdir(parents=True, exist_ok=True)
        (target_root / "migration-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "dryRun": dry_run, **manifest, "target": str(target_root)}
