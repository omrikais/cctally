"""#832 — the status line's five-hour projection resolves its reset generation
for the EXACT projected account (case M13, spec 4.5).

HISTORY. Until #825/#832 this module was a characterization of the defect: the
projection called `_resolve_active_five_hour_reset_event_id(conn, canonical)`
with no account, the parameter defaulted to the `unattributed` sentinel, and a
foreign `unattributed` event therefore (1) became the real account's
`reset_generation`, entering its retry signature, and (2) supplied the
`floor_epoch` that withheld the real account's own five-hour rows. Its
expectations are reversed here, as the old docstring required: the module is
rewritten, never deleted.

WHAT THIS ASSERTS NOW.

  * A foreign account's event changes neither the projected percentage, nor
    the projected generation, nor the retry signature built from it, and its
    instant never filters the projected account's rows.
  * The generation is LOGICAL (effective instant, detection instant, stable
    journal identity), so a real `db rebuild --db stats` that renumbers the
    SQLite rowid leaves it unchanged.
  * The account's OWN genuine event does change it (non-vacuity).
  * The resolver is typed and has no default account; a database failure
    propagates instead of reading as "no event".

THE CLOCK IS PINNED. The projection reads `time.time()` for the five-hour
plausibility gate, so `time.time` is patched to a fixed instant.

The projection helper below calls `_read_db_projection_once` with the explicit
account when the API takes one, and otherwise relies on the patched active
account. That is only so the same assertions can be run against the pre-fix
code to prove they are RED for the #832 reason (the saved RED log under
`.local/reports/825-832-account-bound/red/`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import inspect
import sqlite3
import sys
import time

import pytest

from conftest import load_script, redirect_paths

#: 2026-06-13T00:00:00Z.
PINNED_NOW = 1781308800
#: Inside the gate's `now + 6h` upper bound.
RESETS_EPOCH = PINNED_NOW + 3600
#: The foreign event's effective instant, 10 minutes before the pinned now.
EVENT_EPOCH = PINNED_NOW - 600
REAL_ACCOUNT = "acct-real-832"
UNATTRIBUTED = "unattributed"


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setattr(time, "time", lambda: float(PINNED_NOW))
    import _cctally_statusline as sl
    # Pre-fix code scoped the projection to the active account; the fixed code
    # takes the account explicitly. Patching both keeps one assertion set.
    monkeypatch.setattr(sl, "_statusline_active_account",
                        lambda: REAL_ACCOUNT, raising=False)
    return sys.modules["cctally"]


def _project(account_key: str):
    import _cctally_statusline as sl
    if "account_key" in inspect.signature(sl._read_db_projection_once).parameters:
        return sl._read_db_projection_once(account_key=account_key)
    sl._statusline_active_account = lambda: account_key
    return sl._read_db_projection_once()


def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _canonical(app) -> int:
    return int(app._canonical_5h_window_key(RESETS_EPOCH))


def _insert_five_hour_snapshot(app, *, captured_epoch, five_hour_percent,
                               account_key):
    conn = app.open_db()
    try:
        cur = conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, page_url, source, payload_json, "
            " five_hour_percent, five_hour_resets_at, five_hour_window_key, "
            " account_key) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, 'statusline', '{}', ?, ?, ?, ?)",
            (_iso(captured_epoch), "2026-06-08", "2026-06-15",
             "2026-06-08T00:00:00Z", "2026-06-15T00:00:00Z", 40.0,
             five_hour_percent, _iso(RESETS_EPOCH), _canonical(app),
             account_key),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _insert_five_hour_reset_event(app, *, account_key, effective=EVENT_EPOCH):
    conn = app.open_db()
    try:
        cur = conn.execute(
            "INSERT INTO five_hour_reset_events "
            "(detected_at_utc, five_hour_window_key, prior_percent, "
            " post_percent, effective_reset_at_utc, account_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_iso(effective), _canonical(app), 30.0, 10.0,
             _iso(effective), account_key),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_m13_foreign_unattributed_event_changes_neither_projection_nor_retry_generation(app):
    """Case M13 (RED before the fix): the real account's projection and retry
    signature are identical with and without a foreign `unattributed` event."""
    import _lib_statusline_candidates as candidates

    canonical = _canonical(app)
    _insert_five_hour_snapshot(
        app, captured_epoch=PINNED_NOW - 300, five_hour_percent=12.0,
        account_key=REAL_ACCOUNT)
    before = _project(REAL_ACCOUNT)
    assert before.five_hour is not None
    reduced = candidates.AxisValue(
        percent=12.0, raw_resets_at=RESETS_EPOCH, canonical_key=canonical)
    signature_before = candidates._build_retry_signature(reduced, before.five_hour)

    _insert_five_hour_reset_event(app, account_key=UNATTRIBUTED)
    after = _project(REAL_ACCOUNT)
    assert after.five_hour is not None
    assert after.five_hour.percent == pytest.approx(12.0)
    assert after.five_hour.reset_generation == before.five_hour.reset_generation, (
        "a foreign `unattributed` event became the real account's reset "
        "generation (#832)")
    assert candidates._build_retry_signature(
        reduced, after.five_hour) == signature_before, (
        "a foreign event changed the real account's retry signature, so it "
        "would reopen a spent publication attempt (#832)")


def test_m13_foreign_event_instant_never_filters_the_account_rows(app):
    """The foreign event's effective instant is not this account's floor: a
    row captured before it still projects."""
    _insert_five_hour_reset_event(app, account_key=UNATTRIBUTED)
    _insert_five_hour_snapshot(
        app, captured_epoch=EVENT_EPOCH - 300, five_hour_percent=12.0,
        account_key=REAL_ACCOUNT)
    projection = _project(REAL_ACCOUNT)
    assert projection.five_hour is not None, (
        "a foreign event's instant withheld the account's own five-hour row")
    assert projection.five_hour.percent == pytest.approx(12.0)


def test_m13_own_event_changes_the_generation_and_floors_its_rows(app):
    """Non-vacuity: the account's OWN event is a new logical generation and
    its instant filters that account's pre-event rows."""
    _insert_five_hour_snapshot(
        app, captured_epoch=PINNED_NOW - 300, five_hour_percent=12.0,
        account_key=REAL_ACCOUNT)
    before = _project(REAL_ACCOUNT)
    _insert_five_hour_reset_event(app, account_key=REAL_ACCOUNT)
    after = _project(REAL_ACCOUNT)
    assert after.five_hour is not None
    assert after.five_hour.reset_generation != before.five_hour.reset_generation

    conn = app.open_db()
    try:
        conn.execute("DELETE FROM weekly_usage_snapshots")
        conn.commit()
    finally:
        conn.close()
    _insert_five_hour_snapshot(
        app, captured_epoch=EVENT_EPOCH - 300, five_hour_percent=12.0,
        account_key=REAL_ACCOUNT)
    assert _project(REAL_ACCOUNT).five_hour is None


