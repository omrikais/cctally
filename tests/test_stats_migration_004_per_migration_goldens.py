"""Per-migration goldens for stats migration
``004_heal_forked_week_start_date_buckets`` (#279 S7 W3 backfill).

pre.sqlite: a FORKED ``weekly_usage_snapshots`` row whose ``week_start_date``
(host-TZ contamination) disagrees with ``substr(week_start_at, 1, 10)``, plus a
canonical row for the same physical week. post.sqlite: the fork healed +
the 004 marker. Pure stats handler — no cache, no clock.
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
    / "004_heal_forked_week_start_date_buckets"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"


@pytest.fixture
def ns():
    return load_script()


def _handler(ns):
    for m in ns["_STATS_MIGRATIONS"]:
        if m.name == "004_heal_forked_week_start_date_buckets":
            return m.handler
    raise AssertionError("stats migration 004 not registered")


def _dates(conn):
    return conn.execute(
        "SELECT week_start_date, week_end_date FROM weekly_usage_snapshots "
        "ORDER BY id"
    ).fetchall()


def test_pre_fixture_has_forked_row(ns):
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        forked = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_at IS NOT NULL "
            "  AND week_start_date != substr(week_start_at, 1, 10)"
        ).fetchone()[0]
        assert forked == 1, "pre.sqlite must carry exactly one forked row"
    finally:
        conn.close()


def test_post_fixture_is_healed_with_marker(ns):
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        forked = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_at IS NOT NULL "
            "  AND week_start_date != substr(week_start_at, 1, 10)"
        ).fetchone()[0]
        assert forked == 0, "post.sqlite must have zero forked rows"
        # Both rows now bucket on the canonical UTC calendar day.
        assert _dates(conn) == [
            ("2026-04-13", "2026-04-20"),
            ("2026-04-13", "2026-04-20"),
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='004_heal_forked_week_start_date_buckets'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_heals_and_is_idempotent(ns, tmp_path):
    work = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _handler(ns)(conn)
        ns["_stamp_applied"](conn, "004_heal_forked_week_start_date_buckets")
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_at IS NOT NULL "
            "  AND week_start_date != substr(week_start_at, 1, 10)"
        ).fetchone()[0] == 0
        healed = _dates(conn)
        # Second invocation: empty-fork fast path → no-op, dates unchanged.
        _handler(ns)(conn)
        ns["_stamp_applied"](conn, "004_heal_forked_week_start_date_buckets")
        assert _dates(conn) == healed
    finally:
        conn.close()
