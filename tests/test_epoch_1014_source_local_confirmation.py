"""#769 S2 §3 — epoch 1013 -> 1014: source-local five-hour confirmation state.

The stats migration registry is FROZEN at 13 entries and a stats schema change
is a `STATS_INDEX_EPOCH` bump, never a migration. The reason is mechanical: an
epoch-current `open_db()` returns before the schema helpers run, so an
`add_column_if_missing` dropped into the schema helper would never run on an
upgraded install and the object would simply never appear.

Epoch 1014 adds one schema object: `five_hour_credit_confirmation_state`, one
row per `(account_key, five_hour_window_key, source)`, holding that
contributor's own raw pre-drop baseline and its pending descent. It carries no
explicit index — the composite PRIMARY KEY's implicit `sqlite_autoindex_*` is
excluded from both the index contract and the fingerprint by their shared
`name NOT LIKE 'sqlite_%'` filter — so `_REBUILD_REQUIRED_INDEXES` is
unchanged and this module asserts that too.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

FIXED = dt.datetime(2026, 9, 6, 12, 0, 0, tzinfo=dt.timezone.utc)
PREVIOUS_EPOCH = 1013
#: The CURRENT head, not the 1014 this module is named for. The assertions
#: using it mean "the epoch a store ends up at": the head constant itself, and
#: the epoch an upgraded 1013 store reaches, which is always the head rather
#: than the intermediate epoch that first added this table. #769 S11 moved the
#: head to 1015. Everything else here — what epoch 1014 ADDED, and the prose
#: recording it — is historical fact and stays.
NEW_EPOCH = 1015
STATE_TABLE = "five_hour_credit_confirmation_state"
STATE_COLUMNS = {
    "account_key", "five_hour_window_key", "source", "baseline_pct",
    "pending_low_pct", "pending_at_utc", "pending_observation_id",
}


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
            at="2026-09-06T09:00:00Z", src="record-usage", provider="claude",
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


def _resolve_epoch_transition():
    import _cctally_store as store
    return store.resolve_stats_epoch_mismatch()


def test_the_epoch_is_bumped_and_the_legacy_registry_is_untouched():
    assert _cctally_core.STATS_INDEX_EPOCH == NEW_EPOCH
    assert _cctally_core.LEGACY_STATS_HEAD == 13
    import _cctally_db
    assert len(_cctally_db._STATS_MIGRATIONS) == 13, "the registry is FROZEN"


def test_a_fresh_index_carries_the_confirmation_state_table(ns):
    conn = ns["open_db"]()
    try:
        assert _table_exists(conn, STATE_TABLE)
        assert set(_columns(conn, STATE_TABLE)) == STATE_COLUMNS
    finally:
        conn.close()


def test_the_state_is_unique_per_account_window_and_source(ns):
    """The key is what makes confirmation source-local. Two contributors
    inside one window are two independent rows; the same contributor twice is
    one."""
    conn = ns["open_db"]()
    cols = ("account_key, five_hour_window_key, source, baseline_pct")
    try:
        conn.execute(
            f"INSERT INTO {STATE_TABLE} ({cols}) VALUES (?,?,?,?)",
            ("acct-a", 1788506400, "api", 12.0))
        conn.execute(
            f"INSERT INTO {STATE_TABLE} ({cols}) VALUES (?,?,?,?)",
            ("acct-a", 1788506400, "statusline", 7.0))
        conn.execute(
            f"INSERT INTO {STATE_TABLE} ({cols}) VALUES (?,?,?,?)",
            ("acct-b", 1788506400, "api", 3.0))
        assert conn.execute(
            f"SELECT COUNT(*) FROM {STATE_TABLE}").fetchone()[0] == 3
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {STATE_TABLE} ({cols}) VALUES (?,?,?,?)",
                ("acct-a", 1788506400, "api", 9.0))
    finally:
        conn.close()


def test_the_new_table_is_in_the_rebuild_shape_contract():
    """`_validate_rebuilt_stats_index` asserts EXACT table and index sets, so
    an object missing from the declaration fails the rebuild rather than
    passing quietly — and an object present in the schema but absent from the
    declaration fails as `unexpected`."""
    import _cctally_journal as jr
    assert STATE_TABLE in jr._REBUILD_REQUIRED_TABLES


def test_the_new_table_is_deliberately_not_counted_by_the_rebuild():
    """The explicit `_REBUILD_COUNT_TABLES` decision, pinned so an accidental
    later addition has to argue with a test.

    Membership means "this family is journal truth the rebuild re-derives".
    The confirmation state is disposable operational state that the journal
    does not describe, so a rebuild reproduces none of its rows; counting it
    would report zero on every rebuild while implying the rebuild had checked
    something. `weekly_reset_debounce_state` (#750 S3) set the precedent and is
    asserted here beside it, because the two exclusions share one reason.
    """
    import _cctally_journal as jr
    assert STATE_TABLE not in jr._REBUILD_COUNT_TABLES
    assert "weekly_reset_debounce_state" not in jr._REBUILD_COUNT_TABLES


def test_the_new_table_adds_no_explicit_index(ns):
    """The composite PRIMARY KEY's implicit index is `sqlite_`-prefixed, which
    both the index contract and the fingerprint filter out. If a later change
    adds an explicit index it must join `_REBUILD_REQUIRED_INDEXES`, and this
    assertion is what says so."""
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        named = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='index' "
                "AND tbl_name=? AND name NOT LIKE 'sqlite_%'", (STATE_TABLE,))
        }
    finally:
        conn.close()
    assert named == set()
    assert not any(
        name.endswith("credit_confirmation_state")
        for name in jr._REBUILD_REQUIRED_INDEXES
    )


def test_the_schema_fingerprint_moved_with_the_epoch(ns):
    """Asserted behaviourally in both directions: the shipped schema hashes to
    the shipped constant, and removing the new object changes that hash."""
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        assert jr._stats_schema_fingerprint(conn) == (
            jr._REBUILD_SCHEMA_FINGERPRINT)
        conn.execute(f"DROP TABLE {STATE_TABLE}")
        assert jr._stats_schema_fingerprint(conn) != (
            jr._REBUILD_SCHEMA_FINGERPRINT), (
            "the committed fingerprint does not cover the new state table")
    finally:
        conn.rollback()
        conn.close()


def test_the_table_reaches_an_upgraded_install_via_rebuild(ns):
    """The whole reason this is an epoch bump rather than a schema helper
    edit: an epoch-1013 index is rebuilt from the journal and comes back
    carrying the new table."""
    _seed_journal()
    conn = ns["open_db"]()
    try:
        conn.execute(f"DROP TABLE IF EXISTS {STATE_TABLE}")
        conn.execute(f"PRAGMA user_version = {PREVIOUS_EPOCH}")
        conn.commit()
    finally:
        conn.close()

    # Non-vacuity: the downgraded shape must genuinely lack the table.
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == PREVIOUS_EPOCH
        assert not _table_exists(conn, STATE_TABLE)
    finally:
        conn.close()

    conn = _resolve_epoch_transition()
    try:
        epoch = conn.execute("PRAGMA user_version").fetchone()[0]
        has_table = _table_exists(conn, STATE_TABLE)
    finally:
        conn.close()

    assert epoch == NEW_EPOCH
    assert has_table, "the state table never reached the upgraded install"


def test_a_rebuild_validates_and_publishes_the_new_shape(ns, tmp_path):
    """Rebuild-and-publication acceptance for the new shape.

    `_validate_rebuilt_stats_index` refuses an unexpected table as loudly as a
    missing one, so a rebuild that completes and publishes is what certifies
    the declaration, the index contract and the fingerprint together.
    """
    import _cctally_journal as jr
    _seed_journal()
    conn = ns["open_db"]()
    try:
        conn.execute(
            f"INSERT INTO {STATE_TABLE} "
            "(account_key, five_hour_window_key, source, baseline_pct) "
            "VALUES (?,?,?,?)", ("acct-a", 1788506400, "api", 12.0))
        conn.commit()
    finally:
        conn.close()

    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))
    assert result is not None

    conn = ns["open_db"]()
    try:
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == NEW_EPOCH
        assert _table_exists(conn, STATE_TABLE)
        # Disposable, not journal truth: the rebuild does not carry the rows
        # across, which is the same fact `_REBUILD_COUNT_TABLES` records.
        assert conn.execute(
            f"SELECT COUNT(*) FROM {STATE_TABLE}").fetchone()[0] == 0
    finally:
        conn.close()
