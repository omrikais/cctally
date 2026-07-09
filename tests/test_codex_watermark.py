"""#279 S3 F1 — the Codex delta-resume watermark must be the iterator's REAL
dedup watermark (the cumulative `total_token_usage.total_tokens` the guard
compares against), stamped onto `_CodexIterState.total_tokens` and persisted by
`sync_codex_cache`.

Before this fix, `_CodexIterState.total_tokens` was declared but never written,
and the caller reconstructed the watermark as `initial + Σ(per-turn
last_token_usage.total_tokens)`. Those two quantities are equal only while
Codex's per-turn and cumulative accounting stay mutually consistent — a
divergence (a turn whose cumulative jumps by more than its per-turn delta) makes
the reconstructed sum too low, seeding a too-low watermark on the next resume so
events in the gap re-yield and double-count.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

import _lib_jsonl as lj  # noqa: E402
from conftest import load_script, redirect_paths  # noqa: E402

_CodexIterState = lj._CodexIterState
_iter = lj._iter_codex_jsonl_entries_with_offsets


def _token_count_line(ts, last_total, cumulative, *, include_ttu=True):
    info = {
        "last_token_usage": {
            "input_tokens": last_total, "output_tokens": 0,
            "cached_input_tokens": 0, "reasoning_output_tokens": 0,
            "total_tokens": last_total,
        },
    }
    if include_ttu:
        info["total_token_usage"] = {"total_tokens": cumulative}
    return json.dumps({
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "token_count", "info": info},
    })


def _session_meta_line(ts, sid):
    return json.dumps({"timestamp": ts, "type": "session_meta",
                       "payload": {"id": sid}})


def test_state_total_tokens_carries_real_watermark(tmp_path):
    """Cumulative advances MORE than the per-turn delta: the state must carry
    the cumulative (200), not initial + sum-of-deltas (110)."""
    p = tmp_path / "rollout-a.jsonl"
    lines = [
        _session_meta_line("2026-07-01T10:00:00Z", "sess-1"),
        _token_count_line("2026-07-01T10:00:01Z", 100, 100),
        _token_count_line("2026-07-01T10:00:02Z", 10, 200),  # cumulative +100, delta 10
    ]
    p.write_text("\n".join(lines) + "\n")
    state = _CodexIterState()
    with open(p) as fh:
        rows = list(_iter(fh, str(p), state=state))
    assert len(rows) == 2
    assert state.total_tokens == 200  # the REAL watermark, not 110

    # A follow-up event whose cumulative (150) lies BELOW the real watermark
    # (200) but ABOVE the old reconstructed sum (110) must NOT re-yield on
    # resume — the old code seeded 110 and would have double-counted it.
    state2 = _CodexIterState(total_tokens=state.total_tokens)
    p2 = tmp_path / "cont.jsonl"
    p2.write_text(_token_count_line("2026-07-01T10:00:03Z", 5, 150) + "\n")
    with open(p2) as fh:
        rows2 = list(_iter(fh, str(p2), initial_session_id="sess-1", state=state2))
    assert rows2 == []


def test_legacy_no_ttu_file_does_not_inflate_watermark(tmp_path):
    """No total_token_usage dict (older Codex builds): yields happen
    unconditionally, but the watermark must stay at the seed (0), NOT inflate by
    the per-turn sums — otherwise a later mixed-format tail would skip genuinely
    new events."""
    p = tmp_path / "rollout-legacy.jsonl"
    lines = [
        _session_meta_line("2026-07-01T10:00:00Z", "sess-legacy"),
        _token_count_line("2026-07-01T10:00:01Z", 100, 0, include_ttu=False),
        _token_count_line("2026-07-01T10:00:02Z", 50, 0, include_ttu=False),
    ]
    p.write_text("\n".join(lines) + "\n")
    state = _CodexIterState()
    with open(p) as fh:
        rows = list(_iter(fh, str(p), state=state))
    assert len(rows) == 2
    assert state.total_tokens == 0


def test_state_seed_precedence_state_wins(tmp_path):
    """Caller-supplied NON-ZERO state.total_tokens beats the kwarg (mirrors the
    session_id/model seeding precedence). Guard uses 500 (state), not 100
    (kwarg), so a cumulative-400 event is deduped away."""
    p = tmp_path / "rollout-prec.jsonl"
    lines = [
        _session_meta_line("2026-07-01T10:00:00Z", "sess-prec"),
        _token_count_line("2026-07-01T10:00:01Z", 400, 400),
    ]
    p.write_text("\n".join(lines) + "\n")
    state = _CodexIterState(total_tokens=500)
    with open(p) as fh:
        rows = list(_iter(fh, str(p), initial_total_tokens=100, state=state))
    assert rows == []
    assert state.total_tokens == 500


def test_metadata_only_tail_preserves_prior_watermark(tmp_path):
    """A delta window whose only new record is a session_meta (no yielded
    token_count) leaves the seed watermark untouched — the caller then persists
    the prior value unchanged."""
    p = tmp_path / "rollout-tail.jsonl"
    p.write_text(_session_meta_line("2026-07-01T10:00:05Z", "sess-tail") + "\n")
    state = _CodexIterState(total_tokens=321)
    with open(p) as fh:
        rows = list(_iter(fh, str(p), state=state))
    assert rows == []
    assert state.total_tokens == 321


def test_sync_persists_iterator_watermark(tmp_path, monkeypatch):
    """Integration: sync_codex_cache persists the cumulative (200), not the
    reconstructed initial+Σ(per-turn) sum (110). A second sync over an appended
    stale-cumulative (150) event ingests 0 new rows (idempotent resume)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "01"
    sessions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    rollout = sessions / "rollout-2026-07-01T10-00-00-aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa.jsonl"
    turn_ctx = json.dumps({"timestamp": "2026-07-01T10:00:00Z", "type": "turn_context",
                           "payload": {"model": "gpt-5"}})
    rollout.write_text("\n".join([
        _session_meta_line("2026-07-01T10:00:00Z", "sess-div"),
        turn_ctx,
        _token_count_line("2026-07-01T10:00:01Z", 100, 100),
        _token_count_line("2026-07-01T10:00:02Z", 10, 200),  # cumulative +100, delta 10
    ]) + "\n")

    sync_codex_cache = ns["sync_codex_cache"]
    open_cache_db = ns["open_cache_db"]

    conn = open_cache_db()
    try:
        sync_codex_cache(conn)
        row = conn.execute(
            "SELECT last_total_tokens FROM codex_session_files"
        ).fetchone()
        assert row is not None
        assert row[0] == 200, f"expected the cumulative watermark 200, got {row[0]}"
        n_before = conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone()[0]
        assert n_before == 2

        # Append a stale-cumulative (150) event: below the real watermark (200)
        # but above the old reconstructed sum (110). A correct resume ingests
        # ZERO new rows.
        with rollout.open("a") as fh:
            fh.write(_token_count_line("2026-07-01T10:00:03Z", 5, 150) + "\n")
        sync_codex_cache(conn)
        n_after = conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone()[0]
        assert n_after == 2, f"stale-cumulative event double-counted: {n_after}"
    finally:
        conn.close()
