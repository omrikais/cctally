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


_MARKER = "claude_ingest_walk_complete"


def _marker_present(conn):
    return conn.execute(
        "SELECT 1 FROM cache_meta WHERE key=?", (_MARKER,)
    ).fetchone() is not None


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


def test_truncation_simulated_crash_recovers_on_next_sync(isolated_cache, monkeypatch):
    """Crash-safety (U3 P2 follow-up): if the process is killed between
    the escalation's `DELETE FROM session_entries` commit and the per-file
    re-ingest commits, the NEXT sync must still re-ingest every file —
    not just the originally-truncated one.

    Without zeroing session_files.size_bytes/last_byte_offset alongside
    the DELETE, the partial-state recovery sync would see size_bytes
    unchanged for untruncated files and take the per-file early-exit
    (`if size == prev_size: continue`), leaving rows missing from
    session_entries until file size changes or the operator runs
    `cache-sync --rebuild`.

    We simulate the crash by monkeypatching the per-file re-ingest loop
    to a no-op after the escalation commit, then run sync_cache a
    SECOND time normally and assert all data is back.
    """
    ns, conn, file_a, file_b, sync = isolated_cache

    # Seed: A and B share (m1,r1); B has higher tokens; a separate
    # (m2,r2) lives only on B so we can detect whether B got re-ingested.
    file_a.write_text(_assistant_line("m1", "r1", out_tokens=10))
    file_b.write_text(
        _assistant_line("m1", "r1", out_tokens=3881, speed="standard")
        + _assistant_line("m2", "r2", out_tokens=500)
    )
    sync()
    rows = _read_entries(conn)
    msgs = sorted(r[0] for r in rows)
    assert msgs == ["m1", "m2"], msgs
    pre_offsets = dict(conn.execute(
        "SELECT path, last_byte_offset FROM session_files"
    ).fetchall())
    assert all(v > 0 for v in pre_offsets.values()), pre_offsets

    # Truncate A. Now monkeypatch the per-file JSONL iterator to RAISE,
    # so the escalation commits (DELETE + UPDATE session_files) land but
    # the per-file ingest aborts before any session_files write commits
    # — simulating kill -9 right after the escalation commit, before any
    # per-file `INSERT INTO session_files` runs.
    file_a.write_text("")
    cache_mod = sys.modules["_cctally_cache"]
    real_iter = cache_mod._iter_jsonl_entries_with_offsets

    class _SimulatedCrash(Exception):
        pass

    def crash_after_escalation(*_args, **_kwargs):
        # Raise from the generator body so the per-file `with open(...)`
        # block's read loop unwinds before the session_files UPSERT.
        raise _SimulatedCrash("simulated kill -9 after escalation commit")
        yield  # pragma: no cover  (unreachable; makes this a generator)

    monkeypatch.setattr(
        cache_mod, "_iter_jsonl_entries_with_offsets", crash_after_escalation
    )
    with pytest.raises(_SimulatedCrash):
        sync()
    monkeypatch.setattr(
        cache_mod, "_iter_jsonl_entries_with_offsets", real_iter
    )

    # Post-"crash" state: session_entries empty, session_files offsets
    # zeroed (the load-bearing invariant under test — U3 P2 fix).
    assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == 0
    rows = conn.execute(
        "SELECT size_bytes, last_byte_offset FROM session_files"
    ).fetchall()
    assert all(r[0] == 0 and r[1] == 0 for r in rows), (
        f"session_files offsets must be zeroed after escalation; got {rows}"
    )

    # Recovery sync: even though file_b's on-disk size hasn't changed
    # since the (pre-crash) first sync, the zeroed prev_size must force
    # the per-file branch to re-ingest from offset 0. file_a is now
    # empty so its content contributes nothing; both m1 and m2 must
    # land on disk via file_b's re-ingest. WITHOUT the U3 P2 fix
    # (UPDATE session_files SET size_bytes=0 ...) file_b would be
    # skipped via `size == prev_size: continue` and both rows would
    # stay missing.
    sync()
    rows = _read_entries(conn)
    msgs = sorted(r[0] for r in rows)
    assert msgs == ["m1", "m2"], (
        f"after the crash-recovery sync, B's (m1,r1) AND (m2,r2) rows "
        f"MUST be present — if either is missing, the size_bytes match "
        f"short-circuited B's per-file re-ingest and the U3 P2 fix is "
        f"incomplete. Got: {msgs}"
    )


