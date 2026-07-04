"""View-model kernel for CLI / dashboard / share consumers.

This module owns the per-domain row dataclasses and the ``*View``
wrappers that carry rows + data-plane aggregates (totals, averages).

Bundle 1 (landed):

Row dataclasses moved verbatim from ``bin/_cctally_tui.py`` — no field
changes:

- ``DailyPanelRow``, ``MonthlyPeriodRow``, ``WeeklyPeriodRow``,
  ``TuiSessionRow``.

``TuiTrendRow`` moved with the 10 nullable fields added per spec §4.1
(``week_start_date``, ``week_end_date``, ``week_end_at``,
``weekly_cost_usd``, ``usage_captured_at``, ``cost_captured_at``,
``as_of``, ``range_start_iso``, ``range_end_iso``, ``freshness``). All
defaults are ``None`` so existing TUI / dashboard fixtures that
construct ``TuiTrendRow`` positionally stay byte-stable.

Frozen ``*View`` dataclasses + builders:

- ``DailyView`` + ``build_daily_view(entries, *, now_utc, display_tz)``
- ``MonthlyView`` + ``build_monthly_view(entries, *, now_utc, n,
  display_tz)``
- ``WeeklyView`` + ``build_weekly_view(conn, entries, *, weeks, now_utc,
  display_tz, as_of_utc)``
- ``TrendView`` + ``build_trend_view(conn, *, now_utc, n, display_tz)``
- ``SessionsView`` + ``build_sessions_view(entries, *, now_utc, limit,
  display_tz)``
- ``BlocksView`` + ``build_blocks_view(entries, *, now_utc,
  recorded_windows, block_start_overrides, range_start, range_end,
  display_tz, mode)`` — heuristic-aware (cmd_blocks + dashboard); and
  ``build_blocks_view_from_table_rows(block_dicts, *, period_start,
  period_end, display_tz)`` — API-anchored (cmd_five_hour_blocks
  share). Issue #56.
- ``ForecastView`` + ``build_forecast_view(conn, *, now_utc, targets,
  skip_sync, display_tz)`` — wraps the existing math kernel
  (``_load_forecast_inputs`` + ``_compute_forecast``) and surfaces the
  per-method projection / verdict / header-routing / budget fields
  consumers used to re-derive. Issue #57.
- ``CodexDailyView`` / ``CodexMonthlyView`` / ``CodexWeeklyView`` /
  ``CodexSessionView`` + ``build_codex_{daily,monthly,weekly,session}_view``
  — wrap the existing ``_aggregate_codex_*`` kernel; preserve the
  intentional divergences from upstream (LiteLLM token semantics,
  duplicate-event dedup, ``codex-session`` descending-by-last-activity,
  ``CODEX_LEGACY_FALLBACK_MODEL`` warning). Issue #58.

Each ``*View`` carries ``rows`` (typed row tuple) plus a parallel
``aggregated`` ``BucketUsage`` tuple where CLI byte-stable JSON requires
it (the weekly view also carries an ``overlay`` tuple of ``(used_pct,
dpp)`` pairs aligned with ``aggregated``). All builders return totals
(``total_cost_usd`` / ``total_tokens``) so the dashboard envelope
adapter (in ``bin/_cctally_dashboard.py``) and the React panel layer
share a single source of truth.

Helpers:

- ``_load_lib(name)`` — late-import a sibling under ``bin/`` (matches
  ``_lib_aggregators._load_lib``; keeps the import-time graph acyclic).
- ``_cctally()`` — accessor for the ``cctally`` module at call-time
  (spec §5.5; lets builders touch top-level helpers without binding at
  module load).
- ``_display_tz_label`` / ``_model_breakdowns_to_models_late`` —
  presentation-side helpers shared across builders.

``bin/_cctally_tui.py`` re-exports each name so historical imports
(``from _cctally_tui import DailyPanelRow``, ``ns["DailyPanelRow"]``
direct-dict reads in tests) keep resolving.

Spec: docs/superpowers/specs/2026-05-17-view-model-unification-design.md
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
from dataclasses import dataclass
from typing import Any


def _cctally():
    """Resolve the current ``cctally`` module at call-time (spec §5.5)."""
    return sys.modules["cctally"]


def _load_lib(name: str):
    """Late-import a sibling under ``bin/`` (same recipe as
    ``_lib_aggregators._load_lib`` — keeps the import-time graph acyclic
    even when builders need access to ``_cctally_dashboard`` /
    ``_lib_aggregators`` / ``_lib_share``).
    """
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    import importlib.util as _ilu
    p = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    spec = _ilu.spec_from_file_location(name, p)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ``_lib_fmt``'s only intra-repo deps are the leaf kernels ``_cctally_core`` +
# ``_lib_display_tz`` (neither imports back), so loading it here is acyclic.
# ``stable_sum`` is the interpreter-stable
# float-summation chokepoint (math.fsum) used for output-bound totals.
stable_sum = _load_lib("_lib_fmt").stable_sum


# === Row dataclasses (Task 2: moved verbatim from _cctally_tui.py) =========
# Field order, types, and defaults match the originals byte-stable.


@dataclass
class TuiTrendRow:
    """Trend row used by CLI ``report``, TUI trend panel, dashboard
    trend panel, and share ``_build_report_snapshot``.

    The first 7 fields (``week_label`` through ``is_current``) are the
    historical TUI/dashboard surface — preserved verbatim. The 10
    nullable fields below were added by spec §4.1 so ``cmd_report`` can
    consume a single typed shape (eliminates the camelCase dict
    workaround documented at ``_build_report_snapshot:~12299``).

    JSON serialization sites (``cmd_report --json``, dashboard envelope
    ``trend.weeks[]``) map field-by-field to today's keys; this typed
    in-memory shape is internal.
    """
    # ---- existing TUI/dashboard fields (verbatim) ----
    week_label: str              # e.g. "Apr 14"
    week_start_at: dt.datetime
    used_pct: float | None       # None when the week has a cost snapshot
                                 # but no usage snapshot (phantom weeks)
    dollars_per_percent: float | None
    delta_dpp: float | None      # vs prior week
    spark_height: int            # 1..8 normalized
    is_current: bool

    # ---- NEW (spec §4.1): required by cmd_report JSON contract; nullable ----
    week_start_date: dt.date | None = None
    week_end_date: dt.date | None = None
    week_end_at: dt.datetime | None = None
    weekly_cost_usd: float | None = None
    usage_captured_at: str | None = None      # ISO-8601 or None
    cost_captured_at: str | None = None       # ISO-8601 or None
    as_of: str | None = None                  # ISO-8601 or None
    range_start_iso: str | None = None
    range_end_iso: str | None = None
    freshness: dict | None = None             # {label, captured_at, age_seconds}


@dataclass
class WeeklyPeriodRow:
    """One subscription-week row for the dashboard's Weekly panel/modal.

    `models` is a list of `{model, display, chip, cost_usd, cost_pct}`
    dicts sorted by `cost_usd` descending. Pre-bucketed in Python so
    the React layer never re-derives per-model coloring.
    """
    label: str                          # "04-23" — MM-DD of the week start
    cost_usd: float
    total_tokens: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    used_pct: float | None              # from weekly_usage_snapshots overlay
    dollar_per_pct: float | None        # cost / used_pct when used_pct > 0
    delta_cost_pct: float | None        # (cost - prev_cost) / prev_cost
    is_current: bool
    models: list[dict[str, Any]]
    week_start_at: str                  # ISO-8601 with tz, from SubWeek.start_ts
    week_end_at: str                    # ISO-8601 with tz, from SubWeek.end_ts


@dataclass
class MonthlyPeriodRow:
    """One calendar-month row for the dashboard's Monthly panel/modal."""
    label: str                          # "YYYY-MM"
    cost_usd: float
    total_tokens: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    delta_cost_pct: float | None
    is_current: bool
    models: list[dict[str, Any]]


@dataclass
class BlocksPanelRow:
    """One row of the dashboard's Blocks panel.

    Subset of the ``Block`` dataclass — drops token counts (panel is
    cost-driven; tokens belong to a future modal), drops ``entries_count``
    / ``is_gap`` / ``burn_rate`` / ``projection`` (panel doesn't render
    them), and pre-formats ``label`` server-side for the local-tz
    "HH:MM MMM DD" display.

    Moved from ``bin/_cctally_tui.py`` alongside ``DailyPanelRow`` so
    the BlocksView builder can construct rows without an import edge
    back into the TUI module.
    """
    start_at: str          # ISO-8601 UTC
    end_at: str            # ISO-8601 UTC, start_at + 5h
    anchor: str            # 'recorded' | 'heuristic'
    is_active: bool        # now_utc < end_at AND entries_count > 0
    cost_usd: float
    models: list[dict[str, Any]]   # ModelCostRow shape, sorted desc by cost
    label: str             # "HH:MM MMM DD" in local tz, e.g. "14:00 Apr 26"


@dataclass
class DailyPanelRow:
    """One row of the dashboard's Daily heatmap panel.

    `intensity_bucket` is the server-computed quintile bucket (0..5) —
    bucket 0 is reserved for zero-cost days; buckets 1..5 are quintiles
    over non-zero days.

    v2.3: Added per-day token rollup + `cache_hit_pct` so the Daily
    detail modal can surface the same fields the CLI's `daily` command
    shows. Defaults preserve compatibility with `_empty_dashboard_snapshot`
    and any pre-v2.3 fixture that omits the new fields.
    """
    date: str              # local-tz YYYY-MM-DD
    label: str             # "MM-DD" — pre-formatted, mirrors Weekly/Monthly idiom
    cost_usd: float
    is_today: bool
    intensity_bucket: int  # 0..5
    models: list[dict[str, Any]]   # ModelCostRow shape, sorted desc by cost
    # ---- v2.3 additions: Daily modal token + cache rollup ----
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0
    cache_hit_pct: float | None = None


