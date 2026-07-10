"""Per-migration goldens for stats migration ``003_merge_5h_block_duplicates_v1``
(#279 S7 W3 backfill).

pre.sqlite: TWO ``five_hour_blocks`` rows for the same physical 5h window under
jitter-forked ``five_hour_window_key`` values (their ``five_hour_resets_at``
fall within the 1800 s grouping band), plus a ``weekly_usage_snapshots`` row
keyed on the DROPPED block's window key. post.sqlite: the pair merged into the
canonical (earliest ``first_observed_at_utc``) block — group-MAX aggregates, the
dropped parent gone, the snapshot re-keyed to canonical — + the 003 marker.

The handler is pure-stats (no cache open). It writes ``now_utc_iso()`` into
``last_updated_at_utc``, so this test asserts merge SEMANTICS (block count, key
rewrite, MAX aggregates) rather than that volatile column's exact value; the
builder pins the clock for the committed golden.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "003_merge_5h_block_duplicates_v1"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

CANONICAL_KEY = 1776600000
DROPPED_KEY = 1776600600


@pytest.fixture
def ns():
    return load_script()


def _handler(ns):
    for m in ns["_STATS_MIGRATIONS"]:
        if m.name == "003_merge_5h_block_duplicates_v1":
            return m.handler
    raise AssertionError("stats migration 003 not registered")


def _blocks(conn):
    return conn.execute(
        "SELECT id, five_hour_window_key, total_input_tokens, "
        "       total_output_tokens, final_five_hour_percent "
        "FROM five_hour_blocks ORDER BY id"
    ).fetchall()


def _snap_keys(conn):
    return [
        r[0]
        for r in conn.execute(
            "SELECT five_hour_window_key FROM weekly_usage_snapshots"
        )
    ]


def test_pre_fixture_has_two_forked_blocks(ns):
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert len(_blocks(conn)) == 2
        assert set(_snap_keys(conn)) == {DROPPED_KEY}
    finally:
        conn.close()


def test_post_fixture_merged_with_marker(ns):
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        blocks = _blocks(conn)
        assert len(blocks) == 1, "duplicate must be merged away"
        bid, key, tin, tout, pct = blocks[0]
        assert key == CANONICAL_KEY
        # Group-wide MAX aggregates from the two pre rows (100/150, 200/300).
        assert (tin, tout) == (150, 300)
        # Latest-observation snapshot (the dropped row observed later).
        assert pct == 55.0
        # Snapshot re-keyed to canonical.
        assert set(_snap_keys(conn)) == {CANONICAL_KEY}
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='003_merge_5h_block_duplicates_v1'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_merges_and_is_idempotent(ns, tmp_path):
    work = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    conn.row_factory = sqlite3.Row  # handler reads rows by column name
    try:
        _handler(ns)(conn)
        ns["_stamp_applied"](conn, "003_merge_5h_block_duplicates_v1")
        blocks = _blocks(conn)
        assert len(blocks) == 1
        assert blocks[0][1] == CANONICAL_KEY
        assert (blocks[0][2], blocks[0][3]) == (150, 300)
        assert set(_snap_keys(conn)) == {CANONICAL_KEY}
        # Second invocation: no duplicate groups remain → no merge, no
        # now_utc_iso() write. Block set is unchanged (idempotent).
        _handler(ns)(conn)
        ns["_stamp_applied"](conn, "003_merge_5h_block_duplicates_v1")
        assert len(_blocks(conn)) == 1
        assert set(_snap_keys(conn)) == {CANONICAL_KEY}
    finally:
        conn.close()
