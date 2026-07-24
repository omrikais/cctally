"""Regression coverage for cache migration 027 (fork-preamble accounting)."""
from __future__ import annotations

import fcntl
import pathlib
import shutil
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths


MIGRATION = "027_codex_fork_preamble_rebuild"
PREDECESSOR = "026_codex_conversation_key_backfill"
FIXTURE_DIR = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
IDEMPOTENCY_COVERED = True


def _seed_existing_cache(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core as core

    db = sys.modules["_cctally_db"]
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        db._apply_cache_schema(conn)
        db.add_column_if_missing(
            conn, "codex_session_files", "last_total_tokens", "INTEGER"
        )
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
        )
        for migration in db._CACHE_MIGRATIONS:
            if migration.seq < 27:
                conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at_utc) VALUES (?, ?)",
                    (migration.name, "2026-07-19T00:00:00Z"),
                )
        conn.execute("PRAGMA user_version = 26")
        conn.execute(
            "INSERT INTO codex_source_roots(source_root_key, canonical_root_path, "
            "first_seen_utc, last_seen_utc) VALUES "
            "('root-a', '/codex', '2026-07-19T00:00:00Z', '2026-07-19T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO codex_session_entries(source_path, line_offset, timestamp_utc, "
            "session_id, model, total_tokens, source_root_key) VALUES "
            "('/codex/fork.jsonl', 10, '2026-07-14T06:24:01Z', "
            "'child', 'unknown', 110, 'root-a')"
        )
        conn.execute(
            "INSERT INTO codex_session_files(path, size_bytes, mtime_ns, last_byte_offset, "
            "last_ingested_at, last_session_id, last_model, last_total_tokens, source_root_key) "
            "VALUES ('/codex/fork.jsonl', 100, 1, 100, '2026-07-19T00:00:00Z', "
            "'child', 'gpt-5.6-sol', 10023, 'root-a')"
        )
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('codex_physical_mutation_seq', '7'), "
            "('codex_quota_projection_certificate', 'stale')"
        )
        conn.commit()
    finally:
        conn.close()
    return db, core


def _handler(db):
    for migration in db._CACHE_MIGRATIONS:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"missing cache migration {MIGRATION}")


def _meta(conn, key):
    row = conn.execute("SELECT value FROM cache_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def test_cache_registry_027_is_contiguous_after_026(tmp_path, monkeypatch):
    db, _core = _seed_existing_cache(tmp_path, monkeypatch)
    names = [migration.name for migration in db._CACHE_MIGRATIONS]
    idx = names.index(MIGRATION)
    assert names[idx - 1:idx + 1] == [PREDECESSOR, MIGRATION]
    assert db._CACHE_MIGRATIONS[idx].seq == 27


def test_027_clears_rederivable_codex_state_and_is_idempotent(tmp_path, monkeypatch):
    db, core = _seed_existing_cache(tmp_path, monkeypatch)
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        _handler(db)(conn)
        assert conn.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM codex_session_files").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM codex_source_roots").fetchone()[0] == 0
        assert _meta(conn, "codex_physical_mutation_seq") == "8"
        assert _meta(conn, "codex_quota_projection_certificate") is None

        dump = list(conn.iterdump())
        _handler(db)(conn)
        assert list(conn.iterdump()) == dump
    finally:
        conn.close()


def test_027_defers_before_dml_when_codex_flock_is_held(tmp_path, monkeypatch):
    db, core = _seed_existing_cache(tmp_path, monkeypatch)
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    lock_path = pathlib.Path(str(core.CACHE_DB_PATH) + ".codex.lock")
    with open(lock_path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            _handler(db)(conn)
        except db.MigrationGateNotMet as exc:
            assert "027" in str(exc)
        else:
            raise AssertionError("027 must defer while Codex sync owns the flock")
        assert conn.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 1
    conn.close()


def test_027_defers_before_dml_when_global_writer_flock_is_held(
    tmp_path, monkeypatch
):
    db, core = _seed_existing_cache(tmp_path, monkeypatch)
    lock_path = pathlib.Path(str(core.CACHE_DB_PATH) + ".lock")
    with open(lock_path, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(db.MigrationGateNotMet, match="writer busy"):
            db._eagerly_apply_cache_migrations()
        conn = sqlite3.connect(core.CACHE_DB_PATH)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM codex_session_entries"
            ).fetchone()[0] == 1
        finally:
            conn.close()


def test_027_per_migration_goldens_pin_codex_replay_arm():
    assert PRE_DB.exists() and POST_DB.exists()
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert pre.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)
        ).fetchone()[0] == 0
        assert pre.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 1
        assert post.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)
        ).fetchone()[0] == 1
        assert post.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 0
        assert post.execute("SELECT COUNT(*) FROM codex_session_files").fetchone()[0] == 0
        assert _meta(post, "codex_physical_mutation_seq") == "8"
        assert _meta(post, "codex_quota_projection_certificate") is None
    finally:
        pre.close()
        post.close()


def test_027_handler_matches_committed_post_golden(tmp_path):
    assert PRE_DB.exists() and POST_DB.exists()
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    load_script()
    db = sys.modules["_cctally_db"]
    conn = sqlite3.connect(work)
    try:
        _handler(db)(conn)
        db._stamp_applied(conn, MIGRATION, "2026-07-19T12:00:00Z")
    finally:
        conn.close()

    def dump(path):
        fixture = sqlite3.connect(path)
        try:
            return list(fixture.iterdump())
        finally:
            fixture.close()

    assert dump(work) == dump(POST_DB)
