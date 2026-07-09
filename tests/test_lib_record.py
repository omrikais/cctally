"""Direct unit tests for bin/_lib_record.py (#279 S4 F3).

Decision-table coverage for the seven record write-path kernels lifted out
of cmd_record_usage / maybe_record_projected_alert. Each kernel mirrors the
exact comparison operators of its source fragment; the tests pin the
inclusive band bounds, the round(x, 1) clamp granularity, the `+ 1e-9`
percent/threshold snap, and every branch of the weekly-debounce classifier.
"""
from __future__ import annotations

# conftest puts bin/ on sys.path.
from _lib_record import (
    ARM_MARKER,
    CLEAR_MARKER,
    CONFIRM_RESET,
    FIRE_IMMEDIATE,
    NO_ACTION,
    check_resets_at_plausibility,
    hwm_clamp_applies,
    hwm_file_next,
    milestone_coverage_owes,
    plan_five_hour_credit,
    plan_weekly_credit_debounce,
    projected_crossings,
)


# Real reset constants (bin/_cctally_weekrefs.py): drop 25.0, zero floor 1.0,
# zero min-drop 3.0, 5h drop 5.0.
DROP = 25.0
ZERO_FLOOR = 1.0
ZERO_MIN_DROP = 3.0
FIVE_H_DROP = 5.0


# ── Fragment 1: plausibility band (inclusive both bounds) ──────────────────
def test_plausibility_band_inclusive_bounds():
    now = 1_750_000_000
    past = 30 * 86400
    fut = 8 * 86400
    assert check_resets_at_plausibility(now, now, past_slack_s=past, future_band_s=fut)
    # inclusive lower + upper
    assert check_resets_at_plausibility(now - past, now, past_slack_s=past, future_band_s=fut)
    assert check_resets_at_plausibility(now + fut, now, past_slack_s=past, future_band_s=fut)
    # one second past either bound is out of band
    assert not check_resets_at_plausibility(now - past - 1, now, past_slack_s=past, future_band_s=fut)
    assert not check_resets_at_plausibility(now + fut + 1, now, past_slack_s=past, future_band_s=fut)


def test_plausibility_5h_band_second_granularity():
    now = 1_750_000_000
    past = 600           # 10 min
    fut = 6 * 3600       # 6h
    assert check_resets_at_plausibility(now - 600, now, past_slack_s=past, future_band_s=fut)
    assert not check_resets_at_plausibility(now - 601, now, past_slack_s=past, future_band_s=fut)
    assert check_resets_at_plausibility(now + 6 * 3600, now, past_slack_s=past, future_band_s=fut)
    assert not check_resets_at_plausibility(now + 6 * 3600 + 1, now, past_slack_s=past, future_band_s=fut)


# ── Fragment 2: weekly credit / reset-to-zero debounce (every branch) ──────
def _debounce(prev, new, *, armed=False, baseline=None):
    return plan_weekly_credit_debounce(
        prev, new, drop_threshold=DROP, zero_floor_pct=ZERO_FLOOR,
        zero_min_drop_pct=ZERO_MIN_DROP, marker_armed=armed, marker_baseline=baseline,
    ).action


def test_debounce_big_drop_fires_immediately():
    assert _debounce(46.0, 10.0) == FIRE_IMMEDIATE          # drop 36 >= 25
    assert _debounce(25.0, 0.0) == FIRE_IMMEDIATE           # drop exactly 25 (>=)


def test_debounce_big_drop_wins_even_when_armed():
    assert _debounce(46.0, 10.0, armed=True, baseline=46.0) == FIRE_IMMEDIATE


def test_debounce_armed_confirm_when_stays_low():
    # drop 1 (< 25); armed; new 0 <= baseline/2 (10) -> confirm
    assert _debounce(16.0, 0.0, armed=True, baseline=20.0) == CONFIRM_RESET
    # boundary: new exactly == baseline/2 confirms (<=)
    assert _debounce(16.0, 10.0, armed=True, baseline=20.0) == CONFIRM_RESET


