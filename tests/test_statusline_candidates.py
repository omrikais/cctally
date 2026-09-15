"""Pure schema and candidate-reduction contracts for issue #318."""
from __future__ import annotations

import json

import pytest

import _lib_statusline_candidates as lib


NOW = 1_000


def _plausible(axis: str, epoch: int) -> bool:
    return axis in {"fiveHour", "sevenDay"} and 0 < epoch < 10_000_000


def _candidate(**overrides):
    doc = {
        "schemaVersion": 1,
        "receivedAt": NOW,
        "sevenDay": {"percent": 20.5, "resetsAt": 500_000},
    }
    doc.update(overrides)
    return doc


def test_candidate_schema_accepts_decimal_percent():
    got = lib.validate_candidate_document(
        _candidate(), now_epoch=NOW, reset_is_plausible=_plausible
    )
    assert got.seven_day is not None
    assert got.seven_day.percent == 20.5
    assert got.seven_day.raw_resets_at == 500_000


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1, 101])
def test_candidate_schema_rejects_invalid_percent(value):
    doc = _candidate(sevenDay={"percent": value, "resetsAt": 500_000})
    with pytest.raises(lib.StateValidationError):
        lib.validate_candidate_document(
            doc, now_epoch=NOW, reset_is_plausible=_plausible
        )


@pytest.mark.parametrize(
    "doc",
    [
        _candidate(schemaVersion=True),
        _candidate(receivedAt=True),
        _candidate(receivedAt=NOW + 6),
        _candidate(sevenDay={"percent": 20, "resetsAt": True}),
        _candidate(sevenDay={"percent": 20, "resetsAt": 0}),
        _candidate(extra="nope"),
        {"schemaVersion": 1, "receivedAt": NOW},
        _candidate(sevenDay={"percent": 20, "resetsAt": 500_000, "extra": 1}),
    ],
)
def test_candidate_schema_rejects_noncanonical_shape(doc):
    with pytest.raises(lib.StateValidationError):
        lib.validate_candidate_document(
            doc, now_epoch=NOW, reset_is_plausible=_plausible
        )


def test_candidate_json_loader_rejects_standard_constants_and_oversize():
    with pytest.raises(lib.StateValidationError):
        lib.load_candidate_document(
            '{"schemaVersion":1,"receivedAt":1000,"sevenDay":'
            '{"percent":NaN,"resetsAt":500000}}',
            now_epoch=NOW,
            reset_is_plausible=_plausible,
        )
    with pytest.raises(lib.StateValidationError):
        lib.load_candidate_document(
            json.dumps(_candidate()) + (" " * lib.CANDIDATE_DOCUMENT_MAX_BYTES),
            now_epoch=NOW,
            reset_is_plausible=_plausible,
        )


def test_inflight_tombstone_retains_prior_cutoff():
    got = lib.validate_tombstone_document(
        {
            "schemaVersion": 1,
            "axis": "sevenDay",
            "state": "inflight",
            "startedAt": NOW,
            "priorBlockReceivedAtThrough": 900,
        },
        expected_axis="sevenDay",
        now_epoch=NOW,
    )
    assert got.prior_block_received_at_through == 900
    assert got.block_received_at_through is None


@pytest.mark.parametrize(
    "doc",
    [
        {
            "schemaVersion": 1,
            "axis": "fiveHour",
            "state": "committed",
            "blockReceivedAtThrough": NOW + 6,
        },
        {
            "schemaVersion": 1,
            "axis": "sevenDay",
            "state": "inflight",
            "startedAt": NOW,
            "priorBlockReceivedAtThrough": NOW + 6,
        },
        {
            "schemaVersion": 1,
            "axis": "sevenDay",
            "state": "committed",
            "blockReceivedAtThrough": True,
        },
        {
            "schemaVersion": 1,
            "axis": "sevenDay",
            "state": "committed",
            "blockReceivedAtThrough": NOW,
            "startedAt": NOW,
        },
    ],
)
def test_tombstone_schema_rejects_wrong_axis_future_cutoff_and_mixed_state(doc):
    with pytest.raises(lib.StateValidationError):
        lib.validate_tombstone_document(
            doc, expected_axis="sevenDay", now_epoch=NOW
        )


def _control_doc(*, pending=None):
    return {
        "schemaVersion": 1,
        "dbProjection": {"fiveHour": None, "sevenDay": None},
        "dbFiles": {
            "main": {"device": 1, "inode": 2, "size": 3, "mtimeNs": 4},
            "wal": None,
        },
        "pendingDrops": {"fiveHour": None, "sevenDay": pending},
    }


