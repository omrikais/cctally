"""#825/#832 Task 3.5 — certified recovery, continuing validity and revocation,
case F14 (spec 5.6 items 4, 5, 8).

(a) After a trigger and an idle gap spanning a weekly reset and the end of the
    five-hour window, the first two-axis reading certifies, and later genuine
    outcomes are minted exactly as the no-exclusion reference run mints them.
(b) Each certificate clause has a failing variant through the real pipeline,
    plus the round-6 whole-prefix trace.
(c) Ineligible witnesses never certify.
(d) Revocation by backdated input, a new excluded observation, a legacy spool
    change, a manual `record-credit` and an administrative correction (at the
    fold, and end to end through a real `db rederive`).
(e) Cross-axis: a five-hour credit's suppression deletes the common latest
    row; the certificate survives because every registered fallback read is
    inert, and is revoked when one is not.
Certificate state is identical after ingest, rebuild and rederive.
"""
import json

import pytest

import _outcome_quarantine_trace as T

E0 = "2026-10-12T12:00:00Z"
F0 = "2026-10-09T14:00:00Z"
W_END = "2026-10-19T13:00:00Z"
W_FIVE = "2026-10-12T18:00:00Z"


def cur(at, weekly, *, end=W_END, five=None, five_end=W_FIVE, account="A",
        **extra):
    values = dict(at=at, weekly=weekly, week_end=end, account=account, **extra)
    if five is not None:
        values.update(five=five, five_end=five_end)
    return ("current", values)


PREFIX = [cur("2026-10-09T10:00:00Z", 30, end=E0, five=40, five_end=F0)]
TRIGGER = ("legacy", dict(at="2026-10-09T10:05:00Z", weekly=50, week_end=E0,
                          five=60, five_end=F0, account="B"))
WITNESS = cur("2026-10-12T13:00:00Z", 20, five=70)
#: Every automatic outcome class after the witness, with a held row inside.
GENUINE = [
    cur("2026-10-12T13:10:00Z", 20, five=20),   # five-hour arm
    cur("2026-10-12T13:20:00Z", 20, five=20),   # five-hour credit
    cur("2026-10-12T13:30:00Z", 0, five=20),    # reset-to-zero arm
    cur("2026-10-12T13:40:00Z", 0, five=20),    # reset-to-zero confirm
    cur("2026-10-12T13:50:00Z", 60, five=20),
    cur("2026-10-12T13:55:00Z", 55, five=25),   # weekly held, five-hour grew
    cur("2026-10-12T14:00:00Z", 30, five=25),   # >=25 pp fire
    cur("2026-10-12T14:10:00Z", 70, five=25),
    cur("2026-10-12T14:20:00Z", 10, end="2026-10-20T14:00:00Z", five=25),
]
FURTHER = (cur("2026-10-12T14:30:00Z", 12, end="2026-10-20T14:00:00Z", five=26),
           cur("2026-10-12T14:40:00Z", 13, end="2026-10-20T14:00:00Z", five=27))


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


def _reference_outcomes(monkeypatch, tmp_path, steps):
    ref = T.make_app(monkeypatch, tmp_path / "reference")
    T.run_steps(ref, monkeypatch, steps)
    return T.outcomes(ref)


def _certificate(app, name="A"):
    rows = {row["account_key"]: row for row in
            T.quarantine_state(app)["certificates"]}
    import _lib_accounts
    row = rows.get(_lib_accounts.account_key("claude", name))
    return None if row is None else row["accounting_certificate_json"]


def _decision_for_last_obs(app):
    """`certify` of the newest journal observation against the envelope the
    index held just before it (the fold's own inputs, recomputed)."""
    import _cctally_quarantine as G
    import _lib_outcome_quarantine as Q
    import _cctally_journal as jr
    import _lib_journal as J
    import _cctally_core as core
    last = None
    for segment in jr.list_segments():
        for line in (core.JOURNAL_DIR / segment).read_bytes().splitlines():
            rec = J.decode_line(line)
            if rec and G.is_claude_reading(rec):
                last = rec
    reading = G.reading_from_record(last)
    return reading, Q


