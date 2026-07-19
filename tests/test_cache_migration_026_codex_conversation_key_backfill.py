"""Regression coverage for cache migration 026 (#312).

Historical Codex accounting can be replayed from byte zero only when the
rollout retains both the native id and its thread source.  The migration must
therefore clear every Codex-derived row family, never manufacture metadata,
and leave Claude state untouched while the next Codex sync rederives facts.
"""
from __future__ import annotations

import fcntl
import pathlib
import shutil
import sqlite3
import sys

from conftest import load_script, redirect_paths


MIGRATION = "026_codex_conversation_key_backfill"
PREDECESSOR = "025_codex_conversation_normalization"
FIXTURE_DIR = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

# Required by tests/test_migration_registry_completeness.py.
IDEMPOTENCY_COVERED = True


def _db_module():
    return sys.modules["_cctally_db"]


def _seed_existing_cache(tmp_path, monkeypatch):
    """Build a populated cache at the 025 head with every Codex row family."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core as core

    db = _db_module()
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        db._apply_cache_schema(conn)
        db.add_column_if_missing(conn, "codex_session_files", "last_total_tokens", "INTEGER")
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
        )
        for migration in db._CACHE_MIGRATIONS:
            if migration.seq < 26:
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at_utc) VALUES (?, ?)",
                    (migration.name, "2026-07-18T00:00:00Z"),
                )
        conn.execute("PRAGMA user_version = 25")

        # Claude sentinels are strictly outside 026's destructive scope.
        conn.execute(
            "INSERT INTO session_entries(source_path, line_offset, timestamp_utc, model) "
            "VALUES ('/claude/source.jsonl', 1, '2026-07-18T00:00:00Z', 'claude-test')"
        )
        conn.execute(
            "INSERT INTO conversation_messages(session_id, source_path, byte_offset, entry_type, text) "
            "VALUES ('claude-session', '/claude/source.jsonl', 1, 'assistant', 'keep me')"
        )
        conn.execute(
            "INSERT INTO quota_window_snapshots(source, source_path, line_offset, captured_at_utc, "
            "logical_limit_key, window_minutes, used_percent, resets_at_utc) "
            "VALUES ('claude', '/claude/source.jsonl', 1, '2026-07-18T00:00:00Z', "
            "'claude-window', 60, 10, '2026-07-18T01:00:00Z')"
        )

        conn.execute(
            "INSERT INTO codex_source_roots(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
            "VALUES ('root-a', '/codex', '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO codex_session_entries(source_path, line_offset, timestamp_utc, session_id, model, "
            "total_tokens, source_root_key, conversation_key) "
            "VALUES ('/codex/source.jsonl', 1, '2026-07-18T00:00:00Z', 'native-a', 'gpt-test', "
            "10, 'root-a', 'conversation-a')"
        )
        conn.execute(
            "INSERT INTO codex_session_files(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            "last_session_id, last_model, last_total_tokens, source_root_key, last_native_thread_id, "
            "last_root_thread_id, last_conversation_key) "
            "VALUES ('/codex/source.jsonl', 100, 1, 100, '2026-07-18T00:00:00Z', 'native-a', "
            "'gpt-test', 10, 'root-a', 'native-a', 'root-a', 'conversation-a')"
        )
        conn.execute(
            "INSERT INTO codex_conversation_threads(conversation_key, source_root_key, native_thread_id, "
            "root_thread_id, source_path) VALUES ('conversation-a', 'root-a', 'native-a', 'root-a', "
            "'/codex/source.jsonl')"
        )
        conn.execute(
            "INSERT INTO codex_conversation_events(source_path, line_offset, source_root_key, conversation_key, "
            "native_thread_id, root_thread_id, payload_json) VALUES ('/codex/source.jsonl', 1, 'root-a', "
            "'conversation-a', 'native-a', 'root-a', '{}')"
        )
        conn.execute(
            "INSERT INTO quota_window_snapshots(source, source_root_key, source_path, line_offset, "
            "captured_at_utc, logical_limit_key, window_minutes, used_percent, resets_at_utc) "
            "VALUES ('codex', 'root-a', '/codex/source.jsonl', 1, '2026-07-18T00:00:00Z', "
            "'codex-window', 60, 42, '2026-07-18T01:00:00Z')"
        )
        conn.execute(
            "INSERT INTO codex_conversation_messages(id, conversation_key, source_root_key, source_path, "
            "line_offset, kind, record_family, content_digest, content_len) "
            "VALUES (1, 'conversation-a', 'root-a', '/codex/source.jsonl', 1, 'assistant', "
            "'response_item', 'digest', 0)"
        )
        conn.execute(
            "INSERT INTO codex_conversation_file_touches(message_id, conversation_key, source_path, file_path, tool) "
            "VALUES (1, 'conversation-a', '/codex/source.jsonl', 'edited.py', 'apply_patch')"
        )
        conn.execute(
            "INSERT INTO codex_conversation_rollups(conversation_key, source_root_key, item_count) "
            "VALUES ('conversation-a', 'root-a', 1)"
        )
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES ('codex_physical_mutation_seq', '7'), "
            "('codex_quota_projection_certificate', 'stale')"
        )
        conn.commit()
    finally:
        conn.close()
    return ns, db, core


def _handler(db):
    for migration in db._CACHE_MIGRATIONS:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"missing cache migration {MIGRATION}")


def _codex_counts(conn: sqlite3.Connection) -> tuple[int, ...]:
    tables = (
        "codex_session_entries",
        "codex_session_files",
        "codex_source_roots",
        "codex_conversation_threads",
        "codex_conversation_events",
        "codex_conversation_messages",
        "codex_conversation_file_touches",
        "codex_conversation_rollups",
    )
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables)


def _cache_meta(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM cache_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def test_cache_registry_026_is_contiguous_after_025(tmp_path, monkeypatch):
    _ns, db, _core = _seed_existing_cache(tmp_path, monkeypatch)
    names = [migration.name for migration in db._CACHE_MIGRATIONS]
    idx = names.index(MIGRATION)
    assert names[idx - 1:idx + 1] == [PREDECESSOR, MIGRATION]
    assert db._CACHE_MIGRATIONS[idx].seq == 26


def test_026_clears_only_codex_rederivable_state_and_advances_sequence(tmp_path, monkeypatch):
    _ns, db, core = _seed_existing_cache(tmp_path, monkeypatch)
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        before = int(_cache_meta(conn, "codex_physical_mutation_seq") or 0)
        _handler(db)(conn)
        assert _codex_counts(conn) == (0,) * 8
        assert conn.execute("SELECT COUNT(*) FROM quota_window_snapshots WHERE source='codex'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM quota_window_snapshots WHERE source='claude'").fetchone()[0] == 1
        assert int(_cache_meta(conn, "codex_physical_mutation_seq")) == before + 1
        assert _cache_meta(conn, "codex_quota_projection_certificate") is None

        dump = list(conn.iterdump())
        _handler(db)(conn)
        assert list(conn.iterdump()) == dump
    finally:
        conn.close()


def test_026_defers_before_dml_when_codex_flock_is_held(tmp_path, monkeypatch):
    _ns, db, core = _seed_existing_cache(tmp_path, monkeypatch)
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    lock_path = pathlib.Path(str(core.CACHE_DB_PATH) + ".codex.lock")
    with open(lock_path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        before = _codex_counts(conn)
        try:
            _handler(db)(conn)
        except db.MigrationGateNotMet as exc:
            assert "026" in str(exc)
        else:
            raise AssertionError("026 must defer while Codex sync owns the flock")
        assert _codex_counts(conn) == before
    conn.close()


def test_markerless_codex_only_cache_is_not_fresh(tmp_path, monkeypatch):
    _ns, db, core = _seed_existing_cache(tmp_path, monkeypatch)
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        conn.execute("DROP TABLE schema_migrations")
        conn.execute("DELETE FROM session_entries")
        conn.execute("DELETE FROM conversation_messages")
        conn.execute("DELETE FROM codex_conversation_events")
        conn.execute("DELETE FROM codex_conversation_threads")
        conn.execute("DELETE FROM codex_session_files")
        conn.execute("DELETE FROM codex_source_roots")
        conn.execute("DELETE FROM quota_window_snapshots")
        conn.execute("DELETE FROM codex_conversation_messages")
        conn.execute("DELETE FROM codex_conversation_file_touches")
        conn.execute("DELETE FROM codex_conversation_rollups")
        conn.execute("DELETE FROM codex_session_entries")
        conn.execute(
            "INSERT INTO codex_session_entries(source_path, line_offset, timestamp_utc, session_id, model, "
            "total_tokens, source_root_key, conversation_key) VALUES "
            "('/codex/legacy.jsonl', 1, '2026-07-18T00:00:00Z', 'native', 'gpt-test', 1, 'root-a', NULL)"
        )
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        called = []
        db._run_pending_migrations(
            conn,
            registry=[db.Migration(26, MIGRATION, lambda c: called.append(True))],
            db_label="cache.db",
        )
        assert called == [True]
    finally:
        conn.close()


def test_genuinely_empty_markerless_cache_stamps_without_calling_026():
    db = _db_module()
    conn = sqlite3.connect(":memory:")
    try:
        db._apply_cache_schema(conn)
        called = []
        db._run_pending_migrations(
            conn,
            registry=[db.Migration(26, MIGRATION, lambda c: called.append(True))],
            db_label="cache.db",
        )
        assert called == []
    finally:
        conn.close()


def test_026_per_migration_goldens_pin_codex_clear():
    assert PRE_DB.exists() and POST_DB.exists()
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert pre.execute("SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)).fetchone()[0] == 0
        assert _codex_counts(pre) == (1,) * 8
        assert post.execute("SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)).fetchone()[0] == 1
        assert _codex_counts(post) == (0,) * 8
        assert int(_cache_meta(post, "codex_physical_mutation_seq")) == 8
        assert _cache_meta(post, "codex_quota_projection_certificate") is None
    finally:
        pre.close()
        post.close()


def test_026_handler_matches_committed_post_golden(tmp_path):
    assert PRE_DB.exists() and POST_DB.exists()
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    db = _db_module()
    conn = sqlite3.connect(work)
    try:
        _handler(db)(conn)
        db._stamp_applied(conn, MIGRATION, "2026-07-18T12:00:00Z")
    finally:
        conn.close()

    def dump(path: pathlib.Path) -> list[str]:
        fixture = sqlite3.connect(path)
        try:
            return list(fixture.iterdump())
        finally:
            fixture.close()

    assert dump(work) == dump(POST_DB)
