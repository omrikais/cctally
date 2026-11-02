"""#948 (replacing #825/#832 Task 3.1) — the pure outcome-quarantine kernel.

``bin/_lib_outcome_quarantine.py`` owns the per-axis recovery barrier: the
cohort bounds, the refined weekly and five-hour summaries over the whole
retained prefix, the witness predicates, journal coverage, releases and
revocations (spec 6.3–6.9). Each predicate has its own failing variant, the
T1 schedule's positions are pinned against the cohort arithmetic, and the
capture-order tie rules (BV2, the f14 offset form) are exercised directly.

Failures detected: a recovery barrier that releases on the wrong period
(an accounting error every user would see as a missed or duplicated credit),
a release the clock model does not justify, a revocation that misses a
backward input, and a persisted row that reads as clean when it is not.
"""
import dataclasses
import datetime as dt
import pathlib
import sys

import pytest

from conftest import load_script

sys.path.insert(0, str(pathlib.Path(load_script()["__file__"]).resolve().parent))

import _lib_outcome_quarantine as Q  # noqa: E402

UTC = dt.timezone.utc


def _epoch(text):
    return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def _iso(epoch):
    return dt.datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_week_end(epoch):
    """`_cctally_core._normalize_week_boundary_dt`: round to the nearest
    hour."""
    value = dt.datetime.fromtimestamp(epoch, tz=UTC).replace(second=0)
    if value.minute >= 30:
        value = value.replace(minute=0) + dt.timedelta(hours=1)
    else:
        value = value.replace(minute=0)
    return int(value.timestamp())


def reading(oid, *, at, week_end=None, five_end=None, account="A",
            captured=None, capture_text=None, key=None, eligible=Q.AXES,
            observed=Q.AXES, stamped=True):
    """A Reading built the way the glue builds one from a journal record."""
    detection = _epoch(at)
    capture = _epoch(captured or at)
    end = None if week_end is None else _epoch(week_end)
    start = None if end is None else end - 7 * 86400
    raw5 = None if five_end is None else _epoch(five_end)
    if key is None and raw5 is not None:
        key = raw5 - raw5 % 600
    return Q.Reading(
        observation_id=oid, account_key=account,
        capture_text=capture_text or _iso(capture), capture_epoch=capture,
        detection_epoch=detection,
        week_start_date=(None if start is None else
                         dt.datetime.fromtimestamp(start, tz=UTC).date().isoformat()),
        week_start_at_epoch=start, weekly_end_epoch=end,
        five_hour_key=key, raw_five_hour_reset=raw5,
        observed_axes=frozenset(observed), eligible_axes=frozenset(eligible),
        stamped=stamped)


def weekly_of(*readings):
    b = Q.WeeklyBounds()
    for r in readings:
        b = b.absorb_reading(r, possible_effect=False)
    return b


def five_of(*readings):
    b = Q.FiveHourBounds()
    for r in readings:
        b = b.absorb_reading(r)
    return b


# --------------------------------------------------------------------------
# cohort arithmetic (spec 6.3) at the T1 trigger
# --------------------------------------------------------------------------

def test_t1_cohort_arithmetic():
    """Legacy activity at 2026-11-01 00:00 UTC: R = H + 5 s, E = R + 90 s,
    U5.raw = E + 6 h, U5.key = U5.raw + 1,800 s, U7.end = the normalized
    E + 8 d and U7.date its week start."""
    cohort = Q.Cohort.from_h(_epoch("2026-11-01T00:00:00Z"),
                             normalize_week_end=normalize_week_end)
    assert _iso(cohort.r) == "2026-11-01T00:00:05Z"
    assert _iso(cohort.e) == "2026-11-01T00:01:35Z"
    assert _iso(cohort.u5_raw) == "2026-11-01T06:01:35Z"
    assert _iso(cohort.u5_key) == "2026-11-01T06:31:35Z"
    assert _iso(cohort.u7_end) == "2026-11-09T00:00:00Z"
    assert cohort.u7_date == "2026-11-02"
    assert Q.Cohort.from_document(cohort.to_document()) == cohort


T1_COHORT = Q.Cohort.from_h(_epoch("2026-11-01T00:00:00Z"),
                            normalize_week_end=normalize_week_end)


