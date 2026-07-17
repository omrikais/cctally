"""Dashboard-only, cache-backed provider read models for #294 S4."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from _cctally_core import get_week_start_name
from _cctally_quota import (
    codex_physical_mutation_seq,
    load_codex_quota_observations,
    load_codex_quota_projection_certificate,
)
from _cctally_source_analytics import load_qualified_codex_entries
from _lib_dashboard_sources import (
    CapabilityRecord,
    ProjectionCoherence,
    SourceDashboardState,
    assess_codex_projection_coherence,
    dashboard_resource_key,
)
from _lib_quota import (
    build_blocks,
    build_history,
    forecast_quota,
    percent_milestones,
    quota_freshness,
    select_baseline,
)
from _lib_jsonl import CodexEntry
from _lib_source_analytics import build_codex_project_result
from _lib_view_models import (
    build_codex_daily_view,
    build_codex_monthly_view,
    build_codex_session_view,
    build_codex_weekly_view,
)


UTC = dt.timezone.utc
SOURCE_HISTORY_LIMIT = 250
DASHBOARD_QUOTA_OBSERVATION_LIMIT = 1000
DASHBOARD_QUOTA_RECENT_DAYS = 35


class CodexProjectionIncoherent(RuntimeError):
    """Physical cache and S2 interpreted quota state cannot be mixed."""

    def __init__(self, reason: str | None) -> None:
        self.reason = reason
        super().__init__("Codex quota projection is incoherent")


class SourceCapabilityUnavailable(ValueError):
    """A source is not a physical owner or cannot serve a resource domain."""


class SourceResourceNotFound(LookupError):
    """A valid opaque resource key has no row in its provider state."""


@dataclass(frozen=True)
class DashboardSourceSemantics:
    """One canonical CLI configuration resolution for a dashboard read.

    The source bundle must use the same effective Codex tier and calendar-week
    anchor as the CLI.  Keeping that resolution in one small immutable object
    also makes every render-affecting configuration input explicit in the
    provider identity rather than accidentally treating it as an idle tick.
    """

    display_tz_name: str | None
    week_start_name: str
    week_start_idx: int
    speed: str
    codex_budget: Mapping[str, object] | None
    claude_identity: str
    codex_identity: str


def resolve_dashboard_source_semantics(
    config: Mapping[str, object] | None,
    *,
    display_tz_name: str | None,
) -> DashboardSourceSemantics:
    """Resolve dashboard semantics through the shipped CLI kernels.

    ``_resolve_codex_speed('auto')`` is intentionally the only tier resolver:
    it preserves the CLI's all-$CODEX_HOME fast-service-tier behavior.  The
    weekly index comes from the same ``get_week_start_name``/``WEEKDAY_MAP``
    pair used by the report and budget command surfaces.
    """
    c = sys.modules["cctally"]
    raw_config = dict(config or {})
    week_start_name = get_week_start_name(raw_config)
    week_start_idx = c.WEEKDAY_MAP[week_start_name]
    speed = c._resolve_codex_speed("auto")
    budget_config = c._get_budget_config(raw_config)
    raw_codex_budget = budget_config.get("codex")
    codex_budget = (
        MappingProxyType(dict(raw_codex_budget))
        if isinstance(raw_codex_budget, Mapping) else None
    )
    # The legacy Claude projection owns the non-Codex config surface.  Codex
    # budget semantics are explicitly excluded so changing them cannot evict a
    # byte-identical Claude source object.
    claude_config = dict(raw_config)
    raw_budget = claude_config.get("budget")
    if isinstance(raw_budget, Mapping):
        claude_budget = dict(raw_budget)
        claude_budget.pop("codex", None)
        if claude_budget:
            claude_config["budget"] = claude_budget
        else:
            claude_config.pop("budget", None)
    claude_identity_payload = {
        "display_tz_name": display_tz_name,
        "render_config": claude_config,
    }
    codex_identity_payload = {
        "codex_budget": dict(codex_budget) if codex_budget is not None else None,
        "display_tz_name": display_tz_name,
        "speed": speed,
        "week_start_name": week_start_name,
    }
    claude_identity = hashlib.sha256(json.dumps(
        claude_identity_payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()[:24]
    codex_identity = hashlib.sha256(json.dumps(
        codex_identity_payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()[:24]
    return DashboardSourceSemantics(
        display_tz_name=display_tz_name,
        week_start_name=week_start_name,
        week_start_idx=week_start_idx,
        speed=speed,
        codex_budget=codex_budget,
        claude_identity=claude_identity,
        codex_identity=codex_identity,
    )


@dataclass(frozen=True)
class DashboardReadContext:
    """Already-open, coordinated-ingest database inputs for one provider read."""

    cache_conn: sqlite3.Connection
    stats_conn: sqlite3.Connection
    range_start: dt.datetime
    now_utc: dt.datetime
    display_tz_name: str | None
    week_start_idx: int = 0
    week_start_name: str = "monday"
    speed: str = "standard"
    codex_budget: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        for name in ("range_start", "now_utc"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(UTC))
        if self.now_utc < self.range_start:
            raise ValueError("now_utc must not precede range_start")
        if not isinstance(self.week_start_name, str) or not self.week_start_name:
            raise ValueError("week_start_name must be a non-empty string")
        if self.codex_budget is not None and not isinstance(self.codex_budget, Mapping):
            raise ValueError("codex_budget must be a mapping or None")


_RESOURCE_ROWS = {
    "session": ("sessions", "rows"),
    "project": ("projects", "rows"),
    "block": ("quota", "blocks"),
}


def _public_copy(value: object) -> object:
    """Detach a bounded source row from its immutable published state."""
    if isinstance(value, Mapping):
        return {str(key): _public_copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_public_copy(item) for item in value]
    return value


def source_detail_lookup(bundle: object, source: str, resource: str, key: str) -> dict[str, object]:
    """Find one provider-owned opaque row without I/O, ingest, or fallback.

    The handler has already parsed the fixed route grammar.  This adapter only
    reads the frozen bundle published by the dashboard owner thread, so a
    request cannot accidentally trigger cache sync, rollout parsing, or a
    Claude fallback for a Codex key.
    """
    if source not in ("claude", "codex") or resource not in _RESOURCE_ROWS:
        raise SourceCapabilityUnavailable()
    try:
        state = bundle.sources[source]
        data = state.data
    except (AttributeError, KeyError, TypeError) as exc:
        raise SourceCapabilityUnavailable() from exc
    if state.availability == "unavailable" or not isinstance(data, Mapping):
        raise SourceCapabilityUnavailable()
    domain, rows_key = _RESOURCE_ROWS[resource]
    try:
        rows = data[domain][rows_key]
    except (KeyError, TypeError) as exc:
        raise SourceCapabilityUnavailable() from exc
    for row in rows:
        if isinstance(row, Mapping) and row.get("key") == key:
            return _public_copy(row)  # type: ignore[return-value]
    raise SourceResourceNotFound()


def codex_projection_coherence(
    context: DashboardReadContext,
) -> ProjectionCoherence:
    """Check every active root against the post-reconciliation certificate.

    The source adapter is intentionally a reader: it never reconciles or
    mutates either database.  The certificate is stamped from S2's exact full
    physical signature only after its stats transaction commits, and its cache
    sequence must still match before presentation can use it.
    """
    try:
        active_roots = tuple(sorted(
            str(row[0]) for row in context.cache_conn.execute(
                "SELECT source_root_key FROM codex_source_roots"
            )
        ))
        if not active_roots:
            return ProjectionCoherence(True)
        certificate = load_codex_quota_projection_certificate(context.cache_conn)
        if certificate is None or certificate[0] != codex_physical_mutation_seq(context.cache_conn):
            return ProjectionCoherence(False, "projection_certificate_stale")
        resolved_physical_signatures = dict(certificate[1])
        projection_signatures = {
            str(root_key): str(signature)
            for root_key, signature in context.stats_conn.execute(
                "SELECT source_root_key, physical_signature "
                "FROM quota_projection_state"
            )
        }
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return ProjectionCoherence(False, "projection_read_failed")
    return assess_codex_projection_coherence(
        active_root_keys=active_roots,
        physical_signatures=resolved_physical_signatures,
        projection_signatures=projection_signatures,
    )


def _codex_budget_cost_events(
    context: DashboardReadContext,
    entries: Iterable[object],
) -> tuple[tuple[dt.datetime, float], ...]:
    """Freeze every configured-window cost event for exact idle pace updates."""
    if context.codex_budget is None:
        return ()
    _period, start_at, end_at = _configured_codex_budget_window(context)
    c = sys.modules["cctally"]
    events: list[tuple[dt.datetime, float]] = []
    for entry in entries:
        timestamp = getattr(entry, "timestamp", None)
        if not isinstance(timestamp, dt.datetime):
            continue
        timestamp = timestamp.astimezone(UTC)
        if not start_at <= timestamp < end_at:
            continue
        events.append((
            timestamp,
            c._calculate_codex_entry_cost(
                str(getattr(entry, "model")),
                int(getattr(entry, "input_tokens")),
                int(getattr(entry, "cached_input_tokens")),
                int(getattr(entry, "output_tokens")),
                int(getattr(entry, "reasoning_output_tokens")),
                speed=context.speed,
            ),
        ))
    return tuple(events)


def _bucket_wire(bucket: Any) -> dict[str, object]:
    return {
        "label": bucket.bucket,
        "cost_usd": bucket.cost_usd,
        "input_tokens": bucket.input_tokens,
        "cached_input_tokens": bucket.cached_input_tokens,
        "output_tokens": bucket.output_tokens,
        "reasoning_output_tokens": bucket.reasoning_output_tokens,
        "total_tokens": bucket.total_tokens,
        "models": tuple(bucket.models),
    }


def _period_wire(view: Any) -> dict[str, object]:
    return {
        "rows": tuple(_bucket_wire(row) for row in view.rows),
        "total_cost_usd": view.total_cost_usd,
        "total_tokens": view.total_tokens,
        "display_tz": view.display_tz_label,
    }


def _session_wire(view: Any) -> dict[str, object]:
    rows = []
    for ordinal, row in enumerate(view.rows, start=1):
        # The Codex session aggregator intentionally splits equal relative
        # session paths from distinct $CODEX_HOME roots.  The opaque detail
        # key must use that same grouping identity or two visible rows route
        # to one another's detail payload.
        root_identity = row.codex_root or "single-root"
        rows.append({
            "key": dashboard_resource_key(
                "session", "codex", root_identity, row.session_id_path,
            ),
            "source": "codex",
            "label": f"Session {ordinal}",
            "last_activity": row.last_activity.astimezone(UTC).isoformat(),
            "cost_usd": row.cost_usd,
            "input_tokens": row.input_tokens,
            "cached_input_tokens": row.cached_input_tokens,
            "output_tokens": row.output_tokens,
            "reasoning_output_tokens": row.reasoning_output_tokens,
            "total_tokens": row.total_tokens,
            "models": tuple(row.models),
        })
    return {
        "rows": tuple(rows),
        "total_sessions": view.total_sessions,
        "total_cost_usd": view.total_cost_usd,
        "total_tokens": view.total_tokens,
    }


def _quota_wire(stats_conn: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    try:
        rows = stats_conn.execute(
            "SELECT source_root_key, logical_limit_key, observed_slot, window_minutes, "
            "limit_name, resets_at_utc, current_percent, orphaned_at "
            "FROM quota_window_blocks WHERE source='codex' "
            "ORDER BY resets_at_utc DESC, source_root_key, logical_limit_key, observed_slot "
            "LIMIT ?",
            (SOURCE_HISTORY_LIMIT,),
        ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple({
        "key": dashboard_resource_key(
            "block", "codex", root_key, logical_limit_key, observed_slot, window_minutes, resets_at,
        ),
        "source": "codex",
        "label": limit_name or "Codex quota",
        "resets_at": resets_at,
        "current_percent": current_percent,
        "orphaned": orphaned_at is not None,
    } for (
        root_key, logical_limit_key, observed_slot, window_minutes,
        limit_name, resets_at, current_percent, orphaned_at,
    ) in rows)


def _budget_wire(stats_conn: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    try:
        rows = stats_conn.execute(
            "SELECT period_start_at, period, threshold, budget_usd, spent_usd, "
            "consumption_pct FROM budget_milestones WHERE vendor='codex' "
            "ORDER BY period_start_at DESC, threshold DESC LIMIT ?",
            (SOURCE_HISTORY_LIMIT,),
        ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple({
        "period_start_at": period_start_at,
        "period": period,
        "threshold": threshold,
        "budget_usd": budget_usd,
        "spent_usd": spent_usd,
        "consumption_pct": consumption_pct,
    } for period_start_at, period, threshold, budget_usd, spent_usd, consumption_pct in rows)


def _projected_budget_wire(stats_conn: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    try:
        rows = stats_conn.execute(
            "SELECT period, threshold, projected_value, denominator, crossed_at_utc, alerted_at "
            "FROM projected_milestones WHERE metric='codex_budget_usd' "
            "ORDER BY crossed_at_utc DESC, threshold DESC LIMIT ?",
            (SOURCE_HISTORY_LIMIT,),
        ).fetchall()
    except sqlite3.Error:
        return ()
    return tuple({
        "period": period,
        "threshold": threshold,
        "projected_value": projected_value,
        "denominator": denominator,
        "crossed_at": crossed_at,
        "alerted_at": alerted_at,
    } for period, threshold, projected_value, denominator, crossed_at, alerted_at in rows)


def _configured_codex_budget_status(
    context: DashboardReadContext,
    entries: Iterable[object],
    *,
    cost_events: tuple[tuple[dt.datetime, float], ...] | None = None,
) -> dict[str, object] | None:
    """Compute the live configured Codex budget from the coordinated entries.

    Durable milestone rows are alert history, not the current budget status.
    This reuses the CLI's calendar-window and ``BudgetInputs``/status kernels
    while deliberately keeping the accounting read on the caller-owned cache
    snapshot.
    """
    config = context.codex_budget
    if config is None:
        return None
    c = sys.modules["cctally"]
    period, start_at, end_at = _configured_codex_budget_window(context)

    resolved_events = cost_events if cost_events is not None else _codex_budget_cost_events(
        context, entries,
    )

    def _sum_cost(start: dt.datetime, end: dt.datetime) -> float:
        return sum(cost for timestamp, cost in resolved_events if start <= timestamp < end)

    recent_start = max(start_at, context.now_utc - dt.timedelta(hours=24))
    inputs = c.BudgetInputs(
        target_usd=float(config["amount_usd"]),
        spent_usd=_sum_cost(start_at, context.now_utc),
        recent_24h_usd=_sum_cost(recent_start, context.now_utc),
        week_start_at=start_at,
        week_end_at=end_at,
        now=context.now_utc,
        alert_thresholds=tuple(config["alert_thresholds"]),
    )
    status = c.compute_budget_status(inputs)
    return {
        "period": period,
        "budget_usd": inputs.target_usd,
        "spent_usd": status.spent_usd,
        "remaining_usd": status.remaining_usd,
        "consumption_pct": status.consumption_pct,
        "verdict": status.verdict,
        "low_confidence": status.low_confidence,
        "window_start_at": start_at.astimezone(UTC).isoformat(),
        "window_end_at": end_at.astimezone(UTC).isoformat(),
        "recent_24h_usd": inputs.recent_24h_usd,
        "alert_thresholds": inputs.alert_thresholds,
        "pace": {
            "daily_usd": status.daily_pace_usd,
            "projected_low_usd": status.projected_eow_low_usd,
            "projected_high_usd": status.projected_eow_high_usd,
            "week_avg_projection_usd": status.week_avg_projection_usd,
        },
    }


def _configured_codex_budget_window(
    context: DashboardReadContext,
) -> tuple[str, dt.datetime, dt.datetime]:
    """Resolve the configured Codex accounting window through the CLI kernel."""
    config = context.codex_budget
    if config is None:
        raise ValueError("Codex budget is not configured")
    c = sys.modules["cctally"]
    period = str(config["period"])
    tz = ZoneInfo(context.display_tz_name) if context.display_tz_name else None
    forecast = c._load_sibling("_cctally_forecast")
    start_at, end_at = forecast._resolve_calendar_window(
        period,
        context.now_utc,
        {"collector": {"week_start": context.week_start_name}},
        tz,
    )
    return period, start_at.astimezone(UTC), end_at.astimezone(UTC)


def _quota_read_model(
    context: DashboardReadContext,
    observations: Iterable[object],
) -> dict[str, object]:
    """Use S2's pure history/block/forecast kernels over cache evidence."""
    quota_observations = tuple(observations)
    histories = build_history(quota_observations)
    blocks = build_blocks(quota_observations)
    history_rows: list[dict[str, object]] = []
    milestone_rows: list[dict[str, object]] = []
    active_rows: list[dict[str, object]] = []
    for history in histories:
        identity = history.identity
        key_parts = (
            identity.source_root_key,
            identity.logical_limit_key,
            identity.observed_slot,
            identity.window_minutes,
        )
        baseline = select_baseline(history.observations, context.now_utc)
        freshness = quota_freshness(history.physical_observations, context.now_utc)
        forecast = forecast_quota(history.physical_observations, context.now_utc)
        history_rows.append({
            "key": dashboard_resource_key("quota", "codex", *key_parts),
            "source": "codex",
            "label": identity.limit_name or "Codex quota",
            "observed_slot": identity.observed_slot,
            "window_minutes": identity.window_minutes,
            "current_percent": baseline.used_percent if baseline is not None else None,
            "captured_at": (
                freshness.captured_at.astimezone(UTC).isoformat()
                if freshness.captured_at is not None else None
            ),
            "freshness": freshness.state,
            "stale_after_seconds": freshness.stale_after_seconds,
            "forecast": {
                "status": forecast.status,
                "current_percent": forecast.current_percent,
                "rate_percent_per_hour": forecast.rate_percent_per_hour,
                "projected_percent": forecast.projected_percent,
                "resets_at": forecast.resets_at.astimezone(UTC).isoformat() if forecast.resets_at else None,
                "remaining_seconds": forecast.remaining_seconds,
                "sample_count": forecast.sample_count,
                "sample_span_seconds": forecast.sample_span_seconds,
                "confidence": forecast.confidence,
            },
        })
        if baseline is not None and baseline.resets_at > context.now_utc:
            active_rows.append({
                "key": dashboard_resource_key("quota", "codex", *key_parts),
                "current_percent": baseline.used_percent,
                "captured_at": baseline.captured_at.astimezone(UTC).isoformat(),
                "resets_at": baseline.resets_at.astimezone(UTC).isoformat(),
                "freshness": freshness.state,
                "stale_after_seconds": freshness.stale_after_seconds,
            })
    for block in blocks:
        identity = block.identity
        block_parts = (
            identity.source_root_key,
            identity.logical_limit_key,
            identity.observed_slot,
            identity.window_minutes,
            block.resets_at.astimezone(UTC).isoformat(),
        )
        for milestone in percent_milestones(block):
            milestone_rows.append({
                "key": dashboard_resource_key(
                    "quota_milestone", "codex", *block_parts,
                    milestone.percent, milestone.captured_at.astimezone(UTC).isoformat(),
                ),
                "source": "codex",
                "block_key": dashboard_resource_key("block", "codex", *block_parts),
                "percent": milestone.percent,
                "captured_at": milestone.captured_at.astimezone(UTC).isoformat(),
            })
    latest_percent = max(
        (float(row["current_percent"]) for row in active_rows), default=None,
    )
    active_freshness = (
        "fresh" if active_rows and all(row["freshness"] == "fresh" for row in active_rows)
        else ("unavailable" if not active_rows else "stale")
    )
    # Active identities are presentation-critical.  Keep them ahead of
    # inactive retained history before enforcing the public cardinality cap.
    active_keys = {str(row["key"]) for row in active_rows}
    history_rows.sort(key=lambda row: (str(row["key"]) not in active_keys, str(row["key"])))
    history_rows = history_rows[:SOURCE_HISTORY_LIMIT]
    milestone_rows.sort(key=lambda row: str(row["captured_at"]), reverse=True)
    milestone_rows = milestone_rows[:SOURCE_HISTORY_LIMIT]
    active_rows = active_rows[:SOURCE_HISTORY_LIMIT]
    return {
        "summary": {
            "window_count": len(blocks),
            "active_window_count": len(active_rows),
            "latest_percent": latest_percent,
            "freshness": active_freshness,
            "active": tuple(active_rows),
        },
        "histories": tuple(history_rows),
        "milestones": tuple(milestone_rows),
    }


