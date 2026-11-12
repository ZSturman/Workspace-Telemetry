"""Local project matching, session lifecycle, and queued Notion writes."""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ..notion_api import (
    date_property,
    multi_select_property,
    number_property,
    plain_text,
    relation_property,
    rich_text_property,
    select_property,
    title_property,
)
from ..worklog import flatten_property

from .catalog import ProjectCatalog, normalize_domain, normalize_path, now_iso, write_project_properties
from .config import WorkSessionSettings
from .storage import append_jsonl, atomic_json, read_json


SESSION_VERSION = 1
PROJECT_MAP_VERSION = 1
WORK_LOG_OPERATION = "work_log"
PROJECT_CREATE_OPERATION = "project_create"
_SAFE_CONTEXT_APPS = {"visual studio code", "code", "cursor", "terminal", "iterm2", "finder", "blender", "xcode", "safari", "google chrome", "arc", "firefox", "notion", "chatgpt", "codex"}
CLASSIFICATION_HISTORY_VERSION = 1
CLASSIFICATION_HISTORY_LIMIT = 200

# App-only suggestions are deliberately conservative.  They make a useful
# default visible to the user but do not change a session unless it is accepted.
_APP_CLASSIFICATIONS = {
    "blender": (("Creative", "Design"), "3D Modeling", .92),
    "xcode": (("Engineering",), "Implementation", .76),
    "visual studio code": (("Engineering",), "Implementation", .76),
    "code": (("Engineering",), "Implementation", .76),
    "cursor": (("Engineering",), "Implementation", .76),
}


def _now() -> datetime:
    return datetime.now(UTC).astimezone()


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()


def _normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _safe_text(value: Any, limit: int = 1200) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _path_contains(path: str, root: str) -> bool:
    path, root = normalize_path(path), normalize_path(root)
    return bool(path and root and (path == root or path.startswith(root.rstrip("/") + "/")))


def _property_name(schema: dict[str, Any], wanted: str) -> str:
    return next((name for name in schema if name.casefold() == wanted.casefold()), "")


