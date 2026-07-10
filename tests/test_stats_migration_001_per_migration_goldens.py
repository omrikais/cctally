"""Per-migration goldens for stats migration
``001_five_hour_block_models_backfill_v1`` (#279 S7 W3 backfill).

pre.sqlite: one ``five_hour_blocks`` parent + one ORPHAN
``five_hour_block_models`` row (block_id references no parent). post.sqlite: the
handler's defensive orphan cleanup DELETEs the orphan; its per-block backfill
loop writes zero child rows (the ``skip_sync=False`` cache read finds no
entries) + the 001 marker. The orphan DELETE is the non-vacuous handler effect;
the faithful child-row backfill is covered end-to-end by the ancient→head test
(W2) and migrations-test scenario 11.

The handler re-run isolates the cache read via ``redirect_paths`` (HOME → tmp,
empty projects, tmp cache) so it never touches the developer's real
``~/.claude/projects``.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script, redirect_paths


IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "001_five_hour_block_models_backfill_v1"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

CHILD_TABLE = "five_hour_block_models"


@pytest.fixture
def ns():
    return load_script()


def _handler(ns):
    for m in ns["_STATS_MIGRATIONS"]:
        if m.name == "001_five_hour_block_models_backfill_v1":
            return m.handler
    raise AssertionError("stats migration 001 not registered")


def _child_block_ids(conn):
    return [r[0] for r in conn.execute(f"SELECT block_id FROM {CHILD_TABLE}")]


def test_pre_fixture_has_orphan_child(ns):
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        # One parent, one ORPHAN child (block_id not among parents).
        parents = {r[0] for r in conn.execute("SELECT id FROM five_hour_blocks")}
        assert parents == {1}
        assert _child_block_ids(conn) == [999]
        assert 999 not in parents
    finally:
        conn.close()


def test_post_fixture_orphan_removed_with_marker(ns):
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _child_block_ids(conn) == [], "orphan child must be deleted"
        assert {r[0] for r in conn.execute("SELECT id FROM five_hour_blocks")} == {1}
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='001_five_hour_block_models_backfill_v1'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_cleans_orphan_and_is_idempotent(ns, monkeypatch, tmp_path):
    # Full isolation: HOME → tmp (no projects), cache/lock → tmp share. The
    # backfill loop's skip_sync=False read then finds an empty cache + empty
    # projects → zero child rows written. Never touches real ~/.claude/projects.
    redirect_paths(ns, monkeypatch, tmp_path)
    work = tmp_path / "work-stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    conn.row_factory = sqlite3.Row  # handler reads rows by column name
    try:
        _handler(ns)(conn)
        ns["_stamp_applied"](conn, "001_five_hour_block_models_backfill_v1")
        assert _child_block_ids(conn) == [], "orphan must be removed, none added"
        # Second invocation: orphan already gone, empty cache → no-op.
        _handler(ns)(conn)
        ns["_stamp_applied"](conn, "001_five_hour_block_models_backfill_v1")
        assert _child_block_ids(conn) == []
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='001_five_hour_block_models_backfill_v1'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
