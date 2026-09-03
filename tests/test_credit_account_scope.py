"""One account's credit must not reach another's (#703 + #707 §6.2a).

Called out separately from §6.2 because the helpers the accounting sites are
built on are not account-scoped, so following the consumer table literally on
top of them would give account B account A's credit marker, cadence and cost
basis.

Three seams are pinned here.

The reset-to-zero MARKER is a single file holding one armed candidate. It
recorded the week and the boundary but not the account, so a zero arriving for a
DIFFERENT account matched the arm and confirmed a credit built from the first
account's baseline — and it cleared the marker, so the account that genuinely
lost its counter was left unrepaired.

`_get_canonical_boundary_for_date` queries `weekly_usage_snapshots` across ALL
accounts, so the boundary one account's week is anchored on could come from
another's.

`_apply_reset_events_to_weekrefs` loads every account's events, so one account's
credit rewrote another account's displayed boundaries.
"""
from __future__ import annotations

import argparse
import datetime as dt

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


WEEK_END_DT = dt.datetime(2026, 9, 5, 0, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END_AT = WEEK_END_DT.isoformat(timespec="seconds")
WEEK_START_DATE = "2026-08-29"
WEEK_START_AT = "2026-08-29T00:00:00+00:00"


def _snapshot(conn, captured_at, percent, *, account_key,
              week_start_at=WEEK_START_AT, week_end_at=WEEK_END_AT):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, page_url, source, payload_json, "
        " account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (captured_at, WEEK_START_DATE, "2026-09-05", week_start_at,
         week_end_at, percent, None, "statusline", "{}", account_key))


# ── the armed reset-to-zero marker ─────────────────────────────────────

def test_an_armed_marker_confirms_only_for_its_own_account(ns):
    """Account B's zero must not confirm account A's armed credit.

    The confirmation builds the credit from the marker's `baseline_pct`, so a
    cross-account confirm files account A's pre-credit level as account B's, and
    clearing the marker leaves account A unrepaired on the very tick that was
    supposed to repair it.
    """
    import _cctally_record as rec

    rec._arm_reset_zero_marker(
        WEEK_START_DATE, WEEK_END_AT, baseline_pct=14.0,
        first_zero_iso="2026-09-01T17:59:41+00:00",
        first_zero_capture_iso="2026-09-01T17:59:41Z",
        first_zero_identity="sa:o:acctA",
        account_key="acct-a")

    conn = ns["open_db"]()
    try:
        _snapshot(conn, "2026-09-01T17:50:00Z", 9.0, account_key="acct-b")
        conn.commit()
        rec.detect_reset_and_credit(
            conn, week_start_date=WEEK_START_DATE, week_end_at=WEEK_END_AT,
            weekly_percent=0.0, five_hour_window_key=None,
            five_hour_percent=None, as_of="2026-09-01T17:59:47Z",
            commit=True, ctx=None, account_key="acct-b",
            source_identity="sa:o:acctB",
            capture_at="2026-09-01T17:59:47Z")
        events = conn.execute(
            "SELECT account_key, observed_pre_credit_pct "
            "FROM week_reset_events").fetchall()
    finally:
        conn.close()

    assert [r["account_key"] for r in events if r["account_key"] == "acct-b"] \
        == [], (
        "account B received an event built from account A's baseline: "
        f"{[dict(r) for r in events]}")
    marker = rec._read_reset_zero_marker("acct-a")
    assert marker is not None, "account A's arm was cleared by account B's tick"
    assert marker[2] == 14.0, (
        "account A's arm was overwritten with account B's baseline")
    assert marker[6] == "acct-a"
    # Account B's own tick DID arm — that is the correct outcome for it, and it
    # is what makes the assertion above non-vacuous: the two arms coexist.
    other = rec._read_reset_zero_marker("acct-b")
    assert other is not None and other[2] == 9.0, other


