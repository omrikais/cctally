"""#901 §5.1 (T1): the trigger-maintained raw-partition maxima summary.

The summary must equal a from-scratch bootstrap after every writer class the
spec's inventory names (§4.1), and the rows it selects must equal what the
pre-#901 latest-per-identity window query selects. ``WINDOW_REFERENCE_IDS`` is
that query frozen from ``56e66f07a`` with every column present, so the reference
does not share the summary's key expressions.
"""
from __future__ import annotations

import pathlib
import random
import re
import sqlite3

import pytest

import _sql_plan_guard as guard
from conftest import load_script, redirect_paths

REPO = pathlib.Path(__file__).resolve().parent.parent
ROOT_A = "a" * 32
ROOT_B = "b" * 32
MIGRATION = "047_spill_free_read_paths"
REQUIRED = ("source", "source_root_key", "source_path", "captured_at_utc",
            "observed_slot", "logical_limit_key", "resets_at_utc")
WINDOW_REFERENCE_IDS = (
    "SELECT id FROM (SELECT id, captured_at_utc,"
    " MAX(unixepoch(captured_at_utc)) OVER (PARTITION BY source_root_key,"
    " logical_limit_key, observed_slot, window_minutes, limit_id, limit_name,"
    " observed_model, account_key,"
    " COALESCE(canonical_resets_at_utc, resets_at_utc)) AS _group_latest_capture"
    " FROM quota_window_snapshots WHERE source='codex'"
    " AND source_root_key IS NOT NULL AND "
    + " AND ".join(f"trim(coalesce({column}, '')) <> ''" for column in REQUIRED)
    + " AND unixepoch(captured_at_utc) IS NOT NULL"
    " AND unixepoch(resets_at_utc) IS NOT NULL)"
    " WHERE unixepoch(captured_at_utc) = _group_latest_capture"
)
COLUMNS = ("source", "source_root_key", "source_path", "line_offset",
           "captured_at_utc", "observed_slot", "logical_limit_key", "limit_id",
           "limit_name", "window_minutes", "used_percent", "resets_at_utc",
           "observed_model", "account_key", "canonical_resets_at_utc")
OBJECTS = {
    ("table", "codex_quota_partition_latest"),
    ("index", "idx_qws_partition_capture"),
    ("trigger", "trg_qws_latest_ins"),
    ("trigger", "trg_qws_latest_del"),
    ("trigger", "trg_qws_latest_upd"),
}


@pytest.fixture
def db():
    load_script()
    import _cctally_db
    return _cctally_db


@pytest.fixture
def conn(db, tmp_path):
    connection = sqlite3.connect(tmp_path / "cache.db")
    db._apply_cache_schema(connection)
    connection.commit()
    _seed(connection)
    try:
        yield connection
    finally:
        connection.close()


def _insert(conn, *, verb="INSERT", **values):
    row = {
        "source": "codex", "source_root_key": ROOT_A,
        "source_path": "/roots/a/sessions/r.jsonl", "line_offset": 0,
        "captured_at_utc": "2026-07-20T10:00:00Z", "observed_slot": "primary",
        "logical_limit_key": "limit-primary", "limit_id": "codex",
        "limit_name": None, "window_minutes": 300, "used_percent": 10.0,
        "resets_at_utc": "2026-07-20T15:00:00Z", "observed_model": None,
        "account_key": None, "canonical_resets_at_utc": None,
    }
    row.update(values)
    conn.execute(
        f"{verb} INTO quota_window_snapshots ({', '.join(COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in COLUMNS)})",
        tuple(row[column] for column in COLUMNS),
    )


def _seed(conn):
    offset = 0

    def add(**values):
        nonlocal offset
        offset += 1
        _insert(conn, line_offset=offset, **values)

    for minute in range(5):
        add(captured_at_utc=f"2026-07-20T10:0{minute}:00Z")
    # Two more captures in the maximum's whole second, spelled differently: the
    # summary compares seconds, so all three are tied winners.
    add(captured_at_utc="2026-07-20T10:04:00.500000Z")
    add(captured_at_utc="2026-07-20T10:04:00+00:00")
    for model in (None, "", "gpt-5", "gpt-5.3-codex-spark"):
        add(observed_model=model, captured_at_utc="2026-07-20T09:00:00Z",
            logical_limit_key="limit-model")
    for account in (None, "", "unattributed", "c" * 32):
        add(account_key=account, captured_at_utc="2026-07-20T09:30:00Z",
            logical_limit_key="limit-weekly", window_minutes=10080,
            resets_at_utc="2026-07-25T00:00:00Z")
    add(source_root_key=ROOT_B, captured_at_utc="2025-07-01T00:00:00Z",
        resets_at_utc="2025-07-08T00:00:00Z")
    add(observed_slot=" ", captured_at_utc="2026-07-20T11:00:00Z")
    add(captured_at_utc="not-a-time")
    add(source="claude", source_root_key=None,
        captured_at_utc="2026-07-20T12:00:00Z")
    add(canonical_resets_at_utc="2026-07-20T15:00:00Z",
        captured_at_utc="2026-07-20T08:00:00Z")
    conn.commit()