def _descent(base, five_end):
    """A genuine same-source five-hour descent and confirmation in a fresh
    window (the first reading's changed weekly value makes it materialize)."""
    return [
        cur(T.iso(base), 80, five=70, five_end=five_end),
        cur(T.iso(base + 60), 80, five=10, five_end=five_end),
        cur(T.iso(base + 120), 80, five=10, five_end=five_end),
    ]


def test_the_withheld_descent_is_not_vacuous(app, monkeypatch):
    """Without any trigger the same descent mints exactly one credit."""
    base = T.epoch("2026-10-12T19:00:00Z")
    T.run_steps(app, monkeypatch, PREFIX + [WITNESS]
                + _descent(base, T.iso(base + 4 * 3600)))
    assert len(T.outcomes(app)["five_hour"]) == 1


def _assert_withheld_after(app, monkeypatch, start):
    """A genuine five-hour descent and confirmation after ``start`` mints
    nothing while the account stays quarantined."""
    base = T.epoch(start)
    five_end = T.iso(base + 4 * 3600)
    before = T.outcomes(app)
    T.run_steps(app, monkeypatch, _descent(base, five_end))
    assert T.outcomes(app) == before
    assert T.account_quarantined(app, "A")


# --------------------------------------------------------------------------
# (a) successful recovery
# --------------------------------------------------------------------------

def test_a_recovery_mints_exactly_what_the_reference_mints(app, monkeypatch, tmp_path):
    T.run_trace(app, monkeypatch, PREFIX + [TRIGGER])
    assert T.account_quarantined(app, "A")
    T.run_steps(app, monkeypatch, [WITNESS])
    assert not T.account_quarantined(app, "A"), "the idle-gap reading certifies"
    assert T.account_quarantined(app, "B")
    T.run_steps(app, monkeypatch, GENUINE)
    assert _certificate(app) is not None, (
        "the certificate survives the five-hour suppression: every registered "
        "fallback read is inert")
    result = T.continue_through_maintenance(app, monkeypatch, further=FURTHER)
    assert result["live_state"] == result["rebuilt_state"]
    assert result["after_rebuild_state"] == result["rederived_state"]
    # The no-exclusion reference run of the same readings, in its own store
    # (built last: building it re-points the module paths).
    reference = _reference_outcomes(
        monkeypatch, tmp_path, PREFIX + [WITNESS] + GENUINE + list(FURTHER))
    assert T.outcome_count(reference) == 4, reference  # non-vacuous
    assert result["after_rederive"] == reference
    assert result["live"]["five_hour"] and result["live"]["in_place"]
    assert result["live"]["transitions"]


# --------------------------------------------------------------------------
# (b) failing clauses through the pipeline
# --------------------------------------------------------------------------

def _clause_trace(clause):
    if clause == 1:   # a date key shared with a prefix week end
        prefix = PREFIX + [cur("2026-10-12T12:30:00Z", 30,
                               end="2026-10-19T09:00:00Z", five=40,
                               five_end="2026-10-12T15:00:00Z")]
        trigger = ("legacy", dict(TRIGGER[1], at="2026-10-12T12:35:00Z"))
        return prefix, trigger, WITNESS
    if clause == 2:   # a raw five-hour reset inside the anchor tolerances
        prefix = PREFIX + [cur("2026-10-12T12:40:00Z", 30, end=E0, five=40,
                               five_end="2026-10-12T17:35:00Z")]
        trigger = ("legacy", dict(TRIGGER[1], at="2026-10-12T12:45:00Z"))
        return prefix, trigger, cur("2026-10-12T17:55:00Z", 20, five=70)
    if clause == 3:   # a prefix instant inside the witness's weekly interval
        prefix = PREFIX + [cur("2026-10-12T11:00:00Z", 30, end=E0, five=40,
                               five_end="2026-10-12T12:00:00Z")]
        trigger = ("legacy", dict(TRIGGER[1], at="2026-10-12T11:05:00Z"))
        return prefix, trigger, cur("2026-10-12T13:00:00Z", 20,
                                    end="2026-10-19T06:00:00Z", five=70)
    if clause == 4:   # a prefix capture sorting lexically after the witness
        trigger = ("legacy", dict(
            at="2026-10-12T08:35:00Z", weekly=50, week_end="2026-10-12T06:00:00Z",
            five=60, five_end="2026-10-12T08:00:00Z",
            captured="2026-10-12T13:30:00+05:00"))
        return PREFIX, trigger, WITNESS
    if clause == 5:   # a predecessor raw five-hour reset live at detection
        prefix = PREFIX + [cur("2026-10-12T12:00:00Z", 30, end=E0, five=40,
                               five_end="2026-10-12T13:30:00Z")]
        trigger = ("legacy", dict(TRIGGER[1], at="2026-10-12T12:05:00Z"))
        return prefix, trigger, WITNESS
    if clause == 6:   # a debounce row armed in the witness's own week
        prefix = PREFIX + [cur("2026-10-12T12:10:00Z", 20, five=40,
                               five_end="2026-10-12T17:00:00Z"),
                           cur("2026-10-12T12:20:00Z", 0, five=40,
                               five_end="2026-10-12T17:00:00Z")]
        trigger = ("legacy", dict(TRIGGER[1], at="2026-10-12T12:25:00Z"))
        return prefix, trigger, cur("2026-10-12T13:00:00Z", 0, five=70)
    raise AssertionError(clause)


