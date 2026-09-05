"""#661 S2 Task C1 — epoch 1010 -> 1011: the meter-rate-change event table.

Spec §6.4. The stats migration registry is FROZEN at 13 entries and a stats
schema change is an `STATS_INDEX_EPOCH` bump, never a migration. The reason is
mechanical rather than stylistic: an epoch-current open returns BEFORE any
schema work, so a `@stats_migration` handler — or an `add_column_if_missing`
dropped into the schema helper — would simply never run on an upgraded
install, and the table would never appear. That is what this module pins: not
the constant's value alone, but the behaviour the bump buys.

`quota_alert_arming` is the precedent §6.4 names, and this table follows it:
a durable, forward-only alert boundary that survives a rebuild, so history
cannot re-fire.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import _cctally_core
from conftest import load_script, redirect_paths

FIXED = dt.datetime(2026, 8, 29, 12, 0, 0, tzinfo=dt.timezone.utc)
PREVIOUS_EPOCH = 1010
NEW_TABLE = "meter_rate_change_events"


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
            at="2026-08-29T09:00:00Z", src="record-usage", provider="claude",
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


def _downgrade_to_previous_epoch(ns):
    """The shape a pre-#661-S2 binary left behind."""
    conn = ns["open_db"]()
    try:
        conn.execute(f"DROP TABLE {NEW_TABLE}")
        conn.execute(f"PRAGMA user_version = {PREVIOUS_EPOCH}")
        conn.commit()
    finally:
        conn.close()


def test_c1_the_epoch_is_bumped_and_the_legacy_registry_is_untouched():
    """The registry is FROZEN. A `@stats_migration` handler for this table
    would never run on an upgraded install, because an epoch-current open
    returns before any schema work."""
    assert _cctally_core.STATS_INDEX_EPOCH == 1013
    assert _cctally_core.LEGACY_STATS_HEAD == 13
    import _cctally_db
    assert len(_cctally_db._STATS_MIGRATIONS) == 13, "the registry is FROZEN"


def test_c1_a_fresh_index_carries_the_table(ns):
    conn = ns["open_db"]()
    try:
        assert _table_exists(conn, NEW_TABLE)
        assert set(_columns(conn, NEW_TABLE)) >= {
            "id", "provider", "account_key", "effective_from",
            "previous_units_per_point", "new_units_per_point", "severity",
            "detected_at_utc", "created_at_utc"}
    finally:
        conn.close()


def test_c1_the_table_appears_on_an_upgraded_install_via_rebuild(ns):
    """The whole reason this is an epoch bump. An epoch-1010 index is
    rebuilt from the journal and comes back carrying the table."""
    _seed_journal()
    _downgrade_to_previous_epoch(ns)

    # Non-vacuity: the downgraded shape must genuinely lack the table.
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == PREVIOUS_EPOCH
        assert not _table_exists(conn, NEW_TABLE)
    finally:
        conn.close()

    conn = _resolve_epoch_transition()
    try:
        epoch = conn.execute("PRAGMA user_version").fetchone()[0]
        present = _table_exists(conn, NEW_TABLE)
    finally:
        conn.close()

    assert epoch == _cctally_core.STATS_INDEX_EPOCH
    assert epoch != PREVIOUS_EPOCH, (
        "the epoch was not bumped, so nothing forced the rebuild")
    assert present, (
        f"{NEW_TABLE} is missing after the rebuild — an epoch-current open "
        "returns before any schema work, so a migration-added table would "
        "never appear on an upgraded install")


def test_c1_the_table_is_in_the_rebuild_and_validation_contracts():
    """A table absent from `_REBUILD_REQUIRED_TABLES` fails validation as an
    UNEXPECTED table."""
    import _cctally_journal as jr
    assert NEW_TABLE in jr._REBUILD_REQUIRED_TABLES
    assert NEW_TABLE in jr._REBUILD_COUNT_TABLES


