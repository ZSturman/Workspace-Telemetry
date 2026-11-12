"""JSON CLI used by Hammerspoon's Work Session menu and watchers."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import load_work_session_settings
from .service import WorkSessionService


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m workspace_telemetry.work_sessions", description="Project-aware local work sessions.")
    root.add_argument("--settings-json", default="{}", help="Non-secret Hammerspoon settings JSON.")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh", help="Refresh the cached Notion Projects schema and project list.")
    form = commands.add_parser("form", help="Return the live project creation form metadata.")
    form.add_argument("--refresh", action="store_true")
    listing = commands.add_parser("list", help="List cached projects in recent/frequent order.")
    listing.add_argument("--query", default="")
    listing.add_argument("--limit", type=int, default=80)
    observe = commands.add_parser("observe", help="Resolve one Hammerspoon context observation.")
    observe.add_argument("--payload", required=True)
    suggest = commands.add_parser("suggest-classification", help="Return a confidence-aware, local classification suggestion.")
    suggest.add_argument("--payload", required=True)
    associate = commands.add_parser("associate", help="Persist a local file/path/repo/domain association.")
    associate.add_argument("--project-id", required=True)
    associate.add_argument("--kind", required=True, choices=("path", "file", "repo", "domain", "app", "alias"))
    associate.add_argument("--value", required=True)
    disassociate = commands.add_parser("disassociate", help="Remove an incorrect local association.")
    disassociate.add_argument("--project-id", required=True)
    disassociate.add_argument("--kind", required=True, choices=("path", "file", "repo", "domain", "app", "alias"))
    disassociate.add_argument("--value", required=True)
    create = commands.add_parser("create-project", help="Create a local project or create it in Notion immediately.")
    create.add_argument("--fields", required=True)
    create.add_argument("--notion", action="store_true")
    discard = commands.add_parser("discard-local-project", help="Discard a local-only project draft.")
    discard.add_argument("--project-id", required=True)
    start = commands.add_parser("start", help="Persist a new active session.")
    start.add_argument("--payload", required=True)
    commands.add_parser("status", help="Return current local session state.")
    commands.add_parser("pause", help="Pause current session telemetry.")
    commands.add_parser("resume", help="Resume current session telemetry.")
    change = commands.add_parser("change-projects", help="Change or add project relations for the active session.")
    change.add_argument("--payload", required=True)
    checkpoint = commands.add_parser("checkpoint", help="Continue or finish a session at a checkpoint.")
    checkpoint.add_argument("--payload", required=True)
    finish = commands.add_parser("finish", help="Finish a session and queue its human-confirmed Work Log.")
    finish.add_argument("--payload", required=True)
    reminders = commands.add_parser("reminders", help="Rank incomplete reminders for project IDs.")
    reminders.add_argument("--project-ids", required=True, help="JSON list")
    reminders.add_argument("--limit", type=int, default=12)
    commands.add_parser("flush", help="Retry durable queued Notion/EventKit writes.")
    summary = commands.add_parser("telemetry-summary", help="Return the current session's bounded local telemetry summary.")
    summary.add_argument("--ended-at", default="")
    return root


def _json(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return parsed


def _list(value: str, label: str) -> list[str]:
    try: parsed = json.loads(value)
    except json.JSONDecodeError as exc: raise ValueError(f"{label} must be valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, list): raise ValueError(f"{label} must be a JSON list.")
    return [str(item) for item in parsed if str(item).strip()]


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = load_work_session_settings(_json(args.settings_json, "settings"))
        service = WorkSessionService(settings)
        if args.command == "refresh": result = service.refresh_projects()
        elif args.command == "form":
            if args.refresh: service.refresh_projects()
            result = {"ok": True, "form": service.project_form(), "cache": service.cache_status()}
        elif args.command == "list": result = {"ok": True, **service.list_projects(args.query, args.limit)}
        elif args.command == "observe": result = service.observe(_json(args.payload, "payload"))
        elif args.command == "suggest-classification": result = {"ok": True, "suggestion": service.classification_suggestion(_json(args.payload, "payload"))}
        elif args.command == "associate": result = service.associate(args.project_id, args.kind, args.value)
        elif args.command == "disassociate": result = service.disassociate(args.project_id, args.kind, args.value)
        elif args.command == "create-project": result = service.create_project(_json(args.fields, "fields"), create_in_notion=args.notion)
        elif args.command == "discard-local-project": result = service.discard_local_project(args.project_id)
        elif args.command == "start": result = service.start_session(_json(args.payload, "payload"))
        elif args.command == "status": result = service.status()
        elif args.command == "pause": result = service.pause()
        elif args.command == "resume": result = service.resume()
        elif args.command == "change-projects":
            payload = _json(args.payload, "payload")
            result = service.change_projects(_list(json.dumps(payload.get("projectIds") or []), "projectIds"), primary_project_id=str(payload.get("primaryProjectId") or ""), action=str(payload.get("action") or "change"))
        elif args.command == "checkpoint": result = service.checkpoint(_json(args.payload, "payload"))
        elif args.command == "finish": result = service.finish_session(_json(args.payload, "payload"))
        elif args.command == "reminders": result = {"ok": True, "reminders": service.rank_reminders(_list(args.project_ids, "projectIds"), limit=args.limit)}
        elif args.command == "flush": result = service.flush()
        elif args.command == "telemetry-summary":
            session = service.current_session()
            result = {"ok": True, "summary": service.telemetry_summary(session, args.ended_at) if session else {}, "session": session}
        else: raise ValueError(f"Unsupported command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