@dataclass
class TuiSessionRow:
    started_at: dt.datetime
    duration_minutes: float
    model_primary: str           # first model used in the session
    cost_usd: float
    cache_hit_pct: float | None
    project_label: str           # basename of project_path
    session_id: str              # full session UUID (v2: needed for session-detail modal)
    # Disambiguated display key (matches the Projects panel envelope's
    # `current_week.rows[].key` / `trend.projects[].key`). Populated by
    # the sync-thread builder after `_build_projects_envelope` runs so
    # the SessionsPanel → ProjectsModal cross-nav (spec §4.1) routes by
    # a stable identity. Defaults to ``None`` for fixture modules that
    # construct ``TuiSessionRow`` positionally without the Bundle 6 /
    # projects-panel additions; the client renders the cell as plain
    # text in that case per spec §4.1 stopgap.
    project_key: str | None = None
    # Absolute project_path (NULL ⇒ ``(unknown)`` resolved upstream).
    # Carried so the sync-thread builder can compute ``project_key``
    # without re-reading ``session_files`` and so the share path can
    # privacy-scrub via ``_lib_share._scrub``.
    project_path: str | None = None
    # Dashboard/TUI-only: the human-readable session title (AI-generated
    # title when present, else the first non-marker user prompt). Populated
    # by the dashboard/TUI snapshot wrapper (``_cctally_tui._tui_build_sessions``)
    # via ``_lib_conversation_query._session_titles_map``; left ``None`` on the
    # CLI ``session`` / share paths (they call ``build_sessions_view`` directly).
    # This is transcript-derived content — it rides the per-request transcript
    # privacy gate at envelope serialization (never emitted when the gate is
    # closed). Defaults ``None`` so fixture rows built positionally stay
    # compatible.
    title: str | None = None


# === Internal helpers ======================================================


def _display_tz_label(display_tz) -> str:
    """Mirror of cctally._share_display_tz_label.

    Kept here so builders don't depend on the cctally namespace at
    build time. ``ZoneInfo`` -> ``zone.key``; ``None`` -> ``"local"``
    (per resolve_display_tz's convention).
    """
    return display_tz.key if display_tz is not None else "local"


def _model_breakdowns_to_models_late(model_breakdowns, cost_usd):
    """Late-bound shim for ``_model_breakdowns_to_models`` in
    ``_cctally_dashboard``.

    Cannot eagerly import at module load (``_cctally_dashboard`` is a
    heavier sibling and creating an import-time edge would force its
    side-effects on every builder load). Resolved at first call.
    """
    mod = _load_lib("_cctally_dashboard")
    return mod._model_breakdowns_to_models(model_breakdowns, cost_usd)


# === DailyView + build_daily_view (Task 3) =================================


@dataclass(frozen=True)
class DailyView:
    """Daily domain view — entries-driven, newest-first.

    ``rows`` carries one ``DailyPanelRow`` per *non-empty* day (NO
    gap-fill — the dashboard envelope adapter materializes the
    contiguous heatmap window post-builder; CLI / share consume rows
    as-is to preserve byte-stable ``cctally daily --json``).

    ``aggregated`` is the parallel ``BucketUsage`` tuple from
    ``_aggregate_daily`` (same order as ``rows``). CLI's
    ``_bucket_to_json`` and ``_render_bucket_table`` plus the share
    ``_build_daily_snapshot`` consume this shape; the dashboard
    envelope adapter consumes ``rows``.

    Carrying both shapes (BucketUsage + DailyPanelRow) mirrors the
    SessionsView pattern in Bundle 2 (spec §6.5) — CLI/share renderers
    today depend on BucketUsage fields (``bucket``, ``model_breakdowns``,
    ``models: list[str]``) that aren't present on ``DailyPanelRow``;
    forcing the rename onto the renderer would break byte-stable
    ``cctally daily --json``.
    """
    rows: "tuple[DailyPanelRow, ...]" = ()
    aggregated: tuple = ()                    # tuple[BucketUsage, ...] — forward-ref kept untyped to avoid an import-time edge into the aggregator's BucketUsage shape.
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    period_start: "dt.datetime | None" = None
    period_end: "dt.datetime | None" = None
    display_tz_label: str = ""


def build_daily_view(entries, *, now_utc, display_tz=None, mode="auto",
                     aggregated_override=None):
    """Build a ``DailyView`` from raw ``UsageEntry`` list (spec §5.1).

    Gap-free: only days with entries appear in ``view.rows`` /
    ``view.aggregated`` (newest-first). The contiguous-window
    materialization the dashboard heatmap needs is presentation logic
    and stays at the dashboard envelope adapter.

    Per-row derivations: ``cache_hit_pct`` (cache_read / (input +
    cache_creation + cache_read) * 100), ``is_today`` (date == today
    in display_tz), ``models[]`` via
    ``_model_breakdowns_to_models``.

    Leaves ``DailyPanelRow.label`` and ``intensity_bucket`` at dataclass
    defaults — the dashboard envelope adapter populates them. CLI /
    share consumers ignore them and read ``view.aggregated`` instead.

    ``aggregated_override`` (#268): when provided (a list/tuple of
    ``BucketUsage`` in ascending bucket-key order), skip the
    ``_aggregate_daily(entries, ...)`` re-costing and build the view over
    those pre-aggregated buckets instead. This is the reuse seam the
    dashboard's cached daily builder uses to serve immutable past days
    from memory while keeping every downstream derivation (rows,
    ``cache_hit_pct``, totals, ``period_start``) single-sourced here. When
    ``None`` (CLI / share / cold path) behavior is unchanged and
    byte-identical.
    """
    _agg = _load_lib("_lib_aggregators")
    if aggregated_override is not None:
        buckets = list(aggregated_override)
    else:
        buckets = _agg._aggregate_daily(entries, mode=mode, tz=display_tz)
    if not buckets:
        return DailyView(
            rows=(),
            aggregated=(),
            total_cost_usd=0.0,
            total_tokens=0,
            period_start=None,
            period_end=now_utc,
            display_tz_label=_display_tz_label(display_tz),
        )

    today_local = (
        now_utc.astimezone(display_tz) if display_tz is not None
        # internal fallback: host-local intentional
        else now_utc.astimezone()
    ).date()

    rows = []
    # buckets come oldest-first from _aggregate_daily; reverse for newest-first.
    reversed_buckets = list(reversed(buckets))
    total_cost = 0.0
    total_tok = 0
    for b in reversed_buckets:
        denom = b.input_tokens + b.cache_creation_tokens + b.cache_read_tokens
        cache_hit = (b.cache_read_tokens / denom * 100.0) if denom > 0 else None
        d = dt.date.fromisoformat(b.bucket)
        row = DailyPanelRow(
            date=b.bucket,
            label="",                  # adapter fills
            cost_usd=b.cost_usd,
            is_today=(d == today_local),
            intensity_bucket=0,        # adapter fills
            models=_model_breakdowns_to_models_late(
                b.model_breakdowns, b.cost_usd,
            ),
            input_tokens=b.input_tokens,
            output_tokens=b.output_tokens,
            cache_creation_tokens=b.cache_creation_tokens,
            cache_read_tokens=b.cache_read_tokens,
            total_tokens=b.total_tokens,
            cache_hit_pct=cache_hit,
        )
        rows.append(row)
        total_cost += b.cost_usd
        total_tok += b.total_tokens

    earliest = dt.date.fromisoformat(buckets[0].bucket)
    period_start = dt.datetime.combine(
        earliest, dt.time.min, tzinfo=dt.timezone.utc,
    )
    return DailyView(
        rows=tuple(rows),
        aggregated=tuple(reversed_buckets),
        total_cost_usd=total_cost,
        total_tokens=total_tok,
        period_start=period_start,
        period_end=now_utc,
        display_tz_label=_display_tz_label(display_tz),
    )


# === MonthlyView + build_monthly_view (Task 8) =============================


@dataclass(frozen=True)
class MonthlyView:
    """Monthly domain view — entries-driven, newest-first.

    Like ``DailyView``: ``rows`` carries the typed ``MonthlyPeriodRow``
    tuple for dashboard/share, ``aggregated`` carries the parallel
    ``BucketUsage`` tuple for CLI byte-stability.

    Boundary-spillover bucket is dropped (mirrors the existing dashboard
    builder at ``_dashboard_build_monthly_periods:1752``):
    in tzs west of UTC, the bucket builder emits an extra ``YYYY-MM``
    row for entries that straddle the UTC range start into the prior
    local month. Slicing to ``n`` after reversal drops it.

    ``delta_cost_pct`` is computed per row vs the next-older row; the
    oldest row's value is ``None`` (no prior to compare against).
    """
    rows: "tuple[MonthlyPeriodRow, ...]" = ()
    aggregated: tuple = ()                    # tuple[BucketUsage, ...] — forward-ref kept untyped to avoid an import-time edge into the aggregator's BucketUsage shape.
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    period_start: "dt.datetime | None" = None
    period_end: "dt.datetime | None" = None
    display_tz_label: str = ""


