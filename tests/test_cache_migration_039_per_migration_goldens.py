"""Per-migration goldens for the Codex ``observed_model`` backfill.

Public issue omrikais/cctally#5. The migration is what makes
``quota_window_snapshots`` the complete dependency set the change ledger needs:
with a NULL ``observed_model`` the loader used to fall back to the nearest
preceding ``codex_session_entries.model``, so an accounting row arriving later
could move a window into a different model pool with no quota-row mutation for
the ledger to record.

The behavioural equivalence of the backfill against that removed expression is
proved in ``tests/test_codex_quota_observed_model_backfill.py``, which holds a
frozen copy of it. This module pins the on-disk before/after and the handler's
edge behaviour.
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "039_codex_quota_observed_model_backfill"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "039_codex_quota_observed_model_backfill"
SPARK = "gpt-5.3-codex-spark"


@pytest.fixture(scope="module")
def cctally_module():
    from importlib.machinery import SourceFileLoader

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _handler(cctally_module):
    for migration in cctally_module._CACHE_MIGRATIONS:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"{MIGRATION} not registered")


def _models(conn):
    return list(conn.execute(
        "SELECT line_offset, observed_model FROM quota_window_snapshots "
        "WHERE source='codex' ORDER BY line_offset"))


def test_pre_fixture_is_a_038_head_install_with_unstamped_rows(cctally_module):
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 38
        assert _models(conn) == [(10, None), (30, None), (40, "gpt-5")]
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_change_log").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_resolves_exactly_the_rows_the_fallback_did(cctally_module):
    """One row per outcome: resolved, left NULL, left alone."""
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 39
        assert _models(conn) == [
            # nothing precedes it -> NULL, never fabricated
            (10, None),
            # the Spark entry at offset 20 precedes it
            (30, SPARK),
            # already stamped -> untouched
            (40, "gpt-5"),
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_backfills_own_dml_is_ledgered(cctally_module):
    """Migration 028 rewrote this same column and announced nothing.

    Under the ledger the rewrite dirties its windows automatically, with no rule
    for the migration's author to remember — which is the property that makes
    the ledger a mechanism rather than a convention.
    """
    conn = sqlite3.connect(POST_DB)
    try:
        rows = list(conn.execute(
            "SELECT op, new_canonical_resets_at_utc "
            "FROM quota_window_change_log ORDER BY seq"))
    finally:
        conn.close()

    assert [row[0] for row in rows] == ["update"]
    assert rows[0][1] == "2026-07-31T15:00:00Z"


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        first = _models(conn)
        ledger_after_first = conn.execute(
            "SELECT COUNT(*) FROM quota_window_change_log").fetchone()[0]
        handler(conn)
        assert _models(conn) == first
        # A second run must write nothing at all — not even a no-op UPDATE,
        # which would dirty the window again on every open.
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_change_log"
        ).fetchone()[0] == ledger_after_first
    finally:
        conn.close()


def test_handler_degrades_without_an_accounting_corpus(cctally_module, tmp_path):
    """No accounting table means the fallback could not have resolved anything
    either, so leaving every row NULL IS the equivalent result."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        conn.execute("DROP TABLE codex_session_entries")
        conn.commit()
        _handler(cctally_module)(conn)
        assert _models(conn) == [(10, None), (30, None), (40, "gpt-5")]
    finally:
        conn.close()
