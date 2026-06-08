"""#164 Task A4: the flag-only cache migration
``003_conversation_reingest_tool_ids`` + its ``sync_cache`` consume site.

The migration handler is flag-only: it sets the ``conversation_reingest_pending``
cache_meta flag and returns (the destructive clear + offset-0 re-ingest run in
``sync_cache`` UNDER the held ``cache.db.lock`` flock, mirroring 002's
defer-to-sync pattern — clearing in the handler would race a concurrent sync and
empty the reader on ``--no-sync`` / eager opens). The consume site clears
``conversation_messages`` then re-derives it id-aware via the offset-0
``backfill_conversation_messages`` walk, then clears the flag last so a crash
mid-walk re-runs cleanly on the next sync.

This drives the consume site through the real ``sync_cache`` (the established
``load_script() + redirect_paths()`` harness, per the "HOME-only test loader
reads prod DB" gotcha). It seeds an id-LESS ``conversation_messages`` row, sets
the flag, points the JSONL walk at a fixture whose ``tool_use`` carries an id,
runs ``sync_cache``, and asserts the row is re-ingested id-aware AND the flag is
cleared.
"""
import json
import sqlite3
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db

from conftest import load_script, redirect_paths


def _asst_tool_use_line(uuid, msg_id, req_id, tool_id, *,
                        ts="2026-06-01T00:00:00Z", model="claude-opus-4-7"):
    """One assistant JSONL line whose content carries an id-bearing tool_use."""
    return json.dumps({
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "s1",
        "requestId": req_id,
        "timestamp": ts,
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "tool_use", "id": tool_id, "name": "Read",
                         "input": {"file_path": "/x/y.py"}}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    conn = ns["open_cache_db"]()
    sync_cache = ns["sync_cache"]
    yield ns, conn, projects, sync_cache
    try:
        conn.close()
    except Exception:
        pass


def test_handler_is_flag_only_does_not_clear():
    """The 003 handler ONLY sets the flag — it must NOT touch
    conversation_messages (the clear is deferred to sync_cache under the
    flock)."""
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    # an existing id-less row (pre-feature shape)
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,"
        " text,blocks_json,is_sidechain) "
        "VALUES('s1','a1','a.jsonl',0,'t','assistant','',"
        "'[{\"kind\":\"tool_use\",\"name\":\"Read\",\"input_summary\":\"{}\"}]',0)")
    conn.commit()

    handler = None
    for m in db._CACHE_MIGRATIONS:
        if m.name == "003_conversation_reingest_tool_ids":
            handler = m.handler
            break
    assert handler is not None, "003_conversation_reingest_tool_ids not registered"
    handler(conn)

    # flag set, table UNCHANGED (the handler does not clear).
    assert conn.execute(
        "SELECT value FROM cache_meta WHERE key='conversation_reingest_pending'"
    ).fetchone() == ("1",)
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 1


def test_reingest_flag_consumed_clears_and_repopulates(isolated):
    """The full consume path: a set flag + id-less seeded row + an id-bearing
    JSONL -> sync_cache clears + re-ingests id-aware AND clears the flag."""
    ns, conn, projects, sync_cache = isolated

    # 1) an id-LESS conversation_messages row (pre-feature shape) for a DIFFERENT
    #    physical path than the JSONL we will walk, so it can only survive if the
    #    clear is skipped. Its blocks_json carries no "id".
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,"
        " text,blocks_json,is_sidechain) "
        "VALUES('s1','old','/stale/path.jsonl',0,'2026-06-01T00:00:00Z',"
        "'assistant','',"
        "'[{\"kind\":\"tool_use\",\"name\":\"Read\",\"input_summary\":\"{}\"}]',0)")
    # 2) set the reingest-pending flag (what migration 003 would do)
    conn.execute(
        "INSERT INTO cache_meta(key,value) VALUES('conversation_reingest_pending','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    conn.commit()

    # 3) write a real JSONL whose tool_use carries an id
    (projects / "a.jsonl").write_text(
        _asst_tool_use_line("a1", "m1", "r1", "toolu_xyz"))

    # 4) run sync_cache (consumes the flag under the flock)
    sync_cache(conn)

    # 5a) the flag is cleared
    assert conn.execute(
        "SELECT value FROM cache_meta WHERE key='conversation_reingest_pending'"
    ).fetchone() is None

    # 5b) the stale id-less row was wiped by the clear (re-ingest is offset-0 of
    #     on-disk JSONL only — the /stale/path.jsonl row is not re-derivable)
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE uuid='old'"
    ).fetchone()[0] == 0

    # 5c) the JSONL row is now present AND id-aware (blocks_json carries the id)
    row = conn.execute(
        "SELECT blocks_json FROM conversation_messages WHERE uuid='a1'"
    ).fetchone()
    assert row is not None
    blocks = json.loads(row[0])
    assert blocks[0]["kind"] == "tool_use"
    assert blocks[0]["id"] == "toolu_xyz"
    assert "preview" in blocks[0]


def test_reingest_consume_is_idempotent_when_flag_absent(isolated):
    """A plain sync_cache with NO flag set must not clear the index."""
    ns, conn, projects, sync_cache = isolated
    (projects / "a.jsonl").write_text(
        _asst_tool_use_line("a1", "m1", "r1", "toolu_xyz"))
    sync_cache(conn)
    before = conn.execute(
        "SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
    assert before == 1
    # no flag set; a second sync must be a no-op for the index (no clear)
    sync_cache(conn)
    after = conn.execute(
        "SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
    assert after == 1
