"""Per-migration goldens for cache migration 034's window-scoped spend adoption.

Spec: ``docs/superpowers/specs/2026-07-30-codex-window-scoped-spend-adoption.md``
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "034_codex_window_spend_adoption"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "034_codex_window_spend_adoption"

ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32

# (source_path, line_offset) -> account_key
PRE_ACCOUNTS = {
    ("/roots/rk/sessions/a.jsonl", 10): None,
    ("/roots/rk/sessions/a.jsonl", 20): "",
    ("/roots/rk/sessions/a.jsonl", 30): None,
    ("/roots/rk/sessions/a.jsonl", 40): ACCOUNT_B,
    ("/roots/rk2/sessions/c.jsonl", 10): None,
}
POST_ACCOUNTS = {
    # In range, unattributed, single-account window: adopted.
    ("/roots/rk/sessions/a.jsonl", 10): ACCOUNT_A,
    ("/roots/rk/sessions/a.jsonl", 20): ACCOUNT_A,
    # Before the nominal window opened: untouched.
    ("/roots/rk/sessions/a.jsonl", 30): None,
    # Already identified: never re-stamped.
    ("/roots/rk/sessions/a.jsonl", 40): ACCOUNT_B,
    # Two identified accounts name that window: never guessed.
    ("/roots/rk2/sessions/c.jsonl", 10): None,
}


@pytest.fixture(scope="module")
def cctally_module():
    from importlib.machinery import SourceFileLoader

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _handler(cctally_module):
    for migration in cctally_module._CACHE_MIGRATIONS:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"{MIGRATION} not registered")


def _accounts(conn):
    return {
        (str(row[0]), int(row[1])): row[2]
        for row in conn.execute(
            "SELECT source_path, line_offset, account_key "
            "FROM codex_session_entries")
    }


def test_pre_fixture_reproduces_the_unattributed_spend(cctally_module):
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 33
        assert _accounts(conn) == PRE_ACCOUNTS
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_adopts_only_the_rows_the_window_names(cctally_module):
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 34
        assert _accounts(conn) == POST_ACCOUNTS
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_raw_quota_snapshots_are_never_written_back(cctally_module):
    """``quota_window_snapshots`` stays the PRE-fold raw cache."""
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        query = (
            "SELECT source_path, line_offset, account_key, resets_at_utc, "
            "       canonical_resets_at_utc "
            "  FROM quota_window_snapshots ORDER BY source_path, line_offset"
        )
        assert list(post.execute(query)) == list(pre.execute(query))
    finally:
        pre.close()
        post.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        first = _accounts(conn)
        assert first == POST_ACCOUNTS
        handler(conn)
        assert _accounts(conn) == first
    finally:
        conn.close()
