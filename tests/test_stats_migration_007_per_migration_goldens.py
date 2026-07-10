"""Per-migration goldens for stats migration ``007_observed_pre_credit_pct``
(#279 S7 W3 backfill).

pre.sqlite is the hand-built pre-007 shape: a ``week_reset_events`` table
WITHOUT the ``observed_pre_credit_pct`` column. The handler is a simple ADD
COLUMN. post.sqlite carries the column (NULL on the existing row) + the 007
marker.

Non-colliding name: ``test_stats_migration_007_*`` (the plain
``test_migration_007_*`` slot belongs to a different migration under the
historical mixed convention — spec W3 naming-hazard note).
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


# Consumed by tests/test_migration_registry_completeness.py (W1): declares this
# module exercises the handler's idempotency (a second invocation is a no-op).
IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "007_observed_pre_credit_pct"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"


@pytest.fixture
def ns():
    return load_script()


def _handler(ns):
    for m in ns["_STATS_MIGRATIONS"]:
        if m.name == "007_observed_pre_credit_pct":
            return m.handler
    raise AssertionError("stats migration 007 not registered")


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_pre_fixture_lacks_the_column(ns):
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert "observed_pre_credit_pct" not in _cols(conn, "week_reset_events")
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='007_observed_pre_credit_pct'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_has_column_and_marker(ns):
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert "observed_pre_credit_pct" in _cols(conn, "week_reset_events")
        # Backfill is NULL on the existing row.
        assert conn.execute(
            "SELECT observed_pre_credit_pct FROM week_reset_events"
        ).fetchone() == (None,)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='007_observed_pre_credit_pct'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_adds_column_and_is_idempotent(ns, tmp_path):
    work = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _handler(ns)(conn)
        ns["_stamp_applied"](conn, "007_observed_pre_credit_pct")  # dispatcher owns the stamp (#140)
        assert "observed_pre_credit_pct" in _cols(conn, "week_reset_events")
        assert conn.execute(
            "SELECT observed_pre_credit_pct FROM week_reset_events"
        ).fetchone() == (None,)
        # Second invocation: column already present → no-op, no raise.
        _handler(ns)(conn)
        ns["_stamp_applied"](conn, "007_observed_pre_credit_pct")
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='007_observed_pre_credit_pct'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
