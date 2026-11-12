"""Central configuration and paths for the work-session service.

This intentionally reuses the Workspace Telemetry Notion credentials and database
IDs.  Hammerspoon owns interaction and event capture; this package owns local
state, matching, Notion cache/sync, and durable queued writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import TelemetrySettings, load_settings


DEFAULT_ACTIVITY_TYPES = (
    "Planning", "Researching", "Studying / Learning", "Experimenting",
    "Building / Implementing", "Debugging", "Testing", "Refactoring",
    "Designing", "Creative Work", "Writing", "Reviewing", "Organizing",
    "Administrative", "Maintenance", "Boring but Necessary",
    "Exploring / No Specific Goal",
)

# Work modes are intentionally broad enough for analysis; activity methods add
# useful detail without forcing every session into a giant taxonomy.
DEFAULT_WORK_MODES = (
    "Creative", "Administrative", "Planning", "Research", "Analysis",
    "Engineering", "Writing", "Communication", "Organization", "Learning",
    "Maintenance", "Testing", "Design",
)
DEFAULT_ACTIVITY_METHODS = (
    "3D Modeling", "Visual Design", "Whiteboarding", "Project Architecture",
    "Spreadsheet Analysis", "Data Exploration", "Implementation", "Debugging",
    "Documentation", "Comparison / Evaluation",
)


@dataclass(frozen=True)
class WorkSessionSettings:
    telemetry: TelemetrySettings
    state_directory: Path
    project_map_path: Path
    project_cache_path: Path
    session_path: Path
    pending_path: Path
    telemetry_path: Path
    reminders_path: Path
    terminal_journal_path: Path
    cache_refresh_hours: int = 6
    high_confidence: float = 0.82
    switch_confidence: float = 0.88
    switch_minimum_seconds: int = 90
    switch_minimum_observations: int = 3
    idle_checkpoint_minutes: int = 20
    activity_types: tuple[str, ...] = DEFAULT_ACTIVITY_TYPES
    work_modes: tuple[str, ...] = DEFAULT_WORK_MODES
    activity_methods: tuple[str, ...] = DEFAULT_ACTIVITY_METHODS


def load_work_session_settings(overrides: dict[str, Any] | None = None) -> WorkSessionSettings:
    """Load durable local configuration without putting credentials in Lua."""

    telemetry = load_settings()
    overrides = overrides or {}
    base = Path(str(overrides.get("stateDirectory") or Path.home() / "Library/Application Support/WorkspaceTelemetry/work-sessions")).expanduser()
    paths = {
        "project_map_path": "project-map.json",
        "project_cache_path": "projects-cache.json",
        "session_path": "current-session.json",
        "pending_path": "pending-operations.json",
        "telemetry_path": "session-telemetry.jsonl",
    }
    def configured_path(key: str) -> Path:
        return Path(str(overrides.get(_camel(key)) or base / paths[key])).expanduser()

    activities = overrides.get("activityTypes")
    work_modes = overrides.get("workModes")
    activity_methods = overrides.get("activityMethods")
    return WorkSessionSettings(
        telemetry=telemetry,
        state_directory=base,
        project_map_path=configured_path("project_map_path"),
        project_cache_path=configured_path("project_cache_path"),
        session_path=configured_path("session_path"),
        pending_path=configured_path("pending_path"),
        telemetry_path=configured_path("telemetry_path"),
        reminders_path=Path(str(overrides.get("remindersPath") or Path.home() / "Workspace/To Do/reminders.json")).expanduser(),
        terminal_journal_path=Path(str(overrides.get("terminalJournalPath") or Path.home() / "Library/Application Support/WorkspaceTelemetry/inbox/terminal-events.jsonl")).expanduser(),
        cache_refresh_hours=max(1, int(overrides.get("cacheRefreshHours") or 6)),
        high_confidence=float(overrides.get("highConfidence") or .82),
        switch_confidence=float(overrides.get("switchConfidence") or .88),
        switch_minimum_seconds=max(15, int(overrides.get("switchMinimumSeconds") or 90)),
        switch_minimum_observations=max(1, int(overrides.get("switchMinimumObservations") or 3)),
        idle_checkpoint_minutes=max(5, int(overrides.get("idleCheckpointMinutes") or 20)),
        activity_types=tuple(str(value) for value in activities) if isinstance(activities, list) and activities else DEFAULT_ACTIVITY_TYPES,
        work_modes=tuple(str(value) for value in work_modes) if isinstance(work_modes, list) and work_modes else DEFAULT_WORK_MODES,
        activity_methods=tuple(str(value) for value in activity_methods) if isinstance(activity_methods, list) and activity_methods else DEFAULT_ACTIVITY_METHODS,
    )


def _camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.capitalize() for part in rest)
