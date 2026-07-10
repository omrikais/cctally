"""Skill-content nesting: the flag-only cache migration
``006_conversation_reingest_source_tool_use_id`` + its ``sync_cache`` consume
site.

The handler is flag-only: it sets a DISTINCT cache_meta flag,
``conversation_source_tool_use_reingest_pending``, and returns — NOT the shared
``conversation_reingest_pending`` that migrations 003/004/005 reuse. Reusing the
shared flag would re-arm migration 005's read-time *human*-fallback after it was
consumed (a genuine human prompt starting with the skill preamble could then be
misclassified as a collapsed skill pill during the pre-reingest window). The
destructive clear + offset-0 re-ingest run in ``sync_cache`` UNDER the held
``cache.db.lock`` flock; that consume site now triggers on EITHER flag and
clears BOTH atomically.

Mirrors ``test_migration_003_reingest.py``'s harness (``load_script() +
redirect_paths()``, per the "HOME-only test loader reads prod DB" gotcha). The
per-migration pre/post golden now lives in
``test_cache_migration_006_per_migration_goldens.py`` (#279 S7 W3 backfill — the
lazy-adoption gap was closed and the registry-completeness guard now enforces a
golden for EVERY migration); the ``test_migration_006_*`` filename here covers
the CACHE migration's sync_cache consume site, distinct from the stats-006
golden test ``test_migration_006_per_migration_goldens.py``.
"""
import json
import sqlite3
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db

from conftest import load_script, redirect_paths


def _skill_triple_jsonl(tool_id, *, ts="2026-06-01T00:00:00Z",
                        model="claude-opus-4-7"):
    """Three JSONL lines: a Skill tool_use, its tool_result, and the isMeta
    skill body carrying sourceToolUseID == tool_id."""
    asst = json.dumps({
        "type": "assistant", "uuid": "a1", "sessionId": "s1", "requestId": "r1",
        "timestamp": ts,
        "message": {
            "role": "assistant", "id": "m1", "model": model,
            "content": [{"type": "tool_use", "id": tool_id, "name": "Skill",
                         "input": {"skill": "brainstorming"}}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"
    result = json.dumps({
        "type": "user", "uuid": "u-res", "parentUuid": "a1", "sessionId": "s1",
        "timestamp": ts,
        "message": {"role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                 "content": "Launching skill: brainstorming"}]},
    }) + "\n"
    body = json.dumps({
        "type": "user", "uuid": "u-body", "parentUuid": "u-res",
        "sessionId": "s1", "timestamp": ts, "isMeta": True,
        "sourceToolUseID": tool_id,
        "message": {"role": "user", "content": [
            {"type": "text",
             "text": "Base directory for this skill: /x/skills/brainstorming"}]},
    }) + "\n"
    return asst + result + body


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


def _handler():
    for m in db._CACHE_MIGRATIONS:
        if m.name == "006_conversation_reingest_source_tool_use_id":
            return m.handler
    raise AssertionError(
        "006_conversation_reingest_source_tool_use_id not registered")


def test_handler_sets_distinct_flag_only():
    """The 006 handler sets the DISTINCT flag and must NOT set the shared 005
    flag (which would re-arm the kernel's human-fallback)."""
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    _handler()(conn)
    assert conn.execute(
        "SELECT value FROM cache_meta "
        "WHERE key='conversation_source_tool_use_reingest_pending'"
    ).fetchone() == ("1",)
    # the shared flag is NOT armed
    assert conn.execute(
        "SELECT value FROM cache_meta WHERE key='conversation_reingest_pending'"
    ).fetchone() is None


def test_distinct_flag_consumed_lands_source_tool_use_id(isolated):
    """The full consume path: the distinct flag + an id-LESS seeded row + a
    JSONL skill triple -> sync_cache clears + re-ingests so the skill body's
    source_tool_use_id lands, AND the distinct flag is cleared."""
    ns, conn, projects, sync_cache = isolated

    # an id-LESS conversation_messages row (pre-006 shape) for a DIFFERENT
    # physical path than the JSONL we walk, so it only survives if the clear is
    # skipped.
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,"
        " text,blocks_json,is_sidechain) "
        "VALUES('s1','old','/stale/path.jsonl',0,'2026-06-01T00:00:00Z',"
        "'meta','','[]',0)")
    # set the DISTINCT reingest-pending flag (what migration 006 would do)
    conn.execute(
        "INSERT INTO cache_meta(key,value) "
        "VALUES('conversation_source_tool_use_reingest_pending','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    conn.commit()

    (projects / "a.jsonl").write_text(_skill_triple_jsonl("toolu_S"))

    sync_cache(conn)

    # the distinct flag is cleared
    assert conn.execute(
        "SELECT value FROM cache_meta "
        "WHERE key='conversation_source_tool_use_reingest_pending'"
    ).fetchone() is None

    # #179: the resumable per-file reingest replaced the old global
    # clear_conversation_messages, so it no longer purges rows for JSONL files
    # no longer on disk — the per-file walk only visits on-disk files. The
    # /stale/path.jsonl orphan therefore SURVIVES until a `cache-sync --rebuild`
    # (spec §3 item 3: deliberate, matching sync_cache's existing no-prune
    # posture for orphans on the normal path).
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE uuid='old'"
    ).fetchone()[0] == 1

    # the skill body row now carries source_tool_use_id
    row = conn.execute(
        "SELECT source_tool_use_id, entry_type FROM conversation_messages "
        "WHERE uuid='u-body'"
    ).fetchone()
    assert row is not None
    assert row[0] == "toolu_S"
    assert row[1] == "meta"
