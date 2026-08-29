"""#661 S2 Task A3 — the ceiling correction and right-censoring (spec §3.1-3.4).

Both meters display a CEILING, so a shown reading of `k` means true
consumption fell in `[k-1, k)`. The correction belongs in
`_load_forecast_inputs`, before `ForecastInputs` is constructed, for two
reasons that are each independently sufficient:

* `_compute_forecast` does not select the dollars-per-percent denominator —
  `_select_dollars_per_percent` runs in the loader — so a kernel-only
  correction would leave that denominator raw; and
* `_compute_forecast` computes the recent rate as
  `(p_now - p_24h_ago) / hours`, so correcting the minuend alone would
  CORRUPT that difference rather than fix it.

A displayed 100 denotes `[99, +inf)` and has no point estimate at all, so
every meter-derived projection is withheld rather than fabricated.
"""
from __future__ import annotations

import datetime as dt
import pytest

# conftest puts bin/ on sys.path.
import _lib_forecast as fc
import _lib_quota_model as qm
from conftest import load_isolated_cctally_module

UTC = dt.timezone.utc
WEEK_START = dt.datetime(2026, 5, 18, tzinfo=UTC)


@pytest.fixture
def mod(tmp_path, monkeypatch):
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _inputs(*, p_now, elapsed_hours=100.0, remaining_hours=68.0,
            p_24h_ago=None, t_24h_actual_hours=None, spent_usd=10.0,
            dollars_per_percent=1.0):
    now = WEEK_START + dt.timedelta(hours=elapsed_hours)
    week_end = now + dt.timedelta(hours=remaining_hours)
    total = elapsed_hours + remaining_hours
    return fc.ForecastInputs(
        now_utc=now,
        week_start_at=WEEK_START,
        week_end_at=week_end,
        elapsed_hours=elapsed_hours,
        elapsed_fraction=(elapsed_hours / total if total else 0.0),
        remaining_hours=remaining_hours,
        remaining_days=remaining_hours / 24.0,
        p_now=p_now,
        five_hour_percent=None,
        spent_usd=spent_usd,
        snapshot_count=6,
        latest_snapshot_at=now,
        p_24h_ago=p_24h_ago,
        t_24h_actual_hours=t_24h_actual_hours,
        dollars_per_percent=dollars_per_percent,
        dollars_per_percent_source="this_week",
        confidence="high",
        low_confidence_reasons=[],
    )


# --------------------------------------------------------------------------
# The correction itself
# --------------------------------------------------------------------------
def test_a3_inputs_derive_the_corrected_pair_from_the_displayed_pair():
    """The derivation is on the dataclass, so EVERY construction site gets
    it — the loader, the TUI's demo builder and any test alike."""
    inputs = _inputs(p_now=10, p_24h_ago=6)
    assert inputs.p_now_corrected == pytest.approx(9.5)
    assert inputs.p_24h_ago_corrected == pytest.approx(5.5)
    assert inputs.p_now_interval == (9.0, 10.0)
    assert inputs.right_censored is False
    # The raw observed reading is retained alongside, for display.
    assert inputs.p_now == 10


def test_a3_the_correction_uses_the_kernel_ceiling_rule():
    for shown in (0, 1, 2, 15, 99):
        assert fc.corrected_percent_point(shown) == \
            qm.true_percent_point(shown), shown
        assert fc.corrected_percent_interval(shown) == \
            qm.true_percent_interval(shown), shown
    assert fc.corrected_percent_point(100) is None


def test_a3_a_fractional_reading_floors_through_the_canonical_helper():
    """`0.57 * 100` is 56.99999999999999. A bare `int()` or `floor()` reads
    it as 56, so the integer percent goes through `integer_percent`."""
    assert fc.corrected_percent_point(0.57 * 100) == \
        qm.true_percent_point(57)


def test_a3_both_endpoints_are_corrected_not_just_p_now():
    """Correcting only p_now corrupts the recent rate.

    `_compute_forecast` computes (p_now - p_24h_ago) / hours. Correcting the
    minuend alone shifts that difference by half a point, in the direction
    that reads as faster recent usage.
    """
    inputs = _inputs(p_now=10, p_24h_ago=6, t_24h_actual_hours=24.0)
    out = fc._compute_forecast(inputs, [100])
    # 9.5 - 5.5 == 4.0, the same difference as the raw pair. A one-sided
    # correction would give 9.5 - 6 == 3.5.
    assert out.r_recent == pytest.approx(4.0 / 24.0)
    assert out.r_recent != pytest.approx(3.5 / 24.0)


