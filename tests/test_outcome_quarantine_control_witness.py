"""#825/#832 fix round B: the control witness, certificate revocation by a
current journal-repair acknowledgement, the planned five-hour key bound, and
the OAuth writer's legacy-spool rescan.

* Q6 / P3-2: only an admitted, current-stamped `record-usage` Claude reading
  of payload source `api` whose validated binding names its effective account
  with valid credential evidence, carrying both raw axes and no statusline
  publication, projection, retained-low or reviewed hold, witnesses control in
  the journal; drop eligibility and doctor's "control recovered" claim also
  require the account's reconciled transition to record
  `authorityCommit == observation.id`.
* P2-1: a `db journal-repair` acknowledgement the current binary emits
  (`journal_protocol_resolution_v2`) revokes every accounting certificate.
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


def _status(app, name="A"):
    import _cctally_quarantine as G
    conn = app.open_db()
    try:
        return G.account_status(conn, _key(name))
    finally:
        conn.close()


def _doctor_rows():
    import _cctally_doctor
    data = _cctally_doctor._gather_outcome_quarantine()
    assert data["available"], data
    return {row["account"]: row for row in data["accounts"]}


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

def test_a_cli_api_reading_does_not_certify_control(app, monkeypatch):
    """The CLI adapter captures its binding WITHOUT credential evidence, so
    an `api`-labelled reading it records is an accounting witness only: the
    control certificate stays withheld and doctor keeps the control row."""
    T.run_steps(app, monkeypatch, F14.PREFIX + [F14.TRIGGER])
    T.set_clock(monkeypatch, "2026-10-12T13:00:00Z")
    T.identity("A")
    args = argparse.Namespace(
        percent=20, resets_at=str(T.epoch(F14.W_END)), five_hour_percent=70,
        five_hour_resets_at=str(T.epoch(F14.W_FIVE)), source="api")
    assert app.cmd_record_usage(args, nudge_dashboard=False) == 0
    status = _status(app)
    assert status["accounting"] is True, "non-vacuity: it is a valid witness"
    assert status["control"] is False
    row = _doctor_rows()[_key("A")]
    assert row["controlPaused"] is True
    assert row["accountingPaused"] is False


def test_a_credential_bound_reading_without_the_reconciled_commit_keeps_the_doctor_row(
        app, monkeypatch):
    """The journal half alone is not control recovery: until a reconciled
    authoritative transition records `authorityCommit` equal to the witness,
    doctor still reports drop detection as paused. The authoritative OAuth
    publication then completes it."""
    import _cctally_statusline as sl
    T.run_steps(app, monkeypatch, F14.PREFIX + [F14.TRIGGER])
    T.run_steps(app, monkeypatch, [cur("2026-10-12T13:00:00Z", 20, five=70,
                                       credential="token-A")])
    assert _status(app)["control"] is True, "the journal half holds"
    row = _doctor_rows()[_key("A")]
    assert row["controlPaused"] is True
    assert row["accountingPaused"] is False
    T.set_clock(monkeypatch, "2026-10-12T13:01:00Z")
    T.identity("A")
    args = argparse.Namespace(
        percent=20, resets_at=str(T.epoch(F14.W_END)), five_hour_percent=71,
        five_hour_resets_at=str(T.epoch(F14.W_FIVE)), source="api")
    result = sl._authoritative_record_usage(
        args, ("sevenDay", "fiveHour"),
        binding=sl._capture_binding(credential="token-A"),
        nudge_dashboard=False)
    assert result.status == "ok", result
    assert _key("A") not in _doctor_rows()


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
    assert not G.is_control_witness(
        _witness_record(payload={"five_hour_percent": None}))
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


def test_a_real_journal_repair_acknowledgement_revokes_accounting(
        app, monkeypatch, capsys):
    import _cctally_journal as jr
    import _lib_journal as J
    T.run_steps(app, monkeypatch, F14.PREFIX + [F14.TRIGGER])
    T.set_clock(monkeypatch, "2026-10-11T00:00:00Z")
    jr.append_records(_invalid_marker_conflict())
    T.ingest(app)
    T.run_steps(app, monkeypatch, [F14.WITNESS])
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
        "journal_protocol_resolution_v3"}
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
    assert near.ineligible_reason is None
    far = G.reading_from_record(_witness_record(payload={
        "five_hour_window_key": raw + 1801}))
    assert far.five_hour_key == raw - raw % 600
    assert far.ineligible_reason is not None
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
