"""#750 S3 Task A6 — legacy reset rows across the epoch cutover.

Spec §1.7. A pre-journal store carries `week_reset_events` rows written by a
binary that hour-floored `effective_reset_at_utc`, under a table-level
`UNIQUE(account_key, old_week_end_at, new_week_end_at)` that epoch 1013
retired in favour of two partial indexes. `CREATE TABLE IF NOT EXISTS` cannot
change an existing table, so the cutover needs an idempotent one-table rebuild
that preserves row ids, and it has to run BEFORE `_backfill_week_reset_events`
rather than merely before the cutover export.

The backfill then has to recognize a legacy origin-null row of EITHER shape,
because it now records the exact capture second while the legacy row records
the hour it fell in. The comparison is against `effective_reset_at_utc` in both
cases, because that is the only column every legacy writer filled with the
instant:

* the boundary-shift shape has `old_week_end_at` = the prior provider boundary;
* the current in-place shape has `old == effective`, so either column matches;
* the PRE-v1.7.2 in-place shape is `(cur_end, cur_end)`, whose boundary columns
  both hold the week end and carry no instant at all.

Miss any of them and a cutover preserves the old row while the backfill mints a
second exact-second row beside it.

The guard is scoped to the legacy window and is unreachable after cutover, so
a genuine later reset is always admitted — including the second in-place
credit in one week that the backfill's own `new_week_end_at` pre-check used to
refuse (#732).
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

LEGACY_WEEK_RESET_DDL = """
    CREATE TABLE week_reset_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        detected_at_utc        TEXT NOT NULL,
        old_week_end_at        TEXT NOT NULL,
        new_week_end_at        TEXT NOT NULL,
        effective_reset_at_utc TEXT NOT NULL,
        observed_pre_credit_pct REAL,
        account_key TEXT NOT NULL DEFAULT 'unattributed',
        UNIQUE(account_key, old_week_end_at, new_week_end_at)
    )
"""

LEGACY_SNAPSHOTS_DDL = """
    CREATE TABLE weekly_usage_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at_utc TEXT NOT NULL,
        week_start_date TEXT NOT NULL,
        week_end_date TEXT NOT NULL,
        week_start_at TEXT,
        week_end_at TEXT,
        weekly_percent REAL NOT NULL,
        page_url TEXT,
        source TEXT NOT NULL DEFAULT 'userscript',
        payload_json TEXT NOT NULL,
        account_key TEXT NOT NULL DEFAULT 'unattributed',
        journal_id TEXT
    )
