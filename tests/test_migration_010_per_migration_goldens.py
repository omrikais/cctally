"""Per-migration goldens for stats migration
``010_recompute_percent_milestones_dedup_fix``.

Loads ``tests/fixtures/migrations/per-migration/010_.../pre.sqlite`` and
its paired ``pre-cache.sqlite``, runs the production migration handler
against a copy of pre.sqlite (with the cache sidecar wired in via
``_cctally_core.CACHE_DB_PATH`` / ``CLAUDE_PROJECTS_DIR``), and asserts
the result matches ``post.sqlite`` modulo the marker's applied_at_utc.

Verifies (per spec §I3, finding B2):

  * Every percent_milestones row's ``cumulative_cost_usd`` is
    recomputed from the corrected session_entries over
    ``[week_start_at, captured_at_utc]``.
  * ``marginal_cost_usd`` is recomputed as
    ``cumulative - prior.cumulative`` within the same
    ``(week_start_date, reset_event_id)`` segment, ordered by
    ``percent_threshold`` ASC. First milestone of a week has
    ``marginal == cumulative``.
  * The ``010_recompute_percent_milestones_dedup_fix`` marker is
    stamped into ``schema_migrations``.

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3 (B2).
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
    / "010_recompute_percent_milestones_dedup_fix"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
PRE_CACHE_DB = FIXTURE_DIR / "pre-cache.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"


@pytest.fixture(scope="module")
def db_module():
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
        if m.name == "010_recompute_percent_milestones_dedup_fix":
            return m.handler
    raise AssertionError(
        "stats migration 010_recompute_percent_milestones_dedup_fix "
        "not registered"
    )


def _milestone_rows(conn):
    return [
        (r[0], r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT percent_threshold, cumulative_cost_usd, "
            "       marginal_cost_usd, reset_event_id "
            "FROM percent_milestones "
            "ORDER BY week_start_date, reset_event_id, "
            "         percent_threshold"
        ).fetchall()
    ]


def test_pre_fixture_has_inflated_pre_dedup_costs(db_module):
    """Sanity: pre.sqlite carries 2x-inflated pre-dedup cumulative
    costs at thresholds 1, 2, 3."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        rows = _milestone_rows(conn)
        assert len(rows) == 3
        # threshold, cumulative, marginal, reset_event_id
        assert rows[0] == (1, 0.050, 0.050, 0)
        assert rows[1] == (2, 0.100, 0.050, 0)
        assert rows[2] == (3, 0.150, 0.050, 0)

        marker = conn.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name = '010_recompute_percent_milestones_dedup_fix'"
        ).fetchone()
        assert marker is None
    finally:
        conn.close()


def test_pre_cache_fixture_has_001_marker_and_three_entries(db_module):
    assert PRE_CACHE_DB.exists(), f"missing pre-cache fixture: {PRE_CACHE_DB}"
    conn = sqlite3.connect(PRE_CACHE_DB)
    try:
        m = conn.execute(
            "SELECT applied_at_utc FROM schema_migrations "
            "WHERE name = '001_dedup_highest_wins'"
        ).fetchone()
        assert m is not None
        entry_count = conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0]
        assert entry_count == 3
    finally:
        conn.close()


def test_post_fixture_matches_handler_output(db_module):
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        rows = _milestone_rows(conn)
        assert len(rows) == 3
        # threshold=1: cumulative=0.025, marginal=cumulative (first
        # row of week).
        assert rows[0] == (1, pytest.approx(0.025, abs=1e-9),
                           pytest.approx(0.025, abs=1e-9), 0)
        # threshold=2: cumulative=0.050, marginal=0.025.
        assert rows[1] == (2, pytest.approx(0.050, abs=1e-9),
                           pytest.approx(0.025, abs=1e-9), 0)
        # threshold=3: cumulative=0.075, marginal=0.025.
        assert rows[2] == (3, pytest.approx(0.075, abs=1e-9),
                           pytest.approx(0.025, abs=1e-9), 0)

        marker = conn.execute(
            "SELECT name FROM schema_migrations "
            "WHERE name = '010_recompute_percent_milestones_dedup_fix'"
        ).fetchone()
        assert marker is not None
    finally:
        conn.close()


def test_migration_handler_recomputes_cumulative_and_marginal(
    db_module, tmp_path, monkeypatch
):
    """Run the production handler against a copy of pre.sqlite with the
    paired pre-cache.sqlite wired in. Result must match post.sqlite.

    Key assertion: marginal for row 1 = cumulative (first row of week);
    row 2 marginal = cum[2] - cum[1]; row 3 marginal = cum[3] - cum[2].
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
        db_module._stamp_applied(conn, "010_recompute_percent_milestones_dedup_fix")  # dispatcher now owns the stamp (#140)
        rows = _milestone_rows(conn)
        # threshold=1: cumulative=0.025, marginal=cumulative.
        assert rows[0][0] == 1
        assert rows[0][1] == pytest.approx(0.025, abs=1e-9)
        assert rows[0][2] == pytest.approx(0.025, abs=1e-9)
        # threshold=2: cumulative=0.050, marginal=0.025.
        assert rows[1][0] == 2
        assert rows[1][1] == pytest.approx(0.050, abs=1e-9)
        assert rows[1][2] == pytest.approx(0.025, abs=1e-9)
        # threshold=3: cumulative=0.075, marginal=0.025.
        assert rows[2][0] == 3
        assert rows[2][1] == pytest.approx(0.075, abs=1e-9)
        assert rows[2][2] == pytest.approx(0.025, abs=1e-9)

        applied = conn.execute(
            "SELECT applied_at_utc FROM schema_migrations "
            "WHERE name = '010_recompute_percent_milestones_dedup_fix'"
        ).fetchone()
        assert applied and applied[0]
    finally:
        conn.close()


def test_migration_handler_idempotent_against_marker(
    db_module, tmp_path, monkeypatch
):
    """Second invocation silently no-ops on the marker INSERT — same
    INSERT OR IGNORE race-safety contract as 008/009."""
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
        db_module._stamp_applied(conn, "010_recompute_percent_milestones_dedup_fix")  # dispatcher now owns the stamp (#140)
        handler(conn)
        db_module._stamp_applied(conn, "010_recompute_percent_milestones_dedup_fix")
        cnt = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name = '010_recompute_percent_milestones_dedup_fix'"
        ).fetchone()[0]
        assert cnt == 1
    finally:
        conn.close()
