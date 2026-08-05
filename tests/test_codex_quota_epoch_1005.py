"""Epochs 1004 -> 1005 -> 1006 -> 1007: Codex quota projection state.

Public issue omrikais/cctally#5, Tasks 6 and 10a. The stats migration registry
is FROZEN, so a stats schema change is an ``STATS_INDEX_EPOCH`` bump and never a
new migration. The second, independent reason is mechanical: an epoch-current
open returns BEFORE any schema work, so an ``add_column_if_missing`` dropped into
the schema helper would never run on an upgraded install and the column would
simply never appear. That is what this module pins — not the constant's value
(other modules assert that), but the behaviour the constant buys: an index
stamped at a previous epoch is rebuilt from the journal and comes back carrying
the new state. Ordinary opens defer that work as of #453, so the two transition
assertions below invoke the explicit synchronous resolver used by the detached
worker.

1005 added the reverse map, the per-group digest and the ledger-state row. 1006
added the periodic verification deadline; 1007 adds scheduled ownership. Each
gets its own test because the 1004 case drops the whole table and cannot observe
later column-only deltas.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import _cctally_core  # preserved across load_script()
from conftest import load_script, redirect_paths


FIXED = dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=dt.timezone.utc)
PREVIOUS_EPOCH = 1004
EPOCH_1005 = 1005
EPOCH_1006 = 1006

#: The columns and table epoch 1005 adds, and the reason each exists.
NEW_BLOCK_COLUMNS = ("physical_group_key", "physical_group_digest")
NEW_TABLE = "quota_projection_ledger_state"
#: The periodic verification's deadline — epoch 1006's whole delta.
VERIFICATION_COLUMN = "last_full_pass_at"
SCHEDULE_COLUMN = "next_evaluation_by_root_json"
NEW_LEDGER_STATE_COLUMNS = (
    "source", "watermark_seq", "interpretation_version", "alerts_enabled",
    "next_evaluation_at_utc", VERIFICATION_COLUMN, SCHEDULE_COLUMN,
)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _seed_journal():
    """A non-empty journal. An epoch mismatch with NO journal is a hard error,
    never a silent rebuild-to-empty, so the fixture has to supply one."""
    import _cctally_journal as jr
    import _lib_journal as J

    jr.append_record(
        J.make_obs(
            at="2026-07-31T09:00:00Z", src="record-usage", provider="claude",
            payload={"weekly_percent": 12.0, "source": "statusline"},
        ),
        now_utc=FIXED,
    )


def _columns(conn, table):
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _resolve_epoch_transition():
    import _cctally_store as store
    return store.resolve_stats_epoch_mismatch()


def _downgrade_to_previous_epoch(ns):
    """Turn the live index into the shape a pre-public-#5 binary left behind."""
    conn = ns["open_db"]()
    try:
        for column in NEW_BLOCK_COLUMNS:
            conn.execute(
                f"ALTER TABLE quota_window_blocks DROP COLUMN {column}")
        conn.execute(f"DROP TABLE {NEW_TABLE}")
        conn.execute(f"PRAGMA user_version = {PREVIOUS_EPOCH}")
        conn.commit()
    finally:
        conn.close()


def test_a_previous_epoch_index_rebuilds_carrying_the_projection_state(ns):
    _seed_journal()
    _downgrade_to_previous_epoch(ns)

    # Non-vacuity: the downgraded shape must genuinely lack the state, or the
    # assertions below would hold before the rebuild ran.
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == PREVIOUS_EPOCH
        assert not set(NEW_BLOCK_COLUMNS) & set(
            _columns(conn, "quota_window_blocks"))
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (NEW_TABLE,),
        ).fetchone()[0] == 0
    finally:
        conn.close()

    conn = _resolve_epoch_transition()
    try:
        epoch = conn.execute("PRAGMA user_version").fetchone()[0]
        block_columns = _columns(conn, "quota_window_blocks")
        ledger_columns = _columns(conn, NEW_TABLE)
    finally:
        conn.close()

    assert epoch == _cctally_core.STATS_INDEX_EPOCH
    assert epoch != PREVIOUS_EPOCH, (
        "the epoch was not bumped, so nothing forced the rebuild")
    for column in NEW_BLOCK_COLUMNS:
        assert column in block_columns, (
            f"{column} is missing after the rebuild — an epoch-current open "
            f"returns before any schema work, so it would never appear")
    assert list(ledger_columns) == list(NEW_LEDGER_STATE_COLUMNS)