# --------------------------------------------------------------------------
# the five-hour witness (spec 6.6)
# --------------------------------------------------------------------------

def _t1_five_bounds():
    """The five-hour summaries just before 06:00: the pre-trigger window
    ending 01:00, its successor ending 06:00 and the cohort."""
    prefix = [reading("p1", at="2026-10-31T23:59:00Z",
                      five_end="2026-11-01T01:00:00Z"),
              reading("p2", at="2026-11-01T01:00:30Z",
                      five_end="2026-11-01T06:00:00Z")]
    return five_of(*prefix).absorb_cohort(T1_COHORT)


def test_five_hour_witness_rejects_the_0600_entry_by_detection():
    bounds = _t1_five_bounds()
    first = reading("w06", at="2026-11-01T06:00:10Z",
                    five_end="2026-11-01T11:00:00Z")
    floor = bounds.detect_floor
    assert floor == T1_COHORT.u5_raw
    # Key, anchor separation and capture order all hold; only the detection
    # clause (06:00:10 is not after U5.raw = 06:01:35) refuses it.
    assert first.five_hour_key > bounds.k5
    assert first.raw_five_hour_reset > bounds.raw_upper5 + Q.FIVE_HOUR_SEPARATION_S
    assert not Q.five_hour_witness_qualifies(first, bounds, detection_floor=floor)


def test_five_hour_witness_accepts_the_1100_entry_naming_1600():
    bounds = _t1_five_bounds()
    inside = reading("w06", at="2026-11-01T06:00:10Z",
                     five_end="2026-11-01T11:00:00Z")
    bounds = bounds.absorb_reading(inside)
    fresh = reading("w11", at="2026-11-01T11:00:10Z",
                    five_end="2026-11-01T16:00:00Z")
    assert Q.five_hour_witness_qualifies(fresh, bounds,
                                         detection_floor=bounds.detect_floor)


@pytest.mark.parametrize("variant, change", [
    ("key not fresh", dict(five_end="2026-11-01T11:00:00Z")),
    ("anchor within both tolerances", dict(five_end="2026-11-01T11:00:00Z",
                                           key=_epoch("2026-11-01T16:00:00Z"))),
    ("capture before the maximum", dict(captured="2026-11-01T06:00:00Z")),
    ("detection at the floor", dict(at="2026-11-01T06:01:35Z",
                                    captured="2026-11-01T11:00:10Z")),
])
def test_each_five_hour_clause_has_its_own_failing_variant(variant, change):
    bounds = _t1_five_bounds().absorb_reading(
        reading("w06", at="2026-11-01T06:00:10Z",
                five_end="2026-11-01T11:00:00Z"))
    kwargs = dict(at="2026-11-01T11:00:10Z", five_end="2026-11-01T16:00:00Z")
    kwargs.update(change)
    candidate = reading("x", **kwargs)
    assert not Q.five_hour_witness_qualifies(
        candidate, bounds, detection_floor=bounds.detect_floor), variant


# --------------------------------------------------------------------------
# the weekly witness (spec 6.6), T1's delayed rollover
# --------------------------------------------------------------------------

B = _epoch("2026-11-14T00:00:00Z")


def _t1_weekly_state():
    """Weekly summaries through the Nov 7–14 week (each reading a possible
    effect: its predecessor end is later than its detection), then renders
    at B and B + 60 s that still name end B."""
    b = Q.WeeklyBounds().absorb_reading(
        reading("p", at="2026-10-31T23:59:00Z", week_end="2026-11-07T00:00:00Z"),
        possible_effect=True).absorb_cohort(T1_COHORT)
    for at in ("2026-11-07T00:05:00Z", "2026-11-10T12:00:00Z",
               "2026-11-13T23:59:00Z"):
        b = b.absorb_reading(reading(at, at=at, week_end="2026-11-14T00:00:00Z"),
                             possible_effect=True)
    for offset in (0, 60):
        late = reading(f"late{offset}", at=_iso(B + offset),
                       week_end="2026-11-14T00:00:00Z")
        # The detector's live-end condition: end B is not later than the
        # detection, so no weekly effect was possible at this reading.
        assert not (late.weekly_end_epoch > late.detection_epoch)
        b = b.absorb_reading(late, possible_effect=False)
    return b


