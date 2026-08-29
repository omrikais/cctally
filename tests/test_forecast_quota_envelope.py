"""#661 S2 Task D3 — `forecast.quota` and the hero's coupled header operands.

Spec sections 10 and 10.1.

Two independent subjects share this module because they share one envelope
build: the additive typed `forecast.quota` object, and the guard that stops
the hero publishing a week-over-week comparison on a screen stating no
`$ / 1%` at all.
"""
from __future__ import annotations

import datetime as dt
import pathlib

import pytest

import _cctally_core
import _lib_forecast
from conftest import load_script

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
WEEK_START = dt.datetime(2026, 4, 20, 14, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolated_paths(monkeypatch, tmp_path):
    """Every envelope build in this module reads config and may touch the
    data dir, so the whole module runs against a scratch one."""
    from conftest import redirect_paths
    redirect_paths(load_script(), monkeypatch, tmp_path)


def _trend_row(ns, *, weeks_before, dpp, delta=None, is_current=False):
    return ns["TuiTrendRow"](
        week_label=f"wk-{weeks_before}",
        week_start_at=WEEK_START - dt.timedelta(days=7 * weeks_before),
        used_pct=40.0,
        dollars_per_percent=dpp,
        delta_dpp=delta,
        spark_height=4,
        is_current=is_current,
    )


def _current_week(ns, *, dpp):
    return ns["TuiCurrentWeek"](
        week_start_at=WEEK_START,
        week_end_at=WEEK_START + dt.timedelta(days=7),
        used_pct=40.0,
        five_hour_pct=None,
        five_hour_resets_at=None,
        spent_usd=100.0,
        dollars_per_percent=dpp,
        latest_snapshot_at=NOW,
    )


def _snapshot(ns, *, dpp, trend, forecast=None, rate_change=None):
    return ns["DataSnapshot"](
        current_week=_current_week(ns, dpp=dpp),
        forecast=forecast,
        trend=trend,
        sessions=[],
        last_sync_at=None,
        last_sync_error=None,
        generated_at=NOW,
        quota_rate_change=rate_change,
    )


def _envelope(ns, snap):
    return ns["snapshot_to_envelope"](snap, now_utc=NOW, monotonic_now=None)


# --------------------------------------------------------------------------
# The F7 coupling guard (spec section 10.1)
# --------------------------------------------------------------------------
def test_d3_delta_is_null_when_the_rate_operand_is_absent():
    """The hero could publish a week-over-week comparison on a screen
    stating no `$ / 1%` at all.

    `header.dollar_per_pct` came from `_tui_build_current_week` and
    `header.vs_last_week_delta` from the operand `build_trend_view`
    returned — two computations with no guard between them. This is the
    state no committed dashboard scenario reaches, which is why it is built
    here rather than found: the current week carries NO rate while the
    current trend row carries a delta.
    """
    ns = load_script()
    trend = [
        _trend_row(ns, weeks_before=2, dpp=3.0),
        _trend_row(ns, weeks_before=1, dpp=2.0, delta=-1.0),
        _trend_row(ns, weeks_before=0, dpp=None, delta=0.8, is_current=True),
    ]
    env = _envelope(ns, _snapshot(ns, dpp=None, trend=trend))
    assert env["header"]["dollar_per_pct"] is None
    assert env["header"]["vs_last_week_delta"] is None, (
        "a delta was published beside an absent rate, which is the pair the "
        "hero cannot render consistently")


def test_d3_the_delta_uses_the_header_s_own_current_operand():
    """The discriminating twin. The current trend row's own `delta_dpp` is
    deliberately a different number, so a header still reading that field
    fails here rather than passing by coincidence."""
    ns = load_script()
    trend = [
        _trend_row(ns, weeks_before=1, dpp=2.0),
        _trend_row(ns, weeks_before=0, dpp=9.0, delta=999.0, is_current=True),
    ]
    env = _envelope(ns, _snapshot(ns, dpp=5.0, trend=trend))
    assert env["header"]["dollar_per_pct"] == pytest.approx(5.0)
    assert env["header"]["vs_last_week_delta"] == pytest.approx(3.0)


def test_d3_a_null_rate_prior_row_is_skipped_not_used():
    """Spec section 10.1: rows failing a condition are SKIPPED and the walk
    continues, rather than the delta being abandoned at the first bad row."""
    ns = load_script()
    trend = [
        _trend_row(ns, weeks_before=2, dpp=1.0),
        _trend_row(ns, weeks_before=1, dpp=None),
        _trend_row(ns, weeks_before=0, dpp=5.0, is_current=True),
    ]
    env = _envelope(ns, _snapshot(ns, dpp=5.0, trend=trend))
    assert env["header"]["vs_last_week_delta"] == pytest.approx(4.0)


def test_d3_a_row_from_another_reset_domain_is_skipped():
    """A row synthesized by reset or credit handling sits at a fractional
    offset from the current anchor, so it is not a prior subscription week
    and its rate is over a different interval."""
    ns = load_script()
    ok_row = _trend_row(ns, weeks_before=2, dpp=1.0)
    synthetic = ns["TuiTrendRow"](
        week_label="mid-week",
        week_start_at=WEEK_START - dt.timedelta(days=3),
        used_pct=10.0,
        dollars_per_percent=4.0,
        delta_dpp=None,
        spark_height=2,
        is_current=False,
    )
    env = _envelope(ns, _snapshot(
        ns, dpp=5.0,
        trend=[ok_row, synthetic,
               _trend_row(ns, weeks_before=0, dpp=5.0, is_current=True)]))
    assert env["header"]["vs_last_week_delta"] == pytest.approx(4.0), (
        "the mid-week synthetic row was used as the prior operand; its "
        "rate is over a three-day interval, not a subscription week")


def test_d3_the_current_row_is_never_its_own_prior_operand():
    ns = load_script()
    env = _envelope(ns, _snapshot(
        ns, dpp=5.0,
        trend=[_trend_row(ns, weeks_before=0, dpp=5.0, is_current=True)]))
    assert env["header"]["dollar_per_pct"] == pytest.approx(5.0)
    assert env["header"]["vs_last_week_delta"] is None


def test_d3_a_rate_with_no_comparable_prior_is_still_published():
    """A first week has no prior operand, and hiding its rate would blank
    the hero for every new install. The Codex hero branch this guard is
    modelled on nulls only the DELTA when an operand is missing."""
    ns = load_script()
    env = _envelope(ns, _snapshot(ns, dpp=5.0, trend=[]))
    assert env["header"]["dollar_per_pct"] == pytest.approx(5.0)
    assert env["header"]["vs_last_week_delta"] is None


# --------------------------------------------------------------------------
# `forecast.quota` (spec section 10)
# --------------------------------------------------------------------------
def _forecast(ns, **over):
    fcmod = ns["_load_sibling"]("_lib_forecast")
    fields = dict(
        now_utc=NOW,
        week_start_at=WEEK_START,
        week_end_at=WEEK_START + dt.timedelta(days=7),
        elapsed_hours=120.0,
        elapsed_fraction=120.0 / 168.0,
        remaining_hours=48.0,
        remaining_days=2.0,
        p_now=40.0,
        five_hour_percent=None,
        spent_usd=100.0,
        snapshot_count=5,
        latest_snapshot_at=NOW,
        p_24h_ago=30.0,
        t_24h_actual_hours=24.0,
        dollars_per_percent=2.5,
        dollars_per_percent_source="trailing_4wk_median",
        confidence="high",
        low_confidence_reasons=(),
    )
    fields.update(over)
    inputs = fcmod.ForecastInputs(**fields)
    return fcmod._compute_forecast(inputs, [100, 90])


def test_d3_forecast_quota_is_additive_and_does_not_bump_source_schema():
    ns = load_script()
    baseline = _envelope(ns, _snapshot(ns, dpp=5.0, trend=[]))
    env = _envelope(ns, _snapshot(ns, dpp=5.0, trend=[],
                                  forecast=_forecast(ns)))
    assert "quota" in env["forecast"]
    assert env["source_schema_version"] == baseline["source_schema_version"]


def test_d3_the_quota_object_states_its_basis_and_the_corrected_interval():
    ns = load_script()
    env = _envelope(ns, _snapshot(ns, dpp=5.0, trend=[],
                                  forecast=_forecast(ns)))
    quota = env["forecast"]["quota"]
    assert quota["basis"] == "corrected-meter"
    assert quota["right_censored"] is False
    assert quota["corrected_interval"] == {"lo": 39.0, "hi": 40.0}
    assert quota["projection_pct"] is not None


def test_d3_a_right_censored_week_withholds_with_its_cause_and_presentation():
    """Section 8: the wire keeps the machine `code` and ADDS presentation
    fields. Full sentences do not replace machine codes."""
    ns = load_script()
    env = _envelope(ns, _snapshot(ns, dpp=5.0, trend=[],
                                  forecast=_forecast(ns, p_now=100.0)))
    quota = env["forecast"]["quota"]
    assert quota["basis"] == "withheld"
    assert quota["code"] == "right-censored"
    assert quota["code_presentation"] == {
        "code": "right-censored",
        "short": "right censored",
        "long": ("the meter reached its ceiling, so the real consumption is "
                 "only known to be at least the reading"),
    }
    assert quota["projection_pct"] is None
    assert quota["corrected_interval"]["hi"] is None


def test_d3_a_silent_fallback_is_impossible_because_the_cause_travels():
    """Falling back to the corrected meter is NOT a withholding, so
    `code` cannot carry why the calibrated basis was missed.
    `calibration_code` is the separate field that does, and a surface
    saying nothing there is silently falling back (spec section 1.1)."""
    ns = load_script()
    env = _envelope(ns, _snapshot(
        ns, dpp=5.0, trend=[],
        forecast=_forecast(ns, calibrated_withheld_code="unstable-fit")))
    quota = env["forecast"]["quota"]
    assert quota["basis"] == "corrected-meter"
    assert quota["code"] is None
    assert quota["calibration_code"] == "unstable-fit"
    assert quota["calibration_code_presentation"]["short"] == "unstable fit"


def test_d3_the_calibrated_triple_travels_together_or_not_at_all():
    ns = load_script()
    env = _envelope(ns, _snapshot(
        ns, dpp=5.0, trend=[],
        forecast=_forecast(
            ns, calibrated_projection_pct=61.0,
            calibrated_consumption_pct=44.0,
            calibrated_consumption_interval=(42.0, 46.0),
            calibrated_headroom_pct=56.0)))
    quota = env["forecast"]["quota"]
    assert quota["basis"] == "calibrated"
    assert quota["calibrated_consumption_pct"] == pytest.approx(44.0)
    assert quota["calibrated_consumption_interval"] == {"lo": 42.0, "hi": 46.0}
    assert quota["calibrated_headroom_pct"] == pytest.approx(56.0)


def test_d3_observed_minus_modelled_is_a_difference_and_needs_both_sides():
    """Spec section 10: the residual is the observed meter minus the
    modelled local quota, stated as exactly that. It is null unless both
    sides exist, because a difference from one operand is not a
    difference."""
    ns = load_script()
    # The COMPLETE modelled triple, because the envelope publishes the
    # calibrated view as a unit (#661 S2 review, D2) and a projection beside
    # a null headroom is now withheld rather than published. The producer
    # `_calibrated_week_detail` derives headroom from the same consumption in
    # the same return, so this is the input it actually emits — the partial
    # one this test used to build was never producible.
    with_both = _envelope(ns, _snapshot(
        ns, dpp=5.0, trend=[],
        forecast=_forecast(ns, calibrated_projection_pct=61.0,
                           calibrated_consumption_pct=34.0,
                           calibrated_headroom_pct=66.0)))
    assert with_both["forecast"]["quota"][
        "observed_minus_modelled_pct"] == pytest.approx(6.0)
    without = _envelope(ns, _snapshot(ns, dpp=5.0, trend=[],
                                      forecast=_forecast(ns)))
    assert without["forecast"]["quota"][
        "observed_minus_modelled_pct"] is None


def test_d3_the_rate_change_marker_travels_on_the_quota_object():
    ns = load_script()
    marker = {"active": True, "effective_from": "2026-04-01T00:00:00+00:00",
              "severity": "alarm", "previous_units_per_point": 2_442_620.0,
              "new_units_per_point": 1_685_000.0}
    env = _envelope(ns, _snapshot(ns, dpp=5.0, trend=[],
                                  forecast=_forecast(ns),
                                  rate_change=marker))
    assert env["forecast"]["quota"]["rate_change"] == marker


def test_d3_there_is_no_presentation_latch():
    """Spec section 10: trust is re-evaluated on every refresh. Two
    envelopes from the same builder with different evidence must not agree,
    which is what a latch would produce."""
    ns = load_script()
    # Same reason as the residual test above: the trusted side supplies the
    # whole modelled triple, which is what the producer emits.
    trusted = _envelope(ns, _snapshot(
        ns, dpp=5.0, trend=[],
        forecast=_forecast(ns, calibrated_projection_pct=61.0,
                           calibrated_consumption_pct=34.0,
                           calibrated_headroom_pct=66.0)))
    assert trusted["forecast"]["quota"]["basis"] == "calibrated"
    withdrawn = _envelope(ns, _snapshot(
        ns, dpp=5.0, trend=[],
        forecast=_forecast(ns, calibrated_withheld_code="unstable-fit")))
    assert withdrawn["forecast"]["quota"]["basis"] == "corrected-meter"


# --------------------------------------------------------------------------
# The calibration-read matrix on THIS consumer (spec section 13)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("code", [
    "unavailable",            # absent, unreadable, quarantined or malformed
    "future",                 # an observation captured ahead of the clock
    "stale",                  # fitted under other constants
    "unsupported-model-mix",  # readable, and refused for this population
])
def test_d3_every_unusable_calibration_state_states_a_cause_here(code):
    """The dashboard consumer's leg of the corrupt / version-ahead /
    quarantined / racy matrix, which the plan folded into this task's
    implementation rather than giving it a task of its own.

    Each state reaches this surface as a `calibration_code`, so the panel
    renders a reason rather than a blank, and the basis stays the corrected
    meter rather than silently claiming the model.

    ASSERTED DOWNSTREAM OF THE READ, deliberately: the input already carries
    the code, so this covers "given a cause, the envelope renders it" and
    nothing about which file state produces which cause. That half is
    `tests/test_forecast_calibration_read_matrix.py`, which drives
    `_calibrated_week_detail` against a real file per state. An earlier
    revision of the list above labelled `future` as the version-ahead cause;
    it is not. `_CALIBRATION_REJECTION_CODES` maps only the fingerprint and
    revision mismatches to `stale` and reports every other rejection —
    version-ahead included — as `unavailable`.
    """
    ns = load_script()
    env = _envelope(ns, _snapshot(
        ns, dpp=5.0, trend=[],
        forecast=_forecast(ns, calibrated_withheld_code=code)))
    quota = env["forecast"]["quota"]
    assert quota["basis"] == "corrected-meter"
    assert quota["calibration_code"] == code
    assert quota["calibration_code_presentation"]["long"], code


