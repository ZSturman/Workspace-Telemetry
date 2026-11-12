"""Local-first evidence collection and deterministic Notion Work Log synthesis.

Only filtered event metadata reaches Notion.  The SQLite state is the source
of incremental truth; Notion is an upsert target, never a checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from .notion_api import (
    date_property, flatten_property, multi_select_property, number_property,
    plain_text, relation_property, rich_text_property, select_property,
    title_property,
)

from .activitywatch import ActivityWatchClient
from .config import TelemetrySettings
from .models import TimeRange
from .privacy import normalized_domain
from .state import TelemetryState


WORKLOG_VERSION = "worklog-v1"
# Each analysis is a sequential local-LM call (~1-5s, occasionally much more).
# A large "Needs Next Work Analysis" backlog previously made analyze_next_work
# iterate every checked project in one sync call, which routinely exceeded the
# scheduled worklog_sync timeout before any project got unchecked -- so the
# backlog never drained. Capping the batch keeps one sync call fast and lets
# the backlog drain a little more on every subsequent sync instead.
MAX_NEXT_WORK_ANALYSES_PER_SYNC = 10
DEFAULT_ROOTS = (str(Path.home() / "Projects"), str(Path.home() / ".hammerspoon"))
# Kept in step with Hammerspoon's worklog.ignored_path_fragments. SwiftPM `.build`,
# service writes under ~/Workspace/Logs and fixture output were ~70% of stored
# filesystem evidence before 2026-10-21 and are not deliberate work.
DEFAULT_IGNORES = ("/.git/", "/node_modules/", "/build/", "/.build/", "/dist/", "/.next/", "/DerivedData/", "/.cache/",
                   "/Caches/", "/tmp/", "/.venv/", "/venv/", "/__pycache__/", "/.pytest_cache/", "/Workspace/Logs/",
                   "/fixtures/output/")
SENSITIVE = re.compile(r"(?i)(?:api[_ -]?key|password|secret|token|authorization)\s*[:=]\s*[^\s,;]+|(?:sk|ghp|github_pat)_[A-Za-z0-9_-]+")

# Agents (Claude Code, etc.) are asked to leave known issues, uncommitted-work
# notes, and try-it steps in this file at the end of a session — an existing
# convention already used across several projects, not something new invented
# for this pipeline. The first one found for a project wins.
NEXT_STEPS_RELATIVE_PATHS = ("docs/NEXT_STEPS.md", "NEXT_STEPS.md")
NEXT_STEPS_TEXT_LIMIT = 4000

# Deterministic keyword -> canonical "Next Work" tag classification. Only
# keywords whose tag exists in the live Notion "Next Work" options are ever
# applied, so this never invents a new multi_select option.
NEXT_WORK_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("xctest", "Testing"), ("ui test", "Testing"), ("uitest", "Testing"), ("unit test", "Testing"),
    ("test", "Testing"), ("debug", "Debugging"), ("refactor", "Refactoring"),
    ("document", "Documentation"), ("readme", "Documentation"),
    ("deploy", "Deployment"), ("code review", "Code Review"),
    ("migrat", "Migration"), ("config", "Configuration"), ("integrat", "Integration"),
    ("database", "Database Design"), ("schema", "Database Design"),
    ("api", "API Design"), ("architecture", "Architecture"),
    ("ios", "iOS/macOS Development"), ("macos", "iOS/macOS Development"),
    ("xcode", "iOS/macOS Development"), ("swift", "iOS/macOS Development"),
    ("frontend", "Frontend Development"), ("backend", "Backend Development"),
    ("web app", "Web Development"), ("website", "Web Development"),
    ("polish", "Revision/Polishing"), ("plan", "Planning"), ("prototype", "Prototyping"),
    ("ui/ux", "UI/UX Design"), ("ux", "UI/UX Design"),
)


def _classify_next_work(text: str, allowed: set[str]) -> list[str]:
    lowered = text.casefold()
    tags: list[str] = []
    for keyword, tag in NEXT_WORK_KEYWORDS:
        if keyword in lowered and tag in allowed and tag not in tags:
            tags.append(tag)
    return tags[:6]


def _clean_report_text(text: str, limit: int = NEXT_STEPS_TEXT_LIMIT) -> str:
    cleaned = SENSITIVE.sub("[redacted]", text).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "\n\n… (truncated)"
    return cleaned


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    paths: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    workspaces: tuple[str, ...] = ()
    apps: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    ai_hints: tuple[str, ...] = ()
    tracking_enabled: bool = False
    priority: float = 0.0
    local_directory: str = ""


@dataclass(frozen=True)
class Evidence:
    hash: str
    kind: str
    start: datetime
    end: datetime
    app: str = ""
    domain: str = ""
    path: str = ""
    repo: str = ""
    workspace: str = ""
    category: str = ""
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    project_id: str = ""
    confidence: float = 0.0
    matched_by: str = "unresolved"


def _now(settings: TelemetrySettings) -> datetime:
    return datetime.now(settings.zoneinfo())


def _hash(*parts: Any) -> str:
    return hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()


def _parse_datetime(value: str | datetime, zone: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)


def _strings(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, list):
        values = value
    else:
        values = re.split(r"[\n,;]", str(value))
    result: list[str] = []
    for raw in values:
        text = str(raw).strip().strip("'\"’“”")
        if text and text not in result:
            result.append(os.path.expanduser(text))
    return tuple(result)


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _file_uri_path(value: str) -> str:
    parsed = urlsplit(value)
    return unquote(parsed.path) if parsed.scheme == "file" else ""


class ProjectCatalog:
    def __init__(self, projects: Iterable[Project], presets: dict[str, Any], weights: dict[str, Any]) -> None:
        self.projects = tuple(projects)
        self.presets = presets if isinstance(presets, dict) else {}
        self.weights = {
            "local_directory": 1.0, "repo_root": 1.0, "workspace_root": 0.95, "local_preset": 0.95,
            "file_path_prefix": 0.9, "vscode_workspace": 0.85,
            "browser_domain": 0.65, "app_hint": 0.45, "recent_session": 0.35,
            **{str(k): float(v) for k, v in weights.items() if isinstance(v, (int, float))},
        }

    @classmethod
    def load(cls, notion: Any, settings: TelemetrySettings) -> "ProjectCatalog":
        source = notion.data_source_id(settings.projects_database_id)
        pages = notion.query_data_source(source)
        projects: list[Project] = []
        for page in pages:
            props = page.get("properties") or {}
            name = str(flatten_property(props.get("title") or {})).strip()
            if not name:
                continue
            projects.append(Project(
                id=str(page.get("id") or ""), name=name,
                paths=_strings(flatten_property(props.get("path prefixes") or {})),
                repos=_strings(flatten_property(props.get("repo roots") or {})),
                workspaces=_strings(flatten_property(props.get("workspace roots") or {})),
                apps=tuple(item.casefold() for item in _strings(flatten_property(props.get("app hints") or {}))),
                domains=tuple(normalized_domain(item) for item in _strings(flatten_property(props.get("browser domains") or {}))),
                ai_hints=tuple(item.casefold() for item in _strings(flatten_property(props.get("ai hints") or {}))),
                tracking_enabled=bool(flatten_property(props.get("tracking enabled") or {})),
                priority=float(flatten_property(props.get("tracking priority") or {}) or 0),
                local_directory=str(flatten_property(props.get("Local Directory") or {}) or "").strip().strip("'\"‘’“”").strip(),
            ))
        local = settings.worklog
        return cls(projects, local.get("projectPresets") or {}, local.get("matchingWeights") or {})

    def watch_roots(self) -> list[str]:
        roots = {os.path.expanduser(root) for root in DEFAULT_ROOTS}
        for project in self.projects:
            candidates = (*project.paths, *project.repos, *project.workspaces, *((project.local_directory,) if project.local_directory else ()))
            roots.update(path for path in candidates if Path(path).exists())
        for raw in (self.presets or {}).values():
            if isinstance(raw, dict):
                roots.update(os.path.expanduser(path) for path in _strings(raw.get("paths")) + _strings(raw.get("repos")))
        return sorted(roots)

    def match(self, event: Evidence) -> Evidence:
        scored: list[tuple[float, str, Project]] = []
        path = os.path.realpath(os.path.expanduser(event.path or event.repo or event.workspace)) if (event.path or event.repo or event.workspace) else ""
        # A directory's basename is not project evidence for a filesystem
        # event.  In particular, Finder and build tools routinely touch
        # ``.DS_Store`` and generated assets inside many project folders.  A
        # prior implementation treated that parent folder as a VS Code
        # workspace, which could turn ambient ChewSense paths into an
        # authoritative project match.  Only a foreground Code-like activity
        # can use a workspace label as this weaker fallback.
        code_activity = event.kind in {"activity", "ai"} and event.app.casefold() in {"visual studio code", "code", "cursor", "codex"}
        workspace_name = _normal(Path(event.workspace).name) if code_activity and event.workspace else ""
        for project in self.projects:
            score, matched_by = 0.0, "unresolved"
            preset = self.presets.get(project.id) if isinstance(self.presets, dict) else None
            preset_paths = _strings((preset or {}).get("paths")) + _strings((preset or {}).get("repos")) if isinstance(preset, dict) else ()
            for candidate in project.repos:
                if path and os.path.commonpath([path, os.path.realpath(candidate)]) == os.path.realpath(candidate): score, matched_by = self.weights["repo_root"], "repo_root"
            if project.local_directory:
                local_root = os.path.realpath(os.path.expanduser(project.local_directory))
                if path and os.path.commonpath([path, local_root]) == local_root:
                    score, matched_by = self.weights["local_directory"], "local_directory"
            for candidate in (*project.workspaces, *preset_paths):
                candidate_path = os.path.realpath(os.path.expanduser(candidate))
                if path and os.path.commonpath([path, candidate_path]) == candidate_path and score < self.weights["workspace_root"]:
                    score, matched_by = self.weights["local_preset"] if candidate in preset_paths else self.weights["workspace_root"], "workspace_root"
            for candidate in project.paths:
                candidate_path = os.path.realpath(os.path.expanduser(candidate))
                if path and os.path.commonpath([path, candidate_path]) == candidate_path and score < self.weights["file_path_prefix"]:
                    score, matched_by = self.weights["file_path_prefix"], "file_path_prefix"
            if workspace_name and _normal(project.name) == workspace_name and score < self.weights["vscode_workspace"]:
                score, matched_by = self.weights["vscode_workspace"], "vscode_workspace"
            if event.domain and normalized_domain(event.domain) in project.domains and score < self.weights["browser_domain"]:
                score, matched_by = self.weights["browser_domain"], "browser_domain"
            if event.app and event.app.casefold() in project.apps and score < self.weights["app_hint"]:
                score, matched_by = self.weights["app_hint"], "app_hint"
            if event.kind == "ai" and any(hint in event.summary.casefold() for hint in project.ai_hints) and score < self.weights["app_hint"]:
                score, matched_by = self.weights["app_hint"], "app_hint"
            if score:
                scored.append((min(1.0, score + (0.02 if project.tracking_enabled else 0)), matched_by, project))
        scored.sort(key=lambda item: (-item[0], -item[2].priority, item[2].name.casefold()))
        if not scored or scored[0][0] < 0.75 or (len(scored) > 1 and scored[0][0] - scored[1][0] < 0.10):
            return event
        score, matched_by, project = scored[0]
        return Evidence(**{**event.__dict__, "project_id": project.id, "confidence": score, "matched_by": matched_by})


class WorklogStore:
    def __init__(self, notion: Any, database_id: str, required: dict[str, str]) -> None:
        self.notion, self.source_id, self.required = notion, notion.data_source_id(database_id), required
        self.properties = notion.get_data_source(self.source_id).get("properties") or {}
        missing = [name for name, kind in required.items() if self.properties.get(name, {}).get("type") != kind]
        if missing:
            raise RuntimeError(f"Incompatible Notion schema: {', '.join(missing)}")

    def options(self, name: str) -> set[str]:
        prop = self.properties.get(name) or {}
        kind = prop.get("type")
        return {str(item.get("name")) for item in (prop.get(kind) or {}).get("options") or [] if item.get("name")}

    def upsert(self, properties: dict[str, Any], event_hash: str, *, dry_run: bool) -> tuple[str, str]:
        pages = self.notion.query_data_source(self.source_id, filter_payload={"property": "event_hash", "rich_text": {"equals": event_hash}})
        pages = [page for page in pages if plain_text(page, "event_hash") == event_hash]
        if dry_run:
            return (str(pages[0].get("id") or "") if pages else "", "would-update" if pages else "would-create")
        if pages:
            page_id = str(pages[0].get("id") or "")
            self.notion.update_page(page_id, properties)
            return page_id, "updated"
        page = self.notion.create_page(self.source_id, properties)
        return str(page.get("id") or ""), "created"

    def project_day_pages(self, project_id: str, day: datetime) -> list[dict[str, Any]]:
        start = day.date().isoformat()
        end = (day + timedelta(days=1)).date().isoformat()
        pages = self.notion.query_data_source(self.source_id, filter_payload={"and": [
            {"property": "project", "relation": {"contains": project_id}},
            {"property": "date", "date": {"on_or_after": start}},
            {"property": "date", "date": {"before": end}},
        ]})
        return [
            page for page in pages
            if project_id in (flatten_property((page.get("properties") or {}).get("project") or {}) or [])
            and str(flatten_property((page.get("properties") or {}).get("date") or {})).startswith(start)
        ]


# Bump when the packet, prompt, or schema changes so cached summaries are regenerated.
SYNTHESIS_VERSION = "worklog-synthesis-v2"
SYNTHESIS_SYSTEM = (
    "Return only JSON. You write a factual work log entry for each supplied evidence packet. "
    "Describe what was accomplished, changed, decided, found broken, or left next, using the commit messages, the "
    "assistant session results, the changed file names, and the person's own notes. Name the concrete features, "
    "fixes, files, and decisions. Never list which apps were open and never report bare file counts. Never invent "
    "completion, tests, problems, decisions, motives, or next steps: a decision or reason is included only when the "
    "evidence states it. If a packet only shows that files changed, say which files or areas changed in one plain "
    "sentence. Use null when unsupported. Session types must exactly match allowed values."
)
CONTENT_WORD = re.compile(r"[a-z][a-z0-9_.-]{3,}")
STOPWORDS = set("that this with from have were been they them their there what when which while would could should about into "
                "than then also just more most very much some each other over because being does doing made make added".split())
# Summaries that only restate window titles, app names, or file counts carry nothing for a reader.
ACTIVITY_ONLY = re.compile(r"(?i)^(?:the user )?(?:worked on|used|spent|navigated|modified \d+|changed \d+|accessed|activity recorded|no files)")


def _stems(text: str) -> set[str]:
    return {word[:6] for word in CONTENT_WORD.findall(str(text or "").casefold()) if word not in STOPWORDS}


# Generated files say nothing about the work.
BUILD_ARTIFACT = re.compile(r"(?:^|/)(?:__pycache__|node_modules|\.git|DerivedData|\.build|build|dist|\.next|\.venv)(?:/|$)|\.pyc$|\.DS_Store$|\.tmp$")


def _file_areas(names: list[str], limit: int = 12) -> list[str]:
    """Changed files as a reader would want them: individual files when there are few, otherwise the areas that changed
    with counts. Directory entries (a parent of another changed path) are dropped."""
    names = list(dict.fromkeys(name for name in names if name and not BUILD_ARTIFACT.search(name)))
    names = [name for name in names if not any(other != name and other.startswith(name.rstrip("/") + "/") for other in names)]
    if len(names) <= limit:
        return names
    areas: dict[str, list[str]] = {}
    for name in names:
        parts = name.split("/")
        area = "/".join(parts[:3]) if len(parts) > 3 else "/".join(parts[:-1]) or name
        areas.setdefault(area, []).append(name)
    ranked = sorted(areas.items(), key=lambda item: (-len(item[1]), item[0]))
    return [f"{area} ({len(files)} files)" if len(files) > 1 else files[0] for area, files in ranked[:limit]]


def _grounded(statement: str, evidence_text: str) -> bool:
    """At least half of a statement's content words must appear in the packet it summarises."""
    words = _stems(statement)
    return bool(words) and len(words & _stems(evidence_text)) * 2 >= len(words)


