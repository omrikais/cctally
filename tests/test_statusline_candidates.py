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


def test_higher_same_window_candidate_cancels_pending_drop():
    first = _reduce([_weekly_candidate("a", 20, NOW)])
    cancelled = _reduce([
        _weekly_candidate("a", 20, NOW + 1),
        _weekly_candidate("b", 50, NOW + 1),
    ], control=first.control, now=NOW + 1)
    assert cancelled.action == "WRITE_CONTROL"
    assert cancelled.control.pending_drops["sevenDay"] is None


def test_lower_only_rise_restarts_pending_generation():
    first = _reduce([
        _weekly_candidate("a", 20, NOW),
        _weekly_candidate("b", 20, NOW),
    ])
    restarted = _reduce([
        _weekly_candidate("a", 21, NOW + 1),
        _weekly_candidate("b", 21, NOW + 1),
    ], control=first.control, now=NOW + 1)
    assert restarted.action == "WRITE_CONTROL"
    pending = restarted.control.pending_drops["sevenDay"]
    assert pending is not None
    assert pending.reduced_percent == 21
    assert {c.baseline_received_at for c in pending.contributors.values()} == {NOW + 1}


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


def test_pending_drop_is_cancelled_when_every_contributor_has_expired():
    first = _reduce([_weekly_candidate("a", 20, NOW)])
    assert first.action == "WRITE_CONTROL"
    got = _reduce([], control=first.control, now=NOW + 91)
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
