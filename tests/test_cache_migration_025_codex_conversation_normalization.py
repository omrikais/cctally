"""Coverage for cache migration 025 (#294 S6) — Codex conversation normalization.

The migration derives S6 normalized conversation state for caches whose Codex
events were ingested before S6: it full-clears the derived tables and replays the
pure normalization kernel over stored ``codex_conversation_events``. It must never
touch the event log or Claude-derived state, its Codex flock must defer before
mutation when a Codex sync is active, and a re-run (crash before the central
stamp) must be byte-idempotent including the FTS shadow tables.
"""
from __future__ import annotations

import fcntl
import pathlib
import shutil
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths


MIGRATION = "025_codex_conversation_normalization"
PREDECESSOR = "024_codex_fused_ingest_rebuild"
FIXTURE_DIR = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

# Required by tests/test_migration_registry_completeness.py.
IDEMPOTENCY_COVERED = True

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1"

_SRC = "/codex/golden.jsonl"
_ROOT = "root-golden"
_CK = "conv-golden"
_GIT = '{"branch":"main","repository":"golden-repo"}'


def _db_module():
    return sys.modules["_cctally_db"]


def _rec(payload: dict, rtype: str, timestamp: str) -> str:
    import json
    return json.dumps({"payload": payload, "type": rtype, "timestamp": timestamp},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_EVENTS = [
    (1, "session_meta", None, None, None, "2026-07-14T12:00:00Z",
     _rec({"id": "native-golden", "session_id": "native-golden"},
          "session_meta", "2026-07-14T12:00:00Z")),
    (2, "turn_context", None, "turn-g", None, "2026-07-14T12:01:00Z",
     _rec({"turn_id": "turn-g", "model": "gpt-golden"},
          "turn_context", "2026-07-14T12:01:00Z")),
    (3, "response_item", "message", None, None, "2026-07-14T12:02:00Z",
     _rec({"type": "message", "role": "user",
           "content": [{"type": "input_text", "text": "golden prompt"}]},
          "response_item", "2026-07-14T12:02:00Z")),
    (4, "response_item", "message", None, None, "2026-07-14T12:03:00Z",
     _rec({"type": "message", "role": "assistant",
           "content": [{"type": "output_text", "text": "golden reply"}]},
          "response_item", "2026-07-14T12:03:00Z")),
    (5, "event_msg", "patch_apply_end", "turn-g", "patch-1", "2026-07-14T12:04:00Z",
     _rec({"type": "patch_apply_end", "changes": [{"path": "golden.txt"}]},
          "event_msg", "2026-07-14T12:04:00Z")),
]


def _seed_existing_cache(tmp_path, monkeypatch):
    """A populated post-S1 cache at the 024 head: a stored Codex conversation
    (events + thread) with NO normalized rows, plus a Claude sentinel."""
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
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)")
        for migration in db._CACHE_MIGRATIONS:
            if migration.seq < 25:
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at_utc) VALUES (?, ?)",
                    (migration.name, "2026-07-15T00:00:00Z"))
        conn.execute("PRAGMA user_version = 24")
        conn.execute(
            "INSERT INTO conversation_messages "
            "(session_id, source_path, byte_offset, entry_type, text) "
            "VALUES ('claude-session', '/claude/source.jsonl', 1, 'assistant', 'keep me')")
        conn.execute(
            "INSERT INTO codex_source_roots "
            "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
            "VALUES (?, '/codex', '2026-07-14T12:00:00Z', '2026-07-14T12:00:00Z')", (_ROOT,))
        conn.execute(
            "INSERT INTO codex_conversation_threads "
            "(conversation_key, source_root_key, native_thread_id, root_thread_id, "
            " parent_thread_id, source_path, cwd, git_json) VALUES (?,?,?,?,?,?,?,?)",
            (_CK, _ROOT, "native-golden", "native-golden", None, _SRC, None, _GIT))
        for offset, rtype, etype, turn_id, call_id, ts, payload_json in _EVENTS:
            conn.execute(
                "INSERT INTO codex_conversation_events "
                "(source_path, line_offset, source_root_key, conversation_key, "
                " native_thread_id, root_thread_id, parent_thread_id, timestamp_utc, "
                " record_type, event_type, turn_id, call_id, payload_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_SRC, offset, _ROOT, _CK, "native-golden", "native-golden", None,
                 ts, rtype, etype, turn_id, call_id, payload_json))
        conn.commit()
    finally:
        conn.close()
    return ns, db, core


def _handler(db):
    for migration in db._CACHE_MIGRATIONS:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"missing cache migration {MIGRATION}")


def _marker(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)).fetchone()[0]


def _version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _norm_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0]


def _assert_replayed(conn) -> None:
    """The 3 addressable records (2 prose + 1 event card) normalized; the event
    log + Claude sentinel are untouched; the rollup is derived."""
    assert _norm_count(conn) == 3
    kinds = {r[0] for r in conn.execute("SELECT kind FROM codex_conversation_messages")}
    assert kinds == {"user", "assistant", "event"}
    assert conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_events").fetchone()[0] == len(_EVENTS)
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE text='keep me'").fetchone()[0] == 1
    rollup = conn.execute(
        "SELECT item_count, title, project_key FROM codex_conversation_rollups "
        "WHERE conversation_key = ?", (_CK,)).fetchone()
    assert rollup is not None
    assert rollup[0] == 3            # prompt + response + event
    assert rollup[1] == "golden prompt"
    assert rollup[2] and rollup[2].startswith("project:")
    assert conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_file_touches "
        "WHERE file_path='golden.txt'").fetchone()[0] == 1