def test_a_marker_written_before_the_account_field_still_confirms(ns):
    """The one-tick upgrade window. A marker written by the previous binary
    records no account, and rejecting it would drop a genuine armed reset on the
    tick that spans the upgrade — so it matches any account, exactly as it did
    before the field existed."""
    import _cctally_record as rec

    rec._reset_zero_marker_path().write_text(
        f"{WEEK_START_DATE} {WEEK_END_AT} 14.0 2026-09-01T17:59:41+00:00\n")
    marker = rec._read_reset_zero_marker()
    assert marker is not None
    assert marker[4] is None and marker[5] is None
    assert marker[6] is None

    conn = ns["open_db"]()
    try:
        _snapshot(conn, "2026-09-01T17:50:00Z", 14.0, account_key="acct-a")
        conn.commit()
        rec.detect_reset_and_credit(
            conn, week_start_date=WEEK_START_DATE, week_end_at=WEEK_END_AT,
            weekly_percent=0.0, five_hour_window_key=None,
            five_hour_percent=None, as_of="2026-09-01T17:59:47Z",
            commit=True, ctx=None, account_key="acct-a",
            source_identity="sa:o:acctA",
            capture_at="2026-09-01T17:59:47Z")
        events = conn.execute(
            "SELECT account_key FROM week_reset_events").fetchall()
    finally:
        conn.close()
    assert [r["account_key"] for r in events] == ["acct-a"], \
        [dict(r) for r in events]


# ── the boundary helper and the display coalescer ──────────────────────

def test_the_canonical_boundary_is_scoped_to_its_account(ns):
    """`get_recent_weeks(account_key=…)` scopes its initial rows and then asked
    this helper for the boundary without the account, so account B's week could
    be anchored on a boundary only account A ever reported."""
    conn = ns["open_db"]()
    try:
        _snapshot(conn, "2026-08-29T01:00:00Z", 5.0, account_key="acct-a",
                  week_start_at="2026-08-29T00:00:00+00:00",
                  week_end_at="2026-09-05T00:00:00+00:00")
        _snapshot(conn, "2026-08-29T02:00:00Z", 5.0, account_key="acct-b",
                  week_start_at="2026-08-29T06:00:00+00:00",
                  week_end_at="2026-09-05T06:00:00+00:00")
        conn.commit()
        import _cctally_weekrefs as wr
        a = wr._get_canonical_boundary_for_date(
            conn, WEEK_START_DATE, account_key="acct-a")
        b = wr._get_canonical_boundary_for_date(
            conn, WEEK_START_DATE, account_key="acct-b")
        merged = wr._get_canonical_boundary_for_date(conn, WEEK_START_DATE)
    finally:
        conn.close()
    assert a == ("2026-08-29T00:00:00+00:00", "2026-09-05T00:00:00+00:00"), a
    assert b == ("2026-08-29T06:00:00+00:00", "2026-09-05T06:00:00+00:00"), b
    # The account-blind read is unchanged: it is the merged view every
    # single-account install takes, and it must stay byte-identical there.
    assert merged == a, merged


def test_one_accounts_credit_does_not_move_another_accounts_week(ns):
    """`_apply_reset_events_to_weekrefs` loaded every account's events."""
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
            " week_start_date, observed_at_utc, observed_post_credit_pct, "
            " credit_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-09-01T12:00:00Z", "2026-09-01T12:00:00+00:00", WEEK_END_AT,
             "2026-09-01T12:00:00+00:00", 40.0, "acct-a", WEEK_START_DATE,
             "2026-09-01T12:05:00Z", 2.0, "sa:o:acctA"))
        conn.commit()
        import _cctally_core
        import _cctally_weekrefs as wr
        ref = _cctally_core.make_week_ref(
            week_start_date=WEEK_START_DATE, week_end_date="2026-09-05",
            week_start_at=WEEK_START_AT, week_end_at=WEEK_END_AT)
        for_b = wr._apply_reset_events_to_weekrefs(
            conn, [ref], account_key="acct-b")
        for_a = wr._apply_reset_events_to_weekrefs(
            conn, [ref], account_key="acct-a")
        credited_a = wr._week_ref_has_reset_event(
            conn, ref, account_key="acct-a")
        credited_b = wr._week_ref_has_reset_event(
            conn, ref, account_key="acct-b")
    finally:
        conn.close()
    # #703 + #707 §6.1: a credit is not a display boundary, for EITHER account.
    # This used to assert that account A's week WAS re-anchored to the credit
    # while B's was not; nothing is re-anchored now, so what the account scope
    # protects here is the query rather than the rewrite it used to drive.
    assert [r.week_start_at for r in for_b] == [WEEK_START_AT], (
        "account A's credit moved account B's week: "
        f"{[r.week_start_at for r in for_b]}")
    assert [r.week_start_at for r in for_a] == [WEEK_START_AT], (
        "the credit moment reached a displayed boundary: "
        f"{[r.week_start_at for r in for_a]}")
    # The credit is still visible to the account that owns it, through the
    # accounting read every consumer uses.
    assert credited_a is True
    assert credited_b is False