def test_d3_a_racy_read_that_yields_nothing_is_not_reported_as_a_model():
    """The racy leg: a calibration that vanished between the read and the
    apply produces no projection and no consumption, and the object must
    not publish a calibrated basis with null numbers under it."""
    ns = load_script()
    env = _envelope(ns, _snapshot(
        ns, dpp=5.0, trend=[],
        forecast=_forecast(ns, calibrated_projection_pct=None,
                           calibrated_withheld_code="unavailable",
                           calibrated_consumption_pct=None)))
    quota = env["forecast"]["quota"]
    assert quota["basis"] == "corrected-meter"
    assert quota["calibrated_consumption_pct"] is None
    assert quota["calibrated_headroom_pct"] is None
    assert quota["observed_minus_modelled_pct"] is None


# --------------------------------------------------------------------------
# The calibrated view is published as a UNIT (#661 S2 review, finding D2)
# --------------------------------------------------------------------------
def test_d3_a_calibrated_basis_is_never_published_over_null_modelled_numbers():
    """The invariant held only by the producer's discipline, not by a guard.

    `_calibrated_week_detail` sets `projection_pct`, `consumption_pct` and
    `headroom_pct` together or not at all, so this pair was unreachable — but
    `_forecast_quota_envelope` tested nothing, and `select_projection_basis`
    reads ONLY `calibrated_projection_pct`. A caller that built the inputs by
    hand, or a future producer that split the triple, would publish
    `basis: "calibrated"` beside a null modelled consumption: a screen saying
    the projection came from the model, over a model that states no number.
    """
    ns = load_script()
    env = _envelope(ns, _snapshot(
        ns, dpp=5.0, trend=[],
        forecast=_forecast(ns, calibrated_projection_pct=61.0,
                           calibrated_consumption_pct=None)))
    quota = env["forecast"]["quota"]
    assert quota["basis"] != "calibrated", (
        "a calibrated basis was published over a null modelled consumption")
    assert quota["calibration_code"], (
        "the object refused the model and stated no cause for refusing it")


