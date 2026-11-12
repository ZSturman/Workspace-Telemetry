"""Telemetry orchestration, incremental processing, and comparison records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from .notion_api import plain_text

from .activitywatch import ActivityWatchClient
from .aggregation import aggregate
from .config import TelemetrySettings
from .context_switches import fetch_context_switches
from .lm_studio import LMStudioClient, LMStudioError
from .models import GenerationResult, SummaryContent, TelemetryRecord, TimeRange
from .notion import TelemetrySummariesStore
from .privacy import infer_projects
from .state import StoredSummary, TelemetryState
from .strategies import (
    PROMPT_CONTRACT_VERSION,
    PROMPT_FINGERPRINT,
    build_prompt_input,
    child_cadence,
    deterministic_summary,
    grounded_progress_assessment,
    source_hash,
    strategies_for,
)


class TelemetryPipeline:
    def __init__(
        self,
        settings: TelemetrySettings,
        *,
        notion: Any,
        state: TelemetryState | None = None,
        activitywatch: ActivityWatchClient | None = None,
        lm_studio: LMStudioClient | None = None,
    ) -> None:
        self.settings = settings
        self.notion = notion
        self.state = state or TelemetryState(settings.state_path)
        self.activitywatch = activitywatch or ActivityWatchClient(settings.activitywatch_base_url)
        self.lm_studio = lm_studio or LMStudioClient(
            settings.lm_studio_endpoint,
            settings.lm_studio_model,
            timeout_seconds=settings.lm_studio_timeout_seconds,
            retries=settings.lm_studio_retries,
            response_format=settings.lm_studio_response_format,
        )
        self.store = TelemetrySummariesStore(notion, settings.summaries_database_id)

    def close(self) -> None:
        self.state.close()

    def run(
        self,
        time_range: TimeRange,
        cadence: str,
        *,
        requested_strategies: list[str] | None = None,
        dry_run: bool = False,
        force: bool = False,
        use_lm_studio: bool = True,
    ) -> list[dict[str, Any]]:
        context_switches, context_errors = fetch_context_switches(
            self.notion, self.settings.context_switches_database_id, time_range
        )
        activitywatch = self.activitywatch.collect(time_range, self.settings.privacy)
        metrics = aggregate(
            time_range,
            activitywatch,
            context_switches,
            context_errors,
            self.settings.privacy,
            context_available=not context_errors,
        )
        projects = infer_projects(
            applications=metrics.applications,
            domains=metrics.domains,
            categories=metrics.categories,
            rules=self.settings.privacy,
        )
        outcomes: list[dict[str, Any]] = []
        configured_strategies = requested_strategies or list(self.settings.enabled_strategies) or None
        for strategy in strategies_for(cadence, configured_strategies):
            rollups = self._rollups(time_range, child_cadence(strategy)) if strategy.source_mode in {"rollup", "combined"} else []
            if strategy.id == "rolling-24h-human-v2":
                rollups = self._prior_period_deltas(strategy.id, metrics)
            payload = build_prompt_input(
                time_range=time_range,
                strategy=strategy,
                metrics=metrics,
                rollups=rollups,
                projects=projects,
            )
            payload_hash = source_hash(
                {"input": payload, "promptContractVersion": PROMPT_CONTRACT_VERSION}
                if strategy.context_size == "production" else payload
            )
            key = record_key(time_range, strategy.id)
            stored = self.state.get(key)
            if not force and stored and stored.source_hash == payload_hash and stored.status == "Generated":
                outcomes.append({"key": key, "status": "unchanged", "pageId": stored.page_id, "strategy": strategy.id})
                continue
            generation = self._generate(payload, metrics, strategy, projects, use_lm_studio=use_lm_studio)
            status = _status_for(metrics, generation, strategy.id)
            previous = self.state.previous(strategy.id, cadence, time_range.start) or self._previous_from_notion(
                strategy.id, cadence, time_range.start
            )
            record = TelemetryRecord(
                key=key,
                time_range=time_range,
                strategy=strategy,
                source_hash=payload_hash,
                metrics=metrics,
                generation=generation,
                status=status,
                previous_page_id=previous.page_id if previous else "",
                raw_data={
                    "schemaVersion": 1,
                    "cadence": cadence,
                    "strategy": {"id": strategy.id, "version": strategy.version, "sourceMode": strategy.source_mode, "contextSize": strategy.context_size},
                    "promptFingerprint": PROMPT_FINGERPRINT,
                    "privacyPolicyVersion": 1,
                    "sourceAvailability": {
                        "contextSwitches": not bool(context_errors),
                        "activityWatch": activitywatch.available,
                        "afk": activitywatch.has_afk_bucket,
                    },
                    "rollupInputKeys": [item.get("key", "") for item in rollups],
                },
            )
            page_id, action = self.store.upsert(record, dry_run=dry_run)
            if not dry_run:
                self.state.save(StoredSummary(
                    key=key,
                    cadence=cadence,
                    strategy_id=strategy.id,
                    period_start=time_range.start.isoformat(),
                    period_end=time_range.end.isoformat(),
                    page_id=page_id,
                    source_hash=payload_hash,
                    status=status,
                ))
            outcomes.append({
                "key": key,
                "status": status,
                "action": action,
                "pageId": page_id,
                "strategy": strategy.id,
                "sourceHash": payload_hash,
                "lmStudio": generation.used_lm_studio,
                "error": generation.error,
            })
        return outcomes

    def compare(
        self,
        time_range: TimeRange,
        cadence: str,
        *,
        requested_strategies: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        results = self.run(
            time_range,
            cadence,
            requested_strategies=requested_strategies,
            dry_run=dry_run,
            force=True,
        )
        title = f"Telemetry | comparison | {cadence} | {time_range.start.strftime('%Y-%m-%d %H:%M %z')}"
        comparison = {
            "schemaVersion": 1,
            "summaryType": "comparison",
            "cadence": cadence,
            "results": results,
            "generatedAt": datetime.now(self.settings.zoneinfo()).isoformat(),
        }
        fingerprint = hashlib.sha256(json.dumps(comparison, sort_keys=True).encode("utf-8")).hexdigest()
        summary = "Prompt comparison: " + "; ".join(
            f"{item['strategy']}={item['status']}" for item in results
        )
        page_id, action = self.store.upsert_comparison(
            title=title,
            start=time_range.start.isoformat(),
            end=time_range.end.isoformat(),
            source_hash=fingerprint,
            raw_data=comparison,
            summary=summary,
            dry_run=dry_run,
        )
        return {"results": results, "comparisonPageId": page_id, "comparisonAction": action}

    def tick(
        self,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
        requested_strategies: list[str] | None = None,
        force: bool = False,
        scheduled: bool = False,
    ) -> dict[str, Any]:
        now = (now or datetime.now(self.settings.zoneinfo())).astimezone(self.settings.zoneinfo())
        output: dict[str, Any] = {"interval": [], "rolling": [], "hour": [], "day": [], "week": []}
        cutoff = _floor(now - timedelta(minutes=self.settings.late_arrival_minutes), 30)
        if scheduled:
            period = TimeRange(cutoff - timedelta(minutes=30), cutoff)
            strategies = requested_strategies or [self.settings.scheduled_interval_strategy]
            output["interval"].extend(self.run(
                period,
                "interval",
                dry_run=dry_run,
                requested_strategies=strategies,
                force=force,
                use_lm_studio=False,
            ))
            output["rolling"].extend(self.run(
                TimeRange(now - timedelta(hours=24), now),
                "rolling",
                dry_run=dry_run,
                requested_strategies=["rolling-24h-human-v2"],
                force=force,
                use_lm_studio=True,
            ))
            day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
            output["day"].extend(self.run(
                TimeRange(day_end - timedelta(days=1), day_end),
                "day",
                dry_run=dry_run,
                requested_strategies=["daily-production-v2"],
                force=force,
                use_lm_studio=False,
            ))
            if not dry_run:
                self.state.save_watermark("interval", period.end)
            return output
        interval_periods = self._periods_since("interval", cutoff, timedelta(minutes=30), self.settings.backfill_hours)
        for period in interval_periods:
            output["interval"].extend(self.run(period, "interval", dry_run=dry_run, requested_strategies=requested_strategies, force=force))
        hour_periods = self._periods_since("hour", _floor_hour(now - timedelta(minutes=self.settings.late_arrival_minutes)), timedelta(hours=1), self.settings.backfill_hours)
        for period in hour_periods:
            output["hour"].extend(self.run(period, "hour", dry_run=dry_run, requested_strategies=requested_strategies, force=force))
        yesterday_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        output["day"].extend(self.run(TimeRange(yesterday_end - timedelta(days=1), yesterday_end), "day", dry_run=dry_run, requested_strategies=requested_strategies, force=force))
        week_end = yesterday_end - timedelta(days=yesterday_end.weekday())
        output["week"].extend(self.run(TimeRange(week_end - timedelta(days=7), week_end), "week", dry_run=dry_run, requested_strategies=requested_strategies, force=force))
        if not dry_run:
            if interval_periods:
                self.state.save_watermark("interval", max(item.end for item in interval_periods))
            if hour_periods:
                self.state.save_watermark("hour", max(item.end for item in hour_periods))
        return output

    def bootstrap(
        self,
        *,
        days: int = 7,
        now: datetime | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Seed only the three production views, without experimental strategies."""

        now = (now or datetime.now(self.settings.zoneinfo())).astimezone(self.settings.zoneinfo())
        cutoff = _floor(now - timedelta(minutes=self.settings.late_arrival_minutes), 30)
        output: dict[str, Any] = {"interval": [], "rolling": [], "day": []}
        for offset in range(4, 0, -1):
            end = cutoff - timedelta(minutes=30 * (offset - 1))
            output["interval"].extend(self.run(
                TimeRange(end - timedelta(minutes=30), end), "interval",
                requested_strategies=["interval-production-v2"], dry_run=dry_run,
                force=force, use_lm_studio=False,
            ))
        output["rolling"].extend(self.run(
            TimeRange(now - timedelta(hours=24), now), "rolling",
            requested_strategies=["rolling-24h-human-v2"], dry_run=dry_run,
            force=force, use_lm_studio=True,
        ))
        day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(days, 0, -1):
            end = day_end - timedelta(days=offset - 1)
            output["day"].extend(self.run(
                TimeRange(end - timedelta(days=1), end), "day",
                requested_strategies=["daily-production-v2"], dry_run=dry_run,
                force=force, use_lm_studio=False,
            ))
        return output

    def _periods_since(self, cadence: str, closed_end: datetime, unit: timedelta, max_hours: int) -> list[TimeRange]:
        watermark = self.state.watermark(cadence)
        earliest = closed_end - timedelta(hours=max_hours)
        start = max(watermark or earliest, earliest)
        start = _align(start, unit)
        periods: list[TimeRange] = []
        while start + unit <= closed_end:
            periods.append(TimeRange(start, start + unit))
            start += unit
        # Recheck the three most recently completed periods for late arrivals.
        for offset in range(1, 4):
            end = closed_end - unit * (offset - 1)
            candidate = TimeRange(end - unit, end)
            if candidate not in periods:
                periods.append(candidate)
        periods = sorted({(item.start, item.end): item for item in periods}.values(), key=lambda item: item.start)
        return periods

    def _rollups(self, time_range: TimeRange, cadence: str) -> list[dict[str, Any]]:
        if not cadence:
            return []
        try:
            pages = self.notion.query_database(
                self.settings.summaries_database_id,
                filter_payload={
                    "and": [
                        {"property": "Period Start", "date": {"on_or_after": time_range.start.isoformat()}},
                        {"property": "Period Start", "date": {"before": time_range.end.isoformat()}},
                    ]
                },
            )
        except Exception:
            return []
        result: list[dict[str, Any]] = []
        for page in pages:
            try:
                raw = json.loads(plain_text(page, "Raw Data"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if raw.get("cadence") != cadence:
                continue
            result.append({
                "key": raw.get("summaryKey", ""),
                "strategy": (raw.get("strategy") or {}).get("id", ""),
                "summary": plain_text(page, "Summary")[:1000],
                "status": raw.get("status", ""),
            })
        return result[:30]

    def _previous_from_notion(self, strategy_id: str, cadence: str, before: datetime) -> StoredSummary | None:
        """Recover the predecessor relation after a local checkpoint loss."""

        try:
            pages = self.notion.query_database(
                self.settings.summaries_database_id,
                filter_payload={"property": "Period End", "date": {"before": before.isoformat()}},
                sorts=[{"property": "Period End", "direction": "descending"}],
                page_size=30,
            )
        except Exception:
            return None
        for page in pages:
            try:
                raw = json.loads(plain_text(page, "Raw Data"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if raw.get("cadence") != cadence or (raw.get("strategy") or {}).get("id") != strategy_id:
                continue
            start = plain_text(page, "Period Start")
            end = plain_text(page, "Period End")
            return StoredSummary(
                key=str(raw.get("summaryKey") or ""),
                cadence=cadence,
                strategy_id=strategy_id,
                period_start=start,
                period_end=end,
                page_id=str(page.get("id") or ""),
                source_hash=plain_text(page, "Source Hash"),
                status=str(raw.get("status") or ""),
            )
        return None

    def _prior_period_deltas(self, strategy_id: str, metrics: Any) -> list[dict[str, Any]]:
        try:
            pages = self.notion.query_database(
                self.settings.summaries_database_id,
                filter_payload={"property": "Name", "title": {"contains": strategy_id}},
                sorts=[{"property": "Generated At", "direction": "descending"}],
                page_size=1,
            )
            display = json.loads(plain_text(pages[0], "Display Data")) if pages else {}
            previous = display.get("metrics") if isinstance(display, dict) else {}
        except (Exception, json.JSONDecodeError):
            previous = {}
        if not isinstance(previous, dict):
            return []
        current = metrics.as_prompt_dict()["sessionTotals"]
        deltas: dict[str, float] = {}
        for key in ("activeMinutes", "onTaskMinutes", "distractionMinutes", "idleMinutes"):
            value = previous.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                deltas[key] = round(float(current[key]) - float(value), 1)
        return [{"deltas": deltas}] if deltas else []

    def _generate(
        self,
        payload: dict[str, Any],
        metrics: Any,
        strategy: Any,
        projects: tuple[str, ...],
        *,
        use_lm_studio: bool = True,
    ) -> GenerationResult:
        if not use_lm_studio:
            return GenerationResult(deterministic_summary(metrics=metrics, strategy=strategy, projects=projects), False)
        try:
            generation = self.lm_studio.summarize(payload)
            if strategy.context_size != "production":
                return generation
            progress = grounded_progress_assessment(metrics)
            content = replace(
                generation.content,
                work_completed="No completion can be verified from activity telemetry alone.",
                work_in_progress=progress,
                progress_assessment=progress,
            )
            return replace(generation, content=content)
        except (LMStudioError, ValueError, RuntimeError) as exc:
            return GenerationResult(
                deterministic_summary(metrics=metrics, strategy=strategy, projects=projects),
                False,
                error=str(exc),
            )


def record_key(time_range: TimeRange, strategy_id: str) -> str:
    if strategy_id == "rolling-24h-human-v2":
        return "rolling-24h-human-v2:current"
    return f"{strategy_id}:{time_range.start.isoformat()}:{time_range.end.isoformat()}"


def _status_for(metrics: Any, generation: GenerationResult, strategy_id: str) -> str:
    if metrics.source_errors or (metrics.activitywatch_available and not metrics.afk_known):
        return "Partial"
    if strategy_id in {"interval-production-v2", "daily-production-v2"}:
        return "Generated"
    return "Generated" if generation.used_lm_studio else "Fallback"


def _floor(value: datetime, minutes: int) -> datetime:
    return value.replace(minute=value.minute - value.minute % minutes, second=0, microsecond=0)


def _floor_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _align(value: datetime, unit: timedelta) -> datetime:
    if unit == timedelta(minutes=30):
        return _floor(value, 30)
    if unit == timedelta(hours=1):
        return _floor_hour(value)
    return value
