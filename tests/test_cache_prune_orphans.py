"""_prune_orphaned_cache_entries: safe whole-dir prune vs the three
residual gates (A session-id shared, B uuid-less coverage gap, C surviving
key overlap), degraded conversation_messages, and marker re-establishment."""
from __future__ import annotations
import json, pathlib
import pytest
from conftest import load_script, redirect_paths


def _assistant(msg_id, req_id, *, uuid=None, out=10, ts="2026-07-01T00:00:00Z"):
    obj = {
        "type": "assistant", "timestamp": ts, "requestId": req_id,
        "sessionId": None,
        "message": {"id": msg_id, "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 0, "output_tokens": out,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
    }
    if uuid is not None:
        obj["uuid"] = uuid
        obj["parentUuid"] = None
    return obj


def _write(path, sid, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for ln in lines:
        ln = dict(ln); ln["sessionId"] = sid
        out.append(json.dumps(ln))
    path.write_text("\n".join(out) + "\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects"
    conn = ns["open_cache_db"]()
    return ns, conn, projects


def _counts(conn, path):
    return (
        conn.execute("SELECT count(*) FROM session_files WHERE path=?", (path,)).fetchone()[0],
        conn.execute("SELECT count(*) FROM session_entries WHERE source_path=?", (path,)).fetchone()[0],
        conn.execute("SELECT count(*) FROM conversation_messages WHERE source_path=?", (path,)).fetchone()[0],
    )


def test_safe_whole_dir_prune(env):
    ns, conn, projects = env
    orphan = projects / "-proj-gone" / "s1.jsonl"
    _write(orphan, "S1", [_assistant("m1", "r1", uuid="u1"),
                          _assistant("m2", "r2", uuid="u2")])
    ns["sync_cache"](conn)
    assert _counts(conn, str(orphan)) == (1, 2, 2)
    import shutil; shutil.rmtree(orphan.parent)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 1 and res.pruned_entries == 2 and res.pruned_messages == 2
    assert res.residual_paths == []
    assert _counts(conn, str(orphan)) == (0, 0, 0)
    assert conn.execute("SELECT count(*) FROM conversation_sessions WHERE session_id='S1'").fetchone()[0] == 0


def test_residual_gate_a_shared_session(env):
    ns, conn, projects = env
    live = projects / "-proj-live" / "a.jsonl"
    gone = projects / "-proj-gone" / "b.jsonl"
    _write(live, "S9", [_assistant("m1", "r1", uuid="u1")])
    _write(gone, "S9", [_assistant("m2", "r2", uuid="u2")])
    ns["sync_cache"](conn)
    import os; os.remove(gone)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 0
    assert str(gone) in res.residual_paths


def test_residual_gate_c_surviving_key(env):
    ns, conn, projects = env
    live = projects / "-proj-live" / "a.jsonl"
    gone = projects / "-proj-gone" / "b.jsonl"
    _write(live, "LIVE", [_assistant("m1", "r1", uuid="u1")])
    _write(gone, "GONE", [_assistant("m1", "r1", uuid="u1b")])
    ns["sync_cache"](conn)
    import os; os.remove(gone)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 0 and str(gone) in res.residual_paths
    assert conn.execute("SELECT count(*) FROM session_entries WHERE msg_id='m1' AND req_id='r1'").fetchone()[0] == 1


def test_residual_gate_b_uuidless_blind_spot(env):
    ns, conn, projects = env
    gone = projects / "-proj-gone" / "b.jsonl"
    _write(gone, "SOLO", [_assistant("m1", "r1", uuid=None)])
    ns["sync_cache"](conn)
    assert _counts(conn, str(gone))[1] == 1
    assert _counts(conn, str(gone))[2] == 0
    import os; os.remove(gone)
    res = ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    assert res.pruned_files == 0 and str(gone) in res.residual_paths


def test_marker_reestablished_after_prune(env):
    ns, conn, projects = env
    keep = projects / "-proj-keep" / "k.jsonl"
    gone = projects / "-proj-gone" / "g.jsonl"
    _write(keep, "KEEP", [_assistant("m9", "r9", uuid="u9")])
    _write(gone, "GONE", [_assistant("m1", "r1", uuid="u1")])
    ns["sync_cache"](conn)
    import os; os.remove(gone)
    ns["sync_cache"](conn)
    assert conn.execute("SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'").fetchone() is None
    ns["_prune_orphaned_cache_entries"](conn, lock_timeout=None)
    ns["sync_cache"](conn)
    assert conn.execute("SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'").fetchone() is not None
