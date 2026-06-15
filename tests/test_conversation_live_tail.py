import json
import sqlite3
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db
import _cctally_cache as cc
import _lib_conversation_query as cq

_MODEL = "claude-opus-4-8"  # real id from CLAUDE_MODEL_PRICING so cost > 0


def _conn():
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)
    return c


def _msg(c, **kw):
    cols = ("session_id", "uuid", "parent_uuid", "source_path", "byte_offset",
            "timestamp_utc", "entry_type", "text", "blocks_json", "model",
            "msg_id", "req_id", "cwd", "git_branch", "is_sidechain",
            "source_tool_use_id", "stop_reason", "attribution_skill",
            "attribution_plugin")
    row = {k: kw.get(k) for k in cols}
    row["blocks_json"] = kw.get("blocks_json", "[]")
    row["text"] = kw.get("text", "")
    row["is_sidechain"] = kw.get("is_sidechain", 0)
    row["search_tool"] = kw.get("search_tool", "")
    row["search_thinking"] = kw.get("search_thinking", "")
    row["search_aux"] = kw.get("search_aux", "")
    c.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,is_sidechain,"
        " source_tool_use_id,stop_reason,attribution_skill,attribution_plugin,"
        " search_tool,search_thinking,search_aux)"
        " VALUES(:session_id,:uuid,:parent_uuid,:source_path,:byte_offset,"
        ":timestamp_utc,:entry_type,:text,:blocks_json,:model,:msg_id,:req_id,"
        ":cwd,:git_branch,:is_sidechain,:source_tool_use_id,:stop_reason,"
        ":attribution_skill,:attribution_plugin,"
        ":search_tool,:search_thinking,:search_aux)", row)


def test_session_source_paths_returns_distinct_files():
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="/p/a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="hi")
    _msg(c, session_id="s1", uuid="a1", source_path="/p/a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant", text="x",
         model=_MODEL, msg_id="m1", req_id="r1")
    _msg(c, session_id="s1", uuid="a2", source_path="/p/b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:01:00Z", entry_type="assistant", text="y",
         model=_MODEL, msg_id="m2", req_id="r2")
    _msg(c, session_id="s2", uuid="o1", source_path="/p/c.jsonl", byte_offset=0,
         timestamp_utc="2026-06-02T00:00:00Z", entry_type="human", text="z")
    got = sorted(cq.session_source_paths(c, "s1"))
    assert got == ["/p/a.jsonl", "/p/b.jsonl"]


def test_session_source_paths_unknown_session_is_empty():
    c = _conn()
    assert cq.session_source_paths(c, "nope") == []
