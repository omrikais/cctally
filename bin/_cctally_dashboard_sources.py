"""Dashboard-only, cache-backed provider read models for #294 S4."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType, SimpleNamespace
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from _cctally_core import get_week_start_name
from _cctally_quota import (
    codex_five_hour_percent_at_crossing,
    codex_quota_breakdown,
    codex_physical_mutation_seq,
    load_codex_quota_observations,
    load_codex_quota_projection_certificate,
)
from _cctally_source_analytics import (
    QualifiedMetadataUnavailable,
    has_cached_codex_accounting_entries,
    load_cached_rooted_codex_accounting_entries,
    load_codex_project_metadata_health,
    load_qualified_codex_entries,
)
import _lib_log
import _lib_accounts
from _lib_dashboard_sources import (
    CapabilityRecord,
    ProjectionCoherence,
    SourceDashboardState,
    SourceDashboardWarning,
    assess_codex_projection_coherence,
    dashboard_resource_key,
)
from _lib_quota import (
    QuotaWindowIdentity,
    build_blocks,
    build_history,
    forecast_quota,
    percent_milestones,
    quota_freshness,
    select_baseline,
)
from _lib_jsonl import CodexEntry, codex_model_scoped_quota_pool
from _lib_fmt import stable_sum
from _lib_aggregators import _aggregate_codex_buckets
from _lib_five_hour import _FIVE_HOUR_JITTER_FLOOR_SECONDS
from _lib_source_analytics import (
    build_codex_project_result,
    collision_safe_project_label_map,
)
from _lib_view_models import (
    CodexWeeklyView,
    build_codex_daily_view,
    build_codex_monthly_view,
    build_rooted_codex_session_view,
    build_codex_session_view,
)


UTC = dt.timezone.utc
SOURCE_HISTORY_LIMIT = 250
DASHBOARD_QUOTA_OBSERVATION_LIMIT = 1000
DASHBOARD_QUOTA_RECENT_DAYS = 35


def accounts_identity_digest(stats_conn: sqlite3.Connection) -> str:
    """Digest of the account registry + the providers' live active-account state
    (#341 spec §4 finding 9 — the identity-only invalidation signal).

    Empty when NO account has ever been observed (the ``accounts`` registry is
    empty): every <=1-account install and every pre-accounts fixture is
    byte-neutral (the caller never appends an empty digest to a version/idle
    signature), and no identity-file read happens on the idle path. Once accounts
    exist it folds together (a) each registry row's ``account_key`` / provider /
    label / label_source — catching a new account observed or a label edit — and
    (b) the CURRENTLY-active account keys (``resolve_active_account_keys`` reads
    ``~/.claude.json`` + each Codex root's ``auth.json``, stable-read + best
    effort) — catching an in-place account SWITCH with zero new ingested rows.
    Folded into both the outer dispatch/idle signature and each physical source's
    ``data_version``, so a switch rebuilds the source state (flipping the
    ``active`` marker) on the very next tick without any new rows.
    """
    try:
        row = stats_conn.execute("SELECT COUNT(*) FROM accounts").fetchone()
    except sqlite3.Error:
        return ""
    if not row or not row[0]:
        return ""  # no account ever observed -> byte-stable, no identity I/O
    parts: list[str] = []
    try:
        for r in stats_conn.execute(
            "SELECT account_key, provider, label, label_source FROM accounts "
            "ORDER BY provider, account_key"
        ):
            parts.append(f"{r[0]}|{r[1]}|{r[2] or ''}|{r[3] or ''}")
    except sqlite3.Error:
        pass
    try:
        import _cctally_account
        active = sorted(_cctally_account.resolve_active_account_keys())
    except Exception:
        active = []
    parts.append("active\x1f" + ",".join(active))
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


class CodexCycleUnavailable(RuntimeError):
    """No single active native seven-day boundary can bound hero accounting."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class SourceCapabilityUnavailable(ValueError):
    """A source is not a physical owner or cannot serve a resource domain."""


class SourceResourceNotFound(LookupError):
    """A valid opaque resource key has no row in its provider state."""


@dataclass(frozen=True)
class CodexCycleBoundary:
    """The one active native subscription cycle usable for hero accounting."""

    window_minutes: int
    start_at: dt.datetime
    resets_at: dt.datetime
    # Root provenance is server-only accounting input, never public wire data.
    source_root_keys: tuple[str, ...]
    used_percent: float | None = None
    # Exact server-side quota identity selected for the hero. It is never
    # serialized; milestone-history keys hash it opaquely.
    quota_identity: QuotaWindowIdentity | None = None


@dataclass(frozen=True)
class CodexWeeklyPeriod:
    """One non-overlapping observed native seven-day quota cycle segment."""

    start_at: dt.datetime
    end_at: dt.datetime
    source_root_keys: tuple[str, ...]
    used_percent: float | None = None


def _is_model_scoped_codex_quota(logical_limit_key: object) -> bool:
    """Whether an interpreted native identity belongs outside standard quota."""
    if not isinstance(logical_limit_key, str):
        return False
    try:
        payload = json.loads(logical_limit_key)
    except (json.JSONDecodeError, TypeError):
        return False
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("modelPool"), str)
        and bool(payload["modelPool"].strip())
    )


def _resolve_codex_weekly_cycle(
    observations: Iterable[object],
    now_utc: dt.datetime,
) -> list[CodexCycleBoundary]:
    """Resolve one active 10,080-minute native cycle PER ACCOUNT (#341 spec §4).

    Returns a list with one :class:`CodexCycleBoundary` per account that has
    exactly one active weekly boundary — N simultaneously-active account cycles
    are each valid instead of collapsing to ``CodexCycleUnavailable("conflicting")``.
    ``conflicting`` now fires only for a genuine conflict WITHIN one account.
    Raises ``CodexCycleUnavailable`` only when NO account yields a live cycle. A
    single-account install returns a 1-element list = today's boundary
    byte-for-byte (the hero path is unchanged, spec R8).
    """
    per_account: dict[str, dict[tuple[int, dt.datetime], list[tuple[object, object]]]] = {}
    accounts_seen: set[str] = set()
    stale_by_account: dict[str, bool] = {}
    for history in build_history(tuple(observations)):
        if history.identity.window_minutes != 10_080:
            continue
        if _is_model_scoped_codex_quota(history.identity.logical_limit_key):
            continue
        account = history.identity.account_key
        accounts_seen.add(account)
        baseline = select_baseline(history.observations, now_utc)
        if baseline is None or baseline.resets_at <= now_utc:
            continue
        if quota_freshness(history.physical_observations, now_utc).state != "fresh":
            stale_by_account[account] = True
            continue
        boundary = (history.identity.window_minutes, baseline.resets_at)
        per_account.setdefault(account, {}).setdefault(boundary, []).append((history, baseline))
    cycles: list[CodexCycleBoundary] = []
    reasons: list[str] = []
    for account in sorted(accounts_seen | set(per_account)):
        boundaries = per_account.get(account, {})
        if len(boundaries) != 1:
            # Within one account: 0 boundaries -> stale/missing; >=2 -> conflicting.
            reasons.append(
                "conflicting" if boundaries
                else ("stale" if stale_by_account.get(account) else "missing")
            )
            continue
        (window_minutes, resets_at), candidates = next(iter(boundaries.items()))
        # Preserve the existing hero's max-used-percent choice, then pin every
        # remaining tie deterministically. The selected full identity—not the
        # union of sibling roots/slots/limits—owns the account's hero cycle.
        history, baseline = max(
            candidates,
            key=lambda item: (
                float(item[1].used_percent),
                item[1].captured_at.astimezone(UTC),
                item[0].identity.source_root_key,
                item[0].identity.logical_limit_key,
                item[0].identity.observed_slot,
            ),
        )
        selected_identity = history.identity
        cycles.append(CodexCycleBoundary(
            window_minutes=window_minutes,
            start_at=resets_at - dt.timedelta(minutes=window_minutes),
            resets_at=resets_at,
            source_root_keys=(selected_identity.source_root_key,),
            used_percent=float(baseline.used_percent),
            quota_identity=selected_identity,
        ))
    if not cycles:
        # Aggregate reason: for a single account this is exactly the old reason
        # (byte-stable); across accounts, conflicting > stale > missing.
        if "conflicting" in reasons:
            raise CodexCycleUnavailable("conflicting")
        if "stale" in reasons:
            raise CodexCycleUnavailable("stale")
        raise CodexCycleUnavailable("missing")
    return cycles


