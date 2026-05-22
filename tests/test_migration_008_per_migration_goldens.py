"""Per-migration goldens for stats migration
``008_recompute_weekly_cost_snapshots_dedup_fix``.

Loads ``tests/fixtures/migrations/per-migration/008_.../pre.sqlite`` and
its paired ``pre-cache.sqlite``, runs the production migration handler
against a copy of pre.sqlite (with the cache sidecar wired in via
``_cctally_core.CACHE_DB_PATH`` / ``CLAUDE_PROJECTS_DIR``), and asserts
the result matches ``post.sqlite``.

Verifies:

  * Auto/no-project row's ``cost_usd`` is recomputed from the cache's
    ``session_entries`` ($0.025 at the embedded $25/Mtok opus-4-7 rate).
  * Display row's ``cost_usd`` is preserved verbatim.
  * Project-scoped row's ``cost_usd`` is preserved verbatim.
  * The ``008_recompute_weekly_cost_snapshots_dedup_fix`` marker is
    stamped into ``schema_migrations``.

This is the second per-migration paired-DB scenario (the first was 001's
cache-only fixture). 005, 006, and 007 are stats-only single-DB
scenarios; 008 needs cache.db too because it cross-reads ``session_entries``.

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3.
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "008_recompute_weekly_cost_snapshots_dedup_fix"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
PRE_CACHE_DB = FIXTURE_DIR / "pre-cache.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"


@pytest.fixture(scope="module")
def db_module():
    """Load bin/_cctally_db.py once per module via SourceFileLoader.

    Matches the pattern in test_migration_001_per_migration_goldens.py so
    the production handler is exercised verbatim (no copy-paste drift).
    """
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    # Drop cached sibling modules so a fresh load picks up any in-flight
    # edits within the same session.
    for _name in [
        n for n in list(sys.modules)
        if n.startswith("_cctally_") and n != "_cctally_core"
    ]:
        del sys.modules[_name]
    spec = ilu.spec_from_file_location(
        "_cctally_db", BIN_DIR / "_cctally_db.py"
    )
    mod = ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _migration_handler(db_module):
    for m in db_module._STATS_MIGRATIONS:
        if m.name == "008_recompute_weekly_cost_snapshots_dedup_fix":
            return m.handler
    raise AssertionError(
        "stats migration 008_recompute_weekly_cost_snapshots_dedup_fix "
        "not registered"
    )


def _snapshot_rows(conn):
    return [
        (r[0], r[1], r[2], r[3])  # id, mode, project, cost_usd
        for r in conn.execute(
            "SELECT id, mode, project, cost_usd "
            "FROM weekly_cost_snapshots ORDER BY id"
        ).fetchall()
    ]


def test_pre_fixture_has_three_rows_and_no_008_marker(db_module):
    """Sanity: pre.sqlite has the 3-row scope topology and no 008 marker."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        rows = _snapshot_rows(conn)
        assert len(rows) == 3
        # Row 1: auto, no-project, stale pre-fix cost.
        assert rows[0][1:] == ("auto", None, 100.0)
        # Row 2: display, no-project, user-supplied cost.
        assert rows[1][1:] == ("display", None, 999.0)
        # Row 3: auto, project='myproj', per-project scoped cost.
        assert rows[2][1:] == ("auto", "myproj", 50.0)

        marker = conn.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name = '008_recompute_weekly_cost_snapshots_dedup_fix'"
        ).fetchone()
        assert marker is None, "pre.sqlite must not have the 008 marker yet"
    finally:
        conn.close()


def test_pre_cache_fixture_has_001_marker_and_post_001_ingest(db_module):
    """Sanity: pre-cache.sqlite has the 001 marker, one post-001
    session_files row, and one session_entry inside the week range."""
    assert PRE_CACHE_DB.exists(), f"missing pre-cache fixture: {PRE_CACHE_DB}"
    conn = sqlite3.connect(PRE_CACHE_DB)
    try:
        m = conn.execute(
            "SELECT applied_at_utc FROM schema_migrations "
            "WHERE name = '001_dedup_highest_wins'"
        ).fetchone()
        assert m is not None, "pre-cache.sqlite must have the 001 marker"
        applied_at = m[0]

        # Layer B gate: a session_files row whose last_ingested_at >
        # applied_at_utc (proof of post-001 ingest).
        post_001 = conn.execute(
            "SELECT 1 FROM session_files "
            "WHERE last_ingested_at > ? LIMIT 1",
            (applied_at,),
        ).fetchone()
        assert post_001 is not None, (
            "pre-cache.sqlite must have at least one session_files row "
            "with last_ingested_at > 001.applied_at_utc"
        )

        entry_count = conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0]
        assert entry_count == 1
    finally:
        conn.close()