@pytest.mark.parametrize("clause", [1, 2, 3, 4, 5, 6])
def test_b_a_witness_failing_one_clause_stays_uncertified(app, monkeypatch, clause):
    import _cctally_quarantine as G
    import _lib_outcome_quarantine as Q
    prefix, trigger, witness = _clause_trace(clause)
    T.run_steps(app, monkeypatch, prefix + [trigger])
    T.ingest(app)
    conn = app.open_db()
    try:
        state = G.load_state(conn)
    finally:
        conn.close()
    import _lib_accounts
    envelope = Q.Envelope.merge(
        state.envelope(_lib_accounts.account_key("claude", "A")),
        state.envelope(Q.EXCLUDED_SCOPE))
    T.run_steps(app, monkeypatch, [witness])
    reading, _ = _decision_for_last_obs(app)
    decision = Q.certify(reading, envelope)
    assert not decision.certified
    assert clause in decision.failed_clauses, decision
    if clause in (2, 3, 4, 5):
        assert decision.failed_clauses == (clause,), decision
    assert T.account_quarantined(app, "A")
    _assert_withheld_after(app, monkeypatch, "2026-10-12T19:00:00Z"
                           if clause != 2 else "2026-10-12T18:10:00Z")


def test_b_a_replayed_identity_never_certifies(app, monkeypatch):
    import _cctally_journal as jr
    import _lib_journal as J
    import _cctally_core as core
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER])
    T.ingest(app)
    replay = None
    for segment in jr.list_segments():
        for line in (core.JOURNAL_DIR / segment).read_bytes().splitlines():
            rec = J.decode_line(line)
            if rec and rec.get("t") == "obs" and "wpg" in rec:
                replay = line
    with open(core.JOURNAL_DIR / jr.list_segments()[-1], "ab") as fh:
        fh.write(replay + b"\n")
    T.ingest(app)
    assert T.account_quarantined(app, "A")


def test_b_round_six_whole_prefix_trace(app, monkeypatch):
    steps = [
        cur("2026-10-09T12:00:00Z", 70, end="2026-10-16T12:00:00Z", five=70,
            five_end="2026-10-09T17:00:00Z"),
        ("legacy", dict(at="2026-10-09T12:01:00Z", weekly=80,
                        week_end="2026-10-08T12:00:00Z", five=80,
                        five_end="2026-10-09T12:00:00Z")),
        cur("2026-10-09T12:02:00Z", 10, end="2026-10-15T12:00:00Z", five=10,
            five_end="2026-10-09T18:00:00Z"),
    ]
    result = T.run_trace(app, monkeypatch, steps, further=(
        cur("2026-10-09T12:03:00Z", 10, end="2026-10-15T12:00:00Z", five=10,
            five_end="2026-10-09T18:00:00Z"),
        cur("2026-10-09T12:04:00Z", 10, end="2026-10-15T12:00:00Z", five=10,
            five_end="2026-10-09T18:00:00Z")))
    for phase in ("live", "rebuilt", "after_rebuild", "after_rederive"):
        assert result[phase]["transitions"] == [], phase
    assert T.account_quarantined(app, "A")
    assert result["live_state"] == result["rebuilt_state"]