def _batches(packets: list[dict[str, Any]], max_chars: int = 7000, max_items: int = 3) -> list[list[dict[str, Any]]]:
    """Keep each request well inside the local model's context."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for packet in packets:
        length = len(json.dumps(packet, separators=(",", ":")))
        if current and (size + length > max_chars or len(current) >= max_items):
            batches.append(current); current, size = [], 0
        current.append(packet); size += length
    if current: batches.append(current)
    return batches


class WorklogSynthesizer:
    """Bounded, strict local-LM requests for factual narrative fields, cached per evidence packet."""

    def __init__(self, settings: TelemetrySettings, cache: TelemetryState | None = None, budget_seconds: float = 45.0) -> None:
        self.settings = settings
        self.cache = cache
        # The sync runs every few minutes under a hard timeout; stop starting new requests after this long. Uncached
        # packets keep their evidence-based summary this pass and are picked up by the next one (results are cached).
        self.budget_seconds = budget_seconds

    def synthesize(self, packets: list[dict[str, Any]], allowed_types: set[str]) -> dict[str, dict[str, Any]]:
        if not packets:
            return {}
        results: dict[str, dict[str, Any]] = {}
        pending: list[dict[str, Any]] = []
        keys: dict[str, str] = {}
        for packet in packets:
            key = "synthesis:" + _hash(SYNTHESIS_VERSION, json.dumps(packet, sort_keys=True), sorted(allowed_types))
            cached = self.cache.worklog_checkpoint(key) if self.cache else ""
            if cached:
                try:
                    results[str(packet["id"])] = json.loads(cached); continue
                except json.JSONDecodeError:
                    pass
            keys[str(packet["id"])] = key
            pending.append(packet)
        # Richest evidence first: the person's notes, then commits and assistant results, then file-only activity.
        pending.sort(key=lambda packet: -(10 * len(packet.get("your_notes") or []) + 3 * len(packet.get("commits") or [])
                                          + 2 * len(packet.get("assistant_sessions") or [])))
        started = time.monotonic()
        for index, batch in enumerate(_batches(pending)):
            # The first batch always runs, so every pass makes progress.
            if index and time.monotonic() - started > self.budget_seconds:
                break
            entries = self._request(batch, allowed_types, deadline=started + self.budget_seconds)
            for packet in batch:
                entry = entries.get(str(packet["id"]))
                if not entry:
                    continue
                entry = self._ground(entry, packet)
                results[str(packet["id"])] = entry
                if self.cache:
                    self.cache.save_worklog_checkpoint(keys[str(packet["id"])], json.dumps(entry))
        return results

    def _request(self, packets: list[dict[str, Any]], allowed_types: set[str], deadline: float | None = None) -> dict[str, dict[str, Any]]:
        endpoint = self.settings.lm_studio_endpoint.rstrip("/")
        if not endpoint.endswith("/v1"): endpoint += "/v1"
        text_list = {"type": "array", "items": {"type": "string"}}
        schema = {"type": "object", "properties": {"entries": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "title": {"type": "string"},
            "session_types": {"type": "array", "items": {"type": "string", "enum": sorted(allowed_types)} if allowed_types else {"type": "string"}},
            "what_happened": {"type": "string"}, "accomplishments": text_list, "decisions": text_list,
            "progress_summary": {"type": "string"}, "problems": {"type": ["string", "null"]},
            "next_step": {"type": ["string", "null"]}, "event": {"type": "string"}, "confidence": {"type": "number"},
        }, "required": ["id", "title", "session_types", "what_happened", "accomplishments", "decisions", "progress_summary", "problems", "next_step", "event", "confidence"], "additionalProperties": False}}}, "required": ["entries"], "additionalProperties": False}
        system = SYNTHESIS_SYSTEM
        body = {"model": self.settings.lm_studio_model, "temperature": 0, "max_tokens": 1800, "stream": False, "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps({"allowed_session_types": sorted(allowed_types), "entries": packets}, separators=(",", ":"))}], "response_format": {"type": "json_schema", "json_schema": {"name": "worklog_entries", "strict": True, "schema": schema}}, "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}
        for attempt in range(2):
            if attempt and deadline is not None and time.monotonic() > deadline:
                break  # no time left in this pass for a repair attempt
            try:
                request = Request(f"{endpoint}/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=self.settings.lm_studio_timeout_seconds) as response:
                    raw = json.loads(response.read().decode())
                content = raw["choices"][0]["message"]["content"]
                if not isinstance(content, str): raise ValueError("LM Studio returned non-text content")
                parsed = json.loads(content[content.find("{"):content.rfind("}") + 1])
                return self._validate(parsed, packets, allowed_types)
            except Exception:
                body["messages"][0]["content"] = system + " Repair the malformed prior attempt: emit a complete schema-valid JSON object now."
        return {}

    def _ground(self, entry: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any]:
        """Drop accomplishments and decisions the packet does not support, and summaries that only describe app use."""
        evidence = json.dumps({k: v for k, v in packet.items() if k not in {"id", "time"}}, ensure_ascii=False)
        entry = dict(entry)
        entry["accomplishments"] = [item for item in entry.get("accomplishments") or [] if _grounded(item, evidence)][:6]
        entry["decisions"] = [item for item in entry.get("decisions") or [] if _grounded(item, evidence)][:4]
        if ACTIVITY_ONLY.search(entry.get("what_happened") or "") or not _grounded(entry.get("what_happened") or "", evidence):
            entry["what_happened"] = ""
        return entry

    def _validate(self, value: Any, packets: list[dict[str, Any]], allowed: set[str]) -> dict[str, dict[str, Any]]:
        known = {str(packet["id"]) for packet in packets}
        result: dict[str, dict[str, Any]] = {}
        if not isinstance(value, dict) or not isinstance(value.get("entries"), list): raise ValueError("invalid worklog response")
        for entry in value["entries"]:
            if not isinstance(entry, dict) or str(entry.get("id")) not in known: raise ValueError("unknown worklog entry")
            # The local model writes the string "null" for empty values; treat it as empty rather than as content.
            entry = {key: (None if isinstance(item, str) and item.strip().casefold() in {"null", "none", "n/a"} else item) for key, item in entry.items()}
            for key in ("session_types", "accomplishments", "decisions"):
                if isinstance(entry.get(key), list):
                    entry[key] = [item for item in entry[key] if isinstance(item, str) and item.strip().casefold() not in {"null", "none", "n/a", ""}]
            for key in ("progress_summary", "event"):
                entry[key] = entry.get(key) or ""
            for key in ("problems", "next_step"):
                if isinstance(entry.get(key), str) and re.match(r"(?i)\s*(?:no (?:action|further|next|known|problems?|issues?)|none\b|nothing\b)", entry[key]):
                    entry[key] = None
            types = entry.get("session_types")
            if not isinstance(types, list): raise ValueError("invalid session type")
            # Unknown types are dropped; the work log falls back to the evidence-derived type.
            entry["session_types"] = [item for item in types if item in allowed]
            if not all(isinstance(entry.get(key), str) for key in ("title", "what_happened", "progress_summary", "event")): raise ValueError("missing narrative")
            confidence = entry.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)): raise ValueError("invalid confidence")
            lists = {key: [_safe_text(item, 300) for item in entry.get(key) or [] if isinstance(item, str) and item.strip()] for key in ("accomplishments", "decisions")}
            result[str(entry["id"])] = {**entry, **lists, "title": _safe_text(entry["title"], 120), "what_happened": _safe_text(entry["what_happened"], 1200), "progress_summary": _safe_text(entry["progress_summary"], 600), "event": _safe_text(entry["event"], 300), "problems": _safe_text(entry.get("problems"), 500) or None, "next_step": _safe_text(entry.get("next_step"), 500) or None, "confidence": max(0, min(1, float(confidence)))}
        return result


class NextWorkAnalyzer:
    """One bounded local-LM request that turns a project's existing Next
    Work signals into a concrete, human-readable action list, addressed
    directly to the person who will do the work."""

    def __init__(self, settings: TelemetrySettings) -> None:
        self.settings = settings

    def analyze(self, context: dict[str, Any]) -> str:
        endpoint = self.settings.lm_studio_endpoint.rstrip("/")
        if not endpoint.endswith("/v1"): endpoint += "/v1"
        schema = {"type": "object", "properties": {"next_work_details": {"type": "string"}}, "required": ["next_work_details"], "additionalProperties": False}
        system = (
            "Return only JSON. You write a short, concrete, actionable to-do list for one "
            "software project, addressed directly to the person who will do the work. Base it "
            "strictly on the supplied Next Work tags, prior status notes, and recent work-session "
            "notes — never invent tasks, tools, or outcomes they do not imply. Prefer concrete "
            "phrasing such as 'test the home screen widget on a physical device', 'commit and "
            "deploy the new site', or 'have the coding agent finish the interrupted plan for X'. "
            "Write one to five newline-separated items, most important first, in plain text with "
            "no markdown or numbering. If the supplied context gives no concrete signal, say so "
            "plainly instead of guessing."
        )
        body = {
            "model": self.settings.lm_studio_model, "temperature": 0.2, "max_tokens": 700, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(context, separators=(",", ":"))}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "next_work_details", "strict": True, "schema": schema}},
            "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False},
        }
        for attempt in range(2):
            if attempt and deadline is not None and time.monotonic() > deadline:
                break  # no time left in this pass for a repair attempt
            try:
                request = Request(f"{endpoint}/chat/completions", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=self.settings.lm_studio_timeout_seconds) as response:
                    raw = json.loads(response.read().decode())
                choice = raw["choices"][0]
                if choice.get("finish_reason") not in ("stop", "eos"):
                    raise ValueError("LM Studio output was truncated at its completion limit.")
                content = choice["message"]["content"]
                if not isinstance(content, str): raise ValueError("LM Studio returned non-text content")
                parsed = json.loads(content[content.find("{"):content.rfind("}") + 1])
                text = parsed.get("next_work_details")
                if not isinstance(text, str) or not text.strip(): raise ValueError("LM Studio returned empty next_work_details")
                return _safe_text(text, 1800)
            except Exception:
                body["messages"][0]["content"] = system + " Repair the malformed prior attempt: emit a complete schema-valid JSON object now."
        return ""


def _property(kind: str, value: Any) -> dict[str, Any]:
    return {
        "title": title_property, "rich_text": rich_text_property, "number": number_property,
        "date": date_property, "select": select_property, "multi_select": multi_select_property,
        "relation": relation_property, "checkbox": lambda item: {"checkbox": bool(item)},
    }[kind](value)


def _safe_text(value: Any, limit: int = 700) -> str:
    return SENSITIVE.sub("[redacted]", re.sub(r"\s+", " ", str(value or "")).strip())[:limit]


# Injected context (environment, AGENTS.md, system reminders) is not something the person asked for.
INJECTED_PROMPT = re.compile(r"^\s*(?:<|# AGENTS\.md|AGENTS\.md instructions|\[Request interrupted)")
ASSISTANT_NOISE = re.compile(r"(?i)^\s*(?:you've hit your|you have hit your|usage limit|rate limit|api error|request failed)")


def _text_parts(content: Any) -> str:
    """Transcript content is a string or a list of typed parts; keep only the text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"})
    return ""


