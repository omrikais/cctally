"""Regression test for U3: truncation under the ccusage-parity UPSERT
must not silently drop the winning dedup row.

Walkthrough (the bug U3 fixes):
  1. File A and file B share a (msg_id, req_id) pair.
  2. A ingested first; its (lower-token) row lands with source_path=A.
  3. B ingested next; its higher tokens win the UPSERT contest. Source
     path stays = A (per U1) but token columns reflect B's data.
  4. A truncates (rotated/manually-edited). The per-file truncation path
     DELETEs by `source_path = A` — wiping the winning row.
  5. B's size is unchanged, so the per-file delta-resume early-exits
     ("files_skipped_unchanged"). The winning data is now lost from the
     cache until B is manually touched / a `--rebuild` is run.

Fix: a pre-scan inside sync_cache detects any truncation, drops the
entire session_entries table, and clears the `existing` map so EVERY
file re-ingests from offset 0. The cache is fully re-derivable, the
event is rare, and this sidesteps the per-key contributing-file
bookkeeping that the alternative would require.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys

import pytest

from conftest import load_script


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


def _assistant_line(msg_id, req_id, *, out_tokens, ts="2026-05-22T17:04:00Z",
                    speed=None):
    """One JSONL assistant entry. Mirrors `_iter_jsonl_entries_with_offsets`
    expectations: top-level `type=assistant`, nested `message` carries
    `id`/`model`/`usage`, top-level `requestId` carries req_id."""
    usage = {
        "input_tokens": 0, "output_tokens": out_tokens,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }
    if speed is not None:
        usage["speed"] = speed
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "requestId": req_id,
        "message": {
            "id": msg_id, "model": "claude-opus-4-7", "usage": usage,
        },
    }) + "\n"


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Drive `sync_cache` against a tmp HOME with two synthetic JSONL files.

    Returns: (ns, conn, file_a, file_b, sync). `sync()` invokes a fresh
    `sync_cache(conn)` and returns the IngestStats. `file_a` / `file_b`
    are pathlib.Path handles the caller can rewrite to simulate
    truncation.
    """
    ns = load_script()
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)

    projects = tmp_path / ".claude" / "projects"
    proj_a = projects / "-Users-u-project-A"
    proj_b = projects / "-Users-u-project-B"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    file_a = proj_a / "sess-a.jsonl"
    file_b = proj_b / "sess-b.jsonl"

    sync_cache = ns["sync_cache"]
    open_cache_db = ns["open_cache_db"]
    conn = open_cache_db()

    def sync():
        return sync_cache(conn)

    yield ns, conn, file_a, file_b, sync

    try:
        conn.close()
    except Exception:
        pass


def _read_entries(conn):
    return conn.execute(
        "SELECT msg_id, req_id, source_path, output_tokens "
        "FROM session_entries ORDER BY msg_id"
    ).fetchall()


def test_winner_data_preserved_when_loser_truncates(isolated_cache):
    """Scenario from U1+U3 design:
       1. A ingested first — row pinned to A, low tokens.
       2. B ingested — higher tokens win UPSERT; source_path stays A.
       3. A truncates. Pre-scan detects → global re-ingest from both.
       4. B's higher-token row reappears (sourced from B's content this
          time, attributed to A's source_path on re-ingest order).

    Either source_path is OK after re-ingest (depends on glob order); what
    matters is the winning TOKEN data is preserved.
    """
    ns, conn, file_a, file_b, sync = isolated_cache

    # Step 1: A and B both carry (m1,r1); A's data is lower.
    file_a.write_text(_assistant_line("m1", "r1", out_tokens=10))
    file_b.write_text(_assistant_line("m1", "r1", out_tokens=3881, speed="standard"))
    stats = sync()
    assert stats.files_processed == 2
    rows = _read_entries(conn)
    assert len(rows) == 1
    # Winner's tokens land in the row; source_path is whichever file
    # ingested first (filesystem glob order). Don't assert source_path
    # here — that's covered by test_cache_dedup_source_path_sticky.
    assert rows[0][3] == 3881, "higher-token row must win the UPSERT contest"

    # Step 2: truncate A. Replace with EMPTY content (size shrinks).
    file_a.write_text("")
    stats2 = sync()
    assert stats2.files_reset_truncated >= 1, (
        "the truncated file should be counted in files_reset_truncated"
    )

    # Step 3: winning data must still be present.
    rows2 = _read_entries(conn)
    assert len(rows2) == 1, (
        "the dedup row must NOT be silently dropped just because the "
        "truncated file happened to be the original source_path holder"
    )
    assert rows2[0][3] == 3881, (
        "B's higher-token data is still on disk; after re-ingest it must "
        "be back in the cache"
    )