def test_an_epoch_1005_index_rebuilds_carrying_the_verification_deadline(ns):
    """Epoch 1006's own delta, which the 1004 arm above cannot observe.

    That arm drops ``quota_projection_ledger_state`` outright, so it would stay
    green for a binary whose ``CREATE TABLE`` never gained the column. This one
    reproduces the exact shape a 1005 binary left behind: the table present, the
    deadline absent.
    """
    _seed_journal()
    conn = ns["open_db"]()
    try:
        conn.execute(
            f"ALTER TABLE {NEW_TABLE} DROP COLUMN {VERIFICATION_COLUMN}")
        conn.execute(f"PRAGMA user_version = {EPOCH_1005}")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == EPOCH_1005
        assert VERIFICATION_COLUMN not in _columns(conn, NEW_TABLE)
    finally:
        conn.close()

    conn = _resolve_epoch_transition()
    try:
        epoch = conn.execute("PRAGMA user_version").fetchone()[0]
        ledger_columns = _columns(conn, NEW_TABLE)
    finally:
        conn.close()

    assert epoch == _cctally_core.STATS_INDEX_EPOCH
    assert epoch != EPOCH_1005, (
        "the epoch was not bumped, so nothing forced the rebuild and the "
        "periodic verification would read as due on every tick forever")
    assert VERIFICATION_COLUMN in ledger_columns


def test_an_epoch_1006_index_rebuilds_carrying_boundary_ownership(ns):
    _seed_journal()
    conn = ns["open_db"]()
    try:
        conn.execute(f"ALTER TABLE {NEW_TABLE} DROP COLUMN {SCHEDULE_COLUMN}")
        conn.execute(f"PRAGMA user_version = {EPOCH_1006}")
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == EPOCH_1006
        assert SCHEDULE_COLUMN not in _columns(conn, NEW_TABLE)
    finally:
        conn.close()

    conn = _resolve_epoch_transition()
    try:
        epoch = conn.execute("PRAGMA user_version").fetchone()[0]
        ledger_columns = _columns(conn, NEW_TABLE)
    finally:
        conn.close()

    assert epoch == _cctally_core.STATS_INDEX_EPOCH
    assert epoch != EPOCH_1006
    assert SCHEDULE_COLUMN in ledger_columns


def test_the_ledger_state_is_keyed_by_source_and_starts_empty(ns):
    """It is a change-log cursor, not a snapshot of existing state.

    A rebuilt index has consumed no ledger range, and starting the watermark at
    an invented value would skip every entry at or below it — forever, because
    the ledger's ``seq`` is AUTOINCREMENT and never reissued.
    """
    conn = ns["open_db"]()
    try:
        assert conn.execute(f"SELECT COUNT(*) FROM {NEW_TABLE}").fetchone()[0] == 0
        conn.execute(
            f"INSERT INTO {NEW_TABLE}(source, watermark_seq) VALUES ('codex', 7)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {NEW_TABLE}(source, watermark_seq) "
                "VALUES ('codex', 9)")
    finally:
        conn.close()


@pytest.mark.parametrize("stamped_fixups_version", [1, 2, 3])
def test_a_legacy_index_cuts_over_carrying_the_projection_schema(
    ns, stamped_fixups_version,
):
    """The legacy-cutover seam, which the epoch bump alone does NOT cover.

    An epoch-MISMATCHED index (``user_version > LEGACY_STATS_HEAD``) resolves by
    rebuild and gets the fresh ``CREATE TABLE``. A LEGACY index
    (``user_version <= LEGACY_STATS_HEAD``) takes the IN-PLACE cutover instead,
    which relies on the open-time ``_apply_quota_projection_schema`` — and a
    stamped ``stats_open_fixups`` marker skips that apply outright. Without a
    ``_STATS_OPEN_FIXUPS_VERSION`` bump such a DB is stamped at the new epoch
    while still missing the schema, and every subsequent open returns at the
    steady-state gate before anything could add it: a permanent ``no such
    column``, unrecoverable short of a manual rebuild.

    Parametrized over BOTH stamped versions on purpose. Version 1 guards the
    1 -> 2 bump (the reverse map); version 2 guards the 2 -> 3 bump (the
    periodic verification's deadline). A single case cannot: a marker at 1 is
    below either expectation, so it would stay green through a revert of the
    second bump.
    """
    conn = ns["open_db"]()
    try:
        for column in NEW_BLOCK_COLUMNS:
            conn.execute(
                f"ALTER TABLE quota_window_blocks DROP COLUMN {column}")
        conn.execute(f"DROP TABLE {NEW_TABLE}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stats_open_fixups ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)")
        conn.execute(
            "INSERT INTO stats_open_fixups (id, version) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
            (stamped_fixups_version,))
        conn.execute(f"PRAGMA user_version = {_cctally_core.LEGACY_STATS_HEAD}")
        conn.commit()
    finally:
        conn.close()

    # Non-vacuity: the legacy shape must genuinely lack all three, or a binary
    # that skipped the apply would still satisfy the assertions below.
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            _cctally_core.LEGACY_STATS_HEAD)
        assert not set(NEW_BLOCK_COLUMNS) & set(
            _columns(conn, "quota_window_blocks"))
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (NEW_TABLE,),
        ).fetchone()[0] == 0
    finally:
        conn.close()

    conn = ns["open_db"]()
    try:
        epoch = conn.execute("PRAGMA user_version").fetchone()[0]
        block_columns = _columns(conn, "quota_window_blocks")
        ledger_columns = _columns(conn, NEW_TABLE)
    finally:
        conn.close()

    assert epoch == _cctally_core.STATS_INDEX_EPOCH
    for column in NEW_BLOCK_COLUMNS:
        assert column in block_columns
    assert VERIFICATION_COLUMN in ledger_columns
    assert SCHEDULE_COLUMN in ledger_columns