def _summary_rows(conn):
    return sorted(conn.execute("SELECT * FROM codex_quota_partition_latest"))


def _bootstrap_rows(db, conn):
    return sorted(conn.execute(db._codex_quota_latest_bootstrap_select()))


def _summary_ids(db, conn):
    on = " AND ".join(
        f"{expr} = s.{key}" for key, expr in zip(
            db._CODEX_QUOTA_LATEST_KEY_COLUMNS,
            db._codex_quota_latest_key_exprs("q.")))
    sql = (
        "SELECT q.id FROM codex_quota_partition_latest AS s"
        " CROSS JOIN quota_window_snapshots AS q"
        " INDEXED BY idx_qws_partition_capture"
        f" ON {on} AND unixepoch(q.captured_at_utc) = s.latest_capture_epoch"
        f" WHERE {db._codex_quota_latest_validity_sql('q.')}")
    return sorted(row[0] for row in conn.execute(sql))


def _reference_ids(conn):
    return sorted(row[0] for row in conn.execute(WINDOW_REFERENCE_IDS))


def _assert_exact(db, conn):
    assert _summary_rows(conn) == _bootstrap_rows(db, conn)
    assert _summary_ids(db, conn) == _reference_ids(conn)


def _objects(conn):
    return {
        (str(row[0]), str(row[1])) for row in conn.execute(
            "SELECT type, name FROM sqlite_master")
    }


def _tied_maximum_ids(conn):
    return [row[0] for row in conn.execute(
        "SELECT id FROM quota_window_snapshots WHERE source='codex'"
        " AND source_root_key=? AND logical_limit_key='limit-primary'"
        " AND observed_model IS NULL AND account_key IS NULL"
        " AND unixepoch(captured_at_utc)=unixepoch('2026-07-20T10:04:00Z')",
        (ROOT_A,))]


def _delete_ids(conn, ids):
    conn.executemany(
        "DELETE FROM quota_window_snapshots WHERE id=?", [(i,) for i in ids])


#: name -> (mutation, whether the summary or the reference set must move).
MUTATIONS = {
    "insert-newer": (
        lambda conn: _insert(conn, line_offset=900,
                             captured_at_utc="2026-07-20T10:30:00Z"), True),
    "insert-older": (
        lambda conn: _insert(conn, line_offset=901,
                             captured_at_utc="2026-07-20T07:00:00Z"), False),
    "insert-tie-in-a-new-spelling": (
        lambda conn: _insert(conn, line_offset=902,
                             captured_at_utc="2026-07-20T10:04:00.900Z"), True),
    "insert-or-ignore-conflict": (
        lambda conn: _insert(conn, verb="INSERT OR IGNORE", line_offset=1,
                             captured_at_utc="2026-07-20T23:59:00Z"), False),
    "delete-one-tied-maximum": (
        lambda conn: _delete_ids(conn, _tied_maximum_ids(conn)[:1]), True),
    "delete-every-tied-maximum": (
        lambda conn: _delete_ids(conn, _tied_maximum_ids(conn)), True),
    "delete-a-whole-partition": (
        lambda conn: conn.execute(
            "DELETE FROM quota_window_snapshots WHERE source_root_key=?",
            (ROOT_B,)), True),
    "update-canonical-reset": (
        lambda conn: conn.execute(
            "UPDATE quota_window_snapshots"
            " SET canonical_resets_at_utc='2026-07-20T15:05:00Z'"
            " WHERE logical_limit_key='limit-primary'"
            " AND canonical_resets_at_utc IS NULL"), True),
    "update-observed-model": (
        lambda conn: conn.execute(
            "UPDATE quota_window_snapshots SET observed_model='gpt-5'"
            " WHERE observed_model IS NULL"
            " AND logical_limit_key='limit-model'"), True),
    "update-account-key": (
        lambda conn: conn.execute(
            "UPDATE quota_window_snapshots SET account_key=?"
            " WHERE account_key IS NULL", ("d" * 32,)), True),
    "update-capture-backwards": (
        lambda conn: conn.execute(
            "UPDATE quota_window_snapshots"
            " SET captured_at_utc='2026-07-19T00:00:00Z' WHERE id=?",
            (_tied_maximum_ids(conn)[0],)), True),
    "update-a-non-partition-column": (
        lambda conn: conn.execute(
            "UPDATE quota_window_snapshots SET used_percent=55.0"), False),
    "invalidate-the-tied-maxima": (
        lambda conn: conn.executemany(
            "UPDATE quota_window_snapshots SET observed_slot='' WHERE id=?",
            [(i,) for i in _tied_maximum_ids(conn)]), True),
    "revalidate-a-refused-row": (
        lambda conn: conn.execute(
            "UPDATE quota_window_snapshots SET observed_slot='primary'"
            " WHERE observed_slot=' '"), True),
    "flip-a-claude-row-to-codex": (
        lambda conn: conn.execute(
            "UPDATE quota_window_snapshots SET source='codex',"
            " source_root_key=? WHERE source='claude'", (ROOT_A,)), True),
    "prune-the-change-ledger": (
        lambda conn: conn.execute("DELETE FROM quota_window_change_log"),
        False),
}


