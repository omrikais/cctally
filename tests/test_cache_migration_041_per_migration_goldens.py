"""Per-migration goldens for the unresolved-`observed_model` partial index.

Public issue omrikais/cctally#5, Task 10a Step 14. `sync_codex_cache` runs
`backfill_codex_quota_observed_model` at its tail on EVERY Codex sync — that is
what closes the #373 Spark-pool hole on a `db skip 039` install and on a cache
repopulated from the journal — and its
`WHERE source='codex' AND observed_model IS NULL` had only the source-leading
unique index to work with. Measured on the real 212,207-row store, which has
SIX unresolved rows: 39.8ms per tick, best of five, to find nothing.

The index body holds only unresolved rows, so it costs essentially nothing to
maintain and turns the recurring scan into a seek — 0.02ms best-of-seven on the
same store, with the plan flipping to `SEARCH … USING INDEX
idx_qws_unresolved_model`. That is why the assertions below check the PARTIAL
predicate and the planner's choice, not merely the index's existence: an index
whose `WHERE` does not match the reader's is never used and is pure write cost.
"""
from __future__ import annotations

import importlib.util as ilu
import sqlite3
import sys
from pathlib import Path

import pytest


IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "041_codex_quota_unresolved_model_index"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "041_codex_quota_unresolved_model_index"
INDEX = "idx_qws_unresolved_model"
EXPECTED_PARTIAL = "WHERE observed_model IS NULL"


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


def _index_sql(conn, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    return None if row is None else str(row[0])


def test_pre_fixture_is_a_040_head_install_without_the_index(cctally_module):
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 40
        assert _index_sql(conn, INDEX) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 0
        # Non-vacuity: an index over an empty table proves nothing.
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] == 2
    finally:
        conn.close()


def test_post_fixture_carries_the_partial_index(cctally_module):
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 41
        sql = _index_sql(conn, INDEX)
        assert sql is not None
        assert EXPECTED_PARTIAL in sql, (
            "without the partial predicate the index covers every row and "
            "buys nothing over the existing source-leading one")
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_planner_actually_picks_it_for_the_backfill_predicate(cctally_module):
    """A shape assertion is not enough — SQLite has to choose it.

    A partial index is used only when the query's WHERE implies the index's, so
    a predicate that drifts from the backfill's leaves this silently unused and
    the 212K-row scan back.

    Asserted against the PRODUCTION statement, not a hand-written SELECT proxy.
    A proxy passes for as long as someone keeps it in sync with the real
    `UPDATE`, which is precisely the drift the assertion exists to catch — the
    same reason `tests/test_cache_migration_022_*` plans its production SQL.
    """
    import _cctally_db

    prod_sql = _cctally_db._QUOTA_OBSERVED_MODEL_BACKFILL_SQL
    conn = sqlite3.connect(POST_DB)
    try:
        plan = " ".join(str(row[3]) for row in conn.execute(
            "EXPLAIN QUERY PLAN " + prod_sql))
    finally:
        conn.close()
    assert INDEX in plan, plan


def test_the_handler_is_idempotent(cctally_module, tmp_path):
    import shutil

    target = tmp_path / "cache.db"
    shutil.copyfile(PRE_DB, target)
    conn = sqlite3.connect(target)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        first = _index_sql(conn, INDEX)
        handler(conn)
        assert _index_sql(conn, INDEX) == first
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