def test_truncation_clears_then_rewrites_walk_complete_marker(isolated_cache, monkeypatch):
    """SAFETY (cctally-dev#93, D5/D2): the truncation full-reset DELETEs the
    walk-complete marker atomically with `DELETE FROM session_entries`,
    parallel to the rebuild path. A stale "complete" marker must never survive
    a destructive wipe.

    The wipe and the re-walk normally happen in the SAME `sync_cache` call:
    the escalation clears the marker mid-run, then the run's own clean
    re-ingest walk re-establishes it at the end-of-loop write. Asserting only
    the post-call state would NOT prove the truncation-branch DELETE ran — the
    end-of-loop write refreshes the marker either way, so the net post-state
    is identical whether or not the DELETE fired.

    To pin the truncation-branch DELETE specifically, we borrow the
    crash-injection technique from
    ``test_truncation_simulated_crash_recovers_on_next_sync``: monkeypatch the
    per-file re-ingest iterator to RAISE right after the escalation commit.
    The escalation's `DELETE FROM cache_meta` has committed, but the per-file
    loop (and thus the end-of-loop marker rewrite) never runs. We then observe
    the marker is ABSENT — which is only possible if the truncation branch
    cleared it. A clean recovery sync afterward re-establishes it.
    """
    ns, conn, file_a, file_b, sync = isolated_cache

    # First sync: a multi-line A (headroom to shrink) + single-line B. Clean
    # walk over real JSONL with 001 applied => marker present.
    file_a.write_text(
        _assistant_line("m1", "r1", out_tokens=100)
        + _assistant_line("m1x", "r1x", out_tokens=110)
    )
    file_b.write_text(_assistant_line("m2", "r2", out_tokens=200))
    sync()
    assert _marker_present(conn), (
        "precondition: a clean walk with 001 applied writes the marker"
    )

    # Truncate A (size shrinks) so the next sync's pre-scan detects it and
    # runs the full-reset branch. Inject a raise in the per-file re-ingest
    # iterator so the escalation commit (DELETE session_entries + DELETE
    # marker + zero session_files) lands, but the end-of-loop marker rewrite
    # never executes.
    file_a.write_text(_assistant_line("m1b", "r1b", out_tokens=150))
    cache_mod = sys.modules["_cctally_cache"]
    real_iter = cache_mod._iter_jsonl_entries_with_offsets

    class _SimulatedCrash(Exception):
        pass

    def crash_after_escalation(*_args, **_kwargs):
        raise _SimulatedCrash("simulated kill -9 after escalation commit")
        yield  # pragma: no cover  (unreachable; makes this a generator)

    monkeypatch.setattr(
        cache_mod, "_iter_jsonl_entries_with_offsets", crash_after_escalation
    )
    with pytest.raises(_SimulatedCrash):
        sync()
    monkeypatch.setattr(cache_mod, "_iter_jsonl_entries_with_offsets", real_iter)

    # The escalation committed its DELETE FROM cache_meta; the end-of-loop
    # rewrite never ran. If the truncation branch did NOT clear the marker,
    # it would still be present from the first sync. Its ABSENCE here is the
    # load-bearing assertion for the truncation-path marker clear.
    assert not _marker_present(conn), (
        "truncation full-reset must DELETE the walk-complete marker "
        "atomically with the session_entries wipe"
    )
    # And session_entries is empty (the wipe committed) — sanity that we are
    # observing the post-escalation, pre-rewalk state.
    assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == 0

    # Recovery sync (real iterator restored): a clean re-ingest walk
    # re-establishes the marker for THIS walk.
    sync()
    assert _marker_present(conn), (
        "a clean recovery walk re-establishes the marker"
    )
    rows = _read_entries(conn)
    msgs = sorted(r[0] for r in rows)
    assert msgs == ["m1b", "m2"], (
        f"expected m1b (A's new content) + m2 (B unchanged), got {msgs}"
    )


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
