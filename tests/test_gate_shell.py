"""Thin-shell gate populates inputs from cache.db + disk and raises on DEFER."""
import importlib.util
import pathlib
import sqlite3
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BIN_DIR = _ROOT / "bin"
if str(_BIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_DIR))

# Register in sys.modules BEFORE exec_module so dataclass type-resolution
# (Python 3.12+: dataclasses._is_type does a sys.modules lookup) and the
# module's own ``import _cctally_core`` both resolve cleanly.
_spec = importlib.util.spec_from_file_location("_cctally_db", _BIN_DIR / "_cctally_db.py")
_db = importlib.util.module_from_spec(_spec)
sys.modules["_cctally_db"] = _db
_spec.loader.exec_module(_db)

MARKER = "claude_ingest_walk_complete"


def _cache(tmp_path, *, applied, marker, entries):
    conn = sqlite3.connect(tmp_path / "cache.db")
    _db._apply_cache_schema(conn)
    # ``_apply_cache_schema`` defines data tables only; the migration
    # framework owns ``schema_migrations``. The gate reads it, so create
    # it here (the dispatcher would in production).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    if applied:
        conn.execute("INSERT INTO schema_migrations(name, applied_at_utc) "
                     "VALUES('001_dedup_highest_wins','2026-01-01T00:00:00+00:00')")
    if marker:
        conn.execute("INSERT INTO cache_meta(key,value) VALUES(?, '2026-01-01T00:00:00+00:00')", (MARKER,))
    if entries:
        conn.execute("INSERT INTO session_entries(source_path,line_offset,timestamp_utc,model) "
                     "VALUES('p',0,'2026-01-01T00:00:00+00:00','claude-x')")
    conn.commit()
    return conn


def test_proceed_when_complete_and_nonempty(tmp_path):
    conn = _cache(tmp_path, applied=True, marker=True, entries=True)
    proj = tmp_path / "projects"; proj.mkdir(); (proj / "s.jsonl").write_text("{}\n")
    # data_present=True, complete walk, entries present -> no raise (PROCEED)
    _db._gate_001_post_ingest_completed(conn, [proj], data_present=True)


def test_defer_when_marker_absent_but_jsonl_present(tmp_path):
    conn = _cache(tmp_path, applied=True, marker=False, entries=True)
    proj = tmp_path / "projects"; proj.mkdir(); (proj / "s.jsonl").write_text("{}\n")
    with pytest.raises(_db.MigrationGateNotMet):
        _db._gate_001_post_ingest_completed(conn, [proj], data_present=True)


def test_defer_when_marker_present_but_cache_empty(tmp_path):
    conn = _cache(tmp_path, applied=True, marker=True, entries=False)
    proj = tmp_path / "projects"; proj.mkdir()  # pruned: dir exists, no jsonl
    with pytest.raises(_db.MigrationGateNotMet):
        _db._gate_001_post_ingest_completed(conn, [proj], data_present=True)