def _record(app, monkeypatch, *, five, at):
    monkeypatch.setattr(time, "time", lambda: float(PINNED_NOW + at))
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(PINNED_NOW + at))
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    args = argparse.Namespace(
        percent=20, resets_at=str(PINNED_NOW + 3 * 86400),
        five_hour_percent=five, five_hour_resets_at=str(RESETS_EPOCH),
        source="statusline")
    assert app.cmd_record_usage(args, nudge_dashboard=False) == 0


def test_m13_real_rebuild_renumbering_keeps_the_logical_generation(app, monkeypatch):
    """Case M13 (RED before the fix): a real `db rebuild --db stats` renumbers
    `five_hour_reset_events.id`; the logical generation, and therefore the
    retry signature, is unchanged."""
    import _lib_statusline_candidates as candidates

    monkeypatch.setattr(sys.modules["_cctally_statusline"],
                        "_statusline_active_account", lambda: UNATTRIBUTED,
                        raising=False)
    _record(app, monkeypatch, five=70, at=0)
    _record(app, monkeypatch, five=20, at=2)
    _record(app, monkeypatch, five=20, at=4)
    conn = app.open_db()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM five_hour_reset_events WHERE account_key = ?",
            (UNATTRIBUTED,)).fetchone()[0] == 1, (
            "real recording must create the account's logical reset event")
        conn.execute("UPDATE five_hour_reset_events SET id = id + 1000")
        conn.commit()
    finally:
        conn.close()
    before = _project(UNATTRIBUTED)
    assert before.five_hour is not None

    args = app.build_parser().parse_args(["db", "rebuild", "--db", "stats", "--json"])
    assert args.func(args) == 0
    conn = app.open_db()
    try:
        assert conn.execute(
            "SELECT id FROM five_hour_reset_events").fetchone()[0] < 1000, (
            "the rebuild must actually renumber the row for this to be decisive")
    finally:
        conn.close()
    after = _project(UNATTRIBUTED)
    assert after.five_hour.reset_generation == before.five_hour.reset_generation, (
        "a rowid renumbering changed the reset generation (#832)")
    value = candidates.AxisValue(10.0, RESETS_EPOCH, after.five_hour.canonical_key)
    assert (candidates._build_retry_signature(value, before.five_hour)
            == candidates._build_retry_signature(value, after.five_hour))


def test_resolver_is_typed_exact_account_and_logically_ordered(app):
    """The resolver requires the account, returns a typed result naming it,
    and orders by effective instant, never by rowid."""
    canonical = _canonical(app)
    conn = app.open_db()
    try:
        with pytest.raises(TypeError):
            app.resolve_five_hour_reset_event(conn, canonical)
        none = app.resolve_five_hour_reset_event(
            conn, canonical, account_key=REAL_ACCOUNT)
        assert isinstance(none, app.NoFiveHourResetEvent)
        assert (none.account_key, none.canonical_window_key) == (REAL_ACCOUNT, canonical)
        for physical, effective in ((100, EVENT_EPOCH), (1, EVENT_EPOCH + 60)):
            conn.execute(
                "INSERT INTO five_hour_reset_events (id, detected_at_utc, "
                "five_hour_window_key, prior_percent, post_percent, "
                "effective_reset_at_utc, account_key) VALUES (?,?,?,?,?,?,?)",
                (physical, _iso(effective), canonical, 70, 20, _iso(effective),
                 REAL_ACCOUNT))
        conn.commit()
        event = app.resolve_five_hour_reset_event(
            conn, canonical, account_key=REAL_ACCOUNT)
        assert isinstance(event, app.FiveHourResetEvent)
        assert event.account_key == REAL_ACCOUNT
        assert event.effective_reset_at_utc == _iso(EVENT_EPOCH + 60)
        assert event.floor_epoch == EVENT_EPOCH + 60
        assert event.logical_generation.startswith(_iso(EVENT_EPOCH + 60) + "|")
        assert "100" not in event.logical_generation.split("|")
    finally:
        conn.close()


def test_resolver_database_failure_propagates(app):
    class Broken:
        def execute(self, *args, **kwargs):
            raise sqlite3.DatabaseError("read failed")

    with pytest.raises(sqlite3.DatabaseError, match="read failed"):
        app.resolve_five_hour_reset_event(
            Broken(), _canonical(app), account_key=REAL_ACCOUNT)
