"""Per-migration goldens for conversations migration 005 account dimension."""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


IDEMPOTENCY_COVERED = True
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations" / "per-migration"
    / "conversations_005_conversation_account_dimension"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "005_conversation_account_dimension"
ACCOUNT = "a" * 32


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


def _handler(module):
    return next(m.handler for m in module._CONVERSATIONS_MIGRATIONS if m.name == MIGRATION)


def test_golden_backfills_only_legacy_claude_rows():
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert pre.execute("PRAGMA user_version").fetchone()[0] == 4
        assert post.execute("PRAGMA user_version").fetchone()[0] == 5
        assert pre.execute(
            "SELECT account_key FROM conversation_messages"
        ).fetchone() == (None,)
        assert post.execute(
            "SELECT account_key FROM conversation_messages"
        ).fetchone() == (ACCOUNT,)
        assert post.execute(
            "SELECT account_key FROM codex_conversation_events"
        ).fetchone() == (None,)
        assert post.execute(
            "SELECT account_key FROM codex_conversation_messages"
        ).fetchone() == (None,)
        assert post.execute(
            "SELECT value FROM cache_meta WHERE key='conversation_account_dimension'"
        ).fetchone() == ("1",)
    finally:
        pre.close()
        post.close()


def test_handler_is_idempotent(cctally_module, tmp_path, monkeypatch):
    work = tmp_path / "conversations.db"
    shutil.copy(PRE_DB, work)
    import _cctally_journal
    monkeypatch.setattr(_cctally_journal, "find_accounts_cutover_op", lambda: ACCOUNT)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        first = conn.execute(
            "SELECT account_key FROM conversation_messages"
        ).fetchall()
        handler(conn)
        assert conn.execute(
            "SELECT account_key FROM conversation_messages"
        ).fetchall() == first == [(ACCOUNT,)]
    finally:
        conn.close()


def test_handler_defers_legacy_rows_until_cutover_exists(
        cctally_module, tmp_path, monkeypatch):
    work = tmp_path / "pending-cutover.db"
    shutil.copy(PRE_DB, work)
    import _cctally_journal
    import _cctally_db
    monkeypatch.setattr(_cctally_journal, "find_accounts_cutover_op", lambda: None)
    conn = sqlite3.connect(work)
    try:
        with pytest.raises(_cctally_db.MigrationGateNotMet):
            _handler(cctally_module)(conn)
        assert conn.execute(
            "SELECT account_key FROM conversation_messages"
        ).fetchall() == [(None,)]
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='conversation_account_dimension'"
        ).fetchone() is None
    finally:
        conn.close()


def test_handler_allows_absent_cutover_on_empty_store(
        cctally_module, tmp_path, monkeypatch):
    work = tmp_path / "empty.db"
    conn = sqlite3.connect(work)
    import _cctally_db
    _cctally_db._apply_conversations_schema(conn)
    import _cctally_journal
    monkeypatch.setattr(_cctally_journal, "find_accounts_cutover_op", lambda: None)
    try:
        _handler(cctally_module)(conn)
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='conversation_account_dimension'"
        ).fetchone() == ("1",)
    finally:
        conn.close()


def test_handler_accepts_explicit_unattributed_cutover(
        cctally_module, tmp_path, monkeypatch):
    work = tmp_path / "unattributed.db"
    shutil.copy(PRE_DB, work)
    import _cctally_journal
    monkeypatch.setattr(
        _cctally_journal, "find_accounts_cutover_op", lambda: "unattributed"
    )
    conn = sqlite3.connect(work)
    try:
        _handler(cctally_module)(conn)
        assert conn.execute(
            "SELECT account_key FROM conversation_messages"
        ).fetchall() == [(None,)]
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='conversation_account_dimension'"
        ).fetchone() == ("1",)
    finally:
        conn.close()
