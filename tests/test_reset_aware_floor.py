"""The accounting floor is the exact observation instant (#703 + #707 §5.3).

`effective_reset_at_utc` is hour-floored, and that rounding is what back-dated
the 2026-09-01 incident's epoch. The credit was first observed at 17:59:41, the
row recorded 17:00:00, and a genuine 13.0 snapshot captured at 17:33:38 — real
pre-credit history — therefore fell INSIDE the accounting window. It held the
reset-aware high-water mark at 13.0, the status line clamp raised its rendered
value to that maximum, `record-usage` stored the clamped number rather than the
incoming one, and every genuine 0.0 reading was suppressed as a lagging replica.
The state could not heal, because each tick re-asserted 13.0.

One timestamp cannot serve both human rounding and evidence membership. The
effective instant stays hour-floored and becomes display-only; the accounting
floor reads `COALESCE(observed_at_utc, effective_reset_at_utc)`, so a row that
records the exact instant uses it and a row that predates the column keeps the
only instant it has.
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


WEEK_START_DATE = "2026-08-29"
WEEK_START_AT = "2026-08-29T00:00:00+00:00"
WEEK_END_DT = dt.datetime(2026, 9, 5, 0, 0, 0, tzinfo=dt.timezone.utc)
WEEK_END_AT = WEEK_END_DT.isoformat(timespec="seconds")
WEEK_END_EPOCH = int(WEEK_END_DT.timestamp())
ACCOUNT = "unattributed"


def _snapshot(conn, captured_at, percent, *, account_key=ACCOUNT):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, page_url, source, payload_json, "
        " account_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (captured_at, WEEK_START_DATE, "2026-09-05", WEEK_START_AT,
         WEEK_END_AT, percent, None, "statusline", "{}", account_key))


def _credit(conn, *, effective, observed=None, post=0.0, pre=14.0,
            account_key=ACCOUNT, week_start_date=WEEK_START_DATE,
            boundaries=True, credit_key=None):
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
        " week_start_date, observed_at_utc, observed_post_credit_pct, "
        " credit_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (effective, effective if boundaries else None,
         WEEK_END_AT if boundaries else None, effective, pre, account_key,
         week_start_date, observed, post, credit_key))


def _floor(ns, conn, *, week_start_at=WEEK_START_AT, account_key=ACCOUNT):
    import _cctally_core
    return _cctally_core._reset_aware_floor(
        conn, WEEK_START_DATE, week_start_at, WEEK_END_AT,
        account_key=account_key)


@pytest.fixture
def incident_store(ns):
    conn = ns["open_db"]()
    try:
        _snapshot(conn, "2026-09-01T17:33:38Z", 13.0)
        _snapshot(conn, "2026-09-01T17:59:41Z", 0.0)
        _credit(conn, effective="2026-09-01T17:00:00+00:00",
                observed="2026-09-01T17:59:41Z", post=0.0, pre=14.0,
                credit_key="sa:o:firstzero")
        conn.commit()
        yield conn
    finally:
        conn.close()


def test_the_floor_is_the_exact_observation_not_the_hour_floor(
    ns, incident_store
):
    assert _floor(ns, incident_store) == "2026-09-01T17:59:41Z"


def test_the_incident_hwm_excludes_the_pre_credit_replica(ns, incident_store):
    """The reported defect, end to end over the resolver every clamp reads."""
    import _cctally_record as rec
    assert rec._resolve_reset_aware_hwm(
        incident_store, WEEK_START_DATE, WEEK_START_AT, WEEK_END_AT,
        account_key=ACCOUNT) == 0.0, (
        "the floor is still hour-floored to 17:00 and includes the 13.0")


def test_a_row_without_an_observation_instant_keeps_the_effective_one(ns):
    """NULL means the row predates this change or its source genuinely lacked
    the fact. Nothing is synthesized; the only instant it has is used."""
    conn = ns["open_db"]()
    try:
        _credit(conn, effective="2026-09-01T17:00:00+00:00", observed=None,
                credit_key="legacy:o:one")
        conn.commit()
        assert _floor(ns, conn) == "2026-09-01T17:00:00+00:00"
    finally:
        conn.close()


def test_a_manual_credit_before_an_automatic_one_keeps_its_floor(ns):
    """The narrowing this helper must not inherit.

    The two legs it collapses used DIFFERENT predicates: the reset-event leg
    filtered the effective instant into `[week_start_at, week_end_at)` while the
    credit-floor leg matched `week_start_date` with no time bound. A week that
    also carries an automatic credit has its `week_start_at` overridden to that
    credit's effective instant by the display layer, so a manual credit recorded
    EARLIER falls outside the range — and after the epoch rebuild
    `weekly_credit_floors` is permanently empty, so the range predicate is the
    only one left. The week's own identity is what selects a credit.
    """
    conn = ns["open_db"]()
    try:
        _credit(conn, effective="2026-08-30T09:00:00+00:00",
                observed="2026-08-30T09:12:00Z", post=31.0, pre=46.0,
                boundaries=False, credit_key="o:manual")
        _credit(conn, effective="2026-08-30T14:00:00+00:00",
                observed="2026-08-30T14:03:00Z", post=5.0, pre=40.0,
                credit_key="sa:o:auto")
        conn.commit()
        # The display layer has rewritten week_start_at to the automatic
        # credit's effective instant.
        assert _floor(ns, conn, week_start_at="2026-08-30T14:00:00+00:00") == \
            "2026-08-30T14:03:00Z"
        # And the earlier manual credit is still reachable in its own right.
        conn.execute("DELETE FROM week_reset_events WHERE credit_key = ?",
                     ("sa:o:auto",))
        conn.commit()
        assert _floor(ns, conn, week_start_at="2026-08-30T14:00:00+00:00") == \
            "2026-08-30T09:12:00Z", (
            "the manual credit's floor was lost to a range predicate the "
            "display layer had already narrowed")
    finally:
        conn.close()


def test_the_floor_is_scoped_to_its_own_account(ns):
    conn = ns["open_db"]()
    try:
        _credit(conn, effective="2026-08-30T09:00:00+00:00",
                observed="2026-08-30T09:12:00Z", account_key="acct-a",
                credit_key="sa:o:a")
        conn.commit()
        assert _floor(ns, conn, account_key="acct-b") is None
        assert _floor(ns, conn, account_key="acct-a") == "2026-08-30T09:12:00Z"
        assert _floor(ns, conn, account_key=None) == "2026-08-30T09:12:00Z"
    finally:
        conn.close()


def test_the_latest_credit_wins_by_its_accounting_instant(ns):
    conn = ns["open_db"]()
    try:
        _credit(conn, effective="2026-08-30T14:00:00+00:00", observed=None,
                credit_key="legacy:o:late")
        _credit(conn, effective="2026-08-30T09:00:00+00:00",
                observed="2026-08-30T15:00:00Z", credit_key="sa:o:later")
        conn.commit()
        assert _floor(ns, conn) == "2026-08-30T15:00:00Z"
    finally:
        conn.close()


# ── end to end: the genuine zero is accepted rather than clamp-suppressed ──

def _tick(ns, monkeypatch, *, at, percent):
    monkeypatch.setenv("CCTALLY_AS_OF", at)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    return ns["cmd_record_usage"](argparse.Namespace(
        percent=percent, resets_at=WEEK_END_EPOCH, five_hour_percent=None,
        five_hour_resets_at=None, week_start_name=None))


def test_the_genuine_zero_is_accepted_rather_than_clamp_suppressed(
    ns, monkeypatch
):
    """The incident driven through `record-usage`.

    The 13.0 at 17:33:38 stays — it is real pre-credit history, and §5.1 keeps
    it deliberately. What changes is that the accounting floor is 17:59:41
    rather than 17:00, so the 13.0 is outside the epoch, the clamp stops raising
    the rendered value, and the readings after the credit are stored as
    observed.
    """
    assert _tick(ns, monkeypatch, at="2026-09-01T17:33:38Z", percent=13.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:40:00Z", percent=14.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:41Z", percent=0.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T17:59:47Z", percent=0.0) == 0
    assert _tick(ns, monkeypatch, at="2026-09-01T18:18:50Z", percent=1.0) == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT observed_at_utc, observed_post_credit_pct "
            "FROM week_reset_events ORDER BY id").fetchall()
        assert len(events) == 1, [dict(r) for r in events]
        assert events[0]["observed_at_utc"] == "2026-09-01T17:59:41Z"
        assert events[0]["observed_post_credit_pct"] == 0.0

        import _cctally_record as rec
        assert rec._resolve_reset_aware_hwm(
            conn, WEEK_START_DATE, WEEK_START_AT, WEEK_END_AT,
            account_key=None) == 1.0

        stored = [
            (r["captured_at_utc"], r["weekly_percent"])
            for r in conn.execute(
                "SELECT captured_at_utc, weekly_percent "
                "FROM weekly_usage_snapshots WHERE week_start_date = ? "
                "ORDER BY captured_at_utc, id", (WEEK_START_DATE,))
        ]
    finally:
        conn.close()
    assert ("2026-09-01T18:18:50Z", 1.0) in stored, stored
    assert ("2026-09-01T17:33:38Z", 13.0) in stored, (
        "the genuine pre-credit observation was destroyed; §5.1 keeps it")
