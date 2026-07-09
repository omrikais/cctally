"""Forecast decision kernel for cctally.

Pure-fn layer (no I/O at import time): the forecast inputs/output/budget
dataclasses plus `_compute_forecast` — the projection + budget-headroom
math. Values in (`ForecastInputs`), a `ForecastOutput` out. No DB reads,
no config loads, no `_cctally()` accessor: the projection routes through
`project_linear` (the shared, pure primitive whose real home is
`_lib_budget`).

Imported by `_cctally_forecast.py` (and `_cctally_tui.py`) and re-exported
on the `cctally` namespace via `bin/cctally`, so the existing
`cctally.ForecastInputs` / `mod.ForecastOutput` / `inspect.getsource(
cctally._compute_forecast)` read paths resolve unchanged (re-export
continuity, spec §2). Single definition of each dataclass lives here;
everything else imports it, so class identity stays unique.

Spec: docs/superpowers/specs/2026-07-09-279-s4-record-kernelization-design.md
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from _lib_budget import project_linear


@dataclass
class ForecastInputs:
    now_utc: dt.datetime
    week_start_at: dt.datetime
    week_end_at: dt.datetime
    elapsed_hours: float
    elapsed_fraction: float
    remaining_hours: float
    remaining_days: float
    # Current state
    p_now: float
    five_hour_percent: float | None
    spent_usd: float
    snapshot_count: int
    latest_snapshot_at: dt.datetime
    # Rate inputs
    p_24h_ago: float | None
    t_24h_actual_hours: float | None
    # $/1% selection
    dollars_per_percent: float
    dollars_per_percent_source: str  # "this_week" | "trailing_4wk_median" | "this_week_sparse"
    # Confidence
    confidence: str  # "high" | "low"
    low_confidence_reasons: list[str]


@dataclass
class BudgetRow:
    target_percent: int
    pct_headroom: float | None     # None when already past target
    dollars_per_day: float | None
    percent_per_day: float | None


@dataclass
class ForecastOutput:
    inputs: ForecastInputs
    r_avg: float                   # pct per hour, week-avg
    r_recent: float | None         # pct per hour, 24h recent; None if no prior sample
    final_percent_low: float
    final_percent_high: float
    week_avg_projection_pct: float  # p_now + r_avg*remaining (smooth estimator)
    projected_cap: bool
    already_capped: bool
    cap_at: dt.datetime | None
    budgets: list[BudgetRow]


def _compute_forecast(inputs: ForecastInputs, targets: list[int]) -> ForecastOutput:
    """Implements spec §2. targets are sorted desc for stable output (100, 90, …)."""
    # Rate methods
    r_avg = inputs.p_now / inputs.elapsed_hours if inputs.elapsed_hours > 0 else 0.0
    if inputs.p_24h_ago is not None and inputs.t_24h_actual_hours:
        r_recent: float | None = max(
            0.0, (inputs.p_now - inputs.p_24h_ago) / inputs.t_24h_actual_hours
        )
    else:
        r_recent = None

    # Projected final % — routed through the shared project_linear primitive
    # (spec F1). r_recent is None ⇒ collapse to the average projection.
    if r_recent is None:
        final_low, final_high = project_linear(
            inputs.p_now, inputs.remaining_hours, r_avg, r_avg
        )
    else:
        a, b = project_linear(
            inputs.p_now, inputs.remaining_hours, r_avg, r_recent
        )
        final_low, final_high = min(a, b), max(a, b)

    # Smooth week-average projection (additive surface field). Distinct from
    # the displayed band (which keys off final_high): this is the conservative
    # week-average value the projected-pace alert axis fires on.
    # p_now + r_avg*remaining (== project_linear collapsed to the single rate).
    week_avg_projection_pct = inputs.p_now + r_avg * inputs.remaining_hours

    already_capped = inputs.p_now >= 100.0
    projected_cap = already_capped or final_high >= 100.0

    cap_at: dt.datetime | None = None
    if not already_capped and projected_cap:
        r_pessimistic = max(r_avg, r_recent or 0.0)
        if r_pessimistic > 0:
            hours_to_cap = (100.0 - inputs.p_now) / r_pessimistic
            if hours_to_cap < inputs.remaining_hours:
                cap_at = inputs.now_utc + dt.timedelta(hours=hours_to_cap)

    # Budgets
    budgets: list[BudgetRow] = []
    if not already_capped:
        for t in sorted(targets, reverse=True):
            headroom = t - inputs.p_now
            if headroom <= 0 or inputs.remaining_days <= 0:
                budgets.append(BudgetRow(target_percent=t, pct_headroom=None,
                                         dollars_per_day=None, percent_per_day=None))
                continue
            dollars_day = (headroom * inputs.dollars_per_percent) / inputs.remaining_days
            pct_day = headroom / inputs.remaining_days
            budgets.append(BudgetRow(
                target_percent=t,
                pct_headroom=headroom,
                dollars_per_day=dollars_day,
                percent_per_day=pct_day,
            ))

    return ForecastOutput(
        inputs=inputs,
        r_avg=r_avg,
        r_recent=r_recent,
        final_percent_low=final_low,
        final_percent_high=final_high,
        week_avg_projection_pct=week_avg_projection_pct,
        projected_cap=projected_cap,
        already_capped=already_capped,
        cap_at=cap_at,
        budgets=budgets,
    )