def build_monthly_view(entries, *, now_utc, n=12, display_tz=None, mode="auto",
                       aggregated_override=None):
    """Build a ``MonthlyView`` for the trailing ``n`` calendar months
    (spec §5.2).

    Calls ``_aggregate_monthly``. Drops the boundary-spillover bucket
    (mirrors ``_dashboard_build_monthly_periods``). Computes
    ``delta_cost_pct`` per row vs the next-older row. Newest-first.

    Totals (``total_cost_usd`` / ``total_tokens``) sum over the
    truncated row set so the React panel sees the same number as the
    CLI table footer would.

    ``aggregated_override`` (#268): when provided (a list/tuple of
    ``BucketUsage`` in ascending bucket-key order), skip the
    ``_aggregate_monthly(entries, ...)`` re-costing and build the view over
    those pre-aggregated buckets. The reverse + cap-to-``n`` +
    ``delta_cost_pct`` + ``is_current`` presentation still runs over the
    assembled list, so the current month's delta sees the prior (cached)
    month (Codex F3). ``None`` preserves the CLI/share/cold behavior
    byte-identically.
    """
    _agg = _load_lib("_lib_aggregators")
    if aggregated_override is not None:
        buckets = list(aggregated_override)
    else:
        buckets = _agg._aggregate_monthly(entries, mode=mode, tz=display_tz)
    if not buckets:
        return MonthlyView(
            rows=(), aggregated=(),
            total_cost_usd=0.0, total_tokens=0,
            period_start=None, period_end=now_utc,
            display_tz_label=_display_tz_label(display_tz),
        )

    # Reverse for newest-first AND cap to n BEFORE the delta loop —
    # boundary-spillover drop (see MonthlyView docstring).
    buckets = list(reversed(buckets))[:n]
    cur_label = (
        now_utc.astimezone(display_tz) if display_tz is not None
        # internal fallback: host-local intentional
        else now_utc.astimezone()
    ).strftime("%Y-%m")

    rows = []
    total_cost = 0.0
    total_tok = 0
    for i, b in enumerate(buckets):
        prev = buckets[i + 1] if i + 1 < len(buckets) else None
        delta = None
        if prev is not None and prev.cost_usd > 0:
            delta = (b.cost_usd - prev.cost_usd) / prev.cost_usd
        rows.append(MonthlyPeriodRow(
            label=b.bucket,                          # "YYYY-MM"
            cost_usd=b.cost_usd,
            total_tokens=b.total_tokens,
            input_tokens=b.input_tokens,
            output_tokens=b.output_tokens,
            cache_creation_tokens=b.cache_creation_tokens,
            cache_read_tokens=b.cache_read_tokens,
            delta_cost_pct=delta,
            is_current=(b.bucket == cur_label),
            models=_model_breakdowns_to_models_late(
                b.model_breakdowns, b.cost_usd,
            ),
        ))
        total_cost += b.cost_usd
        total_tok += b.total_tokens

    # period_start = first day of the oldest visible month, UTC.
    earliest_label = buckets[-1].bucket
    yr, mo = earliest_label.split("-")
    period_start = dt.datetime(
        int(yr), int(mo), 1, tzinfo=dt.timezone.utc,
    )
    return MonthlyView(
        rows=tuple(rows),
        aggregated=tuple(buckets),
        total_cost_usd=total_cost,
        total_tokens=total_tok,
        period_start=period_start,
        period_end=now_utc,
        display_tz_label=_display_tz_label(display_tz),
    )


# === WeeklyView + build_weekly_view (Task 9) ===============================


@dataclass(frozen=True)
class WeeklyView:
    """Weekly domain view — subscription-week aligned, newest-first.

    ``rows`` carries the typed ``WeeklyPeriodRow`` tuple (already
    overlaid with ``weekly_usage_snapshots``); ``aggregated`` carries
    the parallel ``BucketUsage`` tuple for CLI byte-stability;
    ``overlay`` carries ``(used_pct, dollar_per_pct)`` tuples in the
    same order as ``aggregated`` (drives the CLI's
    ``_render_weekly_table`` / ``_weekly_to_json``).

    ``rows`` and ``aggregated`` are both newest-first. ``overlay``
    aligns with ``aggregated``.
    """
    rows: "tuple[WeeklyPeriodRow, ...]" = ()
    aggregated: tuple = ()                    # tuple[BucketUsage, ...] — forward-ref kept untyped to avoid an import-time edge into the aggregator's BucketUsage shape.
    overlay: "tuple[tuple[float | None, float | None], ...]" = ()
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    period_start: "dt.datetime | None" = None
    period_end: "dt.datetime | None" = None
    display_tz_label: str = ""


def build_weekly_view(conn, entries, *, weeks, now_utc, display_tz=None,
                      as_of_utc=None, mode="auto"):
    """Build a ``WeeklyView`` from subscription-week boundaries
    (spec §5.3).

    ``weeks`` is the ``list[SubWeek]`` computed by the caller via
    ``_compute_subscription_weeks(conn, range_start, range_end)``.

    Calls ``_aggregate_weekly(entries, weeks)`` and overlays
    ``weekly_usage_snapshots`` per ``WeekRef`` via
    ``get_latest_usage_for_week`` (clamped to ``as_of_utc`` when
    provided — the CLI passes ``range_end``-Z so historical
    ``--until <past>`` queries pick the period-relevant snapshot, not
    today's). Derives ``dollar_per_pct``, ``delta_cost_pct``,
    ``is_current``.

    Output is newest-first across ``rows`` / ``aggregated`` /
    ``overlay`` — the CLI re-reverses for asc rendering.
    """
    _agg = _load_lib("_lib_aggregators")
    _cct_core = _load_lib("_cctally_core")
    buckets_asc = _agg._aggregate_weekly(entries, weeks, mode=mode)
    if not buckets_asc:
        return WeeklyView(
            rows=(), aggregated=(), overlay=(),
            total_cost_usd=0.0, total_tokens=0,
            period_start=None, period_end=now_utc,
            display_tz_label=_display_tz_label(display_tz),
        )

    # Index SubWeek by `start_date.isoformat()` — the invariant
    # _aggregate_weekly enforces is one-to-one between bucket key and
    # SubWeek.
    week_by_key = {w.start_date.isoformat(): w for w in weeks}
    parse_iso = _cct_core.parse_iso_datetime
    make_ref = _cct_core.make_week_ref
    get_usage = _cct_core.get_latest_usage_for_week

    # Build asc overlay + asc WeeklyPeriodRow list first; reverse later.
    asc_overlay: list = []
    asc_rows: list = []
    total_cost = 0.0
    total_tok = 0
    for i, b in enumerate(buckets_asc):
        sw = week_by_key.get(b.bucket)
        if sw is None:
            # _aggregate_weekly invariant: every emitted bucket key maps
            # 1:1 to a SubWeek in ``weeks``. Surface the invariant
            # violation loudly rather than silently desynchronizing the
            # three parallel lists (``asc_rows`` vs ``asc_overlay`` vs
            # ``buckets_asc``) — under the prior defensive ``continue``
            # branch, ``asc_overlay`` would advance one slot while
            # ``asc_rows`` stayed put, creating a latent index-misalign
            # for any consumer reading ``view.rows`` parallel to
            # ``view.aggregated`` / ``view.overlay``.
            raise AssertionError(
                f"_aggregate_weekly emitted bucket without matching "
                f"SubWeek (invariant violation): bucket={b.bucket!r}"
            )
        ref = make_ref(
            week_start_date=sw.start_date.isoformat(),
            week_end_date=sw.end_date.isoformat(),
            week_start_at=sw.start_ts,
            week_end_at=sw.end_ts,
        )
        usage_row = get_usage(conn, ref, as_of_utc=as_of_utc)
        used_pct = None
        if usage_row is not None and usage_row["weekly_percent"] is not None:
            used_pct = float(usage_row["weekly_percent"])
        dpp = (b.cost_usd / used_pct) if (used_pct and used_pct > 0) else None
        asc_overlay.append((used_pct, dpp))

        # delta_cost_pct vs the prior (older) bucket. asc order: prior
        # is at index i - 1.
        prev = buckets_asc[i - 1] if i > 0 else None
        delta = None
        if prev is not None and prev.cost_usd > 0:
            delta = (b.cost_usd - prev.cost_usd) / prev.cost_usd

        # is_current: now_utc falls inside [start_ts, end_ts).
        try:
            sw_start = parse_iso(sw.start_ts, "week.start_ts")
            sw_end = parse_iso(sw.end_ts, "week.end_ts")
            is_current = sw_start <= now_utc < sw_end
        except ValueError:
            # parse_iso_datetime raises ValueError on malformed ISO; any
            # other exception is a genuine bug — let it propagate.
            is_current = False

        asc_rows.append(WeeklyPeriodRow(
            label=sw.start_date.strftime("%m-%d"),
            cost_usd=b.cost_usd,
            total_tokens=b.total_tokens,
            input_tokens=b.input_tokens,
            output_tokens=b.output_tokens,
            cache_creation_tokens=b.cache_creation_tokens,
            cache_read_tokens=b.cache_read_tokens,
            used_pct=used_pct,
            dollar_per_pct=dpp,
            delta_cost_pct=delta,
            is_current=is_current,
            models=_model_breakdowns_to_models_late(
                b.model_breakdowns, b.cost_usd,
            ),
            week_start_at=sw.start_ts,
            week_end_at=sw.end_ts,
        ))
        total_cost += b.cost_usd
        total_tok += b.total_tokens

    # Reverse to newest-first across all three parallel lists.
    rows = list(reversed(asc_rows))
    aggregated = list(reversed(buckets_asc))
    overlay = list(reversed(asc_overlay))

    period_start_dt = None
    if weeks:
        try:
            period_start_dt = parse_iso(weeks[0].start_ts, "weeks[0].start_ts")
        except ValueError:
            # parse_iso_datetime raises ValueError on malformed ISO; any
            # other exception is a genuine bug — let it propagate.
            period_start_dt = None
    return WeeklyView(
        rows=tuple(rows),
        aggregated=tuple(aggregated),
        overlay=tuple(overlay),
        total_cost_usd=total_cost,
        total_tokens=total_tok,
        period_start=period_start_dt,
        period_end=now_utc,
        display_tz_label=_display_tz_label(display_tz),
    )