def test_c1_the_schema_fingerprint_covers_the_new_table(ns):
    """The assertion this module's docstring used to promise and not make.

    `_validate_rebuilt_stats_index` refuses a rebuilt index whose
    `_stats_schema_fingerprint` differs from the committed
    `_REBUILD_SCHEMA_FINGERPRINT` constant, so the constant has to have moved
    with the epoch. Asserted behaviourally, in both directions: the shipped
    schema hashes to the shipped constant, and dropping this one table
    changes that hash — which is what "the fingerprint covers the table"
    means for a whole-schema digest.
    """
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        assert jr._stats_schema_fingerprint(conn) == (
            jr._REBUILD_SCHEMA_FINGERPRINT)
        conn.execute(f"DROP TABLE {NEW_TABLE}")
        assert jr._stats_schema_fingerprint(conn) != (
            jr._REBUILD_SCHEMA_FINGERPRINT), (
            "the committed fingerprint does not cover the new table, so a "
            "rebuilt index missing it would validate")
    finally:
        conn.rollback()
        conn.close()


def test_c1_the_identity_is_unique_per_provider_account_and_instant(ns):
    """§6.3: the key is `(provider, canonical account identity,
    effectiveFrom)`. The fingerprint stays OUT of it — including it would
    re-alert on our own algorithm revisions."""
    conn = ns["open_db"]()
    try:
        row = (
            "claude", "unattributed", "2026-08-25T05:00:00+00:00",
            2_442_620.0, 1_685_000.0, "warn",
            "2026-08-29T00:00:00+00:00", "2026-08-29T00:00:00+00:00")
        columns = ("provider, account_key, effective_from,"
                   " previous_units_per_point, new_units_per_point, severity,"
                   " detected_at_utc, created_at_utc")
        conn.execute(
            f"INSERT INTO {NEW_TABLE} ({columns}) VALUES (?,?,?,?,?,?,?,?)",
            row)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {NEW_TABLE} ({columns})"
                " VALUES (?,?,?,?,?,?,?,?)", row)
        # A different effective instant is a different transition.
        conn.execute(
            f"INSERT INTO {NEW_TABLE} ({columns}) VALUES (?,?,?,?,?,?,?,?)",
            ("claude", "unattributed", "2026-09-01T05:00:00+00:00")
            + row[3:])
        assert conn.execute(
            f"SELECT COUNT(*) FROM {NEW_TABLE}").fetchone()[0] == 2
    finally:
        conn.close()


# --------------------------------------------------------------------------
# #690 / #692 — the four disclosure columns, added at epoch 1012
# --------------------------------------------------------------------------
PREVIOUS_EPOCH_1011 = 1011
DISCLOSURE_COLUMNS = (
    "withholding_status", "detector_input_causes",
    "composition_provenance", "baseline_withheld_days",
)

#: The pre-1012 shape of the table, written out rather than derived from the
#: shipped schema: a legacy fixture built from the CURRENT schema would gain
#: every column this test exists to prove the rebuild adds, and would pass
#: while proving nothing.
_TABLE_AT_1011 = """
    CREATE TABLE meter_rate_change_events (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        provider                 TEXT    NOT NULL,
        account_key              TEXT    NOT NULL DEFAULT 'unattributed',
        effective_from           TEXT    NOT NULL,
        previous_units_per_point REAL    NOT NULL,
        new_units_per_point      REAL    NOT NULL,
        severity                 TEXT    NOT NULL,
        detected_at_utc          TEXT    NOT NULL,
        created_at_utc           TEXT    NOT NULL,
        notified_at              TEXT,
        UNIQUE(provider, account_key, effective_from)
    )
"""

LEGACY_EFFECTIVE_FROM = "2026-08-25T00:00:00+00:00"