# --------------------------------------------------------------------------
# (c) ineligible witnesses
# --------------------------------------------------------------------------

def _publication(kind, at):
    import _outcome_quarantine_trace as TT
    seven = {"kind": kind, "account": "x", "window": 0}
    return {"publicationId": "a" * 64, "capturedAt": TT.epoch(at),
            "fiveHourWindowKey": None,
            "provenance": {"sevenDay": seven, "fiveHour": seven}}


@pytest.mark.parametrize("kind", ["projection", "retained_low"])
def test_c_a_projection_filled_or_retained_low_axis_never_certifies(
        app, monkeypatch, kind):
    import argparse
    import _cctally_statusline as sl
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER])
    at = "2026-10-12T13:00:00Z"
    T.set_clock(monkeypatch, at)
    T.identity("A")
    args = argparse.Namespace(
        percent=20, resets_at=str(T.epoch(W_END)), five_hour_percent=70,
        five_hour_resets_at=str(T.epoch(W_FIVE)), source="statusline")
    assert app.record_usage_bound(
        args, binding=sl._capture_binding(), nudge_dashboard=False,
        publication=_publication(kind, at)) == 0
    assert T.account_quarantined(app, "A")


def test_c_single_axis_and_reviewed_hold_never_certify(app, monkeypatch):
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER, cur(WITNESS[1]["at"], 20)])
    assert T.account_quarantined(app, "A")
    import _cctally_quarantine as G
    import _lib_outcome_quarantine as Q
    import _lib_journal as J
    import _lib_account_protocol as P
    rec = P.stamp(J.make_obs(at="2026-10-12T13:00:00Z", src="record-usage",
                             provider="claude", account="acct",
                             payload={"weekly_percent": 20.0,
                                      "resets_at": T.epoch(W_END),
                                      "five_hour_percent": 70.0,
                                      "five_hour_resets_at": "2026-10-12T18:00:00+00:00",
                                      "captured_at": "2026-10-12T13:00:00Z"}))
    fold = G.QuarantineFold.empty(held_ids={rec["id"]})
    fold.state.on_trigger(kind="excluded", reason="claude-usage")
    gate = fold.on_record(rec)
    assert not gate.allowed
    assert G.reading_from_record(rec, held_ids={rec["id"]}).ineligible_reason == \
        "reviewed-hold"
    # A manual-credit synthetic snapshot is an EVENT, never a reading.
    synthetic = P.stamp(J.make_evt(kind="snapshot_accept", id="sa:x:syn:0",
                                   at="2026-10-12T13:00:00Z",
                                   payload={"account_key": "acct",
                                            "captured_at_utc": "2026-10-12T13:00:00Z",
                                            "week_start_date": "2026-10-12"}))
    assert fold.on_record(synthetic) is None
    assert fold.state.accounting_quarantined("acct")
    assert Q.witness_ineligibility(G.reading_from_record(rec, held_ids={rec["id"]}))


# --------------------------------------------------------------------------
# (d) revocation after recovery
# --------------------------------------------------------------------------

def _recovered(app, monkeypatch):
    T.run_steps(app, monkeypatch, PREFIX + [TRIGGER, WITNESS])
    assert not T.account_quarantined(app, "A")


def _arm_then_confirm(app, monkeypatch, *, rebuild_between):
    """After certified recovery, arm a genuine same-source five-hour 70->20
    descent in a fresh window, optionally run a real `db rebuild`, then
    observe 20 again. Returns the outcomes when armed and at the end."""
    _recovered(app, monkeypatch)
    base = T.epoch("2026-10-12T19:00:00Z")
    five_end = T.iso(base + 4 * 3600)
    T.run_steps(app, monkeypatch, [
        cur(T.iso(base), 80, five=70, five_end=five_end),
        cur(T.iso(base + 60), 80, five=20, five_end=five_end),
    ])
    armed = T.outcomes(app)
    if rebuild_between:
        T.rebuild(app)
    T.run_steps(app, monkeypatch, [
        cur(T.iso(base + 120), 80, five=20, five_end=five_end)])
    return armed, T.outcomes(app)


