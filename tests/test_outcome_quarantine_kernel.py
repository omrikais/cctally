"""#825/#832 Task 3.1 — the pure outcome-quarantine kernel (spec 5.6 items 3-5).

``bin/_lib_outcome_quarantine.py`` owns the whole-prefix envelope, the seven
certificate clauses, continuing validity and the deterministic quarantine
fold. Every clause has its own failing variant (case F14(b)), the round-6
whole-prefix trace is pinned, and clause 6 is PROVED implied by clause 1 by
reproducing the detector's own debounce comparisons rather than assumed.
"""
import dataclasses
import datetime as dt
import itertools
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


def reading(oid, *, at, week_end, five_end=None, account="A", captured=None,
            key=None, ineligible=None, stamped=True):
    """A Reading built the way the glue builds one from a journal record."""
    detection = _epoch(at)
    capture = _epoch(captured or at)
    end = _epoch(week_end)
    start = end - 7 * 86400
    raw5 = None if five_end is None else _epoch(five_end)
    if key is None and raw5 is not None:
        key = raw5 - raw5 % 600
    return Q.Reading(
        observation_id=oid, account_key=account,
        captured_at_utc=_iso(capture), capture_epoch=capture,
        detection_epoch=detection,
        week_start_date=dt.datetime.fromtimestamp(start, tz=UTC).date().isoformat(),
        week_start_at_epoch=start, weekly_end_epoch=end,
        five_hour_key=key, raw_five_hour_reset=raw5,
        two_axis=raw5 is not None, ineligible_reason=ineligible,
        stamped=stamped)


def envelope_of(*readings):
    env = Q.Envelope()
    for r in readings:
        env = env.absorb_reading(r)
    return env


# A quiet prefix: a reading in the week ending 2026-10-16 12:00Z.
PREFIX = reading("p", at="2026-10-09T12:00:00Z",
                 week_end="2026-10-16T12:00:00Z",
                 five_end="2026-10-09T17:00:00Z")
# The genuine recovery: a weekly rollover after an idle gap spanning both the
# old week and the old five-hour window end.
WITNESS = reading("w", at="2026-10-16T13:00:00Z",
                  week_end="2026-10-23T13:00:00Z",
                  five_end="2026-10-16T18:00:00Z")


def test_a_rollover_after_an_idle_gap_certifies():
    decision = Q.certify(WITNESS, envelope_of(PREFIX))
    assert decision == Q.CertificateDecision(True, (), None)


@pytest.mark.parametrize("clause, prefix", [
    # 1. A different normalized end sharing the witness's date key.
    (1, reading("x", at="2026-10-16T12:30:00Z",
                week_end="2026-10-23T09:00:00Z",
                five_end="2026-10-16T13:00:00Z")),
    # 2. A prefix raw five-hour reset within 1,800 s of the witness's.
    (2, None),
    # 3. A prefix effective instant at or after the witness's week start.
    (3, None),
    # 4. A prefix capture that sorts lexically at or after the witness's.
    (4, None),
    # 5. A prefix weekly end at or after the detection instant.
    (5, None),
    # 7. A replayed identity.
    (7, None),
])
def test_each_clause_has_its_own_failing_variant(clause, prefix):
    env = envelope_of(PREFIX)
    seen = frozenset()
    witness = WITNESS
    if clause == 1:
        env = envelope_of(PREFIX, prefix)
    elif clause == 2:
        # Detected late in its window, so the near prefix reset is already in
        # the past at detection and clause 5 stays satisfied.
        witness = reading("w", at="2026-10-16T17:55:00Z",
                          week_end="2026-10-23T13:00:00Z",
                          five_end="2026-10-16T18:00:00Z")
        env = Q.Envelope.merge(env, Q.Envelope(
            max_raw_five_hour_reset=witness.raw_five_hour_reset - 1500))
    elif clause == 3:
        env = Q.Envelope.merge(env, Q.Envelope(
            max_effective_instant_epoch=WITNESS.week_start_at_epoch))
    elif clause == 4:
        env = Q.Envelope.merge(env, Q.Envelope(
            max_capture_at_utc=WITNESS.captured_at_utc))
    elif clause == 5:
        env = Q.Envelope.merge(env, Q.Envelope(
            max_weekly_end_epoch=WITNESS.detection_epoch))
    elif clause == 7:
        seen = frozenset({WITNESS.observation_id})
    decision = Q.certify(witness, env, seen_identities=seen)
    assert not decision.certified
    assert clause in decision.failed_clauses
    if clause in (2, 3, 4, 5, 7):
        # Each of these is isolated: nothing else in the envelope moved.
        assert decision.failed_clauses == (clause,)


