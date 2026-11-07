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


def _partition_delete_steps(db, tmp_path, *, rows, which):
    connection = sqlite3.connect(tmp_path / f"partition-{rows}-{which}.db")
    try:
        db._apply_cache_schema(connection)
        for i in range(rows):
            _insert(connection, line_offset=i, logical_limit_key="limit-big",
                    captured_at_utc=f"2026-07-{1 + i // 86400:02d}T"
                    f"{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:"
                    f"{i % 60:02d}Z")
        connection.commit()
        ids = [row[0] for row in connection.execute(
            "SELECT id FROM quota_window_snapshots ORDER BY id")]
        victim = ids[-1] if which == "latest" else ids[0]
        before = _summary_rows(connection)
        counter = [0]

        def tick():
            counter[0] += 1
            return 0

        connection.set_progress_handler(tick, 1)
        connection.execute(
            "DELETE FROM quota_window_snapshots WHERE id=?", (victim,))
        connection.set_progress_handler(None, 0)
        connection.commit()
        assert (_summary_rows(connection) != before) == (which == "latest"), (
            "non-vacuity: only a latest-row delete moves the summary")
        _assert_exact(db, connection)
        return counter[0]
    finally:
        connection.close()


@pytest.mark.parametrize("which", ["non-latest", "latest"])
def test_partition_latest_delete_work_is_bounded_by_the_partition(
    db, tmp_path, which,
):
    """#971 T3 (failure: every deleted quota row re-reads its whole partition
    to recompute a maximum that is discarded whenever the summary row
    survives, about 0.7 ms per row on a real store). One delete's VM steps do
    not grow with the partition's size, whether or not it held the maximum."""
    small = _partition_delete_steps(db, tmp_path, rows=20, which=which)
    big = _partition_delete_steps(db, tmp_path, rows=2000, which=which)
    assert big <= small + 64, (which, small, big)


def test_the_conversations_store_carries_no_summary(db):
    connection = sqlite3.connect(":memory:")
    try:
        db._apply_conversations_schema(connection)
        assert not (OBJECTS & _objects(connection))
    finally:
        connection.close()


# ── #1033 §5.1: the exact reset-anchor evidence summary ─────────────────────
#
# One row per in-scope (root, slot, limit key, raw reset) bucket with its row
# count and its exact lexicographic minimum (source_path, line_offset, id); a
# stale row's stored minimum is a strict lower bound of the survivors. The
# reset-anchor seed folds these rows instead of the whole history, so any drift
# here seeds wrong anchors. The oracle below aggregates the physical rows in
# Python, sharing nothing with the triggers.

EVIDENCE_STATE_KEY = "codex_reset_anchor_evidence"
EVIDENCE_GENERATION_KEY = "codex_reset_anchor_evidence_generation"
EVIDENCE_OBJECTS = {
    ("table", "codex_reset_anchor_evidence"),
    ("index", "idx_codex_reset_anchor_evidence_stale"),
    ("trigger", "trg_qws_anchor_evidence_ins"),
    ("trigger", "trg_qws_anchor_evidence_del"),
    ("trigger", "trg_qws_anchor_evidence_upd"),
}
EVIDENCE_KEY_ORDER_COLUMNS = (
    "source", "source_root_key", "observed_slot", "logical_limit_key",
    "resets_at_utc", "source_path", "line_offset", "id")
_EV_A = "/roots/a/sessions/a.jsonl"
_EV_B = "/roots/a/sessions/b.jsonl"
_EV_C = "/roots/a/sessions/c.jsonl"


def _meta(conn, key):
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def _evidence_generation(conn):
    return int(_meta(conn, EVIDENCE_GENERATION_KEY))


def _evidence_summary(conn):
    return {
        tuple(row[:4]): (row[4], tuple(row[5:8]), row[8])
        for row in conn.execute(
            "SELECT source_root_key, observed_slot, logical_limit_key,"
            " resets_at_utc, row_count, min_source_path, min_line_offset,"
            " min_id, stale FROM codex_reset_anchor_evidence")
    }


