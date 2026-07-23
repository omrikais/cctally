"""Task 8 Item 2 — §6.2 version gate over open_db's three open-time backfills.

A steady-state open must perform ZERO backfill probe/DDL/backfill work: the
five_hour_window_key backfill probe, the quota-projection schema apply, and the
historical five_hour_blocks rollup backfill (+ its migration-003 re-invocation)
run ONLY when the ``stats_open_fixups`` marker is absent/stale. Isolation mirrors
tests/test_writer_reroute.py: load_script() drops cached _cctally_* siblings, so
we grab fresh modules AFTER load_script(); redirect_paths pins the tmp data dir.
"""
from __future__ import annotations

import sqlite3

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


# SQL fragments that appear ONLY inside the three gated open-time backfills.
_BACKFILL_MARKERS = (
    # window-key backfill probe / UPDATE
    ("five_hour_resets_at IS NOT NULL", "five_hour_window_key IS NULL"),
    ("UPDATE weekly_usage_snapshots", "SET five_hour_window_key"),
    # historical five_hour_blocks backfill probes
    ("SELECT 1 FROM five_hour_blocks",),
    # quota-projection schema apply
    ("CREATE TABLE IF NOT EXISTS quota_window_blocks",),
)


def _is_backfill_stmt(sql: str) -> bool:
    return any(all(tok in sql for tok in toks) for toks in _BACKFILL_MARKERS)


def _trace_open(ns, monkeypatch):
    """Open stats.db once through a traced sqlite3.connect and return the list of
    executed statements captured on that connection."""
    seen: list[str] = []
    real_connect = sqlite3.connect

    def traced(*a, **k):
        conn = real_connect(*a, **k)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    conn = ns["open_db"]()
    conn.close()
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    return seen


def test_fresh_open_stamps_the_fixups_marker(ns):
    import _cctally_store as st
    conn = ns["open_db"]()
    try:
        assert st.stats_open_fixups_current(conn) is True
        row = conn.execute(
            "SELECT version FROM stats_open_fixups WHERE id = 1").fetchone()
        assert row is not None and int(row[0]) == st._STATS_OPEN_FIXUPS_VERSION
        # The quota schema (one of the three backfills) landed on the fresh open.
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='quota_window_blocks'").fetchone() is not None
    finally:
        conn.close()


def test_steady_state_open_runs_no_backfill_work(ns, monkeypatch):
    # First open runs (and gates) the backfills.
    ns["open_db"]().close()
    # The SECOND (steady-state) open must run NONE of the three backfills.
    seen = _trace_open(ns, monkeypatch)
    offenders = [s for s in seen if _is_backfill_stmt(s)]
    assert offenders == [], f"steady-state open ran backfill work: {offenders}"


def test_pre_gate_db_runs_each_backfill_once(ns, monkeypatch):
    # Build an initialized DB, then knock it back to a PRE-GATE state: drop the
    # fixups marker + the quota tables, null a window key (with a resets_at so the
    # probe re-runs), and clear the blocks so all three backfills have work.
    # Task 9: the epoch gate now SUBSUMES the fixups gate — a steady-state
    # (user_version == STATS_INDEX_EPOCH) open does zero schema work regardless of
    # the fixups marker. So a genuine pre-gate DB must ALSO be at a legacy
    # user_version (<= 13); re-opening then runs the schema apply (incl. the three
    # backfills) and cuts over to the epoch.
    conn = ns["open_db"]()
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, weekly_percent, source, "
        " payload_json, five_hour_percent, five_hour_resets_at, five_hour_window_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-01-04T09:00:00Z", "2026-01-01", "2026-01-08", 10.0, "test", "{}",
         20.0, "2026-01-04T14:00:00Z", 999),
    )
    conn.execute("DROP TABLE IF EXISTS stats_open_fixups")
    for t in ("quota_window_blocks", "quota_percent_milestones",
              "quota_threshold_events", "quota_projection_state",
              "quota_alert_arming"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute("UPDATE weekly_usage_snapshots SET five_hour_window_key = NULL")
    conn.execute("DELETE FROM five_hour_blocks")
    conn.execute("PRAGMA user_version = 13")  # legacy shape → schema apply re-runs
    conn.commit()
    conn.close()

    # Re-open: the pre-gate DB runs all three backfills exactly once.
    seen = _trace_open(ns, monkeypatch)
    ran = [toks for toks in _BACKFILL_MARKERS
           if any(all(t in s for t in toks) for s in seen)]
    # window-key probe/update + block probe + quota schema all fired.
    assert any("five_hour_window_key IS NULL" in " ".join(toks) for toks in ran)
    assert any("quota_window_blocks" in " ".join(toks) for toks in ran)

    conn = ns["open_db"]()
    try:
        import _cctally_store as st
        # Backfills completed: window key backfilled, quota tables recreated,
        # marker re-stamped.
        assert conn.execute(
            "SELECT five_hour_window_key FROM weekly_usage_snapshots"
        ).fetchone()[0] is not None
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='quota_window_blocks'"
        ).fetchone() is not None
        assert st.stats_open_fixups_current(conn) is True
    finally:
        conn.close()

    # And the NEXT open runs no backfill work again.
    seen2 = _trace_open(ns, monkeypatch)
    assert [s for s in seen2 if _is_backfill_stmt(s)] == []
