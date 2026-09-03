"""A pre-cutover stats.db must reach epoch 1012 with the CURRENT credit shape.

The unified credit record (#703 + #707) changed ``week_reset_events`` in three
ways a bare ``CREATE TABLE IF NOT EXISTS`` cannot deliver to a store whose table
already exists: six columns were added, both boundary columns became nullable,
and the row constraint moved from the boundary pair to ``(account_key,
credit_key)``.

Two upgrade paths reach the new epoch and they are not alike.

A store at ``user_version`` 1011 is ABOVE ``LEGACY_STATS_HEAD``, so the open
defers to a rebuild that materializes a fresh scratch index through the current
DDL. That path was already correct.

A store at ``user_version <= 13`` is a pre-journal install. Its open runs the
in-place cutover instead: the schema apply is a no-op against the table that is
already there, the cutover exports history and stamps ``PRAGMA user_version =
1012``, and every later open fast-returns at the epoch gate before any schema
work. Without a reshape the store then REPORTS the current epoch while missing
all six fact columns, keeping ``old_week_end_at NOT NULL`` and keeping the old
UNIQUE — so the manual credit fold, the automatic fold and every fact read fail
against it, permanently and with no path back.

These tests seed exactly that store and assert both that the shape converges
and that a credit write actually succeeds on it.
"""
from __future__ import annotations

import sqlite3

import pytest

from conftest import load_script, redirect_paths