def test_weekly_witness_rejects_the_nov_7_week_by_x7():
    b = Q.WeeklyBounds().absorb_reading(
        reading("p", at="2026-10-31T23:59:00Z", week_end="2026-11-07T00:00:00Z"),
        possible_effect=True).absorb_cohort(T1_COHORT)
    first = reading("w7", at="2026-11-07T00:00:30Z",
                    week_end="2026-11-14T00:00:00Z")
    assert first.week_start_date > b.s7_date and first.weekly_end_epoch > b.s7_end
    assert first.week_start_at_epoch < b.x7  # U7.end = Nov 9
    assert not Q.weekly_witness_qualifies(first, b)


def test_weekly_witness_accepts_the_b_plus_120_first_entry():
    b = _t1_weekly_state()
    fresh = reading("w", at=_iso(B + 120), week_end="2026-11-21T00:00:00Z")
    assert b.c7 < B
    assert Q.weekly_witness_qualifies(fresh, b)


@pytest.mark.parametrize("variant, change, bounds_change", [
    ("date key not fresh", {}, dict(s7_date="2026-11-14")),
    ("end not fresh", {}, dict(s7_end=_epoch("2026-11-21T00:00:00Z"))),
    ("possible effect at the week start", {}, dict(c7=B)),
    ("reference-only publication before X7", {},
     dict(x7=B + 1)),
    ("capture before the maximum", dict(captured=_iso(B + 30)), {}),
    ("detection not after the predecessor end", dict(at=_iso(B - 10),
                                                     captured=_iso(B + 120)),
     dict(s7_end=B - 1, c7=B - 7 * 86400)),
])
def test_each_weekly_clause_has_its_own_failing_variant(variant, change,
                                                        bounds_change):
    b = dataclasses.replace(_t1_weekly_state(), **bounds_change)
    kwargs = dict(at=_iso(B + 120), week_end="2026-11-21T00:00:00Z")
    kwargs.update(change)
    candidate = reading("x", **kwargs)
    assert not Q.weekly_witness_qualifies(candidate, b), variant


def test_x7_is_strict_before_and_never_rounded():
    """A week start EQUAL to X7 qualifies (reference-only publications are
    strictly before the end); one second earlier does not."""
    b = dataclasses.replace(_t1_weekly_state(), x7=B)
    assert Q.weekly_witness_qualifies(
        reading("w", at=_iso(B + 120), week_end="2026-11-21T00:00:00Z"), b)
    b = dataclasses.replace(b, x7=B + 1)
    assert not Q.weekly_witness_qualifies(
        reading("w", at=_iso(B + 120), week_end="2026-11-21T00:00:00Z"), b)


def test_a_prefix_reading_of_the_same_week_makes_it_no_first_entry():
    """The first-entry and debounce clauses are implied by key freshness: a
    prefix reading of the witness's own (date, end) fails the date key."""
    b = _t1_weekly_state().absorb_reading(
        reading("early", at=_iso(B + 90), week_end="2026-11-21T00:00:00Z"),
        possible_effect=False)
    assert not Q.weekly_witness_qualifies(
        reading("w", at=_iso(B + 120), week_end="2026-11-21T00:00:00Z"), b)


def test_c7_excludes_a_reading_captured_after_its_predecessor_end():
    state = Q.QuarantineState()
    state.evaluate_reading("A", reading("p", at="2026-11-13T23:00:00Z",
                                        week_end="2026-11-14T00:00:00Z"))
    late = reading("late", at=_iso(B + 60), week_end="2026-11-14T00:00:00Z")
    assert not state.weekly_possible_effect("A", late)
    live = reading("live", at="2026-11-13T23:30:00Z",
                   week_end="2026-11-14T00:00:00Z")
    assert state.weekly_possible_effect("A", live)
    # A projection-filled weekly axis is never a possible effect.
    filled = reading("filled", at="2026-11-13T23:30:00Z",
                     week_end="2026-11-14T00:00:00Z",
                     observed=(Q.FIVE_HOUR,))
    assert not state.weekly_possible_effect("A", filled)


# --------------------------------------------------------------------------
# capture-order ties (spec 6.6, BV2) and the f14 offset form
# --------------------------------------------------------------------------

