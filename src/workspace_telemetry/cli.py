"""CLI for scheduled and manual telemetry processing."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .notion_api import NotionClient

from .activitywatch import ActivityWatchClient
from .config import TelemetrySettings, load_settings
from .lm_studio import LMStudioClient, LMStudioError
from .models import TimeRange
from .pipeline import TelemetryPipeline
from .state import TelemetryState
from .worklog import WorklogPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m workspace_telemetry", description="Generate privacy-filtered personal telemetry.")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="Import an append-only producer JSONL file into the local ledger.")
    ingest.add_argument("--input", required=True, help="Absolute JSONL path written by a producer.")
    ingest.add_argument("--source", required=True, choices=("workspace-watcher", "terminal", "hammerspoon", "work-sessions", "task-management"))
    ingest.add_argument("--dry-run", action="store_true")
    export = sub.add_parser("export-events", help="Print a bounded, read-only normalized-event projection for a consumer.")
    export.add_argument("--source", required=True, choices=("workspace-watcher", "terminal", "hammerspoon", "work-sessions", "task-management"))
    export.add_argument("--project-name", default="", help="Exact Workspace Watcher project name; empty returns all events for the source.")
    export.add_argument("--limit", type=int, default=20)
    legacy = sub.add_parser("migrate-legacy", help="Copy legacy KioskTelemetry data into WorkspaceTelemetry without deleting it.")
    legacy.add_argument("--source", default=str(Path.home() / "Library/Application Support/KioskTelemetry"))
    legacy.add_argument("--dry-run", action="store_true")
    evidence = sub.add_parser("export-social-evidence", help="Print redacted social-draft evidence JSON.")
    evidence.add_argument("--lookback-days", type=int, default=3)
    evidence.add_argument("--limit", type=int, default=250)
    session = sub.add_parser("session", help="Delegate a work-session command to the standalone session service.")
    session.add_argument("arguments", nargs=argparse.REMAINDER)
    tick = sub.add_parser("tick", help="Process pending intervals and completed rollups.")
    tick.add_argument("--dry-run", action="store_true", help="Calculate pending work without writes.")
    tick.add_argument("--force", action="store_true", help="Regenerate matching strategy-period rows.")
    tick.add_argument("--strategy", action="append", help="Limit processing to a strategy ID; may be repeated.")
    tick.add_argument(
        "--scheduled",
        action="store_true",
        help="Bounded production mode: recent interval, rolling recap, and missing completed day.",
    )
    sub.add_parser("doctor", help="Report Notion, ActivityWatch, and LM Studio availability.")
    bootstrap = sub.add_parser("bootstrap", help="Seed recent, rolling-24h, and completed-day production views.")
    bootstrap.add_argument("--days", type=int, default=7, help="Completed daily rows to seed (default: 7).")
    bootstrap.add_argument("--dry-run", action="store_true")
    bootstrap.add_argument("--force", action="store_true")
    migrate = sub.add_parser("migrate", help="Add the validated Display Data property to Telemetry Summaries.")
    migrate.add_argument("--dry-run", action="store_true")
    worklog = sub.add_parser("worklog", help="Collect local evidence and consolidate Notion Work Logs.")
    worklog_sub = worklog.add_subparsers(dest="worklog_command", required=True)
    for name, help_text in (
        ("doctor", "Report local source and Notion schema availability."),
        ("sync", "Incrementally reconcile the current local day."),
        ("rebuild", "Rebuild one local calendar day from local evidence."),
        ("dry-run", "Rebuild one local day without writing Notion or checkpoints."),
        ("reconcile", "Process one retained ActivityWatch history day."),
        ("watch-roots", "Write the current filesystem watch-root manifest."),
        ("next-steps", "Sync each project's docs/NEXT_STEPS.md onto its Notion page."),
        ("analyze-next-work", "Generate Next Work Details via LM Studio for checked Notion Projects."),
        ("prune", "Apply raw evidence retention now; --dry-run only counts rows."),
        ("compact", "VACUUM the local state database and enable incremental auto-vacuum."),
    ):
        command = worklog_sub.add_parser(name, help=help_text)
        if name in {"sync", "rebuild", "dry-run"}:
            command.add_argument("--date", help="Local ISO date; defaults to today.")
        if name in {"sync", "rebuild", "reconcile", "watch-roots", "next-steps", "analyze-next-work", "prune"}:
            command.add_argument("--dry-run", action="store_true")
    _period_command(sub, "interval", "Summarize the previous closed 30-minute interval.")
    _period_command(sub, "hour", "Summarize the previous closed hour.")
    _period_command(sub, "today", "Summarize today through the current time.")
    _period_command(sub, "yesterday", "Summarize yesterday.")
    daily = _period_command(sub, "daily", "Summarize a completed calendar day.")
    daily.add_argument("--date", help="Local ISO date; defaults to yesterday.")
    weekly = _period_command(sub, "weekly", "Summarize a completed calendar week.")
    weekly.add_argument("--week", help="ISO date within the desired week; defaults to last completed week.")
    ranged = _period_command(sub, "range", "Summarize a custom ISO datetime range.")
    ranged.add_argument("--start", required=True)
    ranged.add_argument("--end", required=True)
    ranged.add_argument("--cadence", default="interval", choices=("interval", "hour", "day", "week"))
    compare = _period_command(sub, "compare", "Run strategies and persist a comparison record.")
    compare.add_argument("--start", required=True)
    compare.add_argument("--end", required=True)
    compare.add_argument("--cadence", default="interval", choices=("interval", "hour", "day", "week"))
    regenerate = _period_command(sub, "regenerate", "Force-update one strategy-period summary.")
    regenerate.add_argument("--start", required=True)
    regenerate.add_argument("--end", required=True)
    regenerate.add_argument("--cadence", required=True, choices=("interval", "hour", "day", "week"))
    for action in regenerate._actions:
        if "--strategy" in action.option_strings:
            action.required = True
    return parser


def _period_command(sub: Any, name: str, help_text: str) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help=help_text)
    parser.add_argument("--dry-run", action="store_true", help="Calculate and validate without writing Notion or checkpoint state.")
    parser.add_argument("--force", action="store_true", help="Regenerate matching strategy-period rows.")
    parser.add_argument("--strategy", action="append", help="Limit processing to a strategy ID; may be repeated.")
    if name in {"interval", "hour"}:
        parser.add_argument("--previous", action="store_true", help="Compatibility flag; these commands always use the previous closed period.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "ingest":
            from .ingest import ingest_jsonl
            print(json.dumps(ingest_jsonl(Path(args.input), args.source, dry_run=args.dry_run), indent=2, sort_keys=True))
            return 0
        if args.command == "export-events":
            from .ingest import export_events
            print(json.dumps(export_events(source=args.source, project_name=args.project_name, limit=args.limit), indent=2, sort_keys=True))
            return 0
        if args.command == "migrate-legacy":
            from .migration import migrate_legacy
            print(json.dumps(migrate_legacy(Path(args.source), dry_run=args.dry_run), indent=2, sort_keys=True))
            return 0
        if args.command == "export-social-evidence":
            from .social_export import export_social_evidence
            print(json.dumps(export_social_evidence(lookback_days=args.lookback_days, limit=args.limit), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "session":
            from .work_sessions.cli import main as session_main
            return session_main(args.arguments)
        settings = load_settings()
        if args.command == "doctor":
            print(json.dumps(doctor(settings), indent=2, sort_keys=True))
            return 0
        if args.command == "worklog":
            dry_run = bool(getattr(args, "dry_run", False) or args.worklog_command == "dry-run")
            # A prune dry run counts rows in the real database instead of an empty in-memory one.
            state = TelemetryState(":memory:") if dry_run and args.worklog_command != "prune" else None
            pipeline = WorklogPipeline(settings, _notion(settings), state=state)
            try:
                day = _parse_day(args.date, settings) if getattr(args, "date", None) else None
                if args.worklog_command == "doctor": result = pipeline.doctor()
                elif args.worklog_command == "sync": result = pipeline.sync(day=day, dry_run=dry_run)
                elif args.worklog_command in {"rebuild", "dry-run"}: result = pipeline.sync(day=day, dry_run=dry_run)
                elif args.worklog_command == "reconcile": result = pipeline.reconcile(dry_run=dry_run)
                elif args.worklog_command == "next-steps": result = {"ok": True, "nextSteps": pipeline.sync_next_steps(dry_run=dry_run)}
                elif args.worklog_command == "analyze-next-work": result = {"ok": True, "nextWorkAnalysis": pipeline.analyze_next_work(dry_run=dry_run)}
                elif args.worklog_command == "prune": result = {"ok": True, "dryRun": dry_run, "removed": pipeline.state.prune_worklog_events(datetime.now(settings.zoneinfo()), dry_run=dry_run)}
                elif args.worklog_command == "compact": pipeline.state.compact(); result = {"ok": True}
                else: result = pipeline.write_watch_manifest(dry_run=dry_run)
                print(json.dumps(result, indent=2, sort_keys=True))
                return 0
            finally:
                pipeline.close()
        pipeline = _pipeline(settings, dry_run=bool(getattr(args, "dry_run", False)))
        try:
            if args.command == "tick":
                result = pipeline.tick(
                    dry_run=args.dry_run,
                    requested_strategies=args.strategy,
                    force=args.force,
                    scheduled=args.scheduled,
                )
            elif args.command == "bootstrap":
                result = pipeline.bootstrap(
                    days=max(1, min(31, args.days)), dry_run=args.dry_run, force=args.force
                )
            elif args.command == "migrate":
                result = pipeline.store.migrate_schema(dry_run=args.dry_run)
            else:
                time_range, cadence = _time_range(args.command, args, settings)
                if args.command == "compare":
                    result = pipeline.compare(time_range, cadence, requested_strategies=args.strategy, dry_run=args.dry_run)
                else:
                    result = pipeline.run(
                        time_range,
                        cadence,
                        requested_strategies=args.strategy,
                        dry_run=args.dry_run,
                        force=args.force or args.command == "regenerate",
                    )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        finally:
            pipeline.close()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1


def doctor(settings: TelemetrySettings) -> dict[str, Any]:
    result: dict[str, Any] = {
        "notion": {"database": settings.summaries_database_id, "ok": False},
        "activityWatch": {"baseUrl": settings.activitywatch_base_url, "ok": False},
        "lmStudio": {"endpoint": settings.lm_studio_endpoint, "ok": False},
    }
    notion = _notion(settings)
    try:
        source = notion.get_data_source(notion.data_source_id(settings.summaries_database_id))
        display_type = ((source.get("properties") or {}).get("Display Data") or {}).get("type")
        result["notion"].update({
            "ok": display_type == "rich_text",
            "displayData": display_type or "missing",
        })
        if display_type != "rich_text":
            result["notion"]["error"] = "Display Data rich-text property is missing; run telemetry migrate."
    except Exception as exc:
        result["notion"]["error"] = str(exc)
    snapshot = ActivityWatchClient(settings.activitywatch_base_url).collect(
        TimeRange(_now(settings) - timedelta(minutes=1), _now(settings)), settings.privacy
    )
    result["activityWatch"].update({"ok": snapshot.available, "buckets": list(snapshot.bucket_ids), "errors": snapshot.errors})
    try:
        client = LMStudioClient(
            settings.lm_studio_endpoint, settings.lm_studio_model,
            timeout_seconds=settings.lm_studio_timeout_seconds, retries=0,
            response_format=settings.lm_studio_response_format,
        )
        endpoint, model = client.discover()
        result["lmStudio"].update({
            "ok": True, "endpoint": endpoint, "model": model,
            "responseFormat": settings.lm_studio_response_format,
            "schemaProbeRecommended": settings.lm_studio_response_format == "json_schema",
        })
        if settings.lm_studio_response_format == "json_schema":
            try:
                client.summarize({
                    "reportingWindow": {"start": "probe", "end": "probe", "minutes": 0},
                    "coverage": {"activityWatch": False, "foregroundWindows": False, "afk": False, "limitations": ["Doctor probe only."]},
                    "sessionTotals": {"activeMinutes": 0}, "topActivity": [],
                    "scores": {"productivity": None, "label": "Insufficient activity/data"},
                    "switching": {"count": 0}, "notableWorkBlocks": [], "priorPeriodDeltas": {},
                })
                result["lmStudio"]["schemaProbe"] = {"ok": True}
            except Exception as exc:
                result["lmStudio"]["schemaProbe"] = {"ok": False, "error": str(exc)}
    except LMStudioError as exc:
        result["lmStudio"]["error"] = str(exc)
    return result


def _pipeline(settings: TelemetrySettings, *, dry_run: bool = False) -> TelemetryPipeline:
    from .state import TelemetryState

    state = TelemetryState(":memory:") if dry_run else None
    return TelemetryPipeline(settings, notion=_notion(settings), state=state)


def _notion(settings: TelemetrySettings) -> NotionClient:
    return NotionClient(
        settings.notion_api_key,
        base_url=settings.notion_base_url,
        notion_version=settings.notion_version,
    )


def _time_range(command: str, args: Any, settings: TelemetrySettings) -> tuple[TimeRange, str]:
    now = _now(settings)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if command == "interval":
        end = _floor(now - timedelta(minutes=settings.late_arrival_minutes), 30)
        return TimeRange(end - timedelta(minutes=30), end), "interval"
    if command == "hour":
        end = (now - timedelta(minutes=settings.late_arrival_minutes)).replace(minute=0, second=0, microsecond=0)
        return TimeRange(end - timedelta(hours=1), end), "hour"
    if command == "today":
        return TimeRange(day_start, now), "day"
    if command == "yesterday":
        return TimeRange(day_start - timedelta(days=1), day_start), "day"
    if command == "daily":
        start = _parse_day(args.date, settings) if args.date else day_start - timedelta(days=1)
        return TimeRange(start, start + timedelta(days=1)), "day"
    if command == "weekly":
        reference = _parse_day(args.week, settings) if args.week else day_start - timedelta(days=1)
        start = reference - timedelta(days=reference.weekday())
        return TimeRange(start, start + timedelta(days=7)), "week"
    if command in {"range", "compare", "regenerate"}:
        return TimeRange(_parse_datetime(args.start, settings), _parse_datetime(args.end, settings)), args.cadence
    raise ValueError(f"Unsupported command {command}")


def _parse_day(value: str, settings: TelemetrySettings) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=settings.zoneinfo(), hour=0, minute=0, second=0, microsecond=0)


def _parse_datetime(value: str, settings: TelemetrySettings) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=settings.zoneinfo()) if parsed.tzinfo is None else parsed.astimezone(settings.zoneinfo())


def _now(settings: TelemetrySettings) -> datetime:
    return datetime.now(settings.zoneinfo())


def _floor(value: datetime, minutes: int) -> datetime:
    return value.replace(minute=value.minute - value.minute % minutes, second=0, microsecond=0)