def test_debounce_armed_clear_when_recovers():
    # armed; new 15 > baseline/2 (10) -> recovery clear
    assert _debounce(16.0, 15.0, armed=True, baseline=20.0) == CLEAR_MARKER


def test_debounce_arm_on_first_zero():
    # not big, not armed, new <= floor (1.0) AND drop (4.5) >= min-drop (3) -> arm
    assert _debounce(5.0, 0.5) == ARM_MARKER


def test_debounce_none_when_not_reset_shape():
    # drop 2 < min-drop 3 -> not zero_only, not armed -> none
    assert _debounce(10.0, 8.0) == NO_ACTION
    # new above zero floor -> not zero_only
    assert _debounce(20.0, 15.0) == NO_ACTION


# ── Fragment 3: 5h in-place credit guard ───────────────────────────────────
def test_five_hour_credit_requires_future_reset_and_threshold_drop():
    assert plan_five_hour_credit(46.0, 20.0, drop_threshold=FIVE_H_DROP,
                                 prior_resets_in_future=True)
    # drop 26 >= 5 but window already over -> no credit
    assert not plan_five_hour_credit(46.0, 20.0, drop_threshold=FIVE_H_DROP,
                                     prior_resets_in_future=False)
    # future reset but drop 3 < 5 -> no credit
    assert not plan_five_hour_credit(46.0, 43.0, drop_threshold=FIVE_H_DROP,
                                     prior_resets_in_future=True)
    # drop exactly == threshold fires (>=)
    assert plan_five_hour_credit(10.0, 5.0, drop_threshold=FIVE_H_DROP,
                                 prior_resets_in_future=True)


# ── Fragment 4: reset-aware HWM clamp (round to tenths) ────────────────────
def test_hwm_clamp_rounds_to_tenths():
    assert hwm_clamp_applies(56.94, 57.0)          # 56.9 < 57.0 -> clamp
    assert not hwm_clamp_applies(56.96, 57.0)      # 57.0 == 57.0 -> no clamp
    assert not hwm_clamp_applies(50.0, None)       # no recorded max -> no clamp
    assert not hwm_clamp_applies(60.0, 57.0)       # above max -> no clamp


# ── Fragment 5: self-heal milestone coverage predicate ─────────────────────
def test_milestone_coverage_owes():
    assert milestone_coverage_owes(None, 3)        # no rows -> owes
    assert milestone_coverage_owes(2, 3)           # highest 2 < floor 3 -> owes
    assert not milestone_coverage_owes(3, 3)       # 3 == floor -> covered
    assert not milestone_coverage_owes(5, 3)       # 5 > floor -> covered


# ── Fragment 6: hwm-file monotonic step (>= semantics) ─────────────────────
def test_hwm_file_next_monotonic():
    assert hwm_file_next(None, 42.0) == 42.0       # no prior -> write
    assert hwm_file_next(41.9, 42.0) == 42.0       # higher -> write
    assert hwm_file_next(42.0, 42.0) == 42.0       # equal -> write (>= operator)
    assert hwm_file_next(50.0, 42.0) is None        # lower -> skip
    assert hwm_file_next(0.0, 0.0) == 0.0           # 0 >= 0 -> write


# ── Fragment 7: projected-pace crossings (prescaled + 1e-9 snap) ───────────
def test_projected_crossings_prescaled_pairs():
    # weekly leg shape: comparand == raw threshold
    assert projected_crossings(90.0, [(90, 90.0), (100, 100.0)]) == [90]
    # budget leg shape: comparand == (t/100)*target -- kernel never rescales
    assert projected_crossings(45.0, [(90, 45.0), (100, 50.0)]) == [90]
    # both cross
    assert projected_crossings(100.0, [(90, 90.0), (100, 100.0)]) == [90, 100]
    # none cross
    assert projected_crossings(10.0, [(90, 90.0), (100, 100.0)]) == []


def test_projected_crossings_1e9_snap():
    # a float sitting one ULP under the comparand still crosses (+ 1e-9)
    assert projected_crossings(89.99999999999999, [(90, 90.0)]) == [90]
