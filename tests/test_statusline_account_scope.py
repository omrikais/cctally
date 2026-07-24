"""Statusline account-scoped reset-aware clamp (#341 Task 3, spec §3 write-path
semantics + slice-A review).

The statusline resolves the active Claude account once per read and scopes its
reset-aware 7d clamp to it — a silent correctness fix (rendered output stays
byte-frozen for <=1 real account, R8). Covered here:

  * `_statusline_active_account` torn-handling: torn -> merged (None); identified
    -> the real key; stably-absent -> the `unattributed` sentinel.
  * A `weekly_credit_floors` row stamped to a DIFFERENT account must NOT clamp
    the active account's projection (the scoping); a floor stamped to the ACTIVE
    account still clamps (non-vacuity companion — the clamp mechanism is intact).
"""
from __future__ import annotations

import json
import sys
import time

import pytest

import _cctally_core
from conftest import load_script, redirect_paths


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return sys.modules["cctally"]


def _iso(epoch):
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _key(uuid):
    import _lib_accounts
    return _lib_accounts.account_key("claude", uuid)


def _set_active(uuid):
    _cctally_core.CLAUDE_JSON_PATH.write_text(json.dumps({
        "oauthAccount": {"accountUuid": uuid, "emailAddress": "a@x.com",
                         "plan": "max"}}))
    _cctally_core._ACTIVE_CLAUDE_ACCOUNT_CACHE.update(sig=None, identity=None)


def _insert_snapshot(app, *, weekly_percent, weekly_resets_epoch, captured_epoch,
                     account_key=None):
    week_end = _iso(weekly_resets_epoch)
    week_start = _iso(weekly_resets_epoch - 7 * 86400)
    conn = app.open_db()
    try:
        if account_key is None:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
                " week_end_at, weekly_percent, page_url, source, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, 'statusline', '{}')",
                (_iso(captured_epoch), week_start[:10], week_end[:10], week_start,
                 week_end, weekly_percent),
            )
        else:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
                " week_end_at, weekly_percent, page_url, source, payload_json, "
                " account_key) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, 'statusline', '{}', ?)",
                (_iso(captured_epoch), week_start[:10], week_end[:10], week_start,
                 week_end, weekly_percent, account_key),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_credit_floor(app, weekly_resets_epoch, *, at_epoch, account_key):
    week_start_date = _iso(weekly_resets_epoch - 7 * 86400)[:10]
    at = _iso(at_epoch)
    conn = app.open_db()
    try:
        conn.execute(
            "INSERT INTO weekly_credit_floors "
            "(week_start_date, effective_at_utc, observed_pre_credit_pct, "
            " applied_at_utc, account_key) VALUES (?, ?, ?, ?, ?)",
            (week_start_date, at, 50, at, account_key),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# torn-handling
# --------------------------------------------------------------------------

def test_active_account_torn_falls_back_to_merged(app, monkeypatch):
    import _cctally_statusline as sl
    monkeypatch.setattr(
        _cctally_core, "_resolve_active_claude_identity",
        lambda: {"account_key": "unattributed", "status": "torn"})
    assert sl._statusline_active_account() is None  # merged read; never mis-stamp


def test_active_account_identified(app, monkeypatch):
    import _cctally_statusline as sl
    monkeypatch.setattr(
        _cctally_core, "_resolve_active_claude_identity",
        lambda: {"account_key": "deadbeef", "status": "identified"})
    assert sl._statusline_active_account() == "deadbeef"


def test_active_account_stably_absent_is_unattributed(app, monkeypatch):
    import _cctally_statusline as sl
    monkeypatch.setattr(
        _cctally_core, "_resolve_active_claude_identity",
        lambda: {"account_key": "unattributed", "status": "stably_absent"})
    assert sl._statusline_active_account() == "unattributed"


# --------------------------------------------------------------------------
# floor scoping (correctness) + non-vacuity companion
# --------------------------------------------------------------------------

def test_other_accounts_credit_floor_does_not_clamp_active(app):
    now = int(time.time())
    reset = now + 3 * 86400
    _set_active("uuid-A")
    # The active account's own snapshot (candidate selection now scopes to it too).
    _insert_snapshot(app, weekly_percent=50, weekly_resets_epoch=reset,
                     captured_epoch=now - 20, account_key=_key("uuid-A"))
    # A credit floor stamped to a DIFFERENT account (B) at now-10.
    _seed_credit_floor(app, reset, at_epoch=now - 10, account_key=_key("uuid-B"))
    projection = app._read_db_projection_once()
    # Scoped to A (which has no floor): the pre-floor 50% peak survives.
    assert projection.seven_day is not None
    assert projection.seven_day.percent == 50


def test_active_accounts_own_credit_floor_still_clamps(app):
    now = int(time.time())
    reset = now + 3 * 86400
    _set_active("uuid-A")
    _insert_snapshot(app, weekly_percent=50, weekly_resets_epoch=reset,
                     captured_epoch=now - 20, account_key=_key("uuid-A"))
    # The floor is stamped to the ACTIVE account -> it clamps: the only snapshot
    # is pre-floor, so no eligible row remains and the 7d projection is dropped.
    _seed_credit_floor(app, reset, at_epoch=now - 10, account_key=_key("uuid-A"))
    projection = app._read_db_projection_once()
    assert projection.seven_day is None


# --------------------------------------------------------------------------
# candidate-selection scoping (spec §3: the ENTIRE projection scopes to the
# active account — candidate selection + grouping, not just the clamps). Two
# discriminating directions: the WRONG account is always the recency winner, so
# a merged (unscoped) candidate SELECT picks it; scoping picks the active one.
# --------------------------------------------------------------------------

def test_candidate_selection_scopes_to_active_account_A(app):
    now = int(time.time())
    reset = now + 3 * 86400
    _set_active("uuid-A")
    # A (active) captured EARLIER; B captured LATER with a higher percent.
    _insert_snapshot(app, weekly_percent=50, weekly_resets_epoch=reset,
                     captured_epoch=now - 20, account_key=_key("uuid-A"))
    _insert_snapshot(app, weekly_percent=90, weekly_resets_epoch=reset,
                     captured_epoch=now - 10, account_key=_key("uuid-B"))
    projection = app._read_db_projection_once()
    assert projection.seven_day is not None
    # Merged (unscoped) would pick B's later 90%; scoped picks A's 50%.
    assert projection.seven_day.percent == 50


def test_candidate_selection_scopes_to_active_account_B(app):
    now = int(time.time())
    reset = now + 3 * 86400
    _set_active("uuid-B")
    # B (active) captured EARLIER; A captured LATER with a higher percent.
    _insert_snapshot(app, weekly_percent=50, weekly_resets_epoch=reset,
                     captured_epoch=now - 20, account_key=_key("uuid-B"))
    _insert_snapshot(app, weekly_percent=90, weekly_resets_epoch=reset,
                     captured_epoch=now - 10, account_key=_key("uuid-A"))
    projection = app._read_db_projection_once()
    assert projection.seven_day is not None
    assert projection.seven_day.percent == 50