def test_d3_an_incomplete_modelled_view_withholds_rather_than_mislabelling():
    """What the guard publishes instead, stated exactly.

    Demoting to `corrected-meter` would relabel a number the calibrated model
    produced, and there is no meter projection on the forecast to substitute,
    so the object withholds: an already-rendered basis on both surfaces, with
    the modelled triple nulled beside it.
    """
    ns = load_script()
    env = _envelope(ns, _snapshot(
        ns, dpp=5.0, trend=[],
        forecast=_forecast(ns, calibrated_projection_pct=61.0,
                           calibrated_headroom_pct=None)))
    quota = env["forecast"]["quota"]
    assert quota["basis"] == "withheld"
    assert quota["projection_pct"] is None
    assert quota["calibrated_consumption_pct"] is None
    assert quota["calibrated_consumption_interval"] is None
    assert quota["calibrated_headroom_pct"] is None
    assert quota["observed_minus_modelled_pct"] is None
    assert quota["code"]


def test_d3_a_complete_modelled_view_is_untouched_by_the_guard():
    """Non-vacuity: a guard that fired on every input would satisfy both
    assertions above while destroying the surface."""
    ns = load_script()
    env = _envelope(ns, _snapshot(
        ns, dpp=5.0, trend=[],
        forecast=_forecast(ns, calibrated_projection_pct=61.0,
                           calibrated_consumption_pct=44.0,
                           calibrated_headroom_pct=56.0)))
    quota = env["forecast"]["quota"]
    assert quota["basis"] == "calibrated"
    assert quota["projection_pct"] == pytest.approx(61.0)
    assert quota["calibrated_consumption_pct"] == pytest.approx(44.0)
    assert quota["calibrated_headroom_pct"] == pytest.approx(56.0)


