"""Regression test for U1: source_path stays pinned to whichever JSONL
first inserted a (msg_id, req_id) row, even when a later UPSERT from a
different file wins the higher-token contest.

The fix matters because downstream aggregators (e.g. `cctally project`)
attribute tokens via `LEFT JOIN session_files ON sf.path =
se.source_path`. If `source_path` flipped to the UPSERT winner's file,
the row's project_path would move with it — token usage would
silently migrate between project buckets on each dedup tiebreaker swap.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def cache_db(tmp_path):
    """Production-shape session_entries + partial UNIQUE index."""
    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE session_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            line_offset INTEGER NOT NULL,
            timestamp_utc TEXT NOT NULL,
            model TEXT NOT NULL,
            msg_id TEXT,
            req_id TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_create_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            usage_extra_json TEXT,
            cost_usd_raw REAL
        );
        CREATE UNIQUE INDEX idx_entries_dedup
            ON session_entries(msg_id, req_id)
            WHERE msg_id IS NOT NULL AND req_id IS NOT NULL;
    """)
    yield conn
    conn.close()


# Mirror the production UPSERT byte-identically. If this drifts, the dedup
# tests' UPSERT_SQL fixture is the canonical copy.
UPSERT_SQL = """
INSERT INTO session_entries (
    source_path, line_offset, timestamp_utc, model,
    msg_id, req_id, input_tokens, output_tokens,
    cache_create_tokens, cache_read_tokens,
    usage_extra_json, cost_usd_raw
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(msg_id, req_id)
WHERE msg_id IS NOT NULL AND req_id IS NOT NULL
DO UPDATE SET
    timestamp_utc = excluded.timestamp_utc,
    model = excluded.model,
    input_tokens = excluded.input_tokens,
    output_tokens = excluded.output_tokens,
    cache_create_tokens = excluded.cache_create_tokens,
    cache_read_tokens = excluded.cache_read_tokens,
    usage_extra_json = excluded.usage_extra_json,
    cost_usd_raw = excluded.cost_usd_raw
WHERE
    (excluded.input_tokens + excluded.output_tokens
     + excluded.cache_create_tokens + excluded.cache_read_tokens)
    >
    (session_entries.input_tokens + session_entries.output_tokens
     + session_entries.cache_create_tokens + session_entries.cache_read_tokens)
"""


def _row(source_path, msg_id, req_id, out_tokens, *, line_offset=0):
    return (
        source_path, line_offset, "2026-05-22T17:04:00Z", "claude-opus-4-7",
        msg_id, req_id, 0, out_tokens, 0, 0,
        json.dumps({}), None,
    )


def test_source_path_pinned_to_first_writer(cache_db):
    """File A inserts first; file B's higher-token UPSERT wins the contest
    but the row's source_path stays = A. (B's tokens land on A's row.)"""
    cache_db.execute(UPSERT_SQL, _row("/projects/A/a.jsonl", "m1", "r1", 1))
    cache_db.execute(UPSERT_SQL, _row("/projects/B/b.jsonl", "m1", "r1", 3881))
    rows = cache_db.execute(
        "SELECT source_path, output_tokens FROM session_entries"
    ).fetchall()
    assert rows == [("/projects/A/a.jsonl", 3881)], (
        "source_path must stay pinned to file A even though file B won the "
        "higher-token contest. project attribution depends on this."
    )


def test_source_path_stays_when_loser_arrives_second(cache_db):
    """File A inserts first with high tokens; file B's lower-token UPSERT is
    a no-op (WHERE clause prevents replace) — source_path stays = A."""
    cache_db.execute(UPSERT_SQL, _row("/projects/A/a.jsonl", "m1", "r1", 3881))
    cache_db.execute(UPSERT_SQL, _row("/projects/B/b.jsonl", "m1", "r1", 1))
    rows = cache_db.execute(
        "SELECT source_path, output_tokens FROM session_entries"
    ).fetchall()
    assert rows == [("/projects/A/a.jsonl", 3881)]


def test_line_offset_also_pinned(cache_db):
    """Companion invariant: line_offset is also sticky — it identifies the
    row's position WITHIN its originating file, so it must travel with
    source_path. Updating line_offset to excluded.line_offset would point
    into the WRONG file."""
    cache_db.execute(
        UPSERT_SQL, _row("/projects/A/a.jsonl", "m1", "r1", 1, line_offset=128)
    )
    cache_db.execute(
        UPSERT_SQL, _row("/projects/B/b.jsonl", "m1", "r1", 3881, line_offset=512)
    )
    rows = cache_db.execute(
        "SELECT source_path, line_offset, output_tokens FROM session_entries"
    ).fetchall()
    assert rows == [("/projects/A/a.jsonl", 128, 3881)]


def test_project_attribution_via_join(cache_db):
    """End-to-end: simulate the LEFT JOIN that `cctally project` uses.
    With source_path pinned to A, the winning tokens are attributed to
    project /projects/A — NOT /projects/B."""
    # Build a minimal session_files sidecar for the JOIN.
    cache_db.execute("""
        CREATE TABLE session_files (
            path TEXT PRIMARY KEY,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            mtime_ns INTEGER NOT NULL DEFAULT 0,
            last_byte_offset INTEGER NOT NULL DEFAULT 0,
            last_ingested_at TEXT NOT NULL DEFAULT '',
            session_id TEXT,
            project_path TEXT
        );
    """)
    cache_db.executemany(
        "INSERT INTO session_files(path, project_path) VALUES (?, ?)",
        [
            ("/projects/A/a.jsonl", "/home/u/project-A"),
            ("/projects/B/b.jsonl", "/home/u/project-B"),
        ],
    )
    cache_db.execute(UPSERT_SQL, _row("/projects/A/a.jsonl", "m1", "r1", 1))
    cache_db.execute(UPSERT_SQL, _row("/projects/B/b.jsonl", "m1", "r1", 3881))

    rows = cache_db.execute(
        "SELECT sf.project_path, se.output_tokens "
        "FROM session_entries se "
        "LEFT JOIN session_files sf ON sf.path = se.source_path"
    ).fetchall()
    assert rows == [("/home/u/project-A", 3881)], (
        "project_path follows source_path; sticky source_path keeps "
        "attribution stable across dedup tiebreaker swaps."
    )