def test_a_rebuild_between_arm_and_confirm_mints_no_credit_after_recovery(
        app, monkeypatch, tmp_path):
    """Detects a rebuild that carries the disposable five-hour confirmation
    state across (#751b; the F17 oracle's arm-rebuild-confirm schedule,
    extracted): the confirmation state is not journaled, so a rebuild drops
    it and a credit armed before the rebuild cannot be confirmed after it
    (the admitted cold-start gap). Carrying it across mints a credit no
    rebuilt index can reproduce, so a second rebuild would remove it again.
    The uninterrupted control over the same readings mints exactly one more
    credit, so the withheld one is real."""
    armed, after = _arm_then_confirm(app, monkeypatch, rebuild_between=True)
    assert after == armed, ("a credit crossed the rebuild", armed, after)
    # The uninterrupted control, in its own store (built last: building it
    # re-points the module paths).
    control = T.make_app(monkeypatch, tmp_path / "uninterrupted")
    control_armed, control_after = _arm_then_confirm(
        control, monkeypatch, rebuild_between=False)
    assert control_armed == armed
    assert len(control_after["five_hour"]) == len(armed["five_hour"]) + 1, (
        control_armed, control_after)
    assert T.outcome_count(control_after) == T.outcome_count(armed) + 1


def test_d_a_backdated_weekly_reading_revokes(app, monkeypatch):
    _recovered(app, monkeypatch)
    # Weekly ingress admits resets up to 30 days in the past.
    T.run_steps(app, monkeypatch, [cur("2026-10-12T13:05:00Z", 30, end=E0,
                                       five=70)])
    _assert_withheld_after(app, monkeypatch, "2026-10-12T14:00:00Z")


def test_d_a_new_excluded_observation_naming_another_account_revokes(
        app, monkeypatch):
    _recovered(app, monkeypatch)
    T.legacy(app, monkeypatch, at="2026-10-12T13:05:00Z", weekly=1,
             week_end=W_END, five=1, five_end=W_FIVE, account="B")
    _assert_withheld_after(app, monkeypatch, "2026-10-12T14:00:00Z")


def test_d_a_legacy_spool_change_revokes(app, monkeypatch):
    import _cctally_core as core
    import _cctally_journal as jr
    _recovered(app, monkeypatch)
    core.STATUSLINE_SELECTED_PATH.write_text('{"legacy": 1}')
    jr.append_legacy_spool_activity(
        changed=("statusline-selected.json",),
        new_state=jr.capture_legacy_spool_state(), reason="changed")
    _assert_withheld_after(app, monkeypatch, "2026-10-12T14:00:00Z")


def _status(app, name="A"):
    import _cctally_quarantine as G
    import _lib_accounts
    conn = app.open_db()
    try:
        return G.account_status(conn, _lib_accounts.account_key("claude", name))
    finally:
        conn.close()


def test_d_a_manual_record_credit_revokes(app, monkeypatch):
    _recovered(app, monkeypatch)
    # A credential-bound two-axis `api` reading is a control witness too
    # (#825/#832 Q6: the bare CLI-captured witness above carries no
    # credential evidence, so it never was one).
    T.run_steps(app, monkeypatch, [cur("2026-10-12T13:01:00Z", 20, five=70,
                                       credential="token-A")])
    assert not T.account_quarantined(app, "A")
    assert _status(app)["control"] is True
    T.set_clock(monkeypatch, "2026-10-12T13:05:00Z")
    T.identity("A")
    args = app.build_parser().parse_args(["record-credit", "--to", "5", "--yes"])
    assert args.func(args) == 0
    # It revokes BOTH of A's certificates before its effects (spec 5.6 item 8).
    assert T.account_quarantined(app, "A")
    assert _status(app)["control"] is False
    _assert_withheld_after(app, monkeypatch, "2026-10-12T14:00:00Z")


def _journal_types():
    import _cctally_core as core
    import _cctally_journal as jr
    import _lib_journal as J
    types = []
    for segment in jr.list_segments():
        for line in (core.JOURNAL_DIR / segment).read_bytes().splitlines():
            rec = J.decode_line(line)
            if rec is not None:
                types.append(rec.get("t"))
    return types


