"""Regression tests for ccusage-parity dedup tiebreaker.

The cache INSERT path and the direct-JSONL fallback path must both pick the
post-stream finalization row (higher token total; speed-set on ties) and
drop the streaming-intermediate row. Mirrors ccusage's
`should_replace_deduped_entry` in rust/crates/ccusage/src/claude_loader.rs:531.
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import pathlib
import sqlite3
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str, relpath: str):
    spec = _ilu.spec_from_file_location(name, REPO_ROOT / relpath)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def lib_jsonl():
    # Load _lib_jsonl in isolation so we test its helpers directly.
    return _load("_lib_jsonl", "bin/_lib_jsonl.py")


@pytest.fixture
def cache_db(tmp_path):
    # Spawn a minimal cache.db matching the production schema for
    # session_entries (just the columns + partial unique index we care about).
    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE session_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            line_offset INTEGER NOT NULL,
            timestamp_utc TEXT NOT NULL,
            model TEXT NOT NULL,
            msg_id TEXT,
            req_id TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_create_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            usage_extra_json TEXT,
            cost_usd_raw REAL
        );
        CREATE UNIQUE INDEX idx_entries_dedup
            ON session_entries(msg_id, req_id)
            WHERE msg_id IS NOT NULL AND req_id IS NOT NULL;
    """)
    yield conn
    conn.close()


# The SQL we're about to ship — keep it as a fixture-level constant so the
# tests read like production. Production INSERT will be byte-identical.
UPSERT_SQL = """
INSERT INTO session_entries (
    source_path, line_offset, timestamp_utc, model,
    msg_id, req_id, input_tokens, output_tokens,
    cache_create_tokens, cache_read_tokens,
    usage_extra_json, cost_usd_raw
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(msg_id, req_id)
WHERE msg_id IS NOT NULL AND req_id IS NOT NULL
DO UPDATE SET
    source_path = excluded.source_path,
    line_offset = excluded.line_offset,
    timestamp_utc = excluded.timestamp_utc,
    model = excluded.model,
    input_tokens = excluded.input_tokens,
    output_tokens = excluded.output_tokens,
    cache_create_tokens = excluded.cache_create_tokens,
    cache_read_tokens = excluded.cache_read_tokens,
    usage_extra_json = excluded.usage_extra_json,
    cost_usd_raw = excluded.cost_usd_raw
WHERE
    (excluded.input_tokens + excluded.output_tokens
     + excluded.cache_create_tokens + excluded.cache_read_tokens)
    >
    (session_entries.input_tokens + session_entries.output_tokens
     + session_entries.cache_create_tokens + session_entries.cache_read_tokens)
 OR (
    (excluded.input_tokens + excluded.output_tokens
     + excluded.cache_create_tokens + excluded.cache_read_tokens)
    =
    (session_entries.input_tokens + session_entries.output_tokens
     + session_entries.cache_create_tokens + session_entries.cache_read_tokens)
    AND json_extract(excluded.usage_extra_json, '$.speed') IS NOT NULL
    AND json_extract(session_entries.usage_extra_json, '$.speed') IS NULL
 )
"""


def _row(msg_id, req_id, out_tokens, *, speed=None, in_tokens=0, cc=0, cr=0):
    extras = {} if speed is None else {"speed": speed}
    return (
        "/tmp/fake.jsonl", 0, "2026-05-22T17:04:00Z", "claude-opus-4-7",
        msg_id, req_id, in_tokens, out_tokens, cc, cr,
        json.dumps(extras), None,
    )


def test_higher_tokens_wins(cache_db):
    """Streaming intermediate (output=1) followed by finalization (output=3881)
    leaves only the finalization in session_entries."""
    cache_db.execute(UPSERT_SQL, _row("m1", "r1", out_tokens=1))
    cache_db.execute(UPSERT_SQL, _row("m1", "r1", out_tokens=3881, speed="standard"))
    rows = cache_db.execute(
        "SELECT msg_id, output_tokens FROM session_entries"
    ).fetchall()
    assert rows == [("m1", 3881)]


def test_first_stays_when_first_higher(cache_db):
    """Finalization first, streaming intermediate second: finalization must
    survive (no spurious replacement)."""
    cache_db.execute(UPSERT_SQL, _row("m1", "r1", out_tokens=3881, speed="standard"))
    cache_db.execute(UPSERT_SQL, _row("m1", "r1", out_tokens=1))
    rows = cache_db.execute(
        "SELECT msg_id, output_tokens FROM session_entries"
    ).fetchall()
    assert rows == [("m1", 3881)]