def test_cache_registry_025_is_contiguous_after_024(tmp_path, monkeypatch):
    _ns, db, _core = _seed_existing_cache(tmp_path, monkeypatch)
    names = [m.name for m in db._CACHE_MIGRATIONS]
    idx = names.index(MIGRATION)
    assert names[idx - 1:idx + 1] == [PREDECESSOR, MIGRATION]
    assert db._CACHE_MIGRATIONS[idx].seq == 25


def test_025_handler_replays_derived_only_and_is_idempotent_before_stamp(tmp_path, monkeypatch):
    _ns, db, core = _seed_existing_cache(tmp_path, monkeypatch)
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        _handler(db)(conn)
        _assert_replayed(conn)
        assert _marker(conn) == 0, "handler must never self-stamp"
        assert _version(conn) == 24

        before = list(conn.iterdump())
        _handler(db)(conn)  # crash-before-stamp re-run
        assert list(conn.iterdump()) == before, (
            "re-run must be byte-idempotent INCLUDING FTS shadow tables")
        _assert_replayed(conn)
    finally:
        conn.close()


def test_025_crash_after_handler_before_stamp_retries_safely(tmp_path, monkeypatch):
    ns, db, core = _seed_existing_cache(tmp_path, monkeypatch)
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        _handler(db)(conn)  # committed, not stamped
        assert _marker(conn) == 0
        assert _version(conn) == 24
    finally:
        conn.close()
    conn = ns["open_cache_db"]()
    try:
        _assert_replayed(conn)
        assert _marker(conn) == 1
        assert _version(conn) == 25
    finally:
        conn.close()


def test_025_defers_without_mutation_while_codex_lock_is_held(tmp_path, monkeypatch):
    ns, _db, core = _seed_existing_cache(tmp_path, monkeypatch)
    lock_path = pathlib.Path(str(core.CACHE_DB_PATH) + ".codex.lock")
    lock_fh = open(lock_path, "w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)
    try:
        conn = ns["open_cache_db"]()
        try:
            # Pending state: normalized tables stay empty, version stays at 24.
            assert _norm_count(conn) == 0
            assert _marker(conn) == 0
            assert _version(conn) == 24
        finally:
            conn.close()
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
    conn = ns["open_cache_db"]()
    try:
        _assert_replayed(conn)
        assert _marker(conn) == 1
        assert _version(conn) == 25
    finally:
        conn.close()


def test_025_eager_dispatch_defers_without_mutation_while_codex_lock_is_held(tmp_path, monkeypatch):
    _ns, db, core = _seed_existing_cache(tmp_path, monkeypatch)
    lock_path = pathlib.Path(str(core.CACHE_DB_PATH) + ".codex.lock")
    lock_fh = open(lock_path, "w")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)
    try:
        db._eagerly_apply_cache_migrations()
        conn = sqlite3.connect(core.CACHE_DB_PATH)
        try:
            assert _norm_count(conn) == 0
            assert _marker(conn) == 0
            assert _version(conn) == 24
        finally:
            conn.close()
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
    db._eagerly_apply_cache_migrations()
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        _assert_replayed(conn)
        assert _marker(conn) == 1
        assert _version(conn) == 25
    finally:
        conn.close()


def _norm_content(conn) -> list:
    """Normalized row content modulo the ``id`` rowid alias, in physical order."""
    cols = ("conversation_key, source_root_key, source_path, line_offset, timestamp_utc, "
            "turn_id, call_id, kind, event_type, record_family, model, text, "
            "content_digest, content_len, detail_json, search_tool, search_thinking")
    return list(conn.execute(
        f"SELECT {cols} FROM codex_conversation_messages "
        "ORDER BY source_path, line_offset"))


def test_025_replay_matches_fresh_ingest_modulo_id(tmp_path, monkeypatch):
    """Replaying stored events reproduces the SAME normalized state (row content
    modulo id) as a fresh ingest."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "15" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "rollouts" / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        fresh = _norm_content(conn)
        assert fresh, "fresh ingest must produce normalized rows"
        # Simulate a pre-S6 cache: drop the derived state + unstamp 025.
        conn.execute("DELETE FROM codex_conversation_messages")
        conn.execute("DELETE FROM codex_conversation_file_touches")
        conn.execute("DELETE FROM codex_conversation_rollups")
        conn.execute("DELETE FROM schema_migrations WHERE name = ?", (MIGRATION,))
        conn.execute("PRAGMA user_version = 24")
        conn.commit()
    finally:
        conn.close()
    conn = ns["open_cache_db"]()  # dispatcher replays 025
    try:
        assert _marker(conn) == 1
        replayed = _norm_content(conn)
        assert replayed == fresh, "replay must match fresh ingest modulo id"
    finally:
        conn.close()


def test_025_per_migration_goldens_pin_the_replay(tmp_path):
    assert PRE_DB.exists() and POST_DB.exists()
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert pre.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)).fetchone()[0] == 0
        assert pre.execute("SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0] == 0
        assert post.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)).fetchone()[0] == 1
        _assert_replayed(post)
    finally:
        pre.close()
        post.close()


def test_025_handler_matches_the_committed_post_golden(tmp_path):
    assert PRE_DB.exists() and POST_DB.exists()
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    db = _db_module()
    conn = sqlite3.connect(work)
    try:
        _handler(db)(conn)
        db._stamp_applied(conn, MIGRATION, "2026-07-15T12:00:00Z")
    finally:
        conn.close()

    def dump(path) -> list:
        fixture = sqlite3.connect(path)
        try:
            return list(fixture.iterdump())
        finally:
            fixture.close()

    assert dump(work) == dump(POST_DB)
