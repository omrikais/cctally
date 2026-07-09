"""Dashboard/TUI cache-report snapshot builder (#279 S5 F3).

Consumer-only sibling of ``bin/_cctally_dashboard.py`` — it re-imports every
name below, so ``bin/cctally``'s re-exports and the direct
``sys.modules["_cctally_dashboard"].X`` reaches (TUI ``bin/_cctally_tui.py``,
``bin/cctally-reconcile-test``, the pytest sites) keep resolving unchanged
(spec §2 re-export continuity).

Distinct from two same-named neighbours — keep them straight:

- ``bin/_cctally_cache_report.py`` — the ``cache-report`` CLI command.
- ``bin/_lib_cache_report.py`` — that command's pure kernel, which THIS
  module loads at call time via ``_cache_report_load_kernel`` →
  ``sys.modules["cctally"]._load_sibling("_lib_cache_report")``.

Nothing here has module-level side effects. Kernel/pricing symbols import
from their decentralized homes (``_lib_fmt`` / ``_lib_pricing``); the sole
dashboard-module-object reach is ``get_claude_session_entries``, resolved
late-binding via ``sys.modules["_cctally_dashboard"]`` at call time so the
rebuild-parity tests' spy (they patch that name on the dashboard module
object) is preserved (spec §5 gate P1-2).
"""
from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass

from _lib_fmt import stable_sum
from _lib_pricing import _calculate_entry_cost


# === Cache-report settings validator (spec 2026-05-21 §6) ================
# Validates the optional ``config.json:cache_report`` block. Strict in
# v1: only ``anomaly_threshold_pp`` is settable, must be a plain int in
# ``[1, 100]`` (bool / float / string rejected — bool because it's an
# int subclass in Python and quietly accepting ``true`` for a numeric
# field is exactly the trip-up ``_validate_update_check_ttl_hours_value``
# protects against). HTTP write path raises ``_CacheReportConfigError``
# → ``_handle_post_settings`` maps to HTTP 400 + ``{error, field}``
# (matches the existing handler convention at lines 4587-4602; spec
# explicitly says 400, NOT 422).

@dataclass(frozen=True)
class _CacheReportSettings:
    anomaly_threshold_pp: int


class _CacheReportConfigError(Exception):
    """Validation error for the ``cache_report`` config block.

    ``field`` carries the offending key path (``anomaly_threshold_pp`` or
    the unknown-key name) so the JSON 400 response can surface it.
    """
    def __init__(self, message: str, *, field: str | None = None):
        super().__init__(message)
        self.field = field


_CACHE_REPORT_ALLOWED_KEYS = frozenset({"anomaly_threshold_pp"})


def _validate_cache_report_settings(block: dict) -> dict:
    """Validate a ``cache_report`` config block.

    Pure function. Raises ``_CacheReportConfigError`` on invalid input;
    returns a dict containing ONLY the keys that were present in the
    input (validated). Callers merge the result into the existing
    persisted block instead of replacing it wholesale — this mirrors
    the ``update.check`` partial-PUT pattern at
    ``_handle_post_settings`` (~line 5277) and prevents a combined save
    that omits ``anomaly_threshold_pp`` from clobbering a previously
    persisted user value with the default.

    v1 only accepts ``anomaly_threshold_pp`` — ``anomaly_window_days``
    stays hardcoded at 14 (spec §6.1; F10 from spec §10 tracks adding
    a configurable baseline window along with the UI-copy work).
    """
    if not isinstance(block, dict):
        raise _CacheReportConfigError(
            "cache_report must be an object", field="cache_report",
        )
    for key in block:
        if key not in _CACHE_REPORT_ALLOWED_KEYS:
            raise _CacheReportConfigError(
                f"unknown key in cache_report block: {key!r}",
                field=key,
            )
    validated: dict = {}
    if "anomaly_threshold_pp" in block:
        threshold = block["anomaly_threshold_pp"]
        # bool is an int subclass — reject it explicitly (mirrors the
        # update.check.ttl_hours precedent).
        if isinstance(threshold, bool) or not isinstance(threshold, int):
            raise _CacheReportConfigError(
                "anomaly_threshold_pp must be an integer",
                field="anomaly_threshold_pp",
            )
        if threshold < 1 or threshold > 100:
            raise _CacheReportConfigError(
                "anomaly_threshold_pp must be in [1, 100]",
                field="anomaly_threshold_pp",
            )
        validated["anomaly_threshold_pp"] = threshold
    return validated


