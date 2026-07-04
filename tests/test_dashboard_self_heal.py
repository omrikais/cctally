"""Dashboard self-heal prunes orphans via _prune_orphaned_cache_entries and
is a no-op under skip_sync."""
from __future__ import annotations
import json, os, pathlib, shutil
import pytest
from conftest import load_script, redirect_paths


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    return ns


def _orphan(ns):
    p = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-gone" / "s.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"type": "assistant", "timestamp": "2026-07-01T00:00:00Z",
        "requestId": "r", "sessionId": "S", "uuid": "u", "parentUuid": None,
        "message": {"id": "m", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 0, "output_tokens": 1,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")
    conn = ns["open_cache_db"](); ns["sync_cache"](conn); conn.close()
    shutil.rmtree(p.parent)


def test_self_heal_prunes(env):
    ns = env
    _orphan(ns)
    res = ns["_dashboard_self_heal_orphans"](skip_sync=False)
    assert res is not None and res.pruned_files == 1
    conn = ns["open_cache_db"]()
    assert conn.execute("SELECT count(*) FROM session_files WHERE size_bytes>0").fetchone()[0] == 0


def test_self_heal_noop_under_skip_sync(env):
    ns = env
    _orphan(ns)
    assert ns["_dashboard_self_heal_orphans"](skip_sync=True) is None
    conn = ns["open_cache_db"]()
    assert conn.execute("SELECT count(*) FROM session_files WHERE size_bytes>0").fetchone()[0] == 1
