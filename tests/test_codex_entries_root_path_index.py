"""Query-plan regression for the Codex per-file alias join.

``_codex_conversation_metadata`` runs ``_CODEX_FILE_ALIAS_SQL`` on EVERY dashboard
snapshot build. The join predicate is ``(source_root_key, source_path)``, but the
only index that used to cover it was the single-column
``idx_codex_entries_source_root``. Every Codex rollout on a machine normally
resolves to ONE provider root, so that predicate matches every row and the join
degenerates to ``files x entries``: measured against a real store (2,324 files,
151,903 entries) the single query took 57.2s of an 84s snapshot build, which the
dashboard's work-proportional cooldown then doubled into a ~170s staleness
window.

The composite ``idx_codex_entries_root_path`` restores a linear plan (same store:
57.20s -> 0.208s). These tests pin the PLAN, not a timing: the discriminator is
whether the ``codex_session_entries`` search node constrains ``source_path`` too,
which is exactly what a root-only index cannot do.
"""
from __future__ import annotations

import sqlite3
import sys

import pytest

INDEX = "idx_codex_entries_root_path"
ENTRY_COLUMNS = ("source_root_key", "source_path")


@pytest.fixture()
def cache_conn(cctally_module, tmp_path):
    """A fresh cache.db carrying the current version-gated schema path.

    Fresh stores gain the index through ``_apply_cache_schema``. Existing stores
    gain it through migration 042, which calls the same ensure helper; the schema
    delivery tests pin that provenance. This fixture exercises only the fresh
    path and query plan, not the migration path.
    """
    conn = sqlite3.connect(tmp_path / "cache.db")
    sys.modules["_cctally_db"]._apply_cache_schema(conn)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _plan_rows(conn, sql):
    return conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()


def _entries_search_node(conn, sql):
    """The plan node that reads the ``codex_session_entries`` side of the join.

    SQLite names the node by the query's ALIAS (``e``), not by the table, so that
    is what this matches. The alias cannot drift away from the assertion because
    the test imports the same module constant the production code executes.
    """
    for row in _plan_rows(conn, sql):
        detail = str(row[-1])
        if detail.startswith(("SEARCH e ", "SCAN e ")) or detail in ("SCAN e",):
            return detail
    raise AssertionError(
        "no plan node reads the entries side of the join:\n"
        + "\n".join(str(r[-1]) for r in _plan_rows(conn, sql))
    )


def _seed(conn, *, files=3, entries_per_file=4):
    """Enough rows that the planner has something real to choose over."""
    for f in range(files):
        path = f"/roots/main/rollout-{f}.jsonl"
        conn.execute(
            "INSERT INTO codex_session_files"
            "(source_root_key, path, size_bytes, mtime_ns, last_byte_offset,"
            " last_ingested_at, last_native_thread_id, last_session_id)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                "root-main", path, 1024, 1_000 + f, 1024,
                f"2026-08-13T0{f}:00:00Z", f"thread-{f}", f"session-{f}",
            ),
        )
        for e in range(entries_per_file):
            conn.execute(
                "INSERT INTO codex_session_entries"
                "(source_root_key, source_path, line_offset, timestamp_utc,"
                " session_id, model) VALUES(?,?,?,?,?,?)",
                (
                    "root-main", path, e, f"2026-08-13T0{f}:0{e}:00Z",
                    f"session-{f}", "gpt-5",
                ),
            )
    conn.commit()


def test_index_is_created_by_the_fresh_schema_path(cache_conn):
    """A fresh schema gains the same index migration 042 delivers on upgrade."""
    names = {
        r[0] for r in cache_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert INDEX in names, (
        f"{INDEX} must be created by _apply_cache_schema for fresh caches; "
        "migration 042 separately covers existing caches"
    )


def test_index_columns_are_root_then_path_in_that_order(cache_conn):
    """Column ORDER is the whole point: ``(source_path, source_root_key)`` would
    not serve a root-qualified prefix scan, and the root-only index already
    proved that a leading low-cardinality column is useless here."""
    cols = tuple(
        r[2] for r in cache_conn.execute(f"PRAGMA index_info('{INDEX}')")
    )
    assert cols == ENTRY_COLUMNS, f"expected {ENTRY_COLUMNS}, got {cols}"


def test_file_alias_plan_constrains_source_path(cache_conn, cctally_module):
    """The RED lever.

    Before the index the entries node reads ``SEARCH e USING INDEX
    idx_codex_entries_source_root (source_root_key=?)`` — root-only, so with one
    provider root it visits every entry row per file. Asserting that
    ``source_path`` is constrained is the discriminator; asserting merely "not a
    SCAN" would pass on the broken plan, since the root-only index still
    registers as a SEARCH.
    """
    import _cctally_dashboard_sources as ds

    _seed(cache_conn)
    node = _entries_search_node(cache_conn, ds._CODEX_FILE_ALIAS_SQL)
    assert "source_path=?" in node, (
        "the codex_session_entries join must be constrained on source_path, "
        f"otherwise it degenerates to files x entries. plan node was: {node}"
    )
    assert INDEX in node, f"expected the composite index in the plan, got: {node}"


def test_production_query_still_returns_the_alias_rows(cache_conn):
    """Non-vacuity guard: the SQL the plan test pins must actually select rows,
    so a future edit that empties the result cannot leave the plan assertion
    passing over nothing."""
    import _cctally_dashboard_sources as ds

    _seed(cache_conn)
    rows = tuple(cache_conn.execute(ds._CODEX_FILE_ALIAS_SQL))
    assert len(rows) == 3
    for root_key, path, native_thread_id, _session_id, first_seen in rows:
        assert root_key == "root-main"
        assert path.startswith("/roots/main/rollout-")
        assert native_thread_id.startswith("thread-")
        assert first_seen is not None