# --------------------------------------------------------------------------
# "Nearest" means nearest by ANCHOR (#661 S2 review, finding E4)
# --------------------------------------------------------------------------
def test_d3_the_prior_operand_is_the_nearest_by_anchor_not_by_position():
    """Spec section 10.1 says NEAREST comparable prior operand.

    The walk was `reversed(list(trend))`, so "nearest" meant last in the list
    and its correctness rested entirely on `snap.trend` arriving ascending —
    true of `build_trend_view` and of every committed fixture, and stated
    nowhere. `_same_reset_domain` accepts any positive whole-week multiple, so
    a row twenty weeks back was as comparable as one week back.

    Here the two disagree: the list ends with the OLDER row.
    """
    ns = load_script()
    trend = [
        _trend_row(ns, weeks_before=1, dpp=4.0),
        _trend_row(ns, weeks_before=3, dpp=1.0),
        _trend_row(ns, weeks_before=0, dpp=5.0, is_current=True),
    ]
    env = _envelope(ns, _snapshot(ns, dpp=5.0, trend=trend))
    assert env["header"]["vs_last_week_delta"] == pytest.approx(1.0), (
        "the three-weeks-back row was used because it came last in the "
        "list; the one-week-back row is nearer by anchor")


def test_d3_an_ascending_trend_still_picks_the_same_prior_row():
    """Non-vacuity for the ordering fix: the shipped producer emits ascending
    rows, so the fix must leave that case byte-identical."""
    ns = load_script()
    trend = [
        _trend_row(ns, weeks_before=3, dpp=1.0),
        _trend_row(ns, weeks_before=1, dpp=4.0),
        _trend_row(ns, weeks_before=0, dpp=5.0, is_current=True),
    ]
    env = _envelope(ns, _snapshot(ns, dpp=5.0, trend=trend))
    assert env["header"]["vs_last_week_delta"] == pytest.approx(1.0)