def _evidence_oracle(conn):
    out = {}
    for row in conn.execute(
            "SELECT source_root_key, observed_slot, logical_limit_key,"
            " resets_at_utc, source_path, line_offset, id"
            " FROM quota_window_snapshots"
            " WHERE source='codex' AND observed_slot IS NOT NULL"):
        bucket, order = tuple(row[:4]), tuple(row[4:])
        if bucket in out:
            count, minimum = out[bucket]
            out[bucket] = (count + 1, min(minimum, order))
        else:
            out[bucket] = (1, order)
    return out


def _assert_evidence_exact(conn, *, stale_allowed=False):
    """Exact counts, exact non-stale minima, stale rows a strict lower bound."""
    assert _meta(conn, EVIDENCE_STATE_KEY) == "ready"
    summary, oracle = _evidence_summary(conn), _evidence_oracle(conn)
    assert set(summary) == set(oracle)
    for bucket, (count, minimum, stale) in summary.items():
        assert count == oracle[bucket][0], bucket
        if stale:
            assert stale_allowed, ("unexpected stale bucket", bucket)
            assert minimum < oracle[bucket][1], bucket
        else:
            assert minimum == oracle[bucket][1], bucket


def _evidence_stale(conn):
    return sorted(bucket for bucket, (_c, _m, stale)
                  in _evidence_summary(conn).items() if stale)


def _seed_evidence(conn):
    def add(path, offset, **values):
        _insert(conn, source_path=path, line_offset=offset, **values)

    week = {"logical_limit_key": "limit-weekly", "window_minutes": 10080,
            "resets_at_utc": "2026-07-25T00:00:00Z"}
    # One bucket fed by three files; b holds its minimum through a row that
    # does not hold the minimum offset (path decides first).
    add(_EV_B, 7)
    add(_EV_C, 1)
    add(_EV_B, 9)
    # The same instant spelled another way is another bucket.
    add(_EV_C, 2, resets_at_utc="2026-07-20T15:00:00+00:00")
    add(_EV_B, 3, **week)
    add(_EV_C, 4, **week)
    add(_EV_C, 5, observed_slot="secondary")
    add(_EV_B, 11, observed_slot=None)               # out of scope
    add(_EV_B, 12, source_root_key=ROOT_B)
    add(_EV_C, 13, resets_at_utc="not-a-reset")      # kept, skipped at seed
    add(_EV_B, 14, source="claude", source_root_key=None)
    conn.commit()


@pytest.fixture
def evidence_conn(db, tmp_path):
    connection = sqlite3.connect(tmp_path / "evidence.db")
    db._apply_cache_schema(connection)
    connection.commit()
    _seed_evidence(connection)
    try:
        yield connection
    finally:
        connection.close()


def _ev_ids(conn, path):
    return [row[0] for row in conn.execute(
        "SELECT id FROM quota_window_snapshots WHERE source='codex'"
        " AND source_path=? ORDER BY id", (path,))]


def _ev_statement_rollback(conn):
    columns = ", ".join(COLUMNS)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO quota_window_snapshots ({columns})"
            f" SELECT {columns.replace('line_offset', 'line_offset + 500')}"
            " FROM quota_window_snapshots WHERE source='codex'"
            " UNION ALL SELECT 'codex', ?, ?, 999, '2026-07-20T10:00:00Z',"
            " 'primary', 'limit-primary', 'codex', NULL, 300, 500.0,"
            " '2026-07-20T15:00:00Z', NULL, NULL, NULL", (ROOT_A, _EV_A))


def _ev_transaction_rollback(conn):
    _insert(conn, source_path=_EV_A, line_offset=0)
    conn.rollback()


def _ev_heal(conn):
    """A heal deletes a file's rows and re-inserts the same lines (new ids)."""
    columns = list(COLUMNS)
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM quota_window_snapshots"
        " WHERE source='codex' AND source_path=? ORDER BY line_offset",
        (_EV_B,)).fetchall()
    conn.execute("DELETE FROM quota_window_snapshots WHERE source='codex'"
                 " AND source_path=?", (_EV_B,))
    assert _evidence_stale(conn), "non-vacuity: the delete staled a bucket"
    conn.executemany(
        f"INSERT OR IGNORE INTO quota_window_snapshots ({', '.join(columns)})"
        f" VALUES ({', '.join('?' for _ in columns)})", rows)