def test_d_a_real_rederive_correction_revokes(app, monkeypatch, capsys):
    """End to end (#825/#832 Task 4 gap fill): a real `db rederive` whose plan
    applies a correction batch revokes the recovered account, and a genuine
    five-hour descent after it is withheld. The correction is made genuine by
    retaining priced session input the live recording never saw: the
    milestones crossed at 13:05 were priced without it, so replay re-prices
    them."""
    for name in ("A", "B", "unattributed"):
        T.retain_session_input(app, name)
    _recovered(app, monkeypatch)
    T.run_steps(app, monkeypatch, [cur("2026-10-12T13:05:00Z", 25, five=70)])
    assert not T.account_quarantined(app, "A")
    import _lib_accounts
    cache = app.open_cache_db()
    try:
        cache.execute(
            "INSERT INTO session_entries (source_path, line_offset, timestamp_utc,"
            " model, input_tokens, output_tokens, cache_create_tokens,"
            " cache_read_tokens, cache_create_1h_tokens, account_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("/retained/A/session.jsonl", 1, "2026-10-12T13:02:00Z",
             "claude-3-5-sonnet-20241022", 200000, 20000, 0, 0, 0,
             _lib_accounts.account_key("claude", "A")))
        cache.commit()
    finally:
        cache.close()
    before = _journal_types().count("correction_batch")
    T.rederive(app)
    capsys.readouterr()
    assert _journal_types().count("correction_batch") > before, (
        "non-vacuity: the rederive applied a correction batch")
    assert T.account_quarantined(app, "A")
    _assert_withheld_after(app, monkeypatch, "2026-10-12T19:00:00Z")


def test_d_an_admitted_correction_revokes():
    import _cctally_quarantine as G
    import _lib_journal as J
    import _lib_account_protocol as P
    fold = G.QuarantineFold.empty()
    fold.state.on_trigger(kind="excluded", reason="claude-usage")
    import _lib_outcome_quarantine as Q
    fold.state.accounts["acct"] = Q.AccountRecovery(
        accounting=Q.Certificate("w", "2026-10-12", 1, "x", "x", 1),
        accounting_trigger_seq=fold.state.latest_trigger_seq)
    assert not fold.state.accounting_quarantined("acct")
    fold.on_record(P.stamp({"t": "correction_batch", "id": "c", "at": "x"}))
    assert fold.state.accounting_quarantined("acct")


# --------------------------------------------------------------------------
# (e) cross-axis
# --------------------------------------------------------------------------

def test_e_a_non_inert_fallback_revokes_at_the_deletion_event(app, monkeypatch):
    import _lib_detector_access_registry as R
    monkeypatch.setattr(R, "non_inert_cross_period",
                        lambda registry=None: ("unclassified",))
    _recovered(app, monkeypatch)
    T.run_steps(app, monkeypatch, GENUINE[:2])   # the five-hour credit
    assert len(T.outcomes(app)["five_hour"]) == 1
    T.ingest(app)   # folds the credit's harvested event
    assert T.account_quarantined(app, "A")
    before = T.outcomes(app)
    T.run_steps(app, monkeypatch, GENUINE[2:4])  # reset-to-zero: withheld
    assert T.outcomes(app) == before


def _obs_id_captured_at(at):
    import _cctally_core as core
    import _cctally_journal as jr
    import _lib_journal as J
    for segment in jr.list_segments():
        for line in (core.JOURNAL_DIR / segment).read_bytes().splitlines():
            rec = J.decode_line(line)
            if (rec and rec.get("t") == "obs"
                    and (rec.get("payload") or {}).get("captured_at") == at):
                return rec["id"]
    raise AssertionError(at)


def _reviewed_rederive(app, tmp_path, capsys, held, *, apply):
    import _cctally_journal as jr
    high_water = jr.journal_high_water()
    manifest = {
        "schemaVersion": 2,
        "journalHighWater": {"segment": high_water[0], "offset": high_water[1]},
        "journalPrefixHash": jr.journal_prefix_hash(high_water),
        "reviewedAt": "2026-10-12T13:20:00Z",
        "reason": "F14(e) reviewed weekly hold",
        "weeklyAxisDecisions": [
            {"observationId": held, "disposition": "hold"}],
        "snapshotIdentityDecisions": [],
    }
    path = tmp_path / "reviewed.json"
    path.write_text(json.dumps(manifest))
    parser = app.build_parser()
    capsys.readouterr()
    rc = app.cmd_db_rederive(parser.parse_args([
        "db", "rederive", "--family", "claude-usage",
        "--reviewed-weekly-decisions", str(path), "--json"]))
    preview = capsys.readouterr().out
    if rc != 0 or not apply:
        return rc, preview
    preview = json.loads(preview)
    manifest.update(expectedBaselinePlanHash=preview["baselinePlanHash"],
                    expectedDecisionPlanHash=preview["decisionPlanHash"])
    path.write_text(json.dumps(manifest))
    rc = app.cmd_db_rederive(parser.parse_args([
        "db", "rederive", "--family", "claude-usage",
        "--reviewed-weekly-decisions", str(path), "--yes", "--json"]))
    return rc, capsys.readouterr().out