def test_winner_data_preserved_when_winner_source_truncates(isolated_cache):
    """Mirror scenario: this time B inserts first (low) and A wins via
    UPSERT (high). When B truncates (B = "loser file" by content), the
    winning row's source_path = B is wiped. Re-ingest must reconstruct A's
    winning data."""
    ns, conn, file_a, file_b, sync = isolated_cache

    # First sync: only B is on disk, with low tokens.
    file_b.write_text(_assistant_line("m1", "r1", out_tokens=10))
    sync()
    rows = _read_entries(conn)
    assert len(rows) == 1 and rows[0][3] == 10
    src_after_b = rows[0][2]
    assert "project-B" in src_after_b

    # Second sync: A appears with higher tokens. UPSERT updates the row
    # but source_path stays = B (sticky to first writer).
    file_a.write_text(_assistant_line("m1", "r1", out_tokens=3881, speed="standard"))
    sync()
    rows = _read_entries(conn)
    assert len(rows) == 1
    assert rows[0][3] == 3881
    assert rows[0][2] == src_after_b, (
        "source_path must remain pinned to B (the first writer)"
    )

    # Third sync: B truncates. Without U3, the per-file DELETE
    # WHERE source_path = B wipes the row AND B is then re-ingested
    # with only its (low) content — the high-token winner from A is
    # lost because A's size hasn't changed and the delta-resume path
    # skips it.
    file_b.write_text("")
    sync()

    rows = _read_entries(conn)
    assert len(rows) == 1, "winning row must not vanish"
    assert rows[0][3] == 3881, (
        "A's high-token data is still on disk; after global re-ingest "
        "the cache must reflect it again"
    )


def test_no_regression_on_pure_growth(isolated_cache):
    """Sanity: an ordinary append-only growth path must NOT trigger the
    truncation escalation (which would force a full re-ingest)."""
    ns, conn, file_a, file_b, sync = isolated_cache

    file_a.write_text(_assistant_line("m1", "r1", out_tokens=10))
    file_b.write_text(_assistant_line("m2", "r2", out_tokens=20))
    stats = sync()
    assert stats.files_reset_truncated == 0
    assert stats.files_processed == 2

    # Append a new entry to A. No truncation.
    with file_a.open("a") as fh:
        fh.write(_assistant_line("m3", "r3", out_tokens=30))
    stats2 = sync()
    assert stats2.files_reset_truncated == 0, (
        "pure-append must NOT trip the truncation pre-scan"
    )
    # B unchanged → skipped via delta-resume.
    assert stats2.files_skipped_unchanged >= 1
    # A grew → exactly one new row.
    rows = _read_entries(conn)
    assert sorted(r[0] for r in rows) == ["m1", "m2", "m3"]


def test_truncation_with_no_dedup_overlap(isolated_cache):
    """Truncation on a file that DOESN'T share dedup keys with any other
    file should still re-ingest correctly. (This is the common case —
    log-rotation on an isolated session file.)"""
    ns, conn, file_a, file_b, sync = isolated_cache

    # First sync: a multi-line A and single-line B so we have headroom to
    # shrink A on the next pass.
    file_a.write_text(
        _assistant_line("m1", "r1", out_tokens=100)
        + _assistant_line("m1x", "r1x", out_tokens=110)
        + _assistant_line("m1y", "r1y", out_tokens=120)
    )
    file_b.write_text(_assistant_line("m2", "r2", out_tokens=200))
    sync()
    assert len(_read_entries(conn)) == 4

    # A is rewritten from scratch with shorter, different content.
    file_a.write_text(_assistant_line("m1b", "r1b", out_tokens=150))
    stats = sync()
    assert stats.files_reset_truncated >= 1

    rows = _read_entries(conn)
    # B's row preserved; A's three rows replaced by one.
    msgs = sorted(r[0] for r in rows)
    assert msgs == ["m1b", "m2"], (
        f"expected m1b (A's new content) + m2 (B unchanged), got {msgs}"
    )
