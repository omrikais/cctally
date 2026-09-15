"""#769 S11 (#824) — epoch 1014 -> 1015: the weekly held-provenance column.

The stats migration registry is FROZEN at 13 entries and a stats schema change
is a `STATS_INDEX_EPOCH` bump, never a migration. The reason is mechanical: an
epoch-current `open_db()` returns before the schema helpers run, so an
`add_column_if_missing` dropped into the schema helper would never run on an
upgraded install and the column would simply never appear.

Epoch 1015 adds one column to one existing table:
`weekly_usage_snapshots.weekly_observation_held`, an `INTEGER NOT NULL DEFAULT 0`
constrained to `(0, 1)`. `0` means the row's weekly value, boundary, source and
capture time are all genuine weekly evidence. `1` means the capture time, source
and five-hour fields describe the current tick while the weekly value and
boundary were carried forward from the latest non-held basis, so that a tick
whose weekly percent clamped against the stored high-water mark can still
persist its genuine five-hour reading.

Because the change is a column rather than a table, the table and index
contracts are unchanged and this module asserts that too. What does move is the
`sqlite_schema` definition fingerprint, which is exactly the contract that
exists to catch a silently omitted column.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

FIXED = dt.datetime(2026, 9, 12, 12, 0, 0, tzinfo=dt.timezone.utc)
PREVIOUS_EPOCH = 1014
NEW_EPOCH = 1015
TABLE = "weekly_usage_snapshots"
COLUMN = "weekly_observation_held"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _seed_journal():
    """An epoch mismatch with NO journal is a hard error, never a silent
    rebuild-to-empty, so the fixture has to supply one."""
    import _cctally_journal as jr
    import _lib_journal as J

    jr.append_record(
        J.make_obs(
            at="2026-09-12T09:00:00Z", src="record-usage", provider="claude",
            payload={"weekly_percent": 12.0, "source": "statusline"},
        ),
        now_utc=FIXED,
    )


def _column_row(conn, table, column):
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if str(row[1]) == column:
            return row
    return None


def _resolve_epoch_transition():
    import _cctally_store as store
    return store.resolve_stats_epoch_mismatch()


def test_the_epoch_is_bumped_and_the_legacy_registry_is_untouched():
    assert _cctally_core.STATS_INDEX_EPOCH == NEW_EPOCH
    assert _cctally_core.LEGACY_STATS_HEAD == 13
    import _cctally_db
    assert len(_cctally_db._STATS_MIGRATIONS) == 13, "the registry is FROZEN"


def test_a_fresh_index_carries_the_held_column_with_its_declared_shape(ns):
    conn = ns["open_db"]()
    try:
        row = _column_row(conn, TABLE, COLUMN)
    finally:
        conn.close()
    assert row is not None, f"{TABLE}.{COLUMN} is absent from a fresh index"
    # PRAGMA table_info: 2 is the declared type, 3 the notnull flag, 4 the
    # default expression.
    assert str(row[2]).upper() == "INTEGER"
    assert row[3] == 1
    assert str(row[4]) == "0"


def test_the_flag_domain_is_enforced_by_a_check_constraint(ns):
    """`DEFAULT 0` alone would let a caller store 2 or -1 and every weekly
    reader's `= 0` predicate would then silently exclude that row from both
    axes. The CHECK is what makes the flag a flag."""
    conn = ns["open_db"]()
    insert = (
        f"INSERT INTO {TABLE} (captured_at_utc, week_start_date, "
        " week_end_date, weekly_percent, payload_json, "
        f" {COLUMN}) VALUES (?,?,?,?,?,?)"
    )
    args = ("2026-09-12T09:00:00Z", "2026-09-07", "2026-09-14", 12.0, "{}")
    try:
        conn.execute(insert, args + (0,))
        conn.execute(insert, args + (1,))
        for rejected in (2, -1):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(insert, args + (rejected,))
    finally:
        conn.rollback()
        conn.close()


def test_every_pre_existing_row_defaults_to_not_held(ns):
    """The default is not arbitrary. Every row written before the column
    existed is a genuine weekly observation, so `0` is the truthful value for
    all of them and no backfill is owed."""
    conn = ns["open_db"]()
    try:
        conn.execute(
            f"INSERT INTO {TABLE} (captured_at_utc, week_start_date, "
            " week_end_date, weekly_percent, payload_json) "
            "VALUES (?,?,?,?,?)",
            ("2026-09-12T09:00:00Z", "2026-09-07", "2026-09-14", 12.0, "{}"))
        assert conn.execute(
            f"SELECT {COLUMN} FROM {TABLE}").fetchone()[0] == 0
    finally:
        conn.rollback()
        conn.close()


def test_the_column_change_moves_no_table_or_index_contract():
    """A column addition is invisible to the name-set contracts. Recording
    that explicitly is what keeps a later reader from assuming the omission
    was a miss."""
    import _cctally_journal as jr
    assert TABLE in jr._REBUILD_REQUIRED_TABLES
    assert TABLE in jr._REBUILD_COUNT_TABLES
    assert {"idx_usage_week_time", "idx_usage_week_start_at_time",
            "idx_weekly_usage_snapshots_5h_window_key"} <= (
        jr._REBUILD_REQUIRED_INDEXES)


def test_the_schema_fingerprint_moved_with_the_epoch(ns):
    """The name-set contracts cannot see a column, so the fingerprint is the
    only contract this epoch moves. Asserted in both directions: the shipped
    schema hashes to the shipped constant, and a copy of the table without the
    new column does not."""
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        assert jr._stats_schema_fingerprint(conn) == (
            jr._REBUILD_SCHEMA_FINGERPRINT)
        conn.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
        assert jr._stats_schema_fingerprint(conn) != (
            jr._REBUILD_SCHEMA_FINGERPRINT), (
            "the committed fingerprint does not cover the new column")
    finally:
        conn.rollback()
        conn.close()


def test_the_column_reaches_an_upgraded_install_via_rebuild(ns):
    """The whole reason this is an epoch bump rather than a schema helper
    edit: an epoch-1014 index is rebuilt from the journal and comes back
    carrying the new column."""
    _seed_journal()
    conn = ns["open_db"]()
    try:
        conn.execute(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}")
        conn.execute(f"PRAGMA user_version = {PREVIOUS_EPOCH}")
        conn.commit()
    finally:
        conn.close()

    # Non-vacuity: the downgraded shape must genuinely lack the column.
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == PREVIOUS_EPOCH
        assert _column_row(conn, TABLE, COLUMN) is None
    finally:
        conn.close()

    conn = _resolve_epoch_transition()
    try:
        epoch = conn.execute("PRAGMA user_version").fetchone()[0]
        row = _column_row(conn, TABLE, COLUMN)
    finally:
        conn.close()

    assert epoch == NEW_EPOCH
    assert row is not None, "the held column never reached the upgraded install"


def test_a_rebuild_validates_and_publishes_the_new_shape(ns):
    """Rebuild-and-publication acceptance for the new shape.

    `_validate_rebuilt_stats_index` compares the scratch index's fingerprint
    against the committed constant, so a rebuild that completes and publishes
    is what certifies that the constant was measured from the shape this
    binary actually builds rather than copied from one failure message.
    """
    import _cctally_journal as jr
    _seed_journal()
    ns["open_db"]().close()

    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))
    assert result is not None

    conn = ns["open_db"]()
    try:
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == NEW_EPOCH
        assert _column_row(conn, TABLE, COLUMN) is not None
        assert jr._stats_schema_fingerprint(conn) == (
            jr._REBUILD_SCHEMA_FINGERPRINT)
    finally:
        conn.close()
