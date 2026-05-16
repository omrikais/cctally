"""Per-migration goldens for ``006_five_hour_milestones_reset_event_id``.

Loads ``tests/fixtures/migrations/per-migration/006_.../pre.sqlite``,
runs the migration handler against it, and diffs the result against
``post.sqlite``. Verifies:

  * The ``reset_event_id`` column is added (NOT NULL DEFAULT 0).
  * All existing rows backfill to ``reset_event_id = 0``.
  * The new UNIQUE constraint
    ``UNIQUE(five_hour_window_key, percent_threshold, reset_event_id)``
    allows post-credit threshold crossings (same window+threshold,
    different reset_event_id) to coexist, while preserving the old
    (window, threshold, 0) collision under the new shape.
  * The ``006_five_hour_milestones_reset_event_id`` marker is stamped
    into ``schema_migrations``.
  * The fresh-install fast-path (column already present from the
    updated live DDL — see spec §3.2 / Codex r1 finding 1) stamps the
    marker without invoking the rename-recreate-copy idiom; regression
    that protects fresh-install correctness when the dispatcher
    fast-stamps registered migrations.

Per-migration goldens are lazy-adopted (CLAUDE.md gotcha "lazy-adopted;
not retroactively backfilled"); 006 mirrors the 005 shape one-for-one.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "006_five_hour_milestones_reset_event_id"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"


@pytest.fixture
def ns():
    return load_script()


def _migration_handler(ns):
    for m in ns["_STATS_MIGRATIONS"]:
        if m.name == "006_five_hour_milestones_reset_event_id":
            return m.handler
    raise AssertionError("migration 006 not registered")


def _table_schema(conn, table):
    return [
        (r[1], r[2], r[3], r[4])  # name, type, notnull, default
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    ]


def _milestone_rows(conn):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, five_hour_window_key, percent_threshold, "
            "       reset_event_id "
            "FROM five_hour_milestones ORDER BY id"
        ).fetchall()
    ]


def test_pre_fixture_has_legacy_shape(ns):
    """Sanity: pre.sqlite is at the pre-006 schema."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    conn.row_factory = sqlite3.Row
    try:
        cols = [r[0] for r in _table_schema(conn, "five_hour_milestones")]
        assert "reset_event_id" not in cols, (
            f"pre.sqlite should not have reset_event_id; cols={cols}"
        )
    finally:
        conn.close()


def test_migration_handler_adds_column_and_constraint(ns, tmp_path):
    """Run handler on a fresh copy of pre.sqlite; verify post shape."""
    work = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    conn.row_factory = sqlite3.Row
    try:
        _migration_handler(ns)(conn)

        # Column present.
        cols = {r[0] for r in _table_schema(conn, "five_hour_milestones")}
        assert "reset_event_id" in cols, cols

        # All existing rows have reset_event_id = 0.
        rows = _milestone_rows(conn)
        assert len(rows) == 3
        for r in rows:
            assert r["reset_event_id"] == 0, r

        # Marker stamped.
        assert conn.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name='006_five_hour_milestones_reset_event_id'"
        ).fetchone() is not None

        # New UNIQUE allows (window, threshold, distinct event_id) without
        # collision against the pre-existing (window, threshold, 0) row.
        # threshold=25 already exists with reset_event_id=0; inserting
        # threshold=25 with reset_event_id=42 must succeed under the new
        # UNIQUE shape (post-credit segment).
        conn.execute(
            "INSERT INTO five_hour_milestones "
            "(block_id, five_hour_window_key, percent_threshold, "
            " captured_at_utc, usage_snapshot_id, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, 1746550800, 25, "2026-05-16T18:30:00Z", 99, 42),
        )
        conn.commit()
        post_rows = _milestone_rows(conn)
        assert len(post_rows) == 4

        # Verify the OLD 2-col UNIQUE is gone — a duplicate
        # (same window, same threshold, same reset_event_id=0) must still
        # collide under the new 3-col UNIQUE.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO five_hour_milestones "
                "(block_id, five_hour_window_key, percent_threshold, "
                " captured_at_utc, usage_snapshot_id, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (1, 1746550800, 25, "2026-05-16T18:35:00Z", 100, 0),
            )
    finally:
        conn.close()