def test_equal_capture_instant_and_text_qualifies():
    """A same-second rollover: the old-period reading and the fresh first
    entry share one capture second and text; the journal-order id tie-break
    sorts the fresh entry first, so it witnesses."""
    b = _t1_weekly_state().absorb_reading(
        reading("old", at=_iso(B + 120), week_end="2026-11-14T00:00:00Z"),
        possible_effect=False)
    fresh = reading("w", at=_iso(B + 120), week_end="2026-11-21T00:00:00Z")
    assert fresh.capture_text == b.t7 and fresh.capture_epoch == b.k7
    assert Q.weekly_witness_qualifies(fresh, b)


def test_a_chronologically_equal_but_lexically_earlier_capture_fails():
    """The f14 offset form: `+00:00` text sorts below the `Z` text of the
    same instant, so predecessor reads would select the older row; an equal
    instant with equal text still witnesses."""
    retained = reading("old", at=_iso(B + 100), week_end="2026-11-14T00:00:00Z")
    b = _t1_weekly_state().absorb_reading(retained, possible_effect=False)
    offset_text = dt.datetime.fromtimestamp(B + 100, tz=UTC).isoformat()
    assert offset_text < b.t7
    fresh = reading("w", at=_iso(B + 120), captured=_iso(B + 100),
                    week_end="2026-11-21T00:00:00Z", capture_text=offset_text)
    assert fresh.capture_epoch == b.k7
    assert not Q.weekly_witness_qualifies(fresh, b)
    assert Q.weekly_witness_qualifies(
        dataclasses.replace(fresh, capture_text=_iso(B + 100)), b)
    five = five_of(reading("o5", at=_iso(B + 100), five_end=_iso(B + 3600)))
    fresh5 = reading("w5", at=_iso(B + 7 * 3600), captured=_iso(B + 100),
                     five_end=_iso(B + 12 * 3600), capture_text=offset_text)
    assert not Q.five_hour_witness_qualifies(fresh5, five, detection_floor=None)
    assert Q.five_hour_witness_qualifies(
        dataclasses.replace(fresh5, capture_text=_iso(B + 100)), five,
        detection_floor=None)


# --------------------------------------------------------------------------
# the fold: generations, episodes, coverage, witness, release, revocation
# --------------------------------------------------------------------------

def _paused_state(*, spool=True):
    """A state with one pre-trigger reading for A and one trigger at
    2026-11-01 00:00 (a spool trigger whose cohort sets P there)."""
    state = Q.QuarantineState()
    state.evaluate_reading("A", reading("pre", at="2026-10-31T23:59:00Z",
                                        week_end="2026-11-07T00:00:00Z",
                                        five_end="2026-11-01T01:00:00Z"))
    at = _epoch("2026-11-01T00:00:00Z")
    state.note_admitted(at)
    state.on_trigger(kind=Q.TRIGGER_KIND_LEGACY_SPOOL if spool
                     else Q.TRIGGER_KIND_EXCLUDED, reason="changed",
                     segment="s", offset=1, at=_iso(at))
    if spool:
        state.set_position(normalize_week_end)
    return state


def _oauth(state, oid, at, *, axes=Q.AXES, week_end="2026-11-07T00:00:00Z",
           five_end="2026-11-01T06:00:00Z"):
    """An OAuth observation (a control witness) folded, then its completion
    op. Returns whether the completion was valid."""
    r = reading(oid, at=at, week_end=week_end,
                five_end=five_end if Q.FIVE_HOUR in axes else None)
    state.note_clock(r.capture_epoch)
    state.evaluate_reading("A", r)
    state.on_control_witness("A", Q.ControlWitness(
        oid, r.capture_epoch, frozenset(axes), r.weekly_end_epoch))
    return state.on_authority_completion(
        "A", f"c:{oid}", generation=state.generation, observation_id=oid,
        axes=tuple(axes))


def test_a_trigger_pauses_every_account_including_one_first_seen_later():
    state = _paused_state()
    for account in ("A", "B-first-seen-later"):
        for axis in Q.AXES:
            assert not state.axis_gate(account, axis).allowed
    assert state.waiting_for("A", Q.WEEKLY) == Q.WAIT_EXPIRY


