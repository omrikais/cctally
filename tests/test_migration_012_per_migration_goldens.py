"""Per-migration golden for 012_unify_budget_milestones_vendor (#143)."""
from __future__ import annotations
import shutil, sqlite3
from pathlib import Path
import pytest
from conftest import load_script

# W1 registry-completeness guard (#279 S7): declares this module exercises
# the handler's second-invocation idempotency (test names vary across modules).
IDEMPOTENCY_COVERED = True

FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "migrations"
           / "per-migration" / "012_unify_budget_milestones_vendor")
PRE_DB = FIXTURE / "pre.sqlite"


@pytest.fixture
def ns():
    return load_script()


def _handler(ns):
    for m in ns["_STATS_MIGRATIONS"]:
        if m.name == "012_unify_budget_milestones_vendor":
            return m.handler
    raise AssertionError("migration 012 not registered")


def test_pre_fixture_has_two_tables(ns):
    assert PRE_DB.exists()
    conn = sqlite3.connect(PRE_DB)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "codex_budget_milestones" in names
        cols = {r[1] for r in conn.execute("PRAGMA table_info(budget_milestones)")}
        assert "vendor" not in cols and "week_start_at" in cols
    finally:
        conn.close()


def test_012_unifies(ns, tmp_path):
    work = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _handler(ns)(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(budget_milestones)")}
        assert "vendor" in cols and "period_start_at" in cols and "week_start_at" not in cols
        vendors = sorted(r[0] for r in conn.execute("SELECT vendor FROM budget_milestones"))
        assert vendors == ["claude", "codex"]
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_budget_milestones'"
        ).fetchone() is None
        _handler(ns)(conn)  # idempotent
        assert sorted(r[0] for r in conn.execute("SELECT vendor FROM budget_milestones")) == ["claude", "codex"]
    finally:
        conn.close()