def _ev_applier_record(**over):
    """One journal Codex quota observation, as `_apply_quota_records` reads it."""
    payload = {
        "kind": "quota_window_snapshot", "source": "codex",
        "source_root_key": ROOT_A, "source_path": _EV_A, "line_offset": 60,
        "captured_at_utc": "2026-07-20T10:00:00Z", "observed_slot": "primary",
        "logical_limit_key": "limit-weekly", "limit_id": "codex",
        "limit_name": None, "window_minutes": 10080, "used_percent": 10.0,
        "resets_at_utc": "2026-07-25T00:00:00Z", "plan_type": None,
        "individual_limit_json": None, "reached_type": None,
        "observed_model": None,
    }
    payload.update(over)
    return {"t": "obs", "provider": "codex", "payload": payload,
            "account": None}


class _CoveredOracle:
    """Every record is covered by a durable decision for ``decided``."""

    def __init__(self, decided):
        self._decided = decided

    def resolve(self, rec):
        return True, self._decided


def _ev_applier(conn, record, *, decided=None):
    """The journal applier's real write path (`_apply_quota_records`): its
    `INSERT OR IGNORE` for an uncovered record, or, with ``decided``, the
    covered record's `ON CONFLICT ... DO UPDATE` upsert."""
    import _cctally_journal as journal
    with pytest.MonkeyPatch.context() as patch:
        if decided is not None:
            patch.setattr(journal, "_CodexAttributionOracle",
                          lambda cache: _CoveredOracle(decided))
        journal._apply_quota_records(conn, [record], quiet=True)


def _ev_applier_insert(conn):
    _ev_applier(conn, _ev_applier_record())
    assert conn.execute(
        "SELECT COUNT(*) FROM quota_window_snapshots WHERE source='codex'"
        " AND source_path=? AND line_offset=60", (_EV_A,)).fetchone() == (1,), (
        "non-vacuity: the applier inserted its row")


def _ev_applier_upsert_update(conn):
    """A covered re-apply of a stored row takes the upsert's DO UPDATE
    branch (an UPDATE, which fires no insert trigger), and a second identical
    re-apply changes nothing."""
    natural = ("codex", _EV_B, 7, "limit-primary")
    record = _ev_applier_record(
        source_path=_EV_B, line_offset=7, logical_limit_key="limit-primary",
        window_minutes=300, resets_at_utc="2026-07-20T15:00:00Z")
    for _ in range(2):
        _ev_applier(conn, record, decided="d" * 32)
    assert conn.execute(
        "SELECT account_key FROM quota_window_snapshots WHERE source=?"
        " AND source_path=? AND line_offset=? AND logical_limit_key=?",
        natural).fetchone() == ("d" * 32,), (
        "non-vacuity: the DO UPDATE branch ran")


#: name -> (mutation, summary moves, stale buckets expected afterwards).
EVIDENCE_MUTATIONS = {
    "walk-batch-insert": (lambda conn: conn.executemany(
        f"INSERT OR IGNORE INTO quota_window_snapshots ({', '.join(COLUMNS)})"
        f" VALUES ({', '.join('?' for _ in COLUMNS)})",
        [("codex", ROOT_A, path, offset, "2026-07-20T10:00:00Z", "primary",
          "limit-primary", "codex", None, 300, 10.0, reset, None, None, None)
         for path, offset, reset in (
             (_EV_A, 50, "2026-07-20T15:00:00Z"),      # takes the minimum
             (_EV_C, 51, "2026-07-20T15:00:00Z"),      # counted only
             (_EV_C, 52, "2026-07-20T15:20:00Z"))]),    # a new bucket
        True, False),
    "applier-insert": (_ev_applier_insert, True, False),
    "applier-upsert-do-update": (_ev_applier_upsert_update, False, False),
    "ignored-duplicate-insert": (lambda conn: _insert(
        conn, verb="INSERT OR IGNORE", source_path=_EV_B, line_offset=7,
        resets_at_utc="2026-07-21T00:00:00Z"), False, False),
    "statement-rollback": (_ev_statement_rollback, False, False),
    "transaction-rollback": (_ev_transaction_rollback, False, False),
    "delete-a-non-minimum-row": (lambda conn: conn.execute(
        "DELETE FROM quota_window_snapshots WHERE source='codex'"
        " AND source_path=? AND line_offset=9", (_EV_B,)), True, False),
    "delete-a-minimum-row": (lambda conn: conn.execute(
        "DELETE FROM quota_window_snapshots WHERE source='codex'"
        " AND source_path=? AND line_offset=7", (_EV_B,)), True, True),
    "prune-delete": (lambda conn: conn.execute(
        "DELETE FROM quota_window_snapshots WHERE source='codex'"
        " AND source_path=?", (_EV_B,)), True, True),
    "heal-delete-and-reinsert": (_ev_heal, True, False),
    "whole-provider-delete": (lambda conn: conn.execute(
        "DELETE FROM quota_window_snapshots WHERE source='codex'"), True, False),
    "non-key-updates": (lambda conn: conn.execute(
        "UPDATE quota_window_snapshots SET canonical_resets_at_utc="
        "'2026-07-20T15:00:00Z', observed_model='gpt-5', account_key=?,"
        " used_percent=42.0, captured_at_utc='2026-07-21T00:00:00Z',"
        " window_minutes=301", ("d" * 32,)), False, False),
}