def test_control_schema_rejects_bad_contributor_token_retry_counter_and_future_cutoff():
    base = {
        "canonicalKey": 500_000,
        "reducedPercent": 20,
        "firstSeenAt": NOW,
        "kernelStage": "settling",
        "attempts": 0,
        "contributors": {
            "not-a-token": {"baselineReceivedAt": NOW, "satisfied": False},
        },
        "retrySignature": None,
    }
    with pytest.raises(lib.StateValidationError):
        lib.validate_control_document(_control_doc(pending=base), now_epoch=NOW)

    base["contributors"] = {
        "a" * 64: {"baselineReceivedAt": NOW, "satisfied": False},
    }
    base["attempts"] = 3
    with pytest.raises(lib.StateValidationError):
        lib.validate_control_document(_control_doc(pending=base), now_epoch=NOW)

    base["attempts"] = 0
    base["firstSeenAt"] = NOW + 6
    with pytest.raises(lib.StateValidationError):
        lib.validate_control_document(_control_doc(pending=base), now_epoch=NOW)


def _token(letter: str) -> str:
    return letter * 64


def _weekly_candidate(letter: str, percent: float, received_at: int, key: int = 500_000):
    return lib.Candidate(
        token=_token(letter),
        received_at=received_at,
        five_hour=None,
        seven_day=lib.AxisValue(percent, key, canonical_key=key),
    )


def _mixed_candidate(
    letter: str,
    *,
    received_at: int,
    five: tuple[float, int] | None = None,
    seven: tuple[float, int] | None = None,
):
    return lib.Candidate(
        token=_token(letter),
        received_at=received_at,
        five_hour=(
            lib.AxisValue(five[0], five[1], canonical_key=five[1]) if five else None
        ),
        seven_day=(
            lib.AxisValue(seven[0], seven[1], canonical_key=seven[1]) if seven else None
        ),
    )


def _projection(percent: float | None = 50, key: int = 500_000):
    seven = None
    if percent is not None:
        seven = lib.AxisProjection(
            percent=percent,
            raw_resets_at=key,
            canonical_key=key,
            captured_at=NOW - 1,
            source="statusline",
            reset_generation=0,
        )
    return lib.DbProjection(five_hour=None, seven_day=seven)


def _state(*, pending=None):
    return lib.ControlState(
        db_projection=_projection(),
        pending_drops={"fiveHour": None, "sevenDay": pending},
    )


def _reduce(candidates, *, db=None, control=None, tombstones=None, now=NOW):
    return lib.reduce_candidates(
        candidates,
        db=_projection() if db is None else db,
        control=_state() if control is None else control,
        tombstones={"fiveHour": None, "sevenDay": None} if tombstones is None else tombstones,
        now_epoch=now,
    )