@pytest.mark.parametrize("stamped_fixups_version", [1, 2, 3])
def test_a_legacy_cutover_adds_the_deadline_to_an_existing_state_table(
    ns, stamped_fixups_version,
):
    """The `add_column_if_missing` backstop, which the case above cannot reach.

    That one DROPs `quota_projection_ledger_state`, so the `CREATE TABLE IF NOT
    EXISTS` in the schema apply supplies every column and the backstop line
    never executes — it would stay green with the backstop deleted. The shape
    the backstop actually exists for is a LEGACY index that already took the
    epoch-1005 cutover: the table is present, the deadline column is not, and
    `CREATE TABLE IF NOT EXISTS` is a no-op over the table it already has. Every
    reconcile after such a cutover would fail on `no such column`.
    """
    conn = ns["open_db"]()
    try:
        conn.execute(
            f"ALTER TABLE {NEW_TABLE} DROP COLUMN {VERIFICATION_COLUMN}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stats_open_fixups ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)")
        conn.execute(
            "INSERT INTO stats_open_fixups (id, version) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
            (stamped_fixups_version,))
        conn.execute(f"PRAGMA user_version = {_cctally_core.LEGACY_STATS_HEAD}")
        conn.commit()
    finally:
        conn.close()

    # Non-vacuity: the table must be PRESENT and the column ABSENT, which is
    # exactly what makes the fresh CREATE unable to supply it.
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (NEW_TABLE,),
        ).fetchone()[0] == 1
        assert VERIFICATION_COLUMN not in _columns(conn, NEW_TABLE)
    finally:
        conn.close()

    conn = ns["open_db"]()
    try:
        ledger_columns = _columns(conn, NEW_TABLE)
    finally:
        conn.close()

    assert VERIFICATION_COLUMN in ledger_columns, (
        "the legacy cutover left the state table without the deadline column, "
        "so every reconcile after it fails on `no such column`")


@pytest.mark.parametrize("stamped_fixups_version", [1, 2, 3])
def test_a_legacy_cutover_adds_boundary_ownership_to_existing_state_table(
    ns, stamped_fixups_version,
):
    conn = ns["open_db"]()
    try:
        conn.execute(f"ALTER TABLE {NEW_TABLE} DROP COLUMN {SCHEDULE_COLUMN}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stats_open_fixups ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)")
        conn.execute(
            "INSERT INTO stats_open_fixups (id, version) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
            (stamped_fixups_version,))
        conn.execute(f"PRAGMA user_version = {_cctally_core.LEGACY_STATS_HEAD}")
        conn.commit()
    finally:
        conn.close()

    conn = ns["open_db"]()
    try:
        ledger_columns = _columns(conn, NEW_TABLE)
    finally:
        conn.close()

    assert SCHEDULE_COLUMN in ledger_columns


def test_the_block_reverse_map_is_nullable_for_a_journal_replayed_row(ns):
    """The journal fold does not know a block's physical group.

    ``quota_window_blocks`` is re-materialized by the projector, not by the
    fold, so a rebuilt row can legitimately arrive without the reverse map. A
    NOT NULL column here would fail the rebuild outright; the projector's own
    guard is what turns a NULL into a full sweep rather than a missed one.
    """
    conn = ns["open_db"]()
    try:
        info = {
            str(row[1]): int(row[3])
            for row in conn.execute("PRAGMA table_info(quota_window_blocks)")
        }
    finally:
        conn.close()

    for column in NEW_BLOCK_COLUMNS:
        assert info[column] == 0, f"{column} must be nullable"


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
