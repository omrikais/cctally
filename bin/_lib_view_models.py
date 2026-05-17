"""View-model kernel for CLI / dashboard / share consumers.

This module owns the per-domain row dataclasses and the ``*View``
wrappers that carry rows + data-plane aggregates (totals, averages).

Bundle 1 (this commit) starts with row dataclass relocation from
``bin/_cctally_tui.py``:

- ``DailyPanelRow``, ``MonthlyPeriodRow``, ``WeeklyPeriodRow``,
  ``TuiSessionRow`` move **verbatim** (no field changes).
- ``TuiTrendRow`` moves with the 10 nullable fields added per spec §4.1
  (``week_start_date``, ``week_end_date``, ``week_end_at``,
  ``weekly_cost_usd``, ``usage_captured_at``, ``cost_captured_at``,
  ``as_of``, ``range_start_iso``, ``range_end_iso``, ``freshness``).
  All defaults are ``None`` so existing TUI / dashboard fixtures that
  construct ``TuiTrendRow`` positionally stay byte-stable.

``bin/_cctally_tui.py`` re-exports each name so historical imports
(``from _cctally_tui import DailyPanelRow``, ``ns["DailyPanelRow"]``
direct-dict reads in tests) keep resolving.

Subsequent Bundle 1 tasks add ``*View`` frozen dataclasses and the
``build_*_view(...)`` builders.

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
    rows: tuple = ()                          # tuple[DailyPanelRow, ...]
    aggregated: tuple = ()                    # tuple[BucketUsage, ...]
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    period_start: "dt.datetime | None" = None
    period_end: "dt.datetime | None" = None
    display_tz_label: str = ""


def build_daily_view(entries, *, now_utc, display_tz=None):
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
    """
    _agg = _load_lib("_lib_aggregators")
    buckets = _agg._aggregate_daily(entries, mode="auto", tz=display_tz)
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