def test_migration_handler_idempotent_on_rerun(ns, tmp_path):
    """Second invocation finds the column already present and no-ops."""
    work = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    conn.row_factory = sqlite3.Row
    try:
        _migration_handler(ns)(conn)
        # Second call: should be a no-op (fast-path probe stamps marker
        # but doesn't rename or recreate).
        _migration_handler(ns)(conn)
        # Marker still exists exactly once.
        cnt = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='006_five_hour_milestones_reset_event_id'"
        ).fetchone()[0]
        assert cnt == 1
        # No sibling _old_006 table remains (would indicate a re-rename).
        sibling = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE name = 'five_hour_milestones_old_006'"
        ).fetchone()[0]
        assert sibling == 0
    finally:
        conn.close()


def test_migration_handler_fast_path_when_column_present(ns, tmp_path):
    """Fresh-install path: live DDL already carries reset_event_id; the
    handler sees the column present and stamps the marker WITHOUT redoing
    the rename.

    Regression for the dispatcher's fresh-install fast-stamp path: without
    the live DDL update at ``bin/cctally:3902-3919`` carrying the new
    shape, fresh installs would mark 006 applied while keeping the 2-col
    UNIQUE — silent corruption. Spec §3.2 / Codex r1 finding 1.
    """
    db = tmp_path / "fresh.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.executescript("""
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT NOT NULL
            );
            -- Minimal fresh-install shape: column already present + 3-col
            -- UNIQUE (mirrors the updated bin/cctally live DDL).
            CREATE TABLE five_hour_milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                five_hour_window_key INTEGER NOT NULL,
                percent_threshold INTEGER NOT NULL,
                reset_event_id INTEGER NOT NULL DEFAULT 0,
                UNIQUE(five_hour_window_key, percent_threshold, reset_event_id)
            );
        """)
        _migration_handler(ns)(conn)
        marker = conn.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name='006_five_hour_milestones_reset_event_id'"
        ).fetchone()
        assert marker is not None, "fast-path must stamp the marker"
        # Fast-path MUST NOT have renamed / recreated the table.
        sibling = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE name = 'five_hour_milestones_old_006'"
        ).fetchone()[0]
        assert sibling == 0, (
            "fast-path must not create the _old_006 sibling table"
        )
    finally:
        conn.close()


def test_migration_handler_preserves_row_count_and_post_golden(ns, tmp_path):
    """Upgrade path: pre.sqlite rows carry over with reset_event_id=0,
    schema + row state matches post.sqlite (which was generated by the
    exact same handler at fixture-build time).
    """
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    work = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    conn.row_factory = sqlite3.Row
    try:
        pre_count = conn.execute(
            "SELECT COUNT(*) AS c FROM five_hour_milestones"
        ).fetchone()["c"]
        _migration_handler(ns)(conn)
        post_count = conn.execute(
            "SELECT COUNT(*) AS c FROM five_hour_milestones"
        ).fetchone()["c"]
        assert pre_count == post_count, (
            f"row count must be preserved (pre={pre_count}, post={post_count})"
        )

        # Diff against the on-disk post.sqlite. Schema must match (modulo
        # CREATE-table whitespace, which sqlite_master preserves verbatim
        # from the CREATE statement we executed; both handler invocations
        # used the same source string).
        post_conn = sqlite3.connect(POST_DB)
        post_conn.row_factory = sqlite3.Row
        try:
            work_schema = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE name='five_hour_milestones'"
            ).fetchone()["sql"]
            post_schema = post_conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE name='five_hour_milestones'"
            ).fetchone()["sql"]
            assert work_schema == post_schema, (
                f"schema diverged:\n--- work ---\n{work_schema}\n"
                f"--- post ---\n{post_schema}"
            )

            work_rows = conn.execute(
                "SELECT id, five_hour_window_key, percent_threshold, "
                "       reset_event_id "
                "FROM five_hour_milestones ORDER BY id"
            ).fetchall()
            post_rows = post_conn.execute(
                "SELECT id, five_hour_window_key, percent_threshold, "
                "       reset_event_id "
                "FROM five_hour_milestones ORDER BY id"
            ).fetchall()
            assert [dict(r) for r in work_rows] == [
                dict(r) for r in post_rows
            ]
        finally:
            post_conn.close()
    finally:
        conn.close()