# === TrendView + build_trend_view (Task 10) ================================


@dataclass(frozen=True)
class TrendView:
    """Trend view — last n subscription weeks for cmd_report / TUI / dashboard
    trend panel / share `_build_report_snapshot` (spec §4.2, §5.4).

    Rows are typed ``TuiTrendRow`` (extended per spec §4.1 with 10
    nullable fields so the same shape serves both the TUI's 7-field
    surface and ``cmd_report``'s 11-field JSON contract).

    ``avg_dollars_per_pct`` is the 3-sample-rule mean (None when fewer
    than 3 rows carry a non-None ``dollars_per_percent``). The
    dashboard envelope adapter emits it as ``trend.avg_dollars_per_pct``
    so the React layer doesn't re-derive.

    ``median_dpp_non_current_4w`` is the median of the last 4 non-current
    ``dollars_per_percent`` values (``None`` when fewer than 4 valid
    samples). Matches the rule TrendModal.tsx's ``median4NonCurrent``
    helper used to compute client-side; pre-computed on the View so
    the dashboard envelope can surface it as
    ``trend.history_median_dpp`` (issue #59). The 8-row panel call also
    populates the field — but the dashboard envelope only surfaces the
    12-row history's median (panel modal vs panel-wide are different
    summaries).

    Row ordering matches ``cmd_report`` / ``_tui_build_trend``:
    chronological (oldest first), suitable for the TUI sparkline left-
    to-right walk + cmd_report's `--json` trend list (which is then
    sorted by recency at the render site).
    """
    rows: "tuple[TuiTrendRow, ...]" = ()        # oldest-first
    avg_dollars_per_pct: "float | None" = None
    median_dpp_non_current_4w: "float | None" = None
    period_start: "dt.datetime | None" = None
    period_end: "dt.datetime | None" = None
    display_tz_label: str = ""


def build_trend_view(conn, *, now_utc, n=8, display_tz=None):
    """Build a ``TrendView`` of the last ``n`` subscription weeks
    (spec §5.4).

    Reads ``weekly_usage_snapshots`` for usage% per ``WeekRef``. Reads
    ``weekly_cost_snapshots`` for cost — EXCEPT for weeks touched by a
    reset event, where the builder bypasses the cache and live-
    computes cost from ``session_entries`` via
    ``_compute_cost_for_weekref(week_ref)``. This matches the existing
    cmd_report path (``bin/cctally:~7969-7979``) and ``_tui_build_trend``
    (``bin/_cctally_tui.py:~1515-1524``).

    Rows are emitted oldest-first (chronological); each row populates
    all 17 ``TuiTrendRow`` fields (7 historical + 10 extended).
    ``delta_dpp`` is computed against the prior chronological row's
    non-None dpp. ``spark_height`` is normalized 1..8 across the
    window's valid dpp samples.

    ``avg_dollars_per_pct`` follows the 3-sample rule (spec §4.3):
    mean over non-None ``dollars_per_percent`` values iff at least 3
    rows qualify; else None. Mirrors
    ``_build_report_snapshot``'s historical behavior.
    """
    _cct_core = _load_lib("_cctally_core")
    parse_iso = _cct_core.parse_iso_datetime
    make_ref = _cct_core.make_week_ref
    get_usage = _cct_core.get_latest_usage_for_week
    c = _cctally()

    week_refs = c.get_recent_weeks(conn, max(1, n))
    if not week_refs:
        return TrendView(
            rows=(), avg_dollars_per_pct=None,
            period_start=None, period_end=now_utc,
            display_tz_label=_display_tz_label(display_tz),
        )

    # Determine current_key + current_week_start_at — same pattern as
    # _tui_build_trend / cmd_report's Bug D handling.
    latest_usage = conn.execute(
        "SELECT week_start_date, week_end_date "
        "FROM weekly_usage_snapshots "
        "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
    ).fetchone()
    current_key = None
    current_week_start_at = None
    if latest_usage is not None and latest_usage["week_start_date"] is not None:
        current_key = latest_usage["week_start_date"]
        try:
            canon_start, canon_end = c._get_canonical_boundary_for_date(
                conn, latest_usage["week_start_date"]
            )
            current_ref = make_ref(
                week_start_date=latest_usage["week_start_date"],
                week_end_date=latest_usage["week_end_date"],
                week_start_at=canon_start,
                week_end_at=canon_end,
            )
            _adjusted = c._apply_reset_events_to_weekrefs(conn, [current_ref])
            if _adjusted:
                current_week_start_at = _adjusted[0].week_start_at
        except Exception:
            current_week_start_at = None

    # Build a chronological (oldest-first) intermediate over week_refs.
    # week_refs come newest-first from get_recent_weeks; reverse.
    chrono = list(reversed(week_refs))

    # Split-key set (Bug D): credited weeks appear twice in week_refs
    # with identical WeekRef.key. Pin as_of_utc=week_end_at for those
    # so each segment finds its own latest snapshot.
    split_keys = {
        r.key for r in week_refs
        if sum(1 for x in week_refs if x.key == r.key) > 1
    }

    try:
        _fresh_cfg = c._get_oauth_usage_config(c.load_config())
    except Exception:
        _fresh_cfg = c._get_oauth_usage_config({})

    intermediate: list = []
    for week_ref in chrono:
        usage = get_usage(
            conn, week_ref,
            as_of_utc=(
                week_ref.week_end_at if week_ref.key in split_keys else None
            ),
        )
        usage_captured_at = usage["captured_at_utc"] if usage else None
        if c._week_ref_has_reset_event(conn, week_ref):
            cost_usd = c._compute_cost_for_weekref(week_ref)
            cost_captured_at = (
                usage_captured_at if cost_usd is not None else None
            )
            range_start_iso = week_ref.week_start_at
            range_end_iso = week_ref.week_end_at
        else:
            cost = c.get_latest_cost_for_week(conn, week_ref)
            cost_usd = float(cost["cost_usd"]) if cost else None
            cost_captured_at = cost["captured_at_utc"] if cost else None
            range_start_iso = (
                cost["range_start_iso"] if cost and cost["range_start_iso"]
                else None
            )
            range_end_iso = (
                cost["range_end_iso"] if cost and cost["range_end_iso"]
                else None
            )
        percent = float(usage["weekly_percent"]) if usage else None
        ratio = (
            cost_usd / percent
            if (cost_usd is not None and percent and percent > 0)
            else None
        )
        intermediate.append({
            "week_ref": week_ref,
            "used_pct": percent,
            "cost_usd": cost_usd,
            "dpp": ratio,
            "usage_captured_at": usage_captured_at,
            "cost_captured_at": cost_captured_at,
            "range_start_iso": range_start_iso,
            "range_end_iso": range_end_iso,
        })

    # Normalize dpp into spark heights 1..8 across the chrono window.
    dpps = [d["dpp"] for d in intermediate if d["dpp"] is not None]
    if dpps:
        lo, hi = min(dpps), max(dpps)
        span = (hi - lo) or 1e-9
    else:
        lo, hi, span = 0.0, 1.0, 1e-9

    rows: list = []
    prev_dpp: float | None = None
    for d in intermediate:
        week_ref = d["week_ref"]
        percent = d["used_pct"]
        cost_usd = d["cost_usd"]
        dpp = d["dpp"]
        usage_captured_at = d["usage_captured_at"]
        cost_captured_at = d["cost_captured_at"]
        range_start_iso = d["range_start_iso"]
        range_end_iso = d["range_end_iso"]

        delta = (
            (dpp - prev_dpp)
            if (dpp is not None and prev_dpp is not None) else None
        )
        spark = 1
        if dpp is not None:
            spark = int(round((dpp - lo) / span * 7)) + 1
            spark = max(1, min(8, spark))

        # WeekRef.week_start is a date; build a tz-aware datetime.
        if week_ref.week_start_at:
            week_start_dt = parse_iso(week_ref.week_start_at, "week_start_at")
            _format_dt = c.format_display_dt
            week_label = _format_dt(
                week_start_dt, display_tz, fmt="%b %d", suffix=False,
            )
        else:
            week_start_dt = dt.datetime.combine(
                week_ref.week_start, dt.time(0, 0), dt.timezone.utc,
            )
            week_label = week_ref.week_start.strftime("%b %d")

        is_cur = (
            current_key is not None
            and week_ref.key == current_key
            and (
                current_week_start_at is None
                or week_ref.week_start_at == current_week_start_at
            )
        )

        # as_of = max(usage_captured_at, cost_captured_at).
        usage_dt = c._parse_iso_datetime_optional(usage_captured_at)
        cost_dt = c._parse_iso_datetime_optional(cost_captured_at)
        if usage_dt and cost_dt:
            as_of_dt = usage_dt if usage_dt >= cost_dt else cost_dt
        else:
            as_of_dt = usage_dt or cost_dt
        as_of = as_of_dt.isoformat(timespec="seconds") if as_of_dt else None

        freshness = None
        if usage_captured_at:
            age_s = c._seconds_since_iso(usage_captured_at)
            if age_s is not None and age_s <= 86400:
                freshness = {
                    "label": c._freshness_label(age_s, _fresh_cfg),
                    "captured_at": usage_captured_at,
                    "age_seconds": int(age_s),
                }

        rows.append(TuiTrendRow(
            week_label=week_label,
            week_start_at=week_start_dt,
            used_pct=percent,
            dollars_per_percent=dpp,
            delta_dpp=delta,
            spark_height=spark,
            is_current=is_cur,
            # Extended fields (spec §4.1)
            week_start_date=week_ref.week_start,
            week_end_date=week_ref.week_end,
            week_end_at=(
                parse_iso(week_ref.week_end_at, "week_end_at")
                if week_ref.week_end_at else None
            ),
            weekly_cost_usd=cost_usd,
            usage_captured_at=usage_captured_at,
            cost_captured_at=cost_captured_at,
            as_of=as_of,
            range_start_iso=range_start_iso,
            range_end_iso=range_end_iso,
            freshness=freshness,
        ))
        if dpp is not None:
            prev_dpp = dpp

    # 3-sample average rule (spec §4.3): mean of non-None dpps iff at
    # least 3 samples qualify.
    valid_dpps = [r.dollars_per_percent for r in rows
                  if r.dollars_per_percent is not None]
    avg = (stable_sum(valid_dpps) / len(valid_dpps)) if len(valid_dpps) >= 3 else None

    # Issue #59 — pre-compute the 4-week-median-non-current dpp scalar
    # the dashboard's Trend modal hero KV displays. Rule mirrors
    # ``TrendModal.tsx::median4NonCurrent`` byte-for-byte:
    #   * Drop EXACTLY ONE row by index — the same row
    #     ``findCurrentIndex`` would pick: the FIRST ``is_current``
    #     row, or ``rows.length - 1`` (the last row) when no row is
    #     marked current. This matters when (a) the Bug D credited-
    #     week split emits two rows with the same ``current_key``
    #     (both ``is_current=True``; we drop only the first) and (b)
    #     cost-only histories with no usage snapshot have every row
    #     ``is_current=False`` (we still drop the last row).
    #   * Keep only non-None / finite dpp values.
    #   * Take the LAST 4 (chronological-last, since ``rows`` is
    #     oldest-first); sort ascending; return the midpoint
    #     ``(s[1] + s[2]) / 2``.
    # Returns ``None`` when fewer than 4 non-current valid samples
    # remain (matches the modal's empty-state). The 8-row panel call
    # populates this too — harmless because the envelope only surfaces
    # the 12-row history's value.
    if rows:
        cur_idx = next(
            (i for i, r in enumerate(rows) if r.is_current),
            len(rows) - 1,
        )
    else:
        cur_idx = -1
    non_cur_dpps = [r.dollars_per_percent
                    for i, r in enumerate(rows)
                    if i != cur_idx and r.dollars_per_percent is not None]
    if len(non_cur_dpps) >= 4:
        last4 = sorted(non_cur_dpps[-4:])
        median_dpp_4w = (last4[1] + last4[2]) / 2
    else:
        median_dpp_4w = None

    return TrendView(
        rows=tuple(rows),
        avg_dollars_per_pct=avg,
        median_dpp_non_current_4w=median_dpp_4w,
        period_start=None,
        period_end=now_utc,
        display_tz_label=_display_tz_label(display_tz),
    )


