"""#825/#832 fix round B, rewritten for #948 (spec 6.4, 6.9): the control
witness, revocation by a current journal-repair acknowledgement, the planned
five-hour key bound, and the OAuth writer's legacy-spool rescan.

* Q6 / P3-2: only an admitted, current-stamped `record-usage` Claude reading
  of payload source `api` whose validated binding names its effective account
  with valid credential evidence, observing the axis, and carrying no
  statusline publication, projection, retained-low or reviewed hold, is the
  journal half of an authority completion. Since #948 only the authoritative
  OAuth writer appends the completion, after its reconciliation committed;
  no selected-state `authorityCommit` link gates anything (BV1).
* P2-1: a `db journal-repair` acknowledgement the current binary emits is
  administrative and revokes every released axis.
* P3-5: a planned five-hour key more than 1,800 s from the raw reset is not
  honored (the recording pipeline rejects it too).
* P3-3: the authoritative OAuth writer rescans the legacy spool before its
  publication (spec 5.5 step 6).
"""
from __future__ import annotations

import argparse
import json

import pytest

import _outcome_quarantine_trace as T
import test_outcome_quarantine_recovery_f14 as F14

cur = F14.cur


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


def _key(name):
    import _lib_accounts
    return _lib_accounts.account_key("claude", name)


def _journal_records():
    import _cctally_core as core
    import _cctally_journal as jr
    import _lib_journal as J
    out = []
    for segment in jr.list_segments():
        for line in (core.JOURNAL_DIR / segment).read_bytes().splitlines():
            rec = J.decode_line(line)
            if rec is not None:
                out.append(rec)
    return out


# --------------------------------------------------------------------------
# Q6 / P3-2: the control witness predicate
# --------------------------------------------------------------------------

def test_a_cli_api_reading_covers_nothing(app, monkeypatch):
    """The CLI adapter captures its binding WITHOUT credential evidence and
    is not the OAuth writer, so an `api`-labelled reading it records appends
    no completion: the witness that follows releases nothing."""
    T.run_steps(app, monkeypatch, F14.PREFIX + [F14.TRIGGER])
    T.set_clock(monkeypatch, "2026-10-09T10:10:00Z")
    T.identity("A")
    args = argparse.Namespace(
        percent=31, resets_at=str(T.epoch(F14.E0)), five_hour_percent=41,
        five_hour_resets_at=str(T.epoch(F14.F0)), source="api")
    assert app.cmd_record_usage(args, nudge_dashboard=False) == 0
    T.run_steps(app, monkeypatch, [F14.WITNESS])
    assert T.ops_of_kind("outcome_authority_completion") == []
    assert T.ops_of_kind("outcome_axis_release") == []
    assert T.account_quarantined(app, "A")


def test_a_credential_bound_direct_reading_is_not_authority_but_oauth_is(
        app, monkeypatch):
    """A credential-bound reading recorded outside the OAuth writer is a
    valid control witness in the journal, yet no reconciliation committed for
    it, so it covers nothing. The authoritative OAuth publication covers, and
    the next fresh-period entry is then released."""
    import _cctally_quarantine as G
    T.run_steps(app, monkeypatch, F14.PREFIX + [F14.TRIGGER])
    T.run_steps(app, monkeypatch, [cur("2026-10-09T10:10:00Z", 31,
                                       end=F14.E0, five=41, five_end=F14.F0,
                                       credential="token-A")])
    direct = [rec for rec in _journal_records()
              if (rec.get("payload") or {}).get("captured_at")
              == "2026-10-09T10:10:00Z"]
    assert direct and G.is_control_witness(direct[-1])
    assert T.ops_of_kind("outcome_authority_completion") == []
    T.run_steps(app, monkeypatch, [F14.COVER])
    [completion] = T.ops_of_kind("outcome_authority_completion")
    assert completion["payload"]["axes"] == ["sevenDay", "fiveHour"]
    T.run_steps(app, monkeypatch, [F14.WITNESS])
    assert not T.account_quarantined(app, "A")


def _witness_record(**overrides):
    import _lib_account_binding
    import _lib_account_protocol as P
    import _lib_journal as J
    natural = "A"
    binding = _lib_account_binding.capture(
        {"status": "identified", "account_key": _key(natural),
         "natural_id": natural, "email": "A@example.test",
         "plan_type": None}, credential="token-A")
    payload = {
        "weekly_percent": 20.0, "resets_at": T.epoch(F14.W_END),
        "source": "api", "captured_at": "2026-10-12T13:00:00Z",
        "five_hour_percent": 70.0,
        "five_hour_resets_at": "2026-10-12T18:00:00+00:00",
        "account_binding": binding.document(),
    }
    payload.update(overrides.pop("payload", {}))
    src = overrides.pop("src", "record-usage")
    account = overrides.pop("account", _key(natural))
    return P.stamp(J.make_obs(at="2026-10-12T13:00:00Z", src=src,
                              provider="claude", payload=payload,
                              account=account))