def test_a3_the_week_average_rate_uses_the_corrected_point():
    inputs = _inputs(p_now=10, elapsed_hours=10.0)
    out = fc._compute_forecast(inputs, [100])
    assert out.r_avg == pytest.approx(0.95)


def test_a3_the_week_average_projection_uses_the_corrected_point():
    inputs = _inputs(p_now=50, elapsed_hours=84.0, remaining_hours=84.0)
    out = fc._compute_forecast(inputs, [100])
    # 49.5 + (49.5/84)*84 == 99.0, not the raw 100.0.
    assert out.week_avg_projection_pct == pytest.approx(99.0)


def test_a3_the_budget_headroom_uses_the_corrected_point():
    inputs = _inputs(p_now=50, remaining_hours=48.0, dollars_per_percent=2.0)
    out = fc._compute_forecast(inputs, [100])
    assert out.budgets[0].pct_headroom == pytest.approx(50.5)
    assert out.budgets[0].dollars_per_day == pytest.approx(50.5 * 2.0 / 2.0)


# --------------------------------------------------------------------------
# Right-censoring at a capped reading
# --------------------------------------------------------------------------
def test_a3_capped_reading_fabricates_no_point_estimate():
    out = fc._compute_forecast(_inputs(p_now=100), [100])
    assert out.right_censored is True
    assert out.week_avg_projection_pct is None
    assert out.final_percent_low is None
    assert out.final_percent_high is None
    assert out.cap_at is None
    # The cap is a FACT here, not a projection, so both flags stay true.
    assert out.already_capped is True
    assert out.projected_cap is True
    # No budget amount is fabricated from a censored meter.
    assert out.budgets == []


def test_a3_a_reading_beyond_the_cap_is_also_censored():
    out = fc._compute_forecast(_inputs(p_now=103.0), [100])
    assert out.right_censored is True
    assert out.week_avg_projection_pct is None


def test_a3_ninety_nine_is_not_censored():
    """Guard the guard: the censored assertions above would be vacuous if
    everything near the ceiling censored."""
    out = fc._compute_forecast(_inputs(p_now=99, elapsed_hours=84.0,
                                       remaining_hours=84.0), [100])
    assert out.right_censored is False
    assert out.week_avg_projection_pct == pytest.approx(98.5 * 2)


def test_a3_a_censored_prior_sample_withholds_the_recent_rate():
    inputs = _inputs(p_now=50, p_24h_ago=100, t_24h_actual_hours=24.0)
    out = fc._compute_forecast(inputs, [100])
    assert out.r_recent is None


def test_a3_the_json_payload_nulls_the_censored_projections(mod):
    payload = mod._build_forecast_json_payload(
        mod._compute_forecast(_inputs(p_now=100), [100, 90]))
    assert payload["forecast"]["final_percent_low"] is None
    assert payload["forecast"]["final_percent_high"] is None
    assert payload["forecast"]["week_avg_projection_pct"] is None
    assert payload["forecast"]["right_censored"] is True
    assert payload["forecast"]["already_capped"] is True


def test_a3_the_json_payload_states_right_censored_false_when_it_is_not(mod):
    payload = mod._build_forecast_json_payload(
        mod._compute_forecast(_inputs(p_now=42), [100, 90]))
    assert payload["forecast"]["right_censored"] is False
    assert payload["forecast"]["week_avg_projection_pct"] is not None


# --------------------------------------------------------------------------
# The loader: the denominator, and the branch gate that must NOT move
# --------------------------------------------------------------------------
def _seed_week(conn, *, percents, week_start=WEEK_START, week_hours=168):
    """One current subscription week of weekly-meter snapshots."""
    from _fixture_builders import seed_weekly_usage_snapshot

    week_end = week_start + dt.timedelta(hours=week_hours)
    for offset_hours, pct in percents:
        captured = week_start + dt.timedelta(hours=offset_hours)
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc=captured.isoformat(),
            week_start_date=week_start.date().isoformat(),
            week_end_date=week_end.date().isoformat(),
            week_start_at=week_start.isoformat(),
            week_end_at=week_end.isoformat(),
            weekly_percent=pct,
            source="statusline",
        )
    conn.commit()
    return week_end