def test_expiry_then_usage_refresh_then_fresh_period():
    state = _paused_state()
    # A retained evaluation appended after E whose own instant is before E
    # does not establish expiry.
    state.note_clock(T1_COHORT.e - 1)
    assert state.waiting_for("A", Q.FIVE_HOUR) == Q.WAIT_EXPIRY
    assert _oauth(state, "o1", "2026-11-01T00:02:00Z")
    assert state.waiting_for("A", Q.FIVE_HOUR) == Q.WAIT_FRESH_PERIOD
    assert state.waiting_for("A", Q.WEEKLY) == Q.WAIT_FRESH_PERIOD


def test_an_oauth_reading_before_expiry_covers_nothing():
    state = _paused_state()
    assert not _oauth(state, "o0", "2026-11-01T00:01:00Z")
    assert state.waiting_for("A", Q.WEEKLY) == Q.WAIT_EXPIRY


def test_partial_coverage_then_supplementation_covers_both_axes():
    """F1: a weekly-only reading covers weekly; a later two-axis reading
    covers five-hour with a second completion; a repeat covers nothing."""
    state = _paused_state()
    assert _oauth(state, "o1", "2026-11-01T00:02:00Z", axes=(Q.WEEKLY,))
    assert state.axis("A", Q.WEEKLY).coverage_id == "c:o1"
    assert state.waiting_for("A", Q.FIVE_HOUR) == Q.WAIT_USAGE_REFRESH
    assert state.coverable_axes("A", "o1") == ()
    assert _oauth(state, "o2", "2026-11-01T00:03:00Z")
    assert state.axis("A", Q.FIVE_HOUR).coverage_id == "c:o2"
    assert state.axis("A", Q.WEEKLY).coverage_id == "c:o1"
    assert not _oauth(state, "o3", "2026-11-01T00:04:00Z")


def test_a_completion_naming_a_stale_observation_or_generation_is_invalid():
    state = _paused_state()
    r = reading("o1", at="2026-11-01T00:02:00Z", week_end="2026-11-07T00:00:00Z",
                five_end="2026-11-01T06:00:00Z")
    state.note_clock(r.capture_epoch)
    state.evaluate_reading("A", r)
    state.on_control_witness("A", Q.ControlWitness(
        "o1", r.capture_epoch, frozenset(Q.AXES), r.weekly_end_epoch))
    assert not state.on_authority_completion(
        "A", "c", generation=state.generation - 1, observation_id="o1",
        axes=Q.AXES)
    assert not state.on_authority_completion(
        "A", "c", generation=state.generation, observation_id="other",
        axes=Q.AXES)


def _released_five_hour():
    state = _paused_state()
    assert _oauth(state, "o1", "2026-11-01T00:02:00Z")
    gates = state.evaluate_reading("A", reading(
        "w06", at="2026-11-01T06:00:10Z", week_end="2026-11-07T00:00:00Z",
        five_end="2026-11-01T11:00:00Z"))
    assert state.pending_witness("A", Q.FIVE_HOUR) is None
    gates = state.evaluate_reading("A", reading(
        "w11", at="2026-11-01T11:00:10Z", week_end="2026-11-07T00:00:00Z",
        five_end="2026-11-01T16:00:00Z"))
    # The witness's own derivation runs closed.
    assert not gates[Q.FIVE_HOUR].allowed
    assert state.pending_witness("A", Q.FIVE_HOUR) == "w11"
    assert state.on_release("A", Q.FIVE_HOUR, generation=state.generation,
                            witness_id="w11", completion_id="c:o1",
                            release_id="rel", at="2026-11-01T11:00:11Z")
    return state


def test_a_release_opens_only_its_axis():
    state = _released_five_hour()
    assert state.axis_gate("A", Q.FIVE_HOUR).allowed
    assert not state.axis_gate("A", Q.WEEKLY).allowed
    assert state.waiting_for("A", Q.FIVE_HOUR) is None
    assert state.axis("A", Q.FIVE_HOUR).recovered_at == "2026-11-01T11:00:11Z"


