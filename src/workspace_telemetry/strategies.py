"""Versioned prompt strategies for telemetry experiments."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import Strategy, SummaryContent, TelemetryMetrics, TimeRange
from .privacy import infer_projects


STRATEGIES: tuple[Strategy, ...] = (
    Strategy("interval-production-v2", "interval", "hybrid", "production", version=2),
    Strategy("rolling-24h-human-v2", "rolling", "hybrid", "production", version=2),
    Strategy("daily-production-v2", "day", "hybrid", "production", version=2),
    Strategy("interval-hybrid-detailed-v1", "interval", "hybrid", "detailed"),
    Strategy("interval-hybrid-compressed-v1", "interval", "hybrid", "compressed"),
    Strategy("interval-activitywatch-only-v1", "interval", "activitywatch", "compressed"),
    Strategy("interval-context-switches-only-v1", "interval", "context", "compressed"),
    Strategy("hourly-raw-hybrid-v1", "hour", "raw", "detailed"),
    Strategy("hourly-from-intervals-v1", "hour", "rollup", "compressed"),
    Strategy("hourly-combined-v1", "hour", "combined", "compressed"),
    Strategy("daily-raw-hybrid-v1", "day", "raw", "detailed"),
    Strategy("daily-from-hours-v1", "day", "rollup", "compressed"),
    Strategy("daily-combined-v1", "day", "combined", "compressed"),
    Strategy("weekly-raw-hybrid-v1", "week", "raw", "detailed"),
    Strategy("weekly-from-days-v1", "week", "rollup", "compressed"),
    Strategy("weekly-combined-v1", "week", "combined", "compressed"),
)


SYSTEM_PROMPT = """Explain privacy-filtered personal work telemetry in plain, natural second-person language.
Return only one compact JSON object with exactly these fields: headline, recap, onTaskAssessment,
switchingAssessment, distractionAssessment, progressAssessment, and numeric confidence.
Use only supplied aggregates. Never invent an app, site, project, task, person, or completed result.
Describe progress only as observed activity or a cautious inference about work in progress; telemetry alone
does not prove completion. Do not treat Sleep, Driving, excluded time, off-hours, or missing coverage as
distraction. State important coverage limitations plainly. Keep headline under 90 characters, recap to at
most two sentences, each assessment to one concise sentence, and confidence between 0 and 1.
When a productivity score exists, onTaskAssessment must include its exact number and supplied label verbatim;
do not replace the label with a more favorable or harsher adjective. When discussing switching, prefer the
supplied switches-per-active-hour and dwell facts. Do not infer research from browser use, content generation
from ChatGPT use, or task progress from an app name. progressAssessment may describe only observed app or
sanitized project concentration and must call any work interpretation an inference."""

PROMPT_CONTRACT_VERSION = 4
PROMPT_FINGERPRINT = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()


def strategies_for(cadence: str, requested: list[str] | None = None) -> list[Strategy]:
    candidates = [strategy for strategy in STRATEGIES if strategy.cadence == cadence]
    if not requested:
        return candidates
    requested_ids = {value.strip() for value in requested if value.strip()}
    unknown = requested_ids - {strategy.id for strategy in STRATEGIES}
    if unknown:
        raise ValueError(f"Unknown telemetry strategy: {', '.join(sorted(unknown))}")
    return [strategy for strategy in candidates if strategy.id in requested_ids]


def child_cadence(strategy: Strategy) -> str:
    return {"hour": "interval", "day": "hour", "week": "day"}.get(strategy.cadence, "")


def build_prompt_input(
    *,
    time_range: TimeRange,
    strategy: Strategy,
    metrics: TelemetryMetrics,
    rollups: list[dict[str, Any]],
    projects: tuple[str, ...],
) -> dict[str, Any]:
    if strategy.context_size == "production":
        return {
            "reportingWindow": {
                "start": time_range.start.isoformat(),
                "end": time_range.end.isoformat(),
                "minutes": round(time_range.seconds / 60, 1),
            },
            **metrics.as_prompt_dict(),
            "priorPeriodDeltas": _safe_prior_deltas(rollups),
        }
    metrics_data = metrics.as_dict()
    if strategy.source_mode == "activitywatch":
        metrics_data["contextSwitches"] = 0
        metrics_data["frictionWeight"] = 0
        metrics_data["frictionScore"] = 0
        metrics_data["contextSwitchIds"] = []
    elif strategy.source_mode == "context":
        for key in ("activeMinutes", "idleMinutes", "onTaskMinutes", "productivityScore", "applications", "domains", "activityWatchBuckets"):
            metrics_data[key] = {} if key in {"applications", "domains"} else [] if key == "activityWatchBuckets" else 0
    if strategy.context_size == "compressed":
        for key in ("applications", "categories", "domains"):
            if isinstance(metrics_data.get(key), dict):
                metrics_data[key] = dict(list(metrics_data[key].items())[:5])
        metrics_data["contextSwitchIds"] = metrics_data.get("contextSwitchIds", [])[:10]
    payload: dict[str, Any] = {
        "strategy": {"id": strategy.id, "version": strategy.version, "sourceMode": strategy.source_mode, "contextSize": strategy.context_size},
        "period": {"start": time_range.start.isoformat(), "end": time_range.end.isoformat()},
        "metrics": metrics_data,
        "configuredProjects": list(projects),
    }
    if strategy.source_mode in {"rollup", "combined"}:
        payload["priorSummaries"] = rollups[:12]
    if strategy.source_mode == "rollup":
        payload.pop("metrics")
    return payload


def _safe_prior_deltas(rollups: list[dict[str, Any]]) -> dict[str, float]:
    """Accept only numeric, pre-computed deltas; never forward prior prose."""

    if not rollups:
        return {}
    candidate = rollups[0].get("deltas") if isinstance(rollups[0], dict) else None
    if not isinstance(candidate, dict):
        return {}
    return {
        str(key)[:40]: round(float(value), 1)
        for key, value in candidate.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def source_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def deterministic_summary(
    *,
    metrics: TelemetryMetrics,
    strategy: Strategy,
    projects: tuple[str, ...],
) -> SummaryContent:
    app = metrics.dominant_app or "recorded apps"
    score = (
        f"{metrics.on_task_score:.0f}/100 ({metrics.score_label})"
        if metrics.on_task_score is not None else "not rated because the data was insufficient"
    )
    headline = (
        f"{metrics.score_label}: {metrics.active_minutes:g} active minutes"
        if metrics.on_task_score is not None else
        f"{metrics.active_minutes:g} active minutes; more data is needed for a rating"
    )
    recap = (
        f"You were active for {metrics.active_minutes:g} minutes, led by {app}. "
        f"{metrics.on_task_minutes:g} minutes were classified on task and {metrics.distraction_minutes:g} minutes as distracting."
    )
    on_task_assessment = f"Your productivity rating was {score}."
    switching_assessment = (
        f"You switched {metrics.context_switches} times ({metrics.switches_per_active_hour:.0f} per active hour), "
        f"with a median dwell of {metrics.median_dwell_seconds:.0f} seconds."
    )
    distraction_assessment = (
        f"Distracting activity accounted for {metrics.distraction_share:.0f}% of active time; "
        f"{metrics.excluded_minutes:g} minutes of inactive time were excluded."
    )
    progress_assessment = grounded_progress_assessment(metrics)
    return SummaryContent(
        summary=recap,
        primary_activity=metrics.dominant_category or "unknown activity",
        projects=projects,
        topics=tuple(metrics.categories.keys())[:5],
        people_organizations=(),
        work_completed="No completion can be verified from activity telemetry alone.",
        work_in_progress=progress_assessment,
        next_likely_action="Resume the dominant in-progress activity.",
        observations=(on_task_assessment, switching_assessment, distraction_assessment),
        confidence=0.45 if metrics.source_event_count else 0.1,
        headline=headline,
        recap=recap,
        on_task_assessment=on_task_assessment,
        switching_assessment=switching_assessment,
        distraction_assessment=distraction_assessment,
        progress_assessment=progress_assessment,
    )


def grounded_progress_assessment(metrics: TelemetryMetrics) -> str:
    """Describe observable concentration without inferring a task or result."""

    projects = [name for name, _ in sorted(
        metrics.projects.items(), key=lambda item: (-item[1], item[0].casefold())
    )[:3] if name]
    if projects:
        labels = _human_list(projects)
        return (
            f"Observed activity included the sanitized workspace label{'' if len(projects) == 1 else 's'} "
            f"{labels}; this suggests work in progress but does not verify completion."
        )
    apps = [name for name, _ in sorted(
        metrics.applications.items(), key=lambda item: (-item[1], item[0].casefold())
    )[:3] if name]
    if apps:
        return (
            f"Observed activity was concentrated in {_human_list(apps)}; telemetry does not establish "
            "what work was completed."
        )
    return "No specific progress can be inferred from the available activity telemetry."


def _human_list(values: list[str]) -> str:
    if len(values) < 2:
        return values[0] if values else "recorded apps"
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"