def _schema_from_source(source: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, spec in (source.get("properties") or {}).items():
        kind = str(spec.get("type") or "")
        setting = spec.get(kind) if isinstance(spec.get(kind), dict) else {}
        result[name] = {"type": kind, "options": [str(item.get("name")) for item in setting.get("options") or [] if isinstance(item, dict) and item.get("name")]}
    return result


@dataclass(frozen=True)
class Match:
    project_id: str
    name: str
    score: float
    signals: tuple[dict[str, Any], ...]

    def json(self) -> dict[str, Any]:
        return {"projectId": self.project_id, "name": self.name, "confidence": round(self.score, 3), "signals": list(self.signals)}


class WorkSessionService:
    def __init__(self, settings: WorkSessionSettings, notion: Any | None = None) -> None:
        self.settings = settings
        self.catalog = ProjectCatalog(settings.project_cache_path, settings.project_map_path)
        self.notion = notion

    # ---- Notion cache and form metadata -------------------------------------------------

    def refresh_projects(self) -> dict[str, Any]:
        notion = self._notion()
        cache = self.catalog.refresh(notion, self.settings.telemetry.projects_database_id)
        source_id = notion.data_source_id(self.settings.telemetry.work_log_database_id)
        work_log_schema = _schema_from_source(notion.get_data_source(source_id))
        cache["workLogDatabaseId"] = self.settings.telemetry.work_log_database_id
        cache["workLogDataSourceId"] = source_id
        cache["workLogSchema"] = work_log_schema
        atomic_json(self.settings.project_cache_path, cache)
        return {"ok": True, "projects": len(cache.get("projects") or []), "updatedAt": cache.get("updatedAt"), "form": self.project_form(), "workLogProperties": len(work_log_schema)}

    def cache_status(self) -> dict[str, Any]:
        cache = self.catalog.cache()
        updated = _parse_date(cache.get("updatedAt"))
        stale = not updated or (_now() - updated).total_seconds() > self.settings.cache_refresh_hours * 3600
        return {"available": bool(cache.get("projects")), "projects": len(cache.get("projects") or []), "updatedAt": cache.get("updatedAt") or "", "stale": stale}

    def project_form(self) -> dict[str, Any]:
        schema = self.catalog.cache().get("schema") or {}
        fields: list[dict[str, Any]] = []
        desired = (
            ("title", True, "Project name"), ("one liner", True, "One-line description"),
            ("domain", True, "Domain"), ("status", True, "Status"),
            ("no repo needed", False, "No repository needed"), ("default scope", False, "Default scope"),
            ("category", False, "Category"), ("phase", False, "Phase"), ("tags", False, "Tags"),
            ("summary", False, "Summary"), ("started at", False, "Started at"),
            ("public", False, "Public"), ("in showcase", False, "In showcase"),
            ("featured", False, "Featured"), ("mediums", False, "Technology / medium"),
        )
        for desired_name, required, label in desired:
            name = _property_name(schema, desired_name)
            if not name:
                continue
            spec = schema[name]
            if not spec.get("writable"):
                continue
            fields.append({"name": name, "label": label, "type": spec.get("type"), "options": spec.get("options") or [], "requiredForQuickStart": required})
        return {"fields": fields, "formulaProperty": _property_name(schema, "required action"), "schemaUpdatedAt": self.catalog.cache().get("updatedAt") or ""}

    def _classification_history_path(self) -> Path:
        return self.settings.state_directory / "classification-history.json"

    def _classification_history(self) -> list[dict[str, Any]]:
        value = read_json(self._classification_history_path(), {"version": CLASSIFICATION_HISTORY_VERSION, "entries": []})
        entries = value.get("entries") if isinstance(value, dict) else []
        return [item for item in entries if isinstance(item, dict)][-CLASSIFICATION_HISTORY_LIMIT:]

    def _remember_classification(self, session: dict[str, Any]) -> None:
        if session.get("source") == "project_creation" or not session.get("work_modes"):
            return
        telemetry = session.get("telemetry_summary") or {}
        entry = {
            "projectId": str(session.get("primary_project_id") or ""),
            "application": str((telemetry.get("apps") or [""])[0] or "").casefold(),
            "workModes": list(session.get("work_modes") or []),
            "activityMethod": str(session.get("activity_method") or ""),
            "recordedAt": now_iso(),
            "provenance": str(session.get("classification_provenance") or "manual"),
        }
        if not entry["projectId"]:
            return
        entries = self._classification_history()
        entries.append(entry)
        atomic_json(self._classification_history_path(), {"version": CLASSIFICATION_HISTORY_VERSION, "entries": entries[-CLASSIFICATION_HISTORY_LIMIT:]})

    def classification_suggestion(self, request: dict[str, Any]) -> dict[str, Any]:
        """Suggest, never assert, a semantic classification from strong local evidence."""

        project_id = str(request.get("projectId") or "")
        application = str(request.get("application") or "").casefold()
        learned = [item for item in self._classification_history() if item.get("projectId") == project_id and (not application or item.get("application") == application)]
        if learned:
            grouped: dict[tuple[tuple[str, ...], str], int] = {}
            for item in learned:
                modes = tuple(item.get("workModes") or [])
                method = str(item.get("activityMethod") or "")
                if modes:
                    grouped[(modes, method)] = grouped.get((modes, method), 0) + 1
            if grouped:
                (modes, method), count = max(grouped.items(), key=lambda item: (item[1], item[0][0], item[0][1]))
                # Repetition is useful evidence, but never becomes certainty.
                return {"workModes": list(modes), "activityMethod": method, "confidence": round(min(.94, .58 + .09 * count), 3), "provenance": "learned", "observations": count}
        app_suggestion = _APP_CLASSIFICATIONS.get(application)
        if app_suggestion:
            modes, method, confidence = app_suggestion
            return {"workModes": list(modes), "activityMethod": method, "confidence": confidence, "provenance": "app_context", "observations": 1}
        return {"workModes": [], "activityMethod": "", "confidence": 0.0, "provenance": "unknown", "observations": 0}

    def list_projects(self, query: str = "", limit: int = 80) -> dict[str, Any]:
        projects = self.catalog.combined_projects()
        map_entries = self.catalog.mapping().get("projects") or {}
        needle = _normal(query)
        result: list[dict[str, Any]] = []
        for project in projects:
            local = map_entries.get(project["id"], {})
            haystack = " ".join([project.get("name", ""), *local.get("aliases", [])])
            if needle and needle not in _normal(haystack):
                continue
            result.append({
                "id": project["id"], "name": project.get("name") or "Untitled project", "url": project.get("url") or local.get("notion_url") or "",
                "status": project.get("status") or local.get("status") or "", "domain": project.get("domain") or local.get("domain") or "",
                "notion": bool(project.get("notion")), "requiredAction": project.get("requiredAction") or "",
                "lastSelected": local.get("last_selected") or "", "selectionCount": int(local.get("selection_count") or 0),
            })
        result.sort(key=lambda item: (-bool(item["lastSelected"]), item["lastSelected"]), reverse=True)
        result.sort(key=lambda item: (-item["selectionCount"], item["name"].casefold()))
        # Stable recency first, then frequency, then the rest by name.
        result.sort(key=lambda item: item["lastSelected"], reverse=True)
        return {"projects": result[:max(1, min(limit, 250))], "createQuery": query.strip() if query.strip() and not result else "", "cache": self.cache_status()}

    def create_project(self, fields: dict[str, Any], *, create_in_notion: bool) -> dict[str, Any]:
        name = _safe_text(fields.get("title"), 160)
        if not name:
            raise ValueError("A project title is required.")
        fields = {str(key): value for key, value in fields.items()}
        fields["title"] = name
        if not create_in_notion:
            project = self.catalog.create_local(name, fields)
            return {"ok": True, "project": project, "sync": "local_only", "form": self.project_form()}
        try:
            cache = self.catalog.cache()
            schema = cache.get("schema") or {}
            if not schema:
                self.refresh_projects()
                cache, schema = self.catalog.cache(), self.catalog.cache().get("schema") or {}
            payload = write_project_properties(schema, fields)
            title = _property_name(schema, "title")
            if title not in payload:
                raise RuntimeError("The live Projects schema has no writable title property.")
            source_id = self._notion().data_source_id(self.settings.telemetry.projects_database_id)
            page = self._notion().create_page(source_id, payload)
            project = self.catalog.add_created_notion_project(page, name, fields)
            initial = self._enqueue(WORK_LOG_OPERATION, {"session": self._project_started_session(project)})
            sync = self.flush()
            return {"ok": True, "project": project, "sync": "created", "projectStartedLog": initial["id"], "queue": sync}
        except Exception as exc:
            # Preserve the user's choice as a local draft and retry only the
            # outbound create operation; no session information is discarded.
            project = self.catalog.create_local(name, fields)
            self._enqueue(PROJECT_CREATE_OPERATION, {"localProjectId": project["id"], "fields": fields}, error=str(exc))
            return {"ok": True, "project": project, "sync": "queued", "warning": "Notion is unavailable; the project draft is queued locally."}

    def discard_local_project(self, project_id: str) -> dict[str, Any]:
        session = self.current_session()
        if session and project_id in (session.get("project_ids") or []):
            raise RuntimeError("Finish or change the active session before discarding its local project.")
        return {"ok": True, "discarded": self.catalog.discard_local(project_id), "projectId": project_id}

    # ---- Context resolution and matching ------------------------------------------------

    def observe(self, payload: dict[str, Any]) -> dict[str, Any]:
        context = self._context(payload)
        matches = self.match_context(context)
        session = self.current_session()
        best = matches[0] if matches else None
        result: dict[str, Any] = {"ok": True, "context": context, "matches": [match.json() for match in matches[:5]], "decision": "unresolved"}
        if best and self._unambiguous(matches, self.settings.high_confidence):
            result["decision"] = "recognized"
            result["project"] = best.json()
        elif matches:
            result["decision"] = "uncertain"

        if session and session.get("status") == "active":
            self._append_session_telemetry(session, context, best)
            self._record_context(session, context)
            result.update(self._session_observation(session, best, context))
        elif self._meaningful(context):
            result["shouldStartSession"] = bool(best and self._unambiguous(matches, self.settings.high_confidence))
            # An arbitrary foreground app is not enough to interrupt.  An
            # unknown chooser is reserved for a concrete path/repo/file/domain
            # signal; explicit Start remains available for open-ended work.
            result["needsProjectChoice"] = not bool(best and self._unambiguous(matches, self.settings.high_confidence)) and bool(context.get("paths") or context.get("files") or context.get("repos") or context.get("domains"))
        return result

    def match_context(self, context: dict[str, Any]) -> list[Match]:
        scored: list[Match] = []
        title = _normal(context.get("window"))
        app = str(context.get("application") or "").casefold()
        paths = _strings(context.get("paths")) + _strings(context.get("files")) + _strings(context.get("workspaces"))
        repos = _strings(context.get("repos"))
        domains = [normalize_domain(value) for value in _strings(context.get("domains")) if normalize_domain(value)]
        for project in self.catalog.combined_projects():
            local = project.get("local") or {}
            remote = project.get("notionAssociations") or {}
            signals: list[dict[str, Any]] = []
            for file_path in paths:
                for candidate in local.get("files", []):
                    if normalize_path(file_path) == normalize_path(candidate):
                        signals.append({"kind": "exact_file", "value": candidate, "weight": 1.0})
                for candidate in [*local.get("paths", []), *remote.get("paths", []), *remote.get("workspaces", [])]:
                    if _path_contains(file_path, candidate):
                        signals.append({"kind": "path_prefix", "value": candidate, "weight": .93})
                for candidate in [*local.get("repos", []), *remote.get("repos", [])]:
                    if _path_contains(file_path, candidate):
                        signals.append({"kind": "repo_root", "value": candidate, "weight": .98})
            for repo in repos:
                for candidate in [*local.get("repos", []), *remote.get("repos", [])]:
                    if _path_contains(repo, candidate) or _path_contains(candidate, repo):
                        signals.append({"kind": "repo_root", "value": candidate, "weight": 1.0})
            for domain in domains:
                for candidate in [*local.get("domains", []), *remote.get("domains", [])]:
                    if domain == normalize_domain(candidate):
                        signals.append({"kind": "domain", "value": domain, "weight": .62})
            if app:
                for candidate in [*local.get("apps", []), *remote.get("apps", [])]:
                    if app == str(candidate).casefold():
                        signals.append({"kind": "app", "value": app, "weight": .42})
            aliases = [project.get("name", ""), *local.get("aliases", []), *remote.get("aliases", [])]
            for alias in aliases:
                normal_alias = _normal(alias)
                if normal_alias and len(normal_alias) >= 4 and normal_alias in title:
                    signals.append({"kind": "window_or_alias", "value": str(alias)[:120], "weight": .48 if alias != project.get("name") else .44})
            if not signals and local.get("last_selected"):
                last = _parse_date(local.get("last_selected"))
                if last and (_now() - last).total_seconds() < 2 * 3600:
                    signals.append({"kind": "recent_selection", "value": "", "weight": .18})
            if signals:
                # A Projects row can list the same root in both its path and
                # repo fields.  Those are one observation, not independent
                # evidence that should artificially outrank a deeper root.
                roots: dict[str, dict[str, Any]] = {}
                remaining_signals: list[dict[str, Any]] = []
                for signal in signals:
                    if signal["kind"] in {"exact_file", "path_prefix", "repo_root"}:
                        root = normalize_path(str(signal["value"]))
                        current = roots.get(root)
                        if not current or float(signal["weight"]) > float(current["weight"]):
                            roots[root] = signal
                    else:
                        remaining_signals.append(signal)
                signals = [*remaining_signals, *roots.values()]
                # Independent clues combine, while duplicate signals cannot
                # turn a weak single source into certain knowledge.
                grouped: dict[str, float] = {}
                for signal in signals:
                    grouped[signal["kind"]] = max(grouped.get(signal["kind"], 0.0), float(signal["weight"]))
                score = 1.0
                for weight in grouped.values():
                    score *= 1.0 - weight
                scored.append(Match(str(project["id"]), str(project.get("name") or "Project"), 1.0 - score, tuple(sorted(signals, key=lambda signal: -signal["weight"])) ))
        scored.sort(key=lambda item: (-item.score, -self._path_specificity(item), item.name.casefold()))
        return scored

    def associate(self, project_id: str, kind: str, value: str) -> dict[str, Any]:
        if not project_id or not value:
            raise ValueError("A project and a value to associate are required.")
        entry = self.catalog.remember(project_id, kind=kind, value=value)
        return {"ok": True, "projectId": project_id, "kind": kind, "value": value, "associations": {key: entry.get(key, []) for key in ("paths", "files", "repos", "domains", "apps", "aliases")}}

    def disassociate(self, project_id: str, kind: str, value: str) -> dict[str, Any]:
        field = {"path": "paths", "file": "files", "repo": "repos", "domain": "domains", "app": "apps", "alias": "aliases"}.get(kind)
        if not field:
            raise ValueError(f"Unsupported association kind: {kind}")
        mapping = self.catalog.mapping()
        entry = (mapping.get("projects") or {}).get(project_id)
        if not entry:
            return {"ok": True, "removed": False, "reason": "No local association exists for that project."}
        cleaned = normalize_domain(value) if kind == "domain" else normalize_path(value) if kind in {"path", "file", "repo"} else str(value).strip()
        values = list(entry.get(field) or [])
        entry[field] = [item for item in values if item != cleaned]
        entry["updated_at"] = now_iso()
        self.catalog.save_mapping(mapping)
        return {"ok": True, "removed": len(values) != len(entry[field]), "projectId": project_id, "kind": kind, "value": cleaned}

    # ---- Session lifecycle ---------------------------------------------------------------

    def start_session(self, request: dict[str, Any]) -> dict[str, Any]:
        existing = self.current_session()
        if existing and existing.get("status") == "active":
            return {"ok": True, "session": existing, "alreadyActive": True, "reminders": self.rank_reminders(existing.get("project_ids") or [])}
        project_ids = list(dict.fromkeys(str(value) for value in request.get("projectIds") or [] if value))
        if not project_ids:
            raise ValueError("Choose at least one project before starting a session.")
        known = {project["id"]: project for project in self.catalog.combined_projects()}
        unknown = [project_id for project_id in project_ids if project_id not in known]
        if unknown:
            raise ValueError("The selected project is not in the local project cache.")
        goal = _safe_text(request.get("goal"), 500)
        activity = str(request.get("activityType") or "Exploring / No Specific Goal")
        if activity not in self.settings.activity_types:
            raise ValueError("Use one of the configured activity types.")
        work_modes = list(dict.fromkeys(str(value) for value in request.get("workModes") or [] if value))
        invalid_modes = [value for value in work_modes if value not in self.settings.work_modes]
        if invalid_modes:
            raise ValueError("Use configured work modes only.")
        activity_method = str(request.get("activityMethod") or "")
        if activity_method and activity_method not in self.settings.activity_methods:
            raise ValueError("Use a configured activity method or leave it empty.")
        classification_confidence = float(request.get("classificationConfidence") or 0.0)
        minutes = request.get("plannedMinutes")
        planned = int(minutes) if isinstance(minutes, (int, float, str)) and str(minutes).strip() else 0
        reminder_ids = list(dict.fromkeys(str(value) for value in request.get("reminderIds") or [] if value))
        selected_reminders: list[dict[str, Any]] = []
        for value in request.get("selectedReminders") or []:
            if not isinstance(value, dict) or not value.get("id") or str(value["id"]) not in reminder_ids:
                continue
            selected_reminders.append({
                "id": str(value["id"]), "name": _safe_text(value.get("name"), 240),
                "list": _safe_text(value.get("list"), 120), "dueDate": _safe_text(value.get("dueDate"), 80),
            })
        now = now_iso()
        session = {
            "version": SESSION_VERSION, "session_id": str(uuid.uuid4()), "status": "active", "project_ids": project_ids,
            "primary_project_id": str(request.get("primaryProjectId") or project_ids[0]), "started_at": now,
            "goal": goal, "activity_type": activity, "work_modes": work_modes, "activity_method": activity_method,
            "classification_confidence": max(0.0, min(1.0, classification_confidence)),
            "classification_provenance": str(request.get("classificationProvenance") or ("manual" if work_modes else "unknown")),
            "project_confidence": max(0.0, min(1.0, float(request.get("projectConfidence") or 0.0))),
            "project_provenance": str(request.get("projectProvenance") or "manual"),
            "planned_minutes": max(0, min(planned, 24 * 60)),
            "reminder_ids": reminder_ids, "selected_reminders": selected_reminders,
            "contexts": [], "telemetry_started_at": now, "paused_at": "", "pause_seconds": 0,
            "last_checkpoint_prompt_at": "", "switch_candidate": {}, "notion_work_log_id": "", "source": str(request.get("source") or "manual"),
        }
        self._save_session(session)
        self.catalog.mark_selected(project_ids)
        return {"ok": True, "session": session, "reminders": self.rank_reminders(project_ids)}

    def current_session(self) -> dict[str, Any] | None:
        value = read_json(self.settings.session_path, None)
        return value if isinstance(value, dict) and value.get("session_id") else None

    def status(self) -> dict[str, Any]:
        session = self.current_session()
        return {"ok": True, "session": session, "cache": self.cache_status(), "pending": len(self._pending().get("operations") or [])}

    def pause(self) -> dict[str, Any]:
        session = self._active_session()
        if not session.get("paused_at"):
            session["paused_at"] = now_iso()
            self._save_session(session)
        return {"ok": True, "session": session}

    def resume(self) -> dict[str, Any]:
        session = self._active_session()
        paused = _parse_date(session.get("paused_at"))
        if paused:
            session["pause_seconds"] = int(session.get("pause_seconds") or 0) + max(0, int((_now() - paused).total_seconds()))
            session["paused_at"] = ""
            self._save_session(session)
        return {"ok": True, "session": session}

    def change_projects(self, project_ids: list[str], *, primary_project_id: str = "", action: str = "change") -> dict[str, Any]:
        session = self._active_session()
        project_ids = list(dict.fromkeys(str(value) for value in project_ids if value))
        if not project_ids:
            raise ValueError("A session must keep at least one project.")
        session["project_ids"] = project_ids
        session["primary_project_id"] = primary_project_id if primary_project_id in project_ids else project_ids[0]
        session["switch_candidate"] = {}
        self.catalog.mark_selected(project_ids)
        self._save_session(session)
        return {"ok": True, "action": action, "session": session}

    def checkpoint(self, response: dict[str, Any]) -> dict[str, Any]:
        session = self._active_session()
        outcome = str(response.get("outcome") or "")
        if outcome == "continue":
            session["last_checkpoint_prompt_at"] = now_iso()
            self._save_session(session)
            return {"ok": True, "continued": True, "session": session}
        return self.finish_session(response)

    def finish_session(self, response: dict[str, Any]) -> dict[str, Any]:
        session = self._active_session()
        ended_at = now_iso()
        outcome = _safe_text(response.get("outcome") or "Made progress", 80)
        accomplishment = _safe_text(response.get("accomplishment"), 1200)
        next_step = _safe_text(response.get("nextStep"), 700)
        problems = _safe_text(response.get("problems"), 700)
        completed = [str(value) for value in response.get("completeReminderIds") or [] if value]
        telemetry = self.telemetry_summary(session, ended_at)
        session.update({"status": "completed", "ended_at": ended_at, "outcome": outcome, "accomplishment": accomplishment, "next_step": next_step, "problems": problems, "completed_reminder_ids": completed, "telemetry_summary": telemetry})
        self._save_session(session)
        append_jsonl(
            self.settings.state_directory.parent / "inbox" / "work-sessions.jsonl",
            {"event_id": f"work-session:{session['session_id']}", "timestamp": ended_at, "kind": "session",
             "project_ids": session.get("project_ids") or [], "project_id": session.get("primary_project_id") or "",
             "summary": accomplishment or outcome, "detail": problems, "active_seconds": max(0, int((_parse_date(ended_at) - _parse_date(session.get("started_at"))).total_seconds()) - int(session.get("pause_seconds") or 0))},
        )
        self._remember_classification(session)
        operation = self._enqueue(WORK_LOG_OPERATION, {"session": session})
        for reminder_id in completed:
            self._enqueue("reminder_complete", {"reminderId": reminder_id, "sessionId": session["session_id"]})
        flush = self.flush()
        for result in flush.get("results") or []:
            if result.get("kind") == WORK_LOG_OPERATION and result.get("status") == "completed" and result.get("pageId"):
                session["notion_work_log_id"] = str(result["pageId"])
                self._save_session(session)
                break
        return {"ok": True, "session": session, "queuedOperation": operation["id"], "sync": flush}

    def telemetry_summary(self, session: dict[str, Any], ended_at: str | None = None) -> dict[str, Any]:
        start = _parse_date(session.get("telemetry_started_at") or session.get("started_at")) or _now()
        end = _parse_date(ended_at or session.get("ended_at")) or _now()
        entries: list[dict[str, Any]] = []
        if self.settings.telemetry_path.exists():
            try:
                for raw in self.settings.telemetry_path.read_text(encoding="utf-8").splitlines():
                    item = json.loads(raw)
                    if item.get("sessionId") != session.get("session_id"):
                        continue
                    timestamp = _parse_date(item.get("timestamp"))
                    if timestamp and start <= timestamp <= end:
                        entries.append(item)
            except (OSError, json.JSONDecodeError):
                pass
        values = lambda key: sorted({str(item.get(key) or "") for item in entries if item.get(key)})[:12]
        paths = sorted({str(path) for item in entries for path in item.get("paths") or [] if path})[:12]
        repos = sorted({str(repo) for item in entries for repo in item.get("repos") or [] if repo})[:8]
        domains = sorted({str(domain) for item in entries for domain in item.get("domains") or [] if domain})[:10]
        return {"events": len(entries), "apps": values("application"), "paths": paths, "repos": repos, "domains": domains, "projectMatches": sorted({str(item.get("matchedProjectId") or "") for item in entries if item.get("matchedProjectId")}), "rawTelemetryStoredLocally": bool(entries)}

    # ---- Reminders -----------------------------------------------------------------------

    def rank_reminders(self, project_ids: Iterable[str], *, limit: int = 12) -> list[dict[str, Any]]:
        snapshot = read_json(self.settings.reminders_path, {})
        reminders = snapshot.get("reminders") if isinstance(snapshot, dict) else []
        if not isinstance(reminders, list):
            return []
        selected = {project["id"]: project for project in self.catalog.combined_projects() if project["id"] in set(project_ids)}
        now = _now()
        ranked: list[tuple[float, dict[str, Any]]] = []
        for reminder in reminders:
            if not isinstance(reminder, dict) or reminder.get("completed"):
                continue
            score, reasons = 0.0, []
            due = _parse_date(reminder.get("dueDate"))
            if due:
                if due.date() < now.date(): score += 100; reasons.append("overdue")
                elif due.date() == now.date(): score += 70; reasons.append("due_today")
                elif due <= now + timedelta(days=3): score += 25; reasons.append("due_soon")
            priority = int(reminder.get("priority") or 0)
            if priority in {1, 5, 9}:
                score += {1: 35, 5: 24, 9: 12}[priority]; reasons.append("priority")
            text = _normal(" ".join(str(reminder.get(key) or "") for key in ("name", "body", "url", "list")))
            for project in selected.values():
                local = project.get("local") or {}
                remote = project.get("notionAssociations") or {}
                aliases = [project.get("name", ""), *local.get("aliases", []), *remote.get("aliases", [])]
                if any((candidate := _normal(alias)) and len(candidate) >= 4 and candidate in text for alias in aliases):
                    score += 80; reasons.append("project_match"); break
                if any(_normal(domain) and _normal(domain) in text for domain in [*local.get("domains", []), *remote.get("domains", [])]):
                    score += 55; reasons.append("domain_match"); break
            if score:
                ranked.append((score, {"id": str(reminder.get("id") or ""), "name": _safe_text(reminder.get("name"), 240), "body": _safe_text(reminder.get("body"), 500), "url": _safe_text(reminder.get("url"), 500), "list": _safe_text(reminder.get("list"), 120), "dueDate": reminder.get("dueDate") or "", "priority": priority, "score": round(score, 1), "reasons": reasons}))
        ranked.sort(key=lambda item: (-item[0], str(item[1]["dueDate"] or "9999"), item[1]["name"].casefold()))
        return [item for _, item in ranked[:max(1, min(limit, 30))]]

    # ---- Queue and Notion synchronization ------------------------------------------------

    def flush(self) -> dict[str, Any]:
        pending = self._pending()
        operations = list(pending.get("operations") or [])
        if not operations:
            return {"ok": True, "processed": 0, "remaining": 0, "results": []}
        results: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        # Project creation must precede its dependent work log.
        operations.sort(key=lambda item: 0 if item.get("kind") == PROJECT_CREATE_OPERATION else 1)
        for operation in operations:
            try:
                result = self._execute_operation(operation)
                results.append({"id": operation.get("id"), "kind": operation.get("kind"), "status": "completed", **result})
            except Exception as exc:
                operation["attempts"] = int(operation.get("attempts") or 0) + 1
                operation["lastError"] = _safe_text(exc, 500)
                operation["lastAttemptAt"] = now_iso()
                remaining.append(operation)
                results.append({"id": operation.get("id"), "kind": operation.get("kind"), "status": "deferred", "error": operation["lastError"]})
        pending["operations"] = remaining
        pending["updatedAt"] = now_iso()
        atomic_json(self.settings.pending_path, pending)
        return {"ok": not remaining, "processed": len(results), "remaining": len(remaining), "results": results}

    def _execute_operation(self, operation: dict[str, Any]) -> dict[str, Any]:
        kind = str(operation.get("kind") or "")
        payload = operation.get("payload") or {}
        if kind == PROJECT_CREATE_OPERATION:
            return self._sync_created_project(payload)
        if kind == WORK_LOG_OPERATION:
            return self._write_work_log(payload.get("session") or {})
        if kind == "reminder_complete":
            self._complete_reminder(str(payload.get("reminderId") or ""))
            return {"reminderId": payload.get("reminderId")}
        raise ValueError(f"Unsupported queued operation: {kind}")

    def _sync_created_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        local_id, fields = str(payload.get("localProjectId") or ""), dict(payload.get("fields") or {})
        if not local_id:
            raise ValueError("Queued project creation has no local project ID.")
        schema = self.catalog.cache().get("schema") or {}
        if not schema:
            self.refresh_projects(); schema = self.catalog.cache().get("schema") or {}
        page = self._notion().create_page(self._notion().data_source_id(self.settings.telemetry.projects_database_id), write_project_properties(schema, fields))
        remote = self.catalog.add_created_notion_project(page, _safe_text(fields.get("title"), 160), fields)
        self._replace_project_id(local_id, remote["id"])
        return {"projectId": remote["id"], "name": remote["name"]}

    def _write_work_log(self, session: dict[str, Any]) -> dict[str, Any]:
        if not session.get("session_id"):
            raise ValueError("Queued work log is missing a session ID.")
        ids = [self._resolve_project_id(project_id) for project_id in session.get("project_ids") or []]
        if not ids or any(project_id.startswith("local:") for project_id in ids):
            raise RuntimeError("This session references a local-only project; its Work Log will sync after the project is created in Notion.")
        cache = self.catalog.cache()
        schema = cache.get("workLogSchema") or {}
        if not schema:
            self.refresh_projects(); cache, schema = self.catalog.cache(), self.catalog.cache().get("workLogSchema") or {}
        source_id = str(cache.get("workLogDataSourceId") or self._notion().data_source_id(self.settings.telemetry.work_log_database_id))
        props = self._work_log_properties(schema, session, ids)
        # A prior attempt for this session may already have created the Notion
        # page before failing later (network hiccup, a crash before the queue
        # persisted its removal, or a re-run of `flush` over a stale pending
        # operation) -- query by the same event_hash set below so a retry
        # updates that page instead of creating a duplicate Work Log entry.
        # Mirrors WorklogStore.upsert in ../worklog.py, which already does
        # this for the evidence-sync path.
        event_hash = f"work-session:{session['session_id']}"
        event_hash_property = _property_name(schema, "event_hash")
        existing_page_id = ""
        if event_hash_property:
            matches = self._notion().query_data_source(
                source_id, filter_payload={"property": event_hash_property, "rich_text": {"equals": event_hash}}
            )
            existing_page_id = next(
                (str(page.get("id") or "") for page in matches if plain_text(page, event_hash_property) == event_hash), ""
            )
        if existing_page_id:
            self._notion().update_page(existing_page_id, props)
            page_id = existing_page_id
        else:
            page = self._notion().create_page(source_id, props)
            page_id = str(page.get("id") or "")
        session["notion_work_log_id"] = page_id
        current = self.current_session()
        if current and current.get("session_id") == session.get("session_id"):
            current["notion_work_log_id"] = session["notion_work_log_id"]
            self._save_session(current)
        return {"pageId": session["notion_work_log_id"], "url": f"https://www.notion.so/{session['notion_work_log_id'].replace('-', '')}" if session["notion_work_log_id"] else ""}

    def _work_log_properties(self, schema: dict[str, Any], session: dict[str, Any], project_ids: list[str]) -> dict[str, Any]:
        def name(label: str) -> str: return _property_name(schema, label)
        def set_if(label: str, payload: dict[str, Any]) -> None:
            actual = name(label)
            if actual: properties[actual] = payload
        telemetry = session.get("telemetry_summary") or {}
        start, end = session.get("started_at") or now_iso(), session.get("ended_at") or now_iso()
        types = self._work_log_types(schema, session)
        title = self._work_log_title(session)
        happened = _safe_text(session.get("accomplishment") or session.get("outcome") or "Work session completed.", 1200)
        context_summary = self._context_summary(telemetry, session)
        event_hash = f"work-session:{session['session_id']}"
        properties: dict[str, Any] = {}
        set_if("entry", title_property(title))
        set_if("project", relation_property(project_ids))
        set_if("date", date_property(start))
        set_if("session start", date_property(start))
        set_if("session end", date_property(end))
        set_if("Session type", multi_select_property(types))
        set_if("goal", rich_text_property(session.get("goal") or ""))
        set_if("no goal", {"checkbox": not bool(session.get("goal"))})
        set_if("What Happened", rich_text_property(happened))
        set_if("progress_summary", rich_text_property(happened))
        set_if("Problems", rich_text_property(session.get("problems") or ""))
        set_if("no problems", {"checkbox": not bool(session.get("problems"))})
        set_if("Next Step", rich_text_property(session.get("next_step") or ""))
        set_if("no next steps", {"checkbox": not bool(session.get("next_step"))})
        set_if("event", rich_text_property(_safe_text(session.get("outcome") or "Work session", 300)))
        set_if("event_hash", rich_text_property(event_hash))
        set_if("active seconds", number_property(max(0, int((_parse_date(end) - _parse_date(start)).total_seconds()) - int(session.get("pause_seconds") or 0))))
        set_if("idle seconds", number_property(0))
        set_if("sessions_count", number_property(int(telemetry.get("events") or 0)))
        set_if("top_apps", rich_text_property(", ".join(telemetry.get("apps") or [])))
        set_if("open_questions", rich_text_property(context_summary))
        # Existing schemas store a single confidence number.  Preserve a
        # conservative value rather than pretending a weak classification is
        # confirmed.  New optional fields below are used automatically after a
        # user adds them in Notion; no schema mutation is assumed here.
        set_if("confidence", number_property(round(min(
            float(session.get("project_confidence") or .98),
            float(session.get("classification_confidence") or .98),
        ), 3)))
        def set_optional(label: str, expected_type: str, payload: dict[str, Any]) -> None:
            actual = name(label)
            if actual and (schema.get(actual) or {}).get("type") == expected_type:
                properties[actual] = payload
        set_optional("Work Modes", "multi_select", multi_select_property(session.get("work_modes") or []))
        set_optional("Activity / Method", "multi_select", multi_select_property([session["activity_method"]] if session.get("activity_method") else []))
        set_optional("Classification Confidence", "number", number_property(round(float(session.get("classification_confidence") or 0), 3)))
        set_optional("Classification Source", "select", select_property(session.get("classification_provenance") or "unknown"))
        set_optional("Project Confidence", "number", number_property(round(float(session.get("project_confidence") or 0), 3)))
        set_optional("Project Source", "select", select_property(session.get("project_provenance") or "manual"))
        return properties

    def _work_log_types(self, schema: dict[str, Any], session: dict[str, Any]) -> list[str]:
        actual = _property_name(schema, "Session type")
        allowed = set((schema.get(actual) or {}).get("options") or [])
        mapping = {
            "Planning": "Planning", "Researching": "Research", "Studying / Learning": "Learning", "Experimenting": "Experiment",
            "Building / Implementing": "Build", "Debugging": "Build", "Testing": "Build", "Refactoring": "Build",
            "Designing": "Build", "Creative Work": "Writing", "Writing": "Writing", "Reviewing": "Reading",
            "Organizing": "Organizing", "Administrative": "Organizing", "Maintenance": "Build", "Boring but Necessary": "Organizing",
            "Exploring / No Specific Goal": "Experiment",
        }
        mode_mapping = {
            "Creative": "Experiment", "Administrative": "Organizing", "Planning": "Planning",
            "Research": "Research", "Analysis": "Research", "Engineering": "Build",
            "Writing": "Writing", "Communication": "Writing", "Organization": "Organizing",
            "Learning": "Learning", "Maintenance": "Build", "Testing": "Build", "Design": "Build",
        }
        candidates = [mode_mapping[mode] for mode in session.get("work_modes") or [] if mode in mode_mapping]
        if not candidates:
            candidates = [mapping.get(str(session.get("activity_type") or ""), "Experiment")]
        return list(dict.fromkeys(candidate for candidate in candidates if candidate in allowed)) or (["Build"] if "Build" in allowed else [])

    def _complete_reminder(self, reminder_id: str) -> None:
        if not reminder_id:
            return
        # EventKit is the existing native integration.  Fetch by identifier
        # and save only the selected reminder, never touching unrelated items.
        script = r'''import EventKit
import Foundation
let store = EKEventStore()
let done = DispatchSemaphore(value: 0)
var granted = false
if #available(macOS 14.0, *) { store.requestFullAccessToReminders { value, _ in granted = value; done.signal() } }
else { store.requestAccess(to: .reminder) { value, _ in granted = value; done.signal() } }
_ = done.wait(timeout: .now() + 20)
guard granted else { fputs("Reminders permission was not granted", stderr); exit(2) }
guard let reminder = store.calendarItem(withIdentifier: CommandLine.arguments[1]) as? EKReminder else { fputs("Reminder not found", stderr); exit(3) }
reminder.isCompleted = true
try store.save(reminder, commit: true)
'''
        process = subprocess.run(["/usr/bin/swift", "-", reminder_id], input=script, text=True, capture_output=True, timeout=50)
        if process.returncode != 0:
            raise RuntimeError(_safe_text(process.stderr or process.stdout or "Could not complete Apple Reminder", 400))

    # ---- Internal helpers ----------------------------------------------------------------

    def _context(self, payload: dict[str, Any]) -> dict[str, Any]:
        application = _safe_text(payload.get("application"), 120)
        app_key = application.casefold()
        context = {
            "timestamp": now_iso(), "application": application, "window": _safe_text(payload.get("window"), 300),
            "paths": [normalize_path(value) for value in _strings(payload.get("paths")) if value],
            "files": [normalize_path(value) for value in _strings(payload.get("files")) if value],
            "workspaces": [normalize_path(value) for value in _strings(payload.get("workspaces")) if value],
            "repos": [normalize_path(value) for value in _strings(payload.get("repos")) if value],
            "domains": [normalize_domain(value) for value in _strings(payload.get("domains")) if normalize_domain(value)],
            "idleSeconds": max(0, int(payload.get("idleSeconds") or 0)),
        }
        # Existing terminal journal is the safest source of an actual shell
        # CWD.  Use only a fresh item while a terminal app is foreground.
        if app_key in {"terminal", "iterm2"}:
            latest = self._latest_terminal_context()
            if latest:
                context["paths"].append(latest["cwd"])
                if latest.get("repo"):
                    context["repos"].append(latest["repo"])
        if app_key == "blender":
            match = re.search(r"([^|—]+\.blend)\b", str(payload.get("window") or ""), re.IGNORECASE)
            if match:
                context["files"].append(normalize_path(match.group(1)))
        return {key: list(dict.fromkeys(value)) if isinstance(value, list) else value for key, value in context.items()}

    def _latest_terminal_context(self) -> dict[str, str] | None:
        try:
            lines = self.settings.terminal_journal_path.read_text(encoding="utf-8").splitlines()[-30:]
        except OSError:
            return None
        for raw in reversed(lines):
            try: row = json.loads(raw)
            except json.JSONDecodeError: continue
            timestamp = _parse_date(row.get("timestamp"))
            if not timestamp or (_now() - timestamp).total_seconds() > 300:
                continue
            cwd = normalize_path(str(row.get("cwd") or ""))
            if cwd:
                return {"cwd": cwd, "repo": normalize_path(str(row.get("repo_root") or ""))}
        return None

    def _session_observation(self, session: dict[str, Any], best: Match | None, context: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"session": session}
        current = set(session.get("project_ids") or [])
        if context.get("idleSeconds", 0) >= self.settings.idle_checkpoint_minutes * 60:
            last = _parse_date(session.get("last_checkpoint_prompt_at"))
            if not last or (_now() - last).total_seconds() >= self.settings.idle_checkpoint_minutes * 60:
                result["checkpointSuggested"] = True
            return result
        if not best or best.project_id in current or best.score < self.settings.switch_confidence:
            if session.get("switch_candidate"):
                session["switch_candidate"] = {}
                self._save_session(session)
            return result
        candidate = session.get("switch_candidate") or {}
        timestamp = _now()
        if candidate.get("project_id") == best.project_id:
            candidate["observations"] = int(candidate.get("observations") or 0) + 1
        else:
            candidate = {"project_id": best.project_id, "first_seen_at": timestamp.isoformat(), "observations": 1, "confidence": best.score}
        session["switch_candidate"] = candidate
        self._save_session(session)
        first = _parse_date(candidate.get("first_seen_at")) or timestamp
        if int(candidate.get("observations") or 0) >= self.settings.switch_minimum_observations and (timestamp - first).total_seconds() >= self.settings.switch_minimum_seconds:
            result["projectSwitchSuggested"] = {"currentProjectIds": session.get("project_ids") or [], "detected": best.json(), "observations": candidate["observations"]}
        return result

    def _append_session_telemetry(self, session: dict[str, Any], context: dict[str, Any], best: Match | None) -> None:
        if session.get("paused_at"):
            return
        # Deliberately omit window title and document/browser contents from the
        # durable session journal.  They may influence an in-memory match but
        # are never retained as surveillance data.
        append_jsonl(self.settings.telemetry_path, {"timestamp": context["timestamp"], "sessionId": session["session_id"], "application": context.get("application") if str(context.get("application") or "").casefold() in _SAFE_CONTEXT_APPS else "Other", "paths": context.get("paths") or [], "repos": context.get("repos") or [], "domains": context.get("domains") or [], "matchedProjectId": best.project_id if best else "", "confidence": round(best.score, 3) if best else 0})

    def _record_context(self, session: dict[str, Any], context: dict[str, Any]) -> None:
        value = {"timestamp": context.get("timestamp"), "application": context.get("application"), "paths": context.get("paths") or [], "repos": context.get("repos") or [], "domains": context.get("domains") or []}
        contexts = list(session.get("contexts") or [])
        if not contexts or contexts[-1] != value:
            contexts.append(value)
        session["contexts"] = contexts[-30:]
        self._save_session(session)

    def _meaningful(self, context: dict[str, Any]) -> bool:
        return bool(context.get("paths") or context.get("repos") or context.get("domains") or str(context.get("application") or "").casefold() in _SAFE_CONTEXT_APPS)

    @staticmethod
    def _path_specificity(match: Match) -> int:
        return max((len(str(signal.get("value") or "")) for signal in match.signals if signal.get("kind") in {"exact_file", "path_prefix", "repo_root"}), default=0)

    @classmethod
    def _unambiguous(cls, matches: list[Match], threshold: float) -> bool:
        if not matches or matches[0].score < threshold:
            return False
        if len(matches) == 1 or matches[0].score - matches[1].score >= .10:
            return True
        # Nested known roots are not ambiguous: `/a/project/tools` is more
        # precise than `/a/project` for the same observed child path.  Keep
        # equal-depth associations ambiguous so multi-project work still asks.
        first, second = cls._path_specificity(matches[0]), cls._path_specificity(matches[1])
        return first > second and first - second >= 3

    def _active_session(self) -> dict[str, Any]:
        session = self.current_session()
        if not session or session.get("status") != "active":
            raise RuntimeError("There is no active work session.")
        return session

    def _save_session(self, session: dict[str, Any]) -> None:
        atomic_json(self.settings.session_path, session)

    def _pending(self) -> dict[str, Any]:
        value = read_json(self.settings.pending_path, {"version": 1, "operations": []})
        if not isinstance(value, dict): value = {"version": 1, "operations": []}
        value.setdefault("version", 1); value.setdefault("operations", [])
        return value

    def _enqueue(self, kind: str, payload: dict[str, Any], *, error: str = "") -> dict[str, Any]:
        pending = self._pending()
        operation = {"id": str(uuid.uuid4()), "kind": kind, "payload": payload, "createdAt": now_iso(), "attempts": 0, "lastError": _safe_text(error, 500)}
        pending["operations"].append(operation)
        pending["updatedAt"] = now_iso()
        atomic_json(self.settings.pending_path, pending)
        return operation

    def _replace_project_id(self, local_id: str, remote_id: str) -> None:
        mapping = self.catalog.mapping()
        local = (mapping.get("projects") or {}).pop(local_id, None)
        if local:
            local["notion_page_id"] = remote_id
            mapping["projects"][remote_id] = local
            self.catalog.save_mapping(mapping)
        session = self.current_session()
        if session and local_id in (session.get("project_ids") or []):
            session["project_ids"] = [remote_id if value == local_id else value for value in session["project_ids"]]
            if session.get("primary_project_id") == local_id: session["primary_project_id"] = remote_id
            self._save_session(session)
        pending = self._pending()
        replacement = json.dumps(pending).replace(local_id, remote_id)
        atomic_json(self.settings.pending_path, json.loads(replacement))

    def _resolve_project_id(self, project_id: str) -> str:
        entry = self.catalog.local_entry(project_id)
        return str(entry.get("notion_page_id") or project_id)

    def _work_log_title(self, session: dict[str, Any]) -> str:
        project_names = {project["id"]: project.get("name") for project in self.catalog.combined_projects()}
        primary = project_names.get(session.get("primary_project_id")) or "Work"
        goal = _safe_text(session.get("goal"), 90)
        return f"{primary} — {goal}" if goal else f"{primary} — {session.get('activity_type') or 'Work session'}"

    @staticmethod
    def _project_started_session(project: dict[str, Any]) -> dict[str, Any]:
        end = _now()
        # Existing Work Logs store minute-precision dates and require an end
        # after start. This explicit lifecycle marker is dated to the minute
        # immediately preceding creation rather than a future timestamp.
        start = end - timedelta(minutes=1)
        return {
            "version": SESSION_VERSION, "session_id": f"project-started:{project['id']}", "status": "completed",
            "project_ids": [project["id"]], "primary_project_id": project["id"], "started_at": start.isoformat(), "ended_at": end.isoformat(),
            "goal": "Project started", "activity_type": "Planning", "outcome": "Project started", "accomplishment": "Created the project record.",
            "next_step": "", "problems": "", "pause_seconds": 0, "reminder_ids": [], "completed_reminder_ids": [],
            "telemetry_summary": {"events": 0, "apps": [], "paths": [], "repos": [], "domains": []}, "source": "project_creation",
        }

    @staticmethod
    def _context_summary(telemetry: dict[str, Any], session: dict[str, Any]) -> str:
        parts = []
        if telemetry.get("apps"): parts.append("Apps: " + ", ".join(telemetry["apps"]))
        if telemetry.get("paths"): parts.append("Paths: " + ", ".join(telemetry["paths"][:6]))
        if telemetry.get("repos"): parts.append("Repos: " + ", ".join(telemetry["repos"][:4]))
        if telemetry.get("domains"): parts.append("Domains: " + ", ".join(telemetry["domains"][:6]))
        if session.get("reminder_ids"): parts.append("Selected reminders: " + str(len(session["reminder_ids"])))
        if session.get("completed_reminder_ids"): parts.append("Completed reminders: " + str(len(session["completed_reminder_ids"])))
        return _safe_text("\n".join(parts), 1800)

    def _notion(self) -> Any:
        if self.notion is None:
            from ..notion_api import NotionClient
            self.notion = NotionClient(self.settings.telemetry.notion_api_key)
        return self.notion


def _strings(value: Any) -> list[str]:
    if not value: return []
    if isinstance(value, str): return [value]
    if isinstance(value, (tuple, list, set)): return [str(item) for item in value if str(item).strip()]
    return [str(value)]
