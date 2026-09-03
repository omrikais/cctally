"""Schema shape for the unified Anthropic credit record (#703 + #707).

``week_reset_events`` is now the single durable representation of an Anthropic
weekly credit, so it must carry the facts a credit has: the week it belongs to,
the exact capture-domain instant the post-credit state was first observed, the
capture instant of the observation that confirmed it, the level the counter
landed on, and a durable identity derived from the source journal record rather
than from the week boundaries.

The two boundary columns become nullable because a cutover-exported
``weekly_credit_floor`` op has no week-end timestamp to supply
(``weekly_credit_floors`` retains none), and NULL is the honest value for a row
that never had them.

A stats schema change is an epoch bump and never a migration: the 13-entry
legacy registry is frozen, and an epoch-current open returns before any schema
work, so a ``@stats_migration`` handler would never run on an upgraded install.
See docs/superpowers/specs/2026-09-02-703-707-anthropic-same-window-credit.md §7.
"""
from __future__ import annotations

import pytest

import _cctally_core

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


@pytest.fixture
def conn(ns):
    conn = ns["open_db"]()
    try:
        yield conn
    finally:
        conn.close()


CREDIT_FACT_COLUMNS = (
    "week_start_date",
    "observed_at_utc",
    "confirming_capture_at_utc",
    "observed_post_credit_pct",
    "credit_key",
    "credit_order",
)


def test_week_reset_events_carries_the_credit_fact_columns(conn):
    cols = {r["name"]: r for r in conn.execute(
        "PRAGMA table_info(week_reset_events)").fetchall()}
    missing = [name for name in CREDIT_FACT_COLUMNS if name not in cols]
    assert not missing, f"missing from week_reset_events: {missing}"


def test_the_two_boundary_columns_are_nullable(conn):
    cols = {r["name"]: r for r in conn.execute(
        "PRAGMA table_info(week_reset_events)").fetchall()}
    assert cols["old_week_end_at"]["notnull"] == 0
    assert cols["new_week_end_at"]["notnull"] == 0


def test_a_row_with_null_boundaries_is_accepted(conn):
    """The cutover-exported shape: no week-end timestamps to supply."""
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, week_start_date, credit_key, credit_order) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-09-01T17:59:47Z", None, None, "2026-09-01T17:00:00+00:00",
         "2026-08-29", "legacy:o:abc", 1),
    )
    row = conn.execute(
        "SELECT old_week_end_at, new_week_end_at, week_start_date, credit_key "
        "FROM week_reset_events").fetchone()
    assert row["old_week_end_at"] is None
    assert row["new_week_end_at"] is None
    assert row["week_start_date"] == "2026-08-29"
    assert row["credit_key"] == "legacy:o:abc"


def test_stats_index_epoch_is_bumped_for_the_credit_schema():
    assert _cctally_core.STATS_INDEX_EPOCH == 1012
