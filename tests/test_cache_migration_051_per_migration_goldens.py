"""Per-migration goldens for #927's retained-identity inventory (cache 051).

``pre.sqlite`` is a 050-head store with one Codex cursor row and one
accounting row and none of the inventory objects; ``post.sqlite`` is the
handler's result. Detects: an upgrade that records the inventory as complete
over existing history (the hook would then plan prunes from an empty set), an
upgrade that scans history while opening, or an inventory whose triggers miss
a writer of the four families.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


IDEMPOTENCY_COVERED = True
MIGRATION = "051_codex_retained_identities"
TABLE = "codex_retained_identities"
INDEX = "idx_codex_retained_identities_key"
TRIGGERS = {
    f"trg_codex_identity_{family}_{op}"
    for family in ("files", "entries", "quota", "threads")
    for op in ("ins", "upd")
}
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations"
    / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
PATH = "/fixtures/codex/sessions/2026/10/31/rollout.jsonl"


@pytest.fixture
def db():
    load_script()
    import _cctally_db
    return _cctally_db


def _handler(db):
    return next(item.handler for item in db._CACHE_MIGRATIONS
                if item.name == MIGRATION)


def _objects(conn):
    return tuple(conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        f"WHERE name IN ({','.join('?' * (2 + len(TRIGGERS)))}) "
        "ORDER BY type, name",
        (TABLE, INDEX, *sorted(TRIGGERS)),
    ))


def _state(conn):
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key='codex_identity_inventory'"
    ).fetchone()
    return None if row is None else row[0]


def _copy(path: Path, tmp_path: Path) -> Path:
    target = tmp_path / path.name
    shutil.copyfile(path, target)
    return target


def test_pre_fixture_is_a_050_head_without_the_inventory(tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 50
        assert _objects(conn) == ()
        assert _state(conn) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 1
    finally:
        conn.close()


def test_post_fixture_awaits_its_bootstrap_over_existing_history(tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 51
        assert {row[1] for row in _objects(conn)} == {TABLE, INDEX, *TRIGGERS}
        assert _state(conn) == "bootstrap:files:0"
        assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_takes_pre_to_post_shape_and_is_idempotent(db, tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        handler = _handler(db)
        handler(conn)
        objects = _objects(conn)
        assert {row[1] for row in objects} == {TABLE, INDEX, *TRIGGERS}
        assert _state(conn) == "bootstrap:files:0"
        # A bootstrap in progress survives a markerless retry.
        conn.execute(
            "UPDATE cache_meta SET value='bootstrap:entries:1' "
            "WHERE key='codex_identity_inventory'")
        conn.commit()
        handler(conn)
        assert _objects(conn) == objects
        assert _state(conn) == "bootstrap:entries:1"
    finally:
        conn.close()


def test_handler_marks_an_empty_store_ready_and_triggers_keep_it(db, tmp_path):
    """Detects an inventory that misses a writer: after 051 on a store with no
    Codex rows, every family's INSERT and identity UPDATE lands in it."""
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        conn.execute("DELETE FROM codex_session_entries")
        conn.execute("DELETE FROM codex_session_files")
        conn.commit()
        _handler(db)(conn)
        assert _state(conn) == "ready"
        conn.execute(
            "INSERT INTO codex_session_files (path, size_bytes, mtime_ns, "
            "last_byte_offset, last_ingested_at, source_root_key) "
            "VALUES (?, 1, 1, 1, 'x', NULL)", (PATH,))
        conn.execute(
            "UPDATE codex_session_files SET source_root_key='rk-b' "
            "WHERE path=?", (PATH,))
        conn.execute(
            "INSERT INTO quota_window_snapshots (source, source_root_key, "
            "source_path, line_offset, captured_at_utc, logical_limit_key, "
            "window_minutes, used_percent, resets_at_utc) VALUES "
            "('claude', 'rk-c', ?, 1, 'x', 'primary', 300, 1.0, 'y')", (PATH,))
        claude_only = set(conn.execute(f"SELECT * FROM {TABLE}"))
        conn.execute("UPDATE quota_window_snapshots SET source='codex'")
        conn.commit()
        assert claude_only == {(PATH, None), (PATH, "rk-b")}
        assert set(conn.execute(f"SELECT * FROM {TABLE}")) == {
            (PATH, None), (PATH, "rk-b"), (PATH, "rk-c")}
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