def test_1033_a_fresh_schema_installs_a_ready_exact_evidence_summary(
        db, evidence_conn):
    assert EVIDENCE_OBJECTS <= _objects(evidence_conn)
    assert len(_evidence_summary(evidence_conn)) == 6, (
        "non-vacuity: six in-scope buckets (the NULL slot is excluded)")
    _assert_evidence_exact(evidence_conn)


@pytest.mark.parametrize("name", sorted(EVIDENCE_MUTATIONS))
def test_1033_the_evidence_summary_tracks_every_writer_class(
        evidence_conn, name):
    """Summary drift that would seed wrong anchors, after each writer class."""
    mutate, moves, stale = EVIDENCE_MUTATIONS[name]
    before = _evidence_summary(evidence_conn)
    generation = _evidence_generation(evidence_conn)
    mutate(evidence_conn)
    evidence_conn.commit()
    assert (_evidence_summary(evidence_conn) != before) == moves, name
    assert bool(_evidence_stale(evidence_conn)) == stale, name
    assert _evidence_generation(evidence_conn) == generation, (
        "inserts and deletes never advance the invalidation generation")
    _assert_evidence_exact(evidence_conn, stale_allowed=stale)


def test_1033_a_heal_reinsert_clears_the_staleness_its_delete_caused(
        evidence_conn):
    """R-a: re-inserted rows keep (path, offset) and gain a larger id, which
    the full-tuple rule would never accept below the stale minimum."""
    buckets = set(_evidence_summary(evidence_conn))
    _ev_heal(evidence_conn)
    evidence_conn.commit()
    assert set(_evidence_summary(evidence_conn)) == buckets
    assert _evidence_stale(evidence_conn) == []
    _assert_evidence_exact(evidence_conn)


def test_1033_a_stale_minimum_is_cleared_only_by_a_lower_prefix(evidence_conn):
    evidence_conn.execute(
        "DELETE FROM quota_window_snapshots WHERE source='codex'"
        " AND source_path=? AND line_offset=7", (_EV_B,))
    stale = _evidence_stale(evidence_conn)
    assert len(stale) == 1
    _insert(evidence_conn, source_path=_EV_C, line_offset=70)   # above: stays
    assert _evidence_stale(evidence_conn) == stale
    _assert_evidence_exact(evidence_conn, stale_allowed=True)
    _insert(evidence_conn, source_path=_EV_A, line_offset=71)   # below: exact
    assert _evidence_stale(evidence_conn) == []
    evidence_conn.commit()
    _assert_evidence_exact(evidence_conn)


@pytest.mark.parametrize("column", EVIDENCE_KEY_ORDER_COLUMNS)
def test_1033_a_key_or_order_update_invalidates_the_summary(
        evidence_conn, column):
    """An UPDATE that moves a row between buckets or changes its order would
    leave the summary silently wrong; it flips the state and advances the
    generation instead (an id-only update included, which the change ledger
    cannot see)."""
    new_value = {
        "source": "claude", "source_root_key": ROOT_B,
        "observed_slot": "secondary", "logical_limit_key": "limit-other",
        "resets_at_utc": "2026-07-21T00:00:00Z", "source_path": _EV_A,
        "line_offset": 900, "id": 90_000,
    }[column]
    target = _ev_ids(evidence_conn, _EV_C)[0]
    generation = _evidence_generation(evidence_conn)
    if column == "source":
        evidence_conn.execute(
            "UPDATE quota_window_snapshots SET source='claude',"
            " source_root_key=NULL WHERE id=?", (target,))
    else:
        evidence_conn.execute(
            f"UPDATE quota_window_snapshots SET {column}=? WHERE id=?",
            (new_value, target))
    evidence_conn.commit()
    assert _meta(evidence_conn, EVIDENCE_STATE_KEY) == "bootstrap"
    assert _evidence_generation(evidence_conn) > generation