# === Cache-report envelope dataclasses (spec 2026-05-21) =================
# Snake_case fields are emitted verbatim into the SSE envelope so the React
# store can read ``state.cache_report.<field>`` without a key-transform pass
# (the envelope is intentionally snake_case end-to-end; see
# ``dashboard/web/src/types/envelope.ts:189``). Built by
# ``build_cache_report_snapshot()`` and shipped on the existing 5-minute
# sync cadence — no separate ``/api/cache-report`` endpoint.

# Hardcoded for v1; F10 tracks lifting via cache_report.anomaly_window_days config.
CACHE_REPORT_WINDOW_DAYS = 14
# Two concepts that happen to share a value today: the data window the
# panel renders vs. the baseline window the anomaly classifier reads
# back over. Split so F10 can lift the latter without dragging the
# former along.
CACHE_REPORT_ANOMALY_WINDOW_DAYS = 14

@dataclass(frozen=True)
class CacheReportDailyRow:
    date: str  # YYYY-MM-DD in display tz
    cache_hit_percent: float
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    saved_usd: float
    wasted_usd: float
    net_usd: float
    anomaly_triggered: bool
    anomaly_reasons: tuple[str, ...]


@dataclass(frozen=True)
class CacheReportBreakdownRow:
    """One row of the by-project / by-model breakdown sub-cards."""
    key: str
    cache_hit_percent: float
    net_usd: float


@dataclass(frozen=True)
class CacheReportTodaySpotlight:
    """Today's spotlight card: hit %, baseline-median, Δ vs baseline,
    cumulative net / saved / wasted, anomaly state, and the count of
    baseline daily rows so the React panel can gate the
    "Building baseline · N/5 days" insufficient-baseline state."""
    date: str
    cache_hit_percent: float
    baseline_median_percent: float | None
    delta_pp: float | None
    net_usd: float
    saved_usd: float
    wasted_usd: float
    anomaly_triggered: bool
    anomaly_reasons: tuple[str, ...]
    baseline_daily_row_count: int


def _cache_report_snapshot_to_dict(cr: "CacheReportSnapshot | None") -> "dict | None":
    """Serialize a ``CacheReportSnapshot`` to the SSE envelope dict.

    Returns ``None`` when the snapshot is ``None`` (first tick before
    sync, or sub-build failure recorded on ``last_sync_error``). Snake-
    case keys throughout — the envelope is intentionally snake_case end
    -to-end per ``envelope.ts:189`` (no ``to_camel`` pass). Tuples are
    flattened to lists for JSON palatability.
    """
    if cr is None:
        return None
    return {
        "window_days": cr.window_days,
        "anomaly_threshold_pp": cr.anomaly_threshold_pp,
        "anomaly_window_days": cr.anomaly_window_days,
        "today": {
            "date": cr.today.date,
            "cache_hit_percent": cr.today.cache_hit_percent,
            "baseline_median_percent": cr.today.baseline_median_percent,
            "delta_pp": cr.today.delta_pp,
            "net_usd": cr.today.net_usd,
            "saved_usd": cr.today.saved_usd,
            "wasted_usd": cr.today.wasted_usd,
            "anomaly_triggered": cr.today.anomaly_triggered,
            "anomaly_reasons": list(cr.today.anomaly_reasons),
            "baseline_daily_row_count": cr.today.baseline_daily_row_count,
        },
        "days": [
            {
                "date": d.date,
                "cache_hit_percent": d.cache_hit_percent,
                "input_tokens": d.input_tokens,
                "output_tokens": d.output_tokens,
                "cache_creation_tokens": d.cache_creation_tokens,
                "cache_read_tokens": d.cache_read_tokens,
                "saved_usd": d.saved_usd,
                "wasted_usd": d.wasted_usd,
                "net_usd": d.net_usd,
                "anomaly_triggered": d.anomaly_triggered,
                "anomaly_reasons": list(d.anomaly_reasons),
            }
            for d in cr.days
        ],
        "by_project": [
            {
                "key": b.key,
                "cache_hit_percent": b.cache_hit_percent,
                "net_usd": b.net_usd,
            }
            for b in cr.by_project
        ],
        "by_model": [
            {
                "key": b.key,
                "cache_hit_percent": b.cache_hit_percent,
                "net_usd": b.net_usd,
            }
            for b in cr.by_model
        ],
        "seven_day_net_usd": cr.seven_day_net_usd,
        "seven_day_anomaly_count": cr.seven_day_anomaly_count,
        "fourteen_day_counterfactual_usd": cr.fourteen_day_counterfactual_usd,
        "fourteen_day_efficiency_ratio": cr.fourteen_day_efficiency_ratio,
        "is_empty": cr.is_empty,
    }