# The `week_reset_events` DDL exactly as it stood before the unified credit
# record: both boundary columns NOT NULL, no fact columns, and identity on the
# boundary pair. Copied verbatim from `bin/_cctally_core.py` at cce16e4ee^ so
# the seeded store is a real pre-change store rather than an approximation.
_LEGACY_WEEK_RESET_EVENTS_DDL = """
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

_FACT_COLUMNS = (
    "week_start_date",
    "observed_at_utc",
    "confirming_capture_at_utc",
    "observed_post_credit_pct",
    "credit_key",
    "credit_order",
)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _core():
    import _cctally_core
    return _cctally_core


def _seed_legacy_store(ns):
    """Build a pre-cutover stats.db whose `week_reset_events` is pre-change.

    `open_db()` on a fresh path creates the CURRENT table, so the table is
    dropped and rebuilt from the pre-change DDL. `journal_id` and its two
    indices are added because they predate this change and a real legacy store
    at head 13 carries them.
    """
    core = _core()
    conn = core.open_db()
    try:
        conn.execute("DROP TABLE week_reset_events")
        conn.execute(_LEGACY_WEEK_RESET_EVENTS_DDL)
        conn.execute("ALTER TABLE week_reset_events ADD COLUMN journal_id TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX idx_week_reset_events_journal_id "
            "ON week_reset_events(journal_id) WHERE journal_id IS NOT NULL")
        conn.execute(
            "CREATE INDEX idx_week_reset_events_journal_id_null "
            "ON week_reset_events(id) WHERE journal_id IS NULL")
        conn.execute(
            "INSERT INTO week_reset_events (detected_at_utc, old_week_end_at, "
            "new_week_end_at, effective_reset_at_utc, observed_pre_credit_pct, "
            "account_key) VALUES (?,?,?,?,?,?)",
            ("2026-01-04T11:00:00Z", "2026-01-07T23:59:59+00:00",
             "2026-01-14T23:59:59+00:00", "2026-01-04T11:00:00+00:00", 40.0,
             "unattributed"))
        conn.execute("DROP TABLE IF EXISTS stats_open_fixups")
        conn.execute("PRAGMA user_version = 13")
        conn.commit()
    finally:
        conn.close()


def _open_twice(ns):
    """Open the legacy store, then open it again, and return the second conn.

    The second open is the one that matters: the first cuts over and stamps the
    epoch, and every open after that fast-returns at the epoch gate before any
    schema work runs.
    """
    core = _core()
    first = core.open_db()
    first.close()
    return core.open_db()


def test_a_legacy_store_reaches_the_epoch_with_the_credit_fact_columns(ns):
    _seed_legacy_store(ns)
    conn = _open_twice(ns)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == \
            _core().STATS_INDEX_EPOCH
        cols = {r["name"]: r for r in conn.execute(
            "PRAGMA table_info(week_reset_events)").fetchall()}
        missing = [name for name in _FACT_COLUMNS if name not in cols]
        assert not missing, (
            f"a legacy store reached epoch {_core().STATS_INDEX_EPOCH} still "
            f"missing {missing} from week_reset_events")
        assert cols["old_week_end_at"]["notnull"] == 0
        assert cols["new_week_end_at"]["notnull"] == 0
    finally:
        conn.close()


def test_a_manual_credit_write_succeeds_on_an_upgraded_legacy_store(ns):
    _seed_legacy_store(ns)
    conn = _open_twice(ns)
    try:
        conn.execute(
            "INSERT INTO week_reset_events (detected_at_utc, old_week_end_at, "
            "new_week_end_at, effective_reset_at_utc, observed_pre_credit_pct, "
            "account_key, week_start_date, observed_at_utc, "
            "confirming_capture_at_utc, observed_post_credit_pct, credit_key, "
            "credit_order, journal_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-01-05T09:00:00Z", None, None, "2026-01-05T09:00:00+00:00",
             46.0, "unattributed", "2026-01-01", "2026-01-05T09:12:31Z", None,
             31.0, "o:deadbeef", 1767603151, "o:deadbeef"))
        conn.commit()
        row = conn.execute(
            "SELECT observed_at_utc, observed_post_credit_pct, credit_key "
            "FROM week_reset_events WHERE credit_key = 'o:deadbeef'"
        ).fetchone()
        assert row["observed_at_utc"] == "2026-01-05T09:12:31Z"
        assert row["observed_post_credit_pct"] == 31.0
    finally:
        conn.close()


def test_the_identity_constraint_moves_to_account_and_credit_key(ns):
    """Two credits in one week, distinguished only by `credit_key`.

    Under the legacy `UNIQUE(account_key, old_week_end_at, new_week_end_at)`
    both rows carry the same boundary pair — NULL and NULL — and SQLite treats
    NULLs as distinct, so the old constraint would admit them for the wrong
    reason. The assertion that discriminates is the REVERSE one below: an
    identical `credit_key` must be rejected.
    """
    _seed_legacy_store(ns)
    conn = _open_twice(ns)
    try:
        for key in ("o:aaaa", "o:bbbb"):
            conn.execute(
                "INSERT INTO week_reset_events (detected_at_utc, "
                "effective_reset_at_utc, observed_pre_credit_pct, account_key, "
                "week_start_date, credit_key) VALUES (?,?,?,?,?,?)",
                ("2026-01-05T09:00:00Z", "2026-01-05T09:00:00+00:00", 46.0,
                 "unattributed", "2026-01-01", key))
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM week_reset_events WHERE week_start_date = ?",
            ("2026-01-01",)).fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO week_reset_events (detected_at_utc, "
                "effective_reset_at_utc, observed_pre_credit_pct, account_key, "
                "week_start_date, credit_key) VALUES (?,?,?,?,?,?)",
                ("2026-01-05T10:00:00Z", "2026-01-05T10:00:00+00:00", 46.0,
                 "unattributed", "2026-01-01", "o:aaaa"))
    finally:
        conn.close()


def test_the_legacy_rows_survive_the_reshape_with_their_identifiers(ns):
    """`percent_milestones.reset_event_id` is a documentation-only foreign key.

    A reshape that renumbered the rows would silently re-point every milestone
    at a different credit, so the copy must preserve `id`.
    """
    _seed_legacy_store(ns)
    conn = _open_twice(ns)
    try:
        row = conn.execute(
            "SELECT id, detected_at_utc, old_week_end_at, new_week_end_at, "
            "       observed_pre_credit_pct, account_key "
            "FROM week_reset_events ORDER BY id").fetchall()
        assert len(row) == 1
        assert row[0]["id"] == 1
        assert row[0]["detected_at_utc"] == "2026-01-04T11:00:00Z"
        assert row[0]["old_week_end_at"] == "2026-01-07T23:59:59+00:00"
        assert row[0]["observed_pre_credit_pct"] == 40.0
    finally:
        conn.close()


def test_the_copy_preserves_the_journal_identifier(ns):
    """A legacy row's `journal_id` must cross the reshape.

    Losing it makes the row look un-journaled, and harvest scans exactly the
    rows whose `journal_id IS NULL` — so every already-journaled credit would be
    re-emitted as a fresh `wr:` event on the next cycle.

    Driven at the two halves directly rather than through `open_db`, because the
    end-to-end pre-cutover path cannot see this property: the cutover stamps
    every unstamped row with its own `b:week_reset_events:<rowid>` bootstrap
    identity, so a reshape that dropped the column entirely would still leave a
    stamped row behind and the assertion would hold for the wrong reason.
    """
    import _cctally_db as db

    core = _core()
    conn = core.open_db()
    try:
        fresh_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            ("week_reset_events",)).fetchone()[0]
        conn.execute("DROP TABLE week_reset_events")
        conn.execute(_LEGACY_WEEK_RESET_EVENTS_DDL)
        conn.execute("ALTER TABLE week_reset_events ADD COLUMN journal_id TEXT")
        conn.execute(
            "INSERT INTO week_reset_events (detected_at_utc, old_week_end_at, "
            "new_week_end_at, effective_reset_at_utc, observed_pre_credit_pct, "
            "account_key, journal_id) VALUES (?,?,?,?,?,?,?)",
            ("2026-01-04T11:00:00Z", "2026-01-07T23:59:59+00:00",
             "2026-01-14T23:59:59+00:00", "2026-01-04T11:00:00+00:00", 40.0,
             "unattributed", "wr:unattributed:legacy"))
        conn.commit()

        assert db.begin_week_reset_events_reshape(conn) is True
        conn.execute(fresh_sql)
        db.finish_week_reset_events_reshape(conn)

        row = conn.execute(
            "SELECT id, journal_id, observed_pre_credit_pct "
            "FROM week_reset_events").fetchone()
        assert row["journal_id"] == "wr:unattributed:legacy", dict(row)
        assert row["id"] == 1
        assert row["observed_pre_credit_pct"] == 40.0
    finally:
        conn.close()


def test_a_crash_between_the_two_halves_resumes_on_the_next_open(ns):
    """The parked table is the resume point, and it has to be used as one.

    `begin_week_reset_events_reshape` renames the legacy table and the caller's
    own `CREATE TABLE IF NOT EXISTS` builds the new one, so a process that dies
    between those two steps leaves a store with NO `week_reset_events` at all
    and one `week_reset_events_pre_1012` holding every credit. A `begin` that
    only looked for a legacy-shaped `week_reset_events` would see none, report
    nothing to do, and the caller's CREATE would then produce an EMPTY table
    while the rows sat parked forever.
    """
    import _cctally_db as db

    _seed_legacy_store(ns)
    core = _core()
    conn = core._open_stats_db_raw() if hasattr(core, "_open_stats_db_raw") \
        else sqlite3.connect(str(core.DB_PATH))
    try:
        assert db.begin_week_reset_events_reshape(conn) is True
        conn.commit()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("week_reset_events",)).fetchone() is None
        assert conn.execute(
            f"SELECT COUNT(*) FROM {db.WEEK_RESET_EVENTS_RESHAPE_OLD}"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    conn = _open_twice(ns)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (db.WEEK_RESET_EVENTS_RESHAPE_OLD,)).fetchone() is None, (
            "the parked table outlived the resume")
        rows = conn.execute(
            "SELECT id, detected_at_utc, observed_pre_credit_pct "
            "FROM week_reset_events").fetchall()
        assert len(rows) == 1, [dict(r) for r in rows]
        assert rows[0]["id"] == 1
        assert rows[0]["observed_pre_credit_pct"] == 40.0
    finally:
        conn.close()


def test_the_reshaped_table_matches_a_freshly_created_one_byte_for_byte(ns):
    """`sqlite_master.sql` is what the rebuild validator fingerprints.

    The reshape deliberately does NOT write its own DDL: it renames the legacy
    table aside and lets `open_db`'s own `CREATE TABLE IF NOT EXISTS` build the
    replacement, so there is exactly one copy of the statement. A second copy
    would drift, and the divergence would surface as a schema-fingerprint
    mismatch on a store nobody could reproduce.
    """
    core = _core()
    conn = core.open_db()
    try:
        fresh_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            ("week_reset_events",)).fetchone()[0]
        fresh_indexes = sorted(
            r[0] for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name=? AND sql IS NOT NULL",
                ("week_reset_events",)).fetchall())
    finally:
        conn.close()

    _seed_legacy_store(ns)
    conn = _open_twice(ns)
    try:
        reshaped_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            ("week_reset_events",)).fetchone()[0]
        reshaped_indexes = sorted(
            r[0] for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name=? AND sql IS NOT NULL",
                ("week_reset_events",)).fetchall())
    finally:
        conn.close()

    assert reshaped_sql == fresh_sql
    assert reshaped_indexes == fresh_indexes, (
        "the reshape freed the index names but did not rebuild them "
        "identically")
