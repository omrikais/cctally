"""Direct unit tests for bin/_lib_forecast.py (#279 S4 F2).

The forecast kernel (`ForecastInputs`, `BudgetRow`, `ForecastOutput`,
`_compute_forecast`) moved verbatim out of `bin/_cctally_forecast.py` into
the pure `bin/_lib_forecast.py` home, with the projection routed through the
honest-imported `project_linear` (its real, pure home in `_lib_budget`)
instead of the `c.project_linear` accessor. These tests exercise the kernel
by importing it DIRECTLY, and the final test proves the re-export continuity
trick: `cctally._compute_forecast IS _lib_forecast._compute_forecast`.

`_mk_inputs` copies tests/test_week_avg_projection.py:32-56 verbatim (same
ForecastInputs field set) so the direct-import surface provably matches the
namespace surface.
"""
from __future__ import annotations

import datetime as dt

# conftest puts bin/ on sys.path.
import _lib_forecast
from _lib_forecast import ForecastInputs, ForecastOutput, _compute_forecast
from conftest import load_script


UTC = dt.timezone.utc


def _mk_inputs(*, p_now, elapsed_hours, remaining_hours,
               p_24h_ago=None, t_24h_actual_hours=None):
    ws = dt.datetime(2026, 5, 18, 0, 0, tzinfo=UTC)
    now = ws + dt.timedelta(hours=elapsed_hours)
    we = now + dt.timedelta(hours=remaining_hours)
    total = elapsed_hours + remaining_hours
    return ForecastInputs(
        now_utc=now,
        week_start_at=ws,
        week_end_at=we,
        elapsed_hours=elapsed_hours,
        elapsed_fraction=(elapsed_hours / total if total else 0.0),
        remaining_hours=remaining_hours,
        remaining_days=remaining_hours / 24.0,
        p_now=p_now,
        five_hour_percent=None,
        spent_usd=10.0,
        snapshot_count=5,
        latest_snapshot_at=now,
        p_24h_ago=p_24h_ago,
        t_24h_actual_hours=t_24h_actual_hours,
        dollars_per_percent=1.0,
        dollars_per_percent_source="this_week",
        confidence="high",
        low_confidence_reasons=[],
    )


def test_compute_forecast_returns_output():
    out = _compute_forecast(_mk_inputs(p_now=50.0, elapsed_hours=84.0,
                                       remaining_hours=84.0), [100, 90])
    assert isinstance(out, ForecastOutput)


def test_compute_forecast_band_ordered():
    # With a recent rate distinct from the average, low <= high must hold
    # (the kernel min/max-sorts the two project_linear results).
    out = _compute_forecast(
        _mk_inputs(p_now=50.0, elapsed_hours=84.0, remaining_hours=84.0,
                   p_24h_ago=10.0, t_24h_actual_hours=24.0),
        [100, 90],
    )
    assert out.final_percent_low <= out.final_percent_high


def test_compute_forecast_week_avg_projection_matches_manual():
    # r_avg = 50/84; proj = 50 + (50/84)*84 = 100. Mirrors
    # tests/test_week_avg_projection.py through the direct import.
    out = _compute_forecast(_mk_inputs(p_now=50.0, elapsed_hours=84.0,
                                       remaining_hours=84.0), [100, 90])
    assert abs(out.week_avg_projection_pct - 100.0) < 1e-9


def test_compute_forecast_zero_elapsed_guard():
    # elapsed 0 => r_avg 0 => projection collapses to p_now (no ZeroDivision).
    out = _compute_forecast(_mk_inputs(p_now=33.0, elapsed_hours=0.0,
                                       remaining_hours=84.0), [100, 90])
    assert out.r_avg == 0.0
    assert abs(out.week_avg_projection_pct - 33.0) < 1e-9


def test_compute_forecast_targets_produce_budget_rows():
    out = _compute_forecast(_mk_inputs(p_now=20.0, elapsed_hours=84.0,
                                       remaining_hours=84.0), [100, 90])
    # Not already-capped -> a BudgetRow per target, sorted desc.
    assert [b.target_percent for b in out.budgets] == [100, 90]


def test_compute_forecast_already_capped_has_no_budgets():
    out = _compute_forecast(_mk_inputs(p_now=100.0, elapsed_hours=84.0,
                                       remaining_hours=84.0), [100, 90])
    assert out.already_capped is True
    assert out.budgets == []


def test_compute_forecast_identity_via_namespace():
    ns = load_script()
    assert ns["_compute_forecast"] is _lib_forecast._compute_forecast
    assert ns["ForecastInputs"] is _lib_forecast.ForecastInputs
    assert ns["ForecastOutput"] is _lib_forecast.ForecastOutput
    assert ns["BudgetRow"] is _lib_forecast.BudgetRow