# === SessionsView + build_sessions_view (Task 13) ==========================


@dataclass(frozen=True)
class SessionsView:
    """Sessions domain view — Claude sessions (merged across resumes),
    last-activity descending.

    Dual-shape per the spec §6.5 pattern that the daily/monthly/weekly
    views established:

    - ``rows`` carries the typed ``TuiSessionRow`` tuple consumed by the
      TUI sessions panel and the dashboard session-detail surface.
    - ``aggregated`` carries the parallel ``ClaudeSessionUsage`` tuple
      consumed by ``cmd_session`` (CLI table + ``--json``) and the
      share ``_build_session_snapshot`` (needs ``source_paths``,
      ``model_breakdowns``, ``last_activity`` — fields ``TuiSessionRow``
      doesn't carry).

    Both shapes derive from the SAME ``_aggregate_claude_sessions``
    call so the resumed-session merge invariant (``CLAUDE.md`` "Cost /
    weekly / session" gotcha block — a ``sessionId`` across multiple
    JSONL files collapses into ONE row) is preserved end-to-end:
    ``rows[i]`` and ``aggregated[i]`` describe the same merged
    sessionId.

    ``total_sessions == len(rows) == len(aggregated)`` always (spec
    §4.3). Empty entries → ``rows=()``, ``aggregated=()``, totals
    zero — no exceptions on empty input.

    ``limit=None`` keeps the full aggregator output (CLI use case —
    ``cctally session`` has no ``--limit`` flag and emits everything in
    the date range). ``limit`` ≥ 1 truncates BOTH parallel tuples to
    the leading ``limit`` rows (TUI / dashboard use case — the
    sessions pane promises "last N sessions"). The aggregator's
    descending-by-last-activity sort means the leading rows are the
    most recent; the TUI's ``[:100]`` cap stays semantic-stable.
    """
    rows: "tuple[TuiSessionRow, ...]" = ()
    aggregated: tuple = ()              # tuple[ClaudeSessionUsage, ...] — forward-ref kept untyped to avoid an import-time edge into the aggregator's ClaudeSessionUsage shape.
    total_sessions: int = 0
    total_cost_usd: float = 0.0
    period_start: "dt.datetime | None" = None
    period_end: "dt.datetime | None" = None
    display_tz_label: str = ""


def build_sessions_view(entries, *, now_utc, limit=None, display_tz=None, mode="auto"):
    """Build a ``SessionsView`` from joined Claude session entries
    (spec §5.5).

    ``entries`` is the ``list[_JoinedClaudeEntry]`` from
    ``get_claude_session_entries(range_start, range_end)``. The caller
    controls the date window + ``skip_sync`` semantics; the builder
    does no I/O of its own beyond what ``_aggregate_claude_sessions``
    already does (cost recompute from ``CLAUDE_MODEL_PRICING``).

    Per-row derivations (mirror today's ``_tui_build_sessions``
    inline body):
      - ``duration_minutes`` = ``(last_activity - first_activity)`` in
        minutes (float).
      - ``cache_hit_pct`` = ``cache_read / (input + cache_creation +
        cache_read) * 100`` when the denominator is positive; ``None``
        otherwise.
      - ``model_primary`` = first model in the session's first-seen
        order; ``"—"`` if the session somehow has no models (defensive
        — the aggregator only emits sessions with at least one entry).
      - ``project_label`` = ``os.path.basename(project_path)`` or
        ``project_path`` itself when the basename is empty (root paths).

    ``period_start`` is set to ``now_utc - 365d`` to match
    ``_tui_build_sessions``' bounded scan window — strictly cosmetic
    metadata (the caller's actual date range owns the entries fetched).
    Share / CLI consumers prefer the caller-supplied range; this field
    is informational only.
    """
    import os as _os                # late: keep top-level imports lean.
    _agg = _load_lib("_lib_aggregators")
    aggregated = _agg._aggregate_claude_sessions(entries, mode=mode)
    # Apply limit truncation up front so `rows` and `aggregated` stay
    # in lockstep (spec §4.3 invariant: `total_sessions == len(rows)
    # == len(aggregated)`). limit=None → keep everything.
    if limit is not None:
        aggregated = aggregated[:limit]

    rows = []
    total_cost = 0.0
    for s in aggregated:
        duration_min = (
            (s.last_activity - s.first_activity).total_seconds() / 60.0
        )
        denom = s.input_tokens + s.cache_creation_tokens + s.cache_read_tokens
        cache_pct = (
            (s.cache_read_tokens / denom * 100.0) if denom > 0 else None
        )
        rows.append(TuiSessionRow(
            started_at=s.first_activity,
            duration_minutes=duration_min,
            model_primary=(s.models[0] if s.models else "—"),
            cost_usd=s.cost_usd,
            cache_hit_pct=cache_pct,
            project_label=(
                _os.path.basename(s.project_path) or s.project_path
            ),
            session_id=s.session_id,
            # `project_key` is populated downstream by `_tui_build_snapshot`
            # after `_build_projects_envelope` runs (so the disambiguated
            # display_key matches the Projects envelope exactly). Stash
            # the absolute path here so that pass can map back without
            # re-reading session_files.
            project_path=s.project_path or None,
        ))
        total_cost += s.cost_usd

    return SessionsView(
        rows=tuple(rows),
        aggregated=tuple(aggregated),
        total_sessions=len(rows),
        total_cost_usd=total_cost,
        period_start=(now_utc - dt.timedelta(days=365)),
        period_end=now_utc,
        display_tz_label=_display_tz_label(display_tz),
    )


# === BlocksView + build_blocks_view (Issue #56) ============================


