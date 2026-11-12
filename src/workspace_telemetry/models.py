"""Privacy-safe, independent productivity telemetry records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError("Telemetry ranges must be timezone-aware and increasing.")

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class ActivityEvent:
    source_id: str
    bucket_id: str
    kind: str
    start: datetime
    end: datetime
    app: str = ""
    category: str = "other"
    domain: str = ""
    work_label: str = ""
    afk: bool = False


@dataclass(frozen=True)
class ContextSwitch:
    external_id: str
    source_hash: str
    switched_at: datetime
    from_app: str
    to_app: str
    from_category: str
    to_category: str
    seconds_in_previous_app: float
    friction_weight: float


@dataclass
class ActivityWatchSnapshot:
    available: bool
    server_info: dict[str, Any] = field(default_factory=dict)
    events: list[ActivityEvent] = field(default_factory=list)
    bucket_ids: tuple[str, ...] = ()
    has_window_bucket: bool = False
    has_afk_bucket: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class TelemetryMetrics:
    active_seconds: float = 0.0
    idle_seconds: float = 0.0
    on_task_seconds: float = 0.0
    neutral_seconds: float = 0.0
    distraction_seconds: float = 0.0
    excluded_seconds: float = 0.0
    context_switches: int = 0
    friction_weight: float = 0.0
    applications: dict[str, float] = field(default_factory=dict)
    categories: dict[str, float] = field(default_factory=dict)
    domains: dict[str, float] = field(default_factory=dict)
    projects: dict[str, float] = field(default_factory=dict)
    top_transitions: tuple[tuple[str, str, int], ...] = ()
    notable_blocks: tuple[dict[str, Any], ...] = ()
    session_count: int = 0
    switches_per_active_hour: float = 0.0
    median_dwell_seconds: float = 0.0
    short_dwell_visits: int = 0
    short_dwell_ratio: float = 0.0
    bounce_backs: int = 0
    bounce_ratio: float = 0.0
    productive_share: float = 0.0
    distraction_share: float = 0.0
    stability_score: float = 0.0
    on_task_score: float | None = None
    score_label: str = "Insufficient activity/data"
    score_reason: str = ""
    activitywatch_buckets: tuple[str, ...] = ()
    context_switch_ids: tuple[str, ...] = ()
    source_event_count: int = 0
    canonical_source: str = "none"
    activitywatch_available: bool = False
    afk_known: bool = False
    context_switches_available: bool = False
    source_errors: tuple[str, ...] = ()

    @property
    def active_minutes(self) -> float: return round(self.active_seconds / 60, 2)
    @property
    def idle_minutes(self) -> float: return round(self.idle_seconds / 60, 2)
    @property
    def on_task_minutes(self) -> float: return round(self.on_task_seconds / 60, 2)
    @property
    def neutral_minutes(self) -> float: return round(self.neutral_seconds / 60, 2)
    @property
    def distraction_minutes(self) -> float: return round(self.distraction_seconds / 60, 2)
    @property
    def excluded_minutes(self) -> float: return round(self.excluded_seconds / 60, 2)
    @property
    def productivity_score(self) -> float | None: return self.on_task_score
    @property
    def friction_score(self) -> float:
        if self.friction_weight <= 0: return 0.0
        return round(100 * self.friction_weight / (self.friction_weight + max(1.0, self.active_seconds / 60)), 2)

    def top_name(self, values: dict[str, float]) -> str:
        return max(values.items(), key=lambda item: (item[1], item[0].casefold()))[0] if values else ""
    @property
    def dominant_app(self) -> str: return self.top_name(self.applications)
    @property
    def dominant_category(self) -> str: return self.top_name(self.categories)

    def as_dict(self) -> dict[str, Any]:
        return {
            "activeMinutes": self.active_minutes, "idleMinutes": self.idle_minutes,
            "onTaskMinutes": self.on_task_minutes, "neutralMinutes": self.neutral_minutes,
            "distractionMinutes": self.distraction_minutes, "excludedMinutes": self.excluded_minutes,
            "productivityScore": self.productivity_score, "frictionScore": self.friction_score,
            "contextSwitches": self.context_switches, "frictionWeight": round(self.friction_weight, 3),
            "applications": _rounded_mapping(self.applications), "categories": _rounded_mapping(self.categories),
            "domains": _rounded_mapping(self.domains), "projects": _rounded_mapping(self.projects),
            "switchesPerActiveHour": round(self.switches_per_active_hour, 2),
            "medianDwellSeconds": round(self.median_dwell_seconds, 1), "shortDwellVisits": self.short_dwell_visits,
            "shortDwellRatio": round(self.short_dwell_ratio, 3), "bounceBacks": self.bounce_backs,
            "bounceRatio": round(self.bounce_ratio, 3),
            "topTransitions": [{"from": source, "to": target, "count": count} for source, target, count in self.top_transitions],
            "notableBlocks": list(self.notable_blocks), "sessionCount": self.session_count,
            "productiveShare": round(self.productive_share, 1), "distractionShare": round(self.distraction_share, 1),
            "stabilityScore": round(self.stability_score, 1), "scoreLabel": self.score_label, "scoreReason": self.score_reason,
            "activityWatchBuckets": list(self.activitywatch_buckets), "contextSwitchIds": list(self.context_switch_ids),
            "sourceEventCount": self.source_event_count, "canonicalSource": self.canonical_source,
            "activityWatchAvailable": self.activitywatch_available, "afkKnown": self.afk_known,
            "sourceErrors": list(self.source_errors),
        }

    def as_prompt_dict(self) -> dict[str, Any]:
        active = max(self.active_seconds, 1.0)
        ranked = [
            {"kind": kind, "name": name[:120], "minutes": round(seconds / 60, 1), "activeShare": round(seconds / active * 100, 1)}
            for kind, values in (("app", self.applications), ("domain", self.domains), ("project", self.projects))
            for name, seconds in values.items() if name
        ]
        ranked.sort(key=lambda item: (-item["minutes"], item["kind"], item["name"].casefold()))
        return {
            "coverage": {
                "activityWatch": self.activitywatch_available,
                "foregroundWindows": self.canonical_source in {"activitywatch-window", "activitywatch-browser"},
                "afk": self.afk_known, "contextSwitches": self.context_switches_available,
                "limitations": _safe_limitations(self),
            },
            "sessionTotals": {"activeMinutes": self.active_minutes, "onTaskMinutes": self.on_task_minutes, "neutralMinutes": self.neutral_minutes, "distractionMinutes": self.distraction_minutes, "idleMinutes": self.idle_minutes, "excludedMinutes": self.excluded_minutes, "sessions": self.session_count},
            "topActivity": ranked[:8],
            "scores": {"productivity": self.productivity_score, "label": self.score_label, "productiveShare": round(self.productive_share, 1), "distractionShare": round(self.distraction_share, 1), "stability": round(self.stability_score, 1)},
            "switching": {"count": self.context_switches, "perActiveHour": round(self.switches_per_active_hour, 1), "medianDwellSeconds": round(self.median_dwell_seconds, 1), "under30SecondVisits": self.short_dwell_visits, "bounceBacks": self.bounce_backs, "topTransitions": [{"from": left, "to": right, "count": count} for left, right, count in self.top_transitions[:5]]},
            "notableWorkBlocks": list(self.notable_blocks[:5]),
        }


@dataclass(frozen=True)
class Strategy:
    id: str
    cadence: str
    source_mode: str
    context_size: str
    version: int = 1


@dataclass(frozen=True)
class SummaryContent:
    summary: str
    primary_activity: str
    projects: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    people_organizations: tuple[str, ...] = ()
    work_completed: str = ""
    work_in_progress: str = ""
    next_likely_action: str = ""
    observations: tuple[str, ...] = ()
    confidence: float = 0.0
    headline: str = ""
    recap: str = ""
    on_task_assessment: str = ""
    switching_assessment: str = ""
    distraction_assessment: str = ""
    progress_assessment: str = ""


@dataclass(frozen=True)
class GenerationResult:
    content: SummaryContent
    used_lm_studio: bool
    endpoint: str = ""
    model: str = ""
    error: str = ""


@dataclass(frozen=True)
class TelemetryRecord:
    key: str
    time_range: TimeRange
    strategy: Strategy
    source_hash: str
    metrics: TelemetryMetrics
    generation: GenerationResult
    status: str
    raw_data: dict[str, Any]
    previous_page_id: str = ""


def _rounded_mapping(values: dict[str, float]) -> dict[str, float]:
    return {key: round(value / 60, 2) for key, value in sorted(values.items(), key=lambda item: (-item[1], item[0].casefold()))}


def _safe_limitations(metrics: TelemetryMetrics) -> list[str]:
    result: list[str] = []
    if not metrics.activitywatch_available: result.append("ActivityWatch was unavailable.")
    if not metrics.afk_known: result.append("AFK coverage was unavailable.")
    if not metrics.domains: result.append("Browser-domain coverage was unavailable; browser titles were not used.")
    if metrics.productivity_score is None: result.append(metrics.score_reason or "There was not enough complete activity for a reliable score.")
    return result[:4]
