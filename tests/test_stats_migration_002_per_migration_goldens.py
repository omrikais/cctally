"""Per-migration goldens for stats migration
``002_five_hour_block_projects_backfill_v1`` (#279 S7 W3 backfill).

Mirror of the 001 goldens for the by-project rollup child: pre.sqlite seeds a
``five_hour_blocks`` parent + an ORPHAN ``five_hour_block_projects`` row;
post.sqlite has the orphan DELETEd, no child rows written, + the 002 marker.
The handler re-run isolates the ``skip_sync=False`` cache read via
``redirect_paths``.
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
    / "002_five_hour_block_projects_backfill_v1"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

CHILD_TABLE = "five_hour_block_projects"


@pytest.fixture
def ns():
    return load_script()


def _handler(ns):
    for m in ns["_STATS_MIGRATIONS"]:
        if m.name == "002_five_hour_block_projects_backfill_v1":
            return m.handler
    raise AssertionError("stats migration 002 not registered")


def _child_block_ids(conn):
    return [r[0] for r in conn.execute(f"SELECT block_id FROM {CHILD_TABLE}")]


def test_pre_fixture_has_orphan_child(ns):
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
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
            "WHERE name='002_five_hour_block_projects_backfill_v1'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_cleans_orphan_and_is_idempotent(ns, monkeypatch, tmp_path):
    redirect_paths(ns, monkeypatch, tmp_path)
    work = tmp_path / "work-stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    conn.row_factory = sqlite3.Row
    try:
        _handler(ns)(conn)
        ns["_stamp_applied"](conn, "002_five_hour_block_projects_backfill_v1")
        assert _child_block_ids(conn) == []
        _handler(ns)(conn)
        ns["_stamp_applied"](conn, "002_five_hour_block_projects_backfill_v1")
        assert _child_block_ids(conn) == []
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='002_five_hour_block_projects_backfill_v1'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