def test_an_x_record_between_witness_and_release_forfeits_the_period():
    state = _paused_state()
    assert _oauth(state, "o1", "2026-11-01T00:02:00Z")
    state.evaluate_reading("A", reading(
        "w06", at="2026-11-01T06:00:10Z", week_end="2026-11-07T00:00:00Z",
        five_end="2026-11-01T11:00:00Z"))
    state.evaluate_reading("A", reading(
        "w11", at="2026-11-01T11:00:10Z", week_end="2026-11-07T00:00:00Z",
        five_end="2026-11-01T16:00:00Z"))
    state.evaluate_reading("A", reading(
        "x", at="2026-11-01T11:00:20Z", week_end="2026-11-07T00:00:00Z",
        five_end="2026-11-01T16:00:00Z"))
    assert state.pending_witness("A", Q.FIVE_HOUR) is None
    assert not state.on_release("A", Q.FIVE_HOUR, generation=state.generation,
                                witness_id="w11", completion_id="c:o1",
                                release_id="rel", at=None)
    # Positive endpoint: the next fresh window witnesses and releases.
    state.evaluate_reading("A", reading(
        "w16", at="2026-11-01T16:00:10Z", week_end="2026-11-07T00:00:00Z",
        five_end="2026-11-01T21:00:00Z"))
    assert state.on_release("A", Q.FIVE_HOUR, generation=state.generation,
                            witness_id="w16", completion_id="c:o1",
                            release_id="rel2", at=None)


def test_a_retained_low_or_held_first_entry_is_not_a_witness():
    state = _paused_state()
    assert _oauth(state, "o1", "2026-11-01T00:02:00Z")
    state.evaluate_reading("A", reading(
        "w06", at="2026-11-01T06:00:10Z", week_end="2026-11-07T00:00:00Z",
        five_end="2026-11-01T11:00:00Z"))
    state.evaluate_reading("A", reading(
        "w11", at="2026-11-01T11:00:10Z", week_end="2026-11-07T00:00:00Z",
        five_end="2026-11-01T16:00:00Z", eligible=()))
    assert state.pending_witness("A", Q.FIVE_HOUR) is None


def test_revocation_on_a_strictly_lower_capture_not_on_an_equal_one():
    state = _released_five_hour()
    equal = reading("eq", at="2026-11-01T11:00:10Z",
                    week_end="2026-11-07T00:00:00Z",
                    five_end="2026-11-01T16:00:00Z")
    state.evaluate_reading("A", equal)
    assert state.released("A", Q.FIVE_HOUR)
    lower = reading("lo", at="2026-11-01T11:05:00Z",
                    captured="2026-11-01T11:00:09Z",
                    week_end="2026-11-07T00:00:00Z",
                    five_end="2026-11-01T16:00:00Z")
    state.evaluate_reading("A", lower)
    assert not state.released("A", Q.FIVE_HOUR)
    rec = state.axis("A", Q.FIVE_HOUR)
    assert rec.revocation == Q.REVOKE_ORDER_REGRESSION
    # Coverage stays: only a new witness in a later period is needed.
    assert state.waiting_for("A", Q.FIVE_HOUR) == Q.WAIT_FRESH_PERIOD


def test_revocation_on_a_backward_period_key():
    state = _released_five_hour()
    state.evaluate_reading("A", reading(
        "back", at="2026-11-01T11:10:00Z", week_end="2026-11-07T00:00:00Z",
        five_end="2026-11-01T11:00:00Z"))
    assert state.axis("A", Q.FIVE_HOUR).revocation == Q.REVOKE_BACKWARD_KEY


def test_an_administrative_revocation_voids_coverage():
    state = _released_five_hour()
    state.revoke_all(Q.REVOKE_ADMINISTRATIVE, void_coverage=True)
    assert not state.released("A", Q.FIVE_HOUR)
    assert state.waiting_for("A", Q.FIVE_HOUR) == Q.WAIT_USAGE_REFRESH
    assert state.waiting_for("A", Q.WEEKLY) == Q.WAIT_USAGE_REFRESH


def test_a_renewed_trigger_voids_every_release_and_coverage():
    state = _released_five_hour()
    state.on_trigger(kind=Q.TRIGGER_KIND_EXCLUDED, reason="claude-observation",
                     at="2026-11-01T12:00:00Z")
    assert not state.released("A", Q.FIVE_HOUR)
    assert state.axis("A", Q.FIVE_HOUR).coverage_id is None
    # The earlier cohort stays (expiry judged against the latest spool
    # cohort, already expired), so only coverage is awaited.
    assert state.waiting_for("A", Q.FIVE_HOUR) == Q.WAIT_USAGE_REFRESH


