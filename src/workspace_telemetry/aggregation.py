"""Overlap-safe productivity metrics calculated from activity classifications."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from .config import PrivacyRules
from .models import ActivityEvent, ActivityWatchSnapshot, ContextSwitch, TelemetryMetrics, TimeRange


def aggregate(
    time_range: TimeRange,
    activitywatch: ActivityWatchSnapshot,
    context_switches: list[ContextSwitch],
    context_errors: list[str],
    rules: PrivacyRules,
    *,
    context_available: bool = True,
) -> TelemetryMetrics:
    afk_events = _merge_afk_events([event for event in activitywatch.events if event.kind == "afk" and event.afk])
    windows = [event for event in activitywatch.events if event.kind == "window"]
    browser_events = [event for event in activitywatch.events if event.kind == "browser"]
    canonical = windows or browser_events or _context_duration_events(context_switches, time_range)
    canonical_source = "activitywatch-window" if windows else "activitywatch-browser" if browser_events else "context-switches" if context_switches else "none"
    metrics = TelemetryMetrics(
        friction_weight=round(sum(item.friction_weight for item in context_switches), 3),
        activitywatch_buckets=activitywatch.bucket_ids,
        context_switch_ids=tuple(item.external_id for item in context_switches),
        source_event_count=len(activitywatch.events) + len(context_switches),
        canonical_source=canonical_source,
        activitywatch_available=activitywatch.available,
        afk_known=activitywatch.has_afk_bucket,
        context_switches_available=context_available,
        source_errors=tuple(activitywatch.errors + context_errors),
    )
    blocks = _apply_segments(metrics, canonical, afk_events, time_range, rules)
    _apply_domains(metrics, browser_events, afk_events, time_range)
    _derive_switching(metrics, blocks, context_switches)
    _derive_sessions_and_blocks(metrics, blocks)
    _derive_score(metrics, activitywatch, canonical_source)
    return metrics


def _apply_segments(metrics: TelemetryMetrics, events: list[ActivityEvent], afk_events: list[ActivityEvent], time_range: TimeRange, rules: PrivacyRules) -> list[dict[str, Any]]:
    boundaries = {time_range.start, time_range.end}
    for item in [*events, *afk_events]:
        boundaries.add(max(time_range.start, item.start)); boundaries.add(min(time_range.end, item.end))
    blocks: list[dict[str, Any]] = []
    points = sorted(boundaries)
    for start, end in zip(points, points[1:]):
        seconds = (end - start).total_seconds()
        if seconds <= 0: continue
        midpoint = start + (end - start) / 2
        covering_afk = next((event for event in afk_events if event.start <= midpoint < event.end), None)
        if covering_afk:
            if (covering_afk.end - covering_afk.start).total_seconds() >= 1200: metrics.excluded_seconds += seconds
            else: metrics.idle_seconds += seconds
            blocks.append({"start": start, "end": end, "break": True})
            continue
        active = [event for event in events if event.start <= midpoint < event.end]
        if not active: continue
        chosen = max(active, key=lambda event: (event.start, event.source_id))
        category = (chosen.category or "other").casefold()
        classification = _classification(category, rules)
        metrics.active_seconds += seconds
        if classification == "on-task": metrics.on_task_seconds += seconds
        elif classification == "distracting": metrics.distraction_seconds += seconds
        else: metrics.neutral_seconds += seconds
        if chosen.app: metrics.applications[chosen.app] = metrics.applications.get(chosen.app, 0.0) + seconds
        metrics.categories[category] = metrics.categories.get(category, 0.0) + seconds
        if chosen.work_label: metrics.projects[chosen.work_label] = metrics.projects.get(chosen.work_label, 0.0) + seconds
        blocks.append({"start": start, "end": end, "app": chosen.app or "Unknown", "category": category, "project": chosen.work_label, "classification": classification})
    return _merge_blocks(blocks)


def _classification(category: str, rules: PrivacyRules) -> str:
    if category in rules.productive_categories: return "on-task"
    if category in rules.distracting_categories: return "distracting"
    return "neutral"


def _merge_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("break"):
            merged.append(dict(block)); continue
        if merged and not merged[-1].get("break") and all(merged[-1][key] == block[key] for key in ("app", "category", "project")) and (block["start"] - merged[-1]["end"]).total_seconds() <= 1:
            merged[-1]["end"] = block["end"]
        else: merged.append(dict(block))
    return merged


def _derive_switching(metrics: TelemetryMetrics, blocks: list[dict[str, Any]], context_switches: list[ContextSwitch]) -> None:
    visits = [block for block in blocks if not block.get("break") and block["app"]]
    transitions = [(left["app"], right["app"]) for left, right in zip(visits, visits[1:]) if left["app"] != right["app"]]
    metrics.context_switches = max(len(transitions), len(context_switches))
    dwells = [(block["end"] - block["start"]).total_seconds() for block in visits] or [item.seconds_in_previous_app for item in context_switches if item.seconds_in_previous_app > 0]
    metrics.median_dwell_seconds = median(dwells) if dwells else 0.0
    metrics.short_dwell_visits = sum(1 for seconds in dwells if seconds < 30)
    metrics.short_dwell_ratio = metrics.short_dwell_visits / len(dwells) if dwells else 0.0
    sequence = [block["app"] for block in visits]
    metrics.bounce_backs = sum(1 for index in range(len(sequence) - 2) if sequence[index] == sequence[index + 2] != sequence[index + 1])
    metrics.bounce_ratio = metrics.bounce_backs / max(1, len(transitions))
    counts = Counter(transitions)
    metrics.top_transitions = tuple((left, right, count) for (left, right), count in sorted(counts.items(), key=lambda item: (-item[1], item[0][0].casefold(), item[0][1].casefold()))[:5])
    metrics.switches_per_active_hour = metrics.context_switches / (metrics.active_seconds / 3600) if metrics.active_seconds else 0.0


def _derive_sessions_and_blocks(metrics: TelemetryMetrics, blocks: list[dict[str, Any]]) -> None:
    sessions = 0; previous_end: datetime | None = None
    for block in blocks:
        if block.get("break"): previous_end = None; continue
        if previous_end is None or (block["start"] - previous_end).total_seconds() >= 1200: sessions += 1
        previous_end = block["end"]
    metrics.session_count = sessions
    notable = sorted((block for block in blocks if not block.get("break")), key=lambda block: (block["end"] - block["start"]).total_seconds(), reverse=True)
    metrics.notable_blocks = tuple({"start": block["start"].isoformat(), "end": block["end"].isoformat(), "minutes": round((block["end"] - block["start"]).total_seconds() / 60, 1), "app": block["app"], "project": block["project"], "classification": block["classification"]} for block in notable[:5] if (block["end"] - block["start"]).total_seconds() >= 300)


def _derive_score(metrics: TelemetryMetrics, activitywatch: ActivityWatchSnapshot, canonical_source: str) -> None:
    active = metrics.active_seconds
    if active: metrics.productive_share = 100 * metrics.on_task_seconds / active; metrics.distraction_share = 100 * metrics.distraction_seconds / active
    switch_penalty = _clamp((metrics.switches_per_active_hour - 10) / 50, 0, 1)
    metrics.stability_score = _clamp(100 - 50 * switch_penalty - 30 * metrics.short_dwell_ratio - 20 * metrics.bounce_ratio, 0, 100)
    if active < 1200: metrics.score_reason = "At least 20 active minutes are required for a reliable rating."; return
    if not (activitywatch.available and activitywatch.has_afk_bucket and canonical_source in {"activitywatch-window", "activitywatch-browser"} and not activitywatch.errors):
        metrics.score_reason = "Complete foreground-window and AFK coverage are required for a reliable rating."; return
    score = 0.65 * metrics.productive_share + 0.25 * metrics.stability_score + 0.10 * (100 - metrics.distraction_share)
    metrics.on_task_score = round(_clamp(score, 0, 100), 1); metrics.score_label = _score_label(metrics.on_task_score)


def _score_label(score: float) -> str:
    return "Deeply on task" if score >= 85 else "Steady" if score >= 70 else "Mixed" if score >= 50 else "Fragmented" if score >= 30 else "Mostly off-task"


def _context_duration_events(values: list[ContextSwitch], time_range: TimeRange) -> list[ActivityEvent]:
    result = []
    for item in values:
        if item.seconds_in_previous_app <= 0: continue
        start, end = item.switched_at - timedelta(seconds=item.seconds_in_previous_app), item.switched_at
        if end > time_range.start and start < time_range.end:
            result.append(ActivityEvent(source_id=f"context:{item.external_id}", bucket_id="context-switches", kind="window", start=max(start, time_range.start), end=min(end, time_range.end), app=item.from_app, category=item.from_category))
    return result


def _merge_afk_events(events: list[ActivityEvent]) -> list[ActivityEvent]:
    merged: list[ActivityEvent] = []
    for event in sorted(events, key=lambda item: (item.start, item.end)):
        if merged and (event.start - merged[-1].end).total_seconds() <= 5:
            prior = merged[-1]; merged[-1] = ActivityEvent(source_id=prior.source_id, bucket_id=prior.bucket_id, kind="afk", start=prior.start, end=max(prior.end, event.end), afk=True)
        else: merged.append(event)
    return merged


def _apply_domains(metrics: TelemetryMetrics, browser_events: list[ActivityEvent], afk_events: list[ActivityEvent], time_range: TimeRange) -> None:
    for event in browser_events:
        boundaries = {max(event.start, time_range.start), min(event.end, time_range.end)}
        for afk in afk_events:
            if afk.end > event.start and afk.start < event.end: boundaries.update({max(event.start, afk.start), min(event.end, afk.end)})
        points = sorted(boundaries)
        for start, end in zip(points, points[1:]):
            midpoint = start + (end - start) / 2
            if event.domain and not any(afk.start <= midpoint < afk.end for afk in afk_events): metrics.domains[event.domain] = metrics.domains.get(event.domain, 0.0) + (end - start).total_seconds()


def _clamp(value: float, minimum: float, maximum: float) -> float: return max(minimum, min(maximum, value))