def test_a_fresh_schema_installs_every_summary_object(db, conn):
    assert OBJECTS <= _objects(conn)
    assert _summary_rows(conn), "the seeded store must hold summary rows"
    _assert_exact(db, conn)


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_the_summary_tracks_every_writer_class(db, conn, name):
    mutate, moves = MUTATIONS[name]
    before = (_summary_rows(conn), _reference_ids(conn))
    count_before = conn.execute(
        "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0]
    mutate(conn)
    conn.commit()
    after = (_summary_rows(conn), _reference_ids(conn))
    assert (after != before) == moves, (
        f"{name}: the fixture no longer exercises this case")
    if name == "insert-or-ignore-conflict":
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] == (
            count_before)
    _assert_exact(db, conn)


def test_clearing_every_codex_row_and_replaying_keeps_the_summary_exact(
    db, conn,
):
    import _cctally_cache

    _cctally_cache._clear_codex_derived_rows(conn)
    conn.commit()
    assert _summary_rows(conn) == []
    # The clear leaves the seed's one Claude row behind; drop it so the replay
    # below re-inserts the whole seed without a key conflict.
    conn.execute("DELETE FROM quota_window_snapshots WHERE source <> 'codex'")
    conn.commit()
    _seed(conn)
    _assert_exact(db, conn)


def test_a_randomized_writer_sequence_keeps_the_summary_exact(db, conn):
    rng = random.Random(901)
    spellings = ("2026-07-20T10:%02d:00Z", "2026-07-20T10:%02d:00.250Z",
                 "2026-07-20T10:%02d:00+00:00")
    for step in range(400):
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM quota_window_snapshots")]
        roll = rng.random()
        if roll < 0.3 and ids:
            conn.execute("DELETE FROM quota_window_snapshots WHERE id=?",
                         (rng.choice(ids),))
        elif roll < 0.45 and ids:
            conn.execute(
                "UPDATE quota_window_snapshots SET canonical_resets_at_utc=?"
                " WHERE id=?",
                (rng.choice([None, "2026-07-20T15:05:00Z"]), rng.choice(ids)))
        elif roll < 0.55 and ids:
            conn.execute(
                "UPDATE quota_window_snapshots SET observed_model=? WHERE id=?",
                (rng.choice([None, "", "gpt-5"]), rng.choice(ids)))
        elif roll < 0.65 and ids:
            conn.execute(
                "UPDATE quota_window_snapshots SET captured_at_utc=? WHERE id=?",
                (rng.choice(spellings) % rng.randrange(0, 6), rng.choice(ids)))
        else:
            _insert(
                conn, verb="INSERT OR IGNORE", line_offset=rng.randrange(0, 600),
                captured_at_utc=rng.choice(spellings) % rng.randrange(0, 6),
                observed_model=rng.choice([None, "", "gpt-5"]),
                account_key=rng.choice([None, "", "e" * 32]),
                logical_limit_key=rng.choice(["limit-primary", "limit-weekly"]))
        if step % 25 == 24:
            conn.commit()
            _assert_exact(db, conn)
    conn.commit()
    _assert_exact(db, conn)


def test_schema_apply_rebootstraps_when_an_object_is_missing(db, conn):
    conn.execute("DROP TRIGGER trg_qws_latest_del")
    _delete_ids(conn, _tied_maximum_ids(conn))
    conn.commit()
    assert _summary_rows(conn) != _bootstrap_rows(db, conn), (
        "non-vacuity: without the delete trigger the summary must go stale")
    db._apply_cache_schema(conn)
    conn.commit()
    assert OBJECTS <= _objects(conn)
    _assert_exact(db, conn)


def test_the_migration_handler_rebootstraps_unconditionally(db, conn):
    handler = next(item.handler for item in db._CACHE_MIGRATIONS
                   if item.name == MIGRATION)
    conn.execute(
        "UPDATE codex_quota_partition_latest SET latest_capture_epoch = 0")
    conn.commit()
    assert _summary_rows(conn) != _bootstrap_rows(db, conn)
    handler(conn)
    _assert_exact(db, conn)


