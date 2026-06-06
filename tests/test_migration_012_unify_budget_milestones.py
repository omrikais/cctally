"""Unit tests for stats migration 012_unify_budget_milestones_vendor (#143)."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


def _handler(ns, name):
    for m in ns["_STATS_MIGRATIONS"]:
        if m.name == name:
            return m.handler
    raise AssertionError(f"migration {name} not registered")


def _v011_db(path: Path) -> None:
    """A v011-shape stats.db: post-011 budget/codex tables (period column,
    period-inclusive UNIQUE) with seeded crossings in BOTH."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL);
            CREATE TABLE budget_milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start_at TEXT NOT NULL, period TEXT, threshold INTEGER NOT NULL,
                budget_usd REAL NOT NULL, spent_usd REAL NOT NULL, consumption_pct REAL NOT NULL,
                crossed_at_utc TEXT NOT NULL, alerted_at TEXT,
                UNIQUE(week_start_at, period, threshold));
            CREATE TABLE codex_budget_milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_start_at TEXT NOT NULL, period TEXT, threshold INTEGER NOT NULL,
                budget_usd REAL NOT NULL, spent_usd REAL NOT NULL, consumption_pct REAL NOT NULL,
                crossed_at_utc TEXT NOT NULL, alerted_at TEXT,
                UNIQUE(period_start_at, period, threshold));
            INSERT INTO budget_milestones
              (week_start_at, period, threshold, budget_usd, spent_usd, consumption_pct, crossed_at_utc, alerted_at)
              VALUES ('2026-06-01T00:00:00+00:00','subscription-week',90,100.0,95.0,95.0,'2026-06-02T00:00:00Z','2026-06-02T00:00:00Z');
            INSERT INTO codex_budget_milestones
              (period_start_at, period, threshold, budget_usd, spent_usd, consumption_pct, crossed_at_utc, alerted_at)
              VALUES ('2026-06-01T00:00:00+00:00','calendar-month',100,200.0,210.0,105.0,'2026-06-03T00:00:00Z','2026-06-03T00:00:00Z');
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_012_merges_both_vendors(tmp_path):
    ns = load_script()
    db = tmp_path / "stats.db"
    _v011_db(db)
    conn = sqlite3.connect(db)
    try:
        _handler(ns, "012_unify_budget_milestones_vendor")(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(budget_milestones)")}
        assert "vendor" in cols and "period_start_at" in cols and "week_start_at" not in cols
        rows = conn.execute(
            "SELECT vendor, period_start_at, period, threshold, budget_usd, alerted_at "
            "FROM budget_milestones ORDER BY vendor"
        ).fetchall()
        assert rows == [
            ("claude", "2026-06-01T00:00:00+00:00", "subscription-week", 90, 100.0, "2026-06-02T00:00:00Z"),
            ("codex",  "2026-06-01T00:00:00+00:00", "calendar-month",   100, 200.0, "2026-06-03T00:00:00Z"),
        ]
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_budget_milestones'"
        ).fetchone() is None
        # Idempotent re-run is a no-op.
        _handler(ns, "012_unify_budget_milestones_vendor")(conn)
        assert conn.execute("SELECT COUNT(*) FROM budget_milestones").fetchone()[0] == 2
    finally:
        conn.close()


def test_012_codex_absorb_when_claude_already_unified(tmp_path):
    """Partial-state repair: budget_milestones already unified, codex leftover."""
    ns = load_script()
    db = tmp_path / "stats.db"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE budget_milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT, vendor TEXT NOT NULL,
                period_start_at TEXT NOT NULL, period TEXT, threshold INTEGER NOT NULL,
                budget_usd REAL NOT NULL, spent_usd REAL NOT NULL, consumption_pct REAL NOT NULL,
                crossed_at_utc TEXT NOT NULL, alerted_at TEXT,
                UNIQUE(vendor, period_start_at, period, threshold));
            INSERT INTO budget_milestones (vendor, period_start_at, period, threshold, budget_usd, spent_usd, consumption_pct, crossed_at_utc, alerted_at)
              VALUES ('claude','2026-06-01T00:00:00+00:00','subscription-week',90,100.0,95.0,95.0,'x','x');
            CREATE TABLE codex_budget_milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT, period_start_at TEXT NOT NULL, period TEXT,
                threshold INTEGER NOT NULL, budget_usd REAL NOT NULL, spent_usd REAL NOT NULL,
                consumption_pct REAL NOT NULL, crossed_at_utc TEXT NOT NULL, alerted_at TEXT,
                UNIQUE(period_start_at, period, threshold));
            INSERT INTO codex_budget_milestones (period_start_at, period, threshold, budget_usd, spent_usd, consumption_pct, crossed_at_utc, alerted_at)
              VALUES ('2026-06-01T00:00:00+00:00','calendar-month',100,200.0,210.0,105.0,'y','y');
            """
        )
        conn.commit()
        _handler(ns, "012_unify_budget_milestones_vendor")(conn)
        vendors = [r[0] for r in conn.execute("SELECT vendor FROM budget_milestones ORDER BY vendor")]
        assert vendors == ["claude", "codex"]  # claude NOT re-copied, codex absorbed
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_budget_milestones'"
        ).fetchone() is None
    finally:
        conn.close()


def test_011_skips_missing_codex_table(tmp_path):
    """011 hardening: a DB with NO codex_budget_milestones must not raise."""
    ns = load_script()
    db = tmp_path / "stats.db"
    conn = sqlite3.connect(db)
    try:
        # Pre-011 budget + projected ONLY (no codex table — predates the feature).
        conn.executescript(
            """
            CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL);
            CREATE TABLE budget_milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT, week_start_at TEXT NOT NULL, threshold INTEGER NOT NULL,
                budget_usd REAL NOT NULL, spent_usd REAL NOT NULL, consumption_pct REAL NOT NULL,
                crossed_at_utc TEXT NOT NULL, alerted_at TEXT, UNIQUE(week_start_at, threshold));
            CREATE TABLE projected_milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT, week_start_at TEXT NOT NULL, metric TEXT NOT NULL,
                threshold INTEGER NOT NULL, projected_value REAL NOT NULL, denominator REAL NOT NULL,
                crossed_at_utc TEXT NOT NULL, alerted_at TEXT, UNIQUE(week_start_at, metric, threshold));
            """
        )
        conn.commit()
        _handler(ns, "011_budget_milestone_period_keys")(conn)  # must NOT raise
        for t in ("budget_milestones", "projected_milestones"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            assert "period" in cols
    finally:
        conn.close()
