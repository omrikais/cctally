"""#825/#832 Task 3.3 — quarantine state in the index, the triggers and the
bounds maintenance (spec 5.6 items 1, 3, 9; #948 spec 6.5 replaced the
envelope with the whole-prefix per-axis bounds).

Every excluded record and every current `legacy_spool_activity` op is a
trigger that quarantines EVERY Claude account — A, B, `unattributed` and an
account first observed later — whatever account the excluded record names.
The state lives in the disposable index and is a deterministic function of
journal order: live ingest, `db rebuild --db stats` and `db rederive` agree on
it, and the bounds cover admitted state from before the trigger.
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


def _bounds(app):
    return {(row["scope"], row["axis"]): json.loads(row["bounds_json"])
            for row in T.quarantine_state(app)["bounds"]}


def test_the_bounds_cover_pre_trigger_admitted_state(app, monkeypatch):
    """A witness is judged against the WHOLE retained prefix (#948 spec
    6.5), so bounds summarize admitted readings from before any trigger; an
    excluded reading widens only the every-account `excluded` scope."""
    _seed(app, monkeypatch)
    import _lib_accounts
    a = _lib_accounts.account_key("claude", "A")
    before = _bounds(app)
    assert before[(a, "sevenDay")]["s7Date"] == "2026-10-05"
    assert before[(a, "sevenDay")]["t7"] == "2026-10-09T10:00:00Z"
    T.legacy(app, monkeypatch, at="2026-10-09T10:10:00Z", weekly=50,
             week_end="2026-10-13T12:00:00Z", five=60, five_end=F0)
    T.ingest(app)
    after = _bounds(app)
    assert after[(a, "sevenDay")] == before[(a, "sevenDay")]
    assert after[("excluded", "sevenDay")]["s7Date"] == "2026-10-06"
    assert after[("excluded", "sevenDay")]["t7"] == "2026-10-09T10:10:00Z"


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


def _reads_as_paused(app) -> bool:
    import sqlite3
    try:
        allowed = T.axis_allowed(app, "A")
    except sqlite3.DatabaseError:
        return True   # an unverifiable state decides nothing (fail closed)
    return not any(allowed.values())


@pytest.mark.parametrize("damage", [
    "forged-recovered", "stale-generation", "malformed-bounds",
    "malformed-meta"])
def test_a_missing_or_unverifiable_axis_state_reads_as_paused(
        app, monkeypatch, damage):
    """#948 spec 6.11 (T5 cache cases): a forged `recovered` row without
    its release, witness and coverage ids, a recovered row of a stale
    generation, and malformed bounds or generation state never read as
    clean — the gate reads paused or refuses — and a real rebuild
    reconstructs the state from the fold."""
    import _lib_accounts
    _seed(app, monkeypatch)
    T.legacy(app, monkeypatch, at="2026-10-09T10:10:00Z", weekly=50,
             week_end=E0, five=60, five_end=F0)
    T.ingest(app)
    status = T.account_status(app, "A")
    generation = status["generation"]
    assert generation and status["triggered"]
    before = T.quarantine_state(app)
    key = _lib_accounts.account_key("claude", "A")
    conn = app.open_db()
    try:
        if damage == "forged-recovered":
            conn.execute(
                "INSERT OR REPLACE INTO outcome_quarantine_axis_state "
                "(generation, account_key, axis, state) VALUES "
                "(?, ?, 'sevenDay', 'recovered')", (generation, key))
        elif damage == "stale-generation":
            conn.execute(
                "INSERT OR REPLACE INTO outcome_quarantine_axis_state "
                "(generation, account_key, axis, state, coverage_id, "
                "witness_id, release_id, recovered_at) VALUES "
                "(?, ?, 'sevenDay', 'recovered', 'c', 'w', 'r', ?)",
                (generation - 1, key, "2026-10-09T10:11:00Z"))
        elif damage == "malformed-bounds":
            assert conn.execute(
                "UPDATE outcome_quarantine_bounds SET bounds_json = '{' "
                "WHERE axis IN ('sevenDay', 'fiveHour')").rowcount
        else:
            assert conn.execute(
                "UPDATE outcome_quarantine_bounds SET bounds_json = "
                "'{\"cohort\": 1}' WHERE axis = 'meta'").rowcount
        conn.commit()
    finally:
        conn.close()
    assert _reads_as_paused(app)
    T.rebuild(app)
    assert T.quarantine_state(app) == before
    assert T.axis_allowed(app, "A") == {"sevenDay": False, "fiveHour": False}


def test_no_trigger_means_no_quarantine_and_obs_srcs_agree(app, monkeypatch):
    import _cctally_quarantine
    import _cctally_record
    assert _cctally_quarantine.CLAUDE_OBS_SRCS == _cctally_record._CLAUDE_OBS_SRCS
    _seed(app, monkeypatch)
    assert T.quarantine_state(app)["triggers"] == []
    assert not T.account_quarantined(app, "A")


def test_only_records_the_completion_window_keeps_move_the_cohort_clock(app):
    """#948 spec 6.11 (T5): derivation-completion scratch replay omits every
    observation that is neither rederivable nor a Claude reading (#997 D3:
    Codex quota observations, most of a production journal), so the fold
    must not let one move H or establish expiry; otherwise live ingest and
    the completion replay disagree on the cohort, and so on every witness
    after it."""
    import _cctally_quarantine as G
    import _lib_journal as J
    fold = G.QuarantineFold.empty()
    claude = J.make_obs(
        at="2026-10-09T10:00:00Z", src="record-usage", provider="claude",
        payload={"weekly_percent": 10.0, "resets_at": T.epoch(E0),
                 "captured_at": "2026-10-09T10:00:00Z"})
    codex = J.make_obs(
        at="2026-10-09T10:01:00Z", src="codex-quota", provider="codex",
        payload={"captured_at": "2026-10-09T10:01:00Z", "used_percent": 1.0})
    fold.on_record(claude)
    fold.on_record(codex)
    assert fold.state.max_admitted_at == T.epoch("2026-10-09T10:00:00Z")
    # A spool trigger: its cohort's E is H + 95 s, and expiry needs a later
    # admitted record whose clock instant reaches E.
    fold.on_record(J.prepare_record(J.make_op(
        at="2026-10-09T10:02:00Z", src="statusline",
        payload={"kind": "legacy_spool_activity", "reason": "changed",
                 "changed": [], "digest": "sha256:0"})))
    assert fold.state.cohort is not None
    later = dict(codex, at="2026-10-09T11:00:00Z",
                 payload={"captured_at": "2026-10-09T11:00:00Z",
                          "used_percent": 2.0})
    fold.on_record(later)
    assert not fold.state.expired, "a Codex observation established expiry"
    fold.on_record(dict(claude, at="2026-10-09T11:00:00Z", payload=dict(
        claude["payload"], captured_at="2026-10-09T11:00:00Z")))
    assert fold.state.expired
