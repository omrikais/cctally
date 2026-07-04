"""cmd_cache_sync: --rebuild aggregates a non-zero exit on lock contention
(was silently exit 0); --prune-orphans runs the helper and reports."""
from __future__ import annotations
import argparse, fcntl, json, os, pathlib, shutil
import pytest
from conftest import load_script, redirect_paths


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    return ns, tmp_path, monkeypatch


def test_rebuild_contended_returns_nonzero(env, capsys):
    ns, tmp_path, monkeypatch = env
    # Speed: lower the rebuild lock timeout so the test doesn't wait 30s.
    # cmd_cache_sync reads the constant from its OWN module (_cctally_cache),
    # so patch there (the `cctally` re-export copy is not what it reads).
    monkeypatch.setattr(
        ns["_cctally_cache"], "_REBUILD_LOCK_TIMEOUT_SECONDS", 0.3, raising=False
    )
    lock_path = ns["_cctally_core"].CACHE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w"); fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        args = argparse.Namespace(source="claude", rebuild=True, prune_orphans=False)
        rc = ns["cmd_cache_sync"](args)
        assert rc == 1
        assert "rebuild skipped" in capsys.readouterr().err.lower()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN); holder.close()


def test_prune_orphans_cli(env, capsys):
    ns, tmp_path, monkeypatch = env
    pdir = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-p-gone"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "s.jsonl").write_text(json.dumps({"type": "assistant",
        "timestamp": "2026-07-01T00:00:00Z", "requestId": "r1", "sessionId": "S1",
        "uuid": "u1", "parentUuid": None,
        "message": {"id": "m1", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 0, "output_tokens": 5,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")
    conn = ns["open_cache_db"](); ns["sync_cache"](conn); conn.close()
    shutil.rmtree(pdir)
    args = argparse.Namespace(source="claude", rebuild=False, prune_orphans=True)
    rc = ns["cmd_cache_sync"](args)
    assert rc == 0
    assert "pruned 1 orphaned file" in capsys.readouterr().err.lower()


def test_prune_orphans_source_codex_is_noop(env, capsys):
    # --prune-orphans applies to the Claude cache only; --source codex must
    # print a note and no-op (exit 0) rather than silently pruning Claude.
    ns, tmp_path, monkeypatch = env
    pdir = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-p-gone"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "s.jsonl").write_text(json.dumps({"type": "assistant",
        "timestamp": "2026-07-01T00:00:00Z", "requestId": "r1", "sessionId": "S1",
        "uuid": "u1", "parentUuid": None,
        "message": {"id": "m1", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 0, "output_tokens": 5,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")
    conn = ns["open_cache_db"](); ns["sync_cache"](conn); conn.close()
    shutil.rmtree(pdir)
    args = argparse.Namespace(source="codex", rebuild=False, prune_orphans=True)
    rc = ns["cmd_cache_sync"](args)
    assert rc == 0
    assert "claude cache only" in capsys.readouterr().err.lower()
    # The Claude orphan was NOT pruned (source codex was respected).
    conn = ns["open_cache_db"]()
    assert conn.execute("SELECT count(*) FROM session_files WHERE size_bytes>0").fetchone()[0] == 1