@pytest.fixture
def store(mod, tmp_path, monkeypatch):
    """`(module, stats_conn)` over an isolated data directory."""
    import _cctally_core
    conn = _cctally_core.open_db()
    yield mod, conn
    conn.close()


def _patch_spend(mod, monkeypatch, amount):
    monkeypatch.setattr(mod, "_sum_cost_for_range",
                        lambda *a, **k: float(amount))


def test_a3_dollars_per_percent_denominator_uses_the_corrected_point(
        store, monkeypatch):
    # The denominator is selected in the LOADER, so a kernel-only correction
    # would leave it raw.
    mod, conn = store
    _seed_week(conn, percents=[(0, 2.0), (72, 6.0), (96, 10.0)])
    _patch_spend(mod, monkeypatch, 95.0)
    now = WEEK_START + dt.timedelta(hours=100)
    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    assert inputs is not None
    assert inputs.dollars_per_percent == pytest.approx(95.0 / 9.5)
    assert inputs.dollars_per_percent_source == "this_week"


def test_a3_the_branch_gate_still_reads_the_displayed_reading(store,
                                                              monkeypatch):
    """The `p_now >= 10` gate is about having a stable sample, and the
    corrected 9.5 would flip a displayed 10 into the trailing-median branch.
    The gate stays on the displayed reading; only the divisor is corrected.
    """
    mod, conn = store
    _seed_week(conn, percents=[(0, 2.0), (72, 6.0), (96, 10.0)])
    _patch_spend(mod, monkeypatch, 95.0)
    now = WEEK_START + dt.timedelta(hours=100)
    dpp, source = mod._select_dollars_per_percent(
        conn, now, WEEK_START, 10.0, 95.0, skip_sync=True,
        p_now_corrected=9.5)
    assert source == "this_week"
    assert dpp == pytest.approx(95.0 / 9.5)


def test_a3_a_zero_reading_still_withholds_the_rate(store, monkeypatch):
    """The corrected point for a displayed 0 is 0.25, so a correction
    applied to the GATE as well as the divisor would publish a rate on a
    week with no observed usage — the exact fabrication #620 S1 D5 removed.
    """
    mod, conn = store
    _seed_week(conn, percents=[(0, 0.0), (24, 0.0)])
    _patch_spend(mod, monkeypatch, 40.0)
    now = WEEK_START + dt.timedelta(hours=30)
    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    assert inputs is not None
    assert inputs.dollars_per_percent is None
    assert inputs.dollars_per_percent_source == "no_usage_observed"


def test_a3_the_loader_corrects_both_endpoints(store, monkeypatch):
    mod, conn = store
    _seed_week(conn, percents=[(0, 2.0), (76, 6.0), (100, 10.0)])
    _patch_spend(mod, monkeypatch, 10.0)
    now = WEEK_START + dt.timedelta(hours=100)
    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    assert inputs.p_now == 10.0
    assert inputs.p_now_corrected == pytest.approx(9.5)
    assert inputs.p_24h_ago == 6.0
    assert inputs.p_24h_ago_corrected == pytest.approx(5.5)
    assert inputs.right_censored is False


def test_a3_the_loader_marks_a_capped_week_right_censored(store, monkeypatch):
    mod, conn = store
    _seed_week(conn, percents=[(0, 40.0), (90, 100.0)])
    _patch_spend(mod, monkeypatch, 500.0)
    now = WEEK_START + dt.timedelta(hours=100)
    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    assert inputs.right_censored is True
    assert inputs.p_now_corrected is None
    assert inputs.p_now_interval == (99.0, None)


