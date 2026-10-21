"""#825/#832 Task 1.5 — versioned strict administrative ops (spec 5.1, F6).

The previous release validates two administrative op kinds by exact keys: a
journal-repair acknowledgement (`journal_protocol_resolution`) and a reviewed
weekly decision (`claude_weekly_observation_decision`). A current record carries
the writer-generation stamp, which those validators would reject, so the
current binary emits both under versioned kinds the previous release ignores,
while current readers accept both versions. The new op kinds are registered in
the accounts-machinery set and the rederive op classification registry.

The frozen-release half of case F6 lives in tests/test_frozen_release_compat.py.
"""
import pytest

import _lib_account_protocol as P
import _lib_journal as J
import _lib_rederive

HW = ("observations-2026-10.jsonl", 100)
PREFIX = "sha256:" + "3" * 64


def _violation():
    return J.ProtocolViolation(
        batch_id="batch:x", kind="commit_without_begin",
        evidence=(("commitSequence", 1),),
    )


def test_repair_acknowledgement_is_emitted_only_as_v2():
    op = J.make_protocol_resolution(
        at="2026-10-09T00:00:00Z", violations=[_violation()],
        journal_high_water=HW, journal_prefix_hash=PREFIX)
    assert op["payload"]["kind"] == "journal_protocol_resolution_v2"
    assert P.is_stamped(op)
    assert J.is_protocol_resolution(op)


def test_current_readers_accept_both_resolution_versions():
    v2 = J.make_protocol_resolution(
        at="2026-10-09T00:00:00Z", violations=[_violation()],
        journal_high_water=HW, journal_prefix_hash=PREFIX)
    v1_payload = dict(v2["payload"], kind="journal_protocol_resolution")
    v1 = J.make_op(at=v2["at"], src=v2["src"], payload=v1_payload,
                   writer_generation=None)
    for record in (v1, v2):
        assert J.is_protocol_resolution(record)
        J._validate_protocol_resolution(record)
        fold = J.SelectorFold()
        J.fold_records(fold, [record])
        assert len(fold.resolutions) == 1


def test_resolution_validator_still_rejects_a_foreign_kind():
    v2 = J.make_protocol_resolution(
        at="2026-10-09T00:00:00Z", violations=[_violation()],
        journal_high_water=HW, journal_prefix_hash=PREFIX)
    bad = J.make_op(at=v2["at"], src=v2["src"],
                    payload=dict(v2["payload"], kind="something_else"))
    assert not J.is_protocol_resolution(bad)
    with pytest.raises(J.JournalProtocolError):
        J._validate_protocol_resolution(bad)


def test_reviewed_weekly_decision_kinds(monkeypatch):
    import _cctally_rederive as rederive
    assert rederive._REVIEWED_WEEKLY_KIND == (
        "claude_weekly_observation_decision_v2")
    assert rederive._REVIEWED_WEEKLY_KINDS == frozenset({
        "claude_weekly_observation_decision",
        "claude_weekly_observation_decision_v2",
    })


@pytest.mark.parametrize("kind", [
    "account_bound_cutover", "legacy_spool_activity",
    "journal_protocol_resolution_v2", "claude_weekly_observation_decision_v2",
])
def test_new_op_kinds_are_registered(kind):
    import _cctally_journal as jr
    assert kind in jr._ACCOUNTS_MACHINERY_KINDS
    assert kind in _lib_rederive._OP_CLASSIFICATIONS
