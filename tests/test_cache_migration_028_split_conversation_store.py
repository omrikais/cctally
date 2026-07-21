"""Regression coverage for cache migration 028 (conversation-store split)."""
from __future__ import annotations

import pathlib
import shutil
import sqlite3
import sys

from conftest import load_script, redirect_paths


MIGRATION = "028_split_conversation_store"
PREDECESSOR = "027_codex_fork_preamble_rebuild"
FIXTURE_DIR = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
IDEMPOTENCY_COVERED = True


def _handler(db):
    return next(item.handler for item in db._CACHE_MIGRATIONS if item.name == MIGRATION)


def _dump(path):
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_cache_registry_028_is_contiguous_after_027():
    ns = load_script()
    names = [migration.name for migration in ns["_CACHE_MIGRATIONS"]]
    idx = names.index(MIGRATION)
    assert names[idx - 1:idx + 1] == [PREDECESSOR, MIGRATION]
    assert ns["_CACHE_MIGRATIONS"][idx].seq == 28


def test_028_preserves_core_tables_and_is_idempotent(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    db = sys.modules["_cctally_db"]
    core = sys.modules["_cctally_core"]
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        db._apply_cache_schema(conn)
        conn.execute(
            "INSERT INTO session_entries "
            "(source_path,line_offset,timestamp_utc,model) "
            "VALUES ('core.jsonl',0,'2026-07-20T00:00:00Z','model')"
        )
        conn.execute(
            "INSERT INTO conversation_messages "
            "(source_path,byte_offset,entry_type,text) "
            "VALUES ('prose.jsonl',0,'human','private prose')"
        )
        conn.execute(
            "INSERT INTO codex_conversation_events "
            "(source_path,line_offset,source_root_key,record_type,payload_json) "
            "VALUES ('codex.jsonl',1,'root','turn_context',"
            "'{\"payload\":{\"model\":\"gpt-5.3-codex-spark\"}}')"
        )
        conn.execute(
            "INSERT INTO quota_window_snapshots "
            "(source,source_root_key,source_path,line_offset,captured_at_utc,"
            "observed_slot,logical_limit_key,window_minutes,used_percent,resets_at_utc) "
            "VALUES ('codex','root','codex.jsonl',2,'2026-07-20T00:00:00Z',"
            "'secondary','limit',10080,12,'2026-07-27T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO codex_conversation_events "
            "(source_path,line_offset,source_root_key,record_type,payload_json) "
            "VALUES ('/before-context.jsonl',2,'root-before','turn_context',"
            "'{\"payload\":{\"model\":\"gpt-5.3-codex-spark\"}}')"
        )
        conn.execute(
            "INSERT INTO codex_session_files "
            "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at,"
            "last_model,source_root_key) VALUES "
            "('/before-context.jsonl',10,1,10,'2026-07-20T00:00:00Z',"
            "'gpt-5.3-codex-spark','root-before')"
        )
        conn.execute(
            "INSERT INTO quota_window_snapshots "
            "(source,source_root_key,source_path,line_offset,captured_at_utc,"
            "observed_slot,logical_limit_key,window_minutes,used_percent,resets_at_utc) "
            "VALUES ('codex','root-before','/before-context.jsonl',1,"
            "'2026-07-20T00:00:00Z','secondary','unscoped-before',10080,12,"
            "'2026-07-27T00:00:00Z')"
        )
        conn.commit()
        _handler(db)(conn)
        assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone() == (1,)
        assert conn.execute(
            "SELECT observed_model FROM quota_window_snapshots "
            "WHERE source_path='codex.jsonl'"
        ).fetchone() == ("gpt-5.3-codex-spark",)
        assert conn.execute(
            "SELECT observed_model FROM quota_window_snapshots "
            "WHERE source_path='/before-context.jsonl'"
        ).fetchone() == (None,)
        observations = ns["_cctally_quota"].load_codex_quota_observations(
            cache_conn=conn,
            source_root_keys={"root-before"},
        )
        before = next(
            item for item in observations
            if item.source_path == "/before-context.jsonl"
        )
        assert before.identity.logical_limit_key == "unscoped-before"
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='conversation_messages'"
        ).fetchone() is None
        first = list(conn.iterdump())
        _handler(db)(conn)
        assert list(conn.iterdump()) == first
    finally:
        conn.close()


def test_028_per_migration_goldens_pin_core_split():
    assert PRE_DB.exists() and POST_DB.exists()
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert pre.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone() == (0,)
        assert post.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone() == (1,)
        assert post.execute(
            "SELECT 1 FROM sqlite_master WHERE name='conversation_messages'"
        ).fetchone() is None
        assert post.execute(
            "SELECT 1 FROM sqlite_master WHERE name='session_entries'"
        ).fetchone() is not None
    finally:
        pre.close()
        post.close()


def test_028_handler_matches_committed_post_golden(tmp_path, monkeypatch):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "runtime")
    db = sys.modules["_cctally_db"]
    conn = sqlite3.connect(work)
    try:
        _handler(db)(conn)
        db._stamp_applied(conn, MIGRATION, "2026-07-20T12:00:00Z")
    finally:
        conn.close()
    assert _dump(work) == _dump(POST_DB)
