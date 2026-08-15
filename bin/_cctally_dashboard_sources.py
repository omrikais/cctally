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
    assert_projection_readable,
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
import _lib_snapshot_cache
from _lib_dashboard_sources import (
    CapabilityRecord,
    ProjectionCoherence,
    SourceDashboardState,
    SourceDashboardWarning,
    assess_codex_projection_coherence,
    canonical_alerted_at,
    canonical_alerted_at_sql,
    dashboard_resource_key,
)
from _lib_quota import (
    QuotaWindowIdentity,
    build_blocks,
    build_history,
    forecast_quota,
    latest_physical_observation,
    percent_milestones,
    quota_freshness,
    select_baseline,
    stale_after_seconds,
)
from _lib_jsonl import CodexEntry
from _lib_codex_account_adoption import ACCOUNT_WEEKLY_WINDOW_MINUTES
from _lib_codex_pools import (
    codex_history_is_model_scoped,
    codex_model_scoped_quota_pool,
    is_model_scoped_codex_quota,
)
from _lib_codex_conversation import _display_title as _codex_display_title
from _lib_fmt import stable_sum
from _lib_aggregators import _aggregate_codex_buckets, codex_path_scope
from _lib_five_hour import _FIVE_HOUR_JITTER_FLOOR_SECONDS
from _lib_source_analytics import (
    assign_collision_safe_project_labels,
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


_CODEX_QUOTA_OBSERVATION_CACHE: dict[object, tuple[object, ...]] = {}


def reset_codex_quota_observation_cache() -> None:
    """Clear #582's value-only quota-read memo."""
    _CODEX_QUOTA_OBSERVATION_CACHE.clear()


def _cached_codex_quota_observations(**kwargs) -> tuple[object, ...]:
    """Reuse bounded quota reads while their dedicated mutation stream is idle."""
    conn = kwargs.get("cache_conn")
    if not isinstance(conn, sqlite3.Connection):
        return load_codex_quota_observations(**kwargs)
    try:
        seq_row = conn.execute(
            "SELECT seq FROM sqlite_sequence "
            "WHERE name='quota_window_change_log'"
        ).fetchone()
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='quota_window_change_log'"
        ).fetchone()
        revision_row = conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_window_attribution_revision'"
        ).fetchone()
        db_path = next(
            str(row[2]) for row in conn.execute("PRAGMA database_list")
            if str(row[1]) == "main"
        )
    except (sqlite3.Error, StopIteration):
        return load_codex_quota_observations(**kwargs)
    if table is None:
        return load_codex_quota_observations(**kwargs)
    roots = kwargs.get("source_root_keys")
    physical_signatures = kwargs.get("physical_signatures")
    physical_groups = kwargs.get("physical_groups")
    key = (
        id(load_codex_quota_observations),
        db_path,
        0 if seq_row is None else int(seq_row[0]),
        "" if revision_row is None else str(revision_row[0]),
        None if roots is None else tuple(sorted(str(root) for root in roots)),
        kwargs.get("captured_at_or_after"),
        kwargs.get("active_at"),
        kwargs.get("max_rows"),
        None if physical_signatures is None else tuple(sorted(
            (str(name), str(value))
            for name, value in physical_signatures.items()
        )),
        kwargs.get("canonical_resets_between"),
        None if physical_groups is None else tuple(sorted(physical_groups)),
        bool(kwargs.get("latest_per_identity", False)),
    )
    cached = _CODEX_QUOTA_OBSERVATION_CACHE.get(key)
    if cached is not None:
        return cached
    loaded = tuple(load_codex_quota_observations(**kwargs))
    if len(_CODEX_QUOTA_OBSERVATION_CACHE) >= 32:
        _CODEX_QUOTA_OBSERVATION_CACHE.clear()
    _CODEX_QUOTA_OBSERVATION_CACHE[key] = loaded
    return loaded
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
    # #350: whether this boundary won the §3.2 ranking on STALE evidence (no
    # fresh boundary existed for the account). Backward-looking actuals stay
    # bounded, but the hero discloses it through ``hero.cycle_freshness``.
    evidence_stale: bool = False


@dataclass(frozen=True)
class CodexWeeklyPeriod:
    """One non-overlapping observed native seven-day quota cycle segment."""

    start_at: dt.datetime
    end_at: dt.datetime
    source_root_keys: tuple[str, ...]
    used_percent: float | None = None
    account_keys: tuple[str, ...] = ()


def _codex_history_row_is_model_scoped(row: object) -> bool:
    """Whether a SERIALIZED Codex quota history row sits outside account quota.

    The single predicate both the initial build (``_quota_read_model``) and the
    idle refresh (``refresh_codex_source_clock``) consult, so the two paths
    cannot drift apart: the build stamps ``model_scoped`` from
    ``codex_history_is_model_scoped`` and then asks THIS function about the row
    it just built, and the refresh — which only ever sees the serialized row —
    asks the same function. Fixing one path and not the other is exactly how
    the quota summary and its idle refresh would disagree (#373 spec §7.2).

    The key is additive and OMITTED when false (spec §7.3), so a row without it
    is standard account quota and every fixture that has no model-scoped window
    serializes byte-identically.
    """
    return bool(isinstance(row, Mapping) and row.get("model_scoped"))