def test_a_shared_date_key_fails_clause_one_even_with_a_distinct_end():
    other = reading("x", at="2026-10-16T12:30:00Z",
                    week_end="2026-10-23T09:00:00Z")  # same start date
    assert other.week_start_date == WITNESS.week_start_date
    assert other.weekly_end_epoch != WITNESS.weekly_end_epoch
    decision = Q.certify(WITNESS, envelope_of(PREFIX, other))
    assert 1 in decision.failed_clauses


def _detector_debounce_armed(state, incoming):
    """The exact comparison `detect_reset_and_credit` performs on the one
    `weekly_reset_debounce_state` row: armed iff the stored date AND end equal
    the incoming reading's."""
    return (state is not None and state[0] == incoming.week_start_date
            and state[1] == incoming.weekly_end_epoch)


def test_clause_six_is_implied_by_clause_one_by_the_detector_comparisons():
    """Proved, not assumed: for every prefix reading that could have armed the
    debounce state (or established a five-hour confirmation row), a witness
    passing clause 1 is non-matching under the detector's own comparisons."""
    prefix = [
        reading(f"p{i}", at=f"2026-10-0{i}T12:00:00Z",
                week_end=f"2026-10-{i + 7:02d}T{h:02d}:00:00Z",
                five_end=f"2026-10-0{i}T{14 + i}:00:00Z")
        for i, h in zip(range(1, 9), itertools.cycle((9, 12, 13)))
    ]
    env = envelope_of(*prefix)
    witness = reading("w", at="2026-10-20T13:00:00Z",
                      week_end="2026-10-27T13:00:00Z",
                      five_end="2026-10-20T18:00:00Z")
    decision = Q.certify(witness, env)
    assert 1 not in decision.failed_clauses
    for p in prefix:
        armed_state = (p.week_start_date, p.weekly_end_epoch)
        assert not _detector_debounce_armed(armed_state, witness)
        # The confirmation state is keyed (account, window key, source).
        assert p.five_hour_key != witness.five_hour_key
    assert 6 not in decision.failed_clauses
    # And whenever a debounce state COULD match, clause 6 reports it.
    replay = reading("w2", at="2026-10-08T13:00:00Z",
                     week_end="2026-10-15T13:00:00Z",
                     five_end="2026-10-08T18:00:00Z")
    env2 = envelope_of(*prefix, replay)
    assert 6 in Q.certify(replay, env2).failed_clauses


def test_round_six_whole_prefix_trace():
    """Current P at 12:00 (weekly 70 ending October 16 12:00Z, five-hour 70
    ending 17:00); excluded X at 12:01 (weekly 80 ending October 8 12:00Z,
    five-hour 80 ending 12:00); proposed witness R at 12:02 (weekly 10 ending
    October 15 12:00Z, five-hour 10 ending 18:00). The envelope covers BOTH P
    and X — the whole prefix, not a suffix from the trigger — so R fails
    clauses 1 and 5."""
    p = reading("P", at="2026-10-09T12:00:00Z", week_end="2026-10-16T12:00:00Z",
                five_end="2026-10-09T17:00:00Z")
    x = reading("X", at="2026-10-09T12:01:00Z", week_end="2026-10-08T12:00:00Z",
                five_end="2026-10-09T12:00:00Z", account=None, stamped=False)
    r = reading("R", at="2026-10-09T12:02:00Z", week_end="2026-10-15T12:00:00Z",
                five_end="2026-10-09T18:00:00Z")
    state = Q.QuarantineState()
    assert state.on_reading("A", p) == Q.GATE_NO_TRIGGER
    state.on_trigger(kind="excluded", reason="claude-usage")
    state.absorb_excluded_reading(x)
    decision = Q.certify(r, Q.Envelope.merge(state.envelope("A"),
                                             state.envelope(Q.EXCLUDED_SCOPE)))
    assert not decision.certified
    assert {1, 5} <= set(decision.failed_clauses)
    gate = state.on_reading("A", r)
    assert not gate.allowed
    assert state.accounting_quarantined("A")