def test_post_fixture_matches_handler_output(db_module):
    """Sanity: post.sqlite reflects the handler's expected output —
    row 1 recomputed to $0.025, rows 2 & 3 unchanged, 008 marker stamped."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        rows = _snapshot_rows(conn)
        assert len(rows) == 3
        # Row 1 recomputed: 1000 opus-4-7 output tokens at $25/Mtok = $0.025.
        assert rows[0][1:] == ("auto", None, pytest.approx(0.025, abs=1e-9))
        # Rows 2 & 3 preserved verbatim.
        assert rows[1][1:] == ("display", None, 999.0)
        assert rows[2][1:] == ("auto", "myproj", 50.0)

        marker = conn.execute(
            "SELECT name FROM schema_migrations "
            "WHERE name = '008_recompute_weekly_cost_snapshots_dedup_fix'"
        ).fetchone()
        assert marker is not None, "post.sqlite must carry the 008 marker"
    finally:
        conn.close()


def test_migration_handler_recomputes_auto_rows_preserves_others(
    db_module, tmp_path, monkeypatch
):
    """Run the production handler against a copy of pre.sqlite with the
    paired pre-cache.sqlite wired in via core's path constants. Result must
    match post.sqlite (modulo the marker's applied_at_utc, which is
    now_utc_iso() at handler time)."""
    work_stats = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work_stats)

    # Synthetic JSONL so the gate's empty-disk fallback doesn't fire — we
    # want Layer B (post-001 ingest) to be the path that passes.
    projects_dir = tmp_path / "claude_projects"
    projects_dir.mkdir()
    (projects_dir / "session1.jsonl").write_text("{}\n")

    core = db_module._cctally_core
    monkeypatch.setattr(core, "CACHE_DB_PATH", PRE_CACHE_DB)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", projects_dir)

    handler = _migration_handler(db_module)
    conn = sqlite3.connect(work_stats)
    try:
        handler(conn)

        rows = _snapshot_rows(conn)
        assert rows[0][1:] == ("auto", None, pytest.approx(0.025, abs=1e-9))
        assert rows[1][1:] == ("display", None, 999.0)
        assert rows[2][1:] == ("auto", "myproj", 50.0)

        # Marker stamped.
        applied_at = conn.execute(
            "SELECT applied_at_utc FROM schema_migrations "
            "WHERE name = '008_recompute_weekly_cost_snapshots_dedup_fix'"
        ).fetchone()
        assert applied_at, "008 marker not stamped"
        assert applied_at[0], "008 marker has empty applied_at_utc"
    finally:
        conn.close()


def test_migration_handler_idempotent_against_marker(
    db_module, tmp_path, monkeypatch
):
    """A second invocation re-INSERTs the marker via the same
    ``INSERT INTO ... VALUES`` form — INTEGRITY_ERROR on the PRIMARY KEY.
    The dispatcher provides idempotency by tracking ``applied``; this test
    just documents that the handler body is not idempotent against itself
    (matches 001's per-migration goldens contract)."""
    work_stats = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work_stats)

    projects_dir = tmp_path / "claude_projects"
    projects_dir.mkdir()
    (projects_dir / "session1.jsonl").write_text("{}\n")

    core = db_module._cctally_core
    monkeypatch.setattr(core, "CACHE_DB_PATH", PRE_CACHE_DB)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", projects_dir)

    handler = _migration_handler(db_module)
    conn = sqlite3.connect(work_stats)
    try:
        handler(conn)
        with pytest.raises(sqlite3.IntegrityError):
            handler(conn)
    finally:
        conn.close()
