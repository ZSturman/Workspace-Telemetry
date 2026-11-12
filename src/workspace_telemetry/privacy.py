"""Conservative privacy filtering for telemetry sources and prompts."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .config import PrivacyRules


def normalized_domain(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text if "://" in text else f"//{text}")
    host = (parsed.hostname or "").casefold().strip(".")
    return host[4:] if host.startswith("www.") else host


def category_for_app(app: str, rules: PrivacyRules) -> str:
    normalized = app.casefold().strip()
    if not normalized:
        return "other"
    if normalized in rules.app_categories:
        return rules.app_categories[normalized]
    for pattern, category in rules.app_categories.items():
        if pattern and pattern in normalized:
            return category
    return "browser" if normalized in {"safari", "google chrome", "arc", "firefox", "browser"} else "other"


def sanitize_window_data(data: Any, bucket_id: str, rules: PrivacyRules) -> tuple[str, str, bool]:
    """Return app/domain/keep without retaining a title or full URL."""

    if bucket_id.casefold() in rules.ignored_buckets or not isinstance(data, dict):
        return "", "", False
    raw_app = str(data.get("app") or data.get("application") or "").strip()
    raw_url = str(data.get("url") or data.get("website") or "").strip()
    raw_title = str(data.get("title") or "").strip()
    searchable = "\n".join((raw_app, raw_url, raw_title)).casefold()
    if any(keyword and keyword in searchable for keyword in rules.sensitive_keywords):
        return "", "", False
    app = raw_app[:120]
    if app.casefold() in rules.ignored_apps:
        return "", "", False
    domain = normalized_domain(raw_url)
    if domain in rules.ignored_domains:
        domain = ""
    return app, domain, True


_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{1,59}$")
_EDITOR_NAMES = {"visual studio code", "code", "cursor", "xcode"}
_TERMINAL_NAMES = {"terminal", "iterm2"}
_SHELL_NAMES = {"zsh", "bash", "fish", "nu", "pwsh", "python", "node"}


def safe_work_label(data: Any, app: str, rules: PrivacyRules) -> str:
    """Extract a workspace label only from allowlisted editor/terminal formats."""

    normalized_app = app.casefold().strip()
    if normalized_app not in rules.workspace_label_apps or not isinstance(data, dict):
        return ""
    title = str(data.get("title") or "").strip()
    lowered = title.casefold()
    if not title or any(token in lowered for token in ("://", "www.", "/users/", "\\users\\", "mailto:")):
        return ""
    if any(keyword and keyword in lowered for keyword in rules.sensitive_keywords):
        return ""
    parts = [part.strip() for part in re.split(r"\s+(?:—|–|-)\s+", title) if part.strip()]
    candidate = ""
    if normalized_app in _EDITOR_NAMES and len(parts) >= 2:
        trimmed = [part for part in parts if part.casefold() not in _EDITOR_NAMES]
        candidate = trimmed[-1] if trimmed else ""
    elif normalized_app in _TERMINAL_NAMES and len(parts) >= 2:
        trimmed = [part for part in parts if part.casefold() not in _TERMINAL_NAMES]
        if trimmed and trimmed[-1].casefold() in _SHELL_NAMES:
            trimmed.pop()
        candidate = trimmed[-1] if trimmed else ""
    return _validated_label(candidate, rules)


def _validated_label(candidate: str, rules: PrivacyRules) -> str:
    value = " ".join(candidate.split()).strip(" .-")
    lowered = value.casefold()
    if (
        not _SAFE_LABEL.fullmatch(value)
        or "@" in value
        or "/" in value
        or "\\" in value
        or lowered in _SHELL_NAMES
        or lowered.endswith((".com", ".net", ".org", ".io"))
        or any(keyword and keyword in lowered for keyword in rules.sensitive_keywords)
    ):
        return ""
    return value


def infer_projects(
    *,
    applications: dict[str, float],
    domains: dict[str, float],
    categories: dict[str, float],
    rules: PrivacyRules,
) -> tuple[str, ...]:
    app_names = {value.casefold() for value in applications}
    domain_names = {value.casefold() for value in domains}
    category_names = {value.casefold() for value in categories}
    found: list[str] = []
    for project, rule in rules.project_rules.items():
        if (
            app_names.intersection(rule.get("apps", ()))
            or domain_names.intersection(rule.get("domains", ()))
            or category_names.intersection(rule.get("categories", ()))
        ):
            found.append(project)
    return tuple(sorted(found, key=str.casefold))
