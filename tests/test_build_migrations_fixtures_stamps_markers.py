"""Regression guard for ``bin/build-migrations-fixtures.py`` (issue #194).

The per-migration golden builders call the production migration handler
directly (no copy-paste drift). Issue #140 moved the ``schema_migrations``
marker stamp OUT of the handlers and into the dispatcher's central
``_stamp_applied`` — so a builder that only calls ``handler(conn)`` no longer
produces the marker. The 002 builder stamped via an ``UPDATE`` that silently
matched zero rows post-#140; the 008 builder stamped nothing at all. A full
``build-migrations-fixtures.py`` regen therefore wrote markerless ``post.sqlite``
goldens, silently breaking ``test_migration_002`` / ``test_migration_008``
per-migration goldens — but ONLY on a regen, because those tests read the
COMMITTED fixtures and never exercise the builder.

This guard exercises the builder itself: it rebuilds the 002 and 008 goldens
into a throwaway dir and asserts

  * ``post.sqlite`` carries the migration's marker (the dispatcher's stamp,
    applied by the builder to mirror the handler tests), and
  * the 008 ``pre-cache.sqlite`` sidecar stays a clean pre-008 state (only the
    001 marker) — proving the builder runs the handler's eager cache-migration
    step against a COPY, never mutating the in-tree fixture.

Without these guards, the same drift would recur the next time the central
marker/stamp convention changes under a builder that bypasses the dispatcher.
"""
from __future__ import annotations

import importlib.util as ilu
import sqlite3
import sys
from pathlib import Path

import pytest


BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
BUILDER_PATH = BIN_DIR / "build-migrations-fixtures.py"


@pytest.fixture(scope="module")
def builder_module():
    """Import ``bin/build-migrations-fixtures.py`` so its per-migration build
    functions can be called against a tmp scenario dir."""
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    spec = ilu.spec_from_file_location(
        "build_migrations_fixtures", BUILDER_PATH
    )
    mod = ilu.module_from_spec(spec)
    sys.modules["build_migrations_fixtures"] = mod
    spec.loader.exec_module(mod)
    return mod


def _has_marker(db_path: Path, name: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
        ).fetchone() is not None
    finally:
        conn.close()


def _markers(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT name FROM schema_migrations ORDER BY name"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_builder_002_post_carries_marker(builder_module, tmp_path):
    """The 002 builder must stamp the 002 marker into post.sqlite — pre-fix the
    UPDATE matched zero rows post-#140, leaving it markerless (issue #194)."""
    scenario = tmp_path / "002_conversation_messages_backfill"
    builder_module.build_per_migration_002_conversation_messages_backfill(
        scenario
    )
    post = scenario / "post.sqlite"
    assert post.exists(), "builder did not write 002 post.sqlite"
    assert _has_marker(post, "002_conversation_messages_backfill"), (
        "002 post.sqlite must carry the 002 marker — the builder must apply "
        "the dispatcher's central _stamp_applied after the handler (issue #194)"
    )


def test_builder_008_post_carries_marker(builder_module, tmp_path):
    """The 008 builder must stamp the 008 marker into post.sqlite — pre-fix it
    stamped nothing post-#140, leaving it markerless (issue #194)."""
    scenario = tmp_path / "008_recompute_weekly_cost_snapshots_dedup_fix"
    builder_module.build_per_migration_008_recompute_weekly_cost_snapshots_dedup_fix(
        scenario
    )
    post = scenario / "post.sqlite"
    assert post.exists(), "builder did not write 008 post.sqlite"
    assert _has_marker(
        post, "008_recompute_weekly_cost_snapshots_dedup_fix"
    ), (
        "008 post.sqlite must carry the 008 marker — the builder must apply "
        "the dispatcher's central _stamp_applied after the handler (issue #194)"
    )


def test_builder_008_pre_cache_stays_clean(builder_module, tmp_path):
    """The 008 builder runs the handler's eager cache-migration step against a
    throwaway COPY, so the committed pre-cache sidecar stays a clean pre-008
    state — only the 001 marker, no downstream cache markers, and no stray
    work-cache file left behind (issue #194)."""
    scenario = tmp_path / "008_recompute_weekly_cost_snapshots_dedup_fix"
    builder_module.build_per_migration_008_recompute_weekly_cost_snapshots_dedup_fix(
        scenario
    )
    pre_cache = scenario / "pre-cache.sqlite"
    assert pre_cache.exists(), "builder did not write 008 pre-cache.sqlite"
    assert _markers(pre_cache) == ["001_dedup_highest_wins"], (
        "pre-cache.sqlite must carry ONLY the 001 marker (clean pre-008 cache "
        "state); any downstream cache marker means the builder ran the eager "
        "cache-migration step against the in-tree fixture instead of a copy "
        "(issue #194)"
    )
    # The throwaway cache copy must be cleaned up — the committed fixture dir
    # holds only pre/pre-cache/post .sqlite.
    assert not (scenario / "_work_cache.db").exists(), (
        "builder left its throwaway _work_cache.db behind"
    )