def _read_codex(path: Path) -> tuple[str, str, str, str] | None:
    """(cwd, first genuine request, final reply, time of final reply) from a Codex session file."""
    cwd, prompt, response, timestamp = "", "", "", ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for raw in lines:
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if row.get("type") == "session_meta":
            cwd = cwd or str(payload.get("cwd") or "")
            continue
        role, text = "", ""
        if row.get("type") == "response_item" and payload.get("type") == "message":
            role, text = str(payload.get("role") or ""), _text_parts(payload.get("content"))
        elif row.get("type") == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
            role, text = ("user" if payload["type"] == "user_message" else "assistant"), str(payload.get("message") or "")
        text = text.strip()
        if not text:
            continue
        if role == "user" and not prompt and not INJECTED_PROMPT.search(text):
            prompt = text
        elif role == "assistant" and not ASSISTANT_NOISE.search(text):
            response, timestamp = text, str(row.get("timestamp") or timestamp)
    return (cwd, prompt, response, timestamp) if timestamp and (prompt or response) else None


def _read_claude(path: Path) -> tuple[str, str, str, str] | None:
    """(cwd, first genuine request, final reply, time of final reply) from a Claude Code transcript (main thread only)."""
    cwd, prompt, response, timestamp = "", "", "", ""
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for raw in handle:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("isSidechain"):
                continue
            cwd = cwd or str(row.get("cwd") or "")
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            text = _text_parts(message.get("content")).strip()
            if not text:
                continue
            if row.get("type") == "user" and not prompt and not INJECTED_PROMPT.search(text):
                prompt = text
            elif row.get("type") == "assistant" and not ASSISTANT_NOISE.search(text):
                response, timestamp = text, str(row.get("timestamp") or timestamp)
    return (cwd, prompt, response, timestamp) if timestamp and (prompt or response) else None


