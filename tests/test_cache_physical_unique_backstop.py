"""#279 S3 F3 — the DB-level idempotency backstop `UNIQUE(source_path,
line_offset)` on `session_entries` (mirroring `codex_session_entries`).

Under correct offset bookkeeping a physical-key collision is structurally
impossible. The backstop exists so that an offset-bookkeeping REGRESSION can
never SILENTLY double-count: the collision raises IntegrityError, which the
per-file ingest handler converts into a loud rolled-back failure
(files_failed += 1) instead of a silent doubled row.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths  # noqa: E402


_INSERT = (
    "INSERT INTO session_entries (source_path, line_offset, "
    "timestamp_utc, model, msg_id, req_id, input_tokens, output_tokens, "
    "cache_create_tokens, cache_read_tokens, usage_extra_json, speed, "
    "cost_usd_raw, mutation_seq, mutation_min_ts) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def test_null_keyed_physical_duplicate_raises(tmp_path, monkeypatch):
    """A NULL-keyed row re-inserted at the same (source_path, line_offset) must
    raise IntegrityError — NULL keys are the ones the logical dedup UPSERT can't
    see, so the physical index is their only idempotency protection."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_cache_db"]()
    try:
        row = ("/a.jsonl", 0, "2026-07-01T10:00:00+00:00", "claude-opus-4-8",
               None, None, 1, 1, 0, 0, None, None, None, 0, None)
        conn.execute(_INSERT, row)
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(_INSERT, row)
    finally:
        conn.close()


def test_offset_regression_sync_fails_loud_not_double(tmp_path, monkeypatch):
    """Sync-level: rewind session_files.last_byte_offset (and size_bytes) to 0
    with a NULL-keyed row already present; the re-walk must NOT double the row —
    it must fail that file loudly (files_failed == 1) and leave the count
    unchanged."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sync_cache = ns["sync_cache"]
    open_cache_db = ns["open_cache_db"]

    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    # A NULL-keyed assistant line: message.id present but NO requestId, so
    # req_id is NULL and the row is NOT covered by idx_entries_dedup.
    (proj / "sess.jsonl").write_text(
        '{"type":"assistant","timestamp":"2026-07-01T10:00:00Z",'
        '"message":{"id":"m1","model":"claude-opus-4-8",'
        '"usage":{"input_tokens":1,"output_tokens":1}}}\n'
    )

    conn = open_cache_db()
    try:
        sync_cache(conn)
        before = conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0]
        assert before == 1

        # Simulate an offset-bookkeeping regression: pretend nothing was
        # ingested (offset + size back to 0) while the row is still committed.
        conn.execute("UPDATE session_files SET last_byte_offset = 0, size_bytes = 0")
        conn.commit()

        stats = sync_cache(conn)
        assert stats.files_failed == 1, (
            "the physical-key collision must surface as a loud file failure"
        )
        after = conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0]
        assert after == before, (
            f"the row must NOT be double-counted; got {after} rows (was {before})"
        )
    finally:
        conn.close()