def test_speed_set_breaks_tie(cache_db):
    """Equal token totals: the row with `speed` set wins."""
    cache_db.execute(UPSERT_SQL, _row("m1", "r1", out_tokens=100))
    cache_db.execute(UPSERT_SQL, _row("m1", "r1", out_tokens=100, speed="standard"))
    rows = cache_db.execute(
        "SELECT msg_id, output_tokens, json_extract(usage_extra_json, '$.speed') "
        "FROM session_entries"
    ).fetchall()
    assert rows == [("m1", 100, "standard")]


def test_null_key_fallthrough(cache_db):
    """Two rows with NULL msg_id: both land (partial unique index excludes
    them from the dedup target)."""
    cache_db.execute(UPSERT_SQL, _row(None, None, out_tokens=10))
    cache_db.execute(UPSERT_SQL, _row(None, None, out_tokens=20))
    rows = cache_db.execute(
        "SELECT output_tokens FROM session_entries ORDER BY output_tokens"
    ).fetchall()
    assert rows == [(10,), (20,)]


def test_should_replace_helper(lib_jsonl):
    """Pure-fn tests on the helper that direct-parse uses."""
    UsageEntry = lib_jsonl.UsageEntry
    def make(out, speed=None):
        u = {"input_tokens": 0, "output_tokens": out,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        if speed is not None:
            u["speed"] = speed
        return UsageEntry(timestamp=None, model="m", usage=u, cost_usd=None)

    sr = lib_jsonl._should_replace
    assert sr(make(3881), make(1)) is True               # higher wins
    assert sr(make(1), make(3881)) is False              # don't replace higher
    assert sr(make(100, "standard"), make(100)) is True  # tie → speed wins
    assert sr(make(100), make(100, "standard")) is False
    assert sr(make(100), make(100)) is False             # equal, no signal


def test_direct_parse_picks_higher(lib_jsonl, tmp_path):
    """End-to-end via the direct-JSONL fallback path."""
    import datetime as dt
    fp = tmp_path / "session.jsonl"
    rows = [
        {"type": "assistant", "timestamp": "2026-05-22T17:04:27.000Z",
         "requestId": "r1",
         "message": {"id": "m1", "model": "claude-opus-4-7",
                     "usage": {"input_tokens": 1, "output_tokens": 1,
                               "cache_creation_input_tokens": 872,
                               "cache_read_input_tokens": 106948}}},
        {"type": "assistant", "timestamp": "2026-05-22T17:04:38.000Z",
         "requestId": "r1",
         "message": {"id": "m1", "model": "claude-opus-4-7",
                     "usage": {"input_tokens": 1, "output_tokens": 3881,
                               "cache_creation_input_tokens": 872,
                               "cache_read_input_tokens": 106948,
                               "speed": "standard"}}},
    ]
    fp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    start = dt.datetime(2026, 5, 22, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 5, 22, 23, 59, tzinfo=dt.timezone.utc)
    dedupe_map: dict = {}
    out = lib_jsonl._parse_usage_entries(fp, start, end, dedupe_map=dedupe_map)
    # The dedup-keyed entries live in dedupe_map; no_key returns only
    # null-key entries. So out (no_key) should be empty and dedupe_map
    # should contain the higher-tokens entry.
    assert out == []
    assert len(dedupe_map) == 1
    only = next(iter(dedupe_map.values()))
    assert only.usage["output_tokens"] == 3881


def test_synthetic_model_dropped_at_ingest(lib_jsonl, tmp_path):
    """Rows whose model is '<synthetic>' are dropped, matching ccusage's
    claude_loader.rs:454."""
    import datetime as dt
    fp = tmp_path / "session.jsonl"
    rows = [
        {"type": "assistant", "timestamp": "2026-05-22T17:04:27.000Z",
         "requestId": "r1",
         "message": {"id": "m1", "model": "<synthetic>",
                     "usage": {"input_tokens": 1, "output_tokens": 50,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0}}},
        {"type": "assistant", "timestamp": "2026-05-22T17:04:30.000Z",
         "requestId": "r2",
         "message": {"id": "m2", "model": "claude-opus-4-7",
                     "usage": {"input_tokens": 1, "output_tokens": 100,
                               "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0}}},
    ]
    fp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    start = dt.datetime(2026, 5, 22, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 5, 22, 23, 59, tzinfo=dt.timezone.utc)
    dedupe_map: dict = {}
    no_key = lib_jsonl._parse_usage_entries(
        fp, start, end, dedupe_map=dedupe_map,
    )
    # Only the non-<synthetic> row should appear (in the dedupe map since
    # it has both msg_id and req_id).
    assert no_key == []
    assert len(dedupe_map) == 1
    only = next(iter(dedupe_map.values()))
    assert only.model == "claude-opus-4-7"
    assert only.usage["output_tokens"] == 100