@dataclass(frozen=True)
class BlocksView:
    """Blocks domain view — covers two structurally distinct paths under
    one dataclass.

    1. **Heuristic-aware** (``cmd_blocks`` + dashboard Blocks panel):
       built via ``build_blocks_view(entries, ...)``. Calls
       ``_lib_blocks._group_entries_into_blocks`` and fills BOTH
       ``rows`` (``tuple[BlocksPanelRow, ...]`` — non-gap, dashboard-
       shape, newest-first) and ``aggregated`` (``tuple[Block, ...]``
       — gaps included, CLI-shape, oldest-first per
       ``_group_entries_into_blocks``'s contract). ``total_cost_usd``
       / ``total_tokens`` are summed over non-gap blocks so the React
       panel's footer ``total === sum(visible rows)`` invariant holds.

    2. **API-anchored** (``cmd_five_hour_blocks`` + share snapshot):
       built via ``build_blocks_view_from_table_rows(rows, ...)``.
       Reads sqlite-Row-derived dicts from the ``five_hour_blocks``
       TABLE; leaves ``rows`` empty (consumers read ``aggregated``
       directly). Reset-aware (CLAUDE.md 5-hour gotcha block,
       spec §3.2) — totals come from the table's per-block columns,
       NOT recomputed from ``session_entries``.

    Both builders return BlocksView; consumers branch on which field
    they need. ``period_start`` / ``period_end`` carry time-window
    bounds (used by the share path's ``PeriodSpec`` and by future
    consumers that need a period label).
    """
    rows: "tuple[BlocksPanelRow, ...]" = ()
    aggregated: tuple = ()                    # tuple[Block, ...] for heuristic; tuple[dict, ...] for API-anchored. Forward-ref kept untyped to avoid an import-time edge into _lib_blocks.
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    period_start: "dt.datetime | None" = None
    period_end: "dt.datetime | None" = None
    display_tz_label: str = ""


def build_blocks_view(
    entries,
    *,
    now_utc,
    recorded_windows=None,
    block_start_overrides=None,
    canonical_intervals=None,
    range_start=None,
    range_end=None,
    display_tz=None,
    mode="auto",
    skip_rows: bool = False,
):
    """Build a ``BlocksView`` from raw ``UsageEntry`` list (heuristic-
    aware path; spec §6 blocks follow-up).

    ``aggregated`` carries the full Block list (gaps included), in
    oldest-first order — consumed by ``cmd_blocks`` via
    ``_blocks_to_json`` / ``_render_blocks_table``. ``rows`` carries
    non-gap ``BlocksPanelRow`` entries (newest-first) — consumed by
    ``_dashboard_build_blocks_panel`` and the dashboard envelope
    serializer.

    Per-row enrichment for the dashboard rows uses
    ``_lib_pricing._calculate_entry_cost`` (single pricing source-of-
    truth, mirrors the historical inline body of
    ``_dashboard_build_blocks_panel``). ``label`` is pre-formatted via
    ``format_display_dt`` ("HH:MM MMM DD" in display_tz).

    Totals are summed over non-gap blocks (gaps contribute zero by
    construction — they carry zero cost / tokens). Caller-supplied
    ``range_start`` / ``range_end`` override the period metadata when
    provided (the CLI ``cmd_blocks`` --since/--until path passes them
    explicitly; the dashboard path passes the week window).

    ``skip_rows=True`` skips the dashboard-row construction loop
    entirely — leaves ``view.rows = ()`` while still populating
    ``aggregated`` + totals. The per-block per-model enrichment scans
    every entry for every non-gap block (O(B × N)); the CLI
    ``cmd_blocks`` reads only ``view.aggregated`` and discards rows,
    so it opts in to skip that work on large histories. Dashboard /
    share callers leave the default (``False``) to keep their
    consumers fed.
    """
    _lib_blocks = _load_lib("_lib_blocks")
    _lib_pricing = _load_lib("_lib_pricing")
    c = _cctally()
    blocks = _lib_blocks._group_entries_into_blocks(
        entries,
        mode=mode,
        recorded_windows=recorded_windows,
        block_start_overrides=block_start_overrides,
        canonical_intervals=canonical_intervals,
        now=now_utc,
    )
    rows: list = []
    total_cost = 0.0
    total_tok = 0
    if blocks:
        for b in blocks:
            if b.is_gap:
                continue
            if not skip_rows:
                # Per-block per-model breakdown for the dashboard row.
                # Mirrors `_dashboard_build_blocks_panel`'s historical
                # inline body — re-aggregates entries inside the block
                # interval through the single pricing chokepoint so per-
                # model costs reconcile exactly with `b.cost_usd`.
                per_model: dict[str, float] = {}
                for e in entries:
                    if b.start_time <= e.timestamp < b.end_time:
                        cost = _lib_pricing._calculate_entry_cost(
                            e.model, e.usage, mode=mode, cost_usd=e.cost_usd,
                        )
                        per_model[e.model] = per_model.get(e.model, 0.0) + cost
                model_breakdowns = [
                    {"modelName": name, "cost": cost}
                    for name, cost in sorted(
                        per_model.items(), key=lambda kv: -kv[1],
                    )
                ]
                local_label = c.format_display_dt(
                    b.start_time, display_tz, fmt="%H:%M %b %d", suffix=True,
                )
                rows.append(BlocksPanelRow(
                    start_at=b.start_time.astimezone(dt.timezone.utc).isoformat(),
                    end_at=b.end_time.astimezone(dt.timezone.utc).isoformat(),
                    anchor=b.anchor,
                    is_active=bool(b.is_active and b.entries_count > 0),
                    cost_usd=b.cost_usd,
                    models=_model_breakdowns_to_models_late(
                        model_breakdowns, b.cost_usd,
                    ),
                    label=local_label,
                ))
            total_cost += b.cost_usd
            total_tok += b.total_tokens
    rows.sort(key=lambda r: r.start_at, reverse=True)

    # Period defaults: caller-supplied range wins; otherwise fall back
    # to block extent (first block's start) so the share builder /
    # period-label paths get a sensible window.
    period_start_dt = range_start
    if period_start_dt is None and blocks:
        period_start_dt = blocks[0].start_time
    period_end_dt = range_end or now_utc

    return BlocksView(
        rows=tuple(rows),
        aggregated=tuple(blocks),
        total_cost_usd=total_cost,
        total_tokens=total_tok,
        period_start=period_start_dt,
        period_end=period_end_dt,
        display_tz_label=_display_tz_label(display_tz),
    )


def build_blocks_view_from_table_rows(
    block_dicts,
    *,
    period_start=None,
    period_end=None,
    display_tz=None,
):
    """Build a ``BlocksView`` from API-anchored ``five_hour_blocks``
    table rows (issue #56 — share path).

    Reset-aware totals (CLAUDE.md 5-hour gotcha block, spec §3.2):
    ``total_cost_usd`` is summed from each row's ``total_cost_usd``
    column (already credit-aware at write time);
    ``total_tokens`` is summed across the four token columns
    (``total_input_tokens`` + ``total_output_tokens`` +
    ``total_cache_create_tokens`` + ``total_cache_read_tokens``).
    No recomputation from ``session_entries`` — preserves the
    write-time invariant that ``five_hour_blocks.total_cost_usd``
    is the authoritative per-block cost.

    ``rows`` is left empty — the API-anchored consumers
    (``_five_hour_blocks_to_json``, ``_render_five_hour_blocks_table``,
    ``_build_five_hour_blocks_snapshot``) read ``aggregated`` (the
    underlying dict list) directly. ``BlocksPanelRow`` doesn't carry
    the API-anchored extras (``final_five_hour_percent``,
    ``crossed_seven_day_reset``, ``credits``, ...) so synthesizing
    rows on this path would lose data.

    ``block_dicts`` is consumed as-is; the caller controls ordering
    (``cmd_five_hour_blocks`` produces newest-first DESC).
    """
    rows_seq = list(block_dicts)
    total_cost = stable_sum(
        float(d.get("total_cost_usd") or 0.0) for d in rows_seq
    )
    total_tok = sum(
        int(d.get("total_input_tokens") or 0)
        + int(d.get("total_output_tokens") or 0)
        + int(d.get("total_cache_create_tokens") or 0)
        + int(d.get("total_cache_read_tokens") or 0)
        for d in rows_seq
    )
    return BlocksView(
        rows=(),
        aggregated=tuple(rows_seq),
        total_cost_usd=total_cost,
        total_tokens=total_tok,
        period_start=period_start,
        period_end=period_end,
        display_tz_label=_display_tz_label(display_tz),
    )


# === ForecastView + build_forecast_view (Issue #57) ========================


_FORECAST_VERDICT_GOOD = "GOOD"
_FORECAST_VERDICT_WARN = "WARN"
_FORECAST_VERDICT_OVER = "OVER"
_FORECAST_VERDICT_LOW_CONF = "LOW CONF"


