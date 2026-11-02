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


def test_repair_acknowledgement_is_emitted_only_as_v4():
    # #797/#823/#840 (spec 4.6 B5): the previous release's v2 validator
    # rejects the `wpr` stamp, so the current binary emits the successor;
    # #1019/#1014 (spec 4.7 C5): 1.116.1's v3 validator rejects `wpx`, so
    # the successor is now v4.
    op = J.make_protocol_resolution(
        at="2026-10-09T00:00:00Z", violations=[_violation()],
        journal_high_water=HW, journal_prefix_hash=PREFIX)
    assert op["payload"]["kind"] == "journal_protocol_resolution_v4"
    assert P.is_stamped(op)
    assert J.is_protocol_resolution(op)


def test_current_readers_accept_both_resolution_versions():
    v2 = J.make_protocol_resolution(
        at="2026-10-09T00:00:00Z", violations=[_violation()],
        journal_high_water=HW, journal_prefix_hash=PREFIX)
    v1_payload = dict(v2["payload"], kind="journal_protocol_resolution")
    v1 = J.make_op(at=v2["at"], src=v2["src"], payload=v1_payload,
                   writer_generation=None)
    # The previous release's v2 shape: `wpg` without `wpr`.
    v2_payload = dict(v2["payload"], kind="journal_protocol_resolution_v2")
    v2_retained = J.make_op(at=v2["at"], src=v2["src"], payload=v2_payload,
                            writer_revision=None)
    # 1.116.1's v3 shape: `wpg` and `wpr` without `wpx`.
    v3_payload = dict(v2["payload"], kind="journal_protocol_resolution_v3")
    v3_retained = J.make_op(at=v2["at"], src=v2["src"], payload=v3_payload,
                            writer_successor=None)
    for record in (v1, v2_retained, v3_retained, v2):
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
    "account_bound_revision", "journal_protocol_resolution_v3",
    "account_bound_revision_v3", "journal_protocol_resolution_v4",
    # #948 spec 6.2: the three recovery ops.
    "legacy_spool_inventory_completion", "outcome_authority_completion",
    "outcome_axis_release",
])
def test_new_op_kinds_are_registered(kind):
    import _cctally_journal as jr
    assert kind in jr._ACCOUNTS_MACHINERY_KINDS
    assert kind in _lib_rederive._OP_CLASSIFICATIONS


def test_the_recovery_ops_round_trip_stamped_and_are_no_trigger(
        monkeypatch, tmp_path):
    """#948 spec 6.2: each recovery op is appended current-stamped and
    retained (`rederive` classifies it `retained`, so a correction keeps it),
    reads back exactly, and starts no quarantine generation — a recovery op
    that triggered would re-pause the axis it releases, forever."""
    import _outcome_quarantine_trace as T
    app = T.make_app(monkeypatch, tmp_path)
    import _cctally_journal as jr
    import _cctally_quarantine as Qg
    ids = [
        jr.append_legacy_spool_inventory_completion(
            episode=("observations-2026-11.jsonl", 10), digest="sha256:x"),
        jr.append_outcome_authority_completion(
            generation=1, account="claude:a", observation_id="obs:1",
            axes=("sevenDay",), authority_commit="obs:1"),
        jr.append_outcome_axis_release(
            generation=1, account="claude:a", axis="fiveHour",
            witness_id="obs:2", authority_id="op:1"),
    ]
    records = {rec["id"]: rec for rec in T.journal_records()}
    kinds = []
    fold = Qg.QuarantineFold.empty()
    for rid in ids:
        rec = records[rid]
        assert P.is_stamped(rec), rec
        assert _lib_rederive._OP_CLASSIFICATIONS[
            rec["payload"]["kind"]].mode == "retained"
        kinds.append(rec["payload"]["kind"])
        fold.on_record(rec)
    assert kinds == [P.INVENTORY_COMPLETION_OP_KIND,
                     P.AUTHORITY_COMPLETION_OP_KIND, P.AXIS_RELEASE_OP_KIND]
    assert records[ids[2]]["payload"] == {
        "kind": "outcome_axis_release", "generation": 1,
        "account": "claude:a", "axis": "fiveHour", "witness": "obs:2",
        "authority": "op:1"}
    assert not fold.state.has_trigger()
    T.ingest(app)
    assert T.quarantine_state(app)["triggers"] == []