def test_d3_a_row_with_no_anchor_is_skipped_rather_than_breaking_the_sort():
    """`week_start_at` is optional on the row, and ordering by it must not
    raise on a row carrying None. Such a row fails the reset-domain test
    anyway, so it is skipped."""
    ns = load_script()
    anchorless = ns["TuiTrendRow"](
        week_label="no-anchor",
        week_start_at=None,
        used_pct=10.0,
        dollars_per_percent=99.0,
        delta_dpp=None,
        spark_height=2,
        is_current=False,
    )
    trend = [
        _trend_row(ns, weeks_before=1, dpp=4.0),
        anchorless,
        _trend_row(ns, weeks_before=0, dpp=5.0, is_current=True),
    ]
    env = _envelope(ns, _snapshot(ns, dpp=5.0, trend=trend))
    assert env["header"]["vs_last_week_delta"] == pytest.approx(1.0)


def test_d3_an_anchorless_row_is_not_used_even_as_the_only_candidate():
    """The discriminating twin of the sort test above.

    With any anchored row present the ordering reaches that row first, so the
    anchorless one is never examined and a `_same_reset_domain` that accepted
    a missing anchor would still pass. Mutation testing found exactly that
    masking. Here the anchorless row is the ONLY non-current candidate, so
    the reset-domain test is the one thing standing between it and the delta.
    """
    ns = load_script()
    anchorless = ns["TuiTrendRow"](
        week_label="no-anchor",
        week_start_at=None,
        used_pct=10.0,
        dollars_per_percent=99.0,
        delta_dpp=None,
        spark_height=2,
        is_current=False,
    )
    env = _envelope(ns, _snapshot(
        ns, dpp=5.0,
        trend=[anchorless,
               _trend_row(ns, weeks_before=0, dpp=5.0, is_current=True)]))
    assert env["header"]["dollar_per_pct"] == pytest.approx(5.0)
    assert env["header"]["vs_last_week_delta"] is None, (
        "a row carrying no week anchor was used as the prior operand; its "
        "reset domain is unknown, not shared")


