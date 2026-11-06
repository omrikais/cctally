"""Regression coverage for cache migration 027 (fork-preamble accounting, #983).

027 originally cleared every re-derivable Codex family out of band so the
corrected fused parser would replay the rollouts and drop the copied
fork-preamble ``model='unknown'`` rows. Since #983 the handler only arms
``CODEX_REPLAY_FROM_ZERO_KEY``; the guarded replay inside ``sync_codex_cache``
performs the repair, so a decoder-less upgrade keeps its retained history
(recoverable) instead of losing it.
"""
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

REPLAY_MARKER = "codex_replay_from_zero_pending"
#: cache_meta rows the handler must not touch (the pre-#983 handler deleted or
#: bumped each of them).
CODEX_META = (
    ("codex_physical_mutation_seq", "7"),
    ("codex_quota_projection_certificate", "stale"),
    ("codex_journal_coverage_certificate", '{"sequence":7}'),
    ("codex_journal_coverage_progress", '{"cursor":1}'),
)


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
        conn.executemany(
            "INSERT INTO cache_meta(key, value) VALUES (?, ?)", CODEX_META
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


def _counts(conn):
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("codex_session_entries", "codex_session_files",
                      "codex_source_roots"))


def test_the_marker_name_matches_the_constant_the_sync_consumes(tmp_path, monkeypatch):
    """A drifted literal would arm a marker nothing reads."""
    _seed_existing_cache(tmp_path, monkeypatch)
    assert sys.modules["_cctally_cache"].CODEX_REPLAY_FROM_ZERO_KEY == REPLAY_MARKER


def test_027_only_arms_the_replay_and_is_idempotent(tmp_path, monkeypatch):
    """#983: the out-of-band clear destroyed retained history on a
    decoder-less upgrade. The copied fork-preamble row stays until the guarded
    replay re-derives it; only the replay marker changes here."""
    db, core = _seed_existing_cache(tmp_path, monkeypatch)
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    try:
        before = list(conn.iterdump())
        assert _meta(conn, REPLAY_MARKER) is None
        _handler(db)(conn)
        assert _meta(conn, REPLAY_MARKER) == "1"
        assert [line for line in conn.iterdump()
                if REPLAY_MARKER not in line] == before
        assert _counts(conn) == (1, 1, 1)
        for key, value in CODEX_META:
            assert _meta(conn, key) == value, key
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)
        ).fetchone()[0] == 0, "handler must never self-stamp"

        dump = list(conn.iterdump())
        _handler(db)(conn)
        assert list(conn.iterdump()) == dump
        db._stamp_applied(conn, MIGRATION, "2026-07-19T12:00:00Z")
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_027_defers_before_dml_when_global_writer_flock_is_held(
    tmp_path, monkeypatch
):
    """The dispatcher's own writer exclusion (not the handler) defers 027."""
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
            assert _meta(conn, REPLAY_MARKER) is None
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
        assert _meta(pre, REPLAY_MARKER) is None
        assert post.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", (MIGRATION,)
        ).fetchone()[0] == 1
        assert _counts(post) == _counts(pre)
        assert _meta(post, REPLAY_MARKER) == "1"
        for key in ("codex_physical_mutation_seq", "codex_quota_projection_certificate"):
            assert _meta(post, key) == _meta(pre, key), key
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