def test_a_failed_bootstrap_leaves_no_partial_install(db, tmp_path, monkeypatch):
    connection = sqlite3.connect(tmp_path / "fresh.db")
    try:
        db._apply_cache_schema(connection)
        for statement in (
            "DROP TRIGGER trg_qws_latest_ins", "DROP TRIGGER trg_qws_latest_del",
            "DROP TRIGGER trg_qws_latest_upd",
            "DROP INDEX idx_qws_partition_capture",
            "DROP TABLE codex_quota_partition_latest",
        ):
            connection.execute(statement)
        connection.commit()
        monkeypatch.setattr(
            db, "_codex_quota_latest_bootstrap_sql",
            lambda: "INSERT INTO codex_quota_partition_latest"
                    " SELECT * FROM no_such_table")
        with pytest.raises(sqlite3.OperationalError):
            db._apply_codex_quota_latest_summary(connection)
        assert not (OBJECTS & _objects(connection))
    finally:
        connection.close()


def test_an_older_binary_trim_then_reupgrade_converges(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    import _cctally_db as db

    conn = ns["open_cache_db"]()
    try:
        _seed(conn)
    finally:
        conn.close()
    raw = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    try:
        # What an older binary's opener does to a newer store, then what it
        # keeps doing: writing through the triggers it does not know about.
        raw.execute("DELETE FROM schema_migrations WHERE name=?", (MIGRATION,))
        raw.execute("PRAGMA user_version=46")
        _insert(raw, line_offset=700, captured_at_utc="2026-07-20T10:45:00Z")
        raw.execute("DELETE FROM quota_window_snapshots WHERE source_root_key=?",
                    (ROOT_B,))
        raw.execute(
            "UPDATE codex_quota_partition_latest SET latest_capture_epoch = 0"
            " WHERE k_observed_model = 'NULL'")
        raw.commit()
        assert _summary_rows(raw) != _bootstrap_rows(db, raw)
    finally:
        raw.close()
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(
            db._CACHE_MIGRATIONS)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,)).fetchone()[0] == 1
        _assert_exact(db, conn)
    finally:
        conn.close()


_REPLACING_WRITE = re.compile(
    r"(?:INSERT\s+OR\s+REPLACE|REPLACE)\s+INTO\s+quota_window_snapshots\b",
    re.IGNORECASE,
)


def test_no_writer_replaces_quota_rows():
    """A REPLACE deletes the conflicting row without firing the delete trigger
    (recursive_triggers is off), which would leave a stale maximum behind. An
    upsert's DO UPDATE branch is safe — it fires the update trigger — so only
    REPLACE is refused."""
    assert _REPLACING_WRITE.search(
        "INSERT OR REPLACE INTO quota_window_snapshots (a) VALUES (1)")
    assert _REPLACING_WRITE.search(
        "REPLACE INTO quota_window_snapshots (a) VALUES (1)")
    offenders = []
    for path in sorted((REPO / "bin").iterdir()):
        if not path.is_file() or path.name.startswith("build-"):
            continue
        if path.suffix not in (".py", "") or path.name == "_fixture_builders.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _REPLACING_WRITE.search(text):
            offenders.append(path.name)
    assert offenders == []


def test_the_required_text_set_matches_the_loader(db):
    import _cctally_quota

    assert db._CODEX_QUOTA_LATEST_REQUIRED_TEXT == (
        _cctally_quota._CODEX_QUOTA_REQUIRED_TEXT)


@pytest.mark.parametrize("image", ["NEW", "OLD"])
def test_trigger_repairs_seek_the_partition_index(db, conn, image):
    statement = db._codex_quota_latest_recompute_sql(image, only_if_absent=True)
    params = {column: "x" for column in (
        "source", "source_root_key", "source_path", "captured_at_utc",
        "observed_slot", "logical_limit_key", "resets_at_utc",
        "canonical_resets_at_utc", "window_minutes", "limit_id", "limit_name",
        "observed_model", "account_key")}
    plan = [str(row[3]) for row in conn.execute(
        "EXPLAIN QUERY PLAN " + statement.replace(f"{image}.", ":"), params)]
    assert any(
        detail.startswith(
            "SEARCH quota_window_snapshots USING INDEX "
            "idx_qws_partition_capture (source=? AND <expr>=?")
        for detail in plan), plan
    assert not any(
        detail.startswith("SCAN quota_window_snapshots") for detail in plan), plan
    assert not any(
        marker in detail for detail in plan for marker in guard.TEMP_MARKERS), plan


def test_the_conversations_store_carries_no_summary(db):
    connection = sqlite3.connect(":memory:")
    try:
        db._apply_conversations_schema(connection)
        assert not (OBJECTS & _objects(connection))
    finally:
        connection.close()