"""

#: A raw observation id, so the backfill's candidate carries an ORIGIN.
#:
#: This models a PARTIALLY upgraded store: a newer binary ran the schema apply
#: — which is where `journal_id` is added to `weekly_usage_snapshots` — and
#: then failed before the cutover committed, so `user_version` is still at the
#: legacy head on the next open. It is the shape that needs the guard. On a
#: store that never saw that apply, the snapshots have no `journal_id` at all,
#: every backfilled candidate takes a NULL origin, and the legacy tuple partial
#: index already dedups both shapes.
ORIGIN_ID = "o:" + "a1b2c3d4e5f60718"

WEEK_START_DATE = "2026-08-29"
WEEK_START_AT = "2026-08-29T15:00:00+00:00"
WEEK_END_AT = "2026-09-05T15:00:00+00:00"
NEXT_WEEK_END_AT = "2026-09-08T15:00:00+00:00"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _snapshot(conn, *, captured, percent, week_end_at=WEEK_END_AT,
              journal_id=None):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json, journal_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (captured, WEEK_START_DATE, week_end_at[:10], WEEK_START_AT,
         week_end_at, percent, "test", "{}", journal_id))


def _build_legacy_store(rows=(), snapshots=()):
    """A pre-journal stats.db: legacy table shape, `user_version` at the frozen
    legacy head, no journal identity anywhere."""
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.executescript(LEGACY_SNAPSHOTS_DDL + ";" + LEGACY_WEEK_RESET_DDL)
        for captured, percent, week_end_at, journal_id in snapshots:
            _snapshot(conn, captured=captured, percent=percent,
                      week_end_at=week_end_at, journal_id=journal_id)
        for detected, old, new, effective, pre in rows:
            conn.execute(
                "INSERT INTO week_reset_events "
                "(detected_at_utc, old_week_end_at, new_week_end_at, "
                " effective_reset_at_utc, observed_pre_credit_pct) "
                "VALUES (?,?,?,?,?)",
                (detected, old, new, effective, pre))
        conn.execute(
            f"PRAGMA user_version = {_cctally_core.LEGACY_STATS_HEAD}")
        conn.commit()
    finally:
        conn.close()


def _events(ns):
    conn = ns["open_db"]()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, old_week_end_at, new_week_end_at, "
            "effective_reset_at_utc, origin_observation_id "
            "FROM week_reset_events ORDER BY id")]
    finally:
        conn.close()


def _table_sql(ns):
    conn = ns["open_db"]()
    try:
        return str(conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='week_reset_events'").fetchone()[0])
    finally:
        conn.close()


# --------------------------------------------------------------------------
# The two legacy shapes
# --------------------------------------------------------------------------

def test_a6_a_legacy_in_place_row_survives_the_cutover_once(ns):
    """The in-place shape: `old == effective`, hour-floored. The backfill's
    own candidate for the same snapshots lands on the exact capture second, so
    only the hour-normalized comparison recognizes them as the same event."""
    _build_legacy_store(
        rows=[("2026-09-01T10:23:45+00:00", "2026-09-01T10:00:00+00:00",
               WEEK_END_AT, "2026-09-01T10:00:00+00:00", 67.0)],
        snapshots=[("2026-09-01T09:00:00Z", 67.0, WEEK_END_AT,
                    "b:weekly_usage_snapshots:1"),
                   ("2026-09-01T10:23:45Z", 2.0, WEEK_END_AT,
                    f"sa:{ORIGIN_ID}")])

    events = _events(ns)
    assert len(events) == 1, events
    assert events[0]["id"] == 1, "the cutover did not preserve the row id"
    assert events[0]["origin_observation_id"] is None
    assert events[0]["effective_reset_at_utc"] == "2026-09-01T10:00:00+00:00", (
        "the legacy row's own instant must not be rewritten")


def test_a6_a_pre_v172_in_place_row_survives_the_cutover_once(ns):
    """The shape the backfill's deleted `already` pre-check named: a store
    written before the v1.7.2 round-2 fix records an in-place credit as
    `(cur_end, cur_end)`, so BOTH boundary columns hold the week end and
    neither carries the reset instant. Only `effective_reset_at_utc` does, and
    comparing `old_week_end_at` instead would match nothing and mint a second
    row for the same credit."""
    _build_legacy_store(
        rows=[("2026-09-01T10:23:45+00:00", WEEK_END_AT, WEEK_END_AT,
               "2026-09-01T10:00:00+00:00", 67.0)],
        snapshots=[("2026-09-01T09:00:00Z", 67.0, WEEK_END_AT,
                    "b:weekly_usage_snapshots:1"),
                   ("2026-09-01T10:23:45Z", 2.0, WEEK_END_AT,
                    f"sa:{ORIGIN_ID}")])

    events = _events(ns)
    assert len(events) == 1, events
    assert events[0]["id"] == 1, "the cutover did not preserve the row id"
    assert events[0]["origin_observation_id"] is None
    assert events[0]["old_week_end_at"] == WEEK_END_AT, (
        "the legacy row's own boundary columns must not be rewritten")


def test_a6_a_legacy_boundary_shift_row_survives_the_cutover_once(ns):
    """The boundary-shift shape: `old != effective`. It had no pre-check at
    all and relied entirely on the retired table-level uniqueness, so without
    an explicit guard the exact-second backfill mints a second row beside it.
    """
    _build_legacy_store(
        rows=[("2026-09-01T10:23:45+00:00", WEEK_END_AT, NEXT_WEEK_END_AT,
               "2026-09-01T10:00:00+00:00", None)],
        snapshots=[("2026-09-01T09:00:00Z", 67.0, WEEK_END_AT,
                    "b:weekly_usage_snapshots:1"),
                   ("2026-09-01T10:23:45Z", 2.0, NEXT_WEEK_END_AT,
                    f"sa:{ORIGIN_ID}")])

    events = _events(ns)
    assert len(events) == 1, events
    assert events[0]["id"] == 1, "the cutover did not preserve the row id"
    assert events[0]["origin_observation_id"] is None
    assert events[0]["old_week_end_at"] != events[0]["effective_reset_at_utc"]


def test_a6_a_legacy_boundary_shift_row_does_not_suppress_an_in_place_credit(
        ns):
    """The guard matches a legacy row of the candidate's OWN shape.

    Inside the legacy window a boundary-shift row and an in-place candidate
    share `new_week_end_at` by construction, because the shift moves the
    boundary TO the end the later in-place credit happens under. When both
    also fall in one clock hour, an account plus an end plus an hour cannot
    tell them apart, and matching on those alone discards the in-place credit
    — the suppression maintainer decision 4 disfavours. Both candidates are
    derived here from one snapshot series: the boundary shift is correctly
    recognized as already recorded, and the in-place credit is admitted.
    """
    second_origin = "o:" + "9f8e7d6c5b4a3928"
    _build_legacy_store(
        rows=[("2026-09-01T10:05:00+00:00", WEEK_END_AT, NEXT_WEEK_END_AT,
               "2026-09-01T10:00:00+00:00", None)],
        snapshots=[
            ("2026-09-01T09:00:00Z", 67.0, WEEK_END_AT,
             "b:weekly_usage_snapshots:1"),
            # The boundary shift the legacy row already records.
            ("2026-09-01T10:05:00Z", 2.0, NEXT_WEEK_END_AT,
             f"sa:{ORIGIN_ID}"),
            # A climb inside the new week, then a genuine in-place credit in
            # the SAME clock hour as the shift.
            ("2026-09-01T10:20:00Z", 40.0, NEXT_WEEK_END_AT, None),
            ("2026-09-01T10:40:00Z", 2.0, NEXT_WEEK_END_AT,
             f"sa:{second_origin}"),
        ])

    events = _events(ns)
    assert len(events) == 2, (
        "the legacy boundary-shift row suppressed the in-place credit", events)
    assert events[0]["id"] == 1
    assert events[0]["origin_observation_id"] is None
    assert events[0]["new_week_end_at"] == NEXT_WEEK_END_AT
    assert events[1]["origin_observation_id"] == second_origin
    assert events[1]["old_week_end_at"] == events[1][
        "effective_reset_at_utc"] == "2026-09-01T10:40:00+00:00"


def test_a6_the_cutover_rebuilds_the_table_into_the_current_shape(ns):
    """The retired table-level UNIQUE is gone and both partial indexes are in
    place, with row ids preserved. Without the rebuild the old constraint
    survives the cutover and keeps refusing a legitimate second credit."""
    _build_legacy_store(
        rows=[("2026-09-01T10:23:45+00:00", "2026-09-01T10:00:00+00:00",
               WEEK_END_AT, "2026-09-01T10:00:00+00:00", 67.0)])

    sql = _table_sql(ns)
    # Stripping every space already normalizes `UNIQUE (` to `UNIQUE(`, so one
    # replace is the whole comparison.
    assert "UNIQUE(" not in sql.replace(" ", ""), (
        f"the retired table-level UNIQUE survived the cutover: {sql}")
    assert "origin_observation_id" in sql

    conn = ns["open_db"]()
    try:
        indexes = {str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='week_reset_events'")}
    finally:
        conn.close()
    assert {"idx_week_reset_events_origin",
            "idx_week_reset_events_legacy_tuple"} <= indexes, indexes
    assert _events(ns)[0]["id"] == 1


def test_a6_the_rebuild_is_idempotent(ns):
    """A second CALL of the rebuild must be a no-op that renumbers nothing.

    Re-opening the store proves nothing here: the first open stamps
    `user_version` at the current head, so every later open returns at the
    epoch gate and
    the rebuild is never reached a second time. Comparing two reads of an
    unchanged database would pass against a rebuild that renumbers row ids on
    every call, which is the failure this test exists to catch, so the
    idempotence is asserted against the function itself.
    """
    _build_legacy_store(
        rows=[("2026-09-01T10:23:45+00:00", "2026-09-01T10:00:00+00:00",
               WEEK_END_AT, "2026-09-01T10:00:00+00:00", 67.0)])
    first = _events(ns)
    first_sql = _table_sql(ns)

    conn = ns["open_db"]()
    try:
        assert _cctally_core._rebuild_retired_week_reset_uniqueness(
            conn) is False, (
            "the cutover shape still carries a UNIQUE-constraint index, so "
            "the rebuild would run on every open")
        assert _cctally_core._rebuild_retired_week_reset_uniqueness(
            conn) is False
        conn.commit()
    finally:
        conn.close()

    assert _events(ns) == first
    assert _table_sql(ns) == first_sql


# --------------------------------------------------------------------------
# The widened pre-check
# --------------------------------------------------------------------------

def test_a6_the_backfill_mints_a_second_in_place_credit_for_one_week(ns):
    """#732. The backfill's `already` pre-check was keyed on
    `new_week_end_at` plus account — the same over-broad shape the live path
    carried — so it refused a second in-place credit in one week no matter how
    genuine. Two observed climbs, each followed by a credit, must produce two
    events."""
    conn = ns["open_db"]()
    try:
        for captured, pct in (
            ("2026-09-01T09:00:00Z", 67.0),
            ("2026-09-01T10:23:45Z", 2.0),   # first credit
            ("2026-09-02T09:00:00Z", 58.0),
            ("2026-09-02T11:05:10Z", 3.0),   # second credit
        ):
            _snapshot(conn, captured=captured, percent=pct)
        conn.execute("DELETE FROM week_reset_events")
        conn.commit()
        ns["_backfill_week_reset_events"](conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT effective_reset_at_utc, new_week_end_at "
            "FROM week_reset_events ORDER BY id")]
    finally:
        conn.close()

    assert len(rows) == 2, rows
    assert [r["effective_reset_at_utc"] for r in rows] == [
        "2026-09-01T10:23:45+00:00", "2026-09-02T11:05:10+00:00"]
    assert {r["new_week_end_at"] for r in rows} == {WEEK_END_AT}


def test_a6_the_backfill_is_still_idempotent_over_its_own_output(ns):
    """Re-running the scan must not double the events it already minted. The
    origin partial index carries that now for a row that names an
    observation, and the legacy tuple index for one that does not."""
    conn = ns["open_db"]()
    try:
        for captured, pct in (("2026-09-01T09:00:00Z", 67.0),
                              ("2026-09-01T10:23:45Z", 2.0)):
            _snapshot(conn, captured=captured, percent=pct)
        conn.execute("DELETE FROM week_reset_events")
        conn.commit()
        ns["_backfill_week_reset_events"](conn)
        first = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events").fetchone()[0]
        # The #269 memo would short-circuit a same-process re-scan, so clear it.
        import _cctally_weekrefs as wr
        wr._BACKFILL_RESET_EVENTS_MEMO.clear()
        ns["_backfill_week_reset_events"](conn)
        second = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events").fetchone()[0]
    finally:
        conn.close()
    assert first == 1 and second == 1, (first, second)