@dataclass(frozen=True)
class CacheReportSnapshot:
    """The complete cache-report envelope block.

    ``days`` is newest-first, length ``≤ window_days``. ``by_project`` /
    ``by_model`` are sorted by ``abs(net_usd)`` descending and capped at
    6 entries (top 5 + ``(other)``). ``window_days`` is hardcoded at 14
    in v1; ``anomaly_threshold_pp`` is read from
    ``config.json:cache_report.anomaly_threshold_pp`` (default 15) via
    the dashboard sync thread.
    """
    window_days: int
    anomaly_threshold_pp: int
    anomaly_window_days: int
    today: CacheReportTodaySpotlight
    days: tuple[CacheReportDailyRow, ...]
    by_project: tuple[CacheReportBreakdownRow, ...]
    by_model: tuple[CacheReportBreakdownRow, ...]
    seven_day_net_usd: float
    seven_day_anomaly_count: int
    fourteen_day_counterfactual_usd: float
    fourteen_day_efficiency_ratio: float
    is_empty: bool


# === Cache-report snapshot builder (spec 2026-05-21 §5.2) ================
# Adapter from the I/O layer (``get_claude_session_entries`` +
# ``CLAUDE_MODEL_PRICING`` + ``_calculate_entry_cost``) into the kernel's
# pure ``_build_cache_report`` orchestrator. By-project + by-model
# breakdowns dedup through the kernel's ``_aggregate_cache_breakdown``
# (one path, one ``<synthetic>`` filter rule) so the two axes can't
# silently disagree on token totals when a session has both real and
# synthetic entries on the same project.

def _cache_report_load_kernel():
    """Lazy-load ``_lib_cache_report`` via the cctally ``_load_sibling``
    bridge so monkeypatch-driven test reloads of cctally see the same
    kernel module instance (matches the late-load pattern used by share /
    doctor helpers in this file)."""
    return sys.modules["cctally"]._load_sibling("_lib_cache_report")


def _cache_report_needed_closed_dates(since, now_utc, bucket_tz):
    """The CLOSED display-tz dates a full-window cache-report build needs (#272 §6).

    Every display-tz calendar date overlapping ``[since, now_utc]`` EXCEPT the
    current open day (Codex-2: a ``since``-straddling window yields up to
    ``window_days + 1`` display dates, and classification runs over the full
    set BEFORE the ``days`` cap). Returned sorted ascending. The ``have_all``
    gate requires every one of these to be cached before serving the warm
    (today-only-fetch) path. A genuinely activity-free calendar day produces
    no per-day fold row, so the cold/miss populate branch stores an
    ``is_empty`` sentinel (``empty_cached_day``) for every needed closed date
    the fold didn't emit — registering the quiet day as a cache HIT (the same
    computed-empty registration ``projects_env_week_put`` does for an empty
    week). That keeps ``have_all`` reachable, so a weekend/vacation gap day
    does NOT permanently defeat the warm path; the sentinel is skipped in the
    restitch, so the output stays byte-identical to from-scratch.
    """
    today_key = now_utc.astimezone(bucket_tz).strftime("%Y-%m-%d")
    d = since.astimezone(bucket_tz).date()
    end_date = now_utc.astimezone(bucket_tz).date()
    out: list = []
    while d <= end_date:
        key = d.strftime("%Y-%m-%d")
        if key != today_key:
            out.append(key)
        d += dt.timedelta(days=1)
    return out