@dataclass(frozen=True)
class ForecastView:
    """Forecast domain view — wraps the existing math kernel.

    Unlike the rows-shaped domain views, ``ForecastView`` projects a
    *singular* week into the future. The wrapped ``output`` carries the
    full ``ForecastOutput`` math result (inputs + r_avg + r_recent +
    final_percent_{low,high} + budgets[] + cap_at), and the View
    additively surfaces fields that consumers used to re-derive:

    * ``verdict`` — TUI design-language mapping ("GOOD" / "WARN" /
      "OVER" / "LOW CONF"). Mirrors ``_tui_verdict_of``.
    * ``dashboard_verdict`` — dashboard envelope's mapping ("ok" /
      "cap" / "capped"). Mirrors the per-method routing in
      ``snapshot_to_envelope``.
    * ``week_avg_projection_pct`` / ``recent_24h_projection_pct`` —
      per-method projections from ``r_avg`` / ``r_recent``. The
      recent-24h value is ``None`` when ``r_recent`` is ``None`` or its
      projection equals ``week_avg_projection_pct`` (no new info).
      Routing labels stay correct on decelerating weeks where
      ``r_recent < r_avg``.
    * ``header_projection_pct`` — "pick pessimistic when verdict
      warns" routing the dashboard header runs. Surfaced once on the
      view so the header field and the verdict pill always tell the
      same story.
    * ``budget_100_per_day_usd`` / ``budget_90_per_day_usd`` — the
      matching ``BudgetRow.dollars_per_day`` values, ``None`` when the
      target is out of headroom.
    * ``confidence`` / ``low_confidence`` / ``low_confidence_reasons`` —
      mirrors ``inputs.confidence``. Surfaced separately so callers can
      key on ``view.low_confidence`` without crawling
      ``view.output.inputs``.

    ``output`` is ``None`` when ``_load_forecast_inputs`` returned
    ``None`` (no current-week snapshot). The View still constructs in
    that case so consumers can render an empty-state from a uniformly-
    shaped object; ``verdict`` is then ``"LOW CONF"`` and the projection
    / budget fields are ``None``.

    ``period_start`` / ``period_end`` carry the subscription-week
    bounds (``inputs.week_start_at`` / ``inputs.week_end_at``), mirroring
    the other domain views.
    """
    output: Any | None = None                  # ForecastOutput | None — forward-ref kept untyped to avoid an import-time edge into cctally's dataclasses.
    verdict: str = _FORECAST_VERDICT_LOW_CONF
    dashboard_verdict: str = "ok"
    confidence: str = "unknown"
    low_confidence: bool = False
    low_confidence_reasons: tuple = ()
    week_avg_projection_pct: "float | None" = None
    recent_24h_projection_pct: "float | None" = None
    header_projection_pct: "float | None" = None
    budget_100_per_day_usd: "float | None" = None
    budget_90_per_day_usd: "float | None" = None
    period_start: "dt.datetime | None" = None
    period_end: "dt.datetime | None" = None
    display_tz_label: str = ""
    targets: tuple = ()


def _forecast_verdict_of(output) -> str:
    """Design-language verdict for a ``ForecastOutput``. Mirrors
    ``_tui_verdict_of`` but lives on the view-model layer so consumers
    don't have to round-trip through ``_cctally_tui``.

    None output OR low confidence → ``"LOW CONF"``. Otherwise threshold
    on ``final_percent_high``: ≥100 → OVER, ≥90 → WARN, else GOOD.
    """
    if output is None:
        return _FORECAST_VERDICT_LOW_CONF
    inputs = getattr(output, "inputs", None)
    if inputs is not None and getattr(inputs, "confidence", "high") == "low":
        return _FORECAST_VERDICT_LOW_CONF
    high = float(getattr(output, "final_percent_high", 0.0))
    if high >= 100:
        return _FORECAST_VERDICT_OVER
    if high >= 90:
        return _FORECAST_VERDICT_WARN
    return _FORECAST_VERDICT_GOOD


def _forecast_dashboard_verdict_of(output) -> str:
    """Dashboard-envelope verdict ("ok"/"cap"/"capped"). Pure helper
    used by ``snapshot_to_envelope`` and ``build_forecast_view``."""
    if output is None:
        return "ok"
    if getattr(output, "already_capped", False):
        return "capped"
    if getattr(output, "projected_cap", False):
        return "cap"
    return "ok"


def _forecast_projection_pcts(output) -> "tuple[float | None, float | None]":
    """Return (week_avg_projection_pct, recent_24h_projection_pct).

    Decomposes the dual-method projections from ``r_avg`` / ``r_recent``
    + ``inputs.p_now`` + ``inputs.remaining_hours``. Mirrors the routing
    in ``snapshot_to_envelope``: recent-24h is ``None`` when ``r_recent``
    is ``None`` or its projection equals the week-avg projection (no
    new info — a second method that agrees with the first contributes
    nothing to the user-facing range).
    """
    if output is None:
        return None, None
    inputs = getattr(output, "inputs", None)
    if inputs is None:
        return None, None
    p_now = getattr(inputs, "p_now", None)
    rem = getattr(inputs, "remaining_hours", None)
    r_avg = getattr(output, "r_avg", None)
    r_recent = getattr(output, "r_recent", None)
    week_avg_pct = None
    if p_now is not None and rem is not None and r_avg is not None:
        week_avg_pct = p_now + r_avg * rem
    recent_pct = None
    if p_now is not None and rem is not None and r_recent is not None:
        candidate = p_now + r_recent * rem
        # Suppress the second projection only when it adds no info.
        if week_avg_pct is None or candidate != week_avg_pct:
            recent_pct = candidate
    return week_avg_pct, recent_pct


def _forecast_header_projection_pct(
    week_avg_pct: "float | None",
    recent_24h_pct: "float | None",
    dashboard_verdict: str,
) -> "float | None":
    """Header field routing: when the verdict warns ("cap"/"capped")
    and recent-24h is the more pessimistic of the two, surface that
    so the header number and the verdict pill agree. Otherwise the
    week-avg projection wins (the historical default).
    """
    if (
        dashboard_verdict in ("cap", "capped")
        and recent_24h_pct is not None
        and week_avg_pct is not None
        and recent_24h_pct > week_avg_pct
    ):
        return recent_24h_pct
    return week_avg_pct


def _forecast_budgets(output) -> "tuple[float | None, float | None]":
    """Pull the (100%, 90%) ``BudgetRow.dollars_per_day`` pair from a
    ``ForecastOutput.budgets`` list. Either may be ``None`` when the
    target is out of headroom (``BudgetRow.dollars_per_day is None``).
    """
    if output is None:
        return None, None
    b100 = None
    b90 = None
    for b in getattr(output, "budgets", None) or []:
        tp = getattr(b, "target_percent", None)
        dpd = getattr(b, "dollars_per_day", None)
        if tp == 100:
            b100 = dpd
        elif tp == 90:
            b90 = dpd
    return b100, b90


def build_forecast_view(
    conn,
    *,
    now_utc,
    targets=(100, 90),
    skip_sync: bool = False,
    display_tz=None,
):
    """Build a ``ForecastView`` (issue #57).

    Wraps the existing math kernel (``_load_forecast_inputs`` +
    ``_compute_forecast``) without duplicating logic. Always returns a
    ``ForecastView`` — when ``_load_forecast_inputs`` returns ``None``
    (no current-week snapshot), the View constructs with
    ``output=None`` + ``verdict="LOW CONF"`` so empty-state callers
    don't branch on the wrapper itself.

    ``targets`` are the percent ceilings forwarded to
    ``_compute_forecast`` (default ``(100, 90)`` — matches both
    ``cmd_forecast``'s ``--targets`` default and the TUI sync thread's
    hard-coded value). ``skip_sync`` honours
    ``cctally forecast --no-sync`` (and the dashboard's sync-thread
    refresh skip).
    """
    c = _cctally()
    inputs = c._load_forecast_inputs(conn, now_utc, skip_sync=skip_sync)
    if inputs is None:
        return ForecastView(
            output=None,
            verdict=_FORECAST_VERDICT_LOW_CONF,
            dashboard_verdict="ok",
            confidence="unknown",
            low_confidence=False,
            low_confidence_reasons=(),
            week_avg_projection_pct=None,
            recent_24h_projection_pct=None,
            header_projection_pct=None,
            budget_100_per_day_usd=None,
            budget_90_per_day_usd=None,
            period_start=None,
            period_end=None,
            display_tz_label=_display_tz_label(display_tz),
            targets=tuple(int(t) for t in targets),
        )
    output = c._compute_forecast(inputs, list(int(t) for t in targets))
    verdict = _forecast_verdict_of(output)
    dashboard_verdict = _forecast_dashboard_verdict_of(output)
    week_avg_pct, recent_pct = _forecast_projection_pcts(output)
    header_pct = _forecast_header_projection_pct(
        week_avg_pct, recent_pct, dashboard_verdict,
    )
    b100, b90 = _forecast_budgets(output)
    confidence = getattr(inputs, "confidence", "high")
    return ForecastView(
        output=output,
        verdict=verdict,
        dashboard_verdict=dashboard_verdict,
        confidence=confidence,
        low_confidence=(confidence == "low"),
        low_confidence_reasons=tuple(
            getattr(inputs, "low_confidence_reasons", None) or ()
        ),
        week_avg_projection_pct=week_avg_pct,
        recent_24h_projection_pct=recent_pct,
        header_projection_pct=header_pct,
        budget_100_per_day_usd=b100,
        budget_90_per_day_usd=b90,
        period_start=getattr(inputs, "week_start_at", None),
        period_end=getattr(inputs, "week_end_at", None),
        display_tz_label=_display_tz_label(display_tz),
        targets=tuple(int(t) for t in targets),
    )


