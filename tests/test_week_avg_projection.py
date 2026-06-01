"""Forecast kernel: additive week_avg_projection_pct field (Task 2).

The budget counterpart (week_avg_projection_usd) is covered in
tests/test_budget.py. This module pins the forecast week-average projection
the projected-pace alert axis will fire on:

    week_avg_projection_pct = p_now + r_avg * remaining_hours
    where r_avg = p_now / elapsed_hours   (forecast's week-average rate)

_compute_forecast resolves project_linear via _cctally() -> sys.modules
["cctally"], so the test loads the full bin/cctally module (the canonical
isolated loader) rather than the sibling in isolation.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from conftest import load_isolated_cctally_module

REPO_ROOT = Path(__file__).resolve().parent.parent
UTC = dt.timezone.utc


@pytest.fixture
def cctally_mod(tmp_path, monkeypatch):
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _mk_inputs(mod, *, p_now, elapsed_hours, remaining_hours):
    ws = dt.datetime(2026, 5, 18, 0, 0, tzinfo=UTC)
    now = ws + dt.timedelta(hours=elapsed_hours)
    we = now + dt.timedelta(hours=remaining_hours)
    total = elapsed_hours + remaining_hours
    return mod.ForecastInputs(
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
        p_24h_ago=None,
        t_24h_actual_hours=None,
        dollars_per_percent=1.0,
        dollars_per_percent_source="this_week",
        confidence="high",
        low_confidence_reasons=[],
    )


def test_forecast_output_exposes_week_average_projection_pct(cctally_mod):
    mod = cctally_mod
    inp = _mk_inputs(mod, p_now=50.0, elapsed_hours=84.0, remaining_hours=84.0)
    out = mod._compute_forecast(inp, targets=[100, 90])
    # r_avg = 50/84; proj = 50 + (50/84)*84 = 100.
    assert abs(out.week_avg_projection_pct - 100.0) < 1e-9


def test_forecast_week_avg_projection_zero_elapsed_collapses_to_p_now(cctally_mod):
    mod = cctally_mod
    inp = _mk_inputs(mod, p_now=33.0, elapsed_hours=0.0, remaining_hours=84.0)
    out = mod._compute_forecast(inp, targets=[100, 90])
    # elapsed 0 => r_avg 0 => projection == p_now.
    assert abs(out.week_avg_projection_pct - 33.0) < 1e-9


def test_forecast_json_carries_week_avg_projection_pct(cctally_mod):
    mod = cctally_mod
    inp = _mk_inputs(mod, p_now=50.0, elapsed_hours=84.0, remaining_hours=84.0)
    out = mod._compute_forecast(inp, targets=[100, 90])
    payload = mod._build_forecast_json_payload(out)
    assert "week_avg_projection_pct" in payload["forecast"]
    assert abs(payload["forecast"]["week_avg_projection_pct"] - 100.0) < 1e-3
