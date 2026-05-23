"""Regression test (Codex round 1 P2): the direct-parse fallback path
keeps source_path sticky to the FIRST file that contributed a
(msg_id, req_id) key — it must NOT flip source_path to the higher-token
winner from a different file.

``_direct_parse_claude_session_entries`` runs when the cache DB is
unavailable (or ``sync_cache`` lost its lock). Before the fix, the
cross-file merge did ``dedupe_map[key] = (entry, source_path)`` — moving
both the entry data AND its source_path to the winner. The cache ingest
path instead pins source_path to the first inserter (``source_path`` is
omitted from the ON CONFLICT DO UPDATE SET clause; see
``tests/test_cache_dedup_source_path_sticky.py`` for the U1 cache-path
coverage). Because the fallback stamps each emitted row's
session_id/project_path from ``meta_by_path[source_path]``, a flipped
source_path silently re-attributes cross-file duplicates to the winner's
project — ``cctally project`` then disagrees with the normal cached
behavior exactly when the fallback is exercised.

This test drives the full fallback function with two real JSONL files
sharing one (msg_id, req_id) key but living under different projects.
"""
from __future__ import annotations

import datetime as dt
import importlib.util as _ilu
import json
import pathlib
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


def _load(name: str, relpath: str):
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    spec = _ilu.spec_from_file_location(name, REPO_ROOT / relpath)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cache_mod():
    return _load("_cctally_cache", "bin/_cctally_cache.py")


def _write_jsonl(path: pathlib.Path, *, cwd: str, out_tokens: int) -> None:
    """One assistant message carrying the shared (msg_id, req_id) key."""
    obj = {
        "type": "assistant",
        "sessionId": "sess-shared",
        "cwd": cwd,
        "timestamp": "2026-05-22T17:04:00.000Z",
        "requestId": "r1",
        "message": {
            "id": "m1",
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": 10,
                "output_tokens": out_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
    path.write_text(json.dumps(obj) + "\n", encoding="utf-8")


def test_fallback_pins_source_path_to_first_file(cache_mod, tmp_path, monkeypatch):
    """File A (lower tokens, first) and File B (higher tokens, winner) share
    one (msg_id, req_id). The winning DATA comes from B, but source_path —
    and therefore project_path — must stay pinned to A."""
    file_a = tmp_path / "project-A.jsonl"
    file_b = tmp_path / "project-B.jsonl"
    # A is the streaming-intermediate (lower tokens, ingested first);
    # B is the post-stream finalization (higher tokens, the winner).
    _write_jsonl(file_a, cwd="/home/u/project-A", out_tokens=1)
    _write_jsonl(file_b, cwd="/home/u/project-B", out_tokens=3881)

    # Drive the file discovery deterministically: A first, then B, so B's
    # higher-token entry wins the contest against A's already-seen key.
    monkeypatch.setattr(
        cache_mod, "_discover_session_files",
        lambda range_start, project=None: [file_a, file_b],
    )

    start = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    rows = cache_mod._direct_parse_claude_session_entries(start, end)

    assert len(rows) == 1, "the shared (msg_id, req_id) must dedup to one row"
    row = rows[0]
    # Winner's DATA (B's higher token total).
    assert row.output_tokens == 3881, "higher-token entry must win the data"
    # First contributor's PROVENANCE (A) — NOT the winner's file.
    assert row.source_path == str(file_a), (
        "source_path must stay pinned to the first file (A), not flip to "
        "the higher-token winner (B)"
    )
    assert row.project_path == "/home/u/project-A", (
        "project_path follows source_path; a flipped source_path would "
        "re-attribute the tokens to project-B"
    )
