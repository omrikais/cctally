"""stats.db account-scoping contracts (#341 Task 1, Steps 7-8).

The three core RED tests the spec §7 / plan Step 7 mandate:

  7a  THE core milestone RED — two Claude accounts crossing the SAME integer
      percent in the SAME subscription week produce TWO ``percent_milestones``
      rows (today's ``UNIQUE(week_start_date, percent_threshold, reset_event_id)``
      + week-keyed ``get_max_milestone_for_week`` silently drop the second).
  7b  Shared-window ownership — two accounts observing the SAME physical
      ``five_hour_window_key`` get TWO ``five_hour_blocks`` rows (one per
      account); today the ``UNIQUE(five_hour_window_key)`` named-conflict upsert
      collapses them into one.
  7c  Per-account week walker — ``_compute_subscription_weeks(conn, ...,
      account_key=A)`` returns account A's weeks only, never re-anchored by
      account B's different reset boundary.

Isolation mirrors tests/test_accounts_journal.py (load_script + redirect_paths).

STATUS (#341 Task 1 continuation): these three tests are the Step 7 RED tests,
written and observed RED against HEAD. They are marked ``xfail(strict=True)``
because Step 8 (the account-scoped stats.db DDL + the write-chain threading)
is NOT yet implemented — see the "Task 1 handoff" block in
docs/superpowers/plans/2026-07-23-341-multi-account.md. The implementor of
Step 8 REMOVES the ``xfail`` marks; ``strict=True`` makes the suite fail the
moment the feature lands (xpass), forcing the marks off.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script, redirect_paths

FIXED = dt.datetime(2026, 7, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
_WEEK_END_EPOCH = int(dt.datetime(2026, 1, 8, tzinfo=dt.timezone.utc).timestamp())


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


def _claude_obs(J, *, at, weekly_percent, account, resets_at=_WEEK_END_EPOCH,
                five_hour_percent=None, five_hour_resets_at=None,
                src="record-usage", source="statusline"):
    payload = {"weekly_percent": weekly_percent, "resets_at": resets_at,
               "source": source}
    if five_hour_percent is not None:
        payload["five_hour_percent"] = five_hour_percent
    if five_hour_resets_at is not None:
        payload["five_hour_resets_at"] = five_hour_resets_at
    return J.make_obs(at=at, src=src, provider="claude", payload=payload,
                      account=account)


# --------------------------------------------------------------------------
# 7a — THE core milestone RED
# --------------------------------------------------------------------------

def test_two_accounts_same_threshold_same_week_two_milestones(ns):
    jr, J, acc = _siblings()
    key_a = acc.account_key("claude", "uuid-A")
    key_b = acc.account_key("claude", "uuid-B")

    # Both accounts cross the 60% integer threshold in the same subscription
    # week (same resets_at). Distinct fractional values so neither snapshot is
    # deduped against the other's; the account dimension is what must keep the
    # two crossings apart.
    jr.append_record(
        _claude_obs(J, at="2026-01-04T09:00:00Z", weekly_percent=60.2,
                    account=key_a), now_utc=FIXED)
    jr.append_record(
        _claude_obs(J, at="2026-01-04T09:05:00Z", weekly_percent=60.7,
                    account=key_b), now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    conn = ns["open_db"]()
    try:
        # Count first (no account_key column needed) so the RED demonstrates the
        # actual silent DROP, not merely a missing column.
        count = conn.execute(
            "SELECT COUNT(*) FROM percent_milestones WHERE percent_threshold = 60"
        ).fetchone()[0]
        assert count == 2, (
            "two accounts crossing 60% in one week must produce two milestone "
            f"rows (got {count} — the second was silently dropped by the "
            "week-keyed max + UNIQUE(week_start_date, percent_threshold, "
            "reset_event_id))")
        accounts = sorted(
            r[0] for r in conn.execute(
                "SELECT account_key FROM percent_milestones "
                "WHERE percent_threshold = 60"
            ).fetchall())
    finally:
        conn.close()
    assert accounts == sorted([key_a, key_b]), "one milestone per account"


# --------------------------------------------------------------------------
# 7b — shared-window ownership
# --------------------------------------------------------------------------

def test_two_accounts_share_five_hour_window_key_two_blocks(ns):
    jr, J, acc = _siblings()
    key_a = acc.account_key("claude", "uuid-A")
    key_b = acc.account_key("claude", "uuid-B")
    # Same physical 5h window (identical five_hour_resets_at -> identical
    # canonical window key), two accounts.
    fh_resets = "2026-01-04T13:00:00Z"

    jr.append_record(
        _claude_obs(J, at="2026-01-04T09:00:00Z", weekly_percent=5.0,
                    account=key_a, five_hour_percent=20.0,
                    five_hour_resets_at=fh_resets), now_utc=FIXED)
    jr.append_record(
        _claude_obs(J, at="2026-01-04T09:05:00Z", weekly_percent=6.0,
                    account=key_b, five_hour_percent=25.0,
                    five_hour_resets_at=fh_resets), now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")

    conn = ns["open_db"]()
    try:
        # Count first (no account_key column needed) so the RED demonstrates the
        # actual named-conflict-upsert collapse, not merely a missing column.
        count = conn.execute("SELECT COUNT(*) FROM five_hour_blocks").fetchone()[0]
        assert count == 2, (
            "two accounts sharing one physical 5h window must each get their own "
            f"five_hour_blocks row (got {count} — collapsed by "
            "ON CONFLICT(five_hour_window_key))")
        blocks = conn.execute(
            "SELECT account_key, five_hour_window_key FROM five_hour_blocks "
            "ORDER BY account_key"
        ).fetchall()
        window_keys = {r[1] for r in blocks}
    finally:
        conn.close()
    assert len(window_keys) == 1, "both blocks share the one physical window key"
    assert sorted({r[0] for r in blocks}) == sorted([key_a, key_b])


# --------------------------------------------------------------------------
# 7c — per-account subscription-week walker
# --------------------------------------------------------------------------

def _seed_usage_snapshot(conn, *, account_key, week_start_date, week_end_date,
                         week_start_at, week_end_at, weekly_percent,
                         captured_at):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json, account_key) "
        "VALUES (?, ?, ?, ?, ?, ?, 'statusline', '{}', ?)",
        (captured_at, week_start_date, week_end_date, week_start_at,
         week_end_at, weekly_percent, account_key),
    )


def test_week_walker_scoped_to_account(ns):
    import _lib_subscription_weeks as sw
    _jr, _J, acc = _siblings()
    key_a = acc.account_key("claude", "uuid-A")
    key_b = acc.account_key("claude", "uuid-B")

    conn = ns["open_db"]()
    try:
        # Account A anchored to a Sunday; account B anchored 3 days later — two
        # independent reset cadences that would corrupt each other's week
        # detection under a single global anchor.
        _seed_usage_snapshot(
            conn, account_key=key_a, week_start_date="2026-01-04",
            week_end_date="2026-01-11", week_start_at="2026-01-04T00:00:00+00:00",
            week_end_at="2026-01-11T00:00:00+00:00", weekly_percent=40.0,
            captured_at="2026-01-05T00:00:00Z")
        _seed_usage_snapshot(
            conn, account_key=key_b, week_start_date="2026-01-07",
            week_end_date="2026-01-14", week_start_at="2026-01-07T00:00:00+00:00",
            week_end_at="2026-01-14T00:00:00+00:00", weekly_percent=50.0,
            captured_at="2026-01-08T00:00:00Z")
        conn.commit()

        range_start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        range_end = dt.datetime(2026, 1, 20, tzinfo=dt.timezone.utc)
        weeks_a = sw._compute_subscription_weeks(
            conn, range_start, range_end, account_key=key_a)
        # Companion (review P3-C): the UNscoped (account_key=None) walk over the
        # SAME two-anchor fixture returns BOTH anchors, proving the scoped walk's
        # filtering above is non-vacuous — the exclusion is the account predicate,
        # not an empty fixture / walk artifact.
        weeks_all = sw._compute_subscription_weeks(
            conn, range_start, range_end, account_key=None)
    finally:
        conn.close()

    # SubWeek.start_date is a dt.date; its ISO string is the snapshot bucket key.
    starts = {w.start_date.isoformat() for w in weeks_a}
    assert "2026-01-04" in starts, "account A's own anchor week is present"
    assert "2026-01-07" not in starts, (
        "account B's anchor must NOT leak into account A's week walk")

    starts_all = {w.start_date.isoformat() for w in weeks_all}
    assert {"2026-01-04", "2026-01-07"} <= starts_all, (
        "the unscoped walk must surface BOTH accounts' anchors — proving the "
        "scoped walk dropped 2026-01-07 by the account predicate, not vacuously")