def _codex_weekly_periods(
    stats_conn: sqlite3.Connection,
    *,
    source_root_keys: Iterable[str],
    active_cycle: CodexCycleBoundary | None,
) -> tuple[CodexWeeklyPeriod, ...]:
    """Read durable 10,080-minute boundaries and clip early re-anchors.

    A provider-granted reset changes the native window's nominal start before
    the prior seven-day deadline.  Sorting those nominal starts and ending the
    prior segment at the next start preserves the actual quota-cycle boundary
    without double-counting the overlapping nominal windows.
    """
    roots = tuple(sorted({
        root for root in source_root_keys if isinstance(root, str) and root
    }))
    if not roots:
        return ()
    placeholders = ",".join("?" for _ in roots)
    try:
        rows = stats_conn.execute(
            "SELECT source_root_key, logical_limit_key, resets_at_utc, "
            "nominal_start_at_utc, current_percent "
            "FROM quota_window_blocks "
            "WHERE source='codex' AND window_minutes=10080 "
            f"AND source_root_key IN ({placeholders}) AND orphaned_at IS NULL "
            "ORDER BY nominal_start_at_utc DESC, resets_at_utc DESC, source_root_key "
            "LIMIT ?",
            (*roots, SOURCE_HISTORY_LIMIT),
        ).fetchall()
    except sqlite3.Error:
        rows = ()

    raw_boundaries: list[tuple[dt.datetime, dt.datetime, set[str], list[float]]] = []

    for root_key, logical_limit_key, resets_at_raw, start_at_raw, current_percent in rows:
        if _is_model_scoped_codex_quota(logical_limit_key):
            continue
        try:
            start_at = dt.datetime.fromisoformat(str(start_at_raw).replace("Z", "+00:00"))
            resets_at = dt.datetime.fromisoformat(str(resets_at_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if start_at.tzinfo is None or resets_at.tzinfo is None:
            continue
        start_at = start_at.astimezone(UTC)
        resets_at = resets_at.astimezone(UTC)
        if resets_at <= start_at:
            continue
        used_values = []
        if isinstance(current_percent, (int, float)) and not isinstance(current_percent, bool):
            used_values.append(float(current_percent))
        raw_boundaries.append((start_at, resets_at, {str(root_key)}, used_values))

    if active_cycle is not None:
        raw_boundaries.append((
            active_cycle.start_at.astimezone(UTC),
            active_cycle.resets_at.astimezone(UTC),
            set(active_cycle.source_root_keys),
            [active_cycle.used_percent] if active_cycle.used_percent is not None else [],
        ))

    ordered: list[tuple[dt.datetime, dt.datetime, set[str], list[float]]] = []
    for start_at, resets_at, period_roots, used_values in sorted(
        raw_boundaries, key=lambda item: (item[0], item[1]),
    ):
        if (
            ordered
            and (start_at - ordered[-1][0]).total_seconds()
            < _FIVE_HOUR_JITTER_FLOOR_SECONDS
        ):
            first_start, latest_reset, existing_roots, existing_used = ordered[-1]
            existing_roots.update(period_roots)
            existing_used.extend(used_values)
            ordered[-1] = (
                first_start, max(latest_reset, resets_at), existing_roots, existing_used,
            )
        else:
            ordered.append((start_at, resets_at, set(period_roots), list(used_values)))
    periods: list[CodexWeeklyPeriod] = []
    for index, (start_at, resets_at, period_roots, used_values) in enumerate(ordered):
        next_start = ordered[index + 1][0] if index + 1 < len(ordered) else None
        end_at = min(resets_at, next_start) if next_start is not None else resets_at
        if end_at <= start_at:
            continue
        periods.append(CodexWeeklyPeriod(
            start_at=start_at,
            end_at=end_at,
            source_root_keys=tuple(sorted(period_roots)),
            used_percent=max(used_values) if used_values else None,
        ))
    return tuple(periods)


def _native_limit_label(limit_name: object, window_minutes: object) -> str:
    """Prefer provider label text, deriving duration copy only when absent."""
    if isinstance(limit_name, str) and limit_name.strip():
        return limit_name.strip()
    if window_minutes == 300:
        return "5-hour limit"
    if window_minutes == 10_080:
        return "7-day limit"
    if not isinstance(window_minutes, int) or isinstance(window_minutes, bool) or window_minutes <= 0:
        return "Codex quota"
    if window_minutes % 1_440 == 0:
        return f"{window_minutes // 1_440}-day limit"
    if window_minutes % 60 == 0:
        return f"{window_minutes // 60}-hour limit"
    return f"{window_minutes}-minute limit"


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
    codex_quota_actual_thresholds: tuple[int, ...]
    codex_quota_projected_thresholds: tuple[int, ...]
    cache_report_anomaly_threshold_pp: int
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
    quota_alerts = c._get_quota_alerts_config(raw_config)
    raw_cache_report = raw_config.get("cache_report")
    raw_cache_threshold = (
        raw_cache_report.get("anomaly_threshold_pp", 15)
        if isinstance(raw_cache_report, Mapping) else 15
    )
    cache_threshold = (
        int(raw_cache_threshold)
        if isinstance(raw_cache_threshold, int) and not isinstance(raw_cache_threshold, bool)
        and 1 <= raw_cache_threshold <= 100 else 15
    )
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
        "codex_quota_alerts": quota_alerts,
        "cache_report_anomaly_threshold_pp": cache_threshold,
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
        codex_quota_actual_thresholds=tuple(quota_alerts["actual_thresholds"]),
        codex_quota_projected_thresholds=tuple(quota_alerts["projected_thresholds"]),
        cache_report_anomaly_threshold_pp=cache_threshold,
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
    codex_quota_actual_thresholds: tuple[int, ...] = ()
    codex_quota_projected_thresholds: tuple[int, ...] = ()
    cache_report_anomaly_threshold_pp: int = 15

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


def source_detail_lookup(
    bundle: object, source: str, resource: str, key: str,
    account: "str | None" = None,
) -> dict[str, object]:
    """Find one provider-owned opaque row without I/O, ingest, or fallback.

    The handler has already parsed the fixed route grammar.  This adapter only
    reads the frozen bundle published by the dashboard owner thread, so a
    request cannot accidentally trigger cache sync, rollout parsing, or a
    Claude fallback for a Codex key.

    Server-side account row-ownership (#341 Task 4, spec §4 finding 10).
    ``account`` is the qualifier the modal captured with ``(source, account)`` at
    open. When it is provided AND any row matching ``key`` carries an
    ``account_key`` (the decorated wire shape — two accounts can share one opaque
    resource key), the server VERIFIES ownership: it returns the row owned by
    ``account`` and NEVER a different account's row — a key resolving only to
    another account raises ``SourceResourceNotFound`` rather than leaking it.
    Account-agnostic rows (no ``account_key``: sessions/projects per Decision R4,
    or any undecorated source) ignore the qualifier and match by key alone, so
    the undecorated path stays byte-identical.
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
    key_matches = [
        row for row in rows
        if isinstance(row, Mapping) and row.get("key") == key
    ]
    if not key_matches:
        raise SourceResourceNotFound()
    if account is not None:
        # Any key-match that carries an account_key is account-scoped: enforce
        # ownership so a fetch qualified to account X can never fall through to
        # account Y's row sharing the same opaque key.
        scoped = [row for row in key_matches if "account_key" in row]
        if scoped:
            owned = [row for row in scoped if row.get("account_key") == account]
            if not owned:
                raise SourceResourceNotFound()
            return _public_copy(owned[0])  # type: ignore[return-value]
    return _public_copy(key_matches[0])  # type: ignore[return-value]


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
    result = {
        "label": bucket.bucket,
        "cost_usd": bucket.cost_usd,
        "input_tokens": bucket.input_tokens,
        "cached_input_tokens": bucket.cached_input_tokens,
        "output_tokens": bucket.output_tokens,
        "reasoning_output_tokens": bucket.reasoning_output_tokens,
        "total_tokens": bucket.total_tokens,
        "models": tuple(bucket.models),
        "model_breakdowns": tuple(dict(row) for row in bucket.model_breakdowns),
    }
    for name in ("period_start_at", "period_end_at"):
        value = getattr(bucket, name, None)
        if isinstance(value, dt.datetime):
            result[name.replace("period_", "")] = value.astimezone(UTC).isoformat()
    for name in ("used_pct", "dollar_per_pct"):
        value = getattr(bucket, name, None)
        if value is not None:
            result[name] = value
    return result


def _period_wire(view: Any) -> dict[str, object]:
    return {
        "rows": tuple(_bucket_wire(row) for row in view.rows),
        "total_cost_usd": view.total_cost_usd,
        "total_tokens": view.total_tokens,
        "display_tz": view.display_tz_label,
    }


def _codex_cache_report_wire(
    entries: Iterable[object],
    *,
    metadata: Mapping[tuple[str, str], Mapping[str, object]],
    now_utc: dt.datetime,
    display_tz_name: str | None,
    speed: str,
    anomaly_threshold_pp: int = 15,
    window_days: int = 14,
) -> dict[str, object]:
    """Compute the canonical cache report from Codex's inclusive counters.

    Codex input is cache-inclusive, so the shared cache-report kernel receives
    uncached input plus cached input as two disjoint counters. OpenAI does not
    charge a cache-write premium; the counterfactual is therefore the exact
    uncached-vs-cached input price difference for each token-count event.
    """
    c = sys.modules["cctally"]
    crk = c._load_sibling("_lib_cache_report")
    display_tz = ZoneInfo(display_tz_name) if display_tz_name else None
    cutoff = now_utc - dt.timedelta(days=window_days)

    def _tiered_cost(tokens: int, pricing: Mapping[str, object], base: str, above: str) -> float:
        if tokens <= 0:
            return 0.0
        base_rate = float(pricing.get(base, 0.0) or 0.0)
        above_rate = pricing.get(above)
        threshold = int(c.CODEX_TIERED_THRESHOLD)
        if tokens > threshold and above_rate is not None:
            return threshold * base_rate + (tokens - threshold) * float(above_rate)
        return tokens * base_rate

    wrapped = []
    for entry in entries:
        timestamp = getattr(entry, "timestamp", None)
        if not isinstance(timestamp, dt.datetime) or timestamp < cutoff:
            continue
        model = str(getattr(entry, "model", "") or "unknown")
        input_tokens = int(getattr(entry, "input_tokens", 0))
        cached_tokens = min(input_tokens, int(getattr(entry, "cached_input_tokens", 0)))
        uncached_tokens = max(0, input_tokens - cached_tokens)
        pricing, _is_fallback = c._resolve_codex_pricing(model)
        pricing = pricing or {}
        uncached_counterfactual = _tiered_cost(
            cached_tokens, pricing,
            "input_cost_per_token", "input_cost_per_token_above_272k_tokens",
        )
        cached_actual = _tiered_cost(
            cached_tokens, pricing,
            "cache_read_input_token_cost", "cache_read_input_token_cost_above_272k_tokens",
        )
        multiplier = c._codex_fast_multiplier(model) if speed == "fast" else 1.0
        saved = max(0.0, uncached_counterfactual - cached_actual) * multiplier
        identity = (
            str(getattr(entry, "source_root_key", "") or ""),
            str(getattr(entry, "source_path", "") or ""),
        )
        item_metadata = metadata.get(identity) or {}
        project = (
            str(getattr(entry, "project_label", "") or "").strip()
            or str(item_metadata.get("project_label") or "").strip()
            or "(unknown)"
        )
        wrapped.append(SimpleNamespace(
            timestamp=timestamp,
            model=model,
            cost_usd=float(getattr(entry, "cost_usd", 0.0)),
            project_path=project,
            input_tokens=uncached_tokens,
            output_tokens=int(getattr(entry, "output_tokens", 0)),
            cache_creation_tokens=0,
            cache_read_tokens=cached_tokens,
            cache_saved_usd=saved,
            cache_wasted_usd=0.0,
            cache_net_usd=saved,
            usage={
                "input_tokens": uncached_tokens,
                "output_tokens": int(getattr(entry, "output_tokens", 0)),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cached_tokens,
            },
        ))

    today_iso = now_utc.astimezone(display_tz or UTC).strftime("%Y-%m-%d")
    if not wrapped:
        return {
            "window_days": window_days,
            "anomaly_threshold_pp": anomaly_threshold_pp,
            "anomaly_window_days": window_days,
            "today": {
                "date": today_iso, "cache_hit_percent": 0.0,
                "baseline_median_percent": None, "delta_pp": None,
                "net_usd": 0.0, "saved_usd": 0.0, "wasted_usd": 0.0,
                "anomaly_triggered": False, "anomaly_reasons": (),
                "baseline_daily_row_count": 0,
            },
            "days": (), "by_project": (), "by_model": (),
            "seven_day_net_usd": 0.0, "seven_day_anomaly_count": 0,
            "fourteen_day_counterfactual_usd": 0.0,
            "fourteen_day_efficiency_ratio": 0.0, "is_empty": True,
        }

    result = crk._build_cache_report(
        wrapped,
        now_utc=now_utc,
        window_days=window_days,
        anomaly_threshold_pp=anomaly_threshold_pp,
        anomaly_window_days=window_days,
        display_tz=display_tz,
        pricing=c.CODEX_MODEL_PRICING,
        cost_calculator=lambda _model, _usage, _mode, cost: float(cost or 0.0),
    )
    raw_rows = sorted(result.rows, key=lambda row: row.date or "", reverse=True)
    days = tuple({
        "date": row.date or "",
        "cache_hit_percent": row.cache_hit_percent,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "cache_creation_tokens": row.cache_creation_tokens,
        "cache_read_tokens": row.cache_read_tokens,
        "saved_usd": row.saved_usd,
        "wasted_usd": row.wasted_usd,
        "net_usd": row.net_usd,
        "anomaly_triggered": row.anomaly_triggered,
        "anomaly_reasons": tuple(row.anomaly_reasons),
    } for row in raw_rows[:window_days])
    today_row = next((row for row in raw_rows if row.date == today_iso), None)
    baseline_count = sum(1 for row in raw_rows if row.date != today_iso)
    baseline = result.today_baseline_median
    today_hit = today_row.cache_hit_percent if today_row else 0.0
    kept_dates = {row["date"] for row in days}
    kept_entries = [
        entry for entry in wrapped
        if entry.timestamp.astimezone(display_tz or UTC).strftime("%Y-%m-%d") in kept_dates
    ]
    by_project = crk._aggregate_cache_breakdown(
        kept_entries, key_fn=lambda entry: entry.project_path,
        pricing=c.CODEX_MODEL_PRICING,
    )
    by_model = crk._aggregate_cache_breakdown(
        kept_entries, key_fn=lambda entry: entry.model,
        pricing=c.CODEX_MODEL_PRICING,
    )
    seven = days[:7]
    saved_total = stable_sum(float(row["saved_usd"]) for row in days)
    wasted_total = stable_sum(float(row["wasted_usd"]) for row in days)
    efficiency_denom = saved_total + abs(wasted_total)
    return {
        "window_days": window_days,
        "anomaly_threshold_pp": anomaly_threshold_pp,
        "anomaly_window_days": window_days,
        "today": {
            "date": today_iso,
            "cache_hit_percent": today_hit,
            "baseline_median_percent": baseline,
            "delta_pp": today_hit - baseline if baseline is not None else None,
            "net_usd": today_row.net_usd if today_row else 0.0,
            "saved_usd": today_row.saved_usd if today_row else 0.0,
            "wasted_usd": today_row.wasted_usd if today_row else 0.0,
            "anomaly_triggered": today_row.anomaly_triggered if today_row else False,
            "anomaly_reasons": tuple(today_row.anomaly_reasons) if today_row else (),
            "baseline_daily_row_count": baseline_count,
        },
        "days": days,
        "by_project": tuple({
            "key": row.key, "cache_hit_percent": row.cache_hit_percent,
            "net_usd": row.net_usd,
        } for row in by_project),
        "by_model": tuple({
            "key": row.key, "cache_hit_percent": row.cache_hit_percent,
            "net_usd": row.net_usd,
        } for row in by_model),
        "seven_day_net_usd": stable_sum(float(row["net_usd"]) for row in seven),
        "seven_day_anomaly_count": sum(bool(row["anomaly_triggered"]) for row in seven),
        "fourteen_day_counterfactual_usd": saved_total,
        "fourteen_day_efficiency_ratio": (
            saved_total / efficiency_denom if efficiency_denom > 1e-9 else 0.0
        ),
        "is_empty": False,
    }


def _codex_conversation_metadata(
    cache_conn: sqlite3.Connection,
) -> dict[tuple[str, str], dict[str, object]]:
    """Read task short names and cached project metadata by rooted rollout.

    ``state_5.sqlite.threads.title`` is Codex's persisted user-facing task name.
    Conversation rollup titles are derived from prompt text and therefore must
    never be substituted for that name on the dashboard. Project attribution
    is derived from the compact thread ``cwd``/``git_json`` retained in
    ``cache.db`` so non-conversation panels never open ``conversations.db``.
    """
    metadata: dict[tuple[str, str], dict[str, object]] = {}
    try:
        core_rows = tuple(cache_conn.execute(
            "WITH accounting AS ("
            " SELECT source_root_key, source_path, MIN(id) AS first_id,"
            " MIN(timestamp_utc) AS started_at"
            " FROM codex_session_entries"
            " GROUP BY source_root_key, source_path"
            ") "
            "SELECT t.source_root_key, t.source_path, t.native_thread_id, "
            "e.session_id AS accounting_session_id, "
            "t.cwd, t.git_json, a.started_at, t.last_seen_utc "
            "FROM codex_conversation_threads AS t "
            "LEFT JOIN accounting AS a "
            "ON a.source_root_key=t.source_root_key AND a.source_path=t.source_path "
            "LEFT JOIN codex_session_entries AS e ON e.id=a.first_id "
            "ORDER BY t.last_seen_utc DESC, t.conversation_key DESC"
        ))
        from _cctally_cache import _codex_conversation_project_attribution
        rows = tuple(
            (
                root_key, source_path, native_thread_id, accounting_session_id,
                *_codex_conversation_project_attribution(root_key, cwd, git_json),
                first_seen_at,
            )
            for (
                root_key, source_path, native_thread_id, accounting_session_id,
                cwd, git_json, first_seen_at, _last_seen_at,
            ) in core_rows
        )
        file_aliases = tuple(cache_conn.execute(
            "SELECT f.source_root_key, f.path, f.last_native_thread_id, "
            "f.last_session_id, MIN(e.timestamp_utc) "
            "FROM codex_session_files AS f "
            "LEFT JOIN codex_session_entries AS e "
            "ON e.source_root_key=f.source_root_key AND e.source_path=f.path "
            "WHERE f.last_native_thread_id IS NOT NULL AND f.last_native_thread_id != '' "
            "GROUP BY f.source_root_key, f.path, f.last_native_thread_id, f.last_session_id "
            "ORDER BY f.last_ingested_at DESC, f.path DESC"
        ))
        native_ids = tuple(sorted({
            str(native_thread_id) for _, _, native_thread_id, *_ in rows
            if isinstance(native_thread_id, str) and native_thread_id
        } | {
            str(native_thread_id) for _, _, native_thread_id, *_ in file_aliases
            if isinstance(native_thread_id, str) and native_thread_id
        }))
        provider_roots = {
            str(root_key): pathlib.Path(root_path)
            for root_key, root_path in cache_conn.execute(
                "SELECT source_root_key, canonical_root_path FROM codex_source_roots "
                "ORDER BY source_root_key"
            )
            if isinstance(root_key, str) and root_key
            and isinstance(root_path, str) and root_path
        }
        short_names: dict[str, str] = {}
        for provider_root in provider_roots.values():
            state_path = provider_root / "state_5.sqlite"
            if not state_path.is_file():
                continue
            state_conn: sqlite3.Connection | None = None
            try:
                state_conn = sqlite3.connect(
                    f"{state_path.resolve().as_uri()}?mode=ro",
                    uri=True,
                    timeout=0.05,
                )
                for offset in range(0, len(native_ids), 500):
                    batch = native_ids[offset:offset + 500]
                    if not batch:
                        continue
                    placeholders = ",".join("?" for _ in batch)
                    for thread_id, title in state_conn.execute(
                        f"SELECT id, title FROM threads WHERE id IN ({placeholders})",
                        batch,
                    ):
                        clean_title = " ".join(str(title or "").split())
                        if clean_title:
                            short_names[str(thread_id)] = clean_title
            except (OSError, sqlite3.Error):
                continue
            finally:
                if state_conn is not None:
                    state_conn.close()

        metadata_by_native: dict[tuple[str, str], dict[str, object]] = {}
        for (
            root_key, source_path, native_thread_id, accounting_session_id,
            project_key, project_label, started_at,
        ) in rows:
            identity = (str(root_key or ""), str(source_path or ""))
            if not all(identity) or identity in metadata:
                continue
            item = {
                "title": short_names.get(str(native_thread_id or "")),
                "native_thread_id": native_thread_id,
                "accounting_session_id": accounting_session_id,
                "root_path": str(provider_roots.get(identity[0]) or ""),
                "project_key": project_key,
                "project_label": project_label,
                "started_at": started_at,
            }
            metadata[identity] = item
            native_identity = (identity[0], str(native_thread_id or ""))
            existing = metadata_by_native.get(native_identity)
            if existing is None or (not existing.get("project_key") and project_key):
                metadata_by_native[native_identity] = item

        # A child rollout can be accounting-complete while its own historical
        # conversation-thread row is absent (for example, a file first cached
        # before conversation normalization was introduced). The cursor still
        # persists the rooted native thread id. Inherit only presentation
        # metadata from that rooted task; the child's accounting path and
        # session id remain its own identity and totals are never merged.
        for (
            root_key, source_path, native_thread_id, accounting_session_id,
            started_at,
        ) in file_aliases:
            identity = (str(root_key or ""), str(source_path or ""))
            if not all(identity) or identity in metadata:
                continue
            native_identity = (identity[0], str(native_thread_id or ""))
            inherited = metadata_by_native.get(native_identity)
            metadata[identity] = {
                "title": short_names.get(native_identity[1]) or (inherited or {}).get("title"),
                "native_thread_id": native_thread_id,
                "accounting_session_id": accounting_session_id,
                "root_path": str(provider_roots.get(identity[0]) or ""),
                "project_key": (inherited or {}).get("project_key"),
                "project_label": (inherited or {}).get("project_label"),
                "started_at": started_at or (inherited or {}).get("started_at"),
            }
    except sqlite3.Error:
        return {}
    return metadata


def _session_wire(
    view: Any,
    *,
    metadata: Mapping[tuple[str, str], Mapping[str, object]] | None = None,
) -> dict[str, object]:
    rows = []
    for row in view.rows:
        # The Codex session aggregator intentionally splits equal relative
        # session paths from distinct $CODEX_HOME roots.  The opaque detail
        # key must use that same grouping identity or two visible rows route
        # to one another's detail payload.
        root_identity = row.codex_root or "single-root"
        row_metadata = (metadata or {}).get((str(row.codex_root or ""), str(row.session_id_path)))
        if row_metadata is None and metadata is not None:
            row_metadata = next((
                value for (root_key, source_path), value in metadata.items()
                if (
                    str(value.get("native_thread_id") or "") == str(row.session_id or "")
                    or str(value.get("accounting_session_id") or "") == str(row.session_id or "")
                    or source_path == str(row.session_id_path)
                )
                and (
                    not row.codex_root
                    or str(row.codex_root) in (
                        root_key,
                        str(value.get("root_path") or ""),
                    )
                )
            ), None)
        title = str(row_metadata.get("title") or "").strip() if row_metadata else ""
        project = str(row_metadata.get("project_label") or "").strip() if row_metadata else ""
        started_at = row_metadata.get("started_at") if row_metadata else None
        duration_min = None
        if isinstance(started_at, str):
            try:
                started_dt = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                duration_min = max(0, round((row.last_activity.astimezone(UTC) - started_dt.astimezone(UTC)).total_seconds() / 60))
            except (TypeError, ValueError):
                started_at = None
        rows.append({
            "key": dashboard_resource_key(
                "session", "codex", root_identity, row.session_id_path,
            ),
            "source": "codex",
            "label": title or None,
            "project": project or None,
            "project_key": row_metadata.get("project_key") if row_metadata else None,
            "started_at": started_at,
            "duration_min": duration_min,
            "last_activity": row.last_activity.astimezone(UTC).isoformat(),
            "cost_usd": row.cost_usd,
            "input_tokens": row.input_tokens,
            "cached_input_tokens": row.cached_input_tokens,
            "output_tokens": row.output_tokens,
            "reasoning_output_tokens": row.reasoning_output_tokens,
            "total_tokens": row.total_tokens,
            "models": tuple(row.models),
            "model_breakdowns": tuple(
                dict(item) for item in getattr(row, "model_breakdowns", ())
            ),
        })
    return {
        "rows": tuple(rows),
        "total_sessions": view.total_sessions,
        "total_cost_usd": view.total_cost_usd,
        "total_tokens": view.total_tokens,
    }


def _quota_wire(
    stats_conn: sqlite3.Connection,
    *,
    accounting_entries: Iterable[object] = (),
    cycle: CodexCycleBoundary | None = None,
    now_utc: dt.datetime | None = None,
    display_tz_name: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Build current-cycle Codex 5-hour activity rows from durable windows.

    The durable projection supplies the truthful native block boundaries. Cost,
    tokens, and model splits come from root-qualified accounting inside each
    half-open 300-minute interval. Weekly quota summaries are deliberately not
    activity blocks and never enter this wire.
    """
    if cycle is None or now_utc is None:
        return ()
    try:
        rows = stats_conn.execute(
            "SELECT source_root_key, logical_limit_key, observed_slot, window_minutes, "
            "limit_name, resets_at_utc, nominal_start_at_utc, current_percent, orphaned_at "
            "FROM quota_window_blocks WHERE source='codex' AND window_minutes=300 "
            "ORDER BY resets_at_utc DESC, source_root_key, logical_limit_key, observed_slot "
            "LIMIT ?",
            (SOURCE_HISTORY_LIMIT,),
        ).fetchall()
    except sqlite3.Error:
        return ()
    entries = tuple(accounting_entries)
    display_tz = ZoneInfo(display_tz_name) if display_tz_name else None
    c = sys.modules["cctally"]
    wired: list[dict[str, object]] = []
    seen_windows: set[tuple[str, dt.datetime, dt.datetime]] = set()
    for (
        root_key, logical_limit_key, observed_slot, window_minutes,
        _limit_name, resets_at_raw, nominal_start_raw, current_percent, orphaned_at,
    ) in rows:
        if orphaned_at is not None or str(root_key) not in cycle.source_root_keys:
            continue
        try:
            start_at = dt.datetime.fromisoformat(str(nominal_start_raw).replace("Z", "+00:00"))
            resets_at = dt.datetime.fromisoformat(str(resets_at_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if start_at.tzinfo is None or resets_at.tzinfo is None:
            continue
        start_at = start_at.astimezone(UTC)
        resets_at = resets_at.astimezone(UTC)
        if resets_at <= cycle.start_at or start_at >= cycle.resets_at:
            continue
        physical_key = (str(root_key), start_at, resets_at)
        if physical_key in seen_windows:
            continue
        seen_windows.add(physical_key)
        block_entries = tuple(
            entry for entry in entries
            if str(getattr(entry, "source_root_key", "")) == str(root_key)
            and start_at <= getattr(entry, "timestamp").astimezone(UTC) < resets_at
        )
        if not block_entries:
            continue
        by_model: dict[str, dict[str, object]] = {}
        for entry in block_entries:
            model = str(getattr(entry, "model", "") or "unknown")
            aggregate = by_model.setdefault(model, {
                "modelName": model,
                "inputTokens": 0,
                "cachedInputTokens": 0,
                "outputTokens": 0,
                "reasoningOutputTokens": 0,
                "totalTokens": 0,
                "costParts": [],
            })
            aggregate["inputTokens"] += int(getattr(entry, "input_tokens", 0))
            aggregate["cachedInputTokens"] += int(getattr(entry, "cached_input_tokens", 0))
            aggregate["outputTokens"] += int(getattr(entry, "output_tokens", 0))
            aggregate["reasoningOutputTokens"] += int(getattr(entry, "reasoning_output_tokens", 0))
            aggregate["totalTokens"] += int(getattr(entry, "total_tokens", 0))
            aggregate["costParts"].append(float(getattr(entry, "cost_usd", 0.0)))
        breakdowns: list[dict[str, object]] = []
        for aggregate in by_model.values():
            cost = stable_sum(aggregate.pop("costParts"))
            breakdowns.append({**aggregate, "cost": cost})
        breakdowns.sort(key=lambda row: (-float(row["cost"]), str(row["modelName"])))
        cost_usd = stable_sum(float(row["cost"]) for row in breakdowns)
        wired.append({
            "key": dashboard_resource_key(
                "block", "codex", root_key, logical_limit_key,
                observed_slot, window_minutes, resets_at_raw,
            ),
            "source": "codex",
            "label": c.format_display_dt(
                start_at, display_tz, fmt="%H:%M %b %d", suffix=True,
            ),
            "window_minutes": window_minutes,
            "start_at": start_at.isoformat(),
            "end_at": resets_at.isoformat(),
            "resets_at": resets_at_raw,
            "current_percent": current_percent,
            "orphaned": False,
            "is_active": start_at <= now_utc < resets_at,
            "cost_usd": cost_usd,
            "model_breakdowns": tuple(breakdowns),
        })
    return tuple(wired)


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
    *,
    accounting_entries: Iterable[object] = (),
) -> dict[str, object]:
    """Use S2's pure history/block/forecast kernels over cache evidence."""
    quota_observations = tuple(observations)
    cost_entries = tuple(accounting_entries)
    histories = build_history(quota_observations)
    blocks = build_blocks(quota_observations)
    history_rows: list[dict[str, object]] = []
    milestone_rows: list[dict[str, object]] = []
    active_rows: list[dict[str, object]] = []
    # R8 (#341 Task 4): the per-account `account_key` is serialized onto each
    # history row ONLY when the Codex provider has >1 REAL account, so the idle
    # clock (`_clock_cycle_validity`) can scope weekly-cycle validity per account
    # instead of degrading a genuine multi-account state to `conflicting`. A
    # <=1-real-account install (all fixtures) stays byte-identical (no key added).
    _codex_decorated = False
    try:
        import _cctally_account
        _codex_decorated = _cctally_account.provider_is_decorated(
            context.stats_conn, "codex")
    except Exception:
        _codex_decorated = False
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
            **({"account_key": identity.account_key} if _codex_decorated else {}),
            "label": _native_limit_label(identity.limit_name, identity.window_minutes),
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
        quota_key = dashboard_resource_key(
            "quota", "codex", identity.source_root_key,
            identity.logical_limit_key, identity.observed_slot,
            identity.window_minutes,
        )
        block_cost_entries = tuple(
            entry for entry in cost_entries
            if str(getattr(entry, "source_root_key", "")) == identity.source_root_key
            and block.nominal_start_at
            <= getattr(entry, "timestamp").astimezone(UTC)
            < block.resets_at
        )
        canonical_rows = ()
        if identity.window_minutes == 10_080 and block.resets_at > context.now_utc:
            try:
                canonical_rows = codex_quota_breakdown(
                    identity,
                    block.resets_at,
                    speed=context.speed,
                    cache_conn=context.cache_conn,
                    stats_conn=context.stats_conn,
                )
            except sqlite3.Error:
                # Older or partially migrated stores retain the bounded
                # observation-derived fallback below.  A coherent current
                # store always has the durable projection used by the CLI.
                canonical_rows = ()
        if canonical_rows:
            try:
                correlated_five_hour = tuple(
                    observation
                    for observation in load_codex_quota_observations(
                        source_root_keys={identity.source_root_key},
                        cache_conn=context.cache_conn,
                        captured_at_or_after=block.nominal_start_at,
                    )
                    if observation.identity.window_minutes == 300
                    and observation.identity.observed_slot == identity.observed_slot
                    and observation.identity.limit_id == identity.limit_id
                )
            except sqlite3.Error:
                correlated_five_hour = ()

            for row in canonical_rows:
                milestone_rows.append({
                    "key": dashboard_resource_key(
                        "quota_milestone", "codex", *block_parts,
                        row.percent, row.captured_at.astimezone(UTC).isoformat(),
                    ),
                    "source": "codex",
                    "block_key": dashboard_resource_key("block", "codex", *block_parts),
                    "quota_key": quota_key,
                    "window_minutes": identity.window_minutes,
                    "resets_at": block.resets_at.astimezone(UTC).isoformat(),
                    "percent": row.percent,
                    "captured_at": row.captured_at.astimezone(UTC).isoformat(),
                    "cumulative_usd": row.cost_usd,
                    "marginal_usd": row.marginal_cost_usd,
                    "input_tokens": row.input_tokens,
                    "cached_input_tokens": row.cached_input_tokens,
                    "output_tokens": row.output_tokens,
                    "reasoning_output_tokens": row.reasoning_output_tokens,
                    "total_tokens": row.total_tokens,
                    "five_hour_percent": codex_five_hour_percent_at_crossing(
                        identity, row.captured_at, correlated_five_hour,
                    ),
                })
            continue

        previous_cumulative = 0.0
        for milestone in percent_milestones(block):
            cumulative_usd = stable_sum(
                float(getattr(entry, "cost_usd", 0.0))
                for entry in block_cost_entries
                if getattr(entry, "timestamp").astimezone(UTC) <= milestone.captured_at
            )
            milestone_rows.append({
                "key": dashboard_resource_key(
                    "quota_milestone", "codex", *block_parts,
                    milestone.percent, milestone.captured_at.astimezone(UTC).isoformat(),
                ),
                "source": "codex",
                "block_key": dashboard_resource_key("block", "codex", *block_parts),
                "quota_key": quota_key,
                "window_minutes": identity.window_minutes,
                "resets_at": block.resets_at.astimezone(UTC).isoformat(),
                "percent": milestone.percent,
                "captured_at": milestone.captured_at.astimezone(UTC).isoformat(),
                "cumulative_usd": cumulative_usd,
                "marginal_usd": max(0.0, cumulative_usd - previous_cumulative),
            })
            previous_cumulative = cumulative_usd
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


def _clock_cycle_validity(
    histories: Iterable[object],
    now_utc: dt.datetime,
) -> tuple[bool, str]:
    """Re-evaluate frozen weekly evidence without touching cache or rollouts.

    Per-account (#341 Task 4): boundaries are grouped by the history row's
    ``account_key`` (serialized only when the Codex provider is DECORATED, i.e.
    >1 real account — R8). The idle clock mirrors the build-time
    ``_resolve_codex_weekly_cycle`` resolution: an account yields a live cycle iff
    it has EXACTLY ONE fresh future boundary, and the hero is valid iff AT LEAST
    ONE account does (raising the aggregate ``conflicting > stale > missing``
    reason only when NO account yields one). When ``account_key`` is absent
    (<=1-real-account install — no decoration) every row falls into one global
    bucket, so this reduces EXACTLY to the prior single-boundary logic
    (byte-stable). This removes the documented placeholder degrade where two real
    accounts with distinct weekly cycles collapsed the whole hero to
    ``conflicting`` on the idle clock.
    """
    per_account: dict[str, set[dt.datetime]] = {}
    stale_by_account: dict[str, bool] = {}
    for raw_history in histories:
        if not isinstance(raw_history, Mapping):
            continue
        if raw_history.get("window_minutes") != 10_080:
            continue
        current = raw_history.get("current_percent")
        forecast = raw_history.get("forecast")
        if current is None or not isinstance(forecast, Mapping):
            continue
        try:
            resets_at = dt.datetime.fromisoformat(
                str(forecast.get("resets_at")).replace("Z", "+00:00")
            ).astimezone(UTC)
        except (TypeError, ValueError):
            continue
        if resets_at <= now_utc:
            continue
        # None account_key (undecorated / <=1 real account) -> one global bucket
        # == today's behavior. A real key buckets per account.
        acct = raw_history.get("account_key")
        bucket = acct if acct is not None else "__all__"
        if raw_history.get("freshness") != "fresh":
            stale_by_account[bucket] = True
            continue
        per_account.setdefault(bucket, set()).add(resets_at)
    accounts = set(per_account) | set(stale_by_account)
    reasons: list[str] = []
    for acct in accounts:
        boundaries = per_account.get(acct, set())
        if len(boundaries) == 1:
            return True, "ok"  # at least one account yields a live cycle
        reasons.append(
            "conflicting" if boundaries
            else ("stale" if stale_by_account.get(acct) else "missing")
        )
    if "conflicting" in reasons:
        return False, "conflicting"
    if "stale" in reasons:
        return False, "stale"
    return False, "missing"


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
    cycle_changed = False
    capabilities = state.capabilities
    warnings = state.warnings
    availability = state.availability
    freshness = state.freshness
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
        hero = data.get("hero")
        hero_capability = state.capabilities.get("hero")
        if (
            isinstance(hero, Mapping)
            and isinstance(hero.get("cycle"), Mapping)
            and hero_capability is not None
            and hero_capability.status == "supported"
        ):
            cycle_valid, cycle_reason = _clock_cycle_validity(refreshed_histories, now_utc)
            if not cycle_valid:
                hero = dict(hero)
                for field in (
                    "cost_usd", "input_tokens", "cached_input_tokens", "output_tokens",
                    "reasoning_output_tokens", "total_tokens", "cycle",
                ):
                    hero[field] = None
                data["hero"] = hero
                refreshed_capabilities = dict(state.capabilities)
                refreshed_capabilities["hero"] = CapabilityRecord(
                    "unavailable", "missing-or-conflicting-native-cycle",
                )
                capabilities = refreshed_capabilities
                warnings = tuple(
                    warning for warning in state.warnings
                    if warning.code != "codex_cycle_unavailable"
                ) + (SourceDashboardWarning(
                    "codex_cycle_unavailable",
                    "Codex native reset cycle is unavailable.",
                    "hero",
                ),)
                availability = "partial"
                if cycle_reason == "stale":
                    freshness = "stale"
                cycle_changed = True
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
    if not (quota_changed or budget_changed or cycle_changed):
        return state
    refreshed_state = SourceDashboardState(
        source=state.source,
        availability=availability,
        freshness=freshness,
        warnings=warnings,
        data_version=state.data_version,
        last_success_at=state.last_success_at,
        capabilities=capabilities,
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


def _partial_projects_wire(
    entries: Iterable[object],
    metadata: Mapping[tuple[str, str], Mapping[str, object]],
) -> dict[str, object]:
    """Aggregate the qualified subset when older accounting metadata is mixed.

    Rows without a cached conversation/project identity are omitted and remain
    covered by the Projects-domain warning. Valid projects stay visible; their
    totals never include an unqualified accounting row.
    """
    groups: dict[tuple[str, str], dict[str, object]] = {}
    for entry in entries:
        identity = (
            str(getattr(entry, "source_root_key", "") or ""),
            str(getattr(entry, "source_path", "") or ""),
        )
        row_metadata = metadata.get(identity)
        project_key = str(row_metadata.get("project_key") or "").strip() if row_metadata else ""
        project_label = str(row_metadata.get("project_label") or "").strip() if row_metadata else ""
        if not project_key or not project_label:
            continue
        group_key = (identity[0], project_key)
        group = groups.setdefault(group_key, {
            "project_key": project_key,
            "label": project_label,
            "sessions": set(),
            "first_seen": getattr(entry, "timestamp"),
            "last_seen": getattr(entry, "timestamp"),
            "cost_usd": 0.0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
            "models": {},
            "session_rows": {},
        })
        timestamp = getattr(entry, "timestamp")
        group["first_seen"] = min(group["first_seen"], timestamp)
        group["last_seen"] = max(group["last_seen"], timestamp)
        group["sessions"].add(identity)
        for field in (
            "cost_usd", "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens",
        ):
            group[field] += getattr(entry, field)
        model = str(getattr(entry, "model", "") or "unknown")
        model_totals = group["models"].setdefault(model, {
            "model": model, "cost_usd": 0.0, "input_tokens": 0,
            "cached_input_tokens": 0, "output_tokens": 0,
            "reasoning_output_tokens": 0, "total_tokens": 0,
        })
        session_totals = group["session_rows"].setdefault(identity, {
            "label": str(row_metadata.get("title") or "Session"),
            "last_activity": timestamp.astimezone(UTC).isoformat(),
            "cost_usd": 0.0, "input_tokens": 0, "cached_input_tokens": 0,
            "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0,
        })
        if timestamp.astimezone(UTC).isoformat() > session_totals["last_activity"]:
            session_totals["last_activity"] = timestamp.astimezone(UTC).isoformat()
        for field in (
            "cost_usd", "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens",
        ):
            value = getattr(entry, field)
            model_totals[field] += value
            session_totals[field] += value

    label_map = collision_safe_project_label_map(
        (f"{root_key}\0{project_key}", str(group["label"]))
        for (root_key, project_key), group in groups.items()
    )
    rows = []
    for (root_key, _project_key), group in groups.items():
        internal_identity = f"{root_key}\0{group['project_key']}"
        rows.append({
            "key": dashboard_resource_key("project", "codex", root_key, group["project_key"]),
            "source": "codex",
            "label": label_map[internal_identity],
            "session_count": len(group["sessions"]),
            "first_seen": group["first_seen"].astimezone(UTC).isoformat(),
            "last_seen": group["last_seen"].astimezone(UTC).isoformat(),
            "cost_usd": group["cost_usd"],
            "input_tokens": group["input_tokens"],
            "cached_input_tokens": group["cached_input_tokens"],
            "output_tokens": group["output_tokens"],
            "reasoning_output_tokens": group["reasoning_output_tokens"],
            "total_tokens": group["total_tokens"],
            "models": tuple(sorted(
                group["models"].values(),
                key=lambda item: (-float(item["cost_usd"]), str(item["model"])),
            )),
            "sessions": tuple(sorted(
                group["session_rows"].values(),
                key=lambda item: str(item["last_activity"]), reverse=True,
            )),
        })
    rows.sort(key=lambda row: (-float(row["cost_usd"]), str(row["label"]), str(row["key"])))
    return {
        "rows": tuple(rows),
        "total_cost_usd": stable_sum(float(row["cost_usd"]) for row in rows),
        "total_tokens": sum(int(row["total_tokens"]) for row in rows),
    }


def _codex_entries_from_accounting(entries: Iterable[object]) -> list[CodexEntry]:
    """Adapt coordinated accounting rows for the shipped non-project kernels."""
    converted: list[CodexEntry] = []
    for entry in entries:
        source_path = str(getattr(entry, "source_path", "") or "")
        session_id = str(getattr(entry, "session_id", "") or "")
        if not source_path or not session_id:
            raise SourceCapabilityUnavailable("Codex accounting lacks session identity")
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


def _codex_entries_from_qualified(entries: Iterable[object]) -> list[CodexEntry]:
    """Compatibility name retained for the source-detail reader."""
    return _codex_entries_from_accounting(entries)


def _build_codex_native_weekly_view(
    stats_conn: sqlite3.Connection,
    entries: Iterable[object],
    *,
    source_root_keys: Iterable[str],
    active_cycle: CodexCycleBoundary | None,
    now_utc: dt.datetime,
    display_tz_name: str | None,
    speed: str,
) -> CodexWeeklyView:
    """Aggregate Codex cost into observed native quota-cycle segments."""
    periods = _codex_weekly_periods(
        stats_conn,
        source_root_keys=source_root_keys,
        active_cycle=active_cycle,
    )
    converted: list[CodexEntry] = []
    bucket_by_entry: dict[int, str] = {}
    display_tz = ZoneInfo(display_tz_name) if display_tz_name else None
    labels: dict[str, str] = {}
    periods_by_bucket: dict[str, CodexWeeklyPeriod] = {}
    for entry in entries:
        if codex_model_scoped_quota_pool(getattr(entry, "model", None)) is not None:
            continue
        timestamp = getattr(entry, "timestamp").astimezone(UTC)
        root_key = str(getattr(entry, "source_root_key", "") or "")
        period = next((
            candidate for candidate in periods
            if root_key in candidate.source_root_keys
            and candidate.start_at <= timestamp < candidate.end_at
        ), None)
        if period is None:
            continue
        converted_entry = _codex_entries_from_accounting((entry,))[0]
        bucket = period.start_at.isoformat()
        converted.append(converted_entry)
        bucket_by_entry[id(converted_entry)] = bucket
        local_start = (
            period.start_at.astimezone(display_tz)
            if display_tz is not None else period.start_at.astimezone()
        )
        labels[bucket] = local_start.strftime("%m-%d %H:%M")
        periods_by_bucket[bucket] = period

    rows = _aggregate_codex_buckets(
        converted,
        key_fn=lambda entry: bucket_by_entry[id(entry)],
        speed=speed,
    )
    display_rows = tuple(
        replace(
            row,
            bucket=labels[row.bucket],
            period_start_at=periods_by_bucket[row.bucket].start_at,
            period_end_at=periods_by_bucket[row.bucket].end_at,
            used_pct=periods_by_bucket[row.bucket].used_percent,
            dollar_per_pct=(
                row.cost_usd / periods_by_bucket[row.bucket].used_percent
                if periods_by_bucket[row.bucket].used_percent is not None
                and periods_by_bucket[row.bucket].used_percent > 0
                else None
            ),
        )
        for row in rows
    )
    return CodexWeeklyView(
        rows=display_rows,
        total_cost_usd=stable_sum(row.cost_usd for row in display_rows),
        total_tokens=sum(row.total_tokens for row in display_rows),
        period_start=(periods[0].start_at if periods else None),
        period_end=now_utc,
        display_tz_label=display_tz_name or str(dt.datetime.now().astimezone().tzinfo),
    )


def _codex_account_five_hour_percent(
    observations: Iterable[object],
    now_utc: dt.datetime,
) -> dict[str, float]:
    """Per-account current five-hour (300-minute) used-percent (#341 Task 4).

    Account key -> the highest active-window used-percent among that account's
    fresh sibling 300-minute windows. Used to render the per-account hero card's
    5h bar; account-blind physical breakdown readers are untouched.
    """
    result: dict[str, float] = {}
    for history in build_history(tuple(observations)):
        if history.identity.window_minutes != 300:
            continue
        baseline = select_baseline(history.observations, now_utc)
        if baseline is None or baseline.resets_at <= now_utc:
            continue
        acct = history.identity.account_key
        pct = float(baseline.used_percent)
        if acct not in result or pct > result[acct]:
            result[acct] = pct
    return result


def _codex_accounts_wire(
    context: DashboardReadContext,
    *,
    quota_observations: Iterable[object],
    cycles: list["CodexCycleBoundary"],
    accounting_start: dt.datetime,
    accounting_end: dt.datetime,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Return ``(accounts_wire, hero_cycles_wire)`` for a decorated Codex source.

    Caller must gate on ``provider_is_decorated(stats_conn, "codex")`` — this
    builds nothing for a <=1-real-account install (the whole surface is absent,
    so the envelope stays byte-identical, spec R8). Each account carries
    ``{accountKey, label, plan, active, weeklyPercent, fiveHourPercent, resetsAt,
    spendUsd, inputTokens, cachedInputTokens, outputTokens,
    reasoningOutputTokens, totalTokens, unattributed?}``; ``hero_cycles_wire`` is
    the thin per-account cycle-boundary list the hero renders (``cycles[]``).
    """
    import _cctally_account
    active_keys = _cctally_account.resolve_active_account_keys()
    five_hour = _codex_account_five_hour_percent(quota_observations, context.now_utc)
    cycle_by_account: dict[str, "CodexCycleBoundary"] = {}
    for cyc in cycles:
        acct = (
            cyc.quota_identity.account_key if cyc.quota_identity is not None
            else _lib_accounts.UNATTRIBUTED
        )
        cycle_by_account.setdefault(acct, cyc)
    # Registry accounts (real, deterministically ordered) + the unattributed
    # sentinel when it has any retained accounting (it renders dimmed, totals
    # only). Registry rows never include the sentinel.
    reg = _cctally_account.load_accounts(context.stats_conn, "codex")
    plan_by_key = {r["account_key"]: r.get("plan_type") for r in reg}
    ordered_keys = [r["account_key"] for r in reg]
    # Include unattributed last iff it has cycle/5h/spend evidence.
    unattributed_rows = load_cached_rooted_codex_accounting_entries(
        accounting_start, accounting_end, speed=context.speed,
        cache_conn=context.cache_conn, account_key=_lib_accounts.UNATTRIBUTED,
    )
    if (
        unattributed_rows
        or _lib_accounts.UNATTRIBUTED in cycle_by_account
        or _lib_accounts.UNATTRIBUTED in five_hour
    ):
        ordered_keys.append(_lib_accounts.UNATTRIBUTED)

    def _totals(rows: tuple[object, ...]) -> dict[str, object]:
        entries = _codex_entries_from_accounting(rows)
        cost = build_codex_daily_view(
            entries, now_utc=context.now_utc, tz_name=context.display_tz_name,
            speed=context.speed,
        ).total_cost_usd if entries else 0.0
        return {
            "spendUsd": cost,
            "inputTokens": sum(e.input_tokens for e in entries),
            "cachedInputTokens": sum(e.cached_input_tokens for e in entries),
            "outputTokens": sum(e.output_tokens for e in entries),
            "reasoningOutputTokens": sum(e.reasoning_output_tokens for e in entries),
            "totalTokens": sum(e.total_tokens for e in entries),
        }

    accounts_wire: list[dict[str, object]] = []
    hero_cycles_wire: list[dict[str, object]] = []
    for key in ordered_keys:
        cyc = cycle_by_account.get(key)
        is_unattributed = key == _lib_accounts.UNATTRIBUTED
        if cyc is not None and not is_unattributed:
            cycle_end = min(accounting_end, cyc.resets_at)
            rows = load_cached_rooted_codex_accounting_entries(
                cyc.start_at, cycle_end, speed=context.speed,
                cache_conn=context.cache_conn,
                source_root_keys=cyc.source_root_keys, account_key=key,
            )
            totals = _totals(rows)
        elif is_unattributed:
            totals = _totals(unattributed_rows)
        else:
            # A real account without a live weekly cycle: totals over the
            # accounting range so the card still shows spend (no bars/reset).
            rows = load_cached_rooted_codex_accounting_entries(
                accounting_start, accounting_end, speed=context.speed,
                cache_conn=context.cache_conn, account_key=key,
            )
            totals = _totals(rows)
        card: dict[str, object] = {
            "accountKey": key,
            "label": _cctally_account.account_label(context.stats_conn, key),
            "plan": plan_by_key.get(key),
            "active": key in active_keys,
            "weeklyPercent": (
                None if is_unattributed or cyc is None else cyc.used_percent
            ),
            "fiveHourPercent": (None if is_unattributed else five_hour.get(key)),
            "resetsAt": (
                None if is_unattributed or cyc is None
                else cyc.resets_at.astimezone(UTC).isoformat()
            ),
            **totals,
        }
        if is_unattributed:
            card["unattributed"] = True
        accounts_wire.append(card)
        if cyc is not None and not is_unattributed:
            hero_cycles_wire.append({
                "accountKey": key,
                "window_minutes": cyc.window_minutes,
                "start_at": cyc.start_at.astimezone(UTC).isoformat(),
                "resets_at": cyc.resets_at.astimezone(UTC).isoformat(),
                "used_percent": cyc.used_percent,
                "cost_usd": totals["spendUsd"],
                "total_tokens": totals["totalTokens"],
            })
    return accounts_wire, hero_cycles_wire


def _claude_accounts_wire(
    stats_conn: sqlite3.Connection,
    *,
    now_utc: dt.datetime,
) -> list[dict[str, object]]:
    """Per-account Claude hero cards (#341 Task 4, Ruling C).

    Symmetric with ``_codex_accounts_wire``: the caller gates on
    ``provider_is_decorated(stats_conn, "claude")`` (>1 REAL account, R8), so a
    <=1-real-account install builds nothing and its envelope stays byte-identical
    on BOTH goldens. Each card carries
    ``{accountKey, label, plan, active, weeklyPercent, fiveHourPercent, resetsAt,
    spendUsd, unattributed?}`` drawn from the ALREADY-account-scoped stats
    snapshots (``weekly_usage_snapshots``/``weekly_cost_snapshots`` both hold
    ``account_key`` — Section 6 scope matrix), taking each account's latest
    captured row as its current-cycle state. spendUsd is the snapshotted weekly
    cost (the ``report`` semantics), account-scoped. The unattributed bucket
    renders last, dimmed/totals-only, iff it has any retained snapshot.
    """
    import _cctally_account
    active_keys = _cctally_account.resolve_active_account_keys()
    reg = _cctally_account.load_accounts(stats_conn, "claude")
    plan_by_key = {r["account_key"]: r.get("plan_type") for r in reg}
    ordered_keys = [r["account_key"] for r in reg]

    def _latest_usage(key: str):
        return stats_conn.execute(
            "SELECT weekly_percent, five_hour_percent, week_end_at "
            "FROM weekly_usage_snapshots WHERE account_key=? "
            "ORDER BY captured_at_utc DESC LIMIT 1",
            (key,),
        ).fetchone()

    def _latest_cost(key: str) -> float:
        row = stats_conn.execute(
            "SELECT cost_usd FROM weekly_cost_snapshots WHERE account_key=? "
            "ORDER BY captured_at_utc DESC LIMIT 1",
            (key,),
        ).fetchone()
        return float(row[0]) if row is not None and row[0] is not None else 0.0

    # Include the unattributed bucket last iff it retained any snapshot.
    unattr_usage = _latest_usage(_lib_accounts.UNATTRIBUTED)
    unattr_cost = stats_conn.execute(
        "SELECT 1 FROM weekly_cost_snapshots WHERE account_key=? LIMIT 1",
        (_lib_accounts.UNATTRIBUTED,),
    ).fetchone()
    if unattr_usage is not None or unattr_cost is not None:
        ordered_keys.append(_lib_accounts.UNATTRIBUTED)

    cards: list[dict[str, object]] = []
    for key in ordered_keys:
        is_unattributed = key == _lib_accounts.UNATTRIBUTED
        usage = _latest_usage(key)
        weekly_pct = usage[0] if usage is not None else None
        five_hour_pct = usage[1] if usage is not None else None
        resets_at = usage[2] if usage is not None else None
        card: dict[str, object] = {
            "accountKey": key,
            "label": _cctally_account.account_label(stats_conn, key),
            "plan": plan_by_key.get(key),
            "active": key in active_keys,
            "weeklyPercent": None if is_unattributed else weekly_pct,
            "fiveHourPercent": None if is_unattributed else five_hour_pct,
            "resetsAt": None if is_unattributed else resets_at,
            "spendUsd": _latest_cost(key),
        }
        if is_unattributed:
            card["unattributed"] = True
        cards.append(card)
    return cards


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
    projection_incoherent = not coherence.coherent
    # The cache reader's established report surface treats the ``now`` instant
    # as inclusive.  The qualified adapter is half-open, so extend only its
    # query/result boundary by one microsecond and keep all live budget sums
    # explicitly half-open at ``now`` below.
    accounting_end = context.now_utc + dt.timedelta(microseconds=1)
    accounting_start = context.range_start
    if context.codex_budget is not None:
        _period, budget_start, _budget_end = _configured_codex_budget_window(context)
        accounting_start = min(accounting_start, budget_start)
    health = load_codex_project_metadata_health(
        cache_conn=context.cache_conn,
        start=accounting_start,
        end=accounting_end,
    )
    metadata_incomplete = health.incomplete_rows > 0
    metadata_warning_message = (
        f"{health.incomplete_rows} Codex accounting row(s) lack project metadata; "
        "run `cctally cache-sync --source codex --rebuild`."
        if metadata_incomplete
        else "Codex project metadata could not be read; "
        "run `cctally cache-sync --source codex --rebuild`."
    )
    qualified_entries: tuple[object, ...] = ()
    if not metadata_incomplete:
        try:
            qualified_entries = load_qualified_codex_entries(
                accounting_start,
                accounting_end,
                speed=context.speed,
                sync=False,
                cache_conn=context.cache_conn,
            )
            accounting_entries: tuple[object, ...] = qualified_entries
        except QualifiedMetadataUnavailable:
            # A cached read must be internally coherent, but retain accounting
            # once if a defensive race or malformed row violates that premise.
            _lib_log.get_logger("dashboard").warning(
                "Codex qualified metadata read became unavailable; using cache-only accounting fallback"
            )
            metadata_incomplete = True
            accounting_entries = load_cached_rooted_codex_accounting_entries(
                accounting_start,
                accounting_end,
                speed=context.speed,
                cache_conn=context.cache_conn,
            )
    else:
        accounting_entries = load_cached_rooted_codex_accounting_entries(
            accounting_start,
            accounting_end,
            speed=context.speed,
            cache_conn=context.cache_conn,
        )
    budget_entries = _codex_entries_from_accounting(accounting_entries)
    cycle_reason: str | None = None
    cycles_all: list[CodexCycleBoundary] = []
    try:
        # Per-account list (#341 Task 2). ``cycles_all`` drives the per-account
        # hero cards (Task 4, gated on decoration); ``cycle`` stays the first
        # account's boundary (sorted by account_key) as the interim single hero —
        # for a single-account install this IS today's single boundary
        # (byte-stable), and a multi-account install no longer degrades to
        # `conflicting`.
        cycles_all = _resolve_codex_weekly_cycle(quota_observations, context.now_utc)
        cycle = cycles_all[0] if cycles_all else None
    except CodexCycleUnavailable as exc:
        cycle = None
        cycle_reason = exc.reason
    cycle_failure = cycle is None and has_cached_codex_accounting_entries(
        cache_conn=context.cache_conn,
    )
    hero_failure = projection_incoherent or cycle_failure
    if cycle is None or hero_failure:
        cycle_entries: list[CodexEntry] = []
        cycle_cost_usd: float | None = None if hero_failure else 0.0
    else:
        cycle_end = min(accounting_end, cycle.resets_at)
        cycle_rows = load_cached_rooted_codex_accounting_entries(
            cycle.start_at,
            cycle_end,
            speed=context.speed,
            cache_conn=context.cache_conn,
            source_root_keys=cycle.source_root_keys,
        )
        cycle_entries = _codex_entries_from_accounting(cycle_rows)
        cycle_cost_usd = build_codex_daily_view(
            cycle_entries,
            now_utc=context.now_utc,
            tz_name=context.display_tz_name,
            speed=context.speed,
        ).total_cost_usd
    visible_accounting_entries = tuple(
        entry for entry in accounting_entries
        if context.range_start <= getattr(entry, "timestamp").astimezone(UTC) < accounting_end
    )
    entries = _codex_entries_from_accounting(visible_accounting_entries)
    daily = build_codex_daily_view(
        entries, now_utc=context.now_utc, tz_name=context.display_tz_name, speed=context.speed,
    )
    monthly = build_codex_monthly_view(
        entries, now_utc=context.now_utc, tz_name=context.display_tz_name, speed=context.speed,
    )
    weekly = _build_codex_native_weekly_view(
        context.stats_conn,
        visible_accounting_entries,
        source_root_keys=active_roots,
        active_cycle=cycle,
        now_utc=context.now_utc,
        display_tz_name=context.display_tz_name,
        speed=context.speed,
    )
    sessions = (
        build_rooted_codex_session_view(
            visible_accounting_entries,
            now_utc=context.now_utc,
            tz_name=context.display_tz_name,
            speed=context.speed,
        )
        if metadata_incomplete else build_codex_session_view(
            entries, now_utc=context.now_utc, tz_name=context.display_tz_name, speed=context.speed,
        )
    )
    quota = _quota_read_model(
        context,
        quota_observations,
        accounting_entries=visible_accounting_entries,
    )
    quota_blocks = _quota_wire(
        context.stats_conn,
        accounting_entries=visible_accounting_entries,
        cycle=cycle,
        now_utc=context.now_utc,
        display_tz_name=context.display_tz_name,
    )
    # Hero-modal historical-milestone navigation index (spec §1c, §3). Built
    # here on the non-idle codex source rebuild (idle ticks reuse the stored
    # bundle) over the durable projection — a pure serializer never touches it.
    # Guarded: an index failure must never fail the codex source build.
    cycle_index: tuple = ()
    if cycle is not None and not hero_failure:
        try:
            cycle_index = tuple(
                sys.modules["cctally"].build_codex_cycle_index(
                    context.stats_conn, identity=cycle, now_utc=context.now_utc,
                )
            )
        except sqlite3.Error:
            cycle_index = ()
    quota = {**quota, "blocks": quota_blocks, "cycle_index": cycle_index}
    budget_rows = _budget_wire(context.stats_conn)
    projected_budget_rows = _projected_budget_wire(context.stats_conn)
    budget_cost_events = _codex_budget_cost_events(context, budget_entries)
    configured_budget = _configured_codex_budget_status(
        context, budget_entries, cost_events=budget_cost_events,
    )
    conversation_metadata = _codex_conversation_metadata(context.cache_conn)
    cache_report = _codex_cache_report_wire(
        visible_accounting_entries,
        metadata=conversation_metadata,
        now_utc=context.now_utc,
        display_tz_name=context.display_tz_name,
        speed=context.speed,
        anomaly_threshold_pp=context.cache_report_anomaly_threshold_pp,
    )
    projects = (
        _partial_projects_wire(visible_accounting_entries, conversation_metadata)
        if metadata_incomplete else _projects_wire(
            context,
            quota_observations,
            visible_accounting_entries,
            accounting_end=accounting_end,
        )
    )
    alerts = _alerts_wire(context.stats_conn)
    availability = (
        "partial" if metadata_incomplete or hero_failure
        else ("ok" if (entries or quota_blocks or budget_rows) else "empty")
    )
    hero_input = None if hero_failure else sum(entry.input_tokens for entry in cycle_entries)
    hero_cached = None if hero_failure else sum(entry.cached_input_tokens for entry in cycle_entries)
    hero_output = None if hero_failure else sum(entry.output_tokens for entry in cycle_entries)
    hero_reasoning = None if hero_failure else sum(entry.reasoning_output_tokens for entry in cycle_entries)
    hero_total = None if hero_failure else sum(entry.total_tokens for entry in cycle_entries)
    warnings: list[SourceDashboardWarning] = []
    if metadata_incomplete:
        warnings.append(SourceDashboardWarning(
            "codex_metadata_incomplete",
            metadata_warning_message,
            "projects",
        ))
    if projection_incoherent:
        warnings.append(SourceDashboardWarning(
            "codex_projection_incoherent",
            "Codex quota projection is unavailable.",
            "hero",
        ))
    if cycle_failure:
        warnings.append(SourceDashboardWarning(
            "codex_cycle_unavailable",
            "Codex native reset cycle is unavailable.",
            "hero",
        ))
    # #341 Task 4: the conditional per-account wire. Built ONLY when the Codex
    # provider has >1 REAL account (R8) — a <=1-real-account install adds nothing
    # so its envelope is byte-identical to today. The array ships EVERY account's
    # projection (client-side chip filter); the hero renders per-account cards.
    accounts_wire: list[dict[str, object]] = []
    hero_cycles_wire: list[dict[str, object]] = []
    try:
        import _cctally_account
        _codex_decorated = _cctally_account.provider_is_decorated(
            context.stats_conn, "codex")
    except Exception:
        _codex_decorated = False
    if _codex_decorated:
        try:
            accounts_wire, hero_cycles_wire = _codex_accounts_wire(
                context,
                quota_observations=quota_observations,
                cycles=cycles_all,
                accounting_start=accounting_start,
                accounting_end=accounting_end,
            )
        except (sqlite3.Error, QualifiedMetadataUnavailable):
            # A per-account wire failure must never fail the whole source build;
            # degrade to the byte-stable undecorated shape.
            accounts_wire = []
            hero_cycles_wire = []
    return SourceDashboardState(
        source="codex",
        availability=availability,
        freshness=("stale" if cycle_reason == "stale" else "fresh"),
        warnings=tuple(warnings),
        data_version=data_version,
        last_success_at=context.now_utc,
        capabilities={
            "hero": (
                CapabilityRecord(
                    "unavailable",
                    (
                        "projection-incoherent" if projection_incoherent
                        else "missing-or-conflicting-native-cycle"
                    ),
                )
                if hero_failure
                else CapabilityRecord("supported", "native-reset-cycle")
            ),
            "daily": CapabilityRecord("supported", "calendar-day"),
            "monthly": CapabilityRecord("supported", "calendar-month"),
            "weekly": CapabilityRecord("derived", "native-reset-cycles"),
            "sessions": CapabilityRecord("supported", "inclusive-input-tokens"),
            "forensics": CapabilityRecord("supported", "inclusive-input-token-reuse"),
            "quota": CapabilityRecord("derived", "native-windows"),
            "budget": CapabilityRecord("supported", "calendar-period"),
            "projects": (
                CapabilityRecord("supported", "conversation-metadata-partial")
                if metadata_incomplete
                else CapabilityRecord("supported", "qualified-attribution")
            ),
            "alerts": CapabilityRecord("supported", "provider-native"),
        },
        data={
            "hero": {
                "cost_usd": cycle_cost_usd,
                "input_tokens": hero_input,
                "cached_input_tokens": hero_cached,
                "output_tokens": hero_output,
                "reasoning_output_tokens": hero_reasoning,
                "total_tokens": hero_total,
                "cycle": (
                    {
                        "window_minutes": cycle.window_minutes,
                        "start_at": cycle.start_at.astimezone(UTC).isoformat(),
                        "resets_at": cycle.resets_at.astimezone(UTC).isoformat(),
                    }
                    if cycle is not None and not hero_failure else None
                ),
                "quota": quota["summary"],
                "budget": configured_budget,
                "alerts": {"count": len(alerts)},
                **({"cycles": hero_cycles_wire} if _codex_decorated else {}),
            },
            **({"accounts": accounts_wire} if _codex_decorated else {}),
            "periods": {
                "daily": _period_wire(daily),
                "monthly": _period_wire(monthly),
                "weekly": _period_wire(weekly),
            },
            "sessions": _session_wire(sessions, metadata=conversation_metadata),
            "quota": quota,
            "budget": {
                "status": configured_budget,
                "milestones": budget_rows,
                "projected": projected_budget_rows,
            },
            "projects": projects,
            "alerts": {
                "rows": alerts,
                "actual_thresholds": context.codex_quota_actual_thresholds,
                "projected_thresholds": context.codex_quota_projected_thresholds,
            },
            "cache_report": cache_report,
        },
        clock_data={"codex_budget_cost_events": budget_cost_events},
    )