def test_the_witness_predicate_clauses():
    import _cctally_quarantine as G
    good = _witness_record()
    assert G.is_control_witness(good)
    assert not G.is_control_witness(_witness_record(src="statusline"))
    assert not G.is_control_witness(_witness_record(src="hook-tick"))
    assert not G.is_control_witness(
        _witness_record(payload={"source": "statusline"}))
    assert not G.is_control_witness(_witness_record(account=_key("B")))
    no_credential = dict(good["payload"]["account_binding"],
                         credentialEvidence=None)
    assert not G.is_control_witness(
        _witness_record(payload={"account_binding": no_credential}))
    tampered = dict(good["payload"]["account_binding"], email="x@example.test")
    assert not G.is_control_witness(
        _witness_record(payload={"account_binding": tampered}))
    # Axis-aware: a reading without five-hour values witnesses weekly only.
    weekly_only = _witness_record(payload={"five_hour_percent": None})
    assert not G.is_control_witness(weekly_only)
    assert G.is_control_witness(weekly_only, axis="sevenDay")
    assert not G.is_control_witness(weekly_only, axis="fiveHour")
    assert not G.is_control_witness(
        _witness_record(payload={"weekly_percent": "high"}))
    assert not G.is_control_witness(
        _witness_record(payload={"publication_id": "f" * 64}))
    assert not G.is_control_witness(_witness_record(payload={
        "publication": {"fiveHour": {"kind": "projection"}}}))
    assert not G.is_control_witness(_witness_record(payload={
        "publication": {"sevenDay": {"kind": "retained_low"}}}))
    assert not G.is_control_witness(good, held_ids={good["id"]})
    import _lib_account_protocol as P
    assert not G.is_control_witness(P.strip_stamp(good))


# --------------------------------------------------------------------------
# P2-1: a current journal-repair acknowledgement revokes certificates
# --------------------------------------------------------------------------

def _invalid_marker_conflict():
    import copy
    import _lib_journal as J
    at = "2026-10-11T00:00:00Z"
    begin, action, commit = J.make_correction_batch(
        batch_id="batch:fix-b-invalid-marker", family="claude-usage", at=at,
        actions=[{"action": "replace", "id": "sa:missing", "rev": 1, "at": at,
                  "payload": {"kind": "snapshot_accept",
                              "captured_at_utc": at,
                              "week_start_date": "2026-10-05",
                              "week_start_at": "2026-10-05T00:00:00Z",
                              "weekly_percent": 50.0, "source": "fixture",
                              "account_key": "unattributed"}}])
    divergent = copy.deepcopy(begin)
    divergent["protocol_extension"] = "divergent"
    return [begin, divergent, action, commit]


def _cli(app, capsys, argv):
    capsys.readouterr()
    args = app.build_parser().parse_args(argv)
    rc = args.func(args)
    return rc, capsys.readouterr().out


def test_a_real_journal_repair_acknowledgement_revokes_the_release(
        app, monkeypatch, capsys):
    import _cctally_journal as jr
    import _lib_journal as J
    T.run_steps(app, monkeypatch, F14.PREFIX + [F14.TRIGGER])
    # The conflicting batch lands BEFORE the coverage: an admitted
    # administrative record voids every coverage that precedes it (spec 6.9),
    # so only an acknowledgement after the release is what revokes it here.
    T.set_clock(monkeypatch, "2026-10-09T10:07:00Z")
    jr.append_records(_invalid_marker_conflict())
    T.ingest(app)
    T.run_steps(app, monkeypatch, [F14.COVER, F14.WITNESS])
    assert not T.account_quarantined(app, "A"), "non-vacuity: A recovered"
    rc, out = _cli(app, capsys, ["db", "journal-repair", "--json"])
    assert rc == 0, out
    [violation] = json.loads(out)["unacknowledgedViolations"]
    rc, out = _cli(app, capsys, ["db", "journal-repair", "--violation",
                                 violation["fingerprint"], "--yes", "--json"])
    assert rc == 0, out
    assert json.loads(out)["status"] == "applied"
    kinds = [(r.get("payload") or {}).get("kind") for r in _journal_records()]
    assert J.PROTOCOL_RESOLUTION_KINDS & set(kinds) == {
        "journal_protocol_resolution_v4"}
    assert T.account_quarantined(app, "A")


def test_every_administrative_kind_revokes():
    import _cctally_quarantine as G
    import _lib_account_protocol as P
    import _lib_journal as J
    assert J.PROTOCOL_RESOLUTION_KINDS <= G._ADMIN_OP_KINDS
    assert P._ADMIN_OP_KINDS <= G._ADMIN_OP_KINDS


# --------------------------------------------------------------------------
# P3-5: the planned five-hour key bound
# --------------------------------------------------------------------------

def test_a_planned_key_far_from_the_raw_reset_is_not_honored():
    import _cctally_quarantine as G
    raw = T.epoch("2026-10-12T18:00:00Z")
    near = G.reading_from_record(_witness_record(payload={
        "five_hour_window_key": raw - 1800}))
    assert near.five_hour_key == raw - 1800
    assert "fiveHour" in near.eligible_axes
    far = G.reading_from_record(_witness_record(payload={
        "five_hour_window_key": raw + 1801}))
    assert far.five_hour_key == raw - raw % 600
    assert "fiveHour" not in far.eligible_axes
    assert not G.is_control_witness(_witness_record(payload={
        "five_hour_window_key": raw + 1801}))


# --------------------------------------------------------------------------
# P3-3: the OAuth writer rescans the legacy spool before publishing
# --------------------------------------------------------------------------

def test_the_oauth_writer_journals_legacy_spool_activity_before_publishing(
        app, monkeypatch):
    import test_outcome_quarantine_control_f15 as F15
    clock = F15.Clock(app, monkeypatch, F15.T0)
    F15._seed(app, clock, "A", 70, week_reset=F15.WEEK)
    F15._write_legacy_candidate()
    before = len(_journal_records())
    F15._authoritative(app, clock, "A", 70, F15.WEEK, 11,
                       clock.value + 3 * 3600)
    added = _journal_records()[before:]
    kinds = [((r.get("payload") or {}).get("kind")
              or ("api-obs" if (r.get("payload") or {}).get("source") == "api"
                  else r.get("t"))) for r in added]
    assert "legacy_spool_activity" in kinds, kinds
    assert kinds.index("legacy_spool_activity") < kinds.index("api-obs"), kinds
