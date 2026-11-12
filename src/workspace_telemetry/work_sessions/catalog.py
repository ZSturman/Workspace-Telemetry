"""Notion project cache and local association map.

Only local computer associations live in ``project-map.json``.  The Notion
Project record remains authoritative for project metadata and workflow fields.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..notion_api import (
    date_property,
    multi_select_property,
    rich_text_property,
    select_property,
    title_property,
)
from ..worklog import flatten_property

from .storage import atomic_json, read_json


WRITABLE_TYPES = {"title", "rich_text", "select", "multi_select", "checkbox", "date", "number", "url", "email", "phone_number"}


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def normalize_path(value: str) -> str:
    if not value:
        return ""
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except OSError:
        return str(Path(value).expanduser())


def normalize_domain(value: str) -> str:
    text = (value or "").strip().casefold()
    text = text.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
    return text.removeprefix("www.")


def project_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}" if page_id else ""


class ProjectCatalog:
    def __init__(self, cache_path: Path, map_path: Path) -> None:
        self.cache_path, self.map_path = cache_path, map_path

    def cache(self) -> dict[str, Any]:
        result = read_json(self.cache_path, {"version": 1, "projects": [], "schema": {}})
        return result if isinstance(result, dict) else {"version": 1, "projects": [], "schema": {}}

    def mapping(self) -> dict[str, Any]:
        result = read_json(self.map_path, {"version": 1, "projects": {}})
        if not isinstance(result, dict):
            result = {"version": 1, "projects": {}}
        result.setdefault("version", 1)
        result.setdefault("projects", {})
        return result

    def save_mapping(self, value: dict[str, Any]) -> None:
        atomic_json(self.map_path, value)

    def refresh(self, notion: Any, database_id: str) -> dict[str, Any]:
        source_id = notion.data_source_id(database_id)
        source = notion.get_data_source(source_id)
        schema = _schema(source.get("properties") or {})
        projects: list[dict[str, Any]] = []
        for page in notion.query_data_source(source_id):
            props = page.get("properties") or {}
            name = str(flatten_property(props.get(_title_name(schema)) or {})).strip()
            if not name:
                continue
            projects.append({
                "id": str(page.get("id") or ""),
                "name": name,
                "url": project_url(str(page.get("id") or "")),
                "status": _value(props, "status"),
                "domain": _value(props, "domain"),
                "requiredAction": _value(props, "required action"),
                "lastEditedAt": str(page.get("last_edited_time") or ""),
                "notion": True,
                "notionAssociations": {
                    "paths": _values(props, "path prefixes"),
                    "repos": _values(props, "repo roots"),
                    "workspaces": _values(props, "workspace roots"),
                    "domains": _values(props, "browser domains"),
                    "apps": _values(props, "app hints"),
                    "aliases": _values(props, "ai hints"),
                },
            })
        projects.sort(key=lambda item: item["name"].casefold())
        payload = {"version": 1, "updatedAt": now_iso(), "databaseId": database_id, "dataSourceId": source_id, "schema": schema, "projects": projects}
        atomic_json(self.cache_path, payload)
        self._merge_map_names(payload)
        return payload

    def _merge_map_names(self, cache: dict[str, Any]) -> None:
        mapping = self.mapping()
        entries = mapping["projects"]
        for project in cache.get("projects") or []:
            entry = entries.setdefault(project["id"], _map_entry(project))
            entry["name"] = project["name"]
            entry["notion_page_id"] = project["id"]
        self.save_mapping(mapping)

    def combined_projects(self) -> list[dict[str, Any]]:
        cache = self.cache()
        mapping = self.mapping().get("projects") or {}
        combined = {str(project.get("id")): dict(project) for project in cache.get("projects") or [] if project.get("id")}
        for project_id, entry in mapping.items():
            # A previously-created Notion project may later be archived. Do
            # not resurrect that remote page as a confusing local project.
            if project_id not in combined and entry.get("notion_page_id"):
                continue
            if project_id not in combined:
                combined[project_id] = {"id": project_id, "name": entry.get("name") or "Untitled local project", "url": entry.get("notion_url") or "", "notion": False, "status": entry.get("status") or "Local only", "domain": entry.get("domain") or "", "requiredAction": "Local only"}
        return [self._combined(combined[project_id], mapping.get(project_id) or {}) for project_id in combined]

    @staticmethod
    def _combined(project: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
        result = dict(project)
        result["local"] = local
        return result

    def local_entry(self, project_id: str) -> dict[str, Any]:
        return (self.mapping().get("projects") or {}).get(project_id) or {}

    def remember(self, project_id: str, *, kind: str, value: str, primary: bool = True) -> dict[str, Any]:
        """Learn one local association while keeping lists small and deduplicated."""

        mapping = self.mapping()
        projects = {project["id"]: project for project in self.combined_projects()}
        project = projects.get(project_id) or {"id": project_id, "name": project_id, "notion": not project_id.startswith("local:")}
        entry = mapping["projects"].setdefault(project_id, _map_entry(project))
        field = {"path": "paths", "file": "files", "repo": "repos", "domain": "domains", "app": "apps", "alias": "aliases"}.get(kind)
        if not field:
            raise ValueError(f"Unsupported association kind: {kind}")
        cleaned = normalize_domain(value) if kind == "domain" else normalize_path(value) if kind in {"path", "file", "repo"} else str(value).strip()
        if cleaned and cleaned not in entry[field]:
            entry[field].append(cleaned)
        if primary and kind in {"path", "file", "repo"}:
            entry[field] = sorted(set(entry[field]), key=lambda item: (-len(item), item.casefold()))[:100]
        entry["updated_at"] = now_iso()
        self.save_mapping(mapping)
        return entry

    def mark_selected(self, project_ids: list[str]) -> None:
        mapping = self.mapping()
        known = {project["id"]: project for project in self.combined_projects()}
        for project_id in project_ids:
            project = known.get(project_id) or {"id": project_id, "name": project_id, "notion": not project_id.startswith("local:")}
            entry = mapping["projects"].setdefault(project_id, _map_entry(project))
            entry["last_selected"] = now_iso()
            entry["selection_count"] = int(entry.get("selection_count") or 0) + 1
        self.save_mapping(mapping)

    def create_local(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        import uuid
        project_id = f"local:{uuid.uuid4()}"
        mapping = self.mapping()
        mapping["projects"][project_id] = {**_map_entry({"id": project_id, "name": name, "notion": False}), "status": str(fields.get("status") or "Local only"), "domain": str(fields.get("domain") or ""), "draft_fields": fields}
        self.save_mapping(mapping)
        return {"id": project_id, "name": name, "notion": False, "url": "", "status": str(fields.get("status") or "Local only"), "domain": str(fields.get("domain") or "")}

    def discard_local(self, project_id: str) -> bool:
        mapping = self.mapping()
        entry = (mapping.get("projects") or {}).get(project_id)
        if not entry or entry.get("notion_page_id"):
            return False
        del mapping["projects"][project_id]
        self.save_mapping(mapping)
        return True

    def add_created_notion_project(self, page: dict[str, Any], name: str, fields: dict[str, Any]) -> dict[str, Any]:
        project_id = str(page.get("id") or "")
        if not project_id:
            raise RuntimeError("Notion did not return a project page ID.")
        cache = self.cache()
        record = {"id": project_id, "name": name, "url": project_url(project_id), "notion": True, "status": str(fields.get("status") or ""), "domain": str(fields.get("domain") or ""), "requiredAction": "", "lastEditedAt": str(page.get("last_edited_time") or ""), "notionAssociations": {"paths": [], "repos": [], "workspaces": [], "domains": [], "apps": [], "aliases": []}}
        cache.setdefault("projects", []).append(record)
        cache["projects"].sort(key=lambda item: item["name"].casefold())
        atomic_json(self.cache_path, cache)
        mapping = self.mapping()
        mapping["projects"][project_id] = {**_map_entry(record), "status": record["status"], "domain": record["domain"]}
        self.save_mapping(mapping)
        return record


def _schema(properties: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, spec in properties.items():
        kind = str(spec.get("type") or "")
        setting = spec.get(kind) if isinstance(spec.get(kind), dict) else {}
        options = [str(item.get("name")) for item in setting.get("options") or [] if isinstance(item, dict) and item.get("name")]
        result[name] = {"type": kind, "options": options, "writable": kind in WRITABLE_TYPES, "formula": setting.get("expression") if kind == "formula" else None}
    return result


def _map_entry(project: dict[str, Any]) -> dict[str, Any]:
    return {"name": project.get("name") or "", "notion_page_id": project.get("id") if project.get("notion") else "", "notion_url": project.get("url") or "", "aliases": [], "paths": [], "files": [], "repos": [], "domains": [], "apps": [], "last_selected": "", "selection_count": 0, "created_at": now_iso(), "updated_at": now_iso()}


def _title_name(schema: dict[str, Any]) -> str:
    return next((name for name, spec in schema.items() if spec.get("type") == "title"), "title")


def _value(properties: dict[str, Any], name: str) -> Any:
    actual = next((key for key in properties if key.casefold() == name.casefold()), name)
    return flatten_property(properties.get(actual) or {})


def _values(properties: dict[str, Any], name: str) -> list[str]:
    value = _value(properties, name)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").replace("\n", ",").split(",") if part.strip()]


def write_project_properties(schema: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """Create a payload only for fields that exist in the live Notion schema."""

    payload: dict[str, Any] = {}
    by_normalized = {name.casefold(): (name, spec) for name, spec in schema.items()}
    for supplied_name, value in fields.items():
        target = by_normalized.get(supplied_name.casefold())
        if not target or value in (None, "", []):
            continue
        name, spec = target
        kind = spec.get("type")
        if kind == "title": payload[name] = title_property(value)
        elif kind == "rich_text": payload[name] = rich_text_property(value)
        elif kind == "select": payload[name] = select_property(value)
        elif kind == "multi_select": payload[name] = multi_select_property(value if isinstance(value, list) else [value])
        elif kind == "checkbox": payload[name] = {"checkbox": bool(value)}
        elif kind == "date": payload[name] = date_property(value)
        elif kind == "number": payload[name] = {"number": value}
        elif kind in {"url", "email", "phone_number"}: payload[name] = {kind: str(value)}
    return payload