@pytest.mark.parametrize("reason", Q.INELIGIBLE_REASONS)
def test_ineligible_witnesses_never_certify(reason):
    w = reading("w", at="2026-10-16T13:00:00Z", week_end="2026-10-23T13:00:00Z",
                five_end="2026-10-16T18:00:00Z", ineligible=reason)
    decision = Q.certify(w, envelope_of(PREFIX))
    assert decision == Q.CertificateDecision(False, (), reason)


def test_single_axis_and_unbound_witnesses_never_certify():
    single = reading("w", at="2026-10-16T13:00:00Z",
                     week_end="2026-10-23T13:00:00Z")
    assert Q.certify(single, Q.Envelope()).ineligible == Q.INELIGIBLE_SINGLE_AXIS
    unbound = reading("w", at="2026-10-16T13:00:00Z",
                      week_end="2026-10-23T13:00:00Z",
                      five_end="2026-10-16T18:00:00Z", stamped=False)
    assert Q.certify(unbound, Q.Envelope()).ineligible == Q.INELIGIBLE_UNBOUND


def test_continuing_validity_reasons():
    cert = Q.Certificate.from_witness(WITNESS)
    forward = reading("f", at="2026-10-16T13:10:00Z",
                      week_end="2026-10-23T13:00:00Z",
                      five_end="2026-10-16T18:00:00Z")
    assert Q.continuing_validity(cert, forward) is None
    backdated = reading("b", at="2026-10-16T13:20:00Z",
                        week_end="2026-10-01T13:00:00Z")
    assert Q.continuing_validity(cert, backdated) == "backward-key"
    older_window = reading("o", at="2026-10-16T13:20:00Z",
                           week_end="2026-10-23T13:00:00Z",
                           five_end="2026-10-16T12:00:00Z")
    assert Q.continuing_validity(cert, older_window) == "backward-key"
    regressed = reading("c", at="2026-10-16T12:59:00Z",
                        captured="2026-10-16T13:30:00Z",
                        week_end="2026-10-23T13:00:00Z",
                        five_end="2026-10-16T18:00:00Z")
    assert Q.continuing_validity(cert, regressed) == "clock-regression"
    reordered = reading("r", at="2026-10-16T13:30:00Z",
                        captured="2026-10-16T12:00:00Z",
                        week_end="2026-10-23T13:00:00Z",
                        five_end="2026-10-16T18:00:00Z")
    assert Q.continuing_validity(cert, reordered) == "order-regression"
    assert Q.continuing_validity(
        cert, forward,
        fallback_rows_captured_at=("2026-10-09T12:00:00Z",),
    ) == "non-inert-fallback"
    assert Q.continuing_validity(
        cert, forward,
        fallback_rows_captured_at=(WITNESS.captured_at_utc,)) is None


def test_envelope_is_monotone_and_order_independent():
    rs = [PREFIX, WITNESS,
          reading("z", at="2026-10-12T01:00:00Z", week_end="2026-10-19T01:00:00Z",
                  five_end="2026-10-12T04:00:00Z")]
    envelopes = {envelope_of(*perm) for perm in itertools.permutations(rs)}
    assert len(envelopes) == 1
    env = envelopes.pop()
    assert env == Q.Envelope.merge(envelope_of(rs[0]), envelope_of(*rs[1:]))
    assert Q.Envelope.from_document(env.to_document()) == env
    assert Q.Envelope.from_document({"maxWeekStartDate": 1}) is None


def test_a_trigger_voids_every_certificate_and_quarantines_new_accounts():
    state = Q.QuarantineState()
    state.on_reading("A", PREFIX)
    assert not state.accounting_quarantined("A")
    # An excluded observation: a trigger that leaves no spool marker (spec
    # revision 3), so the idle-gap witness certifies.
    state.on_trigger(kind="excluded", reason="claude-usage")
    for account in ("A", "B", "unattributed", "first-seen-later"):
        assert state.accounting_quarantined(account)
        assert state.control_quarantined(account)
    assert state.on_reading("A", WITNESS).reason == "witness"
    assert not state.accounting_quarantined("A")
    assert state.accounting_quarantined("B")
    # Drop consensus additionally waits for the control certificate.
    assert state.control_quarantined("A")
    state.on_control_witness("A", observation_id="o1",
                             captured_at_utc=WITNESS.captured_at_utc)
    assert not state.control_quarantined("A")
    # A new trigger revokes every certificate.
    state.on_trigger(kind="excluded", reason="claude-usage")
    assert state.accounting_quarantined("A")
    assert state.control_quarantined("A")


