"""#195 Task 3/4: the split lands on replay, including on rows the partial
(msg_id, req_id) dedup index does not cover.

IDEMPOTENCY_COVERED = True   # required by tests/test_migration_registry_completeness.py
"""
IDEMPOTENCY_COVERED = True

import sqlite3
import pytest

DDL = """
CREATE TABLE session_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_path TEXT NOT NULL, line_offset INTEGER NOT NULL,
  timestamp_utc TEXT NOT NULL, model TEXT NOT NULL,
  msg_id TEXT, req_id TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_create_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  usage_extra_json TEXT, cost_usd_raw REAL, speed TEXT,
  mutation_seq INTEGER NOT NULL DEFAULT 0, mutation_min_ts TEXT,
  account_key TEXT,
  cache_create_1h_tokens INTEGER, cache_create_5m_tokens INTEGER);
CREATE UNIQUE INDEX idx_entries_dedup ON session_entries(msg_id, req_id)
  WHERE msg_id IS NOT NULL AND req_id IS NOT NULL;
CREATE UNIQUE INDEX idx_entries_physical ON session_entries(source_path, line_offset);
"""


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(DDL)
    yield c
    c.close()


def _row(path, off, msg_id, req_id, h):
    """One bind tuple, ordered by the production INSERT's column list:
    source_path, line_offset, timestamp_utc, model, msg_id, req_id,
    input_tokens, output_tokens, cache_create_tokens, cache_read_tokens,
    usage_extra_json, speed, cost_usd_raw,
    cache_create_1h_tokens, cache_create_5m_tokens,
    mutation_seq, mutation_min_ts, account_key.

    `speed` is non-NULL on BOTH sides of every replay here, so the existing
    speed tiebreak can never fire — the #195 third guard branch is the only
    thing that can land the enrichment. That is deliberate: it keeps these
    tests non-vacuous about the branch they claim to exercise.
    """
    return (path, off, "2026-07-25T00:00:00+00:00", "claude-opus-5", msg_id, req_id,
            10, 20, 1000, 5, None, "standard", None,
            h, None if h is None else 1000 - h,
            0, None, None)


def _insert(conn, sql, row):
    conn.execute(sql, row)
    conn.commit()


@pytest.mark.parametrize("msg_id,req_id", [
    ("m1", "r1"),        # both keys present — covered by the dedup index
    (None, "r1"),        # msg_id NULL   — NOT covered
    ("m1", None),        # req_id NULL   — NOT covered
    (None, None),        # both NULL     — NOT covered
])
def test_replay_lands_the_split_for_every_key_shape(conn, msg_id, req_id):
    """Gate P1-1: idx_entries_physical is a FULL unique index, so a replay of a
    row the partial dedup index does not cover raises IntegrityError unless the
    INSERT declares a second conflict target."""
    from _cctally_cache import SESSION_ENTRY_UPSERT_SQL_REWALK as SQL
    _insert(conn, SQL, _row("/a.jsonl", 0, msg_id, req_id, None))   # pre-#195 row
    _insert(conn, SQL, _row("/a.jsonl", 0, msg_id, req_id, 700))    # the re-walk
    got = conn.execute("SELECT cache_create_1h_tokens, COUNT(*) FROM session_entries").fetchone()
    assert got == (700, 1), "split must land and the row must not duplicate"


def test_one_nullable_row_does_not_roll_back_its_files_other_rows(conn):
    """The real-world failure: the ingest catches sqlite3.DatabaseError and
    rolls back the WHOLE per-file transaction, so one bad row silently strips
    every row in that file, forever."""
    from _cctally_cache import SESSION_ENTRY_UPSERT_SQL_REWALK as SQL
    for off, (mi, ri) in enumerate([("m1", "r1"), (None, None), ("m2", "r2")]):
        _insert(conn, SQL, _row("/mixed.jsonl", off, mi, ri, None))
    conn.execute("BEGIN")
    for off, (mi, ri) in enumerate([("m1", "r1"), (None, None), ("m2", "r2")]):
        conn.execute(SQL, _row("/mixed.jsonl", off, mi, ri, 700))
    conn.commit()
    rows = conn.execute(
        "SELECT cache_create_1h_tokens FROM session_entries ORDER BY line_offset").fetchall()
    assert rows == [(700,), (700,), (700,)]
    assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == 3


