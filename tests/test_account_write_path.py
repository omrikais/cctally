"""Claude write-path account attribution closures (#341 Task 3, P2-1):

- record-credit stamps the credit op with the ACTIVE Claude account so the
  `weekly_credit_floors` row (and thence the account-scoped `_reset_aware_floor`
  clamp) lands under that account — not the `unattributed` sentinel.
- record-credit EXITS 2 on a torn `~/.claude.json` read (active account
  genuinely unavailable), but PROCEEDS under `unattributed` when the file is
  stably absent (single-account / api-key / legacy install — a resolved outcome).
- sync-week materializes the cost snapshot under the active account.

Isolation via load_isolated_cctally_module (CLAUDE_JSON_PATH -> tmp/.claude.json).
"""
from __future__ import annotations

import argparse
import json

import pytest

import _cctally_core
from conftest import load_isolated_cctally_module

WS_AT = "2026-06-13T00:00:00+00:00"
WE_AT = "2026-06-20T00:00:00+00:00"


@pytest.fixture
def cc(tmp_path, monkeypatch):
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _write_claude_json(uuid="acct-uuid-1", email="me@example.com", plan="max"):
    _cctally_core.CLAUDE_JSON_PATH.write_text(json.dumps({
        "oauthAccount": {
            "accountUuid": uuid, "emailAddress": email, "plan": plan,
        }
    }))
    _cctally_core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)


def _claude_key(uuid="acct-uuid-1"):
    import _lib_accounts
    return _lib_accounts.account_key("claude", uuid)


def _seed_week(cc, pct=46.0):
    conn = cc.open_db()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date, "
            "week_end_date, week_start_at, week_end_at, weekly_percent, page_url, "
            "source, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-06-18T21:12:00Z", "2026-06-13", "2026-06-20", WS_AT, WE_AT, pct,
             None, "userscript", "{}"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_entry(cc, account_key, cost, ts, src):
    """Seed one `session_entries` cache row stamped to `account_key` (cost rides
    `cost_usd_raw` so `mode='auto'` reads it back verbatim)."""
    conn = cc.open_cache_db()
    try:
        conn.execute(
            "INSERT INTO session_entries (source_path, line_offset, timestamp_utc, "
            "model, msg_id, req_id, input_tokens, output_tokens, cache_create_tokens, "
            "cache_read_tokens, usage_extra_json, speed, cost_usd_raw, mutation_seq, "
            "mutation_min_ts, account_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (src, 0, ts, "claude-sonnet-4-20250514", account_key + ts, account_key,
             0, 0, 0, 0, None, None, cost, 1, ts, account_key),
        )
        conn.commit()
    finally:
        conn.close()


def _credit_args(**over):
    a = dict(to=31.0, from_pct=46.0, at="2026-06-19T14:37:00Z", week="2026-06-13",
             dry_run=False, yes=True, json=False, force=False)
    a.update(over)
    return argparse.Namespace(**a)


def _floor_account(cc):
    conn = cc.open_db()
    try:
        row = conn.execute(
            "SELECT account_key FROM weekly_credit_floors "
            "WHERE week_start_date=?", ("2026-06-13",)).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# record-credit account stamp + exit-2 gate
# --------------------------------------------------------------------------

def test_record_credit_stamps_active_account(cc, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    _write_claude_json()
    _seed_week(cc)
    assert cc.cmd_record_credit(_credit_args()) == 0
    assert _floor_account(cc) == _claude_key()      # real key, not 'unattributed'


def test_record_credit_exits_2_on_torn_identity(cc, monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    _seed_week(cc)
    monkeypatch.setattr(
        _cctally_core, "_resolve_active_claude_identity",
        lambda: {"account_key": "unattributed", "status": "torn",
                 "natural_id": None, "email": None, "plan_type": None},
    )
    assert cc.cmd_record_credit(_credit_args()) == 2
    # nothing written (gate fired before the op append)
    assert _floor_account(cc) is None


def test_record_credit_stably_absent_proceeds_unattributed(cc, monkeypatch):
    # No ~/.claude.json -> stably_absent -> a RESOLVED unattributed outcome, NOT
    # unavailable: record-credit proceeds byte-identically to pre-#341.
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-19T14:37:00Z")
    _seed_week(cc)
    assert cc.cmd_record_credit(_credit_args()) == 0
    assert _floor_account(cc) == "unattributed"


# --------------------------------------------------------------------------
# sync-week per-account cost materialization
# --------------------------------------------------------------------------

def test_sync_week_stamps_cost_snapshot_with_active_account(cc):
    _write_claude_json()
    cc.open_db().close()
    args = argparse.Namespace(
        week_start="2026-06-13", week_end="2026-06-20", week_start_name=None,
        mode="auto", offline=False, project=None, json=False, quiet=True)
    assert cc.cmd_sync_week(args) == 0
    conn = cc.open_db()
    try:
        row = conn.execute(
            "SELECT account_key FROM weekly_cost_snapshots "
            "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == _claude_key()


def test_sync_week_no_claude_json_stays_unattributed(cc):
    cc.open_db().close()
    args = argparse.Namespace(
        week_start="2026-06-13", week_end="2026-06-20", week_start_name=None,
        mode="auto", offline=False, project=None, json=False, quiet=True)
    assert cc.cmd_sync_week(args) == 0
    conn = cc.open_db()
    try:
        row = conn.execute(
            "SELECT account_key FROM weekly_cost_snapshots "
            "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "unattributed"     # byte-stable single-account path


def test_sync_week_cost_scoped_to_active_account(cc):
    """P2-CQ2: the active account's cost snapshot carries ONLY that account's
    stamped spend — NOT the merged sum across accounts sharing the week."""
    import _lib_accounts
    ka = _claude_key("acct-uuid-1")                       # active account A
    kb = _lib_accounts.account_key("claude", "acct-uuid-2")  # account B
    _write_claude_json(uuid="acct-uuid-1")                # active = A
    _seed_entry(cc, ka, 1.0, "2026-06-15T12:00:00+00:00", "/x/projects/p/a.jsonl")
    _seed_entry(cc, kb, 10.0, "2026-06-16T12:00:00+00:00", "/x/projects/p/b.jsonl")
    args = argparse.Namespace(
        week_start="2026-06-13", week_end="2026-06-20", week_start_name=None,
        mode="auto", offline=False, project=None, json=False, quiet=True)
    assert cc.cmd_sync_week(args) == 0
    conn = cc.open_db()
    try:
        row = conn.execute(
            "SELECT account_key, cost_usd FROM weekly_cost_snapshots "
            "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == ka
    # A-only ($1.00), NOT the merged A+B ($11.00) the pre-fix summation produced.
    assert abs(float(row[1]) - 1.0) < 1e-9
