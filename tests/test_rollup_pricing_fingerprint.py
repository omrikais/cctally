"""#302 Task 4 — pricing-fingerprint auto-invalidation of the rollup.

The rail now reads MATERIALIZED cost off the conversation_sessions rollup (Task
3/5) instead of recomputing it live per request. So a pricing change (a pricing
sync bumping PRICING_SNAPSHOT_DATE, or a cctally upgrade) would leave untouched
sessions' stored cost stale until a manual `cache-sync --rebuild`. The
fingerprint auto-invalidation self-heals it: on the flock-held rollup-maintenance
block, if the stored fingerprint != the current PRICING_SNAPSHOT_DATE, arm the
full backfill + advance the stored fingerprint (one committed txn); the existing
full-recompute-then-drop-flag machinery re-derives every session's cost.

Crash-safety is unchanged: the durable backfill flag remains the recompute
signal, so advancing the fingerprint on arm cannot strand stale cost.
"""
import json
import pathlib
import sys

from conftest import load_script, redirect_paths  # type: ignore

FLAG = "conversation_sessions_backfill_pending"
FP = "conversation_sessions_pricing_fp"


def _asst_line(uuid, msg_id, req_id, text, *, session_id, ts,
               model="claude-opus-4-8"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": session_id,
        "requestId": req_id, "timestamp": ts,
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 1000, "output_tokens": 500,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def _bin_on_path(ns):
    bin_dir = str(pathlib.Path(ns["__file__"]).resolve().parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)


def _get_meta(conn, key):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _synced(tmp_path, monkeypatch):
    """A synced, authoritative rollup (flag clear) over one priced session."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    import _cctally_cache as cache
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "a.jsonl").write_text(
        _asst_line("a1", "ma1", "ra1", "hi", session_id="s1",
                   ts="2026-06-01T00:00:00Z"))
    conn = ns["open_cache_db"]()
    cache.sync_cache(conn)
    return cache, conn


def test_helper_arms_backfill_and_advances_fingerprint(tmp_path, monkeypatch):
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        # Authoritative after the initial sync.
        assert _get_meta(conn, FLAG) is None
        # Simulate a pricing bump: overwrite the stored fingerprint to stale.
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "STALE"))
        conn.commit()

        cache._arm_rollup_backfill_on_pricing_change(conn)

        assert _get_meta(conn, FLAG) == "1", "stale fingerprint must arm backfill"
        assert _get_meta(conn, FP) == cache.PRICING_SNAPSHOT_DATE, \
            "fingerprint must advance to the current snapshot date"
    finally:
        conn.close()


def test_helper_noop_when_fingerprint_matches(tmp_path, monkeypatch):
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        # After a sync the fingerprint is recorded == current. A second call
        # must NOT arm the backfill (no spurious full recompute every sync).
        assert _get_meta(conn, FP) == cache.PRICING_SNAPSHOT_DATE
        assert _get_meta(conn, FLAG) is None
        cache._arm_rollup_backfill_on_pricing_change(conn)
        assert _get_meta(conn, FLAG) is None, "matching fingerprint is a no-op"
    finally:
        conn.close()


def test_sync_records_fingerprint_and_clears_flag(tmp_path, monkeypatch):
    """End-to-end wire-in: a stale fingerprint with NO new messages triggers a
    full re-derive on the next sync, then clears the flag and records the fp."""
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        cost_before = conn.execute(
            "SELECT cost_usd FROM conversation_sessions WHERE session_id='s1'"
        ).fetchone()[0]
        assert cost_before > 0

        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "STALE"))
        conn.commit()

        cache.sync_cache(conn)  # no new JSONL bytes, but the fp is stale

        assert _get_meta(conn, FLAG) is None, "backfill flag consumed"
        assert _get_meta(conn, FP) == cache.PRICING_SNAPSHOT_DATE
        # Cost re-derived to the same value (pricing dict unchanged in-test), and
        # the rollup stays authoritative.
        cost_after = conn.execute(
            "SELECT cost_usd FROM conversation_sessions WHERE session_id='s1'"
        ).fetchone()[0]
        assert cost_after == cost_before
    finally:
        conn.close()


def test_fresh_sync_records_fingerprint(tmp_path, monkeypatch):
    """A fresh sync (fingerprint absent) records it without leaving the flag set."""
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        assert _get_meta(conn, FP) == cache.PRICING_SNAPSHOT_DATE
        assert _get_meta(conn, FLAG) is None
    finally:
        conn.close()