def _active_row_from_history(
    history_row: Mapping[str, object], *, now_utc: dt.datetime,
) -> dict[str, object] | None:
    """Project a serialized quota history row onto its ``summary.active[]`` row.

    #429 §4.1. The ONE home for the active-window predicate and the active-row
    shape, so the initial build and ``refresh_codex_source_clock`` cannot
    disagree about what ``captured_at`` means — the defect this fixes. Callers
    must keep no separate copy of any part of the predicate, including the #373
    model-pool exclusion: a live Spark/foreign-pool window must never reach an
    account-level aggregate.

    Both call sites see the same serialized shape. The clock's forecast refresh
    rewrites ``status``, ``remaining_seconds`` and ``projected_percent`` but
    never ``current_percent`` or ``resets_at``, so the fields read here are
    identical at build time and at every tick.

    #428: the liveness predicate and the emitted ``resets_at`` are the SAME
    ``forecast.resets_at``, which is ``baseline.canonical_resets_at``
    (``_lib_quota.forecast_quota``). The client compares ``active[].resets_at``
    against ``hero.cycle.resets_at`` (``activeWeeklyKeys``) to decide which
    weekly history is the live one, so both must carry that one anchor.
    """
    if _codex_history_row_is_model_scoped(history_row):
        return None
    forecast = history_row.get("forecast")
    if not isinstance(forecast, Mapping):
        return None
    current = forecast.get("current_percent")
    if not isinstance(current, (int, float)) or isinstance(current, bool):
        return None
    resets_at = forecast.get("resets_at")
    try:
        reset = dt.datetime.fromisoformat(
            str(resets_at).replace("Z", "+00:00")
        ).astimezone(UTC)
    except (TypeError, ValueError):
        return None
    if reset <= now_utc:
        return None
    row: dict[str, object] = {
        "key": history_row.get("key"),
        "current_percent": current,
        # #429 §3: evidence recency — the newest PHYSICAL observation, the same
        # one that produced this row's `freshness` and `stale_after_seconds`.
        # Not the interpreted baseline, which is a value axis and belongs to
        # `current_percent` alone.
        "captured_at": history_row.get("captured_at"),
        "resets_at": resets_at,
        "freshness": history_row.get("freshness"),
        "stale_after_seconds": history_row.get("stale_after_seconds"),
    }
    account_key = history_row.get("account_key")
    if account_key:
        row["account_key"] = account_key
    return row


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

    #350 — FRESH-FIRST ranking (spec §3.2). Codex has no background quota poll,
    so ``stale_after_seconds(10_080) == 3600`` makes an idle weekly observation
    stale after exactly one hour. Discarding a stale-but-FUTURE boundary blanked
    the hero's backward-looking actuals even though the spend was never lost, so
    each account's future weekly boundaries are now collected into a fresh set
    and a stale set and ranked:

    1. exactly one FRESH boundary -> valid, cycle fresh;
    2. else, no fresh boundaries and exactly one STALE boundary -> valid, cycle
       stale (``CodexCycleBoundary.evidence_stale``, surfaced as the additive
       ``hero.cycle_freshness``);
    3. else -> invalid, with today's ``conflicting``/``stale``/``missing`` reason.

    Fresh-first ordering is load-bearing: a flat count over the union would
    regress one-fresh-plus-one-stale, which resolves valid today. Only the EXACT
    ``"stale"`` freshness state is eligible — ``"future"`` (a capture ahead of
    ``now``) and ``"unavailable"`` stay invalid and keep today's reason.
    """
    fresh_by_account: dict[str, dict[tuple[int, dt.datetime], list[tuple[object, object]]]] = {}
    stale_by_account: dict[str, dict[tuple[int, dt.datetime], list[tuple[object, object]]]] = {}
    accounts_seen: set[str] = set()
    ineligible_by_account: dict[str, bool] = {}
    for history in build_history(tuple(observations)):
        if history.identity.window_minutes != 10_080:
            continue
        # The baseline observation is the §7.1 label authority, so it is
        # resolved BEFORE classification wherever the call site has one.
        baseline = select_baseline(history.observations, now_utc)
        if codex_history_is_model_scoped(history, baseline=baseline):
            continue
        account = history.identity.account_key
        accounts_seen.add(account)
        # #428: the CANONICAL anchor, never the raw provider reset. Blocks and
        # milestones are keyed on the anchor (#416 §4.1), so publishing the raw
        # value here mints a second spelling of one window — and the client's
        # current-cycle milestone filter matches `resets_at` exactly, so the
        # ladder empties for precisely the jittered cycles canonicalization
        # exists to collapse. Liveness rides the same instant the hero shows.
        if baseline is None or baseline.canonical_resets_at <= now_utc:
            continue
        state = quota_freshness(history.physical_observations, now_utc).state
        boundary = (history.identity.window_minutes, baseline.canonical_resets_at)
        if state == "fresh":
            bucket = fresh_by_account
        elif state == "stale":
            bucket = stale_by_account
        else:
            # "future"/"unavailable" evidence is never a stale fallback; it keeps
            # today's non-fresh reason so the envelope degrades exactly as before.
            ineligible_by_account[account] = True
            continue
        bucket.setdefault(account, {}).setdefault(boundary, []).append((history, baseline))
    cycles: list[CodexCycleBoundary] = []
    reasons: list[str] = []
    for account in sorted(accounts_seen | set(fresh_by_account) | set(stale_by_account)):
        fresh_boundaries = fresh_by_account.get(account, {})
        stale_boundaries = stale_by_account.get(account, {})
        if len(fresh_boundaries) == 1:
            boundaries, evidence_stale = fresh_boundaries, False
        elif not fresh_boundaries and len(stale_boundaries) == 1:
            boundaries, evidence_stale = stale_boundaries, True
        else:
            # Within one account: >=2 fresh, or no fresh and >=2 stale ->
            # conflicting; nothing eligible -> today's stale/missing reason.
            reasons.append(
                "conflicting" if (fresh_boundaries or stale_boundaries)
                else ("stale" if ineligible_by_account.get(account) else "missing")
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
            evidence_stale=evidence_stale,
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


def resolve_codex_cycle_detail_identity(
    cache_conn,
    *,
    source_root_keys: Iterable[str],
    now_utc: dt.datetime,
    account_key: str | None = None,
):
    """The live-cycle identity for a per-request Codex cycle-DETAIL read (#373).

    The cycle INDEX is built with the hero's live ``CodexCycleBoundary``; the
    detail route runs outside the snapshot build and has no envelope, so it used
    to pass a stub carrying no ``resets_at``. The former future-reset proxy then
    reported ``is_current: true`` for every future-ending cycle — including a
    historic one that an early re-anchor had already closed. Resolving the same
    boundary here is what makes one cycle key describe one cycle on both routes.

    ``source_root_keys`` scopes the returned identity's cycle LOOKUP (the caller
    passes every retained Codex root, so a just-closed cycle stays fetchable);
    the live boundary itself is resolved from the active roots' observations,
    exactly as the source build does.

    ``account_key`` (#416 QA sweep) picks the LIVE boundary belonging to the
    account the route is focused on. ``_resolve_codex_weekly_cycle`` returns one
    boundary per account and this function used ``cycles[0]`` — the first
    account by sorted key — unconditionally, while ``build_codex_cycle_detail``
    was already given the account predicate. So a focused read enumerated
    account B's cycles and judged them against account A's reset: no candidate
    falls inside ``CODEX_CYCLE_JITTER_FLOOR_SECONDS`` of a foreign boundary, so
    ``_select_live_physical_cycle`` returned ``None`` and B's own live cycle
    lost both its ``is_current`` flag and the §7.4 no-clip guard — the exact
    index/detail disagreement #373 closed, re-opened one account over.

    An account with no live weekly cycle resolves to NO boundary rather than a
    sibling's: an unarmed guard is today's honest degrade, a foreign boundary is
    a wrong answer. ``None`` keeps the merged representative and is byte-stable.

    Degrades to a bare-roots identity — today's behaviour — whenever no live
    cycle resolves. The clip guard stays unarmed on that identity by design
    (``_boundary_has_live_reset``), so the detail keeps clipping as it did
    before #373 rather than trusting the proxy.
    """
    identity = SimpleNamespace(
        source_root_keys=tuple(source_root_keys),
        resets_at=None,
        quota_identity=None,
    )
    if cache_conn is None:
        return identity
    try:
        active_roots = tuple(sorted(
            str(row[0]) for row in cache_conn.execute(
                "SELECT source_root_key FROM codex_source_roots"
            )
        ))
        if not active_roots:
            return identity
        observations = _cached_codex_quota_observations(
            source_root_keys=active_roots,
            cache_conn=cache_conn,
            captured_at_or_after=(
                now_utc - dt.timedelta(days=DASHBOARD_QUOTA_RECENT_DAYS)
            ),
            active_at=now_utc,
            max_rows=DASHBOARD_QUOTA_OBSERVATION_LIMIT,
        )
        cycles = _resolve_codex_weekly_cycle(observations, now_utc)
    except (sqlite3.Error, CodexCycleUnavailable, ValueError):
        return identity
    if not cycles:
        return identity
    if account_key is None:
        boundary = cycles[0]
    else:
        boundary = next(
            (
                cyc for cyc in cycles
                if (
                    cyc.quota_identity.account_key
                    if cyc.quota_identity is not None
                    else _lib_accounts.UNATTRIBUTED
                ) == account_key
            ),
            None,
        )
        if boundary is None:
            return identity
    identity.resets_at = boundary.resets_at
    identity.quota_identity = boundary.quota_identity
    return identity


def _codex_next_decision_at(
    observations: Iterable[object],
    cycles: Iterable[CodexCycleBoundary],
    now_utc: dt.datetime,
) -> dt.datetime | None:
    """The earliest future instant at which weekly-cycle resolution can change.

    #350 spec §3.3. Cycle validity is time-dependent even on FROZEN evidence:
    ``_resolve_codex_weekly_cycle`` passes ``now_utc`` to both ``select_baseline``
    (a future-dated capture becomes baseline-eligible purely because time passed,
    which can switch the selected reset) and ``quota_freshness`` (fresh flips to
    stale as age crosses ``stale_after_seconds``). One fresh plus one stale
    boundary resolves FRESH today and ``conflicting`` an hour later on the very
    same rows. The idle clock cannot re-resolve that itself — the public
    histories it sees are capped at ``SOURCE_HISTORY_LIMIT`` and omit
    ``logical_limit_key`` (§2.3) — so build time instead records WHEN the clock
    must stop trusting its verdict, and the tick rebuilds authoritatively at the
    crossing. That is one rebuild per deadline (a handful per weekly cycle), not
    one per tick.

    The deadline is the ``min`` of three candidate kinds, dropping any candidate
    at or before ``now_utc``:

    1. every selected cycle's ``resets_at`` (expiry — including the
       #341 multi-account case where account A expires while B stays live);
    2. ``latest_physical_capture + stale_after_seconds(window)`` for every weekly
       history with a live baseline (fresh -> stale). This is a deliberate
       SUPERSET of the §3.2 ranking participants — it also covers histories whose
       freshness is ``"future"``/``"unavailable"`` and so never enter the ranking
       — because an extra candidate can only pull the deadline EARLIER, and an
       earlier deadline is always the conservative direction;
    3. the ``captured_at`` of any future-dated weekly observation
       (future -> fresh / baseline eligibility).

    Returns ``None`` when nothing can flip. Server-only: it rides ``clock_data``
    and never reaches the public source envelope.
    """
    candidates: list[dt.datetime] = []
    for cycle in cycles:
        candidates.append(cycle.resets_at.astimezone(UTC))
    for history in build_history(tuple(observations)):
        if history.identity.window_minutes != 10_080:
            continue
        baseline = select_baseline(history.observations, now_utc)
        if codex_history_is_model_scoped(history, baseline=baseline):
            continue
        # #428: the anchor, so the decision deadline describes the same window
        # the hero publishes rather than a jitter sibling up to 600s away.
        if baseline is not None and baseline.canonical_resets_at > now_utc:
            latest = latest_physical_observation(history.physical_observations)
            if latest is not None:
                candidates.append(
                    latest.captured_at.astimezone(UTC)
                    + dt.timedelta(
                        seconds=stale_after_seconds(history.identity.window_minutes),
                    )
                )
        # A capture ahead of ``now`` is not baseline-eligible yet, so this runs
        # even for a history with no live baseline at all.
        for observation in (
            *history.observations, *history.physical_observations,
        ):
            captured_at = observation.captured_at.astimezone(UTC)
            if captured_at > now_utc:
                candidates.append(captured_at)
    live = [candidate for candidate in candidates if candidate > now_utc]
    return min(live) if live else None


def codex_decision_deadline_passed(
    state: object,
    now_utc: dt.datetime,
) -> bool:
    """Whether a published Codex state's cycle decision deadline has elapsed.

    #350 spec §3.3. When this holds the tick MUST rebuild Codex authoritatively
    via ``build_codex_source_state`` — bypassing both the idle clock and
    ``reuse_coherent_source_state`` — because the frozen evidence would now
    resolve to a different cycle (or to none). A state with no recorded deadline
    (``None``, or an older generation that predates the field) never forces a
    rebuild; the clock's expiry guard remains its safety net.
    """
    clock_data = getattr(state, "clock_data", None)
    if not isinstance(clock_data, Mapping):
        return False
    deadline = clock_data.get("codex_next_decision_at")
    if not isinstance(deadline, dt.datetime):
        return False
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        return False
    return now_utc.astimezone(UTC) >= deadline.astimezone(UTC)


def _codex_weekly_periods(
    stats_conn: sqlite3.Connection,
    *,
    source_root_keys: Iterable[str],
    active_cycle: CodexCycleBoundary | None,
    account_key: str | None = None,
) -> tuple[CodexWeeklyPeriod, ...]:
    """Read durable 10,080-minute boundaries and clip early re-anchors.

    A provider-granted reset changes the native window's nominal start before
    the prior seven-day deadline.  Sorting those nominal starts and ending the
    prior segment at the next start preserves the actual quota-cycle boundary
    without double-counting the overlapping nominal windows.

    ``account_key`` (#416 Slice 3A review B1) scopes the read to ONE account.
    ``quota_window_blocks`` is ``UNIQUE(source, source_root_key, account_key,
    logical_limit_key, observed_slot, window_minutes, resets_at_utc)``, so two
    accounts on one root genuinely produce two weekly rows; without the
    predicate the jitter merge below pools their ``current_percent`` values and
    ``max(...)`` hands the focused account the OTHER account's percentage —
    the never-combine violation D6 forbids — while ``end_at = min(resets_at,
    next_start)`` clips one account's week at the other's start. ``None`` keeps
    the merged "All accounts" read used by the parent. Every merged boundary
    retains the account-key set that contributed its percentage; the parent
    publishes that set only under the existing multi-account decoration gate.

    The predicate is strict equality, deliberately NOT the one-directional
    ``(account, unattributed)`` widening ``_codex_five_hour_rows`` uses: that
    widening produces a LISTING whose members each keep their own percentage,
    whereas the merge here ADOPTS a pooled percentage onto one account's row.
    Unattributed weekly boundaries are already rendered by the ``unattributed``
    child, which is a first-class scope after D1.
    """
    roots = tuple(sorted({
        root for root in source_root_keys if isinstance(root, str) and root
    }))
    if not roots:
        return ()
    placeholders = ",".join("?" for _ in roots)
    # `quota_window_blocks.account_key` is `NOT NULL DEFAULT 'unattributed'`,
    # so the sentinel needs no NULL branch here (unlike the cache tables).
    account_predicate = "" if account_key is None else "AND account_key = ? "
    account_params: tuple = () if account_key is None else (account_key,)
    # BEFORE the `try`, per #496 S5b section 4.7: beneath that handler a denial
    # would be rendered as empty data rather than as an error.
    assert_projection_readable(stats_conn)
    try:
        rows = stats_conn.execute(
            "SELECT source_root_key, account_key, logical_limit_key, limit_name, "
            "resets_at_utc, nominal_start_at_utc, current_percent "
            "FROM quota_window_blocks "
            "WHERE source='codex' AND window_minutes=10080 "
            f"AND source_root_key IN ({placeholders}) AND orphaned_at IS NULL "
            f"{account_predicate}"
            "ORDER BY nominal_start_at_utc DESC, resets_at_utc DESC, source_root_key "
            "LIMIT ?",
            (*roots, *account_params, SOURCE_HISTORY_LIMIT),
        ).fetchall()
    except sqlite3.Error:
        rows = ()

    # #373 §7.4: the fifth element marks the LIVE boundary, which is never
    # clipped by a successor. Durable rows are never live on their own — only
    # the caller's `active_cycle` is — but the jitter-merge below folds the
    # live boundary together with its own durable row, so the flag is OR-ed on
    # merge rather than taken from either side.
    raw_boundaries: list[
        tuple[dt.datetime, dt.datetime, set[str], list[float], set[str], bool]
    ] = []

    for (root_key, row_account_key, logical_limit_key, limit_name, resets_at_raw,
         start_at_raw, current_percent) in rows:
        if is_model_scoped_codex_quota(logical_limit_key, limit_name):
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
        raw_boundaries.append((
            start_at,
            resets_at,
            {str(root_key)},
            used_values,
            {str(row_account_key or _lib_accounts.UNATTRIBUTED)},
            False,
        ))

    # `active_cycle is None` is the case §7.4 calls out explicitly: no boundary
    # is live, so nothing is exempt and every period clips exactly as before.
    if active_cycle is not None:
        raw_boundaries.append((
            active_cycle.start_at.astimezone(UTC),
            active_cycle.resets_at.astimezone(UTC),
            set(active_cycle.source_root_keys),
            [active_cycle.used_percent] if active_cycle.used_percent is not None else [],
            {
                active_cycle.quota_identity.account_key
                if active_cycle.quota_identity is not None
                else _lib_accounts.UNATTRIBUTED
            },
            True,
        ))

    ordered: list[
        tuple[dt.datetime, dt.datetime, set[str], list[float], set[str], bool]
    ] = []
    for start_at, resets_at, period_roots, used_values, period_accounts, is_live in sorted(
        raw_boundaries, key=lambda item: (item[0], item[1]),
    ):
        if (
            ordered
            and (start_at - ordered[-1][0]).total_seconds()
            < _FIVE_HOUR_JITTER_FLOOR_SECONDS
        ):
            (
                first_start, latest_reset, existing_roots, existing_used,
                existing_accounts, existing_live,
            ) = ordered[-1]
            existing_roots.update(period_roots)
            existing_used.extend(used_values)
            existing_accounts.update(period_accounts)
            ordered[-1] = (
                first_start, max(latest_reset, resets_at), existing_roots, existing_used,
                existing_accounts, existing_live or is_live,
            )
        else:
            ordered.append((
                start_at, resets_at, set(period_roots), list(used_values),
                set(period_accounts), is_live,
            ))
    periods: list[CodexWeeklyPeriod] = []
    for index, (
        start_at, resets_at, period_roots, used_values, period_accounts, is_live,
    ) in enumerate(ordered):
        next_start = ordered[index + 1][0] if index + 1 < len(ordered) else None
        # The live cycle always ends at its own reset (#373 §7.4).
        if is_live:
            next_start = None
        end_at = min(resets_at, next_start) if next_start is not None else resets_at
        if end_at <= start_at:
            continue
        periods.append(CodexWeeklyPeriod(
            start_at=start_at,
            end_at=end_at,
            source_root_keys=tuple(sorted(period_roots)),
            used_percent=max(used_values) if used_values else None,
            account_keys=tuple(sorted(period_accounts)),
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
    # #556 S5 §3.4 — the Claude half of the same budget block, resolved through
    # the same `_get_budget_config` call so the validator's warn-and-ignore
    # stderr line is emitted ONCE per tick rather than once per consumer.
    # Always a mapping (the validator fills defaults); `weekly_usd` is `None`
    # when nothing is configured.
    claude_budget: Mapping[str, object] | None = None


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
        raw_cache_report.get("anomaly_threshold_pp")
        if isinstance(raw_cache_report, Mapping) else None
    )
    # #443 S3 F17: one definition of what the setting means, shared with
    # the Claude read path and the persistence gate. An absent key
    # reaches the resolver as None and defaults there, not here.
    cache_threshold = c._load_sibling(
        "_lib_cache_report"
    ).resolve_cache_report_threshold(raw_cache_threshold)
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
        claude_budget=MappingProxyType({
            name: value for name, value in budget_config.items()
            if name != "codex"
        }),
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

# #556 S2: additional collections a resource may ALSO be routed through.
#
# The primary above stays the capability gate — its absence is still
# `SourceCapabilityUnavailable`. These are searched only after the primary
# misses, and their own absence is an ordinary not-found rather than a
# capability failure, because a provider legitimately need not publish them (a
# Codex source has no aggregate sibling, and a Claude source whose bounded fold
# failed publishes none either).
#
# Projects needs one because `projects.rows` is the current SUBSCRIPTION WEEK
# while `projects.aggregate.rows` is folded over the thirty-day shared range.
# Without this the aggregate ranking would publish rows the drill-down route
# answers 404 for — in the committed `all-combined` fixture, four of six.
_RESOURCE_EXTRA_ROWS: Mapping[str, tuple[tuple[str, ...], ...]] = MappingProxyType({
    "project": (("projects", "aggregate", "rows"),),
})


def _rows_at(data: Mapping, path: "tuple[str, ...]") -> "list | tuple":
    """Read one optional nested rows collection, or an empty tuple."""
    node: object = data
    for step in path:
        if not isinstance(node, Mapping):
            return ()
        node = node.get(step)
    return node if isinstance(node, (list, tuple)) else ()


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
        # Only after the primary collection misses, so every key that resolves
        # today keeps resolving to the same row. The account-ownership branch
        # below does NOT see an unchanged candidate set — a key that resolves
        # only here reaches it as a row the primary collection never carried.
        # That leaks nothing, because an aggregate row carries no
        # `account_key` and the ownership check refuses a row it cannot
        # attribute, but the set is genuinely wider than it was.
        for path in _RESOURCE_EXTRA_ROWS.get(resource, ()):
            key_matches = [
                row for row in _rows_at(data, path)
                if isinstance(row, Mapping) and row.get("key") == key
            ]
            if key_matches:
                break
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
    # BEFORE the `try`, per #496 S5b section 4.7, and for the same reason every
    # other gated site places it there: the handler below catches
    # `ValueError`/`TypeError` as well as `sqlite3.Error`, so a refusal raised
    # inside it would be rendered as `ProjectionCoherence(False,
    # "projection_read_failed")` — a degraded-data verdict instead of a denial.
    assert_projection_readable(context.stats_conn)
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


# #556 S5 Unit 2 review F13 — one-shot per process, following the repo's
# established warn-once pattern (`_CONFIG_CORRUPT_WARNED` in
# `bin/_cctally_config.py`, `_DISPLAY_TZ_BAD_CONFIG_WARNED` in
# `bin/_lib_display_tz.py`). Both call sites below run on EVERY dashboard
# rebuild tick, so an unresolvable window — a bad `display.tz` or `period` is
# not transient — wrote a full traceback per tick indefinitely. The condition
# is already visible to the user: the same failure nulls the budget status and
# publishes `budget_compute_failed`.
_CODEX_BUDGET_WINDOW_WARNED: set[str] = set()


def _warn_codex_budget_window_once(site: str) -> None:
    """Log an unresolvable Codex budget window once PER CALL SITE.

    Keyed by site, not by module. A single flag shared by both callers would let
    whichever failed first permanently silence the other, and the two are not
    interchangeable: one degrades the accounting range, the other is the site
    that used to destroy the whole Codex provider. The site also reaches the
    message, so a reader of the log knows which one they are looking at.
    """
    if site in _CODEX_BUDGET_WINDOW_WARNED:
        return
    _CODEX_BUDGET_WINDOW_WARNED.add(site)
    _lib_log.get_logger("dashboard").error(
        "codex budget window could not be resolved at %s "
        "(logged once per process per site)", site, exc_info=True,
    )


def _codex_budget_cost_events(
    context: DashboardReadContext,
    entries: Iterable[object],
) -> tuple[tuple[dt.datetime, float], ...]:
    """Freeze every configured-window cost event for exact idle pace updates.

    #556 S5 §3.5: window resolution is guarded here as well as in
    ``_codex_budget_status_domain``. This runs FIRST at the build site, so an
    unresolvable period raising out of it would take the provider down before
    the status helper's own boundary could name the reason. Degrading to no
    events cannot publish a false ``$0``, because the same failure reaches the
    status helper and nulls the status instead.
    """
    if context.codex_budget is None:
        return ()
    try:
        _period, start_at, end_at = _configured_codex_budget_window(context)
    except Exception:
        _warn_codex_budget_window_once("cost_events")
        return ()
    c = sys.modules["cctally"]
    events: list[tuple[dt.datetime, float]] = []
    for entry in entries:
        timestamp = getattr(entry, "timestamp", None)
        if not isinstance(timestamp, dt.datetime):
            continue
        timestamp = timestamp.astimezone(UTC)
        if not start_at <= timestamp < end_at:
            continue
        loaded_cost = getattr(entry, "cost_usd", None)
        events.append((
            timestamp,
            float(loaded_cost) if loaded_cost is not None else
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
    account_keys = getattr(bucket, "account_keys", ())
    if account_keys:
        result["account_keys"] = tuple(account_keys)
    return result


def _period_wire(view: Any) -> dict[str, object]:
    return {
        "rows": tuple(_bucket_wire(row) for row in view.rows),
        "total_cost_usd": view.total_cost_usd,
        "total_tokens": view.total_tokens,
        "display_tz": view.display_tz_label,
    }


_CODEX_PERIOD_VIEW_CACHE: dict[object, tuple] = {}
_CODEX_CACHE_REPORT_ROWS: dict[object, tuple] = {}
_CODEX_SESSION_VIEW_CACHE: dict[object, tuple[object, Any]] = {}


def _codex_session_row_key(row: object) -> tuple[str, str]:
    return (
        str(getattr(row, "codex_root", "") or ""),
        str(getattr(row, "session_id_path", "") or ""),
    )


def _codex_session_path_key(source_path: str) -> tuple[str, str]:
    import _lib_aggregators
    id_path, _file_name, _directory = _lib_aggregators._session_path_parts(
        source_path,
    )
    suffix = id_path + ".jsonl"
    root_prefix = (
        source_path[: -len(suffix)]
        if source_path.endswith(suffix) else source_path
    )
    return (
        _lib_aggregators._codex_home_root_from_prefix(root_prefix), id_path,
    )


def _codex_incremental_entry_order(entry: object) -> tuple[object, str, str, int]:
    """Canonical accounting encounter key retained by incremental groups."""
    return (
        getattr(entry, "timestamp"),
        str(getattr(entry, "source_root_key", "") or ""),
        str(getattr(entry, "conversation_key", "") or ""),
        int(getattr(entry, "cache_entry_id", 0) or 0),
    )


def _cached_codex_session_view(
    entries: Iterable[CodexEntry],
    *,
    changed_old: Iterable[object],
    changed_new: Iterable[object],
    cache_key: object,
    semantic_signature: object,
    now_utc: dt.datetime,
    tz_name: str | None,
    speed: str,
) -> Any:
    """Rebuild only session-file groups touched by accounting changes."""
    values = tuple(entries)
    signature = (semantic_signature, tz_name, speed)
    state = _CODEX_SESSION_VIEW_CACHE.get(cache_key)
    if state is None or state[0] != signature:
        view = build_codex_session_view(
            values, now_utc=now_utc, tz_name=tz_name, speed=speed,
        )
        groups: dict[tuple[str, str], list[CodexEntry]] = {}
        for entry in values:
            groups.setdefault(
                _codex_session_path_key(entry.source_path), [],
            ).append(entry)
        _CODEX_SESSION_VIEW_CACHE[cache_key] = (
            signature, view,
            {key: tuple(group) for key, group in groups.items()},
        )
        return view
    changed_paths = {
        str(getattr(entry, "source_path", "") or "")
        for entry in (*tuple(changed_old), *tuple(changed_new))
        if getattr(entry, "source_path", None)
    }
    if not changed_paths:
        return state[1]
    affected_keys = {
        _codex_session_path_key(source_path) for source_path in changed_paths
    }
    old_ids = {
        int(getattr(entry, "cache_entry_id", 0) or 0)
        for entry in changed_old
    }
    groups = dict(state[2])
    for key in affected_keys:
        groups[key] = tuple(
            entry for entry in groups.get(key, ())
            if int(getattr(entry, "cache_entry_id", 0) or 0) not in old_ids
        )
    for entry in changed_new:
        key = _codex_session_path_key(entry.source_path)
        groups[key] = (*groups.get(key, ()), entry)
    for key in affected_keys:
        if groups.get(key):
            groups[key] = tuple(sorted(
                groups[key], key=_codex_incremental_entry_order,
            ))
        else:
            groups.pop(key, None)
    partial_entries = tuple(sorted(
        (
            entry for key in affected_keys for entry in groups.get(key, ())
        ),
        key=_codex_incremental_entry_order,
    ))
    partial = build_codex_session_view(
        partial_entries,
        now_utc=now_utc, tz_name=tz_name, speed=speed,
    )
    encounter_order = {
        key: _codex_incremental_entry_order(group[0])
        for key, group in groups.items() if group
    }
    rows_by_encounter = sorted(
        (
            *(row for row in state[1].rows
              if _codex_session_row_key(row) not in affected_keys),
            *partial.rows,
        ),
        key=lambda row: encounter_order.get(
            _codex_session_row_key(row),
            (dt.datetime.max.replace(tzinfo=UTC), "", "", 0),
        ),
    )
    # The canonical aggregator performs a stable descending timestamp sort, so
    # establish first-encounter order before applying that same stable sort.
    rows = tuple(sorted(
        rows_by_encounter, key=lambda row: row.last_activity, reverse=True,
    ))
    total_cost = 0.0
    total_tokens = 0
    for row in rows:
        total_cost += row.cost_usd
        total_tokens += row.total_tokens
    view = replace(
        state[1], rows=rows, total_sessions=len(rows),
        total_cost_usd=total_cost,
        total_tokens=total_tokens,
        period_start=(min((row.last_activity for row in rows), default=None)),
        period_end=now_utc,
    )
    _CODEX_SESSION_VIEW_CACHE[cache_key] = (signature, view, groups)
    return view


def _codex_period_bucket(
    entry: CodexEntry, *, kind: str, tz_name: str | None,
) -> str:
    zone = ZoneInfo(tz_name) if tz_name else None
    local = entry.timestamp.astimezone(zone) if zone else entry.timestamp.astimezone()
    return local.strftime("%Y-%m-%d" if kind == "daily" else "%Y-%m")


def _cached_codex_period_view(
    entries: Iterable[CodexEntry],
    *,
    changed_old: Iterable[CodexEntry],
    changed_new: Iterable[CodexEntry],
    kind: str,
    cache_key: object,
    semantic_signature: object,
    now_utc: dt.datetime,
    tz_name: str | None,
    speed: str,
) -> Any:
    """Rebuild only date/month buckets touched by changed accounting rows."""
    values = tuple(entries)
    signature = (semantic_signature, kind, tz_name, speed)
    state = _CODEX_PERIOD_VIEW_CACHE.get((cache_key, kind))
    builder = build_codex_daily_view if kind == "daily" else build_codex_monthly_view
    if state is None or state[0] != signature:
        view = builder(values, now_utc=now_utc, tz_name=tz_name, speed=speed)
        groups: dict[str, list[CodexEntry]] = {}
        for entry in values:
            groups.setdefault(
                _codex_period_bucket(entry, kind=kind, tz_name=tz_name), [],
            ).append(entry)
        _CODEX_PERIOD_VIEW_CACHE[(cache_key, kind)] = (signature, view, groups)
        return view
    old_values = tuple(changed_old)
    new_values = tuple(changed_new)
    affected = {
        _codex_period_bucket(entry, kind=kind, tz_name=tz_name)
        for entry in (*old_values, *new_values)
    }
    if not affected:
        return state[1]
    prior = state[1]
    # Copy the mapping only. Unaffected bucket lists are immutable-by-contract
    # and remain shared; affected buckets below receive fresh lists.
    groups = dict(state[2])
    old_ids = {int(entry.cache_entry_id) for entry in old_values}
    for label in affected:
        groups[label] = [
            entry for entry in groups.get(label, ())
            if int(entry.cache_entry_id) not in old_ids
        ]
    for entry in new_values:
        label = _codex_period_bucket(entry, kind=kind, tz_name=tz_name)
        groups.setdefault(label, []).append(entry)
    for label in affected:
        groups[label].sort(key=lambda entry: (
            entry.timestamp, entry.source_root_key,
            entry.conversation_key, int(entry.cache_entry_id),
        ))
    replacements: dict[str, object] = {}
    for label in affected:
        partial = builder(
            tuple(groups[label]),
            now_utc=now_utc, tz_name=tz_name, speed=speed,
        )
        if partial.rows:
            replacements[label] = partial.rows[0]
        else:
            groups.pop(label, None)
    rows = tuple(sorted(
        (
            *(row for row in prior.rows if row.bucket not in affected),
            *replacements.values(),
        ),
        key=lambda row: row.bucket,
    ))
    total_cost = 0.0
    total_tokens = 0
    for row in rows:
        total_cost += row.cost_usd
        total_tokens += row.total_tokens
    period_start = None
    if rows:
        if kind == "daily":
            first = dt.date.fromisoformat(rows[0].bucket)
            period_start = dt.datetime.combine(first, dt.time.min, tzinfo=UTC)
        else:
            year, month = rows[0].bucket.split("-")
            period_start = dt.datetime(int(year), int(month), 1, tzinfo=UTC)
    view = replace(
        prior,
        rows=rows,
        total_cost_usd=total_cost,
        total_tokens=total_tokens,
        period_start=period_start,
        period_end=now_utc,
        period_civil_bucket=bool(rows),
    )
    _CODEX_PERIOD_VIEW_CACHE[(cache_key, kind)] = (signature, view, groups)
    return view


def _codex_cache_report_wire(
    entries: Iterable[object],
    *,
    metadata: Mapping[tuple[str, str], Mapping[str, object]],
    now_utc: dt.datetime,
    display_tz_name: str | None,
    speed: str,
    anomaly_threshold_pp: int = 15,
    window_days: int = 14,
    cache_key: object | None = None,
    changed_old: Iterable[object] = (),
    changed_new: Iterable[object] = (),
    semantic_signature: object | None = None,
) -> dict[str, object]:
    """Compute the canonical cache report from Codex's inclusive counters.

    Codex input is cache-inclusive, so the shared cache-report kernel receives
    uncached input plus cached input as two disjoint counters. OpenAI does not
    charge a cache-write premium; the counterfactual is therefore the exact
    uncached-vs-cached input price difference for each token-count event.
    """
    c = sys.modules["cctally"]
    crk = c._load_sibling("_lib_cache_report")
    # #443 S2 F18 — the one serializer shared with the Claude producer.
    wire = c._load_sibling("_lib_cache_report_wire")
    display_tz = ZoneInfo(display_tz_name) if display_tz_name else None
    cutoff = now_utc - dt.timedelta(days=window_days)
    bucket_tz = crk._resolve_bucket_tz(display_tz)

    def _tiered_cost(tokens: int, pricing: Mapping[str, object], base: str, above: str) -> float:
        if tokens <= 0:
            return 0.0
        base_rate = float(pricing.get(base, 0.0) or 0.0)
        above_rate = pricing.get(above)
        threshold = int(c.CODEX_TIERED_THRESHOLD)
        if tokens > threshold and above_rate is not None:
            return threshold * base_rate + (tokens - threshold) * float(above_rate)
        return tokens * base_rate

    def _wrap_entry(entry: object) -> object | None:
        timestamp = getattr(entry, "timestamp", None)
        if not isinstance(timestamp, dt.datetime) or timestamp < cutoff:
            return None
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
        return SimpleNamespace(
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
            _cache_entry_id=int(getattr(entry, "cache_entry_id", 0) or 0),
            _cache_day=timestamp.astimezone(bucket_tz).strftime("%Y-%m-%d"),
            _cache_order=_codex_incremental_entry_order(entry),
            usage={
                "input_tokens": uncached_tokens,
                "output_tokens": int(getattr(entry, "output_tokens", 0)),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cached_tokens,
            },
        )

    def _freeze_day(day_entries: Iterable[object]):
        """Freeze one changed day without re-folding the retained window."""
        day_values = tuple(day_entries)
        rows = crk._aggregate_cache_by_day(
            day_values,
            display_tz=display_tz,
            pricing=c.CODEX_MODEL_PRICING,
            cost_calculator=(
                lambda _model, _usage, _mode, cost: float(cost or 0.0)
            ),
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise AssertionError("one cache-report day produced multiple rows")
        row = rows[0]
        project_nets: dict[str, list[float]] = {}
        project_tokens: dict[str, list[int]] = {}
        for entry in day_values:
            if entry.model == "<synthetic>":
                continue
            project = entry.project_path or "(unknown)"
            project_nets.setdefault(project, []).append(entry.cache_net_usd)
            tokens = project_tokens.setdefault(project, [0, 0, 0])
            tokens[0] += entry.input_tokens
            tokens[1] += entry.cache_creation_tokens
            tokens[2] += entry.cache_read_tokens
        project_partials = tuple(sorted(
            (
                project,
                crk._ProjectPartial(
                    net_usd=stable_sum(project_nets[project]),
                    input_tokens=tokens[0],
                    cache_creation_tokens=tokens[1],
                    cache_read_tokens=tokens[2],
                ),
            )
            for project, tokens in project_tokens.items()
        ))
        return crk.CachedCacheReportDay(
            date=row.date,
            cache_hit_percent=row.cache_hit_percent,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cache_creation_tokens=row.cache_creation_tokens,
            cache_read_tokens=row.cache_read_tokens,
            cost=row.cost,
            saved_usd=row.saved_usd,
            wasted_usd=row.wasted_usd,
            net_usd=row.net_usd,
            model_breakdowns=tuple(
                crk._FrozenModelBreakdown(
                    model_name=model.model_name,
                    input_tokens=model.input_tokens,
                    output_tokens=model.output_tokens,
                    cache_creation_tokens=model.cache_creation_tokens,
                    cache_read_tokens=model.cache_read_tokens,
                    cache_hit_percent=model.cache_hit_percent,
                    cost=model.cost,
                    saved_usd=model.saved_usd,
                    wasted_usd=model.wasted_usd,
                    net_usd=model.net_usd,
                )
                for model in row.model_breakdowns
            ),
            project_partials=project_partials,
        )

    values = tuple(entries)
    cacheable = cache_key is not None and all(
        int(getattr(entry, "cache_entry_id", 0) or 0) for entry in values
    )
    signature = (semantic_signature, speed, window_days, display_tz_name)
    cached_days = None
    if not cacheable:
        wrapped = [
            wrapped_entry for entry in values
            if (wrapped_entry := _wrap_entry(entry)) is not None
        ]
    else:
        state = _CODEX_CACHE_REPORT_ROWS.get(cache_key)
        if state is None or len(state) != 4 or state[0] != signature:
            cached_rows = {}
            for entry in values:
                wrapped_entry = _wrap_entry(entry)
                if wrapped_entry is not None:
                    cached_rows[int(entry.cache_entry_id)] = wrapped_entry
            groups: dict[str, tuple[int, ...]] = {}
            mutable_groups: dict[str, list[int]] = {}
            for entry in cached_rows.values():
                mutable_groups.setdefault(entry._cache_day, []).append(
                    entry._cache_entry_id)
            for day, ids in mutable_groups.items():
                groups[day] = tuple(sorted(
                    ids, key=lambda cache_id: cached_rows[cache_id]._cache_order,
                ))
            cached_days = {
                day: _freeze_day(cached_rows[cache_id] for cache_id in ids)
                for day, ids in groups.items()
            }
        else:
            cached_rows = dict(state[1])
            groups = dict(state[2])
            cached_days = dict(state[3])
            affected_days: set[str] = set()
            old_ids: set[int] = set()
            # The accounting population is wider than the cache-report window.
            # Rows can therefore age out without any ledger mutation. Preserve
            # the old full-fold contract by evicting them whenever a later
            # dirty source build advances `now_utc`.
            for cache_entry_id, prior in tuple(cached_rows.items()):
                if prior.timestamp < cutoff:
                    old_ids.add(cache_entry_id)
                    affected_days.add(prior._cache_day)
                    cached_rows.pop(cache_entry_id, None)
            for entry in changed_old:
                cache_entry_id = int(getattr(entry, "cache_entry_id", 0) or 0)
                if cache_entry_id:
                    old_ids.add(cache_entry_id)
                    prior = cached_rows.get(cache_entry_id)
                    if prior is not None:
                        affected_days.add(prior._cache_day)
                    cached_rows.pop(cache_entry_id, None)
            for entry in changed_new:
                cache_entry_id = int(getattr(entry, "cache_entry_id", 0) or 0)
                if not cache_entry_id:
                    continue
                wrapped_entry = _wrap_entry(entry)
                if wrapped_entry is None:
                    cached_rows.pop(cache_entry_id, None)
                else:
                    cached_rows[cache_entry_id] = wrapped_entry
                    affected_days.add(wrapped_entry._cache_day)
            for day in affected_days:
                groups[day] = tuple(
                    cache_id for cache_id in groups.get(day, ())
                    if cache_id not in old_ids
                )
            for entry in changed_new:
                cache_entry_id = int(getattr(entry, "cache_entry_id", 0) or 0)
                wrapped_entry = cached_rows.get(cache_entry_id)
                if wrapped_entry is not None:
                    groups[wrapped_entry._cache_day] = (
                        *groups.get(wrapped_entry._cache_day, ()), cache_entry_id,
                    )
            for day in affected_days:
                ids = tuple(sorted(
                    dict.fromkeys(groups.get(day, ())),
                    key=lambda cache_id: cached_rows[cache_id]._cache_order,
                ))
                if ids:
                    groups[day] = ids
                    cached_days[day] = _freeze_day(
                        cached_rows[cache_id] for cache_id in ids)
                else:
                    groups.pop(day, None)
                    cached_days.pop(day, None)
        cached_days = {
            day: unit for day, unit in cached_days.items() if unit is not None
        }
        _CODEX_CACHE_REPORT_ROWS[cache_key] = (
            signature, cached_rows, groups, cached_days,
        )
        wrapped = []

    # One current day per invocation (#443 S3 F23): the focal day and the
    # entry filter below both resolve through the SAME zone the kernel
    # buckets by. ``display_tz or UTC`` diverged from host-local bucketing
    # on every non-UTC host, which published a fabricated spotlight and let
    # the breakdowns draw from a different entry population than the days.
    today_iso = now_utc.astimezone(bucket_tz).strftime("%Y-%m-%d")
    if not wrapped and not cached_days:
        # An empty store measured nothing, so ``observed`` is False and
        # every applicable predicate is unevaluated. The client
        # short-circuits on ``is_empty`` before reading either, so this
        # costs nothing and keeps the empty and populated returns one shape.
        return wire.build_cache_report_wire(
            provider="codex", window_days=window_days,
            anomaly_threshold_pp=anomaly_threshold_pp,
            anomaly_window_days=window_days,
            today={
                "date": today_iso, "cache_hit_percent": 0.0,
                "baseline_median_percent": None, "delta_pp": None,
                "net_usd": 0.0, "saved_usd": 0.0, "wasted_usd": 0.0,
                "anomaly_triggered": False, "anomaly_reasons": (),
                "baseline_daily_row_count": 0,
                "anomaly_unevaluated": wire.CODEX_PREDICATES, "observed": False,
            },
            days=(), by_project=(), by_model=(),
            seven_day_net_usd=0.0, seven_day_anomaly_count=0,
            fourteen_day_counterfactual_usd=0.0,
            fourteen_day_efficiency_ratio=0.0, is_empty=True,
        )

    if cached_days is None:
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
    else:
        result = crk.classify_and_summarize(
            [
                crk.reconstruct_cache_row(cached_days[day])
                for day in sorted(cached_days)
            ],
            now_utc=now_utc,
            window_days=window_days,
            anomaly_threshold_pp=anomaly_threshold_pp,
            anomaly_window_days=window_days,
            display_tz=display_tz,
        )
    raw_rows = sorted(result.rows, key=lambda row: row.date or "", reverse=True)
    today_row = next((row for row in raw_rows if row.date == today_iso), None)
    # #443 F13/F14 — both charts label their rightmost element "Today"
    # positionally, so an idle day left the newest REAL row wearing that
    # label while the spotlight published a fabricated 0%. The Claude
    # builder solves this with a synthetic row for exactly this reason;
    # Codex now does the same. Slice AFTER inserting, as Claude does.
    synthetic_today = today_row is None
    day_dicts = [{
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
        "anomaly_unevaluated": tuple(getattr(row, "anomaly_unevaluated", ())),
        "observed": True,
    } for row in raw_rows]
    if synthetic_today:
        day_dicts.insert(0, {
            "date": today_iso, "cache_hit_percent": 0.0,
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_tokens": 0, "cache_read_tokens": 0,
            "saved_usd": 0.0, "wasted_usd": 0.0, "net_usd": 0.0,
            "anomaly_triggered": False, "anomaly_reasons": (),
            "anomaly_unevaluated": wire.CODEX_PREDICATES, "observed": False,
        })
    days = tuple(day_dicts[:window_days])
    # #443 S3 F22: the rows the median was actually taken over, not every
    # non-today row. The old count was unbounded by the baseline window.
    baseline_count = result.today_baseline_sample_count
    baseline = result.today_baseline_median
    today_hit = today_row.cache_hit_percent if today_row else 0.0
    kept_dates = {row["date"] for row in days}
    if cached_days is None:
        kept_entries = [
            entry for entry in wrapped
            if entry.timestamp.astimezone(bucket_tz).strftime("%Y-%m-%d")
            in kept_dates
        ]
        by_project = crk._aggregate_cache_breakdown(
            kept_entries, key_fn=lambda entry: entry.project_path,
            pricing=c.CODEX_MODEL_PRICING,
        )
        by_model = crk._aggregate_cache_breakdown(
            kept_entries, key_fn=lambda entry: entry.model,
            pricing=c.CODEX_MODEL_PRICING,
        )
    else:
        # Preserve the prior exact float fold: one stable_sum over every
        # individual entry net, not a stable_sum of per-day stable_sums (the
        # latter can differ by one ULP). Walk the already-wrapped immutable
        # rows once and accumulate both axes together, avoiding the old two
        # full pricing/adapter folds while retaining first-seen tie order.
        breakdowns: dict[str, dict[str, list[object]]] = {
            "project": {}, "model": {},
        }
        for raw in values:
            cache_entry_id = int(getattr(raw, "cache_entry_id", 0) or 0)
            entry = cached_rows.get(cache_entry_id)
            if (
                entry is None or entry._cache_day not in kept_dates
                or entry.model == "<synthetic>"
            ):
                continue
            for axis, key in (
                ("project", entry.project_path), ("model", entry.model),
            ):
                parts = breakdowns[axis].setdefault(
                    key, [[], 0, 0, 0],
                )
                parts[0].append(entry.cache_net_usd)
                parts[1] += entry.input_tokens
                parts[2] += entry.cache_creation_tokens
                parts[3] += entry.cache_read_tokens

        def _finish_breakdown(axis: str):
            rows = []
            for key, parts in breakdowns[axis].items():
                rows.append(crk.CacheBreakdownRow(
                    key=key,
                    cache_hit_percent=crk._compute_cache_hit_percent(
                        parts[1], parts[2], parts[3],
                    ),
                    net_usd=stable_sum(parts[0]),
                    input_tokens=parts[1],
                    cache_creation_tokens=parts[2],
                    cache_read_tokens=parts[3],
                ))
            return crk._finalize_breakdown_rows(rows)

        by_project = _finish_breakdown("project")
        by_model = _finish_breakdown("model")
    seven = days[:7]
    saved_total = stable_sum(float(row["saved_usd"]) for row in days)
    wasted_total = stable_sum(float(row["wasted_usd"]) for row in days)
    efficiency_denom = saved_total + abs(wasted_total)
    return wire.build_cache_report_wire(
        provider="codex",
        window_days=window_days,
        anomaly_threshold_pp=anomaly_threshold_pp,
        anomaly_window_days=window_days,
        today={
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
            "anomaly_unevaluated": (
                tuple(getattr(today_row, "anomaly_unevaluated", ()))
                if today_row else wire.CODEX_PREDICATES
            ),
            "observed": today_row is not None,
        },
        days=days,
        by_project=tuple({
            "key": row.key, "cache_hit_percent": row.cache_hit_percent,
            "net_usd": row.net_usd,
        } for row in by_project),
        by_model=tuple({
            "key": row.key, "cache_hit_percent": row.cache_hit_percent,
            "net_usd": row.net_usd,
        } for row in by_model),
        seven_day_net_usd=stable_sum(float(row["net_usd"]) for row in seven),
        # Placeholder: on the Codex path the builder RECOMPUTES this from
        # the day blocks it has already filtered, because a count taken
        # here would still include verdicts whose only reason is about to
        # be dropped as inapplicable. Computing it twice would just invite
        # the two to drift.
        seven_day_anomaly_count=0,
        fourteen_day_counterfactual_usd=saved_total,
        fourteen_day_efficiency_ratio=(
            saved_total / efficiency_denom if efficiency_denom > 1e-9 else 0.0
        ),
        is_empty=False,
    )


#: Per-file terminal thread aliases, joined to their first accounting entry.
#: The ``(source_root_key, source_path)`` join predicate needs the composite
#: ``idx_codex_entries_root_path`` to stay linear: a single-column root index
#: cannot discriminate when every rollout resolves to one provider root, so the
#: join degenerates to files x entries. Module-level so the query-plan
#: regression asserts THIS text rather than a copy that can drift from it.
_CODEX_FILE_ALIAS_SQL = (
    "SELECT f.source_root_key, f.path, f.last_native_thread_id, "
    "f.last_session_id, MIN(e.timestamp_utc) "
    "FROM codex_session_files AS f "
    "LEFT JOIN codex_session_entries AS e "
    "ON e.source_root_key=f.source_root_key AND e.source_path=f.path "
    "WHERE f.last_native_thread_id IS NOT NULL AND f.last_native_thread_id != '' "
    "GROUP BY f.source_root_key, f.path, f.last_native_thread_id, f.last_session_id "
    "ORDER BY f.last_ingested_at DESC, f.path DESC"
)


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
        file_aliases = tuple(cache_conn.execute(_CODEX_FILE_ALIAS_SQL))
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
                        clean_title = _codex_display_title(
                            str(title) if title is not None else None
                        )
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
    private_labels: dict[str, str] | None = None,
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
        title = _codex_display_title(
            str(row_metadata.get("title"))
            if row_metadata and row_metadata.get("title") is not None
            else None
        )
        project = str(row_metadata.get("project_label") or "").strip() if row_metadata else ""
        started_at = row_metadata.get("started_at") if row_metadata else None
        duration_min = None
        if isinstance(started_at, str):
            try:
                started_dt = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                duration_min = max(0, round((row.last_activity.astimezone(UTC) - started_dt.astimezone(UTC)).total_seconds() / 60))
            except (TypeError, ValueError):
                started_at = None
        key = dashboard_resource_key(
            "session", "codex", root_identity, row.session_id_path,
        )
        if title and private_labels is not None:
            private_labels[key] = title
        rows.append({
            "key": key,
            "source": "codex",
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
    account_key: str | None = None,
    decorated: bool = False,
) -> tuple[dict[str, object], ...]:
    """Build current-cycle Codex 5-hour activity rows from durable windows.

    The durable projection supplies the truthful native block boundaries. Cost,
    tokens, and model splits come from root-qualified accounting inside each
    half-open 300-minute interval. Weekly quota summaries are deliberately not
    activity blocks and never enter this wire.

    #416 spec §5.2 (review F9): blocks were filtered by `source_root_key` and
    time ONLY, against a single `cycle` that is `cycles_all[0]` — the FIRST
    account's. Two accounts sharing one physical root therefore saw each other's
    5h blocks. `account_key` scopes both the durable block row and the
    accounting inside it to the block identity's account; `None` keeps the
    merged read, which is byte-stable. `decorated` (R8) serializes the block's
    own account so the client can label it; below two REAL accounts no key is
    added at all.

    The account predicate here is STRICT and deliberately diverges from
    `_codex_five_hour_rows`, which widens the same table (#416 closeout F2).
    Both reads are selection reads, but the rule turns on the stamping
    mechanism, not on the verb: `_codex_five_hour_rows` asks "is this block
    inside the focused CYCLE" — a different physical-window group than the
    weekly key it is scoped by, so a still-`unattributed` 5h block genuinely
    belongs and must be admitted. This is a LISTING of the block rows
    themselves, keyed by the very column it filters, so widening would render
    one unattributed block twice, once under each real account.
    """
    if cycle is None or now_utc is None:
        return ()
    assert_projection_readable(stats_conn)
    try:
        rows = stats_conn.execute(
            "SELECT source_root_key, logical_limit_key, observed_slot, window_minutes, "
            "limit_name, resets_at_utc, nominal_start_at_utc, current_percent, orphaned_at, "
            "account_key "
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
        block_account,
    ) in rows:
        block_account = str(block_account or _lib_accounts.UNATTRIBUTED)
        if account_key is not None and block_account != account_key:
            continue
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
        # The account joins the physical dedup key only under decoration: two
        # accounts sharing one physical 5h window are two windows (never-combine
        # extends to accounts), but a <=1-real-account install must keep exactly
        # today's key so its wire is byte-identical.
        physical_key = (
            (str(root_key), start_at, resets_at, block_account) if decorated
            else (str(root_key), start_at, resets_at)
        )
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
            # R8: snake_case to match the sibling `quota.history[].account_key`
            # rows in this same subtree (#341 Task 4), NOT the camelCase
            # `accounts[].accountKey` hero-card surface.
            **({"account_key": block_account} if decorated else {}),
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


def _budget_wire(
    stats_conn: sqlite3.Connection, *, decorated: bool = False,
) -> tuple[dict[str, object], ...]:
    try:
        rows = stats_conn.execute(
            "SELECT period_start_at, period, threshold, budget_usd, spent_usd, "
            "consumption_pct, account_key FROM budget_milestones WHERE vendor='codex' "
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
        # R8 (#416 §5.5): the per-account ladder needs the key to scope a child;
        # below two REAL accounts nothing is added.
        **({"account_key": str(account_key or _CODEX_VENDOR_WIDE_ACCOUNT)}
           if decorated else {}),
    } for period_start_at, period, threshold, budget_usd, spent_usd,
        consumption_pct, account_key in rows)


def _projected_budget_wire(
    stats_conn: sqlite3.Connection, *, decorated: bool = False,
) -> tuple[dict[str, object], ...]:
    try:
        rows = stats_conn.execute(
            "SELECT period, threshold, projected_value, denominator, crossed_at_utc, "
            "alerted_at, account_key "
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
        **({"account_key": str(account_key or _CODEX_VENDOR_WIDE_ACCOUNT)}
           if decorated else {}),
    } for period, threshold, projected_value, denominator, crossed_at, alerted_at,
        account_key in rows)


def _configured_codex_budget_status(
    context: DashboardReadContext,
    entries: Iterable[object],
    *,
    cost_events: tuple[tuple[dt.datetime, float], ...] | None = None,
    account_key: str | None = None,
) -> dict[str, object] | None:
    """Compute the live configured Codex budget from the coordinated entries.

    Durable milestone rows are alert history, not the current budget status.
    This reuses the CLI's calendar-window and ``BudgetInputs``/status kernels
    while deliberately keeping the accounting read on the caller-owned cache
    snapshot.

    ``account_key`` (#416 §5.5) scopes the status to ONE account: the target is
    that account's own configured budget from ``budget.codex.accounts`` — never a
    share of the vendor amount, which would be an invented number. An account
    with no configured budget therefore has no budget status at all (``None``),
    exactly as an unconfigured vendor does; the merged vendor status stays on the
    parent. ``None`` keeps the merged behaviour and is byte-stable.

    #556 S5 §3.5: a VENDOR-WIDE read of a PER-ACCOUNT-ONLY configuration is the
    same "nothing to compute against" state. ``_validate_codex_budget_block``
    accepts a Codex block with no ``amount_usd`` when a non-empty ``accounts``
    map is present (``bin/_cctally_core.py:1350``), and this function used to
    call ``float(amount_usd)`` on that ``None`` with no exception boundary
    anywhere between here and ``_tui_build_source_bundle``'s
    ``source_build_failed`` handler — so one valid configuration destroyed the
    entire Codex provider's data.
    """
    config = context.codex_budget
    if config is None:
        return None
    amount_usd = config.get("amount_usd")
    if account_key is not None:
        per_account = config.get("accounts")
        if not isinstance(per_account, Mapping) or account_key not in per_account:
            return None
        amount_usd = per_account[account_key]
    if amount_usd is None:
        return None
    c = sys.modules["cctally"]
    period, start_at, end_at = _configured_codex_budget_window(context)

    resolved_events = cost_events if cost_events is not None else _codex_budget_cost_events(
        context, entries,
    )

    def _sum_cost(start: dt.datetime, end: dt.datetime) -> float:
        return sum(cost for timestamp, cost in resolved_events if start <= timestamp < end)

    recent_start = max(start_at, context.now_utc - dt.timedelta(hours=24))
    # #556 S5 §3.1/§3.3: ONE producer of the wire status, shared with Claude.
    return c.budget_status_payload(
        period=period,
        window_start_at=start_at,
        window_end_at=end_at,
        target_usd=amount_usd,
        spent_usd=_sum_cost(start_at, context.now_utc),
        recent_24h_usd=_sum_cost(recent_start, context.now_utc),
        now=context.now_utc,
        alert_thresholds=config["alert_thresholds"],
    )


def _codex_budget_status_domain(
    context: DashboardReadContext,
    entries: Iterable[object],
    *,
    cost_events: tuple[tuple[dt.datetime, float], ...] | None = None,
    account_key: str | None = None,
) -> dict[str, object]:
    """The Codex budget domain's status half, with a LOCAL failure contract.

    #556 S5 §3.5. ``status`` stays required-and-nullable, which is the shape
    Codex's enclosing domain has always emitted and which this session does not
    normalise. What is new is the boundary: a computation that raises now
    degrades this one domain and names its reason, following the same
    ``{code, message, provider}`` shape the Claude half uses, instead of
    escaping into the provider-level handler and turning the whole Codex source
    into ``source_build_failed``.

    ``status_unavailable`` is additive and omitted when inapplicable, so the
    ordinary payload is byte-identical.

    #556 S5 Unit 2 review F1. A VENDOR-WIDE read of a per-account-only Codex
    configuration is a configured state, not an unset one, and returning a bare
    ``{"status": None}`` for it made the client render "No budget set." beside
    the command that sets one — to a user who has budgets set. It publishes the
    same ``not_configured.disposition`` the Claude half publishes, which the
    client already handles. An ACCOUNT-SCOPED read is deliberately excluded:
    ``account_budgets_only`` describes the vendor-wide axis, and that account's
    own missing budget is genuinely unset.
    """
    config = context.codex_budget
    if (
        account_key is None
        and isinstance(config, Mapping)
        and config.get("amount_usd") is None
        and config.get("accounts")
    ):
        return {
            "status": None,
            "not_configured": {"disposition": "account_budgets_only"},
        }
    try:
        return {"status": _configured_codex_budget_status(
            context, entries, cost_events=cost_events, account_key=account_key,
        )}
    except Exception:
        _lib_log.get_logger("dashboard").error(
            "codex budget status could not be computed", exc_info=True,
        )
        return {
            "status": None,
            "status_unavailable": {
                "code": "budget_compute_failed",
                "message": "Codex's budget status could not be computed.",
                "provider": "codex",
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


def _codex_account_admits(scope_key: str | None, row_key: object) -> bool:
    """Whether an account-scoped quota read admits ``row_key`` (#416 B2).

    One-directional widening, identical to ``_codex_five_hour_rows``
    (``bin/_cctally_milestone_history.py``): a REAL account admits its own rows
    plus the ``unattributed`` sentinel, because
    ``adopt_unidentified_observations`` resolves attribution PER physical-window
    group — a decorated install can legitimately carry the weekly window under a
    real account while its 5h windows are still unattributed, and strict
    equality would then correlate nothing. The widening is one-directional on
    purpose: an ``unattributed`` scope never picks up another account's
    identified rows, so no REAL account's number ever reaches another's row.

    ``scope_key is None`` is the merged read and admits everything.
    """
    if scope_key is None:
        return True
    key = str(row_key or _lib_accounts.UNATTRIBUTED)
    if scope_key == _lib_accounts.UNATTRIBUTED:
        return key == _lib_accounts.UNATTRIBUTED
    return key in (scope_key, _lib_accounts.UNATTRIBUTED)


def _quota_read_model(
    context: DashboardReadContext,
    observations: Iterable[object],
    *,
    accounting_entries: Iterable[object] = (),
    account_key: str | None = None,
    decorated: bool,
) -> dict[str, object]:
    """Use S2's pure history/block/forecast kernels over cache evidence.

    ``account_key`` (#416 Slice 3A review B2/F4) scopes the two reads this
    function reaches that the observation partition does NOT already cover: the
    durable milestone breakdown (``codex_quota_breakdown``, whose accounting and
    block-start reads filter by root and time only) and the 5h correlation load
    below. ``None`` keeps the merged parent read, byte-stable.

    The key passed on is POST-fold — it names a registry account or an
    ``obs_partition`` bucket, and ``load_codex_quota_observations`` applies
    ``adopt_unidentified_observations`` before returning — while
    ``codex_quota_breakdown``'s block-start boundary reads the PRE-fold
    ``quota_window_snapshots``. That read widens (#416 closeout F1); its
    accounting read stays strict so the children keep partitioning the parent's
    spend. Neither is elected here — the kernel settles both.
    """
    quota_observations = tuple(observations)
    cost_entries = tuple(accounting_entries)
    histories = build_history(quota_observations)
    blocks = build_blocks(quota_observations)
    milestone_rows: list[dict[str, object]] = []
    # #429 §4.2: one candidate unit per identity — (ordinal, history row, active
    # projection or None) — so the cap retains PAIRS instead of capping the two
    # lists independently and in different orders.
    candidates: list[tuple[int, dict[str, object], dict[str, object] | None]] = []
    # R8 (#341 Task 4): the per-account `account_key` is serialized onto each
    # history row ONLY when the Codex provider has >1 REAL account, so the
    # dashboard client can scope per-account quota rows instead of merging them.
    # A <=1-real-account install (all fixtures) stays byte-identical (no key
    # added). #350 removed the original consumer, `_clock_cycle_validity`: the
    # idle clock no longer re-derives weekly-cycle validity at all, because this
    # public history view is LOSSY — capped at `SOURCE_HISTORY_LIMIT` and without
    # `logical_limit_key` — so it cannot resolve the cycle authoritatively. Build
    # time owns resolution; a `clock_data` decision deadline forces the rebuild.
    #
    # #429 §4.4: the caller owns this gate. Re-querying here, per parent AND per
    # child, let a transient failure emit decorated scopes whose quota rows were
    # silently unstamped — and the active-row helper cannot project a field the
    # history row never carried.
    _codex_decorated = decorated
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
        # #373: a window outside account-level standard quota (a separate model
        # pool such as GPT-5.3-Codex-Spark) stays LISTED — a legitimate
        # independent pool must remain visible — but is excluded from every
        # account-level aggregate below. `baseline` is the label authority when
        # one exists (spec §7.1).
        model_scoped = codex_history_is_model_scoped(history, baseline=baseline)
        row = {
            "key": dashboard_resource_key("quota", "codex", *key_parts),
            "source": "codex",
            **({"model_scoped": True} if model_scoped else {}),
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
        }
        candidates.append((
            len(candidates),
            row,
            _active_row_from_history(row, now_utc=context.now_utc),
        ))
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
                    account_key=account_key,
                )
            except sqlite3.Error:
                # Older or partially migrated stores retain the bounded
                # observation-derived fallback below.  A coherent current
                # store always has the durable projection used by the CLI.
                canonical_rows = ()
        if canonical_rows:
            try:
                # #416 Slice 3A review F4: this load is bounded by root, slot
                # and `limit_id` only, so under focus the crossing was annotated
                # with whichever ACCOUNT's 5h observation happened to sort last.
                correlated_five_hour = tuple(
                    observation
                    for observation in _cached_codex_quota_observations(
                        source_root_keys={identity.source_root_key},
                        cache_conn=context.cache_conn,
                        captured_at_or_after=block.nominal_start_at,
                    )
                    if observation.identity.window_minutes == 300
                    and observation.identity.observed_slot == identity.observed_slot
                    and observation.identity.limit_id == identity.limit_id
                    and _codex_account_admits(
                        account_key, observation.identity.account_key)
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
    # Active account identities are presentation-critical. Independent
    # model-scoped pools are also legitimate provider facts, so reserve the
    # remaining cap space for their newest captures before inactive account
    # history. Opaque resource-key order is only a stable tie-breaker.
    #
    # #429 §4.2 — retention decides ONCE per (history, active) unit, then each
    # list is emitted in its own established order: histories in retention
    # order, actives in identity order. Emitting both in a single order would
    # reorder active rows below the cap and move bytes for every install.
    active_keys = {
        str(active["key"]) for _, _, active in candidates if active is not None
    }

    def _history_retention_key(unit):
        _, row, _ = unit
        key = str(row["key"])
        if key in active_keys:
            return (0, 0.0, key)
        if _codex_history_row_is_model_scoped(row):
            captured_at = row.get("captured_at")
            try:
                captured_epoch = dt.datetime.fromisoformat(
                    str(captured_at).replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError):
                captured_epoch = float("-inf")
            return (1, -captured_epoch, key)
        return (2, 0.0, key)

    retained = sorted(candidates, key=_history_retention_key)[:SOURCE_HISTORY_LIMIT]
    history_rows = [row for _, row, _ in retained]
    active_rows = [
        active
        for _, _, active in sorted(retained, key=lambda unit: unit[0])
        if active is not None
    ]
    latest_percent = max(
        (float(row["current_percent"]) for row in active_rows), default=None,
    )
    active_freshness = (
        "fresh" if active_rows and all(row["freshness"] == "fresh" for row in active_rows)
        else ("unavailable" if not active_rows else "stale")
    )
    milestone_rows.sort(key=lambda row: str(row["captured_at"]), reverse=True)
    milestone_rows = milestone_rows[:SOURCE_HISTORY_LIMIT]
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


def _clock_cycle_expired(cycle: object, now_utc: dt.datetime) -> bool:
    """Whether a retained hero cycle has already reset (#350 spec §3.3).

    The one invariant the idle clock still enforces on frozen evidence: a cycle
    whose ``resets_at`` is at or before ``now_utc`` cannot bound current
    accounting. Fails CLOSED on an unparseable or absent boundary, matching the
    prior behavior where malformed evidence yielded no valid boundary.
    """
    if not isinstance(cycle, Mapping):
        return True
    try:
        resets_at = dt.datetime.fromisoformat(
            str(cycle.get("resets_at")).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return True
    if resets_at.tzinfo is None or resets_at.utcoffset() is None:
        return True
    return resets_at.astimezone(UTC) <= now_utc


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


def _scoped_quota_identity(row: Mapping[str, object]) -> tuple[str, str]:
    """#429 §3.1. `dashboard_resource_key` carries no account, and two accounts
    sharing one $CODEX_HOME root emit the same key, so bare key is not an
    identity under decoration. `"unattributed"` is a legitimate account here."""
    return (str(row.get("account_key") or ""), str(row.get("key")))


def _reclock_quota_domain(
    quota: Mapping[str, object], *, now_utc: dt.datetime,
) -> dict[str, object]:
    """Re-evaluate a quota domain's row freshness and summary against ``now``.

    #429 §4.3. Replaces ONLY `histories` and `summary`; `blocks`, `milestones`
    and `cycle_index` are carried through untouched, because the per-account
    scopes carry them and a scope that lost them would render empty.

    Emits TUPLES, matching what `_quota_read_model` publishes. Publication
    freezes lists into tuples anyway, so this is byte-identical on the wire —
    but it is what lets the caller detect an unchanged domain by comparing the
    result against the frozen original. A list would never compare equal to the
    tuple it was frozen from (``[] != ()``), the caller would report a change on
    every tick, and the retain/degrade paths that assert the EXACT prior ``data``
    object is handed back would break.
    """
    refreshed = dict(quota)
    refreshed_histories: list[dict[str, object]] = []
    active_rows: list[dict[str, object]] = []
    for raw_history in quota.get("histories", ()):
        if not isinstance(raw_history, Mapping):
            continue
        history = dict(raw_history)
        # #350 spec §3.9: this is a PER-ROW value and must never shadow the
        # envelope-level `freshness`. It used to, so after the loop the
        # envelope held the LAST retained history row's freshness — often an
        # inactive row, and with a single weekly history the active weekly
        # one, which silently marked the whole provider stale on an idle
        # stale crossing and tripped idle eligibility on its own.
        row_freshness = _clock_freshness(
            history.get("captured_at"), history.get("stale_after_seconds"), now_utc,
        )
        history["freshness"] = row_freshness
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
            if row_freshness == "future":
                forecast["status"] = "future"
            elif row_freshness == "stale":
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
        # Called unconditionally, exactly as the build calls it. The helper
        # already returns None for a row without a usable forecast, and keeping
        # a caller-side `isinstance(forecast, Mapping)` guard here would put a
        # fragment of the predicate back on the caller — the split #429 exists
        # to remove.
        active = _active_row_from_history(history, now_utc=now_utc)
        if active is not None:
            active_rows.append(active)
        refreshed_histories.append(history)
    summary = dict(quota.get("summary") or {})
    prior_active = summary.get("active")
    if isinstance(prior_active, (tuple, list)):
        # #429 §3.1: scoped identity, not bare key — two decorated rows can
        # share one key and would collapse into a single map entry.
        active_order = {
            _scoped_quota_identity(row): index
            for index, row in enumerate(prior_active)
            if isinstance(row, Mapping)
        }
        active_rows.sort(
            key=lambda row: active_order.get(
                _scoped_quota_identity(row), len(active_order)),
        )
    summary.update({
        "active_window_count": len(active_rows),
        "latest_percent": max(
            (float(row["current_percent"]) for row in active_rows), default=None),
        "freshness": (
            "fresh" if active_rows and all(row["freshness"] == "fresh" for row in active_rows)
            else ("unavailable" if not active_rows else "stale")
        ),
        "active": tuple(active_rows),
    })
    refreshed["histories"] = tuple(refreshed_histories)
    refreshed["summary"] = summary
    return refreshed


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
    domain_freshness = dict(state.domain_freshness or {})
    if isinstance(quota, Mapping):
        reclocked = _reclock_quota_domain(quota, now_utc=now_utc)
        quota_changed = reclocked != quota
        quota = reclocked
        data["quota"] = quota
        # Only account-level active histories reach the summary; the shared
        # model-scoped predicate excludes foreign pools. An unavailable active
        # set is a capability/data-availability fact, not invented staleness,
        # so only the exact stale verdict moves this axis. #429 §4.3 keeps this
        # deriving from the TOP-LEVEL summary only — the #350 §3.9 rule that a
        # row-level freshness value never touches `state.freshness` extends to
        # the per-account scopes clocked below.
        domain_freshness["quota"] = (
            "stale" if quota["summary"]["freshness"] == "stale" else "fresh"
        )
    # #429 §4.3: the per-account scopes are independent evidence domains and
    # were never clocked at all, so a focused account read frozen freshness
    # forever. The published state is recursively frozen (`MappingProxyType`),
    # so nothing may be mutated in place — copy outward: the scopes mapping,
    # then the scope, then only that scope's `quota`.
    scopes = data.get("account_scopes")
    scopes_changed = False
    if isinstance(scopes, Mapping):
        # #556 S5 §3.8: each child's budget reclocks from ITS OWN retained event
        # tuple, never the vendor-wide one — the accounts hold different spend,
        # so borrowing the parent's events would publish another account's
        # trailing-24h rate under this account's card.
        child_budget_events = (
            state.clock_data.get("codex_budget_cost_events_by_account", {})
            if isinstance(state.clock_data, Mapping) else {}
        )
        rebuilt_scopes = dict(scopes)
        for scope_key, scope in scopes.items():
            if not isinstance(scope, Mapping):
                continue
            rebuilt_scope: dict[str, object] | None = None
            scope_quota = scope.get("quota")
            if isinstance(scope_quota, Mapping):
                reclocked_scope_quota = _reclock_quota_domain(
                    scope_quota, now_utc=now_utc)
                if reclocked_scope_quota != scope_quota:
                    rebuilt_scope = dict(scope)
                    rebuilt_scope["quota"] = reclocked_scope_quota
            # #556 S5 §3.8: the child budget status was computed at build and
            # then never advanced, while the quota beside it was. Focused cards
            # read this object, so an unclocked one is a knowingly stale number
            # on the surface a user is looking straight at.
            scope_budget = scope.get("budget")
            if isinstance(scope_budget, Mapping) and isinstance(
                scope_budget.get("status"), Mapping,
            ):
                reclocked_child_budget = _refresh_budget_status_clock(
                    scope_budget["status"],
                    now_utc,
                    cost_events=child_budget_events.get(scope_key, ()),
                )
                if (
                    reclocked_child_budget is not None
                    and reclocked_child_budget != scope_budget["status"]
                ):
                    if rebuilt_scope is None:
                        rebuilt_scope = dict(scope)
                    rebuilt_scope["budget"] = {
                        **dict(scope_budget), "status": reclocked_child_budget,
                    }
            if rebuilt_scope is None:
                continue
            rebuilt_scopes[scope_key] = rebuilt_scope
            scopes_changed = True
        if scopes_changed:
            data["account_scopes"] = rebuilt_scopes
    budget_domain = data.get("budget")
    budget_changed = False
    refreshed_budget = None
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
            budget_changed = True
    # #429 §4.5: all three hero mutations compose on ONE copy, in a stated
    # order. Three independent `dict(hero)` copies let the last write win, which
    # is how `hero["quota"]` stayed frozen at its build-time value while
    # `quota["summary"]` advanced.
    hero = data.get("hero")
    if isinstance(hero, Mapping):
        hero = dict(hero)
        # 1. quota first — `_clock_cycle_expired` reads `hero["cycle"]`, never
        #    `hero["quota"]`, so replacing quota cannot affect the predicate.
        if isinstance(quota, Mapping):
            hero["quota"] = quota["summary"]
        # 2. cycle expiry, with its capability/warning consequences.
        #    #350 spec §3.3: the clock no longer RE-DERIVES cycle validity. Its
        #    public-history view is lossy (capped, no `logical_limit_key`, no
        #    `quota_identity`), so it cannot resolve the cycle correctly — and
        #    per §2.2 it cannot simply trust the old verdict forever either,
        #    because resolution is time-dependent on frozen evidence. Build time
        #    owns resolution and records a decision deadline in `clock_data`; the
        #    tick rebuilds authoritatively at the crossing. All the clock keeps
        #    is this cheap invariant guard: a cycle that has already RESET cannot
        #    bound current accounting, so it degrades exactly as before. Expiry
        #    is also deadline candidate #1, so the two paths are disjoint
        #    belt-and-suspenders rather than a single mechanism.
        hero_capability = state.capabilities.get("hero")
        if (
            isinstance(hero.get("cycle"), Mapping)
            and hero_capability is not None
            and hero_capability.status == "supported"
            and _clock_cycle_expired(hero.get("cycle"), now_utc)
        ):
            for field in (
                "cost_usd", "input_tokens", "cached_input_tokens", "output_tokens",
                "reasoning_output_tokens", "total_tokens", "cycle",
            ):
                hero[field] = None
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
            # #556 S1 §4.1: an EXPIRED boundary is exactly the state the
            # accounting axis reports as stale. Build time can never see it
            # (`_resolve_codex_weekly_cycle` retains only `resets_at > now`),
            # so this clock is the only writer of that value.
            domain_freshness["hero"] = "stale"
            cycle_changed = True
        # 3. budget last
        if refreshed_budget is not None:
            hero["budget"] = refreshed_budget
        data["hero"] = hero
    if not (quota_changed or budget_changed or cycle_changed or scopes_changed):
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
        domain_freshness=domain_freshness,
        clock_data=state.clock_data,
        # #556 S1 §3.8: this constructor lists every field explicitly, so an
        # omission silently drops the authoritative account count and makes the
        # combined figure fail closed on an idle tick that changed nothing else.
        account_scope=state.account_scope,
        private_session_labels=state.private_session_labels,
        # #556 S2 §3.6: the aggregate carrier travels with the rows it
        # describes. This clock refreshes presentation axes only and publishes
        # the SAME rows, so it must carry the range that bounded them —
        # dropping it here would withhold the aggregate on any idle tick whose
        # clock moved.
        aggregate_scope=state.aggregate_scope,
    )
    return state if refreshed_state == state else refreshed_state


def _alerts_wire(
    stats_conn: sqlite3.Connection, *, decorated: bool = False,
) -> tuple[dict[str, object], ...]:
    """Return only safe, source-owned Codex alert context in newest-first order.

    #416 spec §5.4 (review F14): the underlying tables all carry an account key —
    including the vendor-wide ``*`` rows — but this wire neither selected nor
    emitted it, so removing the `alerts-unfiltered-note` disclaimer badge without
    this would silently show one account another's alerts. `account_key` is now
    selected on all three legs and serialized under decoration (R8: below two
    REAL accounts no key is added, so the envelope is byte-identical).

    Vendor-wide ``*`` rows keep that literal key rather than being dropped or
    reassigned: a vendor-wide budget crossing is not attributable to one account,
    so it stays visible under focus and the client labels it as vendor-wide.
    """
    rows: list[dict[str, object]] = []
    if decorated:
        import _cctally_account

        account_labels = _cctally_account.display_label_map(stats_conn, "codex")
        account_labels.update({"*": "All accounts", "unattributed": "Unattributed"})
    else:
        account_labels = {}

    def _account(value: object) -> dict[str, object]:
        if not decorated:
            return {}

        key = str(value or _CODEX_VENDOR_WIDE_ACCOUNT)
        label = account_labels.get(key)
        if label is None:
            label = _cctally_account.account_label(stats_conn, key)
        return {
            # Retained as the internal account-scope selector used by the
            # source-state builder; the camel fields are the public #345 wire.
            "account_key": key,
            "accountKey": key,
            "accountLabel": label,
        }

    # #556 S3 §2.1/§2.4: the firing instant is what this wire filters on, what
    # the panel orders by and what it prints, so it is what every leg selects,
    # orders by and publishes. Two legs previously ordered, truncated and
    # published the CROSSING instant instead, which is a different moment: a
    # row that fired most recently but crossed longest ago was dropped at the
    # LIMIT and never reached the panel at all. `created_at` stays as an
    # equal-valued compatibility alias for a client reading a pre-v7 envelope.
    canon = canonical_alerted_at_sql()

    def _instants(raw: object) -> dict[str, str]:
        value = canonical_alerted_at(raw)
        return {"alerted_at": value, "created_at": value}

    try:
        for period, threshold, consumption_pct, crossed_at, alerted_at, account_key in stats_conn.execute(
            "SELECT period, threshold, consumption_pct, crossed_at_utc, alerted_at, account_key "
            "FROM budget_milestones WHERE vendor='codex' AND alerted_at IS NOT NULL "
            f"ORDER BY {canon} DESC, threshold DESC LIMIT ?",
            (SOURCE_HISTORY_LIMIT,),
        ):
            rows.append({
                # The resource key keeps the crossing instant it has always
                # carried: it is an opaque identity, and re-keying every
                # historical Codex alert row is not this session's change.
                "key": dashboard_resource_key("alert", "codex", "codex_budget", period, threshold, crossed_at),
                "source": "codex",
                "axis": "codex_budget", "period": period, "threshold": threshold,
                "value": consumption_pct, **_instants(alerted_at),
                **_account(account_key),
            })
        for period, threshold, projected_value, crossed_at, alerted_at, account_key in stats_conn.execute(
            "SELECT period, threshold, projected_value, crossed_at_utc, alerted_at, account_key "
            "FROM projected_milestones WHERE metric='codex_budget_usd' AND alerted_at IS NOT NULL "
            f"ORDER BY {canon} DESC, threshold DESC LIMIT ?",
            (SOURCE_HISTORY_LIMIT,),
        ):
            rows.append({
                "key": dashboard_resource_key("alert", "codex", "projected", period, threshold, crossed_at),
                "source": "codex",
                "axis": "projected", "period": period, "threshold": threshold,
                "value": projected_value, **_instants(alerted_at),
                **_account(account_key),
            })
        for (root_key, logical_key, observed_slot, window_minutes, resets_at,
             threshold, severity, created_at, alerted_at, account_key) in stats_conn.execute(
            "SELECT source_root_key, logical_limit_key, observed_slot, window_minutes, resets_at_utc, "
            "threshold, severity, created_at_utc, alerted_at, account_key FROM quota_threshold_events "
            "WHERE source='codex' AND disposition='alerted' AND orphaned_at IS NULL "
            f"ORDER BY {canon} DESC, source_root_key, logical_limit_key, observed_slot, threshold "
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
                **_instants(alerted_at),
                **_account(account_key),
            })
    except sqlite3.Error:
        return ()
    return tuple(sorted(
        rows,
        key=lambda item: canonical_alerted_at(item["alerted_at"]),
        reverse=True,
    )[:SOURCE_HISTORY_LIMIT])


_CODEX_PROJECT_LABEL_CACHE: dict[object, dict[str, object]] = {}


def _cached_project_labeled_entries(
    entries: tuple[object, ...], cache_key: object | None,
) -> tuple[object, ...]:
    """Reuse per-scope display-label annotation for unchanged accounting rows."""
    if cache_key is None or any(
        not int(getattr(entry, "cache_entry_id", 0) or 0) for entry in entries
    ):
        return tuple(assign_collision_safe_project_labels(entries))
    pairs = frozenset(
        (str(entry.project_key), str(entry.project_label)) for entry in entries
    )
    state = _CODEX_PROJECT_LABEL_CACHE.get(cache_key)
    if state is None or state.get("pairs") != pairs:
        labeled = tuple(assign_collision_safe_project_labels(entries))
    else:
        labels = state["labels"]
        prior = state["entries"]
        labeled = tuple(
            prior[int(entry.cache_entry_id)][1]
            if (
                int(entry.cache_entry_id) in prior
                and prior[int(entry.cache_entry_id)][0] == entry
            ) else replace(
                entry, display_label=labels[str(entry.project_key)],
            )
            for entry in entries
        )
    _CODEX_PROJECT_LABEL_CACHE[cache_key] = {
        "pairs": pairs,
        "labels": {
            str(entry.project_key): entry.display_label for entry in labeled
        },
        "entries": {
            int(entry.cache_entry_id): (raw, entry)
            for raw, entry in zip(entries, labeled)
        },
    }
    return labeled


def _projects_wire(
    context: DashboardReadContext,
    _quota_observations: Iterable[object],
    entries: Iterable[object],
    *,
    accounting_end: dt.datetime,
    cache_key: object | None = None,
) -> dict[str, object]:
    """Adapt S3's already-qualified attribution result without re-formulas."""
    qualified_entries = _cached_project_labeled_entries(
        tuple(entries), cache_key,
    )
    result = build_codex_project_result(
        qualified_entries,
        range_start=context.range_start,
        range_end=accounting_end,
        as_of=context.now_utc,
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


_CODEX_PROJECT_WIRE_CACHE: dict[object, tuple] = {}


def _cached_projects_wire(
    context: DashboardReadContext,
    quota_observations: Iterable[object],
    entries: Iterable[object],
    *,
    changed_old: Iterable[object],
    changed_new: Iterable[object],
    accounting_end: dt.datetime,
    cache_key: object,
    semantic_signature: object,
) -> dict[str, object]:
    """Rebuild only project groups touched by accounting changes."""
    values = tuple(entries)
    pairs = frozenset(
        (str(entry.project_key), str(entry.project_label)) for entry in values
    )
    # `entries` is already the complete half-open population for the advancing
    # upper bound.  A moving wall clock is therefore not an aggregation
    # semantic: `build_cached_codex_accounting` emits newly-visible rows in the
    # delta when its upper bound advances.  Keeping `accounting_end` here made
    # every live dirty tick discard every project group before that delta could
    # be spliced.
    signature = (semantic_signature, pairs, context.range_start)
    state = _CODEX_PROJECT_WIRE_CACHE.get(cache_key)
    if state is None or state[0] != signature:
        value = _projects_wire(
            context, quota_observations, values,
            accounting_end=accounting_end, cache_key=cache_key,
        )
        groups: dict[tuple[str, str], list[object]] = {}
        for entry in values:
            groups.setdefault(
                (str(entry.source_root_key), str(entry.project_key)), [],
            ).append(entry)
        _CODEX_PROJECT_WIRE_CACHE[cache_key] = (
            signature, value,
            {key: tuple(group) for key, group in groups.items()},
        )
        return value

    affected = {
        (str(entry.source_root_key), str(entry.project_key))
        for entry in (*tuple(changed_old), *tuple(changed_new))
    }
    if not affected:
        return state[1]
    label_state = _CODEX_PROJECT_LABEL_CACHE.get(cache_key) or {}
    labels = label_state.get("labels") or {}
    old_ids = {
        int(getattr(entry, "cache_entry_id", 0) or 0)
        for entry in changed_old
    }
    groups = dict(state[2])
    for key in affected:
        groups[key] = tuple(
            entry for entry in groups.get(key, ())
            if int(getattr(entry, "cache_entry_id", 0) or 0) not in old_ids
        )
    for entry in changed_new:
        key = (str(entry.source_root_key), str(entry.project_key))
        groups[key] = (*groups.get(key, ()), entry)
    for key in affected:
        if groups.get(key):
            groups[key] = tuple(sorted(
                groups[key], key=_codex_incremental_entry_order,
            ))
        else:
            groups.pop(key, None)
    partial_entries = tuple(
        replace(entry, display_label=labels[str(entry.project_key)])
        for key in sorted(affected) for entry in groups.get(key, ())
    )
    result = build_codex_project_result(
        partial_entries,
        range_start=context.range_start,
        range_end=accounting_end,
        as_of=context.now_utc,
    )
    partial_rows = () if result.data is None else tuple({
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
    } for row in result.data.projects)
    affected_keys = {
        dashboard_resource_key("project", "codex", project_key)
        for _root_key, project_key in affected
    }
    rows = tuple(sorted(
        (
            *(row for row in state[1]["rows"] if row["key"] not in affected_keys),
            *partial_rows,
        ),
        key=lambda row: (
            float(row["cost_usd"]), str(row["label"]), str(row["key"]),
        ),
        reverse=True,
    ))
    value = {
        "rows": rows,
        "total_cost_usd": stable_sum(
            float(row["cost_usd"]) for row in rows),
        "total_tokens": sum(int(row["total_tokens"]) for row in rows),
    }
    _CODEX_PROJECT_WIRE_CACHE[cache_key] = (signature, value, groups)
    return value


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
            # A persisted Codex task title is transcript-derived content. The
            # partial project projection is shared across every dashboard
            # client, so retain only a non-sensitive generic label here.
            "label": "Session",
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


_CODEX_ENTRY_ADAPTER_CACHE: dict[int, tuple[object, CodexEntry]] = {}


def _codex_entries_from_accounting(entries: Iterable[object]) -> list[CodexEntry]:
    """Adapt coordinated accounting rows for the shipped non-project kernels."""
    converted: list[CodexEntry] = []
    for entry in entries:
        cache_entry_id = int(getattr(entry, "cache_entry_id", 0) or 0)
        cached = _CODEX_ENTRY_ADAPTER_CACHE.get(cache_entry_id)
        if cache_entry_id and cached is not None and cached[0] == entry:
            converted.append(cached[1])
            continue
        source_path = str(getattr(entry, "source_path", "") or "")
        session_id = str(getattr(entry, "session_id", "") or "")
        if not source_path or not session_id:
            raise SourceCapabilityUnavailable("Codex accounting lacks session identity")
        value = CodexEntry(
            timestamp=getattr(entry, "timestamp"),
            session_id=session_id,
            model=str(getattr(entry, "model")),
            input_tokens=int(getattr(entry, "input_tokens")),
            cached_input_tokens=int(getattr(entry, "cached_input_tokens")),
            output_tokens=int(getattr(entry, "output_tokens")),
            reasoning_output_tokens=int(getattr(entry, "reasoning_output_tokens")),
            total_tokens=int(getattr(entry, "total_tokens")),
            source_path=source_path,
            cost_usd=(
                float(entry.cost_usd)
                if getattr(entry, "cost_usd", None) is not None else None
            ),
            cache_entry_id=cache_entry_id,
            source_root_key=str(getattr(entry, "source_root_key", "") or ""),
            conversation_key=str(getattr(entry, "conversation_key", "") or ""),
        )
        converted.append(value)
        if cache_entry_id:
            _CODEX_ENTRY_ADAPTER_CACHE[cache_entry_id] = (entry, value)
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
    account_key: str | None = None,
    include_account_keys: bool = False,
) -> CodexWeeklyView:
    """Aggregate Codex cost into observed native quota-cycle segments.

    ``account_key`` scopes the durable boundary read to one account (#416
    Slice 3A review B1); ``None`` is the merged parent read. The additive
    ownership axis is emitted only when ``include_account_keys`` is true.
    """
    periods = _codex_weekly_periods(
        stats_conn,
        source_root_keys=source_root_keys,
        active_cycle=active_cycle,
        account_key=account_key,
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
            account_keys=(
                periods_by_bucket[row.bucket].account_keys
                if include_account_keys else ()
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
        # A DISPLAY LABEL, deliberately not resolved here (#503 S2 review
        # F9). It can be an abbreviation such as `EDT`, which
        # `period_civil_dates` cannot load — but this value is the
        # dashboard envelope's, and the envelope's shape is pinned by
        # oracle tests. Resolution happens at the two SHARE boundaries
        # that turn it into a `PeriodSpec.display_tz`:
        # `_cctally_dashboard_share._share_resolved_display_tz` and
        # `_cctally_codex._build_codex_share_snapshot`, both of which
        # call `resolve_display_tz_name` unconditionally.
        display_tz_label=display_tz_name or str(dt.datetime.now().astimezone().tzinfo),
    )


_CODEX_WEEKLY_VIEW_CACHE: dict[object, tuple] = {}


def _codex_weekly_period_for_entry(
    entry: object, periods: Iterable[CodexWeeklyPeriod],
) -> CodexWeeklyPeriod | None:
    if codex_model_scoped_quota_pool(getattr(entry, "model", None)) is not None:
        return None
    timestamp = getattr(entry, "timestamp").astimezone(UTC)
    root_key = str(getattr(entry, "source_root_key", "") or "")
    return next((
        period for period in periods
        if root_key in period.source_root_keys
        and period.start_at <= timestamp < period.end_at
    ), None)


def _cached_codex_native_weekly_view(
    stats_conn: sqlite3.Connection,
    entries: Iterable[object],
    *,
    changed_old: Iterable[object],
    changed_new: Iterable[object],
    cache_key: object,
    semantic_signature: object,
    source_root_keys: Iterable[str],
    active_cycle: CodexCycleBoundary | None,
    now_utc: dt.datetime,
    display_tz_name: str | None,
    speed: str,
    account_key: str | None = None,
    include_account_keys: bool = False,
) -> CodexWeeklyView:
    """Rebuild only native quota periods touched by accounting changes."""
    values = tuple(entries)
    roots = tuple(source_root_keys)
    periods = _codex_weekly_periods(
        stats_conn,
        source_root_keys=roots,
        active_cycle=active_cycle,
        account_key=account_key,
    )
    signature = (
        semantic_signature, periods, roots, display_tz_name, speed,
        account_key, include_account_keys,
    )
    state = _CODEX_WEEKLY_VIEW_CACHE.get(cache_key)
    if state is None or state[0] != signature:
        view = _build_codex_native_weekly_view(
            stats_conn, values, source_root_keys=roots,
            active_cycle=active_cycle, now_utc=now_utc,
            display_tz_name=display_tz_name, speed=speed,
            account_key=account_key, include_account_keys=include_account_keys,
        )
        groups: dict[dt.datetime, list[object]] = {}
        for entry in values:
            period = _codex_weekly_period_for_entry(entry, periods)
            if period is not None:
                groups.setdefault(period.start_at, []).append(entry)
        _CODEX_WEEKLY_VIEW_CACHE[cache_key] = (
            signature, view,
            {key: tuple(group) for key, group in groups.items()},
        )
        return view

    affected = {
        period.start_at
        for entry in (*tuple(changed_old), *tuple(changed_new))
        if (period := _codex_weekly_period_for_entry(entry, periods)) is not None
    }
    if not affected:
        return state[1]

    prior = state[1]
    old_ids = {
        int(getattr(entry, "cache_entry_id", 0) or 0)
        for entry in changed_old
    }
    groups = dict(state[2])
    for start_at in affected:
        groups[start_at] = tuple(
            entry for entry in groups.get(start_at, ())
            if int(getattr(entry, "cache_entry_id", 0) or 0) not in old_ids
        )
    for entry in changed_new:
        period = _codex_weekly_period_for_entry(entry, periods)
        if period is not None:
            groups[period.start_at] = (*groups.get(period.start_at, ()), entry)
    for start_at in affected:
        if groups.get(start_at):
            groups[start_at] = tuple(sorted(
                groups[start_at], key=_codex_incremental_entry_order,
            ))
        else:
            groups.pop(start_at, None)
    replacements: dict[dt.datetime, object] = {}
    for start_at in affected:
        partial = _build_codex_native_weekly_view(
            stats_conn,
            groups.get(start_at, ()),
            source_root_keys=roots, active_cycle=active_cycle,
            now_utc=now_utc, display_tz_name=display_tz_name, speed=speed,
            account_key=account_key, include_account_keys=include_account_keys,
        )
        row = next((
            row for row in partial.rows
            if getattr(row, "period_start_at", None) == start_at
        ), None)
        if row is not None:
            replacements[start_at] = row
    rows = tuple(sorted(
        (
            *(row for row in prior.rows
              if getattr(row, "period_start_at", None) not in affected),
            *replacements.values(),
        ),
        key=lambda row: row.period_start_at,
    ))
    view = replace(
        prior,
        rows=rows,
        total_cost_usd=stable_sum(row.cost_usd for row in rows),
        total_tokens=sum(row.total_tokens for row in rows),
        period_start=(periods[0].start_at if periods else None),
        period_end=now_utc,
    )
    _CODEX_WEEKLY_VIEW_CACHE[cache_key] = (signature, view, groups)
    return view


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
        # #373: the retained `codex_bengalfox` 5h rows are on the PRIMARY slot —
        # the same slot this account aggregate reads — so a foreign pool at 95%
        # would win the max outright over the real account window.
        baseline = select_baseline(history.observations, now_utc)
        if codex_history_is_model_scoped(history, baseline=baseline):
            continue
        # #428: the anchor — a 5h window whose raw reset has passed but whose
        # canonical anchor has not is still live and still owns its percent.
        if baseline is None or baseline.canonical_resets_at <= now_utc:
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
    reasoningOutputTokens, totalTokens, unattributed?, spendWindow?}``;
    ``hero_cycles_wire`` is the thin per-account cycle-boundary list the hero
    renders (``cycles[]``).
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
    # #564: a card with no live cycle is read over ONE native cycle width
    # ending at `now`, never the whole accounting range. The decorated hero is
    # the sum of these cards under a week label, so an addend spanning the full
    # ~30-day range put spend that label does not cover into the headline.
    #
    # The start comes from `now_utc`, NOT `accounting_end`: the latter is
    # `now + 1us`, an adapter that lets an inclusive-now surface call a
    # half-open reader, so subtracting the width from it would drop a row
    # landing exactly on the boundary while keeping one landing at `now`.
    fallback_start = max(
        accounting_start,
        context.now_utc - dt.timedelta(minutes=ACCOUNT_WEEKLY_WINDOW_MINUTES),
    )
    fallback_window = {
        "kind": "trailing-cycle",
        "startAt": fallback_start.astimezone(UTC).isoformat(),
        "endAt": context.now_utc.astimezone(UTC).isoformat(),
    }
    # Include unattributed last iff it has cycle/5h/spend evidence.
    unattributed_rows = load_cached_rooted_codex_accounting_entries(
        accounting_start, accounting_end, speed=context.speed,
        cache_conn=context.cache_conn, account_key=_lib_accounts.UNATTRIBUTED,
    )
    # Existence is decided over the accounting range so a sentinel holding only
    # older spend keeps its card; the totals below cover the bounded window, so
    # a resolved $0.00 is an honest empty state rather than an absence (#564).
    # The bounded set is a strict subset of the rows already loaded above, so it
    # is derived in memory rather than re-queried on every publish. `timestamp`
    # is normalized to UTC by the reader and the upper bound is already applied,
    # so the two predicates coincide.
    unattributed_window_rows = tuple(
        row for row in unattributed_rows if row.timestamp >= fallback_start
    )
    if (
        unattributed_rows
        or _lib_accounts.UNATTRIBUTED in cycle_by_account
        or _lib_accounts.UNATTRIBUTED in five_hour
    ):
        ordered_keys.append(_lib_accounts.UNATTRIBUTED)
    # #416 §6: population-aware labels, so two Codex accounts that auto-label to
    # one email do not render two identical chips. Collision-only (D5): a
    # non-colliding label is untouched.
    _codex_label_map = _cctally_account.display_label_map(
        context.stats_conn, "codex")

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
            totals = _totals(unattributed_window_rows)
        else:
            # A real account without a live weekly cycle: totals over ONE
            # native cycle width ending now, so this card can be summed into a
            # week-labelled headline without overstating it (#564). No bars or
            # reset, because there is no live cycle to describe.
            rows = load_cached_rooted_codex_accounting_entries(
                fallback_start, accounting_end, speed=context.speed,
                cache_conn=context.cache_conn, account_key=key,
            )
            totals = _totals(rows)
        card: dict[str, object] = {
            "accountKey": key,
            "label": _codex_label_map.get(key) or _cctally_account.account_label(context.stats_conn, key),
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
        elif cyc is not None and cyc.evidence_stale:
            # #360 / #416 closeout: freshness belongs to the account whose
            # resolved cycle supplied the card. The aggregate hero marker
            # cannot speak for a fresh sibling, and staleness is disclosure
            # only — the retained percentage, reset and spend remain useful.
            card["cycleFreshness"] = "stale"
        if is_unattributed or cyc is None:
            # The card's totals came from the bounded fallback rather than a
            # live cycle, so it publishes the exact window it covers. The client
            # reads this key and never infers the case from a null `resetsAt`,
            # which is true of several unrelated states (#564 D3).
            card["spendWindow"] = fallback_window
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


def _codex_partition_by_account(
    entries: Iterable[object],
) -> dict[str, tuple[object, ...]]:
    """Partition already-loaded Codex accounting rows by their stamped account.

    #416 spec §5.2/§5.3 (review F9/F10). The account axis is added by splitting
    rows that are ALREADY in memory and re-running the SHIPPED builders per
    partition — not by threading an account through `CodexEntry`,
    `QualifiedCodexEntry`'s grouping keys, or the shared aggregator kernels. Two
    consequences, both load-bearing:

    * the merged parent is byte-identical BY CONSTRUCTION, because the parent's
      code path is literally unchanged; and
    * `_aggregate_codex_buckets` accumulates in encounter order and preserves
      first-seen model order plus merged `model_breakdowns`, so ENCOUNTER ORDER
      IS PRESERVED within each partition here. Sorting, or partitioning through
      a set, would move a bucket's `models` order for free.

    `NULL ≡ unattributed` — a row with no stamp lands in the reserved sentinel
    bucket, which stays selectable because after D1 it holds the bulk of Codex
    history.
    """
    buckets: dict[str, list[object]] = {}
    for entry in entries:
        key = str(
            getattr(entry, "account_key", "") or _lib_accounts.UNATTRIBUTED)
        buckets.setdefault(key, []).append(entry)
    return {key: tuple(values) for key, values in buckets.items()}


def _codex_fold_visible_rows(
    entries: Iterable[object],
) -> "tuple[list[CodexEntry], dict[str, tuple[object, ...]], dict[str, tuple[CodexEntry, ...]]]":
    """One encounter-ordered pass producing the parent's and each account's rows.

    #566 §5.1 item 2. Each visible row is adapted to a ``CodexEntry`` exactly
    once and then routed into the merged "All" list and into its owning
    account's list, instead of the parent converting the whole population and
    every child re-converting its own slice.

    This removes exactly the four whole-population re-adaptations the children
    performed, worth about 0.4s of a profiled tick on the maintainer's store.
    It does NOT reduce the 191,225 total calls to
    ``_codex_entries_from_accounting`` that a build makes: 191,220 of them come
    from ``_build_codex_native_weekly_view``, which adapts one entry at a time
    per scope, and this fold does not touch that site.

    Encounter order is preserved in every output, and the ordering matters:
    ``_aggregate_codex_buckets`` accumulates in encounter order and preserves
    first-seen model order, so routing through a set, or sorting, would move a
    bucket's ``models`` order for free. Adaptation is
    1:1 and order-preserving, so each account's list is byte-identical to
    adapting that account's rows on their own — which is what makes the fold a
    reuse of work rather than a change to any builder's arithmetic. The
    shipped builders still run per scope, so the merged parent stays
    byte-identical BY CONSTRUCTION (#416 §5.2 review F9/F10).
    """
    rows = tuple(entries)
    all_entries = _codex_entries_from_accounting(rows)
    rows_by_account: dict[str, list[object]] = {}
    entries_by_account: dict[str, list[CodexEntry]] = {}
    for row, converted in zip(rows, all_entries):
        key = str(
            getattr(row, "account_key", "") or _lib_accounts.UNATTRIBUTED)
        rows_by_account.setdefault(key, []).append(row)
        entries_by_account.setdefault(key, []).append(converted)
    return (
        all_entries,
        {key: tuple(values) for key, values in rows_by_account.items()},
        {key: tuple(values) for key, values in entries_by_account.items()},
    )


_CODEX_ACCOUNT_SCOPE_CACHE: dict[
    str, tuple[object, dict[str, object]]
] = {}


def reset_codex_account_scope_cache() -> None:
    """Test/process reset for #582's immutable finalized account scopes."""
    _CODEX_ACCOUNT_SCOPE_CACHE.clear()
    _CODEX_ENTRY_ADAPTER_CACHE.clear()
    _CODEX_PROJECT_LABEL_CACHE.clear()
    _CODEX_PERIOD_VIEW_CACHE.clear()
    _CODEX_WEEKLY_VIEW_CACHE.clear()
    _CODEX_CACHE_REPORT_ROWS.clear()
    _CODEX_SESSION_VIEW_CACHE.clear()
    _CODEX_PROJECT_WIRE_CACHE.clear()


def _codex_account_scopes_wire(
    context: DashboardReadContext,
    *,
    account_keys: Iterable[str],
    quota_observations: Iterable[object],
    cycle_by_account: Mapping[str, "CodexCycleBoundary"],
    visible_accounting_entries: Iterable[object],
    visible_rows_by_account: "Mapping[str, tuple[object, ...]] | None" = None,
    visible_entries_by_account: "Mapping[str, tuple[CodexEntry, ...]] | None" = None,
    active_roots: Iterable[str],
    accounting_end: dt.datetime,
    metadata_incomplete: bool,
    conversation_metadata: Mapping[tuple[str, str], Mapping[str, object]],
    alerts: Iterable[Mapping[str, object]],
    budget_milestones: Iterable[Mapping[str, object]],
    projected_budget_milestones: Iterable[Mapping[str, object]],
    budget_cost_events_by_account: Mapping[str, tuple[tuple[dt.datetime, float], ...]],
    private_session_labels: dict[str, str],
    hero_failure: bool = False,
    dirty_accounts: Iterable[str] = (),
    scope_signature: object | None = None,
    changed_old_by_account: Mapping[str, tuple[CodexEntry, ...]] | None = None,
    changed_new_by_account: Mapping[str, tuple[CodexEntry, ...]] | None = None,
    changed_old_rows_by_account: Mapping[str, tuple[object, ...]] | None = None,
    changed_new_rows_by_account: Mapping[str, tuple[object, ...]] | None = None,
) -> dict[str, dict[str, object]]:
    """The per-account CHILDREN of the merged Codex read model (spec §5.3).

    Caller must gate on `provider_is_decorated(stats_conn, "codex")` — this
    builds nothing for a <=1-real-account install, so the whole surface is ABSENT
    rather than present-and-empty and the envelope stays byte-identical (R8).

    Each child mirrors the parent's own key shape (`periods` / `sessions` /
    `projects` / `cache_report` / `budget` / `quota` / `alerts`) so one client
    selector can return a structurally identical object for "All accounts" (the
    parent) and for a focused account (its child). Nothing is summed on the
    client: §5.3 established that scalar summation cannot reconstruct `models` /
    `model_breakdowns` and that weekly `used_pct` / `dollar_per_pct` are not
    additive at all.

    `is_empty` is the explicit empty state from §6 and acceptance criterion 2 —
    an account with no evidence renders blank rather than the PREVIOUS account's
    numbers, which is the literal reported symptom.

    Every read a child reaches that is NOT already covered by the in-memory
    partition is account-scoped explicitly (#416 Slice 3A review B1/B2/B3):
    `_codex_weekly_periods`, `_quota_wire`, `codex_quota_breakdown` (its block
    boundary AND its accounting), the 5h correlation load, and the cycle index.
    A read that filters by root, time or slot but not by account is the defect
    CLASS this section exists to close — `quota_window_blocks` and
    `quota_window_snapshots` both carry two rows when two accounts share one
    physical root.
    """
    visible = tuple(visible_accounting_entries)
    observations = tuple(quota_observations)
    # #566 §5.1 item 2: the caller folded the visible rows once and hands both
    # partitions down. Re-deriving them here is retained only for direct
    # callers (tests, the source-detail reader) that have no fold to share.
    if visible_rows_by_account is None or visible_entries_by_account is None:
        _all, visible_rows_by_account, visible_entries_by_account = (
            _codex_fold_visible_rows(visible)
        )
    partition = visible_rows_by_account
    entries_partition = visible_entries_by_account
    obs_partition: dict[str, list[object]] = {}
    for observation in observations:
        obs_partition.setdefault(
            observation.identity.account_key, []).append(observation)
    alert_rows = tuple(alerts)
    budget_rows = tuple(budget_milestones)
    projected_rows = tuple(projected_budget_milestones)
    roots = tuple(active_roots)
    dirty_account_keys = {str(key) for key in dirty_accounts}
    changed_old_by_account = changed_old_by_account or {}
    changed_new_by_account = changed_new_by_account or {}
    changed_old_rows_by_account = changed_old_rows_by_account or {}
    changed_new_rows_by_account = changed_new_rows_by_account or {}

    def _for_account(key: str) -> dict[str, object]:
        rows = partition.get(key, ())
        account_observations = tuple(obs_partition.get(key, ()))
        entries = list(entries_partition.get(key, ()))
        cycle = cycle_by_account.get(key)
        sessions_view = (
            build_rooted_codex_session_view(
                rows, now_utc=context.now_utc,
                tz_name=context.display_tz_name, speed=context.speed,
            )
            if metadata_incomplete else _cached_codex_session_view(
                entries,
                changed_old=changed_old_rows_by_account.get(key, ()),
                changed_new=changed_new_rows_by_account.get(key, ()),
                cache_key=("account", key),
                semantic_signature=scope_signature,
                now_utc=context.now_utc,
                tz_name=context.display_tz_name, speed=context.speed,
            )
        )
        quota = _quota_read_model(
            context, account_observations, accounting_entries=rows,
            account_key=key,
            # #429 §4.4: this wire is only ever reached under decoration — the
            # caller gates the whole `account_scopes` surface on it.
            decorated=True,
        )
        # Each decorated child owns the only honest cycle index for that
        # account. Reusing a merged parent index here would render account A's
        # milestone HISTORY on account B's hero. The index is derivable from
        # the child's own `CodexCycleBoundary` plus `stats_conn`; no cycle =>
        # `()`, an honest empty state, never another account's ledger.
        cycle_index: tuple = ()
        if cycle is not None and not hero_failure:
            try:
                cycle_index = tuple(
                    sys.modules["cctally"].build_codex_cycle_index(
                        context.stats_conn, identity=cycle,
                        now_utc=context.now_utc, account_key=key,
                    )
                )
            except sqlite3.Error:
                cycle_index = ()
        quota = {
            **quota,
            "blocks": _quota_wire(
                context.stats_conn, accounting_entries=rows, cycle=cycle,
                now_utc=context.now_utc,
                display_tz_name=context.display_tz_name,
                account_key=key, decorated=True,
            ),
            "cycle_index": cycle_index,
        }
        return {
            # An account is empty when it owns neither accounting rows nor quota
            # evidence. Both axes matter: a brand-new account can carry a live
            # quota window with no spend yet, and a retired one the reverse.
            "is_empty": not rows and not account_observations,
            "periods": {
                "daily": _period_wire(_cached_codex_period_view(
                    entries,
                    changed_old=changed_old_by_account.get(key, ()),
                    changed_new=changed_new_by_account.get(key, ()),
                    kind="daily", cache_key=("account", key),
                    semantic_signature=scope_signature,
                    now_utc=context.now_utc, tz_name=context.display_tz_name,
                    speed=context.speed,
                )),
                "monthly": _period_wire(_cached_codex_period_view(
                    entries,
                    changed_old=changed_old_by_account.get(key, ()),
                    changed_new=changed_new_by_account.get(key, ()),
                    kind="monthly", cache_key=("account", key),
                    semantic_signature=scope_signature,
                    now_utc=context.now_utc, tz_name=context.display_tz_name,
                    speed=context.speed,
                )),
                "weekly": _period_wire(_cached_codex_native_weekly_view(
                    context.stats_conn, rows,
                    changed_old=changed_old_rows_by_account.get(key, ()),
                    changed_new=changed_new_rows_by_account.get(key, ()),
                    cache_key=("account", key),
                    semantic_signature=scope_signature,
                    source_root_keys=roots,
                    active_cycle=cycle, now_utc=context.now_utc,
                    display_tz_name=context.display_tz_name, speed=context.speed,
                    account_key=key,
                )),
            },
            "sessions": _session_wire(
                sessions_view, metadata=conversation_metadata,
                private_labels=private_session_labels,
            ),
            "projects": (
                _partial_projects_wire(rows, conversation_metadata)
                if metadata_incomplete else _cached_projects_wire(
                    context, account_observations, rows,
                    changed_old=changed_old_rows_by_account.get(key, ()),
                    changed_new=changed_new_rows_by_account.get(key, ()),
                    accounting_end=accounting_end,
                    cache_key=("account", key),
                    semantic_signature=scope_signature,
                )
            ),
            "cache_report": _codex_cache_report_wire(
                rows, metadata=conversation_metadata, now_utc=context.now_utc,
                display_tz_name=context.display_tz_name, speed=context.speed,
                anomaly_threshold_pp=context.cache_report_anomaly_threshold_pp,
                cache_key=("account", key),
                changed_old=changed_old_rows_by_account.get(key, ()),
                changed_new=changed_new_rows_by_account.get(key, ()),
                semantic_signature=scope_signature,
            ),
            "budget": {
                **_codex_budget_status_domain(
                    context, rows,
                    cost_events=budget_cost_events_by_account.get(key, ()),
                    account_key=key,
                ),
                "milestones": tuple(_codex_account_scoped_rows(budget_rows, key)),
                "projected": tuple(_codex_account_scoped_rows(projected_rows, key)),
            },
            "quota": quota,
            "alerts": {
                "rows": tuple(_codex_account_scoped_rows(alert_rows, key)),
                "actual_thresholds": context.codex_quota_actual_thresholds,
                "projected_thresholds": context.codex_quota_projected_thresholds,
            },
        }

    # #416 Slice 3A review B4. The requested key set comes from the stats
    # `accounts` REGISTRY (the hero cards) while the two partitions above key
    # off the DATA (`codex_session_entries.account_key` and each observation's
    # identity). Cache/stats drift — a stats rebuild that has not re-registered
    # an account, or a key stamped by a newer binary — therefore left rows in a
    # bucket with NO scope, so the union of the children was silently LESS than
    # the parent with no warning. Residual data keys become scopes of their own:
    # nothing is folded into `unattributed` (that would misattribute a KNOWN
    # key, which D1 forbids) and nothing is dropped. Such a scope has no hero
    # card, so it is simply not chip-selectable until the registry catches up —
    # a safe degrade, never a silent loss.
    #
    # #416 closeout F2: the durable projection is the THIRD axis. Those two
    # partitions cover only the rows this build loaded, and both loads are
    # bounded (`DASHBOARD_QUOTA_RECENT_DAYS` / `DASHBOARD_QUOTA_OBSERVATION_-
    # LIMIT`, and the visible accounting window) while `quota_window_blocks`
    # retains its account stamp indefinitely. A key that survives only there —
    # an older cycle, or a cache pruned behind a retained projection — was in no
    # bucket at all, which is B4's own failure on the axis B4 missed.
    ordered_keys = list(dict.fromkeys(str(key) for key in account_keys))
    residual_keys = sorted(
        (set(partition) | set(obs_partition) | _codex_block_account_keys(
            context.stats_conn, roots)) - set(ordered_keys)
    )
    result: dict[str, dict[str, object]] = {}
    live_keys = ordered_keys + residual_keys
    for key in live_keys:
        account_observations = tuple(obs_partition.get(key, ()))
        account_metadata = tuple(
            (identity, conversation_metadata.get(identity))
            for identity in sorted({
                (
                    str(getattr(row, "source_root_key", "")),
                    str(getattr(row, "source_path", "")),
                )
                for row in partition.get(key, ())
            })
        )
        signature = (
            scope_signature,
            account_observations,
            cycle_by_account.get(key),
            account_metadata,
            tuple(_codex_account_scoped_rows(alert_rows, key)),
            tuple(_codex_account_scoped_rows(budget_rows, key)),
            tuple(_codex_account_scoped_rows(projected_rows, key)),
            budget_cost_events_by_account.get(key, ()),
            context.codex_budget,
            context.codex_quota_actual_thresholds,
            context.codex_quota_projected_thresholds,
            context.cache_report_anomaly_threshold_pp,
            metadata_incomplete,
            hero_failure,
        )
        cached = _CODEX_ACCOUNT_SCOPE_CACHE.get(key)
        if (
            scope_signature is not None
            and key not in dirty_account_keys
            and cached is not None
            and cached[0] == signature
        ):
            result[key] = cached[1]
            continue
        value = _for_account(key)
        result[key] = value
        if scope_signature is not None:
            _CODEX_ACCOUNT_SCOPE_CACHE[key] = (signature, value)
    for stale_key in set(_CODEX_ACCOUNT_SCOPE_CACHE) - set(live_keys):
        _CODEX_ACCOUNT_SCOPE_CACHE.pop(stale_key, None)
    return result


def _codex_block_account_keys(
    stats_conn: sqlite3.Connection, roots: Iterable[str],
) -> set[str]:
    """Every account stamped on a retained Codex block over ``roots`` (#416 F2).

    Scoped to the child-visible cycle roots, so an unrelated root's account
    cannot manufacture a scope. `quota_window_blocks.account_key` is NOT NULL
    DEFAULT `unattributed`, and the `or UNATTRIBUTED` is belt-and-suspenders for
    a store written before that default landed. A read failure degrades to the
    two in-memory axes rather than failing the whole child build.
    """
    root_keys = tuple(dict.fromkeys(str(root) for root in roots))
    if not root_keys:
        return set()
    placeholders = ",".join("?" for _ in root_keys)
    assert_projection_readable(stats_conn)
    try:
        return {
            str(row[0] or _lib_accounts.UNATTRIBUTED)
            for row in stats_conn.execute(
                "SELECT DISTINCT account_key FROM quota_window_blocks "
                "WHERE source='codex' AND orphaned_at IS NULL "
                f"AND source_root_key IN ({placeholders})",
                root_keys,
            )
        }
    except sqlite3.Error:
        return set()


def _codex_account_scoped_rows(
    rows: Iterable[Mapping[str, object]], account_key: str,
) -> list[Mapping[str, object]]:
    """Rows this account owns, PLUS the vendor-wide ``*`` rows (spec §5.4).

    A vendor-wide budget crossing is not attributable to one account, so hiding
    it under focus would silently drop a real alert. It stays visible and keeps
    its ``account_key == "*"`` so the client can label it as vendor-wide rather
    than as this account's.
    """
    return [
        row for row in rows
        if row.get("account_key") in (account_key, _CODEX_VENDOR_WIDE_ACCOUNT)
    ]


_CODEX_VENDOR_WIDE_ACCOUNT = "*"


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

    _claude_label_map = _cctally_account.display_label_map(stats_conn, "claude")

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
            "label": _claude_label_map.get(key) or _cctally_account.account_label(stats_conn, key),
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


def _codex_ingest_backlog_wire(
    cache_conn: sqlite3.Connection,
) -> "dict[str, object] | None":
    """The hook's budgeted-ingest backlog, or ``None`` when there is none.

    Public #5 spec §5. Additive and OMITTED ENTIRELY at zero, so the normal
    payload stays byte-identical for every install whose Codex history is
    already ingested — which is nearly all of them, nearly all the time.

    It deliberately does NOT touch ``availability`` or ``freshness``. Those are
    read by a long and explicitly non-exhaustive list of gates, and degrading a
    shared signal for a domain-local condition is a failure this project has
    already hit; the client renders "history still loading" from this field
    without any shared axis moving.

    Any read or shape failure degrades to ``None``. The record is a health
    signal, not evidence, and a hand-edited one must never be able to fail the
    envelope build.
    """
    try:
        row = cache_conn.execute(
            "SELECT value FROM cache_meta WHERE key = ? LIMIT 1",
            ("codex_ingest_backlog",),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if not row or not row[0]:
        return None
    try:
        record = json.loads(str(row[0]))
        files = int(record["files"])
        pending_bytes = int(record["bytes"])
    except (ValueError, TypeError, KeyError):
        return None
    if files <= 0:
        return None
    since = record.get("since")
    return {
        "files": files,
        "bytes": pending_bytes,
        "since": None if since is None else str(since),
    }


def build_codex_source_state(
    context: DashboardReadContext,
    *,
    data_version: str,
) -> SourceDashboardState:
    """Build Codex data strictly from the coordinated cache/stats reads.

    No sync, rollout scan, CLI parser, or fallback is reachable from this
    adapter.  Period and session arithmetic remains delegated to the shipped
    S3 view kernels, preserving the CLI's inclusive-token vocabulary.

    The whole read runs under ONE ``codex_path_scope`` (#566 §5.1 item 1), so
    the merged parent view and every per-account child share a single session
    root resolution and a single parse per distinct session file. The scope is
    opened here rather than further out because this is the boundary that owns
    every Codex session view in the build, and it is discarded when the read
    returns.
    """
    # This memo deduplicates the several account/parent consumers inside ONE
    # coordinated source build. It may not cross that boundary: a caller can
    # deliberately request a fresh build after stats/account decoration changes
    # without advancing cache.db's quota ledger, and the established contract
    # requires one bounded physical load for that new build.
    reset_codex_quota_observation_cache()
    caches = (
        _CODEX_QUOTA_OBSERVATION_CACHE,
        _CODEX_PERIOD_VIEW_CACHE,
        _CODEX_CACHE_REPORT_ROWS,
        _CODEX_SESSION_VIEW_CACHE,
        _CODEX_PROJECT_LABEL_CACHE,
        _CODEX_PROJECT_WIRE_CACHE,
        _CODEX_ENTRY_ADAPTER_CACHE,
        _CODEX_WEEKLY_VIEW_CACHE,
        _CODEX_ACCOUNT_SCOPE_CACHE,
    )
    cache_checkpoint = tuple(dict(cache) for cache in caches)
    accounting_checkpoint = (
        _lib_snapshot_cache.checkpoint_codex_accounting_cache_state()
    )
    try:
        with codex_path_scope() as path_scope:
            return _build_codex_source_state(
                context, data_version=data_version, path_scope=path_scope,
            )
    except Exception:
        for cache, prior in zip(caches, cache_checkpoint):
            cache.clear()
            cache.update(prior)
        _lib_snapshot_cache.restore_codex_accounting_cache_state(
            accounting_checkpoint)
        raise


def _build_codex_source_state(
    context: DashboardReadContext,
    *,
    data_version: str,
    path_scope: object,
) -> SourceDashboardState:
    active_roots = tuple(sorted(
        str(row[0]) for row in context.cache_conn.execute(
            "SELECT source_root_key FROM codex_source_roots"
        )
    ))
    quota_observations = _cached_codex_quota_observations(
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
    ingest_backlog = _codex_ingest_backlog_wire(context.cache_conn)
    # The cache reader's established report surface treats the ``now`` instant
    # as inclusive.  The qualified adapter is half-open, so extend only its
    # query/result boundary by one microsecond and keep all live budget sums
    # explicitly half-open at ``now`` below.
    accounting_end = context.now_utc + dt.timedelta(microseconds=1)
    accounting_start = context.range_start
    if context.codex_budget is not None:
        # #556 S5 Unit 2 (Unit 1 review R6, widened) — this is the SECOND
        # unguarded call to the window resolver, and unlike
        # `_codex_budget_cost_events` it had no boundary of its own. It sits
        # outside every other `try` in this function, so an unresolvable window
        # escaped into `_tui_build_source_bundle`'s `source_build_failed`
        # handler and destroyed the entire Codex provider's data — the exact
        # failure §3.5 exists to prevent, reached from a different line.
        #
        # Degrading to the un-widened accounting range cannot publish a false
        # figure: the same failure reaches `_codex_budget_status_domain`, which
        # nulls the status and names `budget_compute_failed`.
        try:
            _period, budget_start, _budget_end = _configured_codex_budget_window(context)
        except Exception:
            _warn_codex_budget_window_once("accounting_range")
        else:
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
    accounting_dirty_accounts: tuple[str, ...] = ()
    accounting_changed_old: tuple[object, ...] = ()
    accounting_changed_new: tuple[object, ...] = ()
    if not metadata_incomplete:
        try:
            cached_accounting = _lib_snapshot_cache.build_cached_codex_accounting(
                cache_conn=context.cache_conn,
                range_start=accounting_start,
                range_end=accounting_end,
                extra_signature=(
                    context.speed,
                    tuple(str(root) for root in path_scope.roots),
                    active_roots,
                ),
                load_all=lambda: load_qualified_codex_entries(
                    accounting_start,
                    accounting_end,
                    speed=context.speed,
                    sync=False,
                    cache_conn=context.cache_conn,
                ),
                load_paths=lambda identities: load_qualified_codex_entries(
                    accounting_start,
                    accounting_end,
                    speed=context.speed,
                    sync=False,
                    cache_conn=context.cache_conn,
                    source_identities=identities,
                ),
                path_of=lambda entry: (
                    str(entry.source_root_key), str(entry.source_path),
                ),
                account_of=lambda entry: str(entry.account_key),
                order_key=lambda entry: (
                    entry.timestamp,
                    str(entry.source_root_key),
                    str(entry.conversation_key),
                    int(entry.cache_entry_id),
                ),
                identity_of=lambda entry: int(entry.cache_entry_id),
            )
            qualified_entries = cached_accounting.entries
            accounting_dirty_accounts = cached_accounting.dirty_accounts
            accounting_changed_old = cached_accounting.changed_old
            accounting_changed_new = cached_accounting.changed_new
            accounting_entries: tuple[object, ...] = qualified_entries
        except QualifiedMetadataUnavailable:
            # A cached read must be internally coherent, but retain accounting
            # once if a defensive race or malformed row violates that premise.
            _lib_log.get_logger("dashboard").warning(
                "Codex qualified metadata read became unavailable; using cache-only accounting fallback"
            )
            metadata_incomplete = True
            _lib_snapshot_cache.reset_codex_accounting_cache_state()
            accounting_entries = load_cached_rooted_codex_accounting_entries(
                accounting_start,
                accounting_end,
                speed=context.speed,
                cache_conn=context.cache_conn,
            )
            accounting_dirty_accounts = tuple(sorted({
                str(getattr(entry, "account_key", "") or
                    _lib_accounts.UNATTRIBUTED)
                for entry in accounting_entries
            }))
    else:
        _lib_snapshot_cache.reset_codex_accounting_cache_state()
        accounting_entries = load_cached_rooted_codex_accounting_entries(
            accounting_start,
            accounting_end,
            speed=context.speed,
            cache_conn=context.cache_conn,
        )
        accounting_dirty_accounts = tuple(sorted({
            str(getattr(entry, "account_key", "") or
                _lib_accounts.UNATTRIBUTED)
            for entry in accounting_entries
        }))
    budget_entries = _codex_entries_from_accounting(accounting_entries)
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
    except CodexCycleUnavailable:
        # #556 S1 §4.1: the reason no longer moves a freshness axis. A
        # `stale` reason is observation AGE, which `quota` owns; every reason
        # here leaves `cycle` unresolved, which `cycle_failure` below turns
        # into the hero failure the accounting axis actually reports.
        cycle = None
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
    # #566 §5.1 item 2: one pass over the visible rows produces the merged
    # population and both per-account partitions. The children below reuse
    # these instead of re-partitioning and re-adapting the same rows.
    entries, visible_rows_by_account, visible_entries_by_account = (
        _codex_fold_visible_rows(visible_accounting_entries)
    )
    changed_old_visible = tuple(
        entry for entry in accounting_changed_old
        if context.range_start <= entry.timestamp.astimezone(UTC) < accounting_end
    )
    changed_new_visible = tuple(
        entry for entry in accounting_changed_new
        if context.range_start <= entry.timestamp.astimezone(UTC) < accounting_end
    )
    changed_old_entries = tuple(_codex_entries_from_accounting(changed_old_visible))
    changed_new_entries = tuple(_codex_entries_from_accounting(changed_new_visible))

    def _changed_by_account(rows, converted):
        grouped: dict[str, list[CodexEntry]] = {}
        for row, entry in zip(rows, converted):
            key = str(getattr(row, "account_key", "") or
                      _lib_accounts.UNATTRIBUTED)
            grouped.setdefault(key, []).append(entry)
        return {key: tuple(values) for key, values in grouped.items()}

    changed_old_by_account = _changed_by_account(
        changed_old_visible, changed_old_entries)
    changed_new_by_account = _changed_by_account(
        changed_new_visible, changed_new_entries)

    def _changed_rows_by_account(rows):
        grouped: dict[str, list[object]] = {}
        for row in rows:
            key = str(getattr(row, "account_key", "") or
                      _lib_accounts.UNATTRIBUTED)
            grouped.setdefault(key, []).append(row)
        return {key: tuple(values) for key, values in grouped.items()}

    changed_old_rows_by_account = _changed_rows_by_account(changed_old_visible)
    changed_new_rows_by_account = _changed_rows_by_account(changed_new_visible)
    # The published provider version also carries quota/stat generations.
    # Those generations must rebuild quota domains, but they are not accounting
    # semantics: folding them into these cache keys made one fresh quota sample
    # discard every clean period/session/project/account group.  The accounting
    # population cache above owns upper-bound, root and speed invalidation; the
    # individual builders add their own tz/speed/period/cycle dimensions.
    period_signature = (
        "codex-accounting-v1", context.range_start, metadata_incomplete,
    )
    daily = _cached_codex_period_view(
        entries, changed_old=changed_old_entries,
        changed_new=changed_new_entries, kind="daily", cache_key=("parent",),
        semantic_signature=period_signature, now_utc=context.now_utc,
        tz_name=context.display_tz_name, speed=context.speed,
    )
    monthly = _cached_codex_period_view(
        entries, changed_old=changed_old_entries,
        changed_new=changed_new_entries, kind="monthly", cache_key=("parent",),
        semantic_signature=period_signature, now_utc=context.now_utc,
        tz_name=context.display_tz_name, speed=context.speed,
    )
    # R8 gate, resolved once before the parent weekly projection so that only
    # a decorated merged row gains the additive account axis. Focused children
    # and <=1-real-account providers remain byte-identical.
    try:
        import _cctally_account
        _codex_decorated = _cctally_account.provider_is_decorated(
            context.stats_conn, "codex")
    except Exception:
        _codex_decorated = False
    weekly = _cached_codex_native_weekly_view(
        context.stats_conn,
        visible_accounting_entries,
        changed_old=changed_old_visible,
        changed_new=changed_new_visible,
        cache_key=("parent",),
        semantic_signature=period_signature,
        source_root_keys=active_roots,
        active_cycle=cycle,
        now_utc=context.now_utc,
        display_tz_name=context.display_tz_name,
        speed=context.speed,
        include_account_keys=_codex_decorated,
    )
    sessions = (
        build_rooted_codex_session_view(
            visible_accounting_entries,
            now_utc=context.now_utc,
            tz_name=context.display_tz_name,
            speed=context.speed,
        )
        if metadata_incomplete else _cached_codex_session_view(
            entries, changed_old=changed_old_visible,
            changed_new=changed_new_visible, cache_key=("parent",),
            semantic_signature=period_signature, now_utc=context.now_utc,
            tz_name=context.display_tz_name, speed=context.speed,
        )
    )
    quota = _quota_read_model(
        context,
        quota_observations,
        accounting_entries=visible_accounting_entries,
        decorated=_codex_decorated,
    )
    # R8 gate, resolved ONCE and threaded (#341 Task 4 / #416 §5.8). Every
    # per-account decoration below — block/alert/budget `account_key`, the
    # `accounts[]` cards, `hero.cycles[]`, and the `account_scopes` children —
    # hangs off this single boolean, so a <=1-real-account install is provably
    # byte-identical by construction rather than by golden observation.
    quota_blocks = _quota_wire(
        context.stats_conn,
        accounting_entries=visible_accounting_entries,
        cycle=cycle,
        now_utc=context.now_utc,
        display_tz_name=context.display_tz_name,
        decorated=_codex_decorated,
    )
    # Hero-modal historical-milestone navigation index (spec §1c, §3). A
    # decorated parent has no single cycle history: its representative
    # `cycle` belongs to one account, while every child builds its own index
    # above. Preserve the parent index only for the byte-stable undecorated
    # shape. Guarded: an index failure must never fail the codex source build.
    cycle_index: tuple = ()
    if not _codex_decorated and cycle is not None and not hero_failure:
        try:
            cycle_index = tuple(
                sys.modules["cctally"].build_codex_cycle_index(
                    context.stats_conn, identity=cycle, now_utc=context.now_utc,
                )
            )
        except sqlite3.Error:
            cycle_index = ()
    quota = {**quota, "blocks": quota_blocks, "cycle_index": cycle_index}
    budget_rows = _budget_wire(context.stats_conn, decorated=_codex_decorated)
    projected_budget_rows = _projected_budget_wire(
        context.stats_conn, decorated=_codex_decorated)
    budget_cost_events = _codex_budget_cost_events(context, budget_entries)
    configured_budget_domain = _codex_budget_status_domain(
        context, budget_entries, cost_events=budget_cost_events,
    )
    configured_budget = configured_budget_domain["status"]
    conversation_metadata = _codex_conversation_metadata(context.cache_conn)
    cache_report = _codex_cache_report_wire(
        visible_accounting_entries,
        metadata=conversation_metadata,
        now_utc=context.now_utc,
        display_tz_name=context.display_tz_name,
        speed=context.speed,
        anomaly_threshold_pp=context.cache_report_anomaly_threshold_pp,
        cache_key=("parent",),
        changed_old=changed_old_visible,
        changed_new=changed_new_visible,
        semantic_signature=period_signature,
    )
    projects = (
        _partial_projects_wire(visible_accounting_entries, conversation_metadata)
        if metadata_incomplete else _cached_projects_wire(
            context,
            quota_observations,
            visible_accounting_entries,
            changed_old=changed_old_visible,
            changed_new=changed_new_visible,
            accounting_end=accounting_end,
            cache_key=("parent",),
            semantic_signature=period_signature,
        )
    )
    alerts = _alerts_wire(context.stats_conn, decorated=_codex_decorated)
    # Built here, BEFORE the children, so the parent's session wire is the first
    # writer into `private_session_labels` and the children (strict subsets of
    # the parent's rows) can only ever re-derive the same entries.
    private_session_labels: dict[str, str] = {}
    sessions_wire = _session_wire(
        sessions,
        metadata=conversation_metadata,
        private_labels=private_session_labels,
    )
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
    account_scopes: dict[str, dict[str, object]] = {}
    # #556 S5 §3.8: bound OUTSIDE the try, because the degrade path below has to
    # be able to clear it, and the retained `clock_data` reads it either way.
    budget_events_by_account: dict[str, tuple[tuple[dt.datetime, float], ...]] = {}
    if _codex_decorated:
        try:
            accounts_wire, hero_cycles_wire = _codex_accounts_wire(
                context,
                quota_observations=quota_observations,
                cycles=cycles_all,
                accounting_start=accounting_start,
                accounting_end=accounting_end,
            )
            # #416 §5.3: the per-account CHILDREN beside the merged parent. The
            # scope set is exactly the card set, so every chip the client can
            # focus resolves to a scope (an account with no evidence gets an
            # explicit `is_empty` child, never a missing key the client would
            # have to fall back from).
            cycle_by_account: dict[str, CodexCycleBoundary] = {}
            for cyc in cycles_all:
                cycle_by_account.setdefault(
                    (
                        cyc.quota_identity.account_key
                        if cyc.quota_identity is not None
                        else _lib_accounts.UNATTRIBUTED
                    ),
                    cyc,
                )
            # Budget cost events are frozen per account over the CONFIGURED
            # budget window, which can start before `range_start` — so they come
            # from the full `accounting_entries`, not the visible slice.
            budget_events_by_account = ({
                key: _codex_budget_cost_events(context, rows)
                for key, rows in _codex_partition_by_account(
                    accounting_entries).items()
            } if context.codex_budget is not None else {})
            account_scopes = _codex_account_scopes_wire(
                context,
                account_keys=[str(card["accountKey"]) for card in accounts_wire],
                quota_observations=quota_observations,
                cycle_by_account=cycle_by_account,
                visible_accounting_entries=visible_accounting_entries,
                visible_rows_by_account=visible_rows_by_account,
                visible_entries_by_account=visible_entries_by_account,
                active_roots=active_roots,
                accounting_end=accounting_end,
                metadata_incomplete=metadata_incomplete,
                conversation_metadata=conversation_metadata,
                alerts=alerts,
                budget_milestones=budget_rows,
                projected_budget_milestones=projected_budget_rows,
                budget_cost_events_by_account=budget_events_by_account,
                private_session_labels=private_session_labels,
                hero_failure=hero_failure,
                dirty_accounts=accounting_dirty_accounts,
                changed_old_by_account=changed_old_by_account,
                changed_new_by_account=changed_new_by_account,
                changed_old_rows_by_account=changed_old_rows_by_account,
                changed_new_rows_by_account=changed_new_rows_by_account,
                # Quota/stat generations are already represented by each
                # child's quota observations, cycle and alert/budget rows in
                # `_codex_account_scopes_wire`'s outer signature. Reuse the
                # accounting-only semantic key here so an unrelated account's
                # fresh quota sample cannot evict every clean child.
                scope_signature=period_signature,
            )
            # #416 QA P1-A — the "All accounts" Blocks panel is the UNION of
            # every account's 5-hour blocks. `_quota_wire` filters
            # `str(root_key) not in cycle.source_root_keys` against a single
            # `cycle` that is `cycles_all[0]`, so in the production shape (one
            # Codex root per account) every SIBLING account's live block is
            # dropped: the merged panel read "1 blocks · $0.86" while focusing
            # the sibling revealed a second live block the merged view never
            # showed. That is an UNDERCOUNT, not a misattribution — the strict
            # account predicate `_quota_wire` applies under focus is correct and
            # is untouched; the defect is on the separate CYCLE-ROOT axis.
            #
            # The merge is STRICTLY ADDITIVE over today's parent read: every
            # child's rows are unioned in, and every row the representative-cycle
            # read already produced is kept. Taking the children ALONE was the
            # obvious construction (it mirrors P0-A's sum-of-the-cards, and it
            # makes the merged list incapable of disagreeing with the chip the
            # operator focuses next) but it LOSES rows: a child whose account has
            # no live cycle passes `cycle=None` to `_quota_wire`, which returns
            # `()` — so an account whose key survives only in
            # `quota_window_blocks` (the third stamping axis; see
            # `test_a_block_only_account_key_still_gets_a_scope`) vanished from
            # the merged view that used to list it. Electing a sibling's cycle to
            # bound it instead would be the very defect this fixes, so the parent
            # keeps its own rows and gains the siblings'.
            #
            # Dedup is on `(key, account_key)`: the opaque block key is built
            # from root/limit/slot/window/reset and deliberately excludes the
            # account, so two accounts sharing one physical root are two rows
            # with one key — never-combine extends to accounts.
            #
            # Each row keeps its own `current_percent` and its own `account_key`,
            # so this is a LISTING of independent windows, never a blend (D6).
            # Ordering mirrors `_quota_wire`'s own `resets_at DESC` with the
            # opaque key as the deterministic tie-break.
            by_identity: dict[tuple[str, str], dict[str, object]] = {}
            for block in (
                *(
                    row
                    for scope in account_scopes.values()
                    for row in scope["quota"]["blocks"]
                ),
                *quota["blocks"],
            ):
                by_identity.setdefault(
                    (str(block["key"]), str(block.get("account_key", ""))), block)
            merged_blocks = tuple(sorted(
                by_identity.values(),
                key=lambda row: (
                    str(row["resets_at"]),
                    str(row["key"]),
                    str(row.get("account_key", "")),
                ),
                reverse=True,
            ))
            quota = {**quota, "blocks": merged_blocks}
        except (sqlite3.Error, QualifiedMetadataUnavailable):
            # A per-account wire failure must never fail the whole source build;
            # degrade to the byte-stable undecorated shape.
            accounts_wire = []
            hero_cycles_wire = []
            account_scopes = {}
            budget_events_by_account = {}
    # #416 QA P0-A — the "All accounts" headline is the MERGED spend and tokens
    # (spec §6, decision D6). Everything above resolves the hero from ONE
    # representative cycle (`cycles_all[0]` plus that cycle's own
    # `source_root_keys`), which in the production shape — one Codex root per
    # account — cannot see a sibling's spend at all: the headline then reads as
    # a live total while being byte-identical to a single card sitting directly
    # beneath it.
    #
    # Spend and tokens are the ONLY axes D6 lets "All accounts" merge; the
    # percentage, reset, forecast and $/1% stay per-account and the client
    # blanks them with a pointer to the cards. The merge is a SUM OF THE CARDS
    # rather than a fresh query, so the headline can never disagree with the
    # strip it sits above (an account without a live cycle contributes exactly
    # what its own card shows, over the bounded fallback window that card
    # publishes — #564). Gated on `_codex_decorated`, so a <=1-real-account
    # install keeps the single-cycle hero byte-for-byte (R8); gated on
    # `hero_failure`, so an unavailable hero stays unavailable rather than
    # gaining totals the rest of the envelope says are absent.
    if _codex_decorated and accounts_wire and not hero_failure:
        cycle_cost_usd = stable_sum(
            float(card["spendUsd"]) for card in accounts_wire)
        hero_input = sum(int(card["inputTokens"]) for card in accounts_wire)
        hero_cached = sum(int(card["cachedInputTokens"]) for card in accounts_wire)
        hero_output = sum(int(card["outputTokens"]) for card in accounts_wire)
        hero_reasoning = sum(
            int(card["reasoningOutputTokens"]) for card in accounts_wire)
        hero_total = sum(int(card["totalTokens"]) for card in accounts_wire)
    return SourceDashboardState(
        source="codex",
        availability=availability,
        # A successful source build is one coherent provider generation. Quota
        # observation age and weekly-cycle evidence live on their own axes.
        freshness="fresh",
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
                # #350 (spec §3.4): additive, hero-local staleness disclosure.
                # OMITTED when the cycle is fresh — never emitted as "fresh" —
                # for the legacy client transition. Provider metadata remains
                # coherent; ``domain_freshness.hero`` owns the shared axis.
                **(
                    {"cycle_freshness": "stale"}
                    if cycle is not None and not hero_failure and cycle.evidence_stale
                    else {}
                ),
                "quota": quota["summary"],
                "budget": configured_budget,
                "alerts": {"count": len(alerts)},
                **({"cycles": hero_cycles_wire} if _codex_decorated else {}),
            },
            **({"accounts": accounts_wire} if _codex_decorated else {}),
            # #416 §5.3 — the per-account children. Present ONLY under
            # decoration; the merged parent below is untouched by their
            # existence, which is what makes acceptance criterion 7 true by
            # construction rather than by careful re-derivation.
            **({"account_scopes": account_scopes} if _codex_decorated else {}),
            "periods": {
                "daily": _period_wire(daily),
                "monthly": _period_wire(monthly),
                "weekly": _period_wire(weekly),
            },
            "sessions": sessions_wire,
            "quota": quota,
            "budget": {
                **configured_budget_domain,
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
            # public #5 §5: additive, omitted at zero (the `cycle_freshness`
            # precedent). Never emitted as an empty object — the absence IS the
            # "nothing owed" state, which is what keeps the normal payload
            # byte-identical.
            **({"ingest_backlog": ingest_backlog}
               if ingest_backlog is not None else {}),
        },
        domain_freshness={
            # #556 S1 §4.1: `hero` means current-cycle ACCOUNTING
            # resolvability, not observation age. A stale-but-still-future
            # boundary stays RESOLVED — the spend it bounds is correct, and
            # Codex has no background quota poll, so `stale_after_seconds`
            # (3600) makes an idle weekly observation stale within the hour
            # while nothing about the accounting changed. The percent age is
            # already carried by `quota` below and by the additive hero-local
            # `cycle_freshness` field. `_resolve_codex_weekly_cycle` retains
            # only boundaries with `resets_at > now`, so a resolved cycle is
            # never expired at build time; the idle clock owns expiry.
            "hero": "stale" if hero_failure else "fresh",
            "quota": (
                "stale"
                if quota["summary"]["freshness"] == "stale"
                else "fresh"
            ),
            "sessions": "fresh",
        },
        clock_data={
            "codex_budget_cost_events": budget_cost_events,
            # #556 S5 §3.8: the per-account tuples were computed at build and
            # discarded, so idle refresh had nothing to reclock a child budget
            # from. Empty for every undecorated install, which keeps the
            # retained carrier byte-neutral there.
            "codex_budget_cost_events_by_account": budget_events_by_account,
            # #350 spec §3.3: when the tick passes this instant it must rebuild
            # Codex authoritatively instead of idle-clocking or reusing, because
            # weekly-cycle resolution can change on identical frozen evidence.
            "codex_next_decision_at": _codex_next_decision_at(
                quota_observations, cycles_all, context.now_utc,
            ),
        },
        private_session_labels=private_session_labels,
    )
