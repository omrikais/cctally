"""#825/#832 Task 3.3 — quarantine state in the index, the triggers and the
envelope maintenance (spec 5.6 items 1, 3, 9).

Every excluded record and every current `legacy_spool_activity` op is a
trigger that quarantines EVERY Claude account — A, B, `unattributed` and an
account first observed later — whatever account the excluded record names.
The state lives in the disposable index and is a deterministic function of
journal order: live ingest, `db rebuild --db stats` and `db rederive` agree on
it, and the envelope covers admitted state from before the trigger.
"""
import json

import pytest

import _outcome_quarantine_trace as T

E0 = "2026-10-12T12:00:00Z"
F0 = "2026-10-09T14:00:00Z"


@pytest.fixture
def app(monkeypatch, tmp_path):
    return T.make_app(monkeypatch, tmp_path)


def _seed(app, monkeypatch):
    T.current(app, monkeypatch, at="2026-10-09T10:00:00Z", weekly=10,
              week_end=E0, five=30, five_end=F0, account="A")
    T.current(app, monkeypatch, at="2026-10-09T10:05:00Z", weekly=12,
              week_end=E0, five=40, five_end=F0, account="B")
    T.current(app, monkeypatch, at="2026-10-09T10:07:00Z", weekly=3,
              week_end=E0, five=5, five_end=F0, account="unattributed")


@pytest.mark.parametrize("named", [None, "B", "unattributed"])
def test_an_excluded_observation_quarantines_every_account(app, monkeypatch, named):
    _seed(app, monkeypatch)
    for name in ("A", "B", "unattributed"):
        assert not T.account_quarantined(app, name)
    T.legacy(app, monkeypatch, at="2026-10-09T10:10:00Z", weekly=50,
             week_end=E0, five=60, five_end=F0, account=named)
    T.current(app, monkeypatch, at="2026-10-09T10:20:00Z", weekly=13,
              week_end=E0, five=41, five_end=F0, account="B")
    # An account first observed AFTER the trigger is quarantined too.
    T.current(app, monkeypatch, at="2026-10-09T10:30:00Z", weekly=1,
              week_end=E0, five=1, five_end=F0, account="C")
    for name in ("A", "B", "unattributed", "C", "never-seen"):
        assert T.account_quarantined(app, name), name
    state = T.quarantine_state(app)
    assert [t["kind"] for t in state["triggers"]] == ["excluded"]
    assert state["triggers"][0]["segment"] and state["triggers"][0]["byte_offset"] >= 0


def test_a_legacy_spool_activity_op_quarantines_every_account(app, monkeypatch):
    import _cctally_core as core
    import _cctally_journal as jr
    _seed(app, monkeypatch)
    core.STATUSLINE_SELECTED_PATH.write_text('{"legacy": 70}')
    state = jr.capture_legacy_spool_state()
    jr.append_legacy_spool_activity(
        changed=("statusline-selected.json",), new_state=state, reason="changed")
    T.ingest(app)
    for name in ("A", "B", "unattributed", "C"):
        assert T.account_quarantined(app, name), name
    triggers = T.quarantine_state(app)["triggers"]
    assert [(t["kind"], t["reason"]) for t in triggers] == [
        ("legacy-spool-activity", "changed")]


def test_the_envelope_covers_pre_trigger_admitted_state(app, monkeypatch):
    _seed(app, monkeypatch)
    before = {row["scope"]: json.loads(row["envelope_json"])
              for row in T.quarantine_state(app)["envelope"]}
    import _lib_accounts
    a = _lib_accounts.account_key("claude", "A")
    assert before[a]["maxWeekStartDate"] == "2026-10-05"
    assert before[a]["maxCaptureAtUtc"] == "2026-10-09T10:00:00Z"
    T.legacy(app, monkeypatch, at="2026-10-09T10:10:00Z", weekly=50,
             week_end="2026-10-13T12:00:00Z", five=60, five_end=F0)
    T.ingest(app)
    after = {row["scope"]: json.loads(row["envelope_json"])
             for row in T.quarantine_state(app)["envelope"]}
    # A's own bounds are still there; the excluded reading widened only the
    # every-account scope.
    assert after[a] == before[a]
    assert after["excluded"]["maxWeekStartDate"] == "2026-10-06"
    assert after["excluded"]["maxCaptureAtUtc"] == "2026-10-09T10:10:00Z"


def test_state_is_identical_after_ingest_rebuild_and_rederive(app, monkeypatch):
    _seed(app, monkeypatch)
    T.legacy(app, monkeypatch, at="2026-10-09T10:10:00Z", weekly=50,
             week_end=E0, five=60, five_end=F0, account="A")
    result = T.run_trace(app, monkeypatch, [
        ("current", dict(at="2026-10-09T10:20:00Z", weekly=14, week_end=E0,
                         five=45, five_end=F0, account="A")),
    ])
    assert result["live_state"]["triggers"], "the excluded reading triggered"
    assert result["rebuilt_state"] == result["live_state"]
    assert result["rederived_state"] == result["live_state"]


def test_a_missing_or_unverifiable_certificate_reads_as_quarantined(app, monkeypatch):
    import _lib_accounts
    _seed(app, monkeypatch)
    T.legacy(app, monkeypatch, at="2026-10-09T10:10:00Z", weekly=50,
             week_end=E0, five=60, five_end=F0)
    T.ingest(app)
    conn = app.open_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO outcome_quarantine_certificates "
            "(account_key, accounting_certificate_json, accounting_trigger_seq,"
            " control_certificate_json, control_trigger_seq) "
            "VALUES (?, '{\"forged\": true}', 1, 'not json', 1)",
            (_lib_accounts.account_key("claude", "A"),))
        conn.commit()
    finally:
        conn.close()
    assert T.account_quarantined(app, "A")
    import _cctally_quarantine
    conn = app.open_db()
    try:
        status = _cctally_quarantine.account_status(
            conn, _lib_accounts.account_key("claude", "A"))
    finally:
        conn.close()
    assert status["control"] is False


def test_no_trigger_means_no_quarantine_and_obs_srcs_agree(app, monkeypatch):
    import _cctally_quarantine
    import _cctally_record
    assert _cctally_quarantine.CLAUDE_OBS_SRCS == _cctally_record._CLAUDE_OBS_SRCS
    _seed(app, monkeypatch)
    assert T.quarantine_state(app)["triggers"] == []
    assert not T.account_quarantined(app, "A")
