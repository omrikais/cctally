"""Per-migration goldens for cache migration ``029_backfill_claude_account``
(#341, spec §2 cache.db Claude backfill).

The ``account_key`` columns on ``session_entries`` / ``session_files`` /
``codex_session_entries`` are added by ``_apply_cache_schema`` (present in BOTH
pre and post). Migration 029 bumps the cache head (so an existing install
re-runs that schema apply) AND performs the one-time Claude backfill: legacy
Claude rows attribute to the cutover op's recorded account; Codex rows stay NULL
(Q2). It reads ONLY the journal cutover op (never auth). When the op is absent
but legacy Claude rows exist it DEFERs (``MigrationGateNotMet``); an op recording
the ``unattributed`` sentinel is a resolved no-op (leave NULL).
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

# W1 registry-completeness guard (#279 S7): this module exercises the handler's
# second-invocation idempotency.
IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "029_backfill_claude_account"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "029_backfill_claude_account"
# MUST match build-migrations-fixtures.py::_CACHE_029_CUTOVER_ACCOUNT.
_CUTOVER_ACCOUNT = "cafef00dcafef00dcafef00dcafef00d"


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
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def _set_journal_with_op(monkeypatch, tmp_path, account):
    """Point the journal at a tmp dir carrying a cutover op recording ``account``
    (None => no op appended, i.e. cutover has not run)."""
    core = sys.modules["_cctally_core"]
    journal = sys.modules["_cctally_journal"]
    jdir = tmp_path / "journal"
    jdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(core, "JOURNAL_DIR", jdir)
    monkeypatch.setattr(core, "JOURNAL_LOCK_PATH", jdir / "journal.lock")
    if account is not None:
        journal.append_accounts_cutover_op(account, at="2026-07-15T12:00:00Z")


def _accts(conn, table):
    return {r[0] for r in conn.execute(f"SELECT account_key FROM {table}")}


def test_pre_fixture_at_028_head_null_accounts(cctally_module):
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='028_split_conversation_store'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,)).fetchone()[0] == 0
        # account_key columns present (from _apply_cache_schema), all NULL.
        assert _accts(conn, "session_entries") == {None}
        assert _accts(conn, "session_files") == {None}
        assert _accts(conn, "codex_session_entries") == {None}
    finally:
        conn.close()


def test_post_fixture_backfilled(cctally_module):
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _accts(conn, "session_entries") == {_CUTOVER_ACCOUNT}
        assert _accts(conn, "session_files") == {_CUTOVER_ACCOUNT}
        # Codex rows stay NULL (Q2).
        assert _accts(conn, "codex_session_entries") == {None}
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_backfills_claude_only(cctally_module, tmp_path, monkeypatch):
    _set_journal_with_op(monkeypatch, tmp_path, _CUTOVER_ACCOUNT)
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _handler(cctally_module)(conn)
        assert _accts(conn, "session_entries") == {_CUTOVER_ACCOUNT}
        assert _accts(conn, "session_files") == {_CUTOVER_ACCOUNT}
        assert _accts(conn, "codex_session_entries") == {None}
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path, monkeypatch):
    _set_journal_with_op(monkeypatch, tmp_path, _CUTOVER_ACCOUNT)
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        handler(conn)  # second run: NULL-filter makes it a no-op, must not raise
        assert _accts(conn, "session_entries") == {_CUTOVER_ACCOUNT}
    finally:
        conn.close()


def test_absent_op_with_claude_rows_defers(cctally_module, tmp_path, monkeypatch):
    """No cutover op yet + legacy Claude rows -> defer (retry once the op lands)."""
    _set_journal_with_op(monkeypatch, tmp_path, None)
    gate_exc = sys.modules["_cctally_db"].MigrationGateNotMet
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        with pytest.raises(gate_exc):
            _handler(cctally_module)(conn)
    finally:
        conn.close()


def test_unattributed_op_is_a_noop(cctally_module, tmp_path, monkeypatch):
    """A resolved-unattributed cutover op leaves account_key NULL (== unattributed)."""
    _set_journal_with_op(monkeypatch, tmp_path, "unattributed")
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _handler(cctally_module)(conn)
        assert _accts(conn, "session_entries") == {None}
    finally:
        conn.close()
