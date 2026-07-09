"""#279 S2 F1 / Codex P1-2: anomaly-delta-gated cache_meta persistence."""
import json
import sqlite3
import sys

import pytest

from conftest import load_script


@pytest.fixture
def mod():
    """The _cctally_cache sibling (carries IngestStats +
    _update_parse_health_meta)."""
    load_script()
    return sys.modules["_cctally_cache"]


@pytest.fixture
def cache_conn():
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


def _read_meta(conn, key):
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return json.loads(row[0]) if row else None


def test_first_adoption_writes_even_when_clean(cache_conn, mod):
    mod._update_parse_health_meta(
        cache_conn, "parse_health_claude",
        lines_seen=10, lines_malformed=0, lines_skipped=0,
        skip_reasons={}, rebuild=False)
    rec = _read_meta(cache_conn, "parse_health_claude")
    assert rec["schema"] == 1 and rec["lines_seen"] == 10
    assert rec["last_anomaly_at"] is None and rec["since"]


def test_clean_sync_after_adoption_is_zero_write(cache_conn, mod):
    fn = mod._update_parse_health_meta
    fn(cache_conn, "k", lines_seen=5, lines_malformed=0,
       lines_skipped=0, skip_reasons={}, rebuild=False)
    first = _read_meta(cache_conn, "k")
    fn(cache_conn, "k", lines_seen=7, lines_malformed=0,
       lines_skipped=0, skip_reasons={}, rebuild=False)
    assert _read_meta(cache_conn, "k") == first     # byte-stable: no write


def test_anomaly_delta_accumulates_and_stamps(cache_conn, mod):
    fn = mod._update_parse_health_meta
    fn(cache_conn, "k", lines_seen=5, lines_malformed=0,
       lines_skipped=0, skip_reasons={}, rebuild=False)
    fn(cache_conn, "k", lines_seen=3, lines_malformed=2,
       lines_skipped=1, skip_reasons={"no-usage": 1}, rebuild=False)
    rec = _read_meta(cache_conn, "k")
    assert rec["lines_seen"] == 8 and rec["lines_malformed"] == 2
    assert rec["lines_skipped"] == 1
    assert rec["reasons"] == {"no-usage": 1}
    assert rec["last_anomaly_at"] is not None


def test_rebuild_resets_baseline(cache_conn, mod):
    fn = mod._update_parse_health_meta
    fn(cache_conn, "k", lines_seen=5, lines_malformed=9,
       lines_skipped=0, skip_reasons={"x": 9}, rebuild=False)
    fn(cache_conn, "k", lines_seen=100, lines_malformed=0,
       lines_skipped=0, skip_reasons={}, rebuild=True)
    rec = _read_meta(cache_conn, "k")
    assert rec["lines_malformed"] == 0 and rec["lines_seen"] == 100
    assert rec["reasons"] == {} and rec["last_anomaly_at"] is None


def test_end_to_end_sync_cache_records_and_is_zero_write_second_time(
        tmp_path, monkeypatch):
    ns = load_script()
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)

    projects = tmp_path / ".claude" / "projects" / "-Users-u-project-A"
    projects.mkdir(parents=True)
    healthy = json.dumps({
        "type": "assistant", "timestamp": "2026-07-01T10:00:00Z",
        "requestId": "req_1",
        "message": {"id": "msg_1", "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 5, "output_tokens": 7}},
    })
    lines = [healthy, "{ this is not valid json"]
    (projects / "sess-a.jsonl").write_text("\n".join(lines) + "\n")

    sync_cache = ns["sync_cache"]
    open_cache_db = ns["open_cache_db"]
    conn = open_cache_db()
    try:
        sync_cache(conn)
        rec = _read_meta(conn, "parse_health_claude")
        assert rec is not None
        assert rec["lines_malformed"] == 1
        first = json.dumps(rec, sort_keys=True)
        # A second sync ingests no new bytes and no anomaly -> zero-write.
        sync_cache(conn)
        rec2 = _read_meta(conn, "parse_health_claude")
        assert json.dumps(rec2, sort_keys=True) == first
    finally:
        conn.close()
