"""Per-migration goldens for stats migration
``009_recompute_five_hour_blocks_dedup_fix``.

Loads ``tests/fixtures/migrations/per-migration/009_.../pre.sqlite`` and
its paired ``pre-cache.sqlite``, runs the production migration handler
against a copy of pre.sqlite (with the cache sidecar wired in via
``_cctally_core.CACHE_DB_PATH`` / ``CLAUDE_PROJECTS_DIR``), and asserts
the result matches ``post.sqlite`` modulo the marker's applied_at_utc
(``now_utc_iso()`` at handler time).

Verifies (per spec §I3, finding B1):

  * Two 5h blocks (one closed historical, one active current) both have
    their inflated pre-dedup totals recomputed downward to match the
    real session_entries.
  * ``five_hour_block_models`` per-(window, model) rollup-children are
    replace-all'd by the migration.
  * ``five_hour_block_projects`` per-(window, project) rollup-children
    are replace-all'd; block B's pre-fix collapsed-to-one-project row
    is expanded to the two projects that actually had entries.
  * The ``009_recompute_five_hour_blocks_dedup_fix`` marker is stamped
    into ``schema_migrations``.

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3 (B1).
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
    / "009_recompute_five_hour_blocks_dedup_fix"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
PRE_CACHE_DB = FIXTURE_DIR / "pre-cache.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"


@pytest.fixture(scope="module")
def db_module():
    """Load bin/_cctally_db.py once per module via SourceFileLoader.

    Matches the pattern in test_migration_008_per_migration_goldens.py.
    """
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
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
        if m.name == "009_recompute_five_hour_blocks_dedup_fix":
            return m.handler
    raise AssertionError(
        "stats migration 009_recompute_five_hour_blocks_dedup_fix "
        "not registered"
    )


def _block_rows(conn):
    return [
        (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7])
        for r in conn.execute(
            "SELECT id, five_hour_window_key, "
            "       total_input_tokens, total_output_tokens, "
            "       total_cache_create_tokens, total_cache_read_tokens, "
            "       total_cost_usd, is_closed "
            "FROM five_hour_blocks ORDER BY id"
        ).fetchall()
    ]


def _model_rows(conn):
    return [
        (r[0], r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT five_hour_window_key, model, "
            "       output_tokens, cost_usd "
            "FROM five_hour_block_models "
            "ORDER BY five_hour_window_key, model"
        ).fetchall()
    ]


def _project_rows(conn):
    return [
        (r[0], r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT five_hour_window_key, project_path, "
            "       output_tokens, cost_usd "
            "FROM five_hour_block_projects "
            "ORDER BY five_hour_window_key, project_path"
        ).fetchall()
    ]


def test_pre_fixture_has_inflated_pre_dedup_totals(db_module):
    """Sanity: pre.sqlite carries the inflated pre-dedup totals."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        rows = _block_rows(conn)
        assert len(rows) == 2
        # Block A (closed): 2000 out tokens, $0.050.
        assert rows[0][1:] == (100, 0, 2000, 0, 0, 0.050, 1)
        # Block B (active): 4000 out tokens, $0.100.
        assert rows[1][1:] == (200, 0, 4000, 0, 0, 0.100, 0)

        # No 009 marker yet.
        marker = conn.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name = '009_recompute_five_hour_blocks_dedup_fix'"
        ).fetchone()
        assert marker is None
    finally:
        conn.close()


def test_pre_cache_fixture_has_001_marker_and_post_001_ingest(db_module):
    """Sanity: pre-cache.sqlite carries the 001 marker + a session_files
    row whose last_ingested_at is strictly after the marker (Layer B
    gate)."""
    assert PRE_CACHE_DB.exists(), f"missing pre-cache fixture: {PRE_CACHE_DB}"
    conn = sqlite3.connect(PRE_CACHE_DB)
    try:
        m = conn.execute(
            "SELECT applied_at_utc FROM schema_migrations "
            "WHERE name = '001_dedup_highest_wins'"
        ).fetchone()
        assert m is not None
        applied_at = m[0]
        post_001 = conn.execute(
            "SELECT 1 FROM session_files "
            "WHERE last_ingested_at > ? LIMIT 1",
            (applied_at,),
        ).fetchone()
        assert post_001 is not None
        entry_count = conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0]
        assert entry_count == 3
    finally:
        conn.close()