def _assistant_event(tool: str, path: Path, when: datetime, cwd: str, prompt: str, response: str) -> "Evidence | None":
    intent = _safe_text(prompt, 400)
    outcome = _safe_text(response, 1200)
    if not (intent or outcome):
        return None
    prefix = "codex" if tool == "Codex" else "claude"
    return Evidence(_hash(prefix, path, when.isoformat()), "ai", when, when + timedelta(seconds=1), workspace=cwd, category="Build",
                    summary=_safe_text(intent or outcome, 260),
                    payload={"tool": tool, "conversation_id": path.stem, "intent": intent, "outcome": outcome, "retention_level": "local_trace",
                             "sensitivity": "high" if "[redacted]" in intent + outcome else "low", "trace_ref": _hash(path)})


class WorklogPipeline:
    def __init__(self, settings: TelemetrySettings, notion: Any, *, state: TelemetryState | None = None) -> None:
        self.settings, self.notion = settings, notion
        self.state = state or TelemetryState(settings.state_path)
        self.config = settings.worklog
        self.journal_path = Path(self.config.get("filesystemJournalPath") or Path.home() / "Library/Application Support/WorkspaceTelemetry/fs-events.jsonl")
        self.terminal_journal_path = Path(self.config.get("terminalJournalPath") or Path.home() / "Library/Application Support/WorkspaceTelemetry/terminal-events.jsonl")
        self.catalog = ProjectCatalog.load(notion, settings)
        projects_source_id = notion.data_source_id(settings.projects_database_id)
        projects_schema = notion.get_data_source(projects_source_id).get("properties") or {}
        self.next_work_options = {
            str(option.get("name")) for option in ((projects_schema.get("Next Work") or {}).get("multi_select") or {}).get("options", [])
            if option.get("name")
        }
        self.activity_store = WorklogStore(notion, settings.activity_sessions_database_id, {"Name": "title", "event_hash": "rich_text", "project": "relation", "session start": "date", "session end": "date"})
        self.ai_store = WorklogStore(notion, settings.ai_chat_events_database_id, {"Name": "title", "event_hash": "rich_text", "timestamp": "date", "tool": "select"})
        self.git_store = WorklogStore(notion, settings.git_events_database_id, {"Name": "title", "event_hash": "rich_text", "timestamp": "date", "event_type": "select"})
        self.work_store = WorklogStore(notion, settings.work_log_database_id, {"entry": "title", "event_hash": "rich_text", "project": "relation", "date": "date", "Session type": "multi_select"})

    def close(self) -> None: self.state.close()

    def doctor(self) -> dict[str, Any]:
        return {"ok": True, "projects": len(self.catalog.projects), "watchRoots": self.catalog.watch_roots(), "journal": str(self.journal_path), "activityWatch": ActivityWatchClient(self.settings.activitywatch_base_url).collect(TimeRange(_now(self.settings)-timedelta(minutes=1), _now(self.settings)), self.settings.privacy).available}

    def write_watch_manifest(self, *, dry_run: bool = False) -> dict[str, Any]:
        path = Path(self.config.get("watchRootsManifestPath") or Path.home() / "Library/Application Support/WorkspaceTelemetry/watch-roots.json")
        payload = json.dumps({"roots": self.catalog.watch_roots(), "generatedAt": _now(self.settings).isoformat()}, sort_keys=True)
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(path)
        return {"path": str(path), "roots": self.catalog.watch_roots(), "dryRun": dry_run}

    def sync(self, *, day: datetime | None = None, dry_run: bool = False) -> dict[str, Any]:
        now = _now(self.settings)
        start = (day or now).replace(hour=0, minute=0, second=0, microsecond=0)
        end = min(now, start + timedelta(days=1))
        if end <= start: end = start + timedelta(days=1)
        interval = TimeRange(start, end)
        events = self._collect(interval)
        self.write_watch_manifest(dry_run=dry_run)
        persisted = self._persist_evidence(events, dry_run=dry_run)
        pruned = self._prune_evidence_daily(dry_run=dry_run)
        candidates = self._sessions(events, interval)
        if not dry_run:
            for candidate in candidates:
                self._write_activity_session(candidate, dry_run=False)
        worklogs = self._upsert_worklogs(candidates, start, dry_run=dry_run)
        next_steps = self.sync_next_steps(dry_run=dry_run)
        try:
            next_work_analysis = self.analyze_next_work(dry_run=dry_run)
        except Exception as exc:
            # A checked box just retries next sync; never let a schema hiccup
            # or an unreachable LM Studio fail the rest of the sync pass.
            next_work_analysis = [{"ok": False, "error": str(exc)}]
        return {"ok": True, "date": start.date().isoformat(), "evidence": len(events), "persisted": persisted, "sessions": len(candidates), "workLogs": worklogs, "nextSteps": next_steps, "nextWorkAnalysis": next_work_analysis, "pruned": pruned}

    def _prune_evidence_daily(self, *, dry_run: bool) -> dict[str, Any]:
        """Apply raw-evidence retention (TelemetryState.WORKLOG_RETENTION_DAYS) once per local day."""
        today = _now(self.settings).date().isoformat()
        if dry_run or self.state.worklog_checkpoint("worklog_prune_day") == today:
            return {}
        try:
            removed: dict[str, Any] = self.state.prune_worklog_events(_now(self.settings))
        except Exception as exc:
            # Retention is housekeeping; a locked or busy database must not fail the sync.
            return {"error": str(exc)}
        self.state.save_worklog_checkpoint("worklog_prune_day", today)
        return removed

    def sync_next_steps(self, *, dry_run: bool = False) -> list[dict[str, Any]]:
        """Sync each project's docs/NEXT_STEPS.md (an existing agent-report
        convention) onto its Notion page: classified "Next Work" tags plus the
        full text in "status details". Skips projects with no Local Directory,
        no report file, or unchanged content since the last sync."""

        results: list[dict[str, Any]] = []
        for project in self.catalog.projects:
            if not project.local_directory:
                continue
            root = Path(os.path.expanduser(project.local_directory))
            report_path = next((root / relative for relative in NEXT_STEPS_RELATIVE_PATHS if (root / relative).is_file()), None)
            if report_path is None:
                continue
            try:
                content = report_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not content.strip():
                continue
            content_hash = _hash("next-steps", content)
            checkpoint_key = f"next_steps_hash:{project.id}"
            if self.state.worklog_checkpoint(checkpoint_key) == content_hash:
                continue
            tags = _classify_next_work(content, self.next_work_options)
            properties: dict[str, Any] = {"status details": rich_text_property(_clean_report_text(content))}
            if tags:
                properties["Next Work"] = multi_select_property(tags)
            if not dry_run:
                self.notion.update_page(project.id, properties)
                self.state.save_worklog_checkpoint(checkpoint_key, content_hash)
            results.append({"projectId": project.id, "project": project.name, "reportPath": str(report_path), "tags": tags, "action": "would-update" if dry_run else "updated"})
        return results

    def analyze_next_work(self, *, dry_run: bool = False) -> list[dict[str, Any]]:
        """For each Notion Projects page with "Needs Next Work Analysis"
        checked, ask the local LM to turn its existing "Next Work" tags,
        prior "status details", and recent Work Log entries into a concrete,
        human-readable write-up in "Next Work Details", then uncheck the box.
        A failed or unreachable LM Studio call leaves the checkbox checked
        and the prior "Next Work Details" untouched, so the project is simply
        retried on the next sync."""

        projects_source = self.notion.data_source_id(self.settings.projects_database_id)
        pages = self.notion.query_data_source(projects_source, filter_payload={"property": "Needs Next Work Analysis", "checkbox": {"equals": True}})
        pages = pages[:MAX_NEXT_WORK_ANALYSES_PER_SYNC]
        cutoff = (_now(self.settings) - timedelta(days=60)).date().isoformat()
        analyzer = NextWorkAnalyzer(self.settings)
        results: list[dict[str, Any]] = []
        for page in pages:
            props = page.get("properties") or {}
            project_id = str(page.get("id") or "")
            name = str(flatten_property(props.get("title") or {})).strip() or "Project"
            sessions = self._recent_work_sessions(project_id, cutoff)
            if dry_run:
                results.append({"projectId": project_id, "project": name, "sessionsConsidered": len(sessions), "action": "would-analyze"})
                continue
            context = {
                "project": name,
                "existing_next_work_tags": flatten_property(props.get("Next Work") or {}) or [],
                "existing_status_details": plain_text(page, "status details"),
                "recent_work_sessions": sessions,
            }
            details = analyzer.analyze(context)
            if not details:
                results.append({"projectId": project_id, "project": name, "sessionsConsidered": len(sessions), "action": "skipped-empty"})
                continue
            self.notion.update_page(project_id, {"Next Work Details": rich_text_property(details), "Needs Next Work Analysis": {"checkbox": False}})
            results.append({"projectId": project_id, "project": name, "sessionsConsidered": len(sessions), "action": "updated"})
        return results

    def _recent_work_sessions(self, project_id: str, cutoff_date: str, limit: int = 5) -> list[dict[str, Any]]:
        pages = self.notion.query_data_source(self.work_store.source_id, filter_payload={"and": [
            {"property": "project", "relation": {"contains": project_id}},
            {"property": "date", "date": {"on_or_after": cutoff_date}},
        ]})
        pages.sort(key=lambda entry: plain_text(entry, "date"), reverse=True)
        return [{
            "date": plain_text(entry, "date"),
            "what_happened": plain_text(entry, "What Happened"),
            "progress_summary": plain_text(entry, "progress_summary"),
            "problems": plain_text(entry, "Problems"),
            "next_step": plain_text(entry, "Next Step"),
        } for entry in pages[:limit]]

    def reconcile(self, *, dry_run: bool = False) -> dict[str, Any]:
        snapshot = ActivityWatchClient(self.settings.activitywatch_base_url).collect(TimeRange(_now(self.settings)-timedelta(days=180), _now(self.settings)), self.settings.privacy)
        starts = [event.start.astimezone(self.settings.zoneinfo()).replace(hour=0, minute=0, second=0, microsecond=0) for event in snapshot.events]
        if not starts:
            return {"ok": True, "status": "no-activitywatch-history"}
        cursor = self.state.worklog_checkpoint("reconcile_day")
        current = _parse_datetime(cursor, self.settings.zoneinfo()) if cursor else min(starts)
        today = _now(self.settings).replace(hour=0, minute=0, second=0, microsecond=0)
        if current > today:
            return {"ok": True, "status": "complete"}
        result = self.sync(day=current, dry_run=dry_run)
        if not dry_run: self.state.save_worklog_checkpoint("reconcile_day", (current + timedelta(days=1)).isoformat())
        return {"ok": True, "status": "processed", "result": result}

    def _collect(self, interval: TimeRange) -> list[Evidence]:
        events: list[Evidence] = []
        vscode_roots = self._vscode_roots()
        snapshot = ActivityWatchClient(self.settings.activitywatch_base_url).collect(interval, self.settings.privacy)
        for item in snapshot.events:
            kind = "afk" if item.afk else "activity"
            workspace = item.work_label
            if workspace and not workspace.startswith("/"):
                matches = [root for root in vscode_roots if _normal(Path(root).name) == _normal(workspace)]
                if len(matches) == 1:
                    workspace = matches[0]
            events.append(Evidence(_hash(kind, item.source_id, item.start, item.end), kind, item.start, item.end, app=item.app, domain=item.domain, workspace=workspace, category=item.category, summary=item.work_label))
            tool = {"chatgpt": "ChatGPT", "claude": "Claude Code", "claude code": "Claude Code", "codex": "Codex"}.get(item.app.casefold())
            if tool and not item.afk and (item.end - item.start).total_seconds() >= 30:
                # Window telemetry is metadata-only for tools without a local
                # transcript adapter. It is intentionally not treated as a
                # prompt or response and cannot leak conversation contents.
                events.append(Evidence(_hash("ai-window", item.source_id), "ai", item.start, item.end, app=item.app, workspace=workspace, category="Build", summary=f"{tool} activity", payload={"tool": tool, "conversation_id": item.source_id, "intent": "", "outcome": "", "retention_level": "metadata_only", "sensitivity": "low", "trace_ref": ""}))
        filesystem = self._filesystem_events(interval)
        terminal = self._terminal_events(interval)
        events.extend(filesystem)
        events.extend(terminal)
        events.extend(self._git_events(interval, [*filesystem, *terminal]))
        events.extend(self._codex_events(interval))
        events.extend(self._claude_events(interval))
        return [self.catalog.match(event) for event in events]

    def _vscode_roots(self) -> list[str]:
        storage = Path.home() / "Library/Application Support/Code/User/workspaceStorage"
        roots: set[str] = set()
        if not storage.exists():
            return []
        for path in storage.glob("*/workspace.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            folder = payload.get("folder") if isinstance(payload, dict) else ""
            if isinstance(folder, str) and folder.startswith("file:"):
                roots.add(_file_uri_path(folder))
            for item in (payload.get("folders") or []) if isinstance(payload, dict) else []:
                if isinstance(item, dict) and isinstance(item.get("uri"), str) and item["uri"].startswith("file:"):
                    roots.add(_file_uri_path(item["uri"]))
        return sorted(root for root in roots if root)

    def _filesystem_events(self, interval: TimeRange) -> list[Evidence]:
        if not self.journal_path.exists(): return []
        offset = int(self.state.worklog_checkpoint("filesystem_journal_offset") or "0")
        size = self.journal_path.stat().st_size
        if offset > size: offset = 0
        result: list[Evidence] = []
        with self.journal_path.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            for raw in handle:
                try: row = json.loads(raw)
                except json.JSONDecodeError: continue
                path = os.path.expanduser(str(row.get("path") or ""))
                # Finder metadata and generated outputs describe ambient
                # filesystem churn, not deliberate work on the named project.
                # Do not let them seed passive Work Logs or project matches.
                if (not path or Path(path).name == ".DS_Store"
                        or any(token in path for token in tuple(self.config.get("ignorePathFragments") or DEFAULT_IGNORES))):
                    continue
                timestamp = _parse_datetime(row.get("timestamp") or _now(self.settings).isoformat(), self.settings.zoneinfo())
                if interval.start <= timestamp < interval.end:
                    result.append(Evidence(_hash("fs", timestamp.isoformat(), path), "filesystem", timestamp, timestamp + timedelta(seconds=1), path=path, workspace=str(Path(path).parent), category="Coding", summary=f"Changed {Path(path).name}"))
            new_offset = handle.tell()
        self.state.save_worklog_checkpoint("filesystem_journal_offset", str(new_offset))
        return result

    def _terminal_events(self, interval: TimeRange) -> list[Evidence]:
        if not self.terminal_journal_path.exists(): return []
        offset = int(self.state.worklog_checkpoint("terminal_journal_offset") or "0")
        size = self.terminal_journal_path.stat().st_size
        if offset > size: offset = 0
        result: list[Evidence] = []
        with self.terminal_journal_path.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            for raw in handle:
                try: row = json.loads(raw)
                except json.JSONDecodeError: continue
                timestamp = _parse_datetime(row.get("timestamp") or _now(self.settings).isoformat(), self.settings.zoneinfo())
                if not (interval.start <= timestamp < interval.end): continue
                cwd = os.path.expanduser(str(row.get("cwd") or ""))
                repo = os.path.expanduser(str(row.get("repo_root") or ""))
                category = str(row.get("category") or "shell")[:40]
                result.append(Evidence(_hash("terminal", timestamp.isoformat(), cwd, category), "terminal", timestamp, timestamp + timedelta(seconds=1), path=cwd, repo=repo, workspace=repo or cwd, category="Coding", summary=f"Terminal {category}"))
            new_offset = handle.tell()
        self.state.save_worklog_checkpoint("terminal_journal_offset", str(new_offset))
        return result

    def _git_events(self, interval: TimeRange, hints: list[Evidence]) -> list[Evidence]:
        events: list[Evidence] = []
        for root in self._repos(hints):
            try:
                output = subprocess.check_output(["git", "-C", root, "log", "--all", f"--since={interval.start.isoformat()}", f"--until={interval.end.isoformat()}", "--format=%x1e%H%x1f%cI%x1f%P%x1f%s%x1f%b%x1f", "--name-only"], text=True, stderr=subprocess.DEVNULL, timeout=8)
            except (OSError, subprocess.SubprocessError):
                continue
            for block in output.split("\x1e")[1:]:
                head, _, names = block.rpartition("\x1f")
                fields = head.split("\x1f")
                if len(fields) < 5: continue
                commit, timestamp, parents, subject, body = fields[:5]
                when = _parse_datetime(timestamp, self.settings.zoneinfo())
                paths = [line.strip() for line in names.splitlines() if line.strip()]
                # The full message carries the why and the detail that a subject line leaves out.
                payload = {"event_type": "merge" if len(parents.split()) > 1 else "commit", "head_commit": commit, "branch": self._git_branch(root), "changed_paths": paths[:20], "changed_count": len(paths), "summary": _safe_text(subject, 300), "body": _safe_text(body, 1200)}
                events.append(Evidence(_hash("git", root, commit), "git", when, when + timedelta(seconds=1), repo=root, workspace=root, category="Build", summary=payload["summary"], payload=payload))
            try:
                reflog = subprocess.check_output(["git", "-C", root, "reflog", "--date=iso", "--format=%H%x1f%gD%x1f%gs"], text=True, stderr=subprocess.DEVNULL, timeout=5)
            except (OSError, subprocess.SubprocessError): reflog = ""
            for line in reflog.splitlines():
                if "\x1f" not in line or "checkout:" not in line.casefold(): continue
                commit, ref, summary = line.split("\x1f", 2)
                match = re.search(r"@(\{(.+?)\})", ref)
                if not match: continue
                try: when = _parse_datetime(match.group(2), self.settings.zoneinfo())
                except ValueError: continue
                if interval.start <= when < interval.end:
                    events.append(Evidence(_hash("checkout", root, commit, when), "git", when, when + timedelta(seconds=1), repo=root, workspace=root, category="Build", summary="Checked out branch", payload={"event_type": "checkout", "head_commit": commit, "branch": self._git_branch(root), "summary": "Checked out branch"}))
        return events

    def _repos(self, hints: list[Evidence]) -> list[str]:
        """Discover repos from current work evidence, never by broad polling."""

        candidates = [item.path or item.repo or item.workspace for item in hints]
        candidates.extend(path for project in self.catalog.projects for path in project.repos)
        result: set[str] = set()
        for candidate in candidates:
            root = self._git_root(candidate)
            if root: result.add(root)
        return sorted(result)

    def _git_root(self, candidate: str) -> str:
        if not candidate: return ""
        path = Path(os.path.expanduser(candidate))
        path = path if path.is_dir() else path.parent
        for parent in (path, *path.parents):
            if (parent / ".git").exists(): return str(parent)
            if str(parent) in self.catalog.watch_roots(): break
        return ""

    def _git_branch(self, root: str) -> str:
        try: return subprocess.check_output(["git", "-C", root, "branch", "--show-current"], text=True, stderr=subprocess.DEVNULL, timeout=4).strip()
        except (OSError, subprocess.SubprocessError): return ""

    def _codex_events(self, interval: TimeRange) -> list[Evidence]:
        root = Path.home() / ".codex/sessions"
        if not root.exists(): return []
        events: list[Evidence] = []
        days: list[datetime] = []
        cursor = interval.start.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor <= interval.end:
            days.append(cursor)
            cursor += timedelta(days=1)
        paths = [path for day in days for path in (root / day.strftime("%Y/%m/%d")).glob("*.jsonl")]
        for path in paths:
            if path.stat().st_mtime < interval.start.timestamp() - 86400: continue
            conversation = _read_codex(path)
            if not conversation: continue
            cwd, prompt, response, timestamp = conversation
            when = _parse_datetime(timestamp, self.settings.zoneinfo())
            if not (interval.start <= when < interval.end): continue
            event = _assistant_event("Codex", path, when, cwd, prompt, response)
            if event: events.append(event)
        return events

    def _claude_events(self, interval: TimeRange) -> list[Evidence]:
        """Claude Code sessions: what was asked and the final summary, like the Codex adapter."""
        root = Path.home() / ".claude/projects"
        if not root.exists(): return []
        events: list[Evidence] = []
        for path in root.glob("*/*.jsonl"):
            try:
                if path.stat().st_mtime < interval.start.timestamp(): continue
            except OSError:
                continue
            conversation = _read_claude(path)
            if not conversation: continue
            cwd, prompt, response, timestamp = conversation
            when = _parse_datetime(timestamp, self.settings.zoneinfo())
            if not (interval.start <= when < interval.end): continue
            event = _assistant_event("Claude Code", path, when, cwd, prompt, response)
            if event: events.append(event)
        return events

    def _persist_evidence(self, events: list[Evidence], *, dry_run: bool) -> dict[str, int]:
        counts = {"activity": 0, "filesystem": 0, "git": 0, "ai": 0}
        for event in events:
            # `record_worklog_event` is an `INSERT OR IGNORE` keyed on the event
            # hash -- the module docstring's "SQLite is the source of
            # incremental truth" already assumes this return value gates what
            # is new. Every sync re-collects the *whole* local day, so without
            # this gate every 10-minute cycle re-upserted every git/AI event
            # recorded since midnight to Notion, one at a time -- a redundant,
            # ever-growing cost across the day that was the main reason
            # worklog_sync increasingly timed out at 120s later in the day.
            is_new = dry_run or self.state.record_worklog_event(event.hash, event.kind, event.start, event.end, {**event.__dict__, "start": event.start.isoformat(), "end": event.end.isoformat()})
            if event.kind in counts: counts[event.kind] += 1
            if not dry_run and is_new and event.kind == "git": self._write_git(event, False)
            elif not dry_run and is_new and event.kind == "ai": self._write_ai(event, False)
        return counts

    def _write_git(self, event: Evidence, dry_run: bool) -> None:
        data = event.payload; props = {"Name": title_property(f"{data.get('event_type', 'commit')} in {Path(event.repo).name}"), "timestamp": date_property(event.start.isoformat()), "date": date_property(event.start.date().isoformat()), "repo_name": rich_text_property(Path(event.repo).name), "repo_root": rich_text_property(event.repo), "branch": rich_text_property(data.get("branch", "")), "event_type": select_property(data.get("event_type", "commit")), "head_commit": rich_text_property(data.get("head_commit", "")), "changed_files_count": number_property(data.get("changed_count") or len(data.get("changed_paths") or [])), "changed_paths_preview": rich_text_property("\n".join(data.get("changed_paths") or [])[:1200]), "summary": rich_text_property(data.get("summary", "")), "event_hash": rich_text_property(event.hash), "project confidence": number_property(round(event.confidence, 3)), "matched by": select_property(event.matched_by)}
        if event.project_id: props["project"] = relation_property([event.project_id])
        self.git_store.upsert(props, event.hash, dry_run=dry_run)

    def _write_ai(self, event: Evidence, dry_run: bool) -> None:
        data = event.payload; props = {"Name": title_property(f"{data.get('tool') or 'AI'} — {event.start.strftime('%H:%M')}"), "tool": select_property(data.get("tool", "Generic AI Tool")), "timestamp": date_property(event.start.isoformat()), "date": date_property(event.start.date().isoformat()), "conversation_id": rich_text_property(data.get("conversation_id", "")), "workspace_path": rich_text_property(event.workspace), "intent": rich_text_property(data.get("intent", "")), "prompt_summary": rich_text_property(event.summary), "response_summary": rich_text_property(data.get("outcome", "")), "outcome": rich_text_property(data.get("outcome", "")), "retention_level": select_property(data.get("retention_level", "metadata_only")), "sensitivity": select_property(data.get("sensitivity", "low")), "trace_ref": rich_text_property(data.get("trace_ref", "")), "event_hash": rich_text_property(event.hash), "project confidence": number_property(round(event.confidence, 3)), "matched by": select_property(event.matched_by)}
        if event.project_id: props["project"] = relation_property([event.project_id])
        self.ai_store.upsert(props, event.hash, dry_run=dry_run)

    def _write_activity_session(self, events: list[Evidence], dry_run: bool) -> None:
        project_id = next((event.project_id for event in events if event.project_id), "")
        if not project_id:
            return
        start, end = min(event.start for event in events), max(event.end for event in events)
        core = [event for event in events if event.kind not in {"activity", "afk"}]
        seed = "|".join(sorted(event.hash for event in core))
        event_hash = _hash("activity-session", project_id, start.date().isoformat(), seed)
        apps = [event.app for event in events if event.app]
        domains = [event.domain for event in events if event.domain]
        paths = [event.path or event.workspace or event.repo for event in core if event.path or event.workspace or event.repo]
        # A session with only foreground-window/AFK evidence (no git, AI, or
        # filesystem activity) leaves `core` empty; fall back to the whole
        # session's confidence instead of crashing on max() of nothing.
        core_confidence = [event.confidence for event in core]
        project_confidence = max(core_confidence) if core_confidence else max(event.confidence for event in events)
        props = {
            "Name": title_property(f"Activity in {(apps[0] if apps else 'workspace')}"),
            "date": date_property(start.date().isoformat()), "session start": date_property(start.isoformat()),
            "session end": date_property(end.isoformat()), "duration seconds": number_property(round(_union_seconds([(event.start, event.end) for event in events if event.kind == "activity"]))),
            "dominant app": rich_text_property(apps[0] if apps else ""), "dominant domain": rich_text_property(domains[0] if domains else ""),
            "workspace path": rich_text_property(paths[0] if paths else ""), "repo root": rich_text_property(next((event.repo for event in core if event.repo), "")),
            "branch": rich_text_property(next((str(event.payload.get("branch") or "") for event in core if event.payload.get("branch")), "")),
            "project": relation_property([project_id]), "project confidence": number_property(round(project_confidence, 3)),
            "matched by": select_property(next((event.matched_by for event in core if event.matched_by != "unresolved"), "unresolved")),
            "ai overlap count": number_property(sum(event.kind == "ai" for event in core)), "git overlap count": number_property(sum(event.kind == "git" for event in core)),
            "summary": rich_text_property(_safe_text("; ".join(event.summary for event in core if event.summary), 700)), "unresolved": {"checkbox": False}, "event_hash": rich_text_property(event_hash),
        }
        self.activity_store.upsert(props, event_hash, dry_run=dry_run)

    def _sessions(self, events: list[Evidence], interval: TimeRange) -> list[list[Evidence]]:
        grouped: list[list[Evidence]] = []
        ambient = [event for event in events if event.kind in {"activity", "afk"}]
        for project_id in sorted({event.project_id for event in events if event.project_id}):
            source = sorted([event for event in events if event.project_id == project_id and event.kind != "afk"], key=lambda event: event.start)
            current: list[Evidence] = []
            for event in source:
                if current and event.start - max(item.end for item in current) >= timedelta(minutes=20):
                    grouped.append(current); current = []
                current.append(event)
            if current: grouped.append(current)
        # Window/AFK telemetry is deliberately not project-matched on its own.
        # It becomes time accounting only after strong project evidence creates
        # a session boundary, which prevents an open editor from becoming work.
        for group in grouped:
            first = min(event.start for event in group)
            last = max(event.end for event in group)
            group.extend(event for event in ambient if event.end > first and event.start < last)
        return grouped

    def _upsert_worklogs(self, groups: list[list[Evidence]], day: datetime, *, dry_run: bool) -> list[dict[str, Any]]:
        by_project: dict[str, list[list[Evidence]]] = {}
        for group in groups: by_project.setdefault(group[0].project_id, []).append(group)
        results: list[dict[str, Any]] = []
        allowed_types = self.work_store.options("Session type")
        prepared: list[tuple[str, list[Evidence], str, list[dict[str, Any]]]] = []
        for project_id, project_groups in by_project.items():
            clusters: list[list[Evidence]] = []
            current: list[Evidence] = []
            for group in sorted(project_groups, key=lambda value: value[0].start):
                if current and group[0].start - max(item.end for item in current) >= timedelta(hours=4): clusters.append(current); current = []
                current.extend(group)
            if current: clusters.append(current)
            clusters = clusters[:3]
            existing = self.work_store.project_day_pages(project_id, day)
            for index, cluster in enumerate(clusters, 1):
                event_hash = _hash(WORKLOG_VERSION, project_id, day.date().isoformat(), index)
                prepared.append((project_id, cluster, event_hash, existing))
        notes = self._session_notes(day)
        packets = [self._evidence_packet(cluster, project_id, day, event_hash, notes.get(project_id, [])) for project_id, cluster, event_hash, _ in prepared]
        semantic = {} if dry_run else WorklogSynthesizer(self.settings, cache=self.state).synthesize(packets, allowed_types)
        for project_id, cluster, event_hash, existing in prepared:
                props = self._worklog_properties(cluster, project_id, day, event_hash, allowed_types, semantic.get(event_hash), notes.get(project_id, []))
                target = next((page for page in existing if plain_text(page, "event_hash") == event_hash), None)
                if target and not dry_run:
                    self.notion.update_page(str(target.get("id") or ""), props); action, page_id = "updated", str(target.get("id") or "")
                elif target: action, page_id = "would-update", str(target.get("id") or "")
                elif len(existing) >= 3:
                    page_id, action = str(existing[0].get("id") or ""), "would-merge" if dry_run else "merged"
                    if not dry_run: self.notion.update_page(page_id, props)
                else:
                    page_id, action = self.work_store.upsert(props, event_hash, dry_run=dry_run)
                results.append({"projectId": project_id, "eventHash": event_hash, "action": action, "pageId": page_id})
        return results

    def _session_notes(self, day: datetime) -> dict[str, list[str]]:
        """What the person typed when finishing a work session ("What did you accomplish?"), by project, for this day.
        Optional and never prompted for here: sessions without a note simply contribute nothing."""
        path = getattr(self, "session_inbox_path", None) or Path.home() / "Library/Application Support/WorkspaceTelemetry/inbox/work-sessions.jsonl"
        notes: dict[str, list[str]] = {}
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return notes
        for raw in lines:
            try:
                row = json.loads(raw)
                when = _parse_datetime(str(row.get("timestamp") or ""), self.settings.zoneinfo())
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            text = _safe_text(row.get("summary"), 600)
            # The session outcome labels ("Made progress") are not notes; a sentence is.
            if when.date() != day.date() or len(text.split()) < 4:
                continue
            for project_id in dict.fromkeys([*(row.get("project_ids") or []), row.get("project_id") or ""]):
                if project_id:
                    notes.setdefault(str(project_id), []).append(text)
        return notes

    def _relative(self, project: Project, path: str) -> str:
        for root in (project.local_directory, *project.repos, *project.paths):
            if root and path.startswith(root.rstrip("/") + "/"):
                return path[len(root.rstrip("/")) + 1:]
        return Path(path).name

    def _evidence_packet(self, events: list[Evidence], project_id: str, day: datetime, event_hash: str, notes: list[str] | None = None) -> dict[str, Any]:
        """What the work log is written from: commits, assistant results, changed files, and the person's own notes.
        App names and window titles stay out, so the summary describes the work rather than the tools."""
        project = next((item for item in self.catalog.projects if item.id == project_id), Project(project_id, "Project"))
        commits = [event for event in events if event.kind == "git" and event.payload.get("event_type") in {"commit", "merge"}]
        assistants = [event for event in events if event.kind == "ai" and (event.payload.get("intent") or event.payload.get("outcome"))]
        changed = list(dict.fromkeys(self._relative(project, event.path) for event in events if event.kind == "filesystem" and event.path))
        committed = list(dict.fromkeys(name for event in commits for name in event.payload.get("changed_paths") or []))
        files = list(dict.fromkeys(committed + changed))
        return {"id": event_hash, "project": {"id": project_id, "name": project.name}, "date": day.date().isoformat(),
                "time": {"first_activity": min(event.start for event in events).isoformat(), "last_activity": max(event.end for event in events).isoformat(), "active_seconds": round(_union_seconds([(event.start, event.end) for event in events if event.kind == "activity"])), "idle_seconds": round(_union_seconds([(event.start, event.end) for event in events if event.kind == "afk"]))},
                "your_notes": list(notes or [])[:3],
                "commits": [{"subject": event.summary, "message": _safe_text(event.payload.get("body"), 600), "files": (event.payload.get("changed_paths") or [])[:10]} for event in commits[-8:]],
                "assistant_sessions": [{"tool": event.payload.get("tool"), "asked": _safe_text(event.payload.get("intent"), 300), "result": _safe_text(event.payload.get("outcome"), 700)} for event in assistants[-4:]],
                "files_changed": {"count": len(files), "names": _file_areas(files)}}

    def _fallback_summary(self, events: list[Evidence], project_id: str, notes: list[str]) -> str:
        """Without the model: the person's notes, commit subjects, assistant results, and changed file names."""
        project = next((item for item in self.catalog.projects if item.id == project_id), Project(project_id, "Project"))
        commits = list(dict.fromkeys(event.summary for event in events if event.kind == "git" and event.payload.get("event_type") in {"commit", "merge"} and event.summary))
        results = [_safe_text(str(event.payload.get("outcome") or "").split(". ")[0], 200) for event in events if event.kind == "ai" and event.payload.get("outcome")]
        files = _file_areas(list(dict.fromkeys(self._relative(project, event.path) for event in events if event.kind == "filesystem" and event.path)), 8)
        parts = []
        if notes: parts.append("In your words: " + " ".join(notes))
        if commits: parts.append("Commits: " + "; ".join(commits[:6]) + (f" (+{len(commits) - 6} more)" if len(commits) > 6 else ""))
        if results: parts.append("Assistant work: " + "; ".join(dict.fromkeys(results[-3:])))
        if files: parts.append("Changed: " + ", ".join(files))
        return _safe_text("\n".join(parts), 1800) if parts else ""

    def _worklog_properties(self, events: list[Evidence], project_id: str, day: datetime, event_hash: str, allowed_types: set[str], semantic: dict[str, Any] | None = None, notes: list[str] | None = None) -> dict[str, Any]:
        start, end = min(event.start for event in events), max(event.end for event in events)
        active = _union_seconds([(event.start, event.end) for event in events if event.kind == "activity"])
        idle = _union_seconds([(event.start, event.end) for event in events if event.kind == "afk"])
        git = [event for event in events if event.kind == "git"]; ai = [event for event in events if event.kind == "ai"]
        files = [event for event in events if event.kind == "filesystem"]
        categories = {event.category.casefold() for event in events}
        types = [kind for kind, condition in (("Build", bool(git or files or "coding" in categories)), ("Research", "research" in categories), ("Planning", "planning" in categories), ("Writing", "writing" in categories)) if condition and kind in allowed_types]
        if not types and "Build" in allowed_types: types = ["Build"]
        summary_bits = [event.summary for event in events if event.summary]
        project = next((item for item in self.catalog.projects if item.id == project_id), Project(project_id, "Project"))
        notes = list(notes or [])
        happened = self._fallback_summary(events, project_id, notes) or _safe_text("; ".join(dict.fromkeys(summary_bits)), 900) or "Recorded project activity from local evidence."
        branches = sorted({str(event.payload.get("branch") or "") for event in git if event.payload.get("branch")})
        if semantic:
            types = [item for item in semantic.get("session_types", []) if item in allowed_types] or types
            lines = [semantic.get("what_happened") or ""]
            if semantic.get("accomplishments"): lines.append("Done: " + "; ".join(semantic["accomplishments"]))
            if semantic.get("decisions"): lines.append("Decided: " + "; ".join(semantic["decisions"]))
            # The person's own words are kept verbatim, never paraphrased by the model.
            if notes: lines.append("In your words: " + " ".join(notes))
            composed = "\n".join(line for line in lines if line)
            happened = composed if semantic.get("what_happened") or semantic.get("accomplishments") else (composed + "\n" + happened if composed else happened)
        problems = semantic.get("problems") if semantic else None
        next_step = semantic.get("next_step") if semantic else None
        return {"entry": title_property((semantic or {}).get("title") or f"{project.name} — Daily Work Session"), "project": relation_property([project_id]), "date": date_property(day.date().isoformat()), "session start": date_property(start.isoformat()), "session end": date_property(end.isoformat()), "active seconds": number_property(round(active)), "idle seconds": number_property(round(idle)), "Session type": multi_select_property(types), "What Happened": rich_text_property(happened), "progress_summary": rich_text_property((semantic or {}).get("progress_summary") or _safe_text(git[-1].summary if git else happened, 500)), "Problems": rich_text_property(problems or ""), "no problems": {"checkbox": not bool(problems)}, "Next Step": rich_text_property(next_step or ""), "no next steps": {"checkbox": not bool(next_step)}, "event": rich_text_property((semantic or {}).get("event") or _safe_text(git[-1].summary if git else (ai[-1].summary if ai else "Project work"), 300)), "event_hash": rich_text_property(event_hash), "git_events_count": number_property(len(git)), "ai_events_count": number_property(len(ai)), "branches": rich_text_property(", ".join(branches)), "sessions_count": number_property(len(events)), "top_apps": rich_text_property(", ".join(sorted({event.app for event in events if event.app})[:8])), "confidence": number_property((semantic or {}).get("confidence", round(max(event.confidence for event in events), 3)))}


def _union_seconds(ranges: list[tuple[datetime, datetime]]) -> float:
    total, end = 0.0, None
    for start, stop in sorted(ranges):
        if end is None or start > end: total += (stop-start).total_seconds(); end = stop
        elif stop > end: total += (stop-end).total_seconds(); end = stop
    return total