# === Codex domain views + builders (Issue #58) =============================
#
# Codex domain is CLI-only — no dashboard panel, no share consumer. The
# four views below wrap the existing ``_aggregate_codex_{daily,monthly,
# weekly,sessions}`` math kernel without changing it, so the
# intentional divergences from upstream documented in CLAUDE.md (LiteLLM
# token semantics, duplicate-event dedup, descending-by-last-activity
# session sort, ``CODEX_LEGACY_FALLBACK_MODEL`` warning) are preserved
# end-to-end.
#
# Naming differences from the Claude views are deliberate:
#
# - The slot carrying the aggregator output is named ``rows`` (not
#   ``aggregated``) — Codex has no parallel typed surface row dataclass
#   to pair with, so the aggregator's typed output IS the surface (same
#   precedent as ``TrendView.rows`` of typed ``TuiTrendRow``).
# - ``display_tz_label`` is the already-resolved string label
#   (``tz_name or _local_tz_name()``), not the ``zoneinfo.ZoneInfo.key``
#   the Claude views emit via ``_display_tz_label(tzinfo)``. Codex
#   commands plumb a string ``tz_name`` end-to-end (see
#   ``_resolve_codex_tz_name``); the View carries the rendered label so
#   ``cmd_codex_*`` can read it directly.
#
# Bucket ordering: ``_aggregate_codex_daily`` / ``_aggregate_codex_monthly``
# / ``_aggregate_codex_weekly`` return ASC (earliest bucket first); the
# View carries that order. ``cmd_codex_*`` reverses to DESC when
# ``--order desc``.
#
# Session ordering: ``_aggregate_codex_sessions`` returns DESC
# (most-recent last_activity first); the View carries that order.
# ``cmd_codex_session`` reverses to ASC when ``--order asc``. The
# upstream-parity DESC default matches ``ccusage-codex``'s session view.


@dataclass(frozen=True)
class CodexDailyView:
    """Codex daily-bucket view (CLI-only).

    ``rows`` is the parallel ``CodexBucketUsage`` tuple in ASC order
    (earliest bucket first) — same as the aggregator's default.
    ``cmd_codex_daily`` reverses for ``--order desc``.
    """
    rows: tuple = ()                    # tuple[CodexBucketUsage, ...]
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    period_start: "dt.datetime | None" = None
    period_end: "dt.datetime | None" = None
    display_tz_label: str = ""


@dataclass(frozen=True)
class CodexMonthlyView:
    """Codex monthly-bucket view (CLI-only).

    ``rows`` is the parallel ``CodexBucketUsage`` tuple in ASC order
    (earliest bucket first). ``cmd_codex_monthly`` reverses for
    ``--order desc``.
    """
    rows: tuple = ()                    # tuple[CodexBucketUsage, ...]
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    period_start: "dt.datetime | None" = None
    period_end: "dt.datetime | None" = None
    display_tz_label: str = ""


@dataclass(frozen=True)
class CodexWeeklyView:
    """Codex weekly-bucket view (CLI-only).

    ``rows`` is the parallel ``CodexBucketUsage`` tuple in ASC order
    (earliest week-start first). ``cmd_codex_weekly`` reverses for
    ``--order desc``. Week-start day is resolved by the caller
    (``week_start_idx``) from config.json + ``WEEKDAY_MAP``.
    """
    rows: tuple = ()                    # tuple[CodexBucketUsage, ...]
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    period_start: "dt.datetime | None" = None
    period_end: "dt.datetime | None" = None
    display_tz_label: str = ""


@dataclass(frozen=True)
class CodexSessionView:
    """Codex session view (CLI-only).

    ``rows`` is the parallel ``CodexSessionUsage`` tuple in DESC order
    (most-recent ``last_activity`` first) — matches upstream
    ``ccusage-codex`` and the aggregator's default sort.
    ``cmd_codex_session`` reverses for ``--order asc``.
    """
    rows: tuple = ()                    # tuple[CodexSessionUsage, ...]
    total_sessions: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    period_start: "dt.datetime | None" = None
    period_end: "dt.datetime | None" = None
    display_tz_label: str = ""


def _codex_tz_label(tz_name: "str | None") -> str:
    """Render the timezone label the way ``cmd_codex_*`` already does
    (``tz_name or _local_tz_name()``). Centralized here so the four
    builders share one chokepoint."""
    if tz_name:
        return tz_name
    return _cctally()._local_tz_name()


def _codex_bucket_totals(buckets) -> "tuple[float, int]":
    """Sum ``cost_usd`` and ``total_tokens`` across a
    ``CodexBucketUsage`` list."""
    total_cost = 0.0
    total_tok = 0
    for b in buckets:
        total_cost += b.cost_usd
        total_tok += b.total_tokens
    return total_cost, total_tok


def _codex_period_start_from_date_bucket(buckets) -> "dt.datetime | None":
    """Parse the earliest ``YYYY-MM-DD`` bucket key (daily / weekly)
    into a UTC datetime at midnight. ``None`` when ``buckets`` is empty."""
    if not buckets:
        return None
    try:
        d = dt.date.fromisoformat(buckets[0].bucket)
    except ValueError:
        return None
    return dt.datetime.combine(d, dt.time.min, tzinfo=dt.timezone.utc)


def _codex_period_start_from_month_bucket(buckets) -> "dt.datetime | None":
    """Parse the earliest ``YYYY-MM`` bucket key (monthly) into a UTC
    datetime at the 1st-of-month midnight. ``None`` when ``buckets`` is
    empty or the key is malformed."""
    if not buckets:
        return None
    try:
        yr, mo = buckets[0].bucket.split("-")
        return dt.datetime(int(yr), int(mo), 1, tzinfo=dt.timezone.utc)
    except (ValueError, IndexError):
        return None


def build_codex_daily_view(entries, *, now_utc, tz_name=None, speed="standard"):
    """Build a ``CodexDailyView`` from a list of ``CodexEntry`` (issue #58).

    Delegates bucketing to ``_aggregate_codex_daily`` (LiteLLM-snapshot
    pricing + Codex token semantics — see CLAUDE.md "Codex (OpenAI)
    parity" gotcha block). ``tz_name`` plumbs through verbatim
    (None → host-local fallback inside the aggregator).
    """
    _agg = _load_lib("_lib_aggregators")
    buckets = _agg._aggregate_codex_daily(entries, tz_name=tz_name, speed=speed)
    total_cost, total_tok = _codex_bucket_totals(buckets)
    return CodexDailyView(
        rows=tuple(buckets),
        total_cost_usd=total_cost,
        total_tokens=total_tok,
        period_start=_codex_period_start_from_date_bucket(buckets),
        period_end=now_utc,
        display_tz_label=_codex_tz_label(tz_name),
    )


def build_codex_monthly_view(entries, *, now_utc, tz_name=None, speed="standard"):
    """Build a ``CodexMonthlyView`` from a list of ``CodexEntry`` (issue #58).

    Same wrap-the-kernel posture as ``build_codex_daily_view``; bucket
    key is ``YYYY-MM`` so ``period_start`` resolves to the 1st of the
    earliest visible month at UTC midnight.
    """
    _agg = _load_lib("_lib_aggregators")
    buckets = _agg._aggregate_codex_monthly(entries, tz_name=tz_name, speed=speed)
    total_cost, total_tok = _codex_bucket_totals(buckets)
    return CodexMonthlyView(
        rows=tuple(buckets),
        total_cost_usd=total_cost,
        total_tokens=total_tok,
        period_start=_codex_period_start_from_month_bucket(buckets),
        period_end=now_utc,
        display_tz_label=_codex_tz_label(tz_name),
    )


def build_codex_weekly_view(entries, *, now_utc, tz_name=None,
                            week_start_idx=0, speed="standard"):
    """Build a ``CodexWeeklyView`` from a list of ``CodexEntry`` (issue #58).

    ``week_start_idx`` is the resolved Mon=0..Sun=6 index the caller
    pulls from config via ``get_week_start_name`` + ``WEEKDAY_MAP``.
    Bucket key is the ISO date of the week's first day in the display
    timezone (matches ``_aggregate_codex_weekly`` contract).
    """
    _agg = _load_lib("_lib_aggregators")
    buckets = _agg._aggregate_codex_weekly(entries, tz_name, week_start_idx, speed=speed)
    total_cost, total_tok = _codex_bucket_totals(buckets)
    return CodexWeeklyView(
        rows=tuple(buckets),
        total_cost_usd=total_cost,
        total_tokens=total_tok,
        period_start=_codex_period_start_from_date_bucket(buckets),
        period_end=now_utc,
        display_tz_label=_codex_tz_label(tz_name),
    )


def build_codex_session_view(entries, *, now_utc, tz_name=None, speed="standard"):
    """Build a ``CodexSessionView`` from a list of ``CodexEntry`` (issue #58).

    ``rows`` order mirrors the aggregator: descending by
    ``last_activity`` (upstream parity).
    ``cmd_codex_session`` reverses to ASC when ``--order asc``.

    ``period_start`` is set to ``min(s.last_activity)`` across emitted
    sessions when any exist — best-available approximation since
    ``CodexSessionUsage`` doesn't carry a ``first_activity`` field (the
    aggregator only tracks ``last`` per session). ``None`` on empty.
    """
    _agg = _load_lib("_lib_aggregators")
    sessions = _agg._aggregate_codex_sessions(entries, speed=speed)
    total_cost = 0.0
    total_tok = 0
    earliest = None
    for s in sessions:
        total_cost += s.cost_usd
        total_tok += s.total_tokens
        if earliest is None or s.last_activity < earliest:
            earliest = s.last_activity
    return CodexSessionView(
        rows=tuple(sessions),
        total_sessions=len(sessions),
        total_cost_usd=total_cost,
        total_tokens=total_tok,
        period_start=earliest,
        period_end=now_utc,
        display_tz_label=_codex_tz_label(tz_name),
    )