def test_post_fixture_matches_handler_output(db_module):
    """Sanity: post.sqlite reflects the expected handler output."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        rows = _block_rows(conn)
        assert len(rows) == 2
        # Block A: 1000 out tokens, $0.025.
        assert rows[0][:6] == (1, 100, 0, 1000, 0, 0)
        assert rows[0][6] == pytest.approx(0.025, abs=1e-9)
        # Block B: 2000 out tokens, $0.050.
        assert rows[1][:6] == (2, 200, 0, 2000, 0, 0)
        assert rows[1][6] == pytest.approx(0.050, abs=1e-9)

        marker = conn.execute(
            "SELECT name FROM schema_migrations "
            "WHERE name = '009_recompute_five_hour_blocks_dedup_fix'"
        ).fetchone()
        assert marker is not None
    finally:
        conn.close()


def test_migration_handler_recomputes_parents_and_replace_all_children(
    db_module, tmp_path, monkeypatch
):
    """Run the production handler against a copy of pre.sqlite with the
    paired pre-cache.sqlite wired in via core's path constants. Result
    must match post.sqlite modulo applied_at_utc.
    """
    work_stats = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work_stats)
    work_cache = tmp_path / "cache.db"
    shutil.copy(PRE_CACHE_DB, work_cache)

    projects_dir = tmp_path / "claude_projects"
    projects_dir.mkdir()
    (projects_dir / "session1.jsonl").write_text("{}\n")

    core = db_module._cctally_core
    monkeypatch.setattr(core, "CACHE_DB_PATH", work_cache)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", projects_dir)

    handler = _migration_handler(db_module)
    conn = sqlite3.connect(work_stats)
    try:
        handler(conn)

        # Parent recompute: both blocks downward to real totals.
        rows = _block_rows(conn)
        assert len(rows) == 2
        # Block A: out=1000, cost=$0.025.
        assert rows[0][3] == 1000
        assert rows[0][6] == pytest.approx(0.025, abs=1e-9)
        # Block B: out=2000, cost=$0.050.
        assert rows[1][3] == 2000
        assert rows[1][6] == pytest.approx(0.050, abs=1e-9)

        # Per-model rollup: one row per (window, model). Block A's model
        # row was pre-existing with inflated numbers; the replace-all
        # rewrote it to the corrected values. Same for B.
        m_rows = _model_rows(conn)
        assert len(m_rows) == 2
        assert m_rows[0][:3] == (100, "claude-opus-4-7", 1000)
        assert m_rows[0][3] == pytest.approx(0.025, abs=1e-9)
        assert m_rows[1][:3] == (200, "claude-opus-4-7", 2000)
        assert m_rows[1][3] == pytest.approx(0.050, abs=1e-9)

        # Per-project rollup: A has one row (projA), B has two rows
        # (projA + projB). Pre-fix B had only one projA row with
        # collapsed numbers — the replace-all migration expands it.
        p_rows = _project_rows(conn)
        assert len(p_rows) == 3
        assert p_rows[0][:3] == (100, "/tmp/projA", 1000)
        assert p_rows[0][3] == pytest.approx(0.025, abs=1e-9)
        assert p_rows[1][:3] == (200, "/tmp/projA", 1000)
        assert p_rows[1][3] == pytest.approx(0.025, abs=1e-9)
        assert p_rows[2][:3] == (200, "/tmp/projB", 1000)
        assert p_rows[2][3] == pytest.approx(0.025, abs=1e-9)

        applied = conn.execute(
            "SELECT applied_at_utc FROM schema_migrations "
            "WHERE name = '009_recompute_five_hour_blocks_dedup_fix'"
        ).fetchone()
        assert applied and applied[0]
    finally:
        conn.close()


def test_migration_handler_idempotent_against_marker(
    db_module, tmp_path, monkeypatch
):
    """A second invocation silently no-ops on the marker INSERT
    (INSERT OR IGNORE) — same race-safety contract as 008."""
    work_stats = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work_stats)
    work_cache = tmp_path / "cache.db"
    shutil.copy(PRE_CACHE_DB, work_cache)

    projects_dir = tmp_path / "claude_projects"
    projects_dir.mkdir()
    (projects_dir / "session1.jsonl").write_text("{}\n")

    core = db_module._cctally_core
    monkeypatch.setattr(core, "CACHE_DB_PATH", work_cache)
    monkeypatch.setattr(core, "CLAUDE_PROJECTS_DIR", projects_dir)

    handler = _migration_handler(db_module)
    conn = sqlite3.connect(work_stats)
    try:
        handler(conn)
        handler(conn)
        cnt = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name = '009_recompute_five_hour_blocks_dedup_fix'"
        ).fetchone()[0]
        assert cnt == 1
    finally:
        conn.close()
