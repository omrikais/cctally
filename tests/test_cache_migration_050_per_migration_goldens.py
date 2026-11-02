"""Per-migration goldens for #944's U1 export of existing Claude cost stamps.

``pre.sqlite`` is a 049-head store with three pre-upgrade rows (keyed and
stamped, keyed and NULL, lacking an id and stamped) and no attribution
schema; ``post.sqlite`` is the handler's result. Detects: the upgrade
changing any existing row's account, or leaving a row whose stamp a later
rebuild could not reproduce.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script, redirect_paths


IDEMPOTENCY_COVERED = True
MIGRATION = "050_preserve_claude_cost_attribution"
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations"
    / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
TABLES = ("claude_account_timeline", "claude_cost_decisions")
INDEXES = ("idx_entries_attr_unlinked", "idx_entries_attr_nonkeyed")
STAMPS = [(1, "a" * 32), (2, None), (3, "a" * 32)]


@pytest.fixture
def db(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_db
    return _cctally_db


def _handler(db):
    return next(item.handler for item in db._CACHE_MIGRATIONS
                if item.name == MIGRATION)


def _copy(path: Path, tmp_path: Path) -> Path:
    target = tmp_path / path.name
    shutil.copyfile(path, target)
    return target


def _names(conn, kind) -> set:
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def _columns(conn) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(session_entries)")}


def _linked(conn):
    return conn.execute(
        "SELECT e.id, e.account_key, d.account_key, d.basis, d.form"
        " FROM session_entries e JOIN claude_cost_decisions d"
        " ON d.decision_key = e.attribution_decision_key ORDER BY e.id"
    ).fetchall()


def _assert_exported(conn):
    rows = _linked(conn)
    assert [(r[0], r[1]) for r in rows] == STAMPS
    for _id, account, decided, basis, _form in rows:
        assert decided == account and basis == "preserved_history"
    assert [r[4] for r in rows] == ["k1", "k1", "l1"]
    projection = conn.execute(
        "SELECT projection_json FROM claude_cost_decisions WHERE form='l1'"
    ).fetchone()[0]
    assert json.loads(projection)[0] == "2026-10-01T10:10:00+00:00"


def test_pre_fixture_is_a_049_head_without_the_attribution_schema(tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 49
        assert not _names(conn, "table") & set(TABLES)
        assert not _names(conn, "index") & set(INDEXES)
        assert "attribution_decision_key" not in _columns(conn)
        assert conn.execute(
            "SELECT id, account_key FROM session_entries ORDER BY id"
        ).fetchall() == STAMPS
    finally:
        conn.close()


def test_post_fixture_preserves_every_stamp_as_a_decision(tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 50
        assert set(TABLES) <= _names(conn, "table")
        assert set(INDEXES) <= _names(conn, "index")
        _assert_exported(conn)
        progress = json.loads(conn.execute(
            "SELECT value FROM cache_meta WHERE key='claude_cost_preservation'"
        ).fetchone()[0])
        assert (progress["saved"], progress["total"], progress["afterId"]) == (
            3, 3, 3)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_takes_pre_to_post_shape_and_is_idempotent(db, tmp_path):
    import _cctally_core as core
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        handler = _handler(db)
        handler(conn)
        _assert_exported(conn)
        journal = sorted(p.stat().st_size
                         for p in core.JOURNAL_DIR.glob("*.jsonl"))
        handler(conn)
        _assert_exported(conn)
        assert sorted(p.stat().st_size
                      for p in core.JOURNAL_DIR.glob("*.jsonl")) == journal
    finally:
        conn.close()


def test_handler_waits_for_029(db, tmp_path):
    """Detects: the export recording NULL stamps 029 has yet to backfill."""
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        conn.execute("DELETE FROM schema_migrations "
                     "WHERE name='029_backfill_claude_account'")
        conn.commit()
        with pytest.raises(db.MigrationGateNotMet):
            _handler(db)(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM session_entries "
            "WHERE attribution_decision_key IS NOT NULL").fetchone()[0] == 0
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