def test_not_before_is_s7_end_and_the_detection_floor():
    state = _paused_state()
    assert state.not_before("A", Q.FIVE_HOUR) == T1_COHORT.u5_raw
    assert state.not_before("A", Q.WEEKLY) == T1_COHORT.u7_end


def test_not_before_is_computed_before_a_pending_witness():
    """R2-1: a pending witness's own values must not postpone the
    advertised bound past a release the next writer can resume."""
    state = _paused_state()
    assert _oauth(state, "o1", "2026-11-01T00:02:00Z")
    for at in ("2026-11-07T00:05:00Z", "2026-11-13T23:59:00Z"):
        state.evaluate_reading("A", reading(at, at=at,
                                            week_end="2026-11-14T00:00:00Z"))
    assert state.not_before("A", Q.WEEKLY) == B
    state.evaluate_reading("A", reading(
        "w", at=_iso(B + 120), week_end="2026-11-21T00:00:00Z"))
    assert state.pending_witness("A", Q.WEEKLY) == "w"
    assert state.merged("A").weekly.s7_end > B
    assert state.not_before("A", Q.WEEKLY) == B


def test_an_episode_keeps_every_axis_paused_until_a_journaled_close():
    state = Q.QuarantineState()
    state.note_admitted(_epoch("2026-11-01T00:00:00Z"))
    state.on_trigger(kind=Q.TRIGGER_KIND_LEGACY_SPOOL,
                     reason="inventory-incomplete", segment="s", offset=10,
                     at="2026-11-01T00:00:00Z")
    state.on_episode_open(segment="s", offset=10, at="2026-11-01T00:00:00Z")
    assert state.waiting_for("A", Q.WEEKLY) == Q.WAIT_INVENTORY
    # A revision op naming the open episode continues it.
    assert state.on_episode_continue(segment="s", offset=10)
    generation = state.generation
    # A completion naming another episode closes nothing.
    assert not state.on_episode_close(segment="s", offset=50, at=None,
                                      episode=("s", 11))
    state.note_admitted(_epoch("2026-11-01T03:00:00Z"))
    assert state.on_episode_close(segment="s", offset=60,
                                  at="2026-11-01T03:00:00Z", episode=("s", 10))
    state.set_position(normalize_week_end)
    assert state.generation == generation
    assert state.waiting_for("A", Q.WEEKLY) == Q.WAIT_EXPIRY
    assert _iso(state.cohort.h) == "2026-11-01T03:00:00Z"


def test_bounds_are_monotone_and_order_independent():
    a = reading("a", at="2026-11-01T00:00:00Z", week_end="2026-11-07T00:00:00Z",
                five_end="2026-11-01T05:00:00Z")
    b = reading("b", at="2026-11-02T00:00:00Z", week_end="2026-11-07T00:00:00Z",
                five_end="2026-11-02T05:00:00Z")
    assert weekly_of(a, b) == weekly_of(b, a)
    assert five_of(a, b) == five_of(b, a)
    assert Q.WeeklyBounds.merge(weekly_of(a), weekly_of(b)) == weekly_of(a, b)


def test_documents_round_trip_and_unverifiable_ones_are_refused():
    w = _t1_weekly_state()
    assert Q.WeeklyBounds.from_document(w.to_document()) == w
    f = _t1_five_bounds()
    assert Q.FiveHourBounds.from_document(f.to_document()) == f
    assert Q.WeeklyBounds.from_document({"s7Date": 1}) is None
    assert Q.FiveHourBounds.from_document(None) is None
    assert Q.Cohort.from_document({"h": "x"}) is None


def test_unverifiable_axis_rows_read_as_paused():
    state = _released_five_hour()
    row = Q.axis_row(state, "A", Q.FIVE_HOUR)
    assert Q.axis_from_row(row).state == Q.STATE_RELEASED
    # A released row missing its release or witness is not clean.
    broken = (Q.STATE_RELEASED, None, "c", None, None, None, None, None, None,
              None)
    assert Q.axis_from_row(broken) is None
    assert Q.axis_from_row(("bogus",) + row[1:]) is None