def test_1033_same_value_and_out_of_scope_updates_keep_the_summary(
        evidence_conn):
    generation = _evidence_generation(evidence_conn)
    evidence_conn.execute(
        "UPDATE quota_window_snapshots SET resets_at_utc=resets_at_utc,"
        " id=id, source_path=source_path")
    evidence_conn.execute(
        "UPDATE quota_window_snapshots SET resets_at_utc='2026-07-22T00:00:00Z'"
        " WHERE observed_slot IS NULL OR source='claude'")
    evidence_conn.commit()
    assert _evidence_generation(evidence_conn) == generation
    _assert_evidence_exact(evidence_conn)


def test_1033_a_bootstrap_state_turns_insert_and_delete_maintenance_off(
        evidence_conn):
    evidence_conn.execute(
        "UPDATE cache_meta SET value='bootstrap' WHERE key=?",
        (EVIDENCE_STATE_KEY,))
    before = _evidence_summary(evidence_conn)
    _insert(evidence_conn, source_path=_EV_A, line_offset=80)
    evidence_conn.execute(
        "DELETE FROM quota_window_snapshots WHERE source='codex'"
        " AND source_path=?", (_EV_C,))
    evidence_conn.commit()
    assert _evidence_summary(evidence_conn) == before
    assert _meta(evidence_conn, EVIDENCE_STATE_KEY) == "bootstrap"


def test_1033_a_delete_whose_bucket_is_missing_invalidates_the_summary(
        evidence_conn):
    evidence_conn.execute(
        "DELETE FROM codex_reset_anchor_evidence"
        " WHERE logical_limit_key='limit-weekly'")
    evidence_conn.execute(
        "DELETE FROM quota_window_snapshots WHERE source='codex'"
        " AND logical_limit_key='limit-weekly'")
    evidence_conn.commit()
    assert _meta(evidence_conn, EVIDENCE_STATE_KEY) == "bootstrap"


def test_1033_a_randomized_writer_sequence_keeps_the_evidence_exact(
        evidence_conn):
    rng = random.Random(1033)
    paths = (_EV_A, _EV_B, _EV_C, "/roots/a/sessions/d.jsonl")
    resets = ("2026-07-20T15:00:00Z", "2026-07-20T15:00:00+00:00",
              "2026-07-20T15:09:00Z", "2026-07-25T00:00:00Z")
    for step in range(400):
        roll = rng.random()
        if roll < 0.25:
            ids = [row[0] for row in evidence_conn.execute(
                "SELECT id FROM quota_window_snapshots")]
            if ids:
                evidence_conn.execute(
                    "DELETE FROM quota_window_snapshots WHERE id=?",
                    (rng.choice(ids),))
        elif roll < 0.35:
            path = rng.choice(paths)
            rows = evidence_conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM quota_window_snapshots"
                " WHERE source='codex' AND source_path=?", (path,)).fetchall()
            evidence_conn.execute(
                "DELETE FROM quota_window_snapshots WHERE source='codex'"
                " AND source_path=?", (path,))
            evidence_conn.executemany(
                f"INSERT OR IGNORE INTO quota_window_snapshots"
                f" ({', '.join(COLUMNS)})"
                f" VALUES ({', '.join('?' for _ in COLUMNS)})", rows)
        else:
            _insert(
                evidence_conn, verb="INSERT OR IGNORE",
                source_path=rng.choice(paths),
                line_offset=rng.randrange(0, 40),
                observed_slot=rng.choice(("primary", "secondary", None)),
                logical_limit_key=rng.choice(("limit-primary", "limit-weekly")),
                resets_at_utc=rng.choice(resets))
        if step % 25 == 24:
            evidence_conn.commit()
            _assert_evidence_exact(evidence_conn, stale_allowed=True)
    evidence_conn.commit()
    _assert_evidence_exact(evidence_conn, stale_allowed=True)