def _clock_freshness(
    captured_at: object,
    stale_after: object,
    now_utc: dt.datetime,
) -> str:
    if not isinstance(captured_at, str) or not isinstance(stale_after, int):
        return "unavailable"
    try:
        captured = dt.datetime.fromisoformat(captured_at.replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return "unavailable"
    age_seconds = int((now_utc - captured).total_seconds())
    if age_seconds < -300:
        return "future"
    return "stale" if age_seconds > stale_after else "fresh"


def _refresh_budget_status_clock(
    status: Mapping[str, object] | None,
    now_utc: dt.datetime,
    *,
    cost_events: object = (),
) -> dict[str, object] | None:
    """Re-run only the pure pace kernel from already-published budget facts."""
    if status is None:
        return None
    try:
        c = sys.modules["cctally"]
        start_at = dt.datetime.fromisoformat(
            str(status["window_start_at"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        end_at = dt.datetime.fromisoformat(
            str(status["window_end_at"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        recent_start = max(start_at, now_utc - dt.timedelta(hours=24))
        recent_24h_usd = sum(
            float(cost) for timestamp, cost in cost_events
            if isinstance(timestamp, dt.datetime)
            and start_at <= timestamp.astimezone(UTC) < now_utc
            and timestamp.astimezone(UTC) >= recent_start
        )
        inputs = c.BudgetInputs(
            target_usd=float(status["budget_usd"]),
            spent_usd=float(status["spent_usd"]),
            recent_24h_usd=recent_24h_usd,
            week_start_at=start_at,
            week_end_at=end_at,
            now=now_utc,
            alert_thresholds=tuple(int(value) for value in status["alert_thresholds"]),
        )
        refreshed = c.compute_budget_status(inputs)
    except (KeyError, TypeError, ValueError, OverflowError):
        return dict(status)
    return {
        **dict(status),
        "recent_24h_usd": inputs.recent_24h_usd,
        "remaining_usd": refreshed.remaining_usd,
        "consumption_pct": refreshed.consumption_pct,
        "verdict": refreshed.verdict,
        "low_confidence": refreshed.low_confidence,
        "pace": {
            "daily_usd": refreshed.daily_pace_usd,
            "projected_low_usd": refreshed.projected_eow_low_usd,
            "projected_high_usd": refreshed.projected_eow_high_usd,
            "week_avg_projection_usd": refreshed.week_avg_projection_usd,
        },
    }


def refresh_codex_source_clock(
    state: SourceDashboardState,
    *,
    now_utc: dt.datetime,
) -> SourceDashboardState:
    """Refresh idle-only freshness/pace from frozen facts without provider I/O."""
    if state.source != "codex" or not isinstance(state.data, Mapping):
        return state
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    now_utc = now_utc.astimezone(UTC)
    # Structural copy only: untouched period/session/project branches remain
    # the exact frozen objects from the prior publication.  Idle refresh must
    # not walk or re-freeze the provider's heavy read model.
    data = dict(state.data)
    quota = data.get("quota")
    quota_changed = False
    if isinstance(quota, Mapping):
        quota = dict(quota)
        refreshed_histories: list[dict[str, object]] = []
        active_rows: list[dict[str, object]] = []
        for raw_history in quota.get("histories", ()):
            if not isinstance(raw_history, Mapping):
                continue
            history = dict(raw_history)
            freshness = _clock_freshness(
                history.get("captured_at"), history.get("stale_after_seconds"), now_utc,
            )
            history["freshness"] = freshness
            forecast = history.get("forecast")
            if isinstance(forecast, Mapping):
                forecast = dict(forecast)
                resets_at = forecast.get("resets_at")
                try:
                    reset = dt.datetime.fromisoformat(
                        str(resets_at).replace("Z", "+00:00")
                    ).astimezone(UTC)
                except (TypeError, ValueError):
                    reset = None
                remaining = max(0, int((reset - now_utc).total_seconds())) if reset else None
                forecast["remaining_seconds"] = remaining
                sample_count = int(forecast.get("sample_count") or 0)
                if freshness == "future":
                    forecast["status"] = "future"
                elif freshness == "stale":
                    forecast["status"] = "stale"
                elif sample_count == 0:
                    forecast["status"] = "insufficient-history"
                else:
                    forecast["status"] = "ok"
                rate = forecast.get("rate_percent_per_hour")
                current = forecast.get("current_percent")
                if (
                    isinstance(rate, (int, float)) and not isinstance(rate, bool)
                    and isinstance(current, (int, float)) and not isinstance(current, bool)
                    and remaining is not None
                ):
                    forecast["projected_percent"] = min(
                        100.0, max(float(current), float(current) + float(rate) * remaining / 3600),
                    )
                history["forecast"] = forecast
                if reset is not None and reset > now_utc and current is not None:
                    active_rows.append({
                        "key": history.get("key"),
                        "current_percent": current,
                        "captured_at": history.get("captured_at"),
                        "resets_at": resets_at,
                        "freshness": freshness,
                        "stale_after_seconds": history.get("stale_after_seconds"),
                    })
            refreshed_histories.append(history)
        quota["histories"] = refreshed_histories
        latest_percent = max(
            (float(row["current_percent"]) for row in active_rows), default=None,
        )
        summary = dict(quota.get("summary") or {})
        prior_active = summary.get("active")
        if isinstance(prior_active, (tuple, list)):
            active_order = {
                str(row.get("key")): index
                for index, row in enumerate(prior_active)
                if isinstance(row, Mapping)
            }
            active_rows.sort(
                key=lambda row: active_order.get(str(row.get("key")), len(active_order)),
            )
        summary.update({
            "active_window_count": len(active_rows),
            "latest_percent": latest_percent,
            "freshness": (
                "fresh" if active_rows and all(row["freshness"] == "fresh" for row in active_rows)
                else ("unavailable" if not active_rows else "stale")
            ),
            "active": active_rows,
        })
        quota["summary"] = summary
        data["quota"] = quota
        quota_changed = bool(refreshed_histories)
    budget_domain = data.get("budget")
    budget_changed = False
    if isinstance(budget_domain, Mapping):
        budget_domain = dict(budget_domain)
        refreshed_budget = _refresh_budget_status_clock(
            budget_domain.get("status") if isinstance(budget_domain.get("status"), Mapping) else None,
            now_utc,
            cost_events=(
                state.clock_data.get("codex_budget_cost_events", ())
                if isinstance(state.clock_data, Mapping) else ()
            ),
        )
        if refreshed_budget is not None:
            budget_domain["status"] = refreshed_budget
            data["budget"] = budget_domain
            hero = data.get("hero")
            if isinstance(hero, Mapping):
                hero = dict(hero)
                hero["budget"] = refreshed_budget
                data["hero"] = hero
            budget_changed = True
    if not (quota_changed or budget_changed):
        return state
    refreshed_state = SourceDashboardState(
        source=state.source,
        availability=state.availability,
        freshness=state.freshness,
        warnings=state.warnings,
        data_version=state.data_version,
        last_success_at=state.last_success_at,
        capabilities=state.capabilities,
        data=data,
        clock_data=state.clock_data,
    )
    return state if refreshed_state.data == state.data else refreshed_state


def _alerts_wire(stats_conn: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    """Return only safe, source-owned Codex alert context in newest-first order."""
    rows: list[dict[str, object]] = []
    try:
        for period, threshold, consumption_pct, crossed_at in stats_conn.execute(
            "SELECT period, threshold, consumption_pct, crossed_at_utc "
            "FROM budget_milestones WHERE vendor='codex' AND alerted_at IS NOT NULL "
            "ORDER BY crossed_at_utc DESC, threshold DESC LIMIT ?",
            (SOURCE_HISTORY_LIMIT,),
        ):
            rows.append({
                "key": dashboard_resource_key("alert", "codex", "codex_budget", period, threshold, crossed_at),
                "source": "codex",
                "axis": "codex_budget", "period": period, "threshold": threshold,
                "value": consumption_pct, "created_at": crossed_at,
            })
        for period, threshold, projected_value, crossed_at in stats_conn.execute(
            "SELECT period, threshold, projected_value, crossed_at_utc "
            "FROM projected_milestones WHERE metric='codex_budget_usd' AND alerted_at IS NOT NULL "
            "ORDER BY crossed_at_utc DESC, threshold DESC LIMIT ?",
            (SOURCE_HISTORY_LIMIT,),
        ):
            rows.append({
                "key": dashboard_resource_key("alert", "codex", "projected", period, threshold, crossed_at),
                "source": "codex",
                "axis": "projected", "period": period, "threshold": threshold,
                "value": projected_value, "created_at": crossed_at,
            })
        for root_key, logical_key, observed_slot, window_minutes, resets_at, threshold, severity, created_at in stats_conn.execute(
            "SELECT source_root_key, logical_limit_key, observed_slot, window_minutes, resets_at_utc, "
            "threshold, severity, created_at_utc FROM quota_threshold_events "
            "WHERE source='codex' AND disposition='alerted' AND orphaned_at IS NULL "
            "ORDER BY created_at_utc DESC, source_root_key, logical_limit_key, observed_slot, threshold "
            "LIMIT ?",
            (SOURCE_HISTORY_LIMIT,),
        ):
            rows.append({
                "key": dashboard_resource_key(
                    "alert", "codex", "quota", root_key, logical_key, observed_slot,
                    window_minutes, resets_at, threshold, created_at,
                ),
                "source": "codex",
                "axis": "quota", "threshold": threshold, "severity": severity,
                "created_at": created_at,
            })
    except sqlite3.Error:
        return ()
    return tuple(sorted(
        rows,
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )[:SOURCE_HISTORY_LIMIT])


def _projects_wire(
    context: DashboardReadContext,
    quota_observations: Iterable[object],
    entries: Iterable[object],
    *,
    accounting_end: dt.datetime,
) -> dict[str, object]:
    """Adapt S3's already-qualified attribution result without re-formulas."""
    qualified_entries = tuple(entries)
    result = build_codex_project_result(
        qualified_entries,
        range_start=context.range_start,
        range_end=accounting_end,
        blocks=build_blocks(quota_observations),
        as_of=context.now_utc,
        allocation_entries=qualified_entries,
    )
    data = result.data
    if data is None:
        return {"rows": (), "total_cost_usd": 0.0, "total_tokens": 0}
    return {
        "rows": tuple({
            "key": dashboard_resource_key("project", "codex", row.project_key),
            "source": "codex",
            "label": row.display_label,
            "session_count": row.session_count,
            "first_seen": row.first_seen.astimezone(UTC).isoformat(),
            "last_seen": row.last_seen.astimezone(UTC).isoformat(),
            "cost_usd": row.totals.cost_usd,
            "input_tokens": row.totals.input_tokens,
            "cached_input_tokens": row.totals.cached_input_tokens,
            "output_tokens": row.totals.output_tokens,
            "reasoning_output_tokens": row.totals.reasoning_output_tokens,
            "total_tokens": row.totals.total_tokens,
        } for row in data.projects),
        "total_cost_usd": data.totals.cost_usd,
        "total_tokens": data.totals.total_tokens,
    }


def _codex_entries_from_qualified(entries: Iterable[object]) -> list[CodexEntry]:
    """Adapt the one root-qualified accounting read for shipped view kernels."""
    converted: list[CodexEntry] = []
    for entry in entries:
        source_path = str(getattr(entry, "source_path", "") or "")
        session_id = str(getattr(entry, "session_id", "") or "")
        if not source_path or not session_id:
            raise SourceCapabilityUnavailable("qualified accounting lacks session identity")
        converted.append(CodexEntry(
            timestamp=getattr(entry, "timestamp"),
            session_id=session_id,
            model=str(getattr(entry, "model")),
            input_tokens=int(getattr(entry, "input_tokens")),
            cached_input_tokens=int(getattr(entry, "cached_input_tokens")),
            output_tokens=int(getattr(entry, "output_tokens")),
            reasoning_output_tokens=int(getattr(entry, "reasoning_output_tokens")),
            total_tokens=int(getattr(entry, "total_tokens")),
            source_path=source_path,
        ))
    return converted


def build_codex_source_state(
    context: DashboardReadContext,
    *,
    data_version: str,
) -> SourceDashboardState:
    """Build Codex data strictly from the coordinated cache/stats reads.

    No sync, rollout scan, CLI parser, or fallback is reachable from this
    adapter.  Period and session arithmetic remains delegated to the shipped
    S3 view kernels, preserving the CLI's inclusive-token vocabulary.
    """
    active_roots = tuple(sorted(
        str(row[0]) for row in context.cache_conn.execute(
            "SELECT source_root_key FROM codex_source_roots"
        )
    ))
    quota_observations = load_codex_quota_observations(
        source_root_keys=active_roots,
        cache_conn=context.cache_conn,
        captured_at_or_after=(
            context.now_utc - dt.timedelta(days=DASHBOARD_QUOTA_RECENT_DAYS)
        ),
        active_at=context.now_utc,
        max_rows=DASHBOARD_QUOTA_OBSERVATION_LIMIT,
    )
    coherence = codex_projection_coherence(
        context,
    )
    if not coherence.coherent:
        raise CodexProjectionIncoherent(coherence.reason)
    # The cache reader's established report surface treats the ``now`` instant
    # as inclusive.  The qualified adapter is half-open, so extend only its
    # query/result boundary by one microsecond and keep all live budget sums
    # explicitly half-open at ``now`` below.
    accounting_end = context.now_utc + dt.timedelta(microseconds=1)
    accounting_start = context.range_start
    if context.codex_budget is not None:
        _period, budget_start, _budget_end = _configured_codex_budget_window(context)
        accounting_start = min(accounting_start, budget_start)
    qualified_entries = load_qualified_codex_entries(
        accounting_start,
        accounting_end,
        speed=context.speed,
        sync=False,
        cache_conn=context.cache_conn,
    )
    budget_entries = _codex_entries_from_qualified(qualified_entries)
    visible_qualified_entries = tuple(
        entry for entry in qualified_entries
        if context.range_start <= getattr(entry, "timestamp").astimezone(UTC) < accounting_end
    )
    entries = _codex_entries_from_qualified(visible_qualified_entries)
    daily = build_codex_daily_view(
        entries, now_utc=context.now_utc, tz_name=context.display_tz_name, speed=context.speed,
    )
    monthly = build_codex_monthly_view(
        entries, now_utc=context.now_utc, tz_name=context.display_tz_name, speed=context.speed,
    )
    weekly = build_codex_weekly_view(
        entries,
        now_utc=context.now_utc,
        tz_name=context.display_tz_name,
        week_start_idx=context.week_start_idx,
        speed=context.speed,
    )
    sessions = build_codex_session_view(
        entries, now_utc=context.now_utc, tz_name=context.display_tz_name, speed=context.speed,
    )
    quota = _quota_read_model(context, quota_observations)
    quota_blocks = _quota_wire(context.stats_conn)
    quota = {**quota, "blocks": quota_blocks}
    budget_rows = _budget_wire(context.stats_conn)
    projected_budget_rows = _projected_budget_wire(context.stats_conn)
    budget_cost_events = _codex_budget_cost_events(context, budget_entries)
    configured_budget = _configured_codex_budget_status(
        context, budget_entries, cost_events=budget_cost_events,
    )
    projects = _projects_wire(
        context,
        quota_observations,
        visible_qualified_entries,
        accounting_end=accounting_end,
    )
    alerts = _alerts_wire(context.stats_conn)
    availability = "ok" if (entries or quota_blocks or budget_rows) else "empty"
    total_input = sum(entry.input_tokens for entry in entries)
    total_cached = sum(entry.cached_input_tokens for entry in entries)
    total_output = sum(entry.output_tokens for entry in entries)
    total_reasoning = sum(entry.reasoning_output_tokens for entry in entries)
    return SourceDashboardState(
        source="codex",
        availability=availability,
        freshness="fresh",
        warnings=(),
        data_version=data_version,
        last_success_at=context.now_utc,
        capabilities={
            "hero": CapabilityRecord("supported", "calendar-accounting"),
            "daily": CapabilityRecord("supported", "calendar-day"),
            "monthly": CapabilityRecord("supported", "calendar-month"),
            "weekly": CapabilityRecord("supported", "calendar-week"),
            "sessions": CapabilityRecord("supported", "inclusive-input-tokens"),
            "forensics": CapabilityRecord("supported", "inclusive-input-token-reuse"),
            "quota": CapabilityRecord("derived", "native-windows"),
            "budget": CapabilityRecord("supported", "calendar-period"),
            "projects": CapabilityRecord("supported", "qualified-attribution"),
            "alerts": CapabilityRecord("supported", "provider-native"),
        },
        data={
            "hero": {
                "cost_usd": daily.total_cost_usd,
                "input_tokens": total_input,
                "cached_input_tokens": total_cached,
                "output_tokens": total_output,
                "reasoning_output_tokens": total_reasoning,
                "total_tokens": daily.total_tokens,
                "quota": quota["summary"],
                "budget": configured_budget,
                "alerts": {"count": len(alerts)},
            },
            "periods": {
                "daily": _period_wire(daily),
                "monthly": _period_wire(monthly),
                "weekly": _period_wire(weekly),
            },
            "sessions": _session_wire(sessions),
            "quota": quota,
            "budget": {
                "status": configured_budget,
                "milestones": budget_rows,
                "projected": projected_budget_rows,
            },
            "projects": projects,
            "alerts": {"rows": alerts},
        },
        clock_data={"codex_budget_cost_events": budget_cost_events},
    )