def _day_start(date_iso, bucket_tz):
    """The aware-UTC instant of ``date_iso``'s local midnight in ``bucket_tz`` (#272 §6).

    Lower-bounds the warm-path current-day fetch so it returns exactly the
    entries ``_aggregate_cache_by_day`` buckets to ``date_iso`` (which buckets
    via ``entry.timestamp.astimezone(bucket_tz)``). DST-correct: attaching the
    zone to the naive local-midnight then converting to UTC lets ``ZoneInfo``
    pick the right offset for that wall time.
    """
    naive = dt.datetime.strptime(date_iso, "%Y-%m-%d")
    return naive.replace(tzinfo=bucket_tz).astimezone(dt.timezone.utc)


def _cache_report_empty(
    *, today_iso, window_days, anomaly_threshold_pp, anomaly_window_days,
):
    """The empty (no in-window entries) ``CacheReportSnapshot`` — factored so the
    warm and cold builder paths share one ``is_empty`` return (#272 §6)."""
    empty_today = CacheReportTodaySpotlight(
        date=today_iso,
        cache_hit_percent=0.0,
        baseline_median_percent=None,
        delta_pp=None,
        net_usd=0.0, saved_usd=0.0, wasted_usd=0.0,
        anomaly_triggered=False,
        anomaly_reasons=(),
        baseline_daily_row_count=0,
    )
    return CacheReportSnapshot(
        window_days=window_days,
        anomaly_threshold_pp=anomaly_threshold_pp,
        anomaly_window_days=anomaly_window_days,
        today=empty_today,
        days=(), by_project=(), by_model=(),
        seven_day_net_usd=0.0,
        seven_day_anomaly_count=0,
        fourteen_day_counterfactual_usd=0.0,
        fourteen_day_efficiency_ratio=0.0,
        is_empty=True,
    )


