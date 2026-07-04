"""The orphan warning fires once per distinct orphan set, not every sync."""
from __future__ import annotations
import json, os, pathlib
import pytest
from conftest import load_script, redirect_paths


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["_reset_orphan_warning_throttle"]()
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    return ns


def _mk(ns, sid, name):
    p = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / f"-{name}" / f"{name}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"type": "assistant", "timestamp": "2026-07-01T00:00:00Z",
        "requestId": "r", "sessionId": sid, "uuid": "u", "parentUuid": None,
        "message": {"id": "m", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 0, "output_tokens": 1,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")
    return p


def test_warning_throttled_and_repeats_on_change(env, capsys):
    ns = env
    conn = ns["open_cache_db"]()
    a = _mk(ns, "SA", "a"); _mk(ns, "SB", "b")
    ns["sync_cache"](conn); capsys.readouterr()
    os.remove(a)
    ns["sync_cache"](conn)
    assert "no longer on disk" in capsys.readouterr().err     # first appearance warns
    ns["sync_cache"](conn)
    assert "no longer on disk" not in capsys.readouterr().err # same set -> quiet
    b = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-b" / "b.jsonl"
    os.remove(b)
    ns["sync_cache"](conn)
    assert "no longer on disk" in capsys.readouterr().err     # changed set -> warns again
