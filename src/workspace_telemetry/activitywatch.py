"""Defensive ActivityWatch REST client and event discovery."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import PrivacyRules
from .models import ActivityEvent, ActivityWatchSnapshot, TimeRange
from .privacy import category_for_app, safe_work_label, sanitize_window_data


class ActivityWatchError(RuntimeError):
    pass


class ActivityWatchClient:
    """Collect sanitized events without relying on ActivityWatch bucket names."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int = 10,
        request: Callable[[str, str, dict[str, Any] | None], Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._request_impl = request or self._request

    def collect(self, time_range: TimeRange, rules: PrivacyRules) -> ActivityWatchSnapshot:
        try:
            info = self._request_impl("GET", "/info", None)
            buckets_payload = self._request_impl("GET", "/buckets/", None)
        except ActivityWatchError as exc:
            return ActivityWatchSnapshot(available=False, errors=[str(exc)])

        buckets = _bucket_items(buckets_payload)
        events: list[ActivityEvent] = []
        bucket_ids: list[str] = []
        errors: list[str] = []
        has_window = False
        has_afk = False
        for bucket_id, metadata in buckets:
            if bucket_id.casefold() in rules.ignored_buckets:
                continue
            try:
                payload = self._events(bucket_id, time_range)
            except ActivityWatchError as exc:
                errors.append(f"ActivityWatch bucket {bucket_id} failed: {exc}")
                continue
            bucket_events = _event_items(payload)
            kinds = _bucket_kinds(metadata, bucket_events)
            if not kinds:
                continue
            bucket_ids.append(bucket_id)
            has_window = has_window or "window" in kinds
            has_afk = has_afk or "afk" in kinds
            for raw_event in bucket_events:
                converted = _sanitize_event(raw_event, bucket_id, kinds, time_range, rules)
                if converted:
                    events.append(converted)
        deduplicated = _deduplicate(events)
        return ActivityWatchSnapshot(
            available=True,
            server_info=info if isinstance(info, dict) else {},
            events=deduplicated,
            bucket_ids=tuple(sorted(set(bucket_ids), key=str.casefold)),
            has_window_bucket=has_window,
            has_afk_bucket=has_afk,
            errors=errors,
        )

    def _events(self, bucket_id: str, time_range: TimeRange) -> Any:
        query = urlencode({"start": time_range.start.isoformat(), "end": time_range.end.isoformat()})
        try:
            return self._request_impl("GET", f"/buckets/{bucket_id}/events?{query}", None)
        except ActivityWatchError as exc:
            # Older ActivityWatch servers can reject time filters.  A bounded
            # unfiltered read still permits compatibility when the bucket is small;
            # filtering occurs again locally before persistence.
            if "HTTP 4" not in str(exc):
                raise
            return self._request_impl("GET", f"/buckets/{bucket_id}/events", None)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ActivityWatchError(f"HTTP {exc.code}: {body[:240]}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ActivityWatchError(str(exc)) from exc


def _bucket_items(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(payload, dict):
        return [(str(key), value if isinstance(value, dict) else {}) for key, value in payload.items()]
    if isinstance(payload, list):
        return [
            (str(item.get("id") or item.get("bucket_id") or ""), item)
            for item in payload
            if isinstance(item, dict) and str(item.get("id") or item.get("bucket_id") or "")
        ]
    return []


def _event_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        raw = payload.get("events") or payload.get("data") or []
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    return []


def _bucket_kinds(metadata: dict[str, Any], events: list[dict[str, Any]]) -> set[str]:
    bucket_type = str(metadata.get("type") or metadata.get("client") or "").casefold()
    kinds: set[str] = set()
    if "afk" in bucket_type:
        kinds.add("afk")
    if "window" in bucket_type:
        kinds.add("window")
    if "web" in bucket_type or "browser" in bucket_type:
        kinds.add("browser")
    for event in events[:20]:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        status = str(data.get("status") or "").casefold()
        if status in {"afk", "not-afk", "not afk"}:
            kinds.add("afk")
        if data.get("url") or data.get("website"):
            kinds.add("browser")
        if data.get("app") or data.get("application"):
            kinds.add("window")
    return kinds


def _sanitize_event(
    raw: dict[str, Any],
    bucket_id: str,
    kinds: set[str],
    time_range: TimeRange,
    rules: PrivacyRules,
) -> ActivityEvent | None:
    start = _parse_datetime(raw.get("timestamp"))
    if start is None:
        return None
    try:
        duration = float(raw.get("duration", 0) or 0)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    end = start + timedelta(seconds=duration)
    clipped_start = max(start, time_range.start)
    clipped_end = min(end, time_range.end)
    if clipped_end <= clipped_start:
        return None
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    status = str(data.get("status") or "").casefold()
    is_afk = "afk" in kinds and status == "afk"
    if not is_afk and (clipped_end - clipped_start).total_seconds() < rules.minimum_duration_seconds:
        return None
    app, domain, keep = sanitize_window_data(data, bucket_id, rules)
    if not keep and not is_afk:
        return None
    kind = "afk" if "afk" in kinds else "browser" if "browser" in kinds and domain else "window"
    source_id = _event_id(bucket_id, raw, clipped_start, clipped_end, app, domain, status)
    return ActivityEvent(
        source_id=source_id,
        bucket_id=bucket_id,
        kind=kind,
        start=clipped_start,
        end=clipped_end,
        app=app,
        category=category_for_app(app, rules),
        domain=domain,
        work_label=safe_work_label(data, app, rules),
        afk=is_afk,
    )


def _event_id(
    bucket_id: str,
    raw: dict[str, Any],
    start: datetime,
    end: datetime,
    app: str,
    domain: str,
    status: str,
) -> str:
    supplied = str(raw.get("id") or "").strip()
    if supplied:
        return f"{bucket_id}:{supplied}"
    payload = "\0".join((bucket_id, start.isoformat(), end.isoformat(), app, domain, status))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _deduplicate(events: list[ActivityEvent]) -> list[ActivityEvent]:
    by_id: dict[str, ActivityEvent] = {}
    for event in events:
        current = by_id.get(event.source_id)
        if current is None or (event.end - event.start) > (current.end - current.start):
            by_id[event.source_id] = event
    return sorted(by_id.values(), key=lambda event: (event.start, event.end, event.source_id))


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