def test_replay_is_idempotent(conn):
    from _cctally_cache import SESSION_ENTRY_UPSERT_SQL_REWALK as SQL
    _insert(conn, SQL, _row("/a.jsonl", 0, "m1", "r1", None))
    for _ in range(3):
        _insert(conn, SQL, _row("/a.jsonl", 0, "m1", "r1", 700))
    assert conn.execute(
        "SELECT cache_create_1h_tokens, COUNT(*) FROM session_entries").fetchone() == (700, 1)


def test_physical_conflict_updates_only_split_and_mutation_stamps(conn):
    """#418: the replay-only physical-key handler enriches a retained row.

    Every other incoming mutable value deliberately differs.  The physical
    identity and first account stamp are sticky, while timestamp/model/token/
    usage/speed/raw-cost values remain byte-for-byte those of the stored row.
    """
    from _cctally_cache import SESSION_ENTRY_UPSERT_SQL_REWALK as SQL

    stored = (
        "/physical.jsonl", 17, "2026-07-25T00:00:00+00:00", "stored-model",
        None, None, 10, 20, 1000, 5, '{"stored":true}', "standard", 1.25,
        None, None, 3, "2026-07-25T00:00:00+00:00", "account-first",
    )
    incoming = (
        "/physical.jsonl", 17, "2026-07-24T00:00:00+00:00", "incoming-model",
        None, None, 90, 80, 1000, 70, '{"incoming":true}', "fast", 9.75,
        700, 300, 9, "2026-07-24T00:00:00+00:00", "account-later",
    )
    _insert(conn, SQL, stored)
    before = conn.execute(
        "SELECT source_path, line_offset, timestamp_utc, model, msg_id, req_id, "
        "input_tokens, output_tokens, cache_create_tokens, cache_read_tokens, "
        "usage_extra_json, speed, cost_usd_raw, cache_create_1h_tokens, "
        "cache_create_5m_tokens, mutation_seq, mutation_min_ts, account_key "
        "FROM session_entries"
    ).fetchone()

    _insert(conn, SQL, incoming)
    after = conn.execute(
        "SELECT source_path, line_offset, timestamp_utc, model, msg_id, req_id, "
        "input_tokens, output_tokens, cache_create_tokens, cache_read_tokens, "
        "usage_extra_json, speed, cost_usd_raw, cache_create_1h_tokens, "
        "cache_create_5m_tokens, mutation_seq, mutation_min_ts, account_key "
        "FROM session_entries"
    ).fetchone()

    expected = list(before)
    expected[13:17] = [
        700,
        300,
        9,
        "2026-07-24T00:00:00+00:00",
    ]
    assert after == tuple(expected)
    assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone() == (1,)


@pytest.mark.parametrize("stored_h,stored_m", [
    (None, 300),
    (700, None),
    (700, 300),
])
def test_physical_conflict_requires_the_whole_stored_split_absent(
        conn, stored_h, stored_m):
    """A partial or complete retained split is not replay-overwritten."""
    from _cctally_cache import SESSION_ENTRY_UPSERT_SQL_REWALK as SQL

    stored = list(_row("/physical.jsonl", 17, None, None, None))
    stored[13:15] = [stored_h, stored_m]
    incoming = list(_row("/physical.jsonl", 17, None, None, 600))
    _insert(conn, SQL, tuple(stored))
    before = conn.execute("SELECT * FROM session_entries").fetchone()
    _insert(conn, SQL, tuple(incoming))
    assert conn.execute("SELECT * FROM session_entries").fetchone() == before


# ---------------------------------------------------------------------------
# Per-migration goldens for cache migration 030_session_entries_cache_creation_split
# (#195). The two split columns are added by `_apply_cache_schema`
# (add_column_if_missing), so they are present in BOTH pre and post; this
# migration exists only to ARM the cost-side re-walk: set the dedicated
# `cache_creation_split_rewalk_pending` flag and zero the per-file size/offset
# cursors. It does NOT wipe session_entries, and it deliberately LEAVES
# `claude_ingest_walk_complete` alone — that marker gates the stats recomputes
# against a half-populated session_entries, a hazard 030 does not create.
# ---------------------------------------------------------------------------
import importlib.util as ilu
import shutil
import sys
from pathlib import Path

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "030_session_entries_cache_creation_split"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
_MIGRATION = "030_session_entries_cache_creation_split"


