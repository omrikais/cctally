"""Per-migration goldens for cache migration
``008_session_entries_speed_backfill`` (#181).

Loads ``tests/fixtures/migrations/per-migration/008_session_entries_speed_backfill/pre.sqlite``
(an existing install at the 007 head: cache migrations 001-007 applied, the
``speed`` column PRESENT but NULL on a single legacy ``session_entries`` row
whose ``usage_extra_json`` still carries ``{"speed":"fast"}``, no 008 marker),
runs the production 008 handler against a copy, and asserts the result matches
``post.sqlite``.

008 materializes the only-ever-consumed non-token ``usage`` key (``speed``) into
its own column so the hot cache read paths stop ``json.loads``-ing the
deeply-nested ``usage_extra_json`` blob per row (the per-tick dashboard rebuild
was pegging a core on a ~261K-row cache). The handler runs ONE C-side
``UPDATE … SET speed = json_extract(usage_extra_json, '$.speed') WHERE speed IS
NULL AND usage_extra_json IS NOT NULL``. It deliberately does NOT rewrite/NULL
``usage_extra_json`` on existing rows or VACUUM — the stale blob has no reader
and is reclaimed on the next ``cache-sync --rebuild``. ``WHERE speed IS NULL``
self-guards re-runs. The dispatcher central-stamps the migration marker (#140).

NOTE: this is the CACHE migration 008 — a DISTINCT sequence from the STATS
migration ``008_recompute_weekly_cost_snapshots_dedup_fix`` (pinned by
``tests/test_migration_008_per_migration_goldens.py``). Both legitimately carry
the number 008 because the stats.db and cache.db migration registries are
independent.

Because the column-add lives in ``_apply_cache_schema`` (which runs BEFORE the
dispatcher in production), pre.sqlite MUST already carry the ``speed`` column at
its pre-backfill state — otherwise calling the handler ALONE would hit
``no such column: speed``. The builder uses ``_apply_cache_schema`` to guarantee
that.
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
    / "008_session_entries_speed_backfill"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "008_session_entries_speed_backfill"


@pytest.fixture(scope="module")
def cctally_module():
    """Load bin/cctally once per module (registers the cache migrations).
    bin/cctally has no ``.py`` suffix, so an explicit ``SourceFileLoader`` is
    required."""
    from importlib.machinery import SourceFileLoader

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _migration_handler(cctally_module):
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def test_pre_fixture_has_speed_column_unbackfilled(cctally_module):
    """Sanity: pre.sqlite carries the ``speed`` column (so the backfill UPDATE
    can run), the single legacy row has ``speed IS NULL`` while its
    ``usage_extra_json`` still holds ``{"speed":"fast"}``, has 007 applied, and
    has NOT the 008 marker — the existing-install shape before the materialize
    backfill."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        # The speed column MUST exist (else the handler errors).
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(session_entries)"
        )}
        assert "speed" in cols, "pre fixture must carry the speed column"
        rows = conn.execute(
            "SELECT speed, usage_extra_json FROM session_entries"
        ).fetchall()
        assert len(rows) == 1
        speed, usage_extra_json = rows[0]
        assert speed is None, "pre fixture row's speed column must be NULL"
        assert usage_extra_json == '{"speed": "fast"}', (
            "pre fixture must keep the legacy blob carrying the speed value"
        )
        # 007 applied, 008 not yet.
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='007_conversation_reingest_enrichment'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_has_speed_backfilled_blob_unchanged(cctally_module):
    """Sanity: post.sqlite has ``speed='fast'`` materialized into the column,
    the legacy ``usage_extra_json`` blob UNCHANGED (the handler never NULLs/
    rewrites it), and the 008 marker stamped."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        rows = conn.execute(
            "SELECT speed, usage_extra_json FROM session_entries"
        ).fetchall()
        assert len(rows) == 1
        speed, usage_extra_json = rows[0]
        assert speed == "fast", "handler must backfill speed='fast'"
        assert usage_extra_json == '{"speed": "fast"}', (
            "handler must leave usage_extra_json UNCHANGED (no NULL/rewrite)"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_backfills_speed(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite; it must backfill the
    ``speed`` column from the legacy blob while leaving ``usage_extra_json``
    UNCHANGED, then stamp the 008 marker."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        pre_blob = conn.execute(
            "SELECT usage_extra_json FROM session_entries"
        ).fetchone()[0]

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)

        speed, usage_extra_json = conn.execute(
            "SELECT speed, usage_extra_json FROM session_entries"
        ).fetchone()
        assert speed == "fast", "handler must materialize speed from the blob"
        assert usage_extra_json == pre_blob, (
            "handler must NOT touch usage_extra_json"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    """A second handler run must not raise and must leave ``speed='fast'`` (the
    ``WHERE speed IS NULL`` guard makes the re-run a no-op), with the blob still
    unchanged."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _migration_handler(cctally_module)
        handler(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)
        handler(conn)  # must not raise, must not re-touch the already-set row
        cctally_module._stamp_applied(conn, _MIGRATION)
        speed, usage_extra_json = conn.execute(
            "SELECT speed, usage_extra_json FROM session_entries"
        ).fetchone()
        assert speed == "fast"
        assert usage_extra_json == '{"speed": "fast"}'
    finally:
        conn.close()
