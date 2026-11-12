"""Current Notion API client used exclusively by the page-authored kiosk."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


NOTION_VERSION = "2026-03-11"
MAX_RICH_TEXT_CHUNK = 2000
KIOSK_WARNING_MARKER = "[KIOSK WARNING]"
KIOSK_SECTION_STATUS_MARKER = "[KIOSK SECTION STATUS]"


@dataclass(frozen=True)
class ViewPage:
    """One bounded page from a saved Notion view query."""

    rows: list[dict[str, Any]]
    query_id: str
    next_cursor: str | None
    has_more: bool


class NotionClient:
    def __init__(self, api_key: str, *, base_url: str = "https://api.notion.com/v1", timeout_seconds: int = 30, minimum_request_interval: float = 0.4, notion_version: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.headers = {
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            # Older telemetry callers pass their historic API version. Data
            # sources require the current contract, so this client stays on
            # the single version exercised by the kiosk.
            "Notion-Version": NOTION_VERSION,
        }
        # The page compiler makes several small metadata calls before it can
        # query a saved view. Stay below Notion's average 3 req/s budget while
        # avoiding the minutes-long cold start caused by one request per
        # second on a deeply nested authored page.  The 429 backoff below is
        # still authoritative when Notion asks us to slow down.
        self.minimum_request_interval = max(0.0, minimum_request_interval)
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0
        self._data_source_cache: dict[str, dict[str, Any]] = {}
        self._progress: Callable[[str, str, int | None, int | None], None] | None = None

    def set_progress(self, callback: Callable[[str, str, int | None, int | None], None] | None) -> None:
        """Expose useful API wait states without leaking response content."""

        self._progress = callback

    def _report(self, phase: str, detail: str) -> None:
        if self._progress:
            self._progress(phase, detail, None, None)

    def get_page(self, page_id: str) -> dict[str, Any]:
        return self._request("GET", f"/pages/{page_id}")

    def get_database(self, database_id: str) -> dict[str, Any]:
        return self._request("GET", f"/databases/{database_id}")

    def get_data_source(self, data_source_id: str) -> dict[str, Any]:
        if data_source_id not in self._data_source_cache:
            self._data_source_cache[data_source_id] = self._request("GET", f"/data_sources/{data_source_id}")
        return self._data_source_cache[data_source_id]

    def data_source_id(self, database_or_source_id: str) -> str:
        """Resolve a database ID to an accessible data-source ID."""

        try:
            self.get_data_source(database_or_source_id)
            return database_or_source_id
        except Exception:
            database = self.get_database(database_or_source_id)
            sources = database.get("data_sources") or []
            source_id = str((sources[0] if sources else {}).get("id") or "")
            if not source_id:
                raise RuntimeError(f"Notion database {database_or_source_id} has no accessible data source.")
            return source_id

    def get_block_children(self, block_id: str, *, page_size: int = 100) -> list[dict[str, Any]]:
        return self._paginate("GET", f"/blocks/{block_id}/children", page_size=page_size)

    def list_views(self, database_id: str) -> list[dict[str, Any]]:
        # List responses already contain the id/name/parent metadata required
        # by the compiler.  Fetching each item again doubled page-load calls
        # on a multi-section dashboard and invited avoidable rate limits.
        return self._paginate("GET", "/views", params={"database_id": database_id})

    def get_view(self, view_id: str) -> dict[str, Any]:
        return self._request("GET", f"/views/{view_id}")

    def query_view(self, view_id: str, *, page_size: int = 100, limit: int | None = None) -> list[dict[str, Any]]:
        """Return pages in precisely the stored Notion view order.

        The Views API uses an expiring query object.  Deleting it is best effort
        so a successful dashboard render is never discarded by cleanup failure.
        """

        if limit is not None:
            page_size = max(1, min(page_size, limit))
        query = self._request("POST", f"/views/{view_id}/queries", {"page_size": page_size})
        query_id = str(query.get("id") or "")
        results = list(query.get("results") or [])[:limit] if limit is not None else list(query.get("results") or [])
        cursor = query.get("next_cursor")
        try:
            while query.get("has_more") and cursor and (limit is None or len(results) < limit):
                query = self._request("GET", f"/views/{view_id}/queries/{query_id}", params={"start_cursor": str(cursor), "page_size": page_size})
                remaining = None if limit is None else max(0, limit - len(results))
                results.extend((query.get("results") or [])[:remaining])
                cursor = query.get("next_cursor")
        finally:
            if query_id:
                try:
                    self._request("DELETE", f"/views/{view_id}/queries/{query_id}")
                except Exception:
                    pass
        pages: list[dict[str, Any]] = []
        for result in (results[:limit] if limit is not None else results):
            if isinstance(result, dict) and isinstance(result.get("properties"), dict):
                pages.append(result)
                continue
            page_id = str(result.get("id") or "") if isinstance(result, dict) else ""
            if page_id:
                pages.append(self.get_page(page_id))
        return pages

    def query_view_page(
        self,
        view_id: str,
        *,
        page_size: int,
        query_id: str | None = None,
        cursor: str | None = None,
    ) -> ViewPage:
        """Read exactly one page and keep its short-lived query open.

        The dashboard owns the query handle only while the user is paging a
        list.  A normal refresh closes it and starts again at the first page,
        which makes reloads bounded even for a saved view with thousands of
        rows.
        """

        size = max(1, min(100, int(page_size)))
        if query_id:
            params: dict[str, Any] = {"page_size": size}
            if cursor:
                params["start_cursor"] = cursor
            query = self._request("GET", f"/views/{view_id}/queries/{query_id}", params=params)
        else:
            query = self._request("POST", f"/views/{view_id}/queries", {"page_size": size})
            query_id = str(query.get("id") or "")
        rows: list[dict[str, Any]] = []
        for result in query.get("results") or []:
            if not isinstance(result, dict):
                continue
            if isinstance(result.get("properties"), dict):
                rows.append(result)
                continue
            page_id = str(result.get("id") or "")
            if page_id:
                rows.append(self.get_page(page_id))
        next_cursor = str(query.get("next_cursor") or "") or None
        return ViewPage(rows, str(query_id or ""), next_cursor, bool(query.get("has_more") and next_cursor))

    def close_view_query(self, view_id: str, query_id: str) -> None:
        """Best-effort cleanup for a paged saved-view query."""

        if not query_id:
            return
        try:
            self._request("DELETE", f"/views/{view_id}/queries/{query_id}")
        except Exception:
            pass

    def query_data_source(self, data_source_id: str, *, filter_payload: dict[str, Any] | None = None, page_size: int = 100) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"page_size": page_size}
        if filter_payload:
            payload["filter"] = filter_payload
        return self._paginate("POST", f"/data_sources/{data_source_id}/query", payload=payload, page_size=page_size)

    # Compatibility shims keep the pre-data-source telemetry summaries job
    # working while all new persistence uses data sources explicitly.
    def query_database(self, database_id: str, *, filter_payload: dict[str, Any] | None = None, sorts: list[dict[str, Any]] | None = None, page_size: int = 100) -> list[dict[str, Any]]:
        source_id = self.data_source_id(database_id)
        payload: dict[str, Any] = {"page_size": page_size}
        if filter_payload:
            payload["filter"] = filter_payload
        if sorts:
            payload["sorts"] = sorts
        return self._paginate("POST", f"/data_sources/{source_id}/query", payload=payload, page_size=page_size)

    def update_database(self, database_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/databases/{database_id}", {"properties": payload})

    def create_page(self, data_source_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": data_source_id}, "properties": properties})

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def archive_page(self, page_id: str) -> dict[str, Any]:
        # The current Notion API renamed the page archive flag to ``in_trash``.
        # Sending the retired ``archived`` property now produces a validation
        # error and breaks YAML ``delete_item`` actions such as habit undo.
        return self._request("PATCH", f"/pages/{page_id}", {"in_trash": True})

    def update_block(self, block_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/blocks/{block_id}", payload)

    def archive_block(self, block_id: str) -> dict[str, Any]:
        return self._request("PATCH", f"/blocks/{block_id}", {"in_trash": True})

    def append_block_children(self, block_id: str, children: list[dict[str, Any]], *, after: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"children": children}
        if after:
            payload["after"] = after
        return self._request("PATCH", f"/blocks/{block_id}/children", payload)

    def upsert_dashboard_warning(self, page_id: str, message: str) -> None:
        """Keep one active, machine-owned warning immediately below globals.

        Only the marker-prefixed callout is ever changed.  This lets a layout
        warning be reconciled repeatedly without touching nearby author text.
        """

        blocks = self.get_block_children(page_id)
        warning = next(
            (
                block for block in blocks
                if block.get("type") == "callout" and KIOSK_WARNING_MARKER in _block_plain_text(block)
            ),
            None,
        )
        if not message:
            if warning:
                self.archive_block(str(warning.get("id") or ""))
            return
        rich_text = text_fragments(f"{KIOSK_WARNING_MARKER} {message}")
        callout = {"rich_text": rich_text, "icon": {"type": "emoji", "emoji": "⚠️"}, "color": "red_background"}
        if warning:
            self.update_block(str(warning.get("id") or ""), {"callout": callout})
            return
        global_yaml = next(
            (
                block for block in blocks
                if block.get("type") == "code" and str((block.get("code") or {}).get("language") or "").lower() in {"yaml", "yml"}
            ),
            None,
        )
        after = str((global_yaml or {}).get("id") or "") or None
        self.append_block_children(page_id, [{"object": "block", "type": "callout", "callout": callout}], after=after)

    def reconcile_dashboard_warnings(
        self,
        page_id: str,
        message: str,
        section_statuses: dict[str, dict[str, str]],
    ) -> None:
        """Reconcile the one global warning and each affected Section status.

        ``section_statuses`` is keyed by the owning Notion callout block ID.
        Every compiled Section is included, with an empty message when healthy,
        so an old machine-owned status callout is archived as soon as the
        condition clears. These blocks are metadata for Notion authors only;
        the compiler explicitly excludes their marker text from the kiosk UI.
        """

        self.upsert_dashboard_warning(page_id, message)
        for section_block_id, status in section_statuses.items():
            self.upsert_section_status(
                section_block_id,
                str(status.get("sectionId") or "Section"),
                str(status.get("message") or ""),
            )

    def upsert_section_status(self, section_block_id: str, section_id: str, message: str) -> None:
        """Put one machine-owned status callout directly below local YAML."""

        if not section_block_id:
            return
        blocks = self.get_block_children(section_block_id)
        marker = f"{KIOSK_SECTION_STATUS_MARKER} {section_id}"
        status = next(
            (
                block
                for block in blocks
                if block.get("type") == "callout" and KIOSK_SECTION_STATUS_MARKER in _block_plain_text(block)
            ),
            None,
        )
        if not message:
            if status:
                self.archive_block(str(status.get("id") or ""))
            return
        text = (
            f"{marker}\n"
            f"Warning: {message}\n"
            "What to do: shorten displayed text, lower items, or add truncate_lines; do not hide the Section."
        )
        callout = {
            "rich_text": text_fragments(text),
            "icon": {"type": "emoji", "emoji": "⚠️"},
            "color": "orange_background",
        }
        if status:
            self.update_block(str(status.get("id") or ""), {"callout": callout})
            return
        local_yaml = next(
            (
                block
                for block in blocks
                if block.get("type") == "code" and str((block.get("code") or {}).get("language") or "").lower() in {"yaml", "yml"}
            ),
            None,
        )
        after = str((local_yaml or {}).get("id") or "") or None
        self.append_block_children(section_block_id, [{"object": "block", "type": "callout", "callout": callout}], after=after)

    def _paginate(self, method: str, path: str, payload: dict[str, Any] | None = None, *, params: dict[str, Any] | None = None, page_size: int = 100) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            current_params = dict(params or {})
            current_payload = dict(payload or {})
            if method == "GET":
                current_params.setdefault("page_size", page_size)
                if cursor:
                    current_params["start_cursor"] = cursor
                response = self._request(method, path, params=current_params)
            else:
                current_payload.setdefault("page_size", page_size)
                if cursor:
                    current_payload["start_cursor"] = cursor
                response = self._request(method, path, current_payload)
            values.extend(item for item in response.get("results") or [] if isinstance(item, dict))
            if not response.get("has_more"):
                return values
            cursor = str(response.get("next_cursor") or "") or None
            if cursor is None:
                return values

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        suffix = f"?{urlencode(params)}" if params else ""
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(f"{self.base_url}{path}{suffix}", data=body, headers=self.headers, method=method)
        for attempt in range(5):
            try:
                self._report("Contacting Notion", f"{method} {path} (attempt {attempt + 1} of 5)")
                self._throttle()
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt < 4:
                    try:
                        retry_after = float(exc.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0
                    delay = max(retry_after, 1.0 + attempt)
                    self._report("Notion rate limit", f"Notion asked the kiosk to wait {delay:.0f}s before retrying {path}")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Notion API {method} {path} failed with {exc.code}: {detail}") from exc
        raise RuntimeError(f"Notion API {method} {path} failed after retries.")

    def _throttle(self) -> None:
        with self._request_lock:
            now = time.monotonic()
            wait = self._next_request_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_request_at = now + self.minimum_request_interval


def text_fragments(value: Any) -> list[dict[str, dict[str, str]]]:
    text = "" if value is None else str(value)
    return [{"text": {"content": text[index:index + MAX_RICH_TEXT_CHUNK]}} for index in range(0, len(text), MAX_RICH_TEXT_CHUNK)] if text else []


def title_property(value: Any) -> dict[str, Any]: return {"title": text_fragments(value)}
def rich_text_property(value: Any) -> dict[str, Any]: return {"rich_text": text_fragments(value)}
def number_property(value: Any) -> dict[str, Any]: return {"number": None if value is None else value}
def date_property(value: Any) -> dict[str, Any]: return {"date": None if value in (None, "") else {"start": str(value)}}
def select_property(value: Any) -> dict[str, Any]: return {"select": {"name": str(value)} if value not in (None, "") else None}
def multi_select_property(values: list[Any]) -> dict[str, Any]: return {"multi_select": [{"name": str(value)} for value in values if value not in (None, "")]}
def relation_property(values: list[Any]) -> dict[str, Any]: return {"relation": [{"id": str(value)} for value in values if value]}


def _block_plain_text(block: dict[str, Any]) -> str:
    kind = str(block.get("type") or "")
    values = (block.get(kind) or {}).get("rich_text") or []
    return "".join(str(value.get("plain_text") or value.get("text", {}).get("content") or "") for value in values if isinstance(value, dict))


def property_value(value: Any, property_type: str | None) -> dict[str, Any]:
    """Convert simple YAML operation values into Notion property payloads."""

    kind = (property_type or "").lower()
    if kind == "title": return {"title": text_fragments(value)}
    if kind in {"rich_text", "text"}: return {"rich_text": text_fragments(value)}
    if kind in {"select", "status"}: return {kind: {"name": str(value)} if value not in (None, "") else None}
    if kind == "multi_select": return {"multi_select": [{"name": str(item)} for item in (value if isinstance(value, list) else [value]) if item not in (None, "")]}
    if kind == "checkbox": return {"checkbox": bool(value)}
    if kind == "number": return {"number": None if value in (None, "") else value}
    if kind == "date": return {"date": None if value in (None, "") else {"start": str(value)}}
    if kind == "url": return {"url": None if value in (None, "") else str(value)}
    if kind == "relation":
        values = value if isinstance(value, list) else [value]
        return {"relation": [{"id": str(item.get("id") if isinstance(item, dict) else item)} for item in values if item]}
    raise ValueError(f"Unsupported writable Notion property type {property_type!r}.")


def flatten_property(property_: dict[str, Any]) -> Any:
    kind = property_.get("type")
    if kind in {"title", "rich_text"}:
        return "".join(str(item.get("plain_text") or item.get("text", {}).get("content") or "") for item in property_.get(kind) or [])
    if kind in {"select", "status"}:
        value = property_.get(kind) or {}; return value.get("name") or ""
    if kind == "multi_select": return [item.get("name") or "" for item in property_.get(kind) or []]
    if kind in {"checkbox", "number", "url", "email", "phone_number"}: return property_.get(kind)
    if kind == "date": return (property_.get("date") or {}).get("start") or ""
    if kind == "relation": return [item.get("id") for item in property_.get("relation") or [] if item.get("id")]
    if kind == "formula": return flatten_property(property_.get("formula") or {"type": None})
    return ""


def plain_text(page: dict[str, Any], property_name: str) -> str:
    """Legacy-friendly scalar extraction for a page property."""

    value = flatten_property((page.get("properties") or {}).get(property_name) or {})
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item)
    return "" if value is None else str(value)


def number_value(page: dict[str, Any], property_name: str) -> float:
    value = flatten_property((page.get("properties") or {}).get(property_name) or {})
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
