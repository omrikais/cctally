"""P1#3 guard — the Codex cache purge is one-shot and NEVER re-fires on a
legitimate ``last_total_tokens IS NULL`` row (cctally-dev#93, spec D4/P1#3,
plan Task 7 Step 3 / test 5d′).

Background
----------
``open_cache_db`` carries a one-time Codex-cache purge keyed on
``add_column_if_missing(conn, "codex_session_files", "last_total_tokens",
"INTEGER")`` *returning True* (i.e. the column was just added — the first
open after the upgrade that introduced the column):

    if add_column_if_missing(conn, "codex_session_files",
                             "last_total_tokens", "INTEGER"):
        conn.execute("DELETE FROM codex_session_entries")
        conn.execute("DELETE FROM codex_session_files")
        conn.commit()

A Codex review (pre-plan round 1, P1#3) rejected swapping that trigger for
a data-driven ``WHERE last_total_tokens IS NULL`` probe: ``sync_codex_cache``
LEGITIMATELY persists ``last_total_tokens = NULL`` on post-upgrade rows
(``new_last_total_tokens = running_total if yielded_count > 0 else
prev_total_tokens``, and ``prev_total_tokens`` can be ``NULL``). A NULL
probe would re-purge a healthy Codex cache on EVERY open. The
``add_column``-return trigger is kept BECAUSE it is one-shot.

cctally-dev#93 D4 moved the rest of the cache.db schema into the shared
``_apply_cache_schema`` helper but DELIBERATELY left this Codex
``last_total_tokens`` ALTER (and its purge) in ``open_cache_db``, out of
the shared helper. This test pins that the trigger stayed one-shot
through that refactor: a second ``open_cache_db`` over a populated,
already-migrated cache.db with a legitimate ``last_total_tokens IS NULL``
row does NOT re-purge.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths  # noqa: E402


def test_codex_purge_does_not_refire_on_null_last_total_tokens(
    tmp_path, monkeypatch
):
    """Second ``open_cache_db`` must NOT re-purge a populated Codex cache
    whose ``codex_session_files.last_total_tokens IS NULL`` (a legitimate
    post-upgrade state ``sync_codex_cache`` produces)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    open_cache_db = ns["open_cache_db"]

    # First open: creates the schema and runs the one-time
    # last_total_tokens ALTER + purge. The cache is empty here, so the
    # purge is a no-op DELETE — but the column now exists.
    conn = open_cache_db()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(codex_session_files)")}
        assert "last_total_tokens" in cols, (
            "first open_cache_db must add codex_session_files.last_total_tokens"
        )

        # Seed a populated Codex cache with a LEGITIMATE NULL
        # last_total_tokens row (the exact state P1#3 says sync_codex_cache
        # persists) + a matching entry. If the purge were data-driven on
        # NULL, the next open would wipe both.
        conn.execute(
            "INSERT INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " last_session_id, last_model, last_total_tokens) "
            "VALUES ('/tmp/codex-sess.jsonl', 100, 0, 100, "
            " '2026-05-22T00:00:00Z', 'sess-c', 'gpt-5', NULL)"
        )
        conn.execute(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            " input_tokens, cached_input_tokens, output_tokens, "
            " reasoning_output_tokens, total_tokens) "
            "VALUES ('/tmp/codex-sess.jsonl', 0, '2026-05-22T00:00:00Z', "
            " 'sess-c', 'gpt-5', 100, 0, 50, 0, 150)"
        )
        conn.commit()
    finally:
        conn.close()

    # Second open: the column already exists →
    # ``add_column_if_missing`` returns False → the purge branch is NOT
    # taken → the seeded rows survive.
    conn2 = open_cache_db()
    try:
        files = conn2.execute(
            "SELECT COUNT(*) FROM codex_session_files"
        ).fetchone()[0]
        entries = conn2.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone()[0]
        null_row = conn2.execute(
            "SELECT last_total_tokens FROM codex_session_files "
            "WHERE path = '/tmp/codex-sess.jsonl'"
        ).fetchone()
    finally:
        conn2.close()

    assert files == 1, (
        "P1#3: a legitimate last_total_tokens IS NULL row must NOT be "
        f"re-purged on the second open_cache_db; got {files} codex_session_files rows"
    )
    assert entries == 1, (
        "P1#3: the matching codex_session_entries row must also survive; "
        f"got {entries} rows"
    )
    assert null_row is not None and null_row[0] is None, (
        "the surviving row must keep its legitimate NULL last_total_tokens "
        f"(not be coerced or re-purged); got {null_row!r}"
    )