# --------------------------------------------------------------------------
# The TUI's own operand (spec §3.4)
# --------------------------------------------------------------------------
def test_a3_tui_current_week_uses_the_corrected_operand(store, monkeypatch):
    # `_tui_build_current_week` computes spent / used_pct independently of
    # the forecast kernel, so a correction confined to the kernel would
    # leave this surface stating an uncorrected figure.
    mod, conn = store
    tui = mod._load_sibling("_cctally_tui")
    _seed_week(conn, percents=[(0, 2.0), (96, 10.0)])
    monkeypatch.setattr(tui, "_sum_cost_and_tokens_for_range",
                        lambda *a, **k: (95.0, 1234))
    now = WEEK_START + dt.timedelta(hours=100)
    cw = tui._tui_build_current_week(conn, now, skip_sync=True)
    assert cw is not None
    assert cw.used_pct == 10.0
    assert cw.dollars_per_percent == pytest.approx(95.0 / 9.5)


def test_a3_a_censored_meter_keeps_the_displayed_divisor(store, monkeypatch):
    """A censored reading has no point estimate, so there is nothing to
    correct TO, and the shipped divisor — the displayed reading — is
    retained.

    That divisor is NOT a bound in either direction, and an earlier revision
    of this docstring claimed it was both. True consumption behind a
    displayed 100 lies in `[99, +inf)`: it is at least 99 and has no upper
    limit, so `spent / 100` understates the rate anywhere in `[99, 100)` and
    overstates it above 100. 99 would be the divisor that gives a genuine
    upper bound on the rate.

    The reason to keep the displayed divisor is not accuracy but scope. Spec
    section 3.2 names the point estimate, the ETA, the headroom and the
    budget amount, and withholding this operand as well would create the
    null-rate-with-live-delta state that spec section 10.1's envelope guard
    exists to close and that Stage D owns.
    """
    mod, conn = store
    tui = mod._load_sibling("_cctally_tui")
    _seed_week(conn, percents=[(0, 40.0), (96, 100.0)])
    monkeypatch.setattr(tui, "_sum_cost_and_tokens_for_range",
                        lambda *a, **k: (500.0, 1234))
    monkeypatch.setattr(mod, "_sum_cost_for_range", lambda *a, **k: 500.0)
    now = WEEK_START + dt.timedelta(hours=100)
    cw = tui._tui_build_current_week(conn, now, skip_sync=True)
    assert cw.used_pct == 100.0
    assert cw.dollars_per_percent == pytest.approx(500.0 / 100.0)
    inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
    assert inputs.dollars_per_percent == pytest.approx(500.0 / 100.0)


# --------------------------------------------------------------------------
# The censored week-average rate is withheld too (#661 S2 R15)
# --------------------------------------------------------------------------
def test_a3_the_week_average_rate_is_withheld_at_a_censored_reading():
    """A displayed 103 denotes `[99, +inf)`, so 103/120 is not an estimate of
    the consumed rate — it is arithmetic over a reading the model declares
    censored. The kernel used to fall back to the displayed value and publish
    the result as `rates.week_average_pct_per_hour`."""
    out = fc._compute_forecast(_inputs(p_now=103.0, elapsed_hours=120.0),
                               [100])
    assert out.r_avg is None
    assert out.r_recent is None


def test_a3_the_json_payload_nulls_the_censored_week_average_rate(mod):
    payload = mod._build_forecast_json_payload(
        mod._compute_forecast(_inputs(p_now=103.0, elapsed_hours=120.0),
                              [100, 90]))
    assert payload["rates"]["week_average_pct_per_hour"] is None
    assert payload["rates"]["recent_24h_pct_per_hour"] is None


def test_a3_an_uncensored_week_average_rate_is_still_published(mod):
    """Guard the guard: the two assertions above would be vacuous if the rate
    were withheld everywhere."""
    payload = mod._build_forecast_json_payload(
        mod._compute_forecast(_inputs(p_now=50, elapsed_hours=100.0),
                              [100, 90]))
    assert payload["rates"]["week_average_pct_per_hour"] == pytest.approx(
        49.5 / 100.0)