def test_e_a_weekly_hold_with_no_accepted_basis(app, monkeypatch, tmp_path,
                                                capsys):
    """A weekly hold with no accepted basis skips a reading that carries new
    five-hour evidence (spec 5.6 item 4). The registered `BASIS` read is inert
    for a certificate because that shape is reachable only from a scratch
    reviewed hold, which is never a witness and whose reviewed op revokes
    every certificate. Each half is pinned here:

    1. the mechanism: on the recovered store, a forced hold with no basis
       skips new five-hour evidence the ordinary decision would write;
    2. the live store never reaches it: a reviewed hold of A's first reading
       (no earlier accepted row, so no basis) refuses with exit 2, appends
       nothing and leaves the certificate as it was. (A later reading always
       has one: the accepted basis is the account's latest accepted row, in
       any week.);
    3. a reviewed hold that does have a basis appends the reviewed op, which
       revokes every account's certificate, and the certificate state is
       identical after ingest, a real rebuild and a real rederive."""
    import _cctally_journal as jr
    import _lib_accounts
    for name in ("A", "B", "unattributed"):
        T.retain_session_input(app, name)
    _recovered(app, monkeypatch)
    T.run_steps(app, monkeypatch, [cur("2026-10-12T13:10:00Z", 25, five=75)])
    assert not T.account_quarantined(app, "A")

    conn = app.open_db()
    try:
        row = dict(zip(
            ("week_start_date", "week_start_at", "week_end_at",
             "five_hour_window_key"),
            conn.execute(
                "SELECT week_start_date, week_start_at, week_end_at,"
                " five_hour_window_key FROM weekly_usage_snapshots"
                " WHERE account_key = ? ORDER BY captured_at_utc DESC LIMIT 1",
                (_lib_accounts.account_key("claude", "A"),)).fetchone()))
        payload = dict(row, account_key=_lib_accounts.account_key("claude", "A"),
                       weekly_percent=26.0, five_hour_percent=80.0)
        ordinary = jr._usage_snapshot_fold_decision(conn, payload)
        no_basis = jr._usage_snapshot_fold_decision(
            conn, payload, force_weekly_hold=True, reviewed_weekly_basis=None)
        conn.rollback()
    finally:
        conn.close()
    assert ordinary.snapshot_action == jr.SNAPSHOT_WRITE_OBSERVED
    assert no_basis.five_hour.effective_pct == 80.0, "new five-hour evidence"
    assert no_basis.weekly.basis is None
    assert no_basis.snapshot_action == jr.SNAPSHOT_SKIP_NO_CHANGE

    state = T.quarantine_state(app)
    high_water = jr.journal_high_water()
    rc, out = _reviewed_rederive(
        app, tmp_path, capsys, _obs_id_captured_at("2026-10-09T10:00:00Z"),
        apply=False)
    assert rc == 2, out
    assert "basis" in out.lower(), out
    assert jr.journal_high_water() == high_water, "the refusal appended nothing"
    assert T.quarantine_state(app) == state
    assert not T.account_quarantined(app, "A")

    rc, out = _reviewed_rederive(
        app, tmp_path, capsys, _obs_id_captured_at("2026-10-12T13:10:00Z"),
        apply=True)
    assert rc == 0, out
    assert json.loads(out)["reviewedWeeklyDecisionId"]
    for name in ("A", "B", "unattributed"):
        assert T.account_quarantined(app, name), name
    result = T.continue_through_maintenance(app, monkeypatch)
    assert result["live_state"] == result["rebuilt_state"]
    assert result["rebuilt_state"]["certificates"] == \
        result["rederived_state"]["certificates"]
