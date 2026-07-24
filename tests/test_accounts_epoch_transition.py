"""Epoch-transition coordinator (#341 Task 1, Steps 5-6, spec §2).

STATS_INDEX_EPOCH 1000 -> 1001. The coordinator, in exact order: (1) resolve the
cutover identity WITHOUT opening stats.db (stable-read of ~/.claude.json;
stably-absent -> unattributed); (2) atomically check/append the canonical cutover
op (stable semantic id `accounts-cutover-v1`, dedup on retry); (3) capture the
journal HW and rebuild — so the op is always inside the rebuild's input.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

import _cctally_core  # preserved across load_script()
from conftest import load_script, redirect_paths

FIXED = dt.datetime(2026, 7, 22, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _siblings():
    import _cctally_journal
    import _lib_journal
    import _lib_accounts
    return _cctally_journal, _lib_journal, _lib_accounts


def _seed_journal(jr, J):
    # A minimal legacy (account-less) usage obs so the journal is non-empty and
    # a real rebuild runs.
    obs = J.make_obs(at="2026-07-22T11:00:00Z", src="statusline", provider="claude",
                     payload={"kind": "heartbeat"})
    jr.append_record(obs, now_utc=FIXED)


def _write_claude_json(path, account_uuid):
    path.write_text(json.dumps({"oauthAccount": {
        "accountUuid": account_uuid, "emailAddress": "me@x.com"}}))


# --------------------------------------------------------------------------

def test_epoch_is_1001(ns):
    assert _cctally_core.STATS_INDEX_EPOCH == 1001


def test_transition_appends_op_with_resolved_identity(ns, tmp_path):
    jr, J, acc = _siblings()
    _seed_journal(jr, J)
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, "uuid-XYZ")

    key = jr.run_epoch_transition(claude_json_path=str(claude_json))
    assert key == acc.account_key("claude", "uuid-XYZ")
    assert jr.find_accounts_cutover_op() == key


def test_identity_unavailable_yields_unattributed_op(ns, tmp_path):
    jr, J, acc = _siblings()
    _seed_journal(jr, J)
    missing = tmp_path / "nope" / ".claude.json"  # stably-absent

    key = jr.run_epoch_transition(claude_json_path=str(missing))
    assert key == acc.UNATTRIBUTED
    assert jr.find_accounts_cutover_op() == acc.UNATTRIBUTED


def test_op_precedes_rebuild_hw(ns, tmp_path):
    # After the transition the rebuilt stats.db cursor equals the journal HW,
    # proving the cutover op (appended before the rebuild) was inside the
    # rebuild's consumed range (op-before-rebuild-HW ordering).
    jr, J, acc = _siblings()
    _seed_journal(jr, J)
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, "uuid-XYZ")

    jr.run_epoch_transition(claude_json_path=str(claude_json))

    conn = ns["open_db"]()
    try:
        cur = jr._read_cursor(conn)
    finally:
        conn.close()
    assert cur is not None
    assert cur == jr.journal_high_water()


def test_second_transition_does_not_duplicate_op(ns, tmp_path):
    jr, J, acc = _siblings()
    _seed_journal(jr, J)
    claude_json = tmp_path / ".claude.json"
    _write_claude_json(claude_json, "uuid-XYZ")

    jr.run_epoch_transition(claude_json_path=str(claude_json))
    jr.run_epoch_transition(claude_json_path=str(claude_json))

    # Exactly one cutover-op line across the whole journal.
    count = 0
    for seg in jr.list_segments():
        seg_path = _cctally_core.JOURNAL_DIR / seg
        import os
        size = os.path.getsize(seg_path)
        for _n, _o, raw in jr._read_segment_lines(seg_path, 0, size):
            rec = J.decode_line(raw)
            if rec is not None and rec.get("id") == jr.CUTOVER_OP_ID:
                count += 1
    assert count == 1
