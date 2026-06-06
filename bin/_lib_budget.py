"""Pure-function kernel for `cctally budget` (no I/O — every dep injected).

Mirrors the _lib_statusline.py / _lib_doctor.py / _lib_pricing_check.py
pattern. Re-exported on the cctally module. See
docs/superpowers/specs/2026-05-29-cctally-budget-design.md §3.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

# Early in the week / no data → projections are unreliable; annotate LOW CONF
# (mirrors forecast's thin-data caution). Tunable single source of truth.
_BUDGET_LOW_CONF_ELAPSED_FRACTION = 0.15
# Fallback warn fraction when alert_thresholds is empty (alerts silenced) but
# we still render a verdict.
_BUDGET_DEFAULT_WARN_FRACTION = 0.90


def project_linear(
    current: float,
    remaining: float,
    rate_low: float,
    rate_high: float,
) -> tuple[float, float]:
    """Project ``current + rate * remaining`` for a (low, high) rate band.

    Pure; unit-agnostic — percent for forecast, dollars for budget. The caller
    is responsible for passing ``rate_low <= rate_high`` if it wants ordered
    output; this primitive does NOT sort (forecast sorts the outputs to stay a
    byte-exact no-op vs its goldens; budget passes a pre-ordered band).
    """
    return (current + rate_low * remaining, current + rate_high * remaining)


def calendar_month_window(
    now: dt.datetime, tz: dt.tzinfo
) -> tuple[dt.datetime, dt.datetime]:
    """Civil month window in ``tz``, returned as UTC-normalized instants.

    Pure; no I/O. ``now`` is a tz-aware datetime and ``tz`` a tzinfo. Returns
    ``(start_utc, end_utc)`` where ``start`` = the 1st of ``now``'s civil month
    at 00:00 local and ``end`` = the 1st of the *next* month at 00:00 local
    (civil rollover via ``(year, month + 1)`` with year carry — NEVER a fixed
    ``timedelta(days=30)``, so 28/29/30/31-day months and Dec→Jan are exact),
    both converted to UTC so the kernel's elapsed-seconds math stays single-tz.
    """
    local = now.astimezone(tz)
    start_local = local.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    if start_local.month == 12:
        end_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        end_local = start_local.replace(month=start_local.month + 1)
    return (
        start_local.astimezone(dt.timezone.utc),
        end_local.astimezone(dt.timezone.utc),
    )


def calendar_week_window(
    now: dt.datetime, tz: dt.tzinfo, week_start_idx: int
) -> tuple[dt.datetime, dt.datetime]:
    """Civil week window in ``tz`` anchored on ``week_start_idx`` (Mon=0..Sun=6),
    returned as UTC-normalized instants.

    Pure; no I/O. Snaps ``now``'s local date back to the most recent
    ``week_start_idx`` weekday at 00:00 local via ``(weekday − start_idx) % 7``,
    then adds the 7-day delta to the *aware local* start so a DST week is a true
    167h/169h span before normalizing both ends to UTC.
    """
    local = now.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    diff = (midnight.weekday() - week_start_idx) % 7
    start_local = midnight - dt.timedelta(days=diff)
    end_local = start_local + dt.timedelta(days=7)
    return (
        start_local.astimezone(dt.timezone.utc),
        end_local.astimezone(dt.timezone.utc),
    )


@dataclass(frozen=True)
class BudgetInputs:
    target_usd: float
    spent_usd: float            # cumulative equiv-$ this subscription week
    recent_24h_usd: float       # trailing-24h equiv-$ (recent-rate projection)
    week_start_at: dt.datetime  # effective (post-reset) week start, tz-aware
    week_end_at: dt.datetime    # tz-aware
    now: dt.datetime            # tz-aware
    alert_thresholds: tuple[int, ...]


@dataclass(frozen=True)
class BudgetStatus:
    spent_usd: float
    remaining_usd: float                # target - spent (may be < 0)
    consumption_pct: float              # spent / target * 100 (monotonic key)
    elapsed_fraction: float             # [0, 1]
    projected_eow_low_usd: float
    projected_eow_high_usd: float
    week_avg_projection_usd: float      # spent + rate_avg*remaining (smooth estimator)
    verdict: str                        # "ok" | "warn" | "over"
    daily_budget_remaining_usd: float   # remaining / remaining-days
    daily_pace_usd: float               # current burn $/day (week-average)
    low_confidence: bool
    crossed_thresholds: tuple[int, ...]


def compute_budget_status(inputs: BudgetInputs) -> BudgetStatus:
    """Compute budget status from injected inputs. Pure; deterministic."""
    target = float(inputs.target_usd)
    spent = float(inputs.spent_usd)

    total_seconds = (inputs.week_end_at - inputs.week_start_at).total_seconds()
    elapsed_seconds = (inputs.now - inputs.week_start_at).total_seconds()
    # Clamp elapsed into [0, total] so a now before/after the window stays sane.
    if total_seconds <= 0:
        elapsed_seconds = 0.0
        elapsed_fraction = 0.0
    else:
        elapsed_seconds = max(0.0, min(elapsed_seconds, total_seconds))
        elapsed_fraction = elapsed_seconds / total_seconds
    remaining_seconds = max(0.0, total_seconds - elapsed_seconds)

    elapsed_hours = elapsed_seconds / 3600.0
    remaining_hours = remaining_seconds / 3600.0
    remaining_days = remaining_hours / 24.0

    consumption_pct = (spent / target * 100.0) if target > 0 else 0.0
    remaining_usd = target - spent

    # Dollar rates ($/hour). Week-average from spend-so-far; recent from
    # trailing-24h spend. Ordered band low<=high for project_linear.
    rate_avg = (spent / elapsed_hours) if elapsed_hours > 0 else 0.0
    rate_recent = float(inputs.recent_24h_usd) / 24.0
    rate_low = min(rate_avg, rate_recent)
    rate_high = max(rate_avg, rate_recent)

    projected_low, projected_high = project_linear(
        spent, remaining_hours, rate_low, rate_high
    )

    # Smooth week-average end-of-week projection (additive surface field).
    # Distinct from the displayed band (which keys off the high end): this is
    # the conservative week-average value the projected-pace alert axis fires
    # on. spent + rate_avg*remaining (== project_linear collapsed to one rate).
    week_avg_projection_usd = spent + rate_avg * remaining_hours

    daily_pace_usd = rate_avg * 24.0
    daily_budget_remaining_usd = (
        (remaining_usd / remaining_days) if remaining_days > 0 else remaining_usd
    )

    thresholds = tuple(sorted(set(int(t) for t in inputs.alert_thresholds)))
    if thresholds:
        warn_fraction = min(thresholds) / 100.0
    else:
        warn_fraction = _BUDGET_DEFAULT_WARN_FRACTION

    projected = max(projected_low, projected_high)
    if spent > target or projected > target:
        verdict = "over"
    elif projected >= warn_fraction * target:
        verdict = "warn"
    else:
        verdict = "ok"

    low_confidence = (
        elapsed_fraction < _BUDGET_LOW_CONF_ELAPSED_FRACTION or spent <= 0.0
    )

    crossed = tuple(t for t in thresholds if consumption_pct + 1e-9 >= t)

    return BudgetStatus(
        spent_usd=spent,
        remaining_usd=remaining_usd,
        consumption_pct=consumption_pct,
        elapsed_fraction=elapsed_fraction,
        projected_eow_low_usd=projected_low,
        projected_eow_high_usd=projected_high,
        week_avg_projection_usd=week_avg_projection_usd,
        verdict=verdict,
        daily_budget_remaining_usd=daily_budget_remaining_usd,
        daily_pace_usd=daily_pace_usd,
        low_confidence=low_confidence,
        crossed_thresholds=crossed,
    )
