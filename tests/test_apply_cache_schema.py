"""_apply_cache_schema is the single cache.db schema source (cctally-dev#93, D4)."""
import importlib.util
import pathlib
import sqlite3
import sys

_BIN_DIR = pathlib.Path(__file__).resolve().parents[1] / "bin"
if str(_BIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_DIR))
_spec = importlib.util.spec_from_file_location("_cctally_db", _BIN_DIR / "_cctally_db.py")
_db = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("_cctally_db", _db)
_spec.loader.exec_module(_db)


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_creates_cache_meta_and_claude_tables():
    conn = sqlite3.connect(":memory:")
    _db._apply_cache_schema(conn)
    t = _tables(conn)
    assert {"session_files", "session_entries", "cache_meta",
            "codex_session_files", "codex_session_entries"} <= t


def test_session_files_has_session_id_and_project_path():
    conn = sqlite3.connect(":memory:")
    _db._apply_cache_schema(conn)
    assert {"session_id", "project_path"} <= _cols(conn, "session_files")


def test_cache_meta_columns():
    conn = sqlite3.connect(":memory:")
    _db._apply_cache_schema(conn)
    assert {"key", "value"} <= _cols(conn, "cache_meta")


def test_does_not_add_codex_last_total_tokens():
    # The Codex last_total_tokens ALTER stays in open_cache_db (P1#3); the
    # shared helper must NOT add it (it carries a one-time purge side-effect).
    conn = sqlite3.connect(":memory:")
    _db._apply_cache_schema(conn)
    assert "last_total_tokens" not in _cols(conn, "codex_session_files")


def test_idempotent_second_apply_is_noop():
    conn = sqlite3.connect(":memory:")
    _db._apply_cache_schema(conn)
    _db._apply_cache_schema(conn)  # must not raise
    assert "cache_meta" in _tables(conn)


def test_009_style_project_path_join_resolves():
    # The R3 landmine: a join on sf.project_path must prepare without error.
    conn = sqlite3.connect(":memory:")
    _db._apply_cache_schema(conn)
    conn.execute(
        "SELECT se.id FROM session_entries se "
        "LEFT JOIN session_files sf ON se.source_path = sf.path "
        "WHERE sf.project_path IS NULL"
    ).fetchall()  # no 'no such column: sf.project_path'


def _indexes(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


_PHYS_INSERT = (
    "INSERT INTO session_entries (source_path, line_offset, timestamp_utc, "
    "model, msg_id, req_id, input_tokens, output_tokens, cache_create_tokens, "
    "cache_read_tokens, usage_extra_json, speed, cost_usd_raw, mutation_seq, "
    "mutation_min_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def test_apply_cache_schema_creates_physical_index_when_clean(tmp_path):
    # #279 S3 F3: a fresh/clean DB gets the physical-key UNIQUE backstop
    # immediately at schema-apply time.
    conn = sqlite3.connect(tmp_path / "c.db")
    _db._apply_cache_schema(conn)
    assert "idx_entries_physical" in _indexes(conn)


def test_apply_cache_schema_tolerates_preexisting_physical_dupes(tmp_path):
    # #279 S3 F3: the guarded create runs on EVERY open BEFORE the migration
    # dispatcher, so a legacy DB holding historical physical-key duplicates must
    # still open (index left ABSENT) — never crash at schema-apply time. Cache
    # migration 020 dedups it, after which a later open creates the index.
    conn = sqlite3.connect(tmp_path / "legacy.db")
    _db._apply_cache_schema(conn)
    # A genuine pre-020 DB lacks the index; drop it so we can seed duplicates.
    conn.execute("DROP INDEX IF EXISTS idx_entries_physical")
    dup = ("/a.jsonl", 0, "2026-07-01T10:00:00+00:00", "claude-opus-4-8",
           None, None, 1, 1, 0, 0, None, None, None, 0, None)
    dup2 = ("/a.jsonl", 0, "2026-07-01T11:00:00+00:00", "claude-opus-4-8",
            None, None, 2, 2, 0, 0, None, None, None, 0, None)
    conn.execute(_PHYS_INSERT, dup)
    conn.execute(_PHYS_INSERT, dup2)
    conn.commit()

    _db._apply_cache_schema(conn)  # must NOT raise despite the duplicates
    assert "idx_entries_physical" not in _indexes(conn)