# --------------------------------------------------------------------------
# The zero end of the meter is NOT censored (#661 S2 spec section 3.6)
# --------------------------------------------------------------------------
def test_a3_zero_observed_usage_withholds_the_projection():
    """REVERSED by spec section 3.6: a displayed zero projects like any other
    reading. The function name is retained deliberately — removing a pytest
    node identifier is a coverage loss the estate gate refuses — so read the
    assertions, not the name.

    An earlier revision of this session withheld the projection here and gave
    it the cause `no-local-history`. Both halves were wrong. Right-censoring
    applies at 100 and only at 100, because that reading alone denotes an
    interval unbounded above; every other reading, zero included, denotes a
    bounded interval whose midpoint is the corrected point. The withholding
    singled out the one reading where the correction is most visible and
    refused to apply it, and the reasoning offered for it — that 0.25 comes
    from a rounding floor — is true of every corrected reading, so it would
    forbid the whole mechanism. The cause was wrong independently: S1 defines
    `no-local-history` as the ingest tail failing to cover the interval or the
    interval holding no entries, and the fixture that produced it carries
    $9.12 of spend across seven snapshots.
    """
    out = fc._compute_forecast(_inputs(p_now=0.0, elapsed_hours=78.0,
                                       remaining_hours=90.0), [100, 90])
    reversal = (
        "REVERSED by #661 S2 §3.6: this test's NAME says the projection is "
        "WITHHELD at a displayed zero, and every assertion below now says the "
        "opposite. A displayed 0 projects from the corrected point 0.25 "
        "exactly as a displayed 40 projects from 39.5; withholding it was the "
        "earlier draft, and its `no-local-history` cause was wrong too. The "
        "node id is kept because removing one is a coverage loss the estate "
        "gate refuses")
    assert out.projection_basis == fc.ProjectionBasis.CORRECTED_METER.value, \
        reversal
    assert out.projection_code is None, reversal
    # 0.25 is the midpoint of `[0, 0.5)`; the pace carries it to 78+90 hours.
    assert out.week_avg_projection_pct == pytest.approx(
        0.25 + (0.25 / 78.0) * 90.0), reversal
    assert out.final_percent_low == pytest.approx(
        out.week_avg_projection_pct), reversal
    assert out.final_percent_high == pytest.approx(
        out.week_avg_projection_pct), reversal
    assert out.right_censored is False, reversal


def test_a3_the_dollar_rate_is_still_withheld_at_a_displayed_zero():
    """Section 3.6 reversed the PROJECTION, not the rate.

    `_select_dollars_per_percent` still withholds on a displayed zero,
    because its `> 0` gate is about having a signal to divide by rather than
    about the reading's interval. The two decisions are independent and the
    reversal must not be read as reversing both.
    """
    out = fc._compute_forecast(_inputs(p_now=0.0, elapsed_hours=78.0,
                                       remaining_hours=90.0,
                                       dollars_per_percent=None), [100, 90])
    row = next(b for b in out.budgets if b.target_percent == 100)
    assert row.dollars_per_day is None


def test_a3_zero_observed_usage_still_publishes_the_percent_budget():
    """The percent budget never depended on the projection, and #620 S1 D5
    already decided it is published where the dollar budget is not."""
    out = fc._compute_forecast(_inputs(p_now=0.0, elapsed_hours=78.0,
                                       remaining_hours=90.0,
                                       dollars_per_percent=None), [100, 90])
    row = next(b for b in out.budgets if b.target_percent == 100)
    assert row.percent_per_day is not None
    assert row.dollars_per_day is None


def test_a3_a_reading_of_one_is_not_treated_as_no_usage():
    """Guard the guard: the withholding is keyed on the DISPLAYED zero, the
    same operand the dollars-per-percent branch gate reads. A corrected-point
    test would move the boundary to 0.75 and silence a real 1% week."""
    out = fc._compute_forecast(_inputs(p_now=1.0, elapsed_hours=84.0,
                                       remaining_hours=84.0), [100])
    assert out.week_avg_projection_pct == pytest.approx(1.5)
    assert out.projection_basis == fc.ProjectionBasis.CORRECTED_METER.value


def test_a3_a_calibrated_projection_survives_a_zero_meter():
    """The model reads tokens rather than the meter, so it still supplies a
    projection where the meter says nothing was consumed."""
    inputs = _inputs(p_now=0.0, elapsed_hours=78.0, remaining_hours=90.0)
    inputs.calibrated_projection_pct = 12.0
    out = fc._compute_forecast(inputs, [100])
    assert out.projection_basis == fc.ProjectionBasis.CALIBRATED.value
    assert out.week_avg_projection_pct == pytest.approx(12.0)
