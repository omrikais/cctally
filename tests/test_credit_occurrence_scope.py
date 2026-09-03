"""Manual-credit occurrence resolution is per account, and deterministic.

Two Tranche 2 review findings live here.

Section 6.2a requires every added or changed query to be account-scoped, and the
manual occurrence helpers were not. They select on `week_start_date` plus the
NULL-boundary shape and nothing else, so on a multi-account install account B
running `record-credit --at <A's instant>` is refused by a message naming
account A's credit, and B's plain run classifies A's half-applied credit as B's
own completion — reusing A's `credit_key`, so B's row carries A's identity.

And `_EXISTING_MANUAL_CREDIT_SQL` ordered by `unixepoch(effective_reset_at_utc)
DESC, id DESC`. Two credits inside one hour are legal now and share the floored
instant, so the tiebreak was `id DESC` — a projection-local number a rebuild
reassigns, which section 5.3 states is unusable.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script, redirect_paths

WEEK_START_DATE = "2026-08-29"
WEEK_END_AT = "2026-09-05T00:00:00+00:00"
ACCOUNT_A = "acct-a"
ACCOUNT_B = "acct-b"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _manual_credit(conn, *, account_key, observed_at, effective_at,
                   pre_pct=46.0, credit_key=None, credit_order=None,
                   journal_id=None):
    """One manual credit: the NULL-boundary shape every helper classifies on."""
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
        " week_start_date, observed_at_utc, confirming_capture_at_utc, "
        " observed_post_credit_pct, credit_key, credit_order, journal_id) "
        "VALUES (?,NULL,NULL,?,?,?,?,?,?,?,?,?,?)",
        (observed_at, effective_at, pre_pct, account_key, WEEK_START_DATE,
         observed_at, observed_at, 2.0, credit_key, credit_order, journal_id))
    conn.commit()


def _at(iso):
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))


def test_force_does_not_name_another_accounts_credit(ns):
    import _cctally_record as rec
    conn = ns["open_db"]()
    try:
        _manual_credit(conn, account_key=ACCOUNT_A,
                       observed_at="2026-08-30T10:00:00+00:00",
                       effective_at="2026-08-30T10:00:00+00:00",
                       credit_key="o:aaa", credit_order=10,
                       journal_id="o:aaa")
        occ, _completion, refusal = rec._resolve_credit_occurrence(
            conn, week_start_date=WEEK_START_DATE,
            at_dt=_at("2026-08-30T10:00:00Z"), is_force=True,
            account_key=ACCOUNT_B)
        assert occ is None
        assert refusal is not None and "no credit recorded" in refusal, refusal
    finally:
        conn.close()


def test_a_plain_run_does_not_complete_another_accounts_half_applied_credit(ns):
    """The half-applied scan is the destructive one: completing A's credit under
    B reuses A's `credit_key`, so B's row carries A's identity."""
    import _cctally_record as rec
    conn = ns["open_db"]()
    try:
        _manual_credit(conn, account_key=ACCOUNT_A,
                       observed_at="2026-08-30T10:00:00+00:00",
                       effective_at="2026-08-30T10:00:00+00:00",
                       credit_key="o:aaa", credit_order=10,
                       journal_id="o:aaa")
        occ, is_completion, refusal = rec._resolve_credit_occurrence(
            conn, week_start_date=WEEK_START_DATE,
            at_dt=_at("2026-08-30T14:00:00Z"), is_force=False,
            account_key=ACCOUNT_B)
        assert refusal is None, refusal
        assert is_completion is False
        assert occ is None, (
            "account B adopted account A's half-applied credit")
    finally:
        conn.close()


def test_the_latest_manual_credit_is_scoped_to_its_account(ns):
    import _cctally_record as rec
    conn = ns["open_db"]()
    try:
        _manual_credit(conn, account_key=ACCOUNT_A,
                       observed_at="2026-08-30T10:00:00+00:00",
                       effective_at="2026-08-30T10:00:00+00:00",
                       pre_pct=46.0, credit_key="o:aaa", credit_order=10)
        assert rec._latest_manual_credit(
            conn, WEEK_START_DATE, account_key=ACCOUNT_B) is None
        assert rec._latest_manual_credit(
            conn, WEEK_START_DATE, account_key=ACCOUNT_A)[2] == 46.0
    finally:
        conn.close()


def test_the_latest_manual_credit_breaks_an_in_hour_tie_by_credit_order(ns):
    """Both credits floor to the same hour, so `effective_reset_at_utc` cannot
    separate them and `id DESC` is not allowed to. The accounting instant
    decides, and `credit_order` is the tiebreak when even that ties."""
    import _cctally_record as rec
    conn = ns["open_db"]()
    try:
        # Inserted newest-first, so `id DESC` would answer with the 14:05 row.
        _manual_credit(conn, account_key=ACCOUNT_A,
                       observed_at="2026-08-30T14:05:00+00:00",
                       effective_at="2026-08-30T14:00:00+00:00",
                       pre_pct=46.0, credit_key="o:early", credit_order=10)
        _manual_credit(conn, account_key=ACCOUNT_A,
                       observed_at="2026-08-30T14:47:00+00:00",
                       effective_at="2026-08-30T14:00:00+00:00",
                       pre_pct=31.0, credit_key="o:late", credit_order=20)
        got = rec._latest_manual_credit(
            conn, WEEK_START_DATE, account_key=ACCOUNT_A)
        assert got["credit_key"] == "o:late", dict(got)
        assert got[2] == 31.0
    finally:
        conn.close()
