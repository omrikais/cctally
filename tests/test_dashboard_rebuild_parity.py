"""Rebuild-path tests for the #268 dashboard perf refactor.

M1: the dashboard rebuild (`_tui_build_snapshot`) must ingest JSONL
exactly ONCE at the top and then read every builder with
``skip_sync=True`` (pure SQLite reads). Before this change each of the
~8 wide builders ran its own ``sync_cache`` → ~8-10 redundant whole-tree
globs per rebuild (spec §4).

The spy counts total ``sync_cache`` invocations across a whole rebuild.
Builder-internal calls resolve to ``_cctally_cache.sync_cache`` (the
bare name inside ``get_entries`` / ``get_claude_session_entries``); the
new top-of-rebuild call resolves through the ``cctally.sync_cache``
re-export. Patching BOTH names to one shared spy therefore counts every
real ingest — which is exactly the "8-10 → 1" story the change lands.
"""
from __future__ import annotations

import datetime as dt
import json

from conftest import load_script, redirect_paths  # type: ignore


NOW_UTC = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)


def _asst_line(uuid, msg_id, req_id, text, *, ts, model="claude-opus-4-8"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": "s1",
        "requestId": req_id, "timestamp": ts,
        "cwd": "/Users/u/proj",
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 100, "output_tokens": 40,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def _seed_jsonl(tmp_path):
    """One Claude JSONL file with a couple of recent assistant entries."""
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / "s1.jsonl"
    p.write_text(
        _asst_line("u1", "m1", "r1", "hi", ts="2026-07-04T09:00:00Z")
        + _asst_line("u2", "m2", "r2", "yo", ts="2026-07-04T10:30:00Z")
    )
    return p


def _install_sync_spy(ns, monkeypatch):
    """Patch both sync_cache re-export sites to one counting spy that
    delegates to the real ingest. Returns the call-count dict."""
    import _cctally_cache

    calls = {"n": 0}
    real = _cctally_cache.sync_cache

    def spy(conn, **kw):
        calls["n"] += 1
        return real(conn, **kw)

    monkeypatch.setattr(_cctally_cache, "sync_cache", spy)
    monkeypatch.setitem(ns, "sync_cache", spy)
    return calls


def test_sync_cache_called_once_per_rebuild(monkeypatch, tmp_path):
    """A dashboard rebuild (caller skip_sync=False) ingests exactly ONCE."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_jsonl(tmp_path)
    calls = _install_sync_spy(ns, monkeypatch)

    snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=False)

    assert snap is not None
    assert calls["n"] == 1, (
        f"expected exactly 1 sync_cache per rebuild, got {calls['n']} "
        "(pre-change: each wide builder re-globs → ~8-10 redundant syncs)"
    )


def test_no_sync_never_ingests_but_still_reads(monkeypatch, tmp_path):
    """--no-sync (caller skip_sync=True): the top-of-rebuild ingest is
    gated OFF (zero sync_cache), yet builders still read the already-cached
    rows (spec §4)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_jsonl(tmp_path)

    # Pre-populate the cache with a real ingest BEFORE the spy, so a pure
    # --no-sync read has existing rows to serve.
    conn = ns["open_cache_db"]()
    ns["sync_cache"](conn)
    conn.close()

    calls = _install_sync_spy(ns, monkeypatch)

    snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=True)

    assert snap is not None
    assert calls["n"] == 0, (
        f"--no-sync must not ingest, but sync_cache ran {calls['n']} times"
    )
    # The snapshot still populated from the pre-existing cache rows — the
    # seeded entries' tokens surface without any fresh ingest.
    assert snap.daily_total_tokens > 0, (
        "expected --no-sync rebuild to read existing cached entries"
    )