def test_d3_an_in_progress_row_is_skipped_on_its_own_condition():
    """`is_current` is an INDEPENDENT condition, not a restatement of the
    anchor arithmetic.

    In the ordinary case the current row's anchor equals the header's, so
    `_same_reset_domain`'s `delta <= 0` guard rejects it too and either check
    alone would do — which is what let a mutation deleting the `is_current`
    filter pass every test. Here the row marked current sits a whole week
    back, so the anchor arithmetic ACCEPTS it and only the completed-interval
    condition rejects it. A partial week's rate is over a shorter interval
    than the week it is compared against, whatever its anchor says.
    """
    ns = load_script()
    trend = [
        _trend_row(ns, weeks_before=2, dpp=1.0),
        _trend_row(ns, weeks_before=1, dpp=9.0, is_current=True),
    ]
    env = _envelope(ns, _snapshot(ns, dpp=5.0, trend=trend))
    assert env["header"]["vs_last_week_delta"] == pytest.approx(4.0), (
        "the in-progress row was used as the prior operand")


def test_d3_the_guards_wire_literals_still_match_the_basis_enum():
    """The guard compares `basis` against bare strings; pin them to the enum.

    `bin/_cctally_dashboard_envelope.py` names `"calibrated"` and `"withheld"`
    as literals and deliberately does not import `_lib_forecast`, so the
    envelope module carries no import-time edge to the forecast kernel. The
    cost of that choice is that changing `ProjectionBasis`'s values would
    leave the guard comparing against dead strings, and the negative
    assertion in its own test (`basis != "calibrated"`) would then pass
    vacuously rather than fail. This test fails on the enum change itself
    rather than on its consequence.
    """
    assert _lib_forecast.ProjectionBasis.CALIBRATED.value == "calibrated"
    assert _lib_forecast.ProjectionBasis.WITHHELD.value == "withheld"

    source = (pathlib.Path(_cctally_core.__file__).resolve().parent
              / "_cctally_dashboard_envelope.py").read_text(encoding="utf-8")
    guard = source.split("The calibrated view is published as a UNIT")[1]
    guard = guard.split("return {")[0]
    assert 'basis == "calibrated"' in guard, (
        "the guard no longer names the CALIBRATED wire value")
    assert 'basis = "withheld"' in guard, (
        "the guard no longer degrades to the WITHHELD wire value")