def _seed_legacy_rate_change_event(ns):
    """One v1 `mrc:` record on the JOURNAL, not in the index.

    The rebuild materializes stats.db from the journal, so a row inserted
    straight into the index would be discarded by the very rebuild under
    test and its survival would prove nothing about the fold.
    """
    import _lib_journal
    import _cctally_journal as jr
    mrc = ns["_load_sibling"]("_lib_meter_rate_change")
    transition = mrc.RateChangeTransition(
        provider="claude", account_key="unattributed",
        effective_from=LEGACY_EFFECTIVE_FROM,
        previous_units_per_point=2_442_620.0,
        new_units_per_point=1_665_096.0,
        severity="alarm", detected_at=FIXED.isoformat())
    created_at = FIXED.isoformat()
    jr.append_record(
        _lib_journal.make_evt(
            kind=mrc.EVT_KIND,
            id=_lib_journal.evt_id(mrc.EVT_ID_PREFIX, *transition.identity()),
            at=created_at,
            payload=mrc.event_payload(transition, created_at=created_at)),
        now_utc=FIXED)


def _downgrade_to_1011_shape(ns):
    """The shape a pre-#750-S2 binary left behind: the table exists, the four
    disclosure columns do not, and the stamp reads 1011."""
    conn = ns["open_db"]()
    try:
        conn.execute(f"DROP TABLE {NEW_TABLE}")
        conn.execute(_TABLE_AT_1011)
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_meter_rate_change_events_key"
            f" ON {NEW_TABLE}(provider, account_key, effective_from)")
        conn.execute(f"PRAGMA user_version = {PREVIOUS_EPOCH_1011}")
        conn.commit()
    finally:
        conn.close()


def test_690_an_1011_shaped_store_gains_the_disclosure_columns_on_rebuild(ns):
    """The epoch contract: a store at the previous epoch rebuilds forward.

    Asserts the columns are present AND that the journaled row is
    materialized with all four at NULL. A presence-only assertion would also
    pass on a rebuild that simply produced an empty table, which would prove
    nothing about the v1 fold under the new schema.
    """
    _seed_journal()
    _seed_legacy_rate_change_event(ns)
    _downgrade_to_1011_shape(ns)

    # Non-vacuity: the downgraded shape must genuinely lack the columns and
    # carry the previous stamp, or the rebuild below is not being forced.
    conn = sqlite3.connect(_cctally_core.DB_PATH)
    try:
        assert conn.execute(
            "PRAGMA user_version").fetchone()[0] == PREVIOUS_EPOCH_1011
        assert not set(_columns(conn, NEW_TABLE)) & set(DISCLOSURE_COLUMNS)
    finally:
        conn.close()

    conn = _resolve_epoch_transition()
    try:
        epoch = int(conn.execute("PRAGMA user_version").fetchone()[0])
        cols = set(_columns(conn, NEW_TABLE))
        row = conn.execute(
            "SELECT withholding_status, detector_input_causes,"
            " composition_provenance, baseline_withheld_days"
            f" FROM {NEW_TABLE} WHERE effective_from = ?",
            (LEGACY_EFFECTIVE_FROM,)).fetchone()
    finally:
        conn.close()

    assert epoch == _cctally_core.STATS_INDEX_EPOCH
    assert epoch == 1013
    assert epoch != PREVIOUS_EPOCH_1011, (
        "the epoch was not bumped, so nothing forced the rebuild")
    assert set(DISCLOSURE_COLUMNS) <= cols, (
        f"the rebuild did not supply {sorted(set(DISCLOSURE_COLUMNS) - cols)}"
        " — an epoch-current open returns before any schema work, so a"
        " column added by any other mechanism would never appear here")
    assert row is not None, (
        "the journaled v1 record did not materialize, so the fold was never "
        "exercised under the new schema")
    # `tuple(...)` because the stats connection sets `row_factory =
    # sqlite3.Row`, and a `Row` never compares equal to a plain tuple however
    # right its values are.
    assert tuple(row) == (None, None, None, None), (
        "a v1 record materialized fabricated evidence; it must leave all "
        f"four disclosure columns NULL, got {tuple(row)!r}")
