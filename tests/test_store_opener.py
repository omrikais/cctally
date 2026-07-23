"""Task 2 — unified store opener with version-gated schema apply.

Spec docs/superpowers/specs/2026-07-22-db-journal-redesign-design.md §6.1
(one opener, one policy) + §6.2 (version-gated schema apply).

The behavior change under test: for cache.db (and conversations.db once it
joins the framework in Task 10), the full schema executescript +
``add_column_if_missing`` probes + FTS branch run ONLY when the store's
stamped ``user_version`` differs from the migration-registry head. The
steady-state open becomes connect → PRAGMAs → one ``user_version`` read →
done, with zero DDL.

Isolation goes through the canonical ``load_script`` + ``redirect_paths``
helpers (never the real prod data dir). NOTE ``load_script()`` DROPS every
cached ``_cctally_*`` sibling from ``sys.modules`` (except ``_cctally_core``)
and bin/cctally reloads FRESH ones, so a top-level ``import _cctally_store``
would be a STALE object the reloaded ``open_cache_db`` never uses. Every test
therefore grabs the fresh sibling modules from ``sys.modules`` AFTER
``load_script()`` and monkeypatches those.
"""
import sqlite3
import sys

import pytest

import _cctally_core  # preserved across load_script(), safe at module top
from conftest import load_script, redirect_paths


# A DDL statement is a CREATE, an ALTER, or a `PRAGMA table_info(...)` probe
# (the three things add_column_if_missing / _apply_*_schema emit and the
# version gate must suppress). Policy PRAGMAs (journal_mode, synchronous, …),
# `PRAGMA user_version`, and plain SELECTs are NOT DDL.
def _is_ddl(sql: str) -> bool:
    s = sql.lstrip().upper()
    return s.startswith("CREATE") or s.startswith("ALTER") or s.startswith(
        "PRAGMA TABLE_INFO"
    )


class _DdlCounter:
    def __init__(self):
        self.count = 0
        self.statements = []

    def __call__(self, sql):
        if _is_ddl(sql):
            self.count += 1
            self.statements.append(sql.strip().splitlines()[0])


def _fresh_siblings():
    """The reloaded store/db modules that the current bin/cctally uses."""
    return sys.modules["_cctally_store"], sys.modules["_cctally_db"]


# --------------------------------------------------------------------------
# (a) first open on a fresh dir creates the full schema
# --------------------------------------------------------------------------

def test_first_open_cache_creates_full_schema(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _store, _db = _fresh_siblings()
    conn = ns["open_cache_db"]()
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # Representative core tables from _apply_cache_schema.
        assert "session_entries" in names
        assert "session_files" in names
        assert "codex_session_entries" in names
        # Registry head stamped so the next open gates out.
        uv = conn.execute("PRAGMA user_version").fetchone()[0]
        assert uv == len(_db._CACHE_MIGRATIONS)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# (b) second open executes ZERO DDL (version gate)
# --------------------------------------------------------------------------

def test_second_open_cache_executes_zero_ddl(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _store, _db = _fresh_siblings()

    # Positive control (non-vacuity): the FIRST open — a fresh DB below the
    # registry head — MUST run schema DDL through the trace hook. If this is
    # zero the hook never fires and the negative assertion below is vacuous.
    first = _DdlCounter()
    monkeypatch.setattr(_store, "_TRACE_HOOK", first)
    ns["open_cache_db"]().close()
    assert first.count > 0, "trace hook never observed the first-open schema DDL"

    # Negative: the steady-state open (user_version == registry head) runs ZERO
    # DDL — the whole schema executescript + add_column probes are gated out.
    second = _DdlCounter()
    monkeypatch.setattr(_store, "_TRACE_HOOK", second)
    conn = ns["open_cache_db"]()
    try:
        assert second.count == 0, (
            f"steady-state open ran DDL: {second.statements}"
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------
# (c) bumping the registry head re-triggers apply exactly once
# --------------------------------------------------------------------------

def test_registry_bump_retriggers_apply_once(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _store, _db = _fresh_siblings()
    ns["open_cache_db"]().close()  # schema created, user_version == head

    # Bump the registry head with a no-op migration. Append IN PLACE to the one
    # shared list object: _cctally_cache._CACHE_MIGRATIONS is bound to the very
    # same list as _cctally_db._CACHE_MIGRATIONS, so both the version gate
    # (schema_current) and the dispatcher registry see the +1 head. Manual
    # pop() in finally restores it (monkeypatch can't undo an in-place append).
    reg = _db._CACHE_MIGRATIONS
    fake = _db.Migration(
        seq=len(reg) + 1,
        name=f"{len(reg) + 1:03d}_store_test_noop",
        handler=lambda conn: None,
    )
    reg.append(fake)
    try:
        # First open after the bump: schema_current() is now false → the schema
        # apply re-triggers (DDL > 0) AND the dispatcher runs+stamps the fake,
        # advancing user_version to the new head.
        counter = _DdlCounter()
        monkeypatch.setattr(_store, "_TRACE_HOOK", counter)
        ns["open_cache_db"]().close()
        assert counter.count > 0, "registry bump did not re-trigger schema apply"

        # Next open: user_version now equals the bumped head → gated out again.
        counter2 = _DdlCounter()
        monkeypatch.setattr(_store, "_TRACE_HOOK", counter2)
        conn = ns["open_cache_db"]()
        try:
            assert counter2.count == 0, (
                f"apply re-ran after the bump was absorbed: {counter2.statements}"
            )
            assert conn.execute("PRAGMA user_version").fetchone()[0] == len(reg)
        finally:
            conn.close()
    finally:
        reg.pop()


# --------------------------------------------------------------------------
# (d) PRAGMA values per store match the §6.1 policy table
# --------------------------------------------------------------------------

def _pragma(conn, name):
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_pragma_policy_cache(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_cache_db"]()
    try:
        assert _pragma(conn, "journal_mode") == "wal"
        assert _pragma(conn, "synchronous") == 1  # NORMAL
        assert _pragma(conn, "busy_timeout") == 15000
        assert _pragma(conn, "journal_size_limit") == 128 * 1024 * 1024
        assert _pragma(conn, "auto_vacuum") == 2  # INCREMENTAL
    finally:
        conn.close()


def test_pragma_policy_stats(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    try:
        assert _pragma(conn, "journal_mode") == "wal"
        assert _pragma(conn, "synchronous") == 1  # NORMAL
        assert _pragma(conn, "busy_timeout") == 15000
        assert _pragma(conn, "journal_size_limit") == 16 * 1024 * 1024
        # stats auto_vacuum stays NONE on a normal open (§6.1: INCREMENTAL only
        # from the first epoch rebuild, never at in-place cutover).
        assert _pragma(conn, "auto_vacuum") == 0
    finally:
        conn.close()


def test_pragma_policy_conversations(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        assert _pragma(conn, "journal_mode") == "wal"
        assert _pragma(conn, "synchronous") == 1
        assert _pragma(conn, "busy_timeout") == 15000
        assert _pragma(conn, "journal_size_limit") == 128 * 1024 * 1024
        assert _pragma(conn, "auto_vacuum") == 2
    finally:
        conn.close()


# --------------------------------------------------------------------------
# (e) conversations still attaches cache.db read-only
# --------------------------------------------------------------------------

def test_conversations_attaches_cache_ro(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_conversations_db"](attach_cache=True)
    try:
        dbs = {r[1] for r in conn.execute("PRAGMA database_list").fetchall()}
        assert "cache_db" in dbs
        # The attach is read-only: a write against cache_db must be refused.
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE cache_db.should_fail (x INTEGER)")
    finally:
        conn.close()
