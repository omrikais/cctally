"""#279 S3 F4 — pin two ACCEPTED-but-undocumented ingest behaviors so a future
change can't silently alter them (no behavior change here; these PIN current
behavior with specific counter assertions).

1. Size-only delta detection (spec §6): a same-byte-length in-place rewrite is
   invisible until `cache-sync --rebuild`. Claude Code JSONL is append-only in
   practice and mtime was deliberately excluded (clock-skew); size is the delta
   signal.
2. Targeted (only_paths) ingest silently drops a requested path that has
   vanished (session rotated/deleted mid-live-tail) — flagging it a failure
   would wedge the watch loop's targeted_clean advance forever.
"""
from __future__ import annotations

import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths  # noqa: E402


def _assistant_line(msg_id, req_id, out_tokens):
    # Fixed key order so two lines differ ONLY in the (equal-length) values.
    return json.dumps({
        "type": "assistant",
        "timestamp": "2026-07-01T10:00:00Z",
        "requestId": req_id,
        "message": {
            "id": msg_id,
            "model": "claude-opus-4-8",
            "usage": {"input_tokens": 0, "output_tokens": out_tokens,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    })


def test_same_size_rewrite_is_invisible_until_rebuild(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sync_cache = ns["sync_cache"]
    open_cache_db = ns["open_cache_db"]

    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / "sess.jsonl"

    original = _assistant_line("m1", "r1", 100)
    rewrite = _assistant_line("m2", "r2", 200)
    assert len(original) == len(rewrite), "test setup: rewrite must be same length"
    f.write_text(original + "\n")

    conn = open_cache_db()
    try:
        sync_cache(conn)
        assert conn.execute(
            "SELECT msg_id, output_tokens FROM session_entries"
        ).fetchall() == [("m1", 100)]

        # Same-byte-length in-place rewrite: delta detection sees no size change.
        f.write_text(rewrite + "\n")
        stats = sync_cache(conn)
        assert stats.files_skipped_unchanged == 1, (
            "a same-size rewrite must be skipped as unchanged (size-only delta)"
        )
        assert conn.execute(
            "SELECT msg_id, output_tokens FROM session_entries"
        ).fetchall() == [("m1", 100)], "the stale row must survive the same-size rewrite"

        # --rebuild re-reads from offset 0 and picks up the new content.
        sync_cache(conn, rebuild=True)
        assert conn.execute(
            "SELECT msg_id, output_tokens FROM session_entries"
        ).fetchall() == [("m2", 200)], "rebuild must pick up the rewritten content"
    finally:
        conn.close()


def test_vanished_only_paths_file_is_silently_dropped(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sync_cache = ns["sync_cache"]
    open_cache_db = ns["open_cache_db"]

    conn = open_cache_db()
    try:
        gone = str(tmp_path / ".claude" / "projects" / "-Users-u-proj" / "gone.jsonl")
        stats = sync_cache(conn, only_paths={gone})
        assert stats.files_total == 0, "a vanished requested path is filtered out"
        assert stats.files_failed == 0, "a vanished path is NOT a failure"
        assert stats.targeted_clean is True, (
            "targeted_clean must stay True so the watch loop advances"
        )
    finally:
        conn.close()
