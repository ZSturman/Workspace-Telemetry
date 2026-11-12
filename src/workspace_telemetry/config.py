"""Configuration for the standalone Workspace Telemetry service."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTEXT_SWITCHES_DATABASE_ID = "39daec944d3c805c9194f50de42b4902"
DEFAULT_SUMMARIES_DATABASE_ID = "39daec944d3c8090bd45eb8e00c7f5d0"
DEFAULT_PROJECTS_DATABASE_ID = "304aec944d3c80a882f6fd7ff4d2b607"
DEFAULT_WORK_LOG_DATABASE_ID = "304aec944d3c803f9e54ffda217f9426"
DEFAULT_ACTIVITY_SESSIONS_DATABASE_ID = "365aec944d3c800fa8c5f9bace335936"
DEFAULT_AI_CHAT_EVENTS_DATABASE_ID = "365aec944d3c80baaeaff14f2fbd6453"
DEFAULT_GIT_EVENTS_DATABASE_ID = "365aec944d3c8021bf64d37b2e862676"
DEFAULT_DATA_DIRECTORY = Path.home() / "Library/Application Support/WorkspaceTelemetry"
DEFAULT_STATE_PATH = DEFAULT_DATA_DIRECTORY / "telemetry.sqlite"
DEFAULT_CONFIG_PATH = DEFAULT_DATA_DIRECTORY / "config.json"


@dataclass(frozen=True)
class PrivacyRules:
    ignored_apps: frozenset[str] = frozenset({"loginwindow", "screensaverengine", "screen saver", "windowmanager"})
    ignored_domains: frozenset[str] = frozenset()
    ignored_buckets: frozenset[str] = frozenset()
    sensitive_keywords: tuple[str, ...] = ()
    minimum_duration_seconds: float = 5.0
    app_categories: dict[str, str] = field(default_factory=lambda: {
        "visual studio code": "coding",
        "code": "coding",
        "cursor": "coding",
        "terminal": "coding",
        "iterm2": "coding",
        "xcode": "coding",
        "chatgpt": "work",
        "numbers": "work",
        "calendar": "work",
        "notion": "notes",
        "notes": "notes",
        "slack": "communication",
        "messages": "communication",
        "mail": "communication",
        "discord": "communication",
        "spotify": "media",
        "music": "media",
        "youtube": "distraction",
    })
    productive_categories: frozenset[str] = frozenset({"coding", "work", "notes"})
    distracting_categories: frozenset[str] = frozenset({"media", "distraction"})
    workspace_label_apps: frozenset[str] = frozenset({
        "visual studio code", "code", "cursor", "xcode", "terminal", "iterm2"
    })
    project_rules: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)


@dataclass(frozen=True)
class TelemetrySettings:
    notion_api_key: str
    context_switches_database_id: str = DEFAULT_CONTEXT_SWITCHES_DATABASE_ID
    summaries_database_id: str = DEFAULT_SUMMARIES_DATABASE_ID
    projects_database_id: str = DEFAULT_PROJECTS_DATABASE_ID
    work_log_database_id: str = DEFAULT_WORK_LOG_DATABASE_ID
    activity_sessions_database_id: str = DEFAULT_ACTIVITY_SESSIONS_DATABASE_ID
    ai_chat_events_database_id: str = DEFAULT_AI_CHAT_EVENTS_DATABASE_ID
    git_events_database_id: str = DEFAULT_GIT_EVENTS_DATABASE_ID
    notion_base_url: str = "https://api.notion.com/v1"
    notion_version: str = "2022-06-28"
    timezone: str = "America/Los_Angeles"
    activitywatch_base_url: str = "http://localhost:5600/api/0"
    lm_studio_endpoint: str = "http://localhost:1234/v1"
    lm_studio_model: str = "google/gemma-4-12b-qat"
    lm_studio_timeout_seconds: int = 70
    lm_studio_retries: int = 0
    lm_studio_response_format: str = "json_object"
    state_path: Path = DEFAULT_STATE_PATH
    config_path: Path = DEFAULT_CONFIG_PATH
    privacy: PrivacyRules = field(default_factory=PrivacyRules)
    enabled_strategies: tuple[str, ...] = ()
    scheduled_interval_strategy: str = "interval-production-v2"
    late_arrival_minutes: int = 15
    backfill_hours: int = 24
    worklog: dict[str, Any] = field(default_factory=dict)

    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_settings(*, dotenv_path: Path | None = None) -> TelemetrySettings:
    _load_dotenv(dotenv_path or PROJECT_ROOT / ".env")
    api_key = os.environ.get("TELEMETRY_NOTION_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TELEMETRY_NOTION_API_KEY is missing. Add it to .env or the environment.")

    config_path = Path(os.environ.get("TELEMETRY_CONFIG_PATH", str(DEFAULT_CONFIG_PATH))).expanduser()
    local_config = _load_local_config(config_path)
    privacy = _privacy_from_config(local_config.get("privacy", {}))
    timezone = os.environ.get("TELEMETRY_TIMEZONE", "America/Los_Angeles").strip() or "America/Los_Angeles"
    ZoneInfo(timezone)
    return TelemetrySettings(
        notion_api_key=api_key,
        context_switches_database_id=os.environ.get(
            "TELEMETRY_CONTEXT_SWITCHES_DATABASE_ID", DEFAULT_CONTEXT_SWITCHES_DATABASE_ID
        ).strip() or DEFAULT_CONTEXT_SWITCHES_DATABASE_ID,
        summaries_database_id=os.environ.get(
            "TELEMETRY_SUMMARIES_DATABASE_ID", DEFAULT_SUMMARIES_DATABASE_ID
        ).strip() or DEFAULT_SUMMARIES_DATABASE_ID,
        projects_database_id=os.environ.get("TELEMETRY_PROJECTS_DATABASE_ID", DEFAULT_PROJECTS_DATABASE_ID).strip() or DEFAULT_PROJECTS_DATABASE_ID,
        work_log_database_id=os.environ.get("TELEMETRY_WORK_LOG_DATABASE_ID", DEFAULT_WORK_LOG_DATABASE_ID).strip() or DEFAULT_WORK_LOG_DATABASE_ID,
        activity_sessions_database_id=os.environ.get("TELEMETRY_ACTIVITY_SESSIONS_DATABASE_ID", DEFAULT_ACTIVITY_SESSIONS_DATABASE_ID).strip() or DEFAULT_ACTIVITY_SESSIONS_DATABASE_ID,
        ai_chat_events_database_id=os.environ.get("TELEMETRY_AI_CHAT_EVENTS_DATABASE_ID", DEFAULT_AI_CHAT_EVENTS_DATABASE_ID).strip() or DEFAULT_AI_CHAT_EVENTS_DATABASE_ID,
        git_events_database_id=os.environ.get("TELEMETRY_GIT_EVENTS_DATABASE_ID", DEFAULT_GIT_EVENTS_DATABASE_ID).strip() or DEFAULT_GIT_EVENTS_DATABASE_ID,
        timezone=timezone,
        activitywatch_base_url=os.environ.get(
            "TELEMETRY_ACTIVITYWATCH_BASE_URL", "http://localhost:5600/api/0"
        ).rstrip("/"),
        lm_studio_endpoint=os.environ.get(
            "TELEMETRY_LM_STUDIO_ENDPOINT", "http://localhost:1234/v1"
        ).rstrip("/"),
        lm_studio_model=os.environ.get("TELEMETRY_LM_STUDIO_MODEL", "google/gemma-4-12b-qat").strip() or "google/gemma-4-12b-qat",
        lm_studio_timeout_seconds=_positive_int("TELEMETRY_LM_STUDIO_TIMEOUT_SECONDS", 70),
        lm_studio_retries=_nonnegative_int("TELEMETRY_LM_STUDIO_RETRIES", 0),
        lm_studio_response_format=(
            os.environ.get("TELEMETRY_LM_STUDIO_RESPONSE_FORMAT", "json_object").strip()
            or "json_object"
        ),
        state_path=Path(os.environ.get("TELEMETRY_STATE_PATH", str(DEFAULT_STATE_PATH))).expanduser(),
        config_path=config_path,
        privacy=privacy,
        enabled_strategies=tuple(sorted(_strings(local_config.get("enabledStrategies")))),
        scheduled_interval_strategy=(
            os.environ.get("TELEMETRY_SCHEDULED_INTERVAL_STRATEGY", "interval-production-v2").strip()
            or "interval-production-v2"
        ),
        late_arrival_minutes=_positive_int("TELEMETRY_LATE_ARRIVAL_MINUTES", 15),
        backfill_hours=_positive_int("TELEMETRY_BACKFILL_HOURS", 24),
        worklog=_worklog_config(local_config.get("worklog")),
    )


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), _strip_quotes(value.strip()))


def _load_local_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Telemetry config {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Telemetry config {path} must be a JSON object.")
    return parsed


def _privacy_from_config(value: Any) -> PrivacyRules:
    if not isinstance(value, dict):
        raise RuntimeError("Telemetry privacy configuration must be a JSON object.")
    defaults = PrivacyRules()
    categories = dict(defaults.app_categories)
    raw_categories = value.get("appCategories", {})
    if isinstance(raw_categories, dict):
        for app, category in raw_categories.items():
            app_text = str(app).strip().casefold()
            category_text = str(category).strip().casefold()
            if app_text and category_text:
                categories[app_text] = category_text
    return PrivacyRules(
        ignored_apps=_strings(value.get("ignoredApps")) or defaults.ignored_apps,
        ignored_domains=_strings(value.get("ignoredDomains")),
        ignored_buckets=_strings(value.get("ignoredBuckets")),
        sensitive_keywords=tuple(sorted(_strings(value.get("sensitiveKeywords")), key=str.casefold)),
        minimum_duration_seconds=_number(value.get("minimumDurationSeconds"), defaults.minimum_duration_seconds, minimum=0),
        app_categories=categories,
        productive_categories=_strings(value.get("productiveCategories")) or defaults.productive_categories,
        distracting_categories=_strings(value.get("distractingCategories")) or defaults.distracting_categories,
        workspace_label_apps=_strings(value.get("workspaceLabelApps")) or defaults.workspace_label_apps,
        project_rules=_project_rules(value.get("projectRules")),
    )


def _strings(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(str(item).strip().casefold() for item in value if str(item).strip())


def _project_rules(value: Any) -> dict[str, dict[str, tuple[str, ...]]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for name, raw_rule in value.items():
        if not isinstance(raw_rule, dict) or not str(name).strip():
            continue
        result[str(name).strip()] = {
            key: tuple(sorted(_strings(raw_rule.get(key))))
            for key in ("apps", "domains", "categories")
        }
    return result


def _worklog_config(value: Any) -> dict[str, Any]:
    """Retain only structured local settings; secrets stay in the environment."""

    if not isinstance(value, dict):
        return {}
    return value


def _number(value: Any, default: float, *, minimum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number >= minimum else default


def _positive_int(name: str, default: int) -> int:
    return max(1, _nonnegative_int(name, default))


def _nonnegative_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except ValueError:
        return default
    return max(0, value)


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
