"""Small local checkpoint database with no raw telemetry payloads."""

from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


# Raw worklog evidence retention by kind. Filesystem events are ~90% of rows and
# matter only for the day being synced; git/AI events are kept indefinitely.
WORKLOG_RETENTION_DAYS: dict[str, int] = {"filesystem": 14, "activity": 90, "afk": 90, "terminal": 90}


@dataclass(frozen=True)
class StoredSummary:
    key: str
    cadence: str
    strategy_id: str
    period_start: str
    period_end: str
    page_id: str
    source_hash: str
    status: str


class TelemetryState:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS summaries (
                key TEXT PRIMARY KEY, cadence TEXT NOT NULL, strategy_id TEXT NOT NULL,
                period_start TEXT NOT NULL, period_end TEXT NOT NULL, page_id TEXT NOT NULL,
                source_hash TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS summaries_previous ON summaries(strategy_id, cadence, period_start)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS watermarks (cadence TEXT PRIMARY KEY, ended_at TEXT NOT NULL)"
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS worklog_events (
                event_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
            )"""
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS worklog_events_time ON worklog_events(started_at, ended_at)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS worklog_checkpoints (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def get(self, key: str) -> StoredSummary | None:
        row = self.connection.execute(
            "SELECT key,cadence,strategy_id,period_start,period_end,page_id,source_hash,status FROM summaries WHERE key=?", (key,)
        ).fetchone()
        return StoredSummary(*row) if row else None

    def previous(self, strategy_id: str, cadence: str, start: datetime) -> StoredSummary | None:
        row = self.connection.execute(
            """SELECT key,cadence,strategy_id,period_start,period_end,page_id,source_hash,status
               FROM summaries WHERE strategy_id=? AND cadence=? AND period_start<? AND page_id!=''
               ORDER BY period_start DESC LIMIT 1""",
            (strategy_id, cadence, start.isoformat()),
        ).fetchone()
        return StoredSummary(*row) if row else None

    def save(self, item: StoredSummary) -> None:
        self.connection.execute(
            """INSERT INTO summaries(key,cadence,strategy_id,period_start,period_end,page_id,source_hash,status,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
               page_id=excluded.page_id, source_hash=excluded.source_hash, status=excluded.status, updated_at=excluded.updated_at""",
            (*item.__dict__.values(), datetime.now().isoformat()),
        )
        self.connection.commit()

    def watermark(self, cadence: str) -> datetime | None:
        row = self.connection.execute("SELECT ended_at FROM watermarks WHERE cadence=?", (cadence,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def save_watermark(self, cadence: str, ended_at: datetime) -> None:
        self.connection.execute(
            "INSERT INTO watermarks(cadence,ended_at) VALUES(?,?) ON CONFLICT(cadence) DO UPDATE SET ended_at=excluded.ended_at",
            (cadence, ended_at.isoformat()),
        )
        self.connection.commit()

    def record_worklog_event(self, event_hash: str, kind: str, started_at: datetime, ended_at: datetime, payload: dict) -> bool:
        """Persist privacy-filtered evidence exactly once in the local WAL DB."""

        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO worklog_events(event_hash,kind,started_at,ended_at,payload,created_at)
               VALUES(?,?,?,?,?,?)""",
            (event_hash, kind, started_at.isoformat(), ended_at.isoformat(), json.dumps(payload, sort_keys=True), datetime.now().isoformat()),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def worklog_events(self, start: datetime, end: datetime, *, kinds: tuple[str, ...] = ()) -> list[dict]:
        clauses = ["ended_at>?", "started_at<?"]
        values: list[object] = [start.isoformat(), end.isoformat()]
        if kinds:
            clauses.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
            values.extend(kinds)
        rows = self.connection.execute(
            "SELECT event_hash,kind,started_at,ended_at,payload FROM worklog_events WHERE " + " AND ".join(clauses) + " ORDER BY started_at,event_hash",
            values,
        ).fetchall()
        return [
            {"event_hash": row[0], "kind": row[1], "started_at": row[2], "ended_at": row[3], "payload": json.loads(row[4])}
            for row in rows
        ]

    def prune_worklog_events(self, now: datetime, retention_days: dict[str, int] | None = None, *, dry_run: bool = False) -> dict[str, int]:
        """Delete raw evidence older than its kind's retention window.

        Rows are only re-read through `record_worklog_event`'s de-duplication for
        the day being synced; Notion holds the durable Work Logs. Kinds without a
        window (git, ai) are kept: they gate Notion writes and are small.
        """
        removed: dict[str, int] = {}
        for kind, days in (WORKLOG_RETENTION_DAYS if retention_days is None else retention_days).items():
            cutoff = (now - timedelta(days=days)).isoformat()
            where = "kind=? AND ended_at<?"
            if dry_run:
                removed[kind] = self.connection.execute(f"SELECT count(*) FROM worklog_events WHERE {where}", (kind, cutoff)).fetchone()[0]
            else:
                removed[kind] = self.connection.execute(f"DELETE FROM worklog_events WHERE {where}", (kind, cutoff)).rowcount
        if not dry_run:
            self.connection.commit()
            # Return freed pages to the filesystem when the database was converted
            # to incremental auto-vacuum (see `compact`); otherwise they are reused.
            self.connection.execute("PRAGMA incremental_vacuum").fetchall()  # frees one page per step
            self.connection.commit()
        return removed

    def compact(self) -> None:
        """One-off VACUUM that also enables incremental auto-vacuum for later prunes."""
        self.connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.connection.execute("VACUUM")

    def worklog_checkpoint(self, key: str) -> str:
        row = self.connection.execute("SELECT value FROM worklog_checkpoints WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else ""

    def save_worklog_checkpoint(self, key: str, value: str) -> None:
        self.connection.execute(
            """INSERT INTO worklog_checkpoints(key,value,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (key, value, datetime.now().isoformat()),
        )
        self.connection.commit()
