"""Stage-B regression coverage for cache migration 024 (#294 S1).

The migration has one job: invalidate old Codex-only cache state so the fused
walker can truthfully rederive it from rollouts. It must never manufacture S1
metadata or disturb Claude-derived state, and its Codex flock must defer before
SQLite mutation when a Codex sync is active.
"""
from __future__ import annotations

import fcntl
import pathlib
import shutil
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths


MIGRATION = "024_codex_fused_ingest_rebuild"
PREDECESSOR = "023_conversation_sessions_enrichment_columns"
BEFORE_PREDECESSOR = "022_index_conversation_messages_model"
FIXTURE_DIR = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

# Required by tests/test_migration_registry_completeness.py. The explicit
# rerun assertion below drives the handler's post-commit/pre-stamp contract.
IDEMPOTENCY_COVERED = True


def _db_module():
    return sys.modules["_cctally_db"]


def _seed_existing_cache(tmp_path, monkeypatch):
    """Create a populated cache at the shipped 023 head.

    The old Codex rows deliberately omit every S1 linkage value. The migration
    must clear rather than guess those values; the Claude rows and the Claude
    quota observation are sentinels for the narrow destructive scope.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core as core

    db = _db_module()
    cache_path = core.CACHE_DB_PATH
    conn = sqlite3.connect(cache_path)
    try:
        db._apply_cache_schema(conn)
        # This additive column intentionally remains outside _apply_cache_schema
        # because its first addition purges stale Codex accounting. Seed a real
        # post-addition 023 DB so testing 024 cannot accidentally pass because
        # open_cache_db performed that older purge first.
        db.add_column_if_missing(
            conn, "codex_session_files", "last_total_tokens", "INTEGER"
        )
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
        )
        for migration in db._CACHE_MIGRATIONS:
            if migration.seq < 24:
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at_utc) VALUES (?, ?)",
                    (migration.name, "2026-07-15T00:00:00Z"),
                )
        conn.execute("PRAGMA user_version = 23")

        conn.execute(
            """INSERT INTO session_entries
               (source_path, line_offset, timestamp_utc, model, output_tokens)
               VALUES ('/claude/source.jsonl', 1, '2026-07-15T00:00:00Z',
                       'claude-test', 7)"""
        )
        conn.execute(
            """INSERT INTO conversation_messages
               (session_id, source_path, byte_offset, entry_type, text)
               VALUES ('claude-session', '/claude/source.jsonl', 1, 'assistant', 'keep me')"""
        )
        conn.execute(
            """INSERT INTO quota_window_snapshots
               (source, source_path, line_offset, captured_at_utc,
                logical_limit_key, window_minutes, used_percent, resets_at_utc)
               VALUES ('claude', '/claude/source.jsonl', 1, '2026-07-15T00:00:00Z',
                       'claude-window', 60, 10, '2026-07-15T01:00:00Z')"""
        )

        conn.execute(
            """INSERT INTO codex_session_entries
               (source_path, line_offset, timestamp_utc, session_id, model,
                input_tokens, cached_input_tokens, output_tokens,
                reasoning_output_tokens, total_tokens)
               VALUES ('/codex/source.jsonl', 2, '2026-07-15T00:00:00Z',
                       'legacy-session', 'gpt-test', 1, 2, 3, 4, 10)"""
        )
        conn.execute(
            """INSERT INTO codex_session_files
               (path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at,
                last_session_id, last_model, last_total_tokens)
               VALUES ('/codex/source.jsonl', 100, 1, 100, '2026-07-15T00:00:00Z',
                       'legacy-session', 'gpt-test', 10)"""
        )
        conn.execute(
            """INSERT INTO codex_source_roots
               (source_root_key, canonical_root_path, first_seen_utc, last_seen_utc)
               VALUES ('root-a', '/codex', '2026-07-15T00:00:00Z',
                       '2026-07-15T00:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO codex_conversation_threads
               (conversation_key, source_root_key, native_thread_id, root_thread_id,
                source_path, cwd, git_json)
               VALUES ('conversation-a', 'root-a', 'native-a', 'root-a',
                       '/codex/source.jsonl', '/project', '{"branch":"main"}')"""
        )
        conn.execute(
            """INSERT INTO quota_window_snapshots
               (source, source_root_key, source_path, line_offset, captured_at_utc,
                logical_limit_key, window_minutes, used_percent, resets_at_utc)
               VALUES ('codex', 'root-a', '/codex/source.jsonl', 2,
                       '2026-07-15T00:00:00Z', 'codex-window', 60, 42,
                       '2026-07-15T01:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO codex_conversation_events
               (source_path, line_offset, source_root_key, payload_json)
               VALUES ('/codex/source.jsonl', 2, 'root-a', '{"legacy":true}')"""
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


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    def _count_if_present(table: str) -> int:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is None:
            return 0
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    return {
        "claude_entries": conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0],
        "claude_messages": _count_if_present("conversation_messages"),
        "claude_quota": conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots WHERE source='claude'"
        ).fetchone()[0],
        "codex_entries": conn.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0],
        "codex_files": conn.execute("SELECT COUNT(*) FROM codex_session_files").fetchone()[0],
        "codex_roots": conn.execute("SELECT COUNT(*) FROM codex_source_roots").fetchone()[0],
        "codex_threads": conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_threads"
        ).fetchone()[0],
        "codex_quota": conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots WHERE source='codex'"
        ).fetchone()[0],
        "codex_events": _count_if_present("codex_conversation_events"),
    }


def _marker(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)
    ).fetchone()[0]


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _assert_codex_cleared_claude_unchanged(
    conn: sqlite3.Connection, *, legacy_claude_messages: bool = True,
) -> None:
    assert _counts(conn) == {
        "claude_entries": 1,
        "claude_messages": 1 if legacy_claude_messages else 0,
        "claude_quota": 1,
        "codex_entries": 0,
        "codex_files": 0,
        "codex_roots": 0,
        "codex_threads": 0,
        "codex_quota": 0,
        "codex_events": 0,
    }


def _assert_pre_migration_scope_unchanged(conn: sqlite3.Connection) -> None:
    """A held Codex flock must defer before changing *any* 024-owned row."""
    assert _counts(conn) == {
        "claude_entries": 1,
        "claude_messages": 1,
        "claude_quota": 1,
        "codex_entries": 1,
        "codex_files": 1,
        "codex_roots": 1,
        "codex_threads": 1,
        "codex_quota": 1,
        "codex_events": 1,
    }


def test_cache_registry_024_is_contiguous_after_023(tmp_path, monkeypatch):
    _ns, db, _core = _seed_existing_cache(tmp_path, monkeypatch)
    # 024 is no longer last (S6 appended 025); locate it and assert its
    # immediate predecessors + seq.
    names = [migration.name for migration in db._CACHE_MIGRATIONS]
    idx = names.index(MIGRATION)
    assert names[idx - 2:idx + 1] == [BEFORE_PREDECESSOR, PREDECESSOR, MIGRATION]
    assert db._CACHE_MIGRATIONS[idx - 2].seq == 22
    assert db._CACHE_MIGRATIONS[idx - 1].seq == 23
    assert db._CACHE_MIGRATIONS[idx].seq == 24


def test_024_handler_clears_only_codex_and_is_idempotent_before_central_stamp(
    tmp_path, monkeypatch
):
    _ns, db, core = _seed_existing_cache(tmp_path, monkeypatch)
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        handler = _handler(db)
        handler(conn)
        _assert_codex_cleared_claude_unchanged(conn)
        assert _marker(conn) == 0, "handler must never self-stamp"
        assert _version(conn) == 23, "only the dispatcher advances user_version"

        before = list(conn.iterdump())
        handler(conn)
        assert list(conn.iterdump()) == before, (
            "the retry after a handler/data commit but before central stamping "
            "must be byte-idempotent"
        )
        _assert_codex_cleared_claude_unchanged(conn)
    finally:
        conn.close()


def test_024_crash_after_handler_before_stamp_retries_safely(tmp_path, monkeypatch):
    ns, db, core = _seed_existing_cache(tmp_path, monkeypatch)
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        _handler(db)(conn)  # crash boundary: handler committed, dispatcher did not stamp
        assert _marker(conn) == 0
        assert _version(conn) == 23
    finally:
        conn.close()

    conn = ns["open_cache_db"]()
    try:
        _assert_codex_cleared_claude_unchanged(
            conn, legacy_claude_messages=False
        )
        assert _marker(conn) == 1
        # Fork-preamble accounting rebuild appended migration 027.
        assert _version(conn) == 28
    finally:
        conn.close()


def test_024_open_cache_db_defers_without_mutation_while_codex_lock_is_held(
    tmp_path, monkeypatch
):
    ns, _db, core = _seed_existing_cache(tmp_path, monkeypatch)
    expected_lock = pathlib.Path(str(core.CACHE_DB_PATH) + ".codex.lock")
    assert core.CACHE_LOCK_CODEX_PATH == expected_lock
    lock_fh = open(expected_lock, "w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)
    try:
        conn = ns["open_cache_db"]()
        try:
            _assert_pre_migration_scope_unchanged(conn)
            assert _marker(conn) == 0
            assert _version(conn) == 23
        finally:
            conn.close()
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()

    conn = ns["open_cache_db"]()
    try:
        _assert_codex_cleared_claude_unchanged(
            conn, legacy_claude_messages=False
        )
        assert _marker(conn) == 1
        # Fork-preamble accounting rebuild appended migration 027.
        assert _version(conn) == 28
    finally:
        conn.close()


def test_024_eager_dispatch_defers_without_mutation_while_codex_lock_is_held(
    tmp_path, monkeypatch
):
    _ns, db, core = _seed_existing_cache(tmp_path, monkeypatch)
    expected_lock = pathlib.Path(str(core.CACHE_DB_PATH) + ".codex.lock")
    lock_fh = open(expected_lock, "w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)
    try:
        db._eagerly_apply_cache_migrations()
        conn = sqlite3.connect(core.CACHE_DB_PATH)
        try:
            _assert_pre_migration_scope_unchanged(conn)
            assert _marker(conn) == 0
            assert _version(conn) == 23
        finally:
            conn.close()
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()

    db._eagerly_apply_cache_migrations()
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        _assert_codex_cleared_claude_unchanged(
            conn, legacy_claude_messages=False
        )
        assert _marker(conn) == 1
        # Fork-preamble accounting rebuild appended migration 027.
        assert _version(conn) == 28
    finally:
        conn.close()


def test_024_handler_invalidates_stored_quota_projection_certificate(
    tmp_path, monkeypatch
):
    """F3: 024 clears Codex quota state, so a stale-valid certificate that would
    let the reconcile short-circuit must be deleted in the same transaction."""
    _ns, db, core = _seed_existing_cache(tmp_path, monkeypatch)
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('codex_quota_projection_certificate', ?)",
            ('{"sequence":7,"signatures":{"root-a":"' + "a" * 64 + '"}}',),
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM cache_meta "
            "WHERE key='codex_quota_projection_certificate'"
        ).fetchone()[0] == 1

        _handler(db)(conn)

        assert conn.execute(
            "SELECT COUNT(*) FROM cache_meta "
            "WHERE key='codex_quota_projection_certificate'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_024_per_migration_goldens_pin_the_existing_cache_clear():
    """The pre/post artifacts carry an actual populated Codex-only reset.

    This is deliberately more than a marker check: Claude and Codex sentinels
    make a too-broad DELETE or a no-op handler observable in the committed
    migration fixture.
    """
    assert PRE_DB.exists(), f"missing 024 pre fixture: {PRE_DB}"
    assert POST_DB.exists(), f"missing 024 post fixture: {POST_DB}"
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert pre.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (PREDECESSOR,)
        ).fetchone()[0] == 1
        assert pre.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)
        ).fetchone()[0] == 0
        assert _counts(pre)["codex_entries"] == 1
        assert _counts(pre)["claude_entries"] == 1

        assert post.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)
        ).fetchone()[0] == 1
        _assert_codex_cleared_claude_unchanged(post)
    finally:
        pre.close()
        post.close()


def test_024_handler_matches_the_committed_post_golden(tmp_path):
    """Run the production handler on pre.sqlite and reproduce central stamping."""
    assert PRE_DB.exists(), f"missing 024 pre fixture: {PRE_DB}"
    assert POST_DB.exists(), f"missing 024 post fixture: {POST_DB}"
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    db = _db_module()
    conn = sqlite3.connect(work)
    try:
        _handler(db)(conn)
        db._stamp_applied(conn, MIGRATION, "2026-07-15T12:00:00Z")
    finally:
        conn.close()

    def dump(path: pathlib.Path) -> list[str]:
        fixture = sqlite3.connect(path)
        try:
            return list(fixture.iterdump())
        finally:
            fixture.close()

    assert dump(work) == dump(POST_DB)