def build_cache_report_snapshot(
    *,
    now_utc: dt.datetime,
    anomaly_threshold_pp: int,
    anomaly_window_days: int,
    display_tz: "ZoneInfo | None",
    skip_sync: bool = False,
    use_cache_report_cache: bool = False,
) -> CacheReportSnapshot:
    """Build the ``cache_report`` envelope field from the session-entry cache.

    Pulls entries via ``get_claude_session_entries`` (uses the cache when
    warm, falls back to direct-JSONL parse on cache miss / lock
    contention — same chain the CLI uses). Aggregates per closed day via
    ``_lib_cache_report.build_cached_days`` (served from the per-day cache
    when ``use_cache_report_cache`` and reconciled), reconstructs fresh
    rows (``reconstruct_cache_row``), runs the cross-row
    ``classify_and_summarize`` + ``combine_day_project_partials`` restitch,
    and shapes the result into a frozen ``CacheReportSnapshot``.

    ``window_days`` is hardcoded at 14 in v1 (spec §6.1 hardcodes
    ``anomaly_window_days`` too; ``anomaly_threshold_pp`` is the only
    user-configurable knob). F10 from spec §10 tracks making the window
    configurable, plus the UI-copy work it'd require.

    **Per-day cache (#272 §6).** With ``use_cache_report_cache=True`` (set by
    the dashboard sync thread AFTER ``reconcile_cache_report_cache`` succeeds),
    the CLOSED days are served from the immutable per-day cache and only the
    current (open) day is fetched + folded fresh — the warm-tick win. The
    reconcile (in ``_tui_build_snapshot``) owns invalidation; this builder only
    reads/populates the cache. With the flag OFF (default / safe fallback), the
    full window is fetched every tick and the output is byte-identical to the
    cached path (same restructured assembly, cache bypassed).
    """
    crk = _cache_report_load_kernel()
    cctally_ns = sys.modules["cctally"]
    _sc = sys.modules["_lib_snapshot_cache"]

    window_days = CACHE_REPORT_WINDOW_DAYS  # v1: hardcoded per spec §6.1.
    since = now_utc - dt.timedelta(days=window_days)
    pricing = cctally_ns.CLAUDE_MODEL_PRICING

    # Cache mechanics key on the BUCKETING tz (``_resolve_bucket_tz`` — the same
    # tz ``_aggregate_cache_by_day`` buckets by), so ``today_key`` / the closed-
    # day keys line up with ``CacheRow.date``. The spotlight's ``today_iso``
    # keeps its own UTC-fallback derivation unchanged (byte-identity); the two
    # agree whenever ``display_tz`` is set — always, on the dashboard.
    bucket_tz = crk._resolve_bucket_tz(display_tz)
    today_key = now_utc.astimezone(bucket_tz).strftime("%Y-%m-%d")
    today_iso = now_utc.astimezone(
        display_tz if display_tz is not None else dt.timezone.utc
    ).strftime("%Y-%m-%d")

    def _wrap_day_entries(raw):
        # Day-mode kernel expects entries with a ``usage`` dict (matches
        # ``UsageEntry``); ``get_claude_session_entries`` returns flat
        # ``_JoinedClaudeEntry`` objects. SimpleNamespace keeps the wrapper
        # pure-Python and avoids a new dataclass type just for the bridge.
        from types import SimpleNamespace as _NS
        return [
            _NS(
                timestamp=e.timestamp,
                model=e.model,
                cost_usd=e.cost_usd,
                usage={
                    "input_tokens": e.input_tokens,
                    "output_tokens": e.output_tokens,
                    "cache_creation_input_tokens": e.cache_creation_tokens,
                    "cache_read_input_tokens": e.cache_read_tokens,
                },
            )
            for e in raw
        ]

    # Which CLOSED display-tz dates the window needs (every date overlapping
    # [since, now] except the current open day). If the cache holds them ALL,
    # fetch only the current day (the warm win); else one full-window fetch
    # (re)populates the closed days + recomputes today.
    needed_closed = _cache_report_needed_closed_dates(since, now_utc, bucket_tz)
    have_all = use_cache_report_cache and all(
        _sc.cache_report_day_get(d) is not None for d in needed_closed
    )

    cached_days: dict = {}  # date_key -> CachedCacheReportDay
    if have_all:
        # Steady-state warm: recompute ONLY the open bucket; reuse cached closed.
        # late-binding: tests patch this on the dashboard module object (rebuild_parity :3803,:4031)
        raw_today = list(sys.modules["_cctally_dashboard"].get_claude_session_entries(
            _day_start(today_key, bucket_tz), now_utc,
            project=None, skip_sync=skip_sync,
        ))
        built = crk.build_cached_days(
            _wrap_day_entries(raw_today), raw_today,
            display_tz=display_tz, pricing=pricing,
            cost_calculator=_calculate_entry_cost,
        )
        for d in needed_closed:
            cached_days[d] = _sc.cache_report_day_get(d)
        cached_days.update(built)  # the fresh current day (today only)
    else:
        # Cold / partial-miss / post-eviction: pay one full-window fetch, then
        # (re)compute + store every closed day. Runs on the rare stale tick.
        # late-binding: tests patch this on the dashboard module object (rebuild_parity :3803,:4031)
        raw = list(sys.modules["_cctally_dashboard"].get_claude_session_entries(
            since, now_utc, project=None, skip_sync=skip_sync,
        ))
        if not raw:
            return _cache_report_empty(
                today_iso=today_iso, window_days=window_days,
                anomaly_threshold_pp=anomaly_threshold_pp,
                anomaly_window_days=anomaly_window_days,
            )
        built = crk.build_cached_days(
            _wrap_day_entries(raw), raw,
            display_tz=display_tz, pricing=pricing,
            cost_calculator=_calculate_entry_cost,
        )
        cached_days = dict(built)
        if use_cache_report_cache:
            for d, unit in built.items():
                if d != today_key:  # never store the OPEN day as a closed unit
                    _sc.cache_report_day_store(d, unit)
            # Empty/gap-day sentinel: a genuinely activity-free CLOSED day never
            # appears in ``built`` (``build_cached_days`` only emits days with
            # entries), so register an ``is_empty`` sentinel for every needed
            # closed date the fold produced nothing for. Without this,
            # ``have_all`` would see that quiet day as a perpetual miss and the
            # builder would full-window-refetch on EVERY warm tick — permanently
            # defeating the cache for any user with an off day. Mirrors
            # ``projects_env_week_put`` registering a computed-empty week as a
            # hit. ``needed_closed`` already excludes ``today_key``; the sentinel
            # is skipped in the restitch below, so this is byte-identical.
            for d in needed_closed:
                if d not in built:
                    _sc.cache_report_day_store(d, crk.empty_cached_day(d))
            # Window-rolloff eviction (#275): the reconcile's seq-gated pass only
            # evicts CHANGED days (>= the change watermark); days that roll off the
            # trailing edge of the [since, now] window are never pruned there and
            # would accrete one frozen unit/day on a long-uptime dashboard. Drop
            # them here on this rare cold/rollover store tick (the warm path never
            # reaches this branch). ``needed_closed`` is sorted ascending, so its
            # first element is the window's oldest still-needed closed day; every
            # needed day (real or ``is_empty`` sentinel) is >= it and survives.
            if needed_closed:
                _sc.cache_report_day_evict_before(needed_closed[0])

    if not cached_days:
        return _cache_report_empty(
            today_iso=today_iso, window_days=window_days,
            anomaly_threshold_pp=anomaly_threshold_pp,
            anomaly_window_days=anomaly_window_days,
        )

    # Reconstruct fresh MUTABLE rows from the frozen cached days (F7 — cached
    # units are never touched), then run the cross-row classify + summarize
    # pass over the FULL assembled set every tick (the cached day aggregates
    # carry no anomaly state).
    rows = [
        crk.reconstruct_cache_row(cached_days[d])
        for d in sorted(cached_days)
        if not cached_days[d].is_empty  # sentinel gap days emit no CacheRow
    ]
    result = crk.classify_and_summarize(
        rows,
        now_utc=now_utc,
        window_days=window_days,
        anomaly_threshold_pp=anomaly_threshold_pp,
        anomaly_window_days=anomaly_window_days,
        display_tz=display_tz,
        mode="day",
    )

    # Pick out today's row (if any) and the baseline-daily-row count for
    # the spotlight. The spotlight median is computed against ALL rows
    # except today (cross-row reference; mirrors what the panel's
    # "Δ vs 14d median" label means). The median itself rides back on
    # ``result.today_baseline_median`` (EFF-3 — kernel computes it once
    # alongside the anomaly classifier so we don't re-walk the same
    # row set here).
    today_row = next((r for r in result.rows if r.date == today_iso), None)
    other_rows = [r for r in result.rows if r.date != today_iso]
    baseline_median = result.today_baseline_median

    baseline_daily_row_count = len(other_rows)

    # ``delta_pp`` sign convention (spec §4.2): "signed; negative = today
    # below median" → ``delta = today − baseline``. The empty-day branch
    # uses today_hit_pct = 0.0 so the formula degenerates to
    # ``0.0 − baseline_median``, which IS what users expect (a flat-zero
    # today read against a healthy 60% baseline yields delta=-60pp).
    today_hit_pct = today_row.cache_hit_percent if today_row is not None else 0.0
    delta_pp = (
        None if baseline_median is None
        else today_hit_pct - baseline_median
    )

    if today_row is None:
        today_spotlight = CacheReportTodaySpotlight(
            date=today_iso,
            cache_hit_percent=0.0,
            baseline_median_percent=baseline_median,
            delta_pp=delta_pp,
            net_usd=0.0, saved_usd=0.0, wasted_usd=0.0,
            anomaly_triggered=False,
            anomaly_reasons=(),
            baseline_daily_row_count=baseline_daily_row_count,
        )
    else:
        today_spotlight = CacheReportTodaySpotlight(
            date=today_iso,
            cache_hit_percent=today_row.cache_hit_percent,
            baseline_median_percent=baseline_median,
            delta_pp=delta_pp,
            net_usd=today_row.net_usd,
            saved_usd=today_row.saved_usd,
            wasted_usd=today_row.wasted_usd,
            anomaly_triggered=today_row.anomaly_triggered,
            anomaly_reasons=tuple(today_row.anomaly_reasons),
            baseline_daily_row_count=baseline_daily_row_count,
        )

    # Daily rows — newest first, capped at ``window_days``.
    #
    # Slice cap (spec §4.2 — "length up to ``window_days``"): the kernel's
    # ``since = now_utc - timedelta(days=window_days)`` rolling window
    # straddles midnight in any non-UTC ``display_tz`` (and in fact even
    # in UTC, since ``now_utc - 14d`` and ``now_utc`` flank the same
    # wall-clock minute on different calendar dates), so the kernel can
    # emit ``window_days + 1`` distinct calendar-date buckets. Capping
    # here (and not in the kernel) keeps the kernel agnostic of the
    # envelope's hard ceiling while honoring the contract every TS /
    # React consumer relies on (the sparkline ladder is hard-sized to
    # ``window_days`` points). Regression:
    # ``test_build_cache_report_snapshot_days_bounded_by_window``.
    #
    # Synthetic-today insertion: if the trailing window has older activity
    # but no entries for the current display-tz day, the kernel emits a
    # rows[] list whose newest row is yesterday (or older). Both React
    # consumers (``CacheSparkline`` and ``CacheNetBars``) treat the
    # rightmost element of ``days`` as "Today" purely positionally
    # (``ordered.length - 1`` / ``isLast ? 'Today'``), so without an
    # explicit today bucket they would mis-label the older row as Today.
    # Insert a zero-valued CacheReportDailyRow at position 0 (newest)
    # whenever ``today_row is None``. The zero values mirror the
    # ``today_spotlight`` synthesized above (kept in lock-step), and
    # contribute 0 to ``seven_day_*`` / ``fourteen_day_*`` rollups so
    # the rollup math stays untouched.
    raw_days_newest_first = sorted(
        result.rows, key=lambda r: r.date or "", reverse=True,
    )
    days_newest_first: list = []
    if today_row is None:
        # Build a zero-valued synthetic today row mirroring today_spotlight.
        days_newest_first.append(
            CacheReportDailyRow(
                date=today_iso,
                cache_hit_percent=0.0,
                input_tokens=0,
                output_tokens=0,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                saved_usd=0.0,
                wasted_usd=0.0,
                net_usd=0.0,
                anomaly_triggered=False,
                anomaly_reasons=(),
            )
        )
    days_newest_first.extend(
        CacheReportDailyRow(
            date=r.date or "",
            cache_hit_percent=r.cache_hit_percent,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cache_creation_tokens=r.cache_creation_tokens,
            cache_read_tokens=r.cache_read_tokens,
            saved_usd=r.saved_usd,
            wasted_usd=r.wasted_usd,
            net_usd=r.net_usd,
            anomaly_triggered=r.anomaly_triggered,
            anomaly_reasons=tuple(r.anomaly_reasons),
        )
        for r in raw_days_newest_first
    )
    days = tuple(days_newest_first[:window_days])

    # By-project + by-model breakdowns are window-wide aggregates (not
    # today-only) so the panel can surface the project / model carrying
    # the bulk of net savings across the trailing 14d. by-project walks
    # raw entries (project_path is per-entry, not on the day-model
    # buckets); by-model folds the per-row ``model_breakdowns`` already
    # produced by day-mode, which avoids re-running the tiered-pricing
    # math per entry. Both paths apply the same ``<synthetic>`` filter so
    # the axes can't silently disagree on token totals.
    #
    # Constrain both axes to the SAME calendar dates as ``days``: the
    # kernel's rolling window can emit ``window_days + 1`` distinct
    # display-tz buckets (see the slice-cap comment above), and ``days``
    # drops the oldest. Without the same drop here the by-project /
    # by-model cards would silently include the clipped 15th day and
    # their net totals stop reconciling against the visible table /
    # CacheNetBars in the modal. The filter mirrors the kernel's
    # bucket-key derivation (``entry.timestamp.astimezone(tz)``) so a
    # cache entry and its corresponding day row always agree on which
    # bucket they belong to.
    kept_dates = frozenset(r.date for r in days if r.date)
    rows_in_window = [r for r in result.rows if r.date in kept_dates]
    # by_project via the #272 §4 two-level ``stable_sum`` fold
    # (grouping-invariant). The per-(day, project) partials are carried on each
    # ``CachedCacheReportDay`` (§5) — served from cache for closed days and
    # freshly folded for today — so restitching them here memoizes the fold
    # byte-identically. Their day keys use ``_resolve_bucket_tz(display_tz)``
    # (matching ``CacheRow.date`` / ``kept_dates``), so combining over
    # ``kept_dates`` is exactly the old full-window slice, modulo the one-time
    # ULP fold move. Every ``kept_date`` is in ``cached_days`` (it came from a
    # day row); the ``if d in cached_days`` guard is defensive.
    by_project_rows = crk.combine_day_project_partials(
        {
            d: dict(cached_days[d].project_partials)
            for d in kept_dates
            if d in cached_days and not cached_days[d].is_empty
        }
    )
    by_model_rows = crk._aggregate_cache_breakdown_from_rows(
        rows_in_window,
        skip_synthetic=True,
    )
    by_project = tuple(
        CacheReportBreakdownRow(
            key=r.key, cache_hit_percent=r.cache_hit_percent, net_usd=r.net_usd,
        )
        for r in by_project_rows
    )
    by_model = tuple(
        CacheReportBreakdownRow(
            key=r.key, cache_hit_percent=r.cache_hit_percent, net_usd=r.net_usd,
        )
        for r in by_model_rows
    )

    # 7-day rollup: today + 6 prior. Walk by string date; ``days_newest_first``
    # is already in the right order.
    seven_day_rows = days[:7]
    seven_day_net_usd = stable_sum(r.net_usd for r in seven_day_rows)
    seven_day_anomaly_count = sum(
        1 for r in seven_day_rows if r.anomaly_triggered
    )

    # 14-day counterfactual: sum(saved_usd) across the window.
    fourteen_day_counterfactual_usd = stable_sum(r.saved_usd for r in days)
    fourteen_day_wasted_usd = stable_sum(r.wasted_usd for r in days)
    denom = fourteen_day_counterfactual_usd + abs(fourteen_day_wasted_usd)
    fourteen_day_efficiency_ratio = (
        (fourteen_day_counterfactual_usd / denom) if denom > 1e-9 else 0.0
    )

    return CacheReportSnapshot(
        window_days=window_days,
        anomaly_threshold_pp=anomaly_threshold_pp,
        anomaly_window_days=anomaly_window_days,
        today=today_spotlight,
        days=days,
        by_project=by_project,
        by_model=by_model,
        seven_day_net_usd=seven_day_net_usd,
        seven_day_anomaly_count=seven_day_anomaly_count,
        fourteen_day_counterfactual_usd=fourteen_day_counterfactual_usd,
        fourteen_day_efficiency_ratio=fourteen_day_efficiency_ratio,
        is_empty=False,
    )