def _canonicalize(raw: int, prior: tuple[int, int] | None) -> int:
    if prior is not None and abs(raw - prior[0]) < 600:
        return prior[1]
    return (raw // 600) * 600


def test_rolling_clusters_old_window_and_new_boundary_straddle():
    axes = [
        lib.AxisValue(70.0, 10_000),
        lib.AxisValue(41.0, 20_399),
        lib.AxisValue(55.0, 20_400),
    ]
    got = lib.canonicalize_five_hour_axes(
        list(reversed(axes)), db_anchor=(10_000, 9_600), canonicalize=_canonicalize
    )
    current = [axis for axis in got if axis.raw_resets_at >= 20_399]
    assert len({axis.canonical_key for axis in current}) == 1
    assert max(axis.percent for axis in current) == 55.0


def test_reduction_chooses_newer_anchor_before_lower_percent_and_is_order_independent():
    db = _projection(50, key=500_000)
    older = _weekly_candidate("a", 90, NOW, key=500_000)
    newer = _weekly_candidate("b", 20, NOW, key=600_000)
    left = _reduce([older, newer], db=db)
    right = _reduce([newer, older], db=db)
    assert left.action == right.action == "PUBLISH_DB"
    assert left.plan == right.plan
    assert left.plan is not None
    assert left.plan.seven_day is not None
    assert left.plan.seven_day.percent == 20
    assert left.plan.seven_day.canonical_key == 600_000


def test_reduction_keeps_axes_independent():
    db = lib.DbProjection(
        five_hour=lib.AxisProjection(10, 200, 200, NOW - 1, "statusline", 0),
        seven_day=lib.AxisProjection(20, 500_000, 500_000, NOW - 1, "statusline", 0),
    )
    got = _reduce(
        [
            _mixed_candidate("a", received_at=NOW, five=(30, 300)),
            _mixed_candidate("b", received_at=NOW, seven=(24, 500_000)),
        ],
        db=db,
        control=lib.ControlState(db_projection=db, pending_drops={"fiveHour": None, "sevenDay": None}),
    )
    assert got.action == "PUBLISH_DB"
    assert got.plan is not None
    assert got.plan.five_hour is not None and got.plan.five_hour.percent == 30
    assert got.plan.seven_day is not None and got.plan.seven_day.percent == 24


def test_equal_window_drop_freezes_then_confirms_contributor_baselines():
    first = _reduce([
        _weekly_candidate("a", 20, NOW),
        _weekly_candidate("b", 20, NOW),
    ])
    assert first.action == "WRITE_CONTROL"
    pending = first.control.pending_drops["sevenDay"]
    assert pending is not None
    assert {c.baseline_received_at for c in pending.contributors.values()} == {NOW}

    staggered = _reduce([
        _weekly_candidate("a", 20, NOW + 1),
        _weekly_candidate("b", 20, NOW),
    ], control=first.control, now=NOW + 1)
    assert staggered.action == "WRITE_CONTROL"
    retained = staggered.control.pending_drops["sevenDay"]
    assert retained is not None
    assert retained.contributors[_token("a")].baseline_received_at == NOW

    confirmed = _reduce([
        _weekly_candidate("a", 20, NOW + 1),
        _weekly_candidate("b", 20, NOW + 2),
    ], control=staggered.control, now=NOW + 2)
    assert confirmed.action == "PUBLISH_DB"
    assert confirmed.plan is not None and confirmed.plan.seven_day is not None
    assert confirmed.plan.seven_day.percent == 20


def test_a_dissenting_contributor_at_the_baseline_does_not_erase_the_evidence():
    """#755: this contract used to say the opposite, and that WAS the stall.

    `_reduced_candidate` selects the maximum, so a contributor still reporting
    the database value made the reduction equal the database and cleared the
    pending drop before any consensus could be reached. The maximum decides
    what is displayed; it may not erase the accumulated evidence.
    """
    first = _reduce([_weekly_candidate("a", 20, NOW)])
    kept = _reduce([
        _weekly_candidate("a", 20, NOW + 1),
        _weekly_candidate("b", 50, NOW + 1),
    ], control=first.control, now=NOW + 1)
    pending = kept.control.pending_drops["sevenDay"]
    assert pending is not None
    assert pending.first_seen_at == NOW
    assert pending.deadline_at == NOW + lib.PENDING_DROP_DEADLINE_SECONDS
    assert pending.retained is not None and pending.retained.percent == 20


def test_a_supporter_reporting_the_baseline_cancels_the_pending_drop():
    """Cancellation is a supporter actively retracting, and nothing else."""
    first = _reduce([_weekly_candidate("a", 20, NOW)])
    cancelled = _reduce(
        [_weekly_candidate("a", 50, NOW + 1)], control=first.control, now=NOW + 1
    )
    assert cancelled.action == "WRITE_CONTROL"
    assert cancelled.control.pending_drops["sevenDay"] is None


def test_an_upward_correction_updates_one_contributor_and_restarts_nothing():
    """#755: an upward correction used to call `_new_pending`.

    That reset `first_seen_at` and marked every contributor unsatisfied, so a
    single correcting session discarded every other session's progress and, once
    the deadline existed, would have moved it too.
    """
    first = _reduce([
        _weekly_candidate("a", 20, NOW),
        _weekly_candidate("b", 20, NOW),
    ])
    corrected = _reduce([
        _weekly_candidate("a", 21, NOW + 1),
        _weekly_candidate("b", 20, NOW),
    ], control=first.control, now=NOW + 1)
    pending = corrected.control.pending_drops["sevenDay"]
    assert pending is not None
    assert pending.first_seen_at == NOW
    assert pending.deadline_at == NOW + lib.PENDING_DROP_DEADLINE_SECONDS
    assert pending.reduced_percent == 21
    assert pending.contributors[_token("b")].baseline_received_at == NOW
    assert pending.contributors[_token("b")].satisfied is False
    assert pending.retained is not None and pending.retained.percent == 20


def test_a_wholesale_rise_moves_the_retained_low_without_a_new_deadline():
    first = _reduce([
        _weekly_candidate("a", 20, NOW),
        _weekly_candidate("b", 20, NOW),
    ])
    raised = _reduce([
        _weekly_candidate("a", 21, NOW + 1),
        _weekly_candidate("b", 21, NOW + 1),
    ], control=first.control, now=NOW + 1)
    pending = raised.control.pending_drops["sevenDay"]
    assert pending is not None
    assert pending.first_seen_at == NOW
    assert pending.deadline_at == NOW + lib.PENDING_DROP_DEADLINE_SECONDS
    assert pending.reduced_percent == 21
    assert pending.retained is not None and pending.retained.percent == 21


def test_joining_lower_contributor_requires_its_own_later_receipt():
    first = _reduce([_weekly_candidate("a", 20, NOW)])
    joined = _reduce([
        _weekly_candidate("a", 20, NOW + 1),
        _weekly_candidate("b", 20, NOW + 1),
    ], control=first.control, now=NOW + 1)
    assert joined.action == "WRITE_CONTROL"
    pending = joined.control.pending_drops["sevenDay"]
    assert pending is not None
    assert pending.contributors[_token("b")].baseline_received_at == NOW + 1
    confirmed = _reduce([
        _weekly_candidate("a", 20, NOW + 1),
        _weekly_candidate("b", 20, NOW + 2),
    ], control=joined.control, now=NOW + 2)
    assert confirmed.action == "PUBLISH_DB"


def test_tombstone_blocks_only_the_observed_axis_through_future_skew_cutoff():
    db = lib.DbProjection(five_hour=None, seven_day=None)
    control = lib.ControlState(db_projection=db, pending_drops={"fiveHour": None, "sevenDay": None})
    got = _reduce(
        [
            _mixed_candidate("a", received_at=NOW + 5, five=(40, 900), seven=(30, 700_000)),
        ],
        db=db,
        control=control,
        tombstones={
            "fiveHour": None,
            "sevenDay": lib.Tombstone("sevenDay", "committed", block_received_at_through=NOW + 5),
        },
    )
    assert got.action == "PUBLISH_DB"
    assert got.plan is not None
    assert got.plan.five_hour is not None
    assert got.plan.seven_day is None


def test_a_pending_drop_outlives_the_contributors_that_armed_it():
    """#755: the pending drop used to be cleared once its contributors aged out.

    A candidate is active only while `-5 <= now - received_at < 90` while the
    deadline is 180 seconds, so every contributor supporting a pending low can
    age out before that drop's own deadline expires. Clearing on absence made
    the deadline unreachable in exactly the case it exists for.
    """
    first = _reduce([_weekly_candidate("a", 20, NOW)])
    assert first.action == "WRITE_CONTROL"
    aged = _reduce([], control=first.control, now=NOW + 91)
    pending = aged.control.pending_drops["sevenDay"]
    assert pending is not None, "ageing out of the active window is not retraction"
    assert pending.deadline_at == NOW + lib.PENDING_DROP_DEADLINE_SECONDS
    assert pending.retained is not None and pending.retained.percent == 20

    expired = _reduce(
        [], control=aged.control, now=NOW + lib.PENDING_DROP_DEADLINE_SECONDS
    )
    assert expired.action == "PUBLISH_DB"
    assert expired.plan is not None and expired.plan.seven_day is not None
    assert expired.plan.seven_day.percent == 20


def test_an_evidence_free_pending_record_is_reconciled_rather_than_published():
    """A control document written by the previous binary carries no evidence."""
    legacy = lib.PendingDrop(
        canonical_key=500_000,
        reduced_percent=20,
        first_seen_at=NOW,
        kernel_stage="settling",
        attempts=0,
        contributors={},
        retry_signature=None,
        deadline_at=NOW + lib.PENDING_DROP_DEADLINE_SECONDS,
        retained=None,
    )
    got = _reduce(
        [], control=_state(pending=legacy),
        now=NOW + lib.PENDING_DROP_DEADLINE_SECONDS + 1,
    )
    assert got.action == "WRITE_CONTROL"
    assert got.control.pending_drops["sevenDay"] is None


def test_unsupported_drop_retries_twice_then_requires_exact_axis_signature_change():
    first = _reduce([_weekly_candidate("a", 20, NOW)])
    confirmed = _reduce(
        [_weekly_candidate("a", 20, NOW + 1)], control=first.control, now=NOW + 1
    )
    assert confirmed.action == "PUBLISH_DB"
    one = confirmed.control.pending_drops["sevenDay"]
    assert one is not None
    assert (one.kernel_stage, one.attempts) == ("ready", 1)

    second = _reduce(
        [_weekly_candidate("a", 20, NOW + 2)], control=confirmed.control, now=NOW + 2
    )
    assert second.action == "PUBLISH_DB"
    suppressed = second.control.pending_drops["sevenDay"]
    assert suppressed is not None
    assert (suppressed.kernel_stage, suppressed.attempts) == ("suppressed", 2)

    unchanged = _reduce(
        [_weekly_candidate("a", 20, NOW + 3)], control=second.control, now=NOW + 3
    )
    assert unchanged.action == "NOOP"

    other_axis_changed = lib.DbProjection(
        five_hour=lib.AxisProjection(99, 700, 700, NOW + 3, "statusline", 0),
        seven_day=_projection().seven_day,
    )
    still_suppressed = _reduce(
        [_weekly_candidate("a", 20, NOW + 4)],
        db=other_axis_changed,
        control=lib.ControlState(other_axis_changed, second.control.pending_drops),
        now=NOW + 4,
    )
    assert still_suppressed.action == "NOOP"

    rearmed = _reduce(
        [_weekly_candidate("a", 19, NOW + 5)], control=second.control, now=NOW + 5
    )
    assert rearmed.action == "PUBLISH_DB"
    retry = rearmed.control.pending_drops["sevenDay"]
    assert retry is not None
    assert (retry.kernel_stage, retry.attempts) == ("ready", 1)
    assert retry.retry_signature is not None
    assert retry.retry_signature.candidate_percent == 19


def test_zero_drop_requires_a_revalidated_second_kernel_attempt_and_can_cancel():
    first = _reduce([_weekly_candidate("a", 0, NOW)])
    armed = _reduce(
        [_weekly_candidate("a", 0, NOW + 1)], control=first.control, now=NOW + 1
    )
    assert armed.action == "PUBLISH_DB"
    pending = armed.control.pending_drops["sevenDay"]
    assert pending is not None
    assert (pending.kernel_stage, pending.attempts) == ("zero_armed", 1)

    cancelled = _reduce(
        [_weekly_candidate("a", 50, NOW + 2)], control=armed.control, now=NOW + 2
    )
    assert cancelled.action == "WRITE_CONTROL"
    assert cancelled.control.pending_drops["sevenDay"] is None

    retried = _reduce(
        [_weekly_candidate("a", 0, NOW + 2)], control=armed.control, now=NOW + 2
    )
    assert retried.action == "PUBLISH_DB"
    retry = retried.control.pending_drops["sevenDay"]
    assert retry is not None
    assert (retry.kernel_stage, retry.attempts) == ("suppressed", 2)


def test_persisted_integer_validation_is_signed_i64_across_artifacts():
    maximum = 2**63 - 1
    minimum = -(2**63)
    assert lib.validate_candidate_document(
        {
            "schemaVersion": 1,
            "receivedAt": maximum,
            "sevenDay": {"percent": 20, "resetsAt": minimum},
        },
        now_epoch=maximum,
        reset_is_plausible=lambda _axis, _epoch: True,
    ).seven_day is not None

    control = _control_doc()
    control["dbProjection"]["sevenDay"] = {
        "percent": 20,
        "rawResetsAt": minimum,
        "canonicalKey": maximum,
        "capturedAt": maximum,
        "source": "statusline",
        "resetGeneration": maximum,
    }
    control["dbFiles"]["main"] = {
        "device": maximum, "inode": maximum, "size": maximum, "mtimeNs": maximum,
    }
    assert lib.validate_control_document(control, now_epoch=maximum).db_projection.seven_day
    assert lib.validate_tombstone_document(
        {"schemaVersion": 1, "axis": "sevenDay", "state": "committed",
         "blockReceivedAtThrough": maximum},
        expected_axis="sevenDay", now_epoch=maximum,
    ).block_received_at_through == maximum

    too_large = maximum + 1
    for value in (too_large, minimum - 1):
        with pytest.raises(lib.StateValidationError):
            lib.validate_candidate_document(
                {"schemaVersion": 1, "receivedAt": 0,
                 "sevenDay": {"percent": 20, "resetsAt": value}},
                now_epoch=maximum, reset_is_plausible=lambda _axis, _epoch: True,
            )
        corrupted = _control_doc()
        corrupted["dbFiles"]["main"]["inode"] = value
        with pytest.raises(lib.StateValidationError):
            lib.validate_control_document(corrupted, now_epoch=maximum)
        with pytest.raises(lib.StateValidationError):
            lib.validate_tombstone_document(
                {"schemaVersion": 1, "axis": "sevenDay", "state": "committed",
                 "blockReceivedAtThrough": value},
                expected_axis="sevenDay", now_epoch=maximum,
            )


# --- #755: bounded drop consensus ------------------------------------------


def _five_and_seven_projection(*, five_percent=10.0, seven_percent=50.0,
                               five_key=900, seven_key=500_000):
    return lib.DbProjection(
        five_hour=lib.AxisProjection(
            percent=five_percent, raw_resets_at=five_key, canonical_key=five_key,
            captured_at=NOW - 1, source="statusline", reset_generation=0,
        ),
        seven_day=lib.AxisProjection(
            percent=seven_percent, raw_resets_at=seven_key, canonical_key=seven_key,
            captured_at=NOW - 1, source="statusline", reset_generation=0,
        ),
    )


def test_a_weekly_deadline_survives_its_axis_losing_every_candidate():
    """Preservation is per axis and per physical window, not per spool.

    The five-hour axis keeps reporting throughout, so the spool is never empty
    and evaluations keep running, while the weekly axis loses every candidate
    and regains one before its original deadline.
    """
    db = _five_and_seven_projection()
    control = lib.ControlState(db, {"fiveHour": None, "sevenDay": None})
    armed = _reduce(
        [_mixed_candidate("a", received_at=NOW, five=(10.0, 900), seven=(20.0, 500_000))],
        db=db, control=control,
    )
    pending = armed.control.pending_drops["sevenDay"]
    assert pending is not None and pending.deadline_at == NOW + 180

    control = armed.control
    for offset in (30, 60, 90, 120):
        got = _reduce(
            [_mixed_candidate("b", received_at=NOW + offset, five=(10.0, 900))],
            db=db, control=control, now=NOW + offset,
        )
        control = got.control
        held = control.pending_drops["sevenDay"]
        assert held is not None, "an axis with no candidate is not a retracted axis"
        assert held.deadline_at == NOW + 180

    regained = _reduce(
        [_mixed_candidate("c", received_at=NOW + 150, five=(10.0, 900), seven=(20.0, 500_000))],
        db=db, control=control, now=NOW + 150,
    )
    back = regained.control.pending_drops["sevenDay"]
    assert back is not None
    assert back.first_seen_at == NOW
    assert back.deadline_at == NOW + 180, "regaining a candidate established a new deadline"


def test_an_inflight_tombstone_does_not_retract_a_pending_drop():
    """Eligibility also removes tombstoned values, and that is not a retraction.

    An in-flight tombstone means an authoritative writer is repairing the axis.
    It suppresses every candidate on that axis, and the authoritative path
    clears the pending drop itself once it succeeds.
    """
    first = _reduce([_weekly_candidate("a", 20, NOW)])
    blocked = _reduce(
        [_weekly_candidate("a", 20, NOW + 1)],
        control=first.control,
        tombstones={
            "fiveHour": None,
            "sevenDay": lib.Tombstone("sevenDay", "inflight", started_at=NOW + 1),
        },
        now=NOW + 1,
    )
    pending = blocked.control.pending_drops["sevenDay"]
    assert pending is not None and pending.deadline_at == NOW + 180


def test_expiry_publishes_from_retained_evidence_after_its_supporters_age_out():
    """The 90-second active window is shorter than the 180-second deadline."""
    first = _reduce([
        _weekly_candidate("a", 50, NOW),
        _weekly_candidate("b", 0, NOW),
    ])
    pending = first.control.pending_drops["sevenDay"]
    assert pending is not None and pending.retained.percent == 0

    control = first.control
    for offset in (30, 60, 95, 130, 170):
        control = _reduce(
            [_weekly_candidate("a", 50, NOW + offset)],
            control=control, now=NOW + offset,
        ).control
        held = control.pending_drops["sevenDay"]
        assert held is not None and held.retained.percent == 0

    expired = _reduce(
        [_weekly_candidate("a", 50, NOW + 180)], control=control, now=NOW + 180
    )
    assert expired.action == "PUBLISH_DB"
    assert expired.plan.seven_day.percent == 0, (
        "expiry published the all-contributor maximum instead of the retained low"
    )


def test_sustained_membership_churn_never_advances_the_deadline():
    control = _reduce([_weekly_candidate("a", 20, NOW)]).control
    letters = "cdefghijklmnopqr"
    for index, offset in enumerate(range(10, 180, 10)):
        control = _reduce(
            [_weekly_candidate(letters[index % len(letters)], 20, NOW + offset)],
            control=control, now=NOW + offset,
        ).control
        pending = control.pending_drops["sevenDay"]
        assert pending is not None
        assert pending.deadline_at == NOW + 180, f"the deadline moved at +{offset}s"


def test_a_replayed_observation_is_not_a_second_confirmation():
    """Identity is content-derived, so re-reading one reading adds nothing.

    Receipt time cannot express this: another render of unchanged upstream data
    carries a new receipt time and is still the same observation.
    """
    first = _reduce([
        _weekly_candidate("a", 50, NOW),
        _weekly_candidate("b", 0, NOW),
    ])
    retained = first.control.pending_drops["sevenDay"].retained
    assert len(retained.observations) == 1

    again = _reduce([
        _weekly_candidate("a", 50, NOW + 1),
        _weekly_candidate("b", 0, NOW + 1),
    ], control=first.control, now=NOW + 1)
    repeated = again.control.pending_drops["sevenDay"].retained
    assert len(repeated.observations) == 1
    assert repeated.observations[0].received_at == NOW
    assert repeated.observations[0].observation_id == retained.observations[0].observation_id

    moved = _reduce([
        _weekly_candidate("a", 50, NOW + 2),
        _weekly_candidate("b", 1, NOW + 2),
    ], control=again.control, now=NOW + 2)
    distinct = moved.control.pending_drops["sevenDay"].retained
    assert distinct.percent == 1
    assert len(distinct.observations) == 1
    assert distinct.observations[0].observation_id != retained.observations[0].observation_id


def test_the_control_document_version_is_separate_from_the_spool_version():
    """Bumping the control shape must not invalidate peer spool documents."""
    assert lib.CONTROL_SCHEMA_VERSION == 2
    assert lib.SCHEMA_VERSION == 1
    assert lib.CONTROL_SCHEMA_VERSIONS_READ == (1, 2)

    base = {
        "canonicalKey": 500_000,
        "reducedPercent": 20,
        "firstSeenAt": NOW,
        "kernelStage": "settling",
        "attempts": 0,
        "contributors": {},
        "retrySignature": None,
    }
    # A version-1 document parses, and its pending drops do not survive the
    # read. The record carries neither the deadline nor the evidence, and
    # neither can be recovered: `firstSeenAt` was stamped under the pre-#755
    # arming rule, so it does not mean "when this low was first seen", and
    # inventing evidence for a record that never carried any is exactly what
    # this section forbids. The projection is what the document is read FOR.
    legacy = _control_doc(pending=dict(base))
    parsed = lib.validate_control_document(legacy, now_epoch=NOW)
    assert parsed.pending_drops["sevenDay"] is None
    assert parsed.pending_drops["fiveHour"] is None

    # The shape is still validated, so a malformed version-1 record is refused
    # rather than silently dropped.
    malformed = _control_doc(pending=dict(base, kernelStage="nonsense"))
    with pytest.raises(lib.StateValidationError):
        lib.validate_control_document(malformed, now_epoch=NOW)

    current = _control_doc(pending=dict(base, deadlineAt=NOW + 180, retained=None))
    current["schemaVersion"] = 2
    assert lib.validate_control_document(current, now_epoch=NOW) is not None

    # A version-2 document is required to carry both fields.
    missing = _control_doc(pending=dict(base))
    missing["schemaVersion"] = 2
    with pytest.raises(lib.StateValidationError):
        lib.validate_control_document(missing, now_epoch=NOW)

    # A deadline earlier than the instant it is measured from is refused.
    backwards = _control_doc(pending=dict(base, deadlineAt=NOW - 1, retained=None))
    backwards["schemaVersion"] = 2
    with pytest.raises(lib.StateValidationError):
        lib.validate_control_document(backwards, now_epoch=NOW)


def test_a_spent_attempt_after_expiry_terminates_the_pending_record():
    """The bounded attempts are spent, so nothing can advance this record.

    `_hold_pending` already clears in exactly this state. The armed path kept
    the record instead, and an unsupported drop the recording policy rejects
    then sat armed for ever: its contributor set churned as sessions opened and
    closed, rewriting the control document at statusline cadence, and
    `_pending_kernel_attempt` re-fired the whole two-attempt cycle whenever the
    retry signature changed, republishing retained evidence that was by then
    arbitrarily old.
    """
    control = _reduce([
        _weekly_candidate("a", 20, NOW),
        _weekly_candidate("b", 50, NOW),
    ]).control
    armed = control.pending_drops["sevenDay"]
    assert armed is not None and armed.first_seen_at == NOW

    observed = []
    for offset in (180, 190, 200):
        got = _reduce([
            _weekly_candidate("a", 20, NOW + offset),
            _weekly_candidate("b", 50, NOW + offset),
        ], control=control, now=NOW + offset)
        control = got.control
        pending = control.pending_drops["sevenDay"]
        observed.append((
            got.action,
            None if pending is None else (pending.kernel_stage, pending.attempts),
        ))

    assert observed[0] == ("PUBLISH_DB", ("ready", 1))
    assert observed[1] == ("PUBLISH_DB", ("suppressed", 2))
    assert observed[2] == ("WRITE_CONTROL", None), (
        "the record outlived its own spent attempts, so it kept rewriting the "
        "control document and could re-fire on any later signature change"
    )


def test_a_report_above_the_baseline_does_not_discard_the_pending_drop():
    """D5's deadline is not extendable, and an upward report used to extend it.

    A contributor reporting above the current database value made the reduction
    exceed the baseline, and that branch published the rise and threw the
    pending drop away. The database then moved up, the next tick armed a fresh
    record, and a peer that keeps reporting above the current value restarted
    the clock indefinitely. That is the stall family: a session reporting a real
    reset must not be erased by a peer reporting a higher number.

    The retained low keeps its meaning across the rise, because the baseline
    only moved UP: a low armed below the old baseline is still below the new
    one, so it is still a drop, measured against whichever baseline the record
    is judged against at expiry.
    """
    first = _reduce([_weekly_candidate("a", 0, NOW)])
    armed = first.control.pending_drops["sevenDay"]
    assert armed is not None and armed.deadline_at == NOW + 180

    rise = _reduce([
        _weekly_candidate("a", 0, NOW + 1),
        _weekly_candidate("b", 70, NOW + 1),
    ], control=first.control, now=NOW + 1)
    assert rise.action == "PUBLISH_DB"
    assert rise.plan is not None and rise.plan.seven_day is not None
    assert rise.plan.seven_day.percent == 70, "the rise itself must still publish"
    kept = rise.control.pending_drops["sevenDay"]
    assert kept is not None, "an upward report discarded another session's reset"
    assert kept.first_seen_at == NOW
    assert kept.deadline_at == NOW + 180, "an upward report advanced the deadline"
    assert kept.retained is not None and kept.retained.percent == 0

    raised = _projection(percent=70)
    expired = _reduce(
        [_weekly_candidate("b", 70, NOW + 180)],
        db=raised,
        control=lib.ControlState(raised, rise.control.pending_drops),
        now=NOW + 180,
    )
    assert expired.action == "PUBLISH_DB"
    assert expired.plan is not None and expired.plan.seven_day is not None
    assert expired.plan.seven_day.percent == 0, (
        "the drop armed against the old baseline never published against the new one"
    )


def test_a_supporter_that_reports_the_rise_itself_cancels_the_drop():
    """Preservation across a rise is not preservation against its own supporter."""
    first = _reduce([_weekly_candidate("a", 0, NOW)])
    assert first.control.pending_drops["sevenDay"] is not None

    retracted = _reduce(
        [_weekly_candidate("a", 70, NOW + 1)], control=first.control, now=NOW + 1
    )
    assert retracted.action == "PUBLISH_DB"
    assert retracted.plan.seven_day.percent == 70
    assert retracted.control.pending_drops["sevenDay"] is None


def test_cancellation_is_decided_over_the_supporters_that_are_present():
    """Absence is silence; presence at the baseline is retraction.

    Requiring EVERY retained supporter to be present made one absent supporter
    veto a live retraction: the peer that had ended and aged out reported
    nothing, and its silence outweighed the session that came back and actively
    said the database value was right.
    """
    armed = _reduce([
        _weekly_candidate("a", 0, NOW),
        _weekly_candidate("b", 0, NOW),
    ])
    supporters = armed.control.pending_drops["sevenDay"].retained.observations
    assert {item.token for item in supporters} == {_token("a"), _token("b")}

    # Every supporter present and back at the baseline: cancelled.
    both_back = _reduce([
        _weekly_candidate("a", 50, NOW + 1),
        _weekly_candidate("b", 50, NOW + 1),
    ], control=armed.control, now=NOW + 1)
    assert both_back.control.pending_drops["sevenDay"] is None

    # One present and retracted while another is present and still low: kept,
    # because live evidence still supports the drop.
    one_low = _reduce([
        _weekly_candidate("a", 50, NOW + 1),
        _weekly_candidate("b", 0, NOW + 1),
    ], control=armed.control, now=NOW + 1)
    kept = one_low.control.pending_drops["sevenDay"]
    assert kept is not None and kept.retained.percent == 0

    # One present and retracted while the other has aged out: cancelled. The
    # absent supporter is not treated as retracting; it is simply not counted.
    aged_out = _reduce(
        [_weekly_candidate("a", 50, NOW + 100)], control=armed.control, now=NOW + 100
    )
    assert aged_out.control.pending_drops["sevenDay"] is None, (
        "a live retraction was ignored because a peer supporter had aged out"
    )


def test_an_empty_retained_record_is_discarded_by_both_readers():
    """`_hold_pending` and `_reduce_axis` must agree about evidence-free state.

    A retained low with no observations supports nothing. `_retained_low`
    accepts one, `_supporters_retracted` read the empty supporter set as
    unanimous retraction and cancelled, and `_hold_pending` skipped that check
    and would have published the unsupported value at expiry. Only a
    hand-crafted control document reaches this, but the two readers must not
    disagree about what it means.
    """
    hollow = lib.PendingDrop(
        canonical_key=500_000,
        reduced_percent=20,
        first_seen_at=NOW,
        kernel_stage="settling",
        attempts=0,
        contributors={},
        retry_signature=None,
        deadline_at=NOW + lib.PENDING_DROP_DEADLINE_SECONDS,
        retained=lib.RetainedLow(
            percent=20, raw_resets_at=500_000, canonical_key=500_000, observations=()
        ),
    )
    expired = _reduce([], control=_state(pending=hollow), now=NOW + 181)
    assert expired.action == "WRITE_CONTROL", (
        "an evidence-free record published a value nothing supports"
    )
    assert expired.control.pending_drops["sevenDay"] is None

    live = _reduce(
        [_weekly_candidate("a", 20, NOW + 181)],
        control=_state(pending=hollow), now=NOW + 181,
    )
    assert live.action != "PUBLISH_DB"
    rearmed = live.control.pending_drops["sevenDay"]
    assert rearmed is not None, "the hollow record cancelled a live drop"
    assert rearmed.first_seen_at == NOW + 181
    assert rearmed.retained is not None and rearmed.retained.observations