@pytest.fixture(scope="module")
def cctally_module():
    from importlib.machinery import SourceFileLoader

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _handler(cctally_module):
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def _marker(conn):
    return conn.execute(
        "SELECT value FROM cache_meta WHERE key='claude_ingest_walk_complete'"
    ).fetchone()


def _rewalk_flag(conn):
    return conn.execute(
        "SELECT value FROM cache_meta WHERE key='cache_creation_split_rewalk_pending'"
    ).fetchone()


def _cursors(conn):
    return conn.execute(
        "SELECT path, size_bytes, last_byte_offset FROM session_files ORDER BY path"
    ).fetchall()


# The post-030 cursor state. `size_bytes = -1` is the invalidation sentinel:
# it never equals a real `st_size` (so the `size == prev_size` early-exit
# misses), it is always LESS than one (so the truncation escalation cannot
# fire), and it is TRUTHY — which is what keeps a tracked-but-deleted path
# visible to the two orphan gates that read `size_bytes` as the
# had-ingested-bytes bit. `/p/c.jsonl` never ingested anything, so it stays at
# 0 and is NOT promoted into an orphan candidate (review gate P2a).
_INVALIDATED_CURSORS = [("/p/a.jsonl", -1, 0),
                        ("/p/b.jsonl", -1, 0),
                        ("/p/c.jsonl", 0, 0)]


def test_never_ingested_row_is_not_promoted_to_the_sentinel(cctally_module, tmp_path):
    """Both orphan gates treat a truthy `size_bytes` as 'this path had ingested
    bytes'. A row that never ingested must keep its 0 or 030 would invent
    orphans out of the synthetic rows fixtures deliberately seed."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _handler(cctally_module)(conn)
        assert conn.execute(
            "SELECT size_bytes FROM session_files WHERE path='/p/c.jsonl'"
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_sentinel_survives_a_rerun_rather_than_compounding(cctally_module, tmp_path):
    """`CASE WHEN size_bytes > 0` is a one-way gate: a second arm must leave the
    already-invalidated rows at -1, never walk them further negative."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        handler(conn)
        assert _cursors(conn) == _INVALIDATED_CURSORS
    finally:
        conn.close()


def test_pre_fixture_at_029_head_with_marker_and_cursors(cctally_module):
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='029_backfill_claude_account'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,)).fetchone()[0] == 0
        # The columns come from _apply_cache_schema, not this migration.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_entries)")}
        assert {"cache_create_1h_tokens", "cache_create_5m_tokens"} <= cols
        assert _marker(conn) is not None, "pre must carry a walk-complete marker"
        assert _rewalk_flag(conn) is None, "pre must not be armed yet"
        assert _cursors(conn) == [("/p/a.jsonl", 4096, 4096),
                                  ("/p/b.jsonl", 8192, 8192),
                                  ("/p/c.jsonl", 0, 0)]
        assert conn.execute(
            "SELECT COUNT(*) FROM session_entries "
            "WHERE cache_create_1h_tokens IS NULL").fetchone()[0] == 2
    finally:
        conn.close()


def test_post_fixture_has_the_walk_armed(cctally_module):
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _rewalk_flag(conn) == ("1",), "the re-walk must be armed"
        # The stats-recompute gate's marker is NOT collateral damage: 030
        # preserves every row, so the half-populated hazard it guards does not
        # exist and clearing it would defer migrations 008/009/010 for a reason
        # that is not real.
        assert _marker(conn) is not None, "walk-complete marker must survive 030"
        assert _cursors(conn) == _INVALIDATED_CURSORS
        # Rows are PRESERVED — this migration arms a walk, it does not wipe.
        assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_arms_the_rewalk(cctally_module, tmp_path):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _handler(cctally_module)(conn)
        assert _rewalk_flag(conn) == ("1",)
        assert _marker(conn) is not None
        assert _cursors(conn) == _INVALIDATED_CURSORS
        assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == 2
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    """Re-running re-arms a walk that is already correct — must not raise."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        handler(conn)
        assert _rewalk_flag(conn) == ("1",)
        assert _marker(conn) is not None
        assert _cursors(conn) == _INVALIDATED_CURSORS
        assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == 2
    finally:
        conn.close()