def test_a_spool_trigger_leaves_a_marker_that_nothing_clears():
    """Spec revision 3, section 5.6 item 1(b) (Codex design-fix-round-4 Q6):
    a digest-only spool trigger bounds no envelope, so no reading certifies
    while its marker is unresolved — not the idle-gap witness, not after a
    control witness, not for an account first seen later — and a later
    trigger adds a generation without discarding the earlier marker. The
    control certificate still reconciles on its own."""
    state = Q.QuarantineState()
    state.on_reading("A", PREFIX)
    state.on_trigger(kind="legacy-spool-activity", reason="changed",
                     segment="observations-2026-10.jsonl", offset=10,
                     at="2026-10-09T12:05:00Z")
    assert state.has_unresolved_spool_marker()
    for account in ("A", "B", "unattributed", "first-seen-later"):
        gate = state.on_reading(account, dataclasses.replace(
            WITNESS, observation_id=f"w-{account}", account_key=account))
        assert not gate.allowed and gate.reason == "unresolved-spool-marker"
        assert state.accounting_quarantined(account)
    state.on_control_witness("A", observation_id="o1",
                             captured_at_utc=WITNESS.captured_at_utc)
    assert not state.control_quarantined("A")
    assert state.accounting_quarantined("A")
    state.on_trigger(kind="excluded", reason="claude-usage")
    state.on_trigger(kind="legacy-spool-activity", reason="changed",
                     segment="observations-2026-10.jsonl", offset=20,
                     at="2026-10-09T12:06:00Z")
    assert [(m["seq"], m["offset"], m["at"]) for m in state.spool_markers] == [
        (1, 10, "2026-10-09T12:05:00Z"), (3, 20, "2026-10-09T12:06:00Z")]
    later = dataclasses.replace(
        WITNESS, observation_id="w-later", captured_at_utc="2026-10-30T13:00:00Z",
        capture_epoch=WITNESS.capture_epoch + 14 * 86400,
        detection_epoch=WITNESS.detection_epoch + 14 * 86400,
        week_start_date="2026-10-30",
        week_start_at_epoch=WITNESS.week_start_at_epoch + 14 * 86400,
        weekly_end_epoch=WITNESS.weekly_end_epoch + 14 * 86400,
        five_hour_key=WITNESS.five_hour_key + 14 * 86400,
        raw_five_hour_reset=WITNESS.raw_five_hour_reset + 14 * 86400)
    assert not state.on_reading("A", later).allowed


def test_a_certificate_persisted_beside_a_marker_is_invalidated():
    """A certificate issued without the marker check (the rule before spec
    revision 3) and persisted beside a retained spool trigger is invalidated
    when the state is loaded, never honored or advanced."""
    stale = Q.AccountRecovery(
        accounting=Q.Certificate.from_witness(WITNESS), accounting_trigger_seq=1)
    state = Q.QuarantineState(
        latest_trigger_seq=1, accounts={"A": stale},
        spool_markers=[{"seq": 1, "segment": "s", "offset": 0,
                        "at": "2026-10-09T12:05:00Z"}])
    assert state.accounting_quarantined("A")
    assert state.accounts["A"].accounting is None
    assert "A" in state.dirty_accounts
    assert not state.on_reading("A", dataclasses.replace(
        WITNESS, observation_id="w2")).allowed


def test_unverifiable_certificate_rows_read_as_quarantined():
    rec = Q.account_from_row("{not json", 3, '{"observationId": 1}', 3)
    assert rec.accounting is None and rec.control is None
    state = Q.QuarantineState(latest_trigger_seq=3, accounts={"A": rec})
    assert state.accounting_quarantined("A")
    assert state.control_quarantined("A")