def test_one_accounts_moved_boundary_does_not_extend_another_accounts_week(ns):
    """`_extend_sub_weeks_past_a_moved_boundary` took no account scope (§6.2a).

    Its caller `_compute_subscription_weeks` carries one and scopes its own
    snapshot read, so the sub-weeks handed in belong to one account — but the
    `week_reset_events` query behind the extension read every account's rows.
    On a multi-account install account B's boundary-change credit therefore
    carried account A's week end forward, which moves A's cost window and its
    `usedPercent` bucket.
    """
    import datetime as dt

    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
            " week_start_date, observed_at_utc, observed_post_credit_pct, "
            " credit_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-09-01T12:00:00Z", WEEK_END_AT, "2026-09-07T00:00:00+00:00",
             "2026-09-01T12:00:00+00:00", 40.0, "acct-b", WEEK_START_DATE,
             "2026-09-01T12:05:00Z", 2.0, "sa:o:acctB"))
        conn.commit()
        import _lib_subscription_weeks as sw_mod
        sw = ns["SubWeek"](
            start_ts=WEEK_START_AT, end_ts=WEEK_END_AT,
            start_date=dt.date(2026, 8, 29), end_date=dt.date(2026, 9, 4),
            source="snapshot", display_start_date=dt.date(2026, 8, 29))
        for_a = sw_mod._apply_reset_events_to_subweeks(
            conn, [sw], account_key="acct-a")
        for_b = sw_mod._apply_reset_events_to_subweeks(
            conn, [sw], account_key="acct-b")
        merged = sw_mod._apply_reset_events_to_subweeks(conn, [sw])
    finally:
        conn.close()
    assert [w.end_ts for w in for_a] == [WEEK_END_AT], (
        "account B's moved boundary extended account A's week: "
        f"{[w.end_ts for w in for_a]}")
    assert [w.end_ts for w in for_b] == ["2026-09-07T00:00:00+00:00"], (
        "the owning account lost its own extension: "
        f"{[w.end_ts for w in for_b]}")
    # `None` stays the explicit merged read every single-account install takes.
    assert [w.end_ts for w in merged] == ["2026-09-07T00:00:00+00:00"], merged


def test_the_reset_aware_floor_is_scoped_to_its_account(ns):
    """The floor every clamp site consults. A real key sees only that account's
    credits; `None` stays the explicit merged read."""
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
            " week_start_date, observed_at_utc, observed_post_credit_pct, "
            " credit_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-09-01T12:00:00Z", None, None, "2026-09-01T12:00:00+00:00",
             40.0, "acct-a", WEEK_START_DATE, "2026-09-01T12:05:00Z", 2.0,
             "o:acctA"))
        conn.commit()
        import _cctally_core
        assert _cctally_core._reset_aware_floor(
            conn, WEEK_START_DATE, WEEK_START_AT, WEEK_END_AT,
            account_key="acct-b") is None
        assert _cctally_core._reset_aware_floor(
            conn, WEEK_START_DATE, WEEK_START_AT, WEEK_END_AT,
            account_key="acct-a") == "2026-09-01T12:05:00Z"
    finally:
        conn.close()
