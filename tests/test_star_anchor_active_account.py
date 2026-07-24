"""`*`-anchor active-account re-anchoring (#341 Task 3 slice C, spec §6 rev 4).

Every `*`-scoped subscription-week writer (the vendor budget ladder, the
vendor-budget projected metric, and `project_budget_milestones`) resolves "the
current week" as the ACTIVE account's subscription week, under the same
skip+WARN rule; per-account ladders use their OWN account's week. The shared
current-week resolver `_fetch_current_week_snapshots` (and
`_resolve_current_budget_window`) gains an optional `account_key` that scopes
the snapshot window. Byte-stable at <=1 account (default `None` = merged, and a
lone `unattributed` install resolves the same window).
"""
from __future__ import annotations

import datetime as dt
import sys

import pytest

import _cctally_core
from conftest import load_script, redirect_paths


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return sys.modules["cctally"]


def _iso(epoch):
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _key(uuid):
    import _lib_accounts
    return _lib_accounts.account_key("claude", uuid)


def _insert(app, *, weekly_percent, week_start_epoch, week_end_epoch,
            captured_epoch, account_key):
    ws = _iso(week_start_epoch)
    we = _iso(week_end_epoch)
    conn = app.open_db()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, page_url, source, payload_json, "
            " account_key) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, 'statusline', '{}', ?)",
            (_iso(captured_epoch), ws[:10], we[:10], ws, we, weekly_percent,
             account_key),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_two_account_weeks(app, now):
    ka, kb = _key("uuid-A"), _key("uuid-B")
    # A's week: [now-2d, now+5d); B's week: [now-5d, now+2d). Both contain now,
    # different boundaries. B captured LATER (would win a merged/account-blind
    # resolve on the captured-at tie-break).
    _insert(app, weekly_percent=40, week_start_epoch=now - 2 * 86400,
            week_end_epoch=now + 5 * 86400, captured_epoch=now - 30,
            account_key=ka)
    _insert(app, weekly_percent=80, week_start_epoch=now - 5 * 86400,
            week_end_epoch=now + 2 * 86400, captured_epoch=now - 10,
            account_key=kb)
    return ka, kb


def _call(app, fn, *args, **kwargs):
    conn = app.open_db()
    try:
        return fn(conn, *args, **kwargs)
    finally:
        conn.close()


def test_fetch_current_week_snapshots_scopes_to_account(app):
    import _cctally_forecast as fc
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    ka, kb = _seed_two_account_weeks(app, now)
    now_dt = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)

    a_win = _call(app, fc._fetch_current_week_snapshots, now_dt, account_key=ka)
    b_win = _call(app, fc._fetch_current_week_snapshots, now_dt, account_key=kb)
    assert a_win is not None and b_win is not None
    # A's window starts now-2d; B's starts now-5d.
    assert int(a_win[0].timestamp()) == now - 2 * 86400
    assert int(b_win[0].timestamp()) == now - 5 * 86400
    # Merged (None) picks B's later-captured week (account-blind, today's behavior).
    merged = _call(app, fc._fetch_current_week_snapshots, now_dt)
    assert int(merged[0].timestamp()) == now - 5 * 86400


def test_resolve_current_budget_window_scopes_to_account(app):
    import _cctally_forecast as fc
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    ka, kb = _seed_two_account_weeks(app, now)
    now_dt = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)

    a_start, _ = _call(app, fc._resolve_current_budget_window, now_dt,
                       account_key=ka)
    b_start, _ = _call(app, fc._resolve_current_budget_window, now_dt,
                       account_key=kb)
    assert int(a_start.timestamp()) == now - 2 * 86400
    assert int(b_start.timestamp()) == now - 5 * 86400
