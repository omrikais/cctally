"""#750 S3 Task A1 — epoch 1012 -> 1013: reset-event origin identity.

Spec §1.6. The stats migration registry is FROZEN at 13 entries and a stats
schema change is a `STATS_INDEX_EPOCH` bump, never a migration. The reason is
mechanical: an epoch-current `open_db()` returns at
`bin/_cctally_core.py:1971`, BEFORE the schema helpers at `:2237`, so an
`add_column_if_missing` dropped into the schema helper would never run on an
upgraded install and the column would simply never appear.

Epoch 1013 adds three schema objects:

* `week_reset_events.origin_observation_id`, the originating journal
  observation's raw id, nullable because every legacy row has none.
* two PARTIAL unique indexes discriminated on `origin_observation_id IS NULL`,
  because identity is dual-shaped: a row with an origin is unique on
  `(account_key, origin_observation_id)` while a legacy row keeps the old
  `(account_key, old_week_end_at, new_week_end_at)` tuple. Collapsing every
  legacy row onto one key is what a single index would do.
* `weekly_reset_debounce_state`, the transactional replacement for the
  filesystem reset-to-zero marker.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

FIXED = dt.datetime(2026, 9, 5, 12, 0, 0, tzinfo=dt.timezone.utc)
PREVIOUS_EPOCH = 1012
#: The CURRENT head, not the 1013 this module is named for. Two assertions
#: below mean "the epoch a store ends up at": the head constant itself, and the
#: epoch an upgraded 1012 store reaches, which is always the head rather than
#: the intermediate epoch that first added these objects. #769 S11 moved the
#: head to 1015. Everything else here — what epoch 1013 ADDED, and the prose
#: recording it — is historical fact and stays.
NEW_EPOCH = 1015
DEBOUNCE_TABLE = "weekly_reset_debounce_state"
ORIGIN_INDEX = "idx_week_reset_events_origin"
LEGACY_TUPLE_INDEX = "idx_week_reset_events_legacy_tuple"


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
            at="2026-09-05T09:00:00Z", src="record-usage", provider="claude",
            payload={"weekly_percent": 12.0, "source": "statusline"},
        ),
        now_utc=FIXED,
    )


def _columns(conn, table):
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _table_exists(conn, table):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


def _index_sql(conn, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (name,)).fetchone()
    return None if row is None else str(row[0])


def _resolve_epoch_transition():
    import _cctally_store as store
    return store.resolve_stats_epoch_mismatch()


def test_a1_the_epoch_is_bumped_and_the_legacy_registry_is_untouched():
    assert _cctally_core.STATS_INDEX_EPOCH == NEW_EPOCH
    assert _cctally_core.LEGACY_STATS_HEAD == 13
    import _cctally_db
    assert len(_cctally_db._STATS_MIGRATIONS) == 13, "the registry is FROZEN"


def test_a1_a_fresh_index_carries_the_origin_column(ns):
    conn = ns["open_db"]()
    try:
        assert "origin_observation_id" in _columns(conn, "week_reset_events")
    finally:
        conn.close()


def test_a1_a_fresh_index_carries_both_partial_unique_indexes(ns):
    conn = ns["open_db"]()
    try:
        origin = _index_sql(conn, ORIGIN_INDEX)
        legacy = _index_sql(conn, LEGACY_TUPLE_INDEX)
    finally:
        conn.close()
    assert origin is not None, f"{ORIGIN_INDEX} is missing"
    assert legacy is not None, f"{LEGACY_TUPLE_INDEX} is missing"
    assert "UNIQUE" in origin.upper()
    assert "UNIQUE" in legacy.upper()
    # The discriminator is what keeps the two shapes from colliding.
    assert "origin_observation_id IS NOT NULL" in origin
    assert "origin_observation_id IS NULL" in legacy


def test_a1_a_fresh_index_carries_the_debounce_state_table(ns):
    conn = ns["open_db"]()
    try:
        assert _table_exists(conn, DEBOUNCE_TABLE)
        assert set(_columns(conn, DEBOUNCE_TABLE)) == {
            "account_key", "week_start_date", "week_end_at", "baseline_pct",
            "first_zero_at_utc", "first_zero_observation_id"}
    finally:
        conn.close()


def test_a1_two_distinct_origins_may_share_one_legacy_tuple(ns):
    """The whole point of the dual shape. Two genuine resets detected from
    distinct observations can carry the same `(old, new)` tuple, and the
    pre-1013 table-level UNIQUE refused the second one."""
    conn = ns["open_db"]()
    cols = ("detected_at_utc, old_week_end_at, new_week_end_at, "
            "effective_reset_at_utc, account_key, origin_observation_id")
    base = ("2026-09-05T10:00:00+00:00", "2026-09-05T10:00:00+00:00",
            "2026-09-07T15:00:00+00:00", "2026-09-05T10:00:00+00:00",
            "unattributed")
    try:
        conn.execute(
            f"INSERT INTO week_reset_events ({cols}) VALUES (?,?,?,?,?,?)",
            base + ("o:" + "a" * 16,))
        conn.execute(
            f"INSERT INTO week_reset_events ({cols}) VALUES (?,?,?,?,?,?)",
            base + ("o:" + "b" * 16,))
        assert conn.execute(
            "SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 2
        # The same origin twice is still one event.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO week_reset_events ({cols}) VALUES (?,?,?,?,?,?)",
                base + ("o:" + "a" * 16,))
    finally:
        conn.close()


def test_a1_two_null_origin_rows_still_collide_on_the_legacy_tuple(ns):
    """The legacy half of the dual shape. Without the partial tuple index a
    NULL origin would make every legacy row unique on a NULL key, so the
    duplicate protection that existed before 1013 would be gone."""
    conn = ns["open_db"]()
    cols = ("detected_at_utc, old_week_end_at, new_week_end_at, "
            "effective_reset_at_utc, account_key")
    base = ("2026-09-05T10:00:00+00:00", "2026-09-05T10:00:00+00:00",
            "2026-09-07T15:00:00+00:00", "2026-09-05T10:00:00+00:00",
            "unattributed")
    try:
        conn.execute(
            f"INSERT INTO week_reset_events ({cols}) VALUES (?,?,?,?,?)", base)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO week_reset_events ({cols}) VALUES (?,?,?,?,?)",
                base)
        # A different account is a different event.
        conn.execute(
            f"INSERT INTO week_reset_events ({cols}) VALUES (?,?,?,?,?)",
            base[:4] + ("acct-b",))
        assert conn.execute(
            "SELECT COUNT(*) FROM week_reset_events").fetchone()[0] == 2
    finally:
        conn.close()


def test_a1_the_new_objects_appear_on_an_upgraded_install_via_rebuild(ns):
    """The whole reason this is an epoch bump rather than a schema helper
    edit: an epoch-1012 index is rebuilt from the journal and comes back
    carrying every new object."""
    _seed_journal()
    conn = ns["open_db"]()
    try:
        conn.execute(f"DROP INDEX IF EXISTS {ORIGIN_INDEX}")
        conn.execute(f"DROP INDEX IF EXISTS {LEGACY_TUPLE_INDEX}")
        conn.execute(f"DROP TABLE IF EXISTS {DEBOUNCE_TABLE}")
        conn.execute(
            "ALTER TABLE week_reset_events DROP COLUMN origin_observation_id")
        conn.execute(f"PRAGMA user_version = {PREVIOUS_EPOCH}")
        conn.commit()
    finally:
        conn.close()

    # Non-vacuity: the downgraded shape must genuinely lack the objects.
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == PREVIOUS_EPOCH
        assert not _table_exists(conn, DEBOUNCE_TABLE)
        assert "origin_observation_id" not in _columns(
            conn, "week_reset_events")
    finally:
        conn.close()

    conn = _resolve_epoch_transition()
    try:
        epoch = conn.execute("PRAGMA user_version").fetchone()[0]
        has_table = _table_exists(conn, DEBOUNCE_TABLE)
        has_column = "origin_observation_id" in _columns(
            conn, "week_reset_events")
        has_origin_index = _index_sql(conn, ORIGIN_INDEX) is not None
        has_legacy_index = _index_sql(conn, LEGACY_TUPLE_INDEX) is not None
    finally:
        conn.close()

    assert epoch == NEW_EPOCH
    assert has_column, "the origin column never reached the upgraded install"
    assert has_table, "the debounce state table never reached the upgrade"
    assert has_origin_index and has_legacy_index


def test_a1_the_new_objects_are_in_the_rebuild_shape_contract():
    """`_validate_rebuilt_stats_index` asserts EXACT table and index sets, so
    an object missing from the declaration fails the rebuild rather than
    passing quietly — and an object present in the schema but absent from the
    declaration fails as `unexpected`."""
    import _cctally_journal as jr
    assert DEBOUNCE_TABLE in jr._REBUILD_REQUIRED_TABLES
    assert ORIGIN_INDEX in jr._REBUILD_REQUIRED_INDEXES
    assert LEGACY_TUPLE_INDEX in jr._REBUILD_REQUIRED_INDEXES
    # Disposable operational state is not journal truth, so the rebuild does
    # not reproduce its rows and must not count them.
    assert DEBOUNCE_TABLE not in jr._REBUILD_COUNT_TABLES


def test_a1_the_schema_fingerprint_moved_with_the_epoch(ns):
    """Asserted behaviourally in both directions: the shipped schema hashes to
    the shipped constant, and removing one of the new objects changes that
    hash."""
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        assert jr._stats_schema_fingerprint(conn) == (
            jr._REBUILD_SCHEMA_FINGERPRINT)
        conn.execute(f"DROP TABLE {DEBOUNCE_TABLE}")
        assert jr._stats_schema_fingerprint(conn) != (
            jr._REBUILD_SCHEMA_FINGERPRINT)
        conn.rollback()
        conn.execute(f"DROP INDEX {ORIGIN_INDEX}")
        assert jr._stats_schema_fingerprint(conn) != (
            jr._REBUILD_SCHEMA_FINGERPRINT), (
            "the committed fingerprint does not cover the origin index")
    finally:
        conn.rollback()
        conn.close()
