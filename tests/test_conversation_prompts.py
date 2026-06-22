"""Kernel + HTTP tests for ``get_conversation_prompts`` / the ``/prompts`` route
(#217 S7, F10 — session comparison data spine).

The prompt spine reuses the SAME ``_assemble_session`` pass as the outline and the
SAME main-thread predicate as the recipe/prompts export (``_human_prompts`` /
``_item_text`` in ``_lib_conversation_export``), so the route and the export can
never drift. Contract under test: ordered main-thread human prompts only — no
sidechain, no empty-text rows — each carrying its anchor uuid; queued-command
prompts (already promoted to plain human rows by the parser) are included; an
unknown session id returns ``None`` (the handler's 404 sentinel).

Standalone-by-convention: the ``_conn``/``_msg`` helpers mirror
``test_conversation_outline.py``.
"""
import json as _json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db
import _lib_conversation_query as cq


def _conn():
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)
    return c


def _msg(c, **kw):
    # The #177 enrichment columns (stop_reason / attribution_skill /
    # attribution_plugin) are TAIL-APPENDED, matching the production INSERT
    # tuple (copied from test_conversation_outline.py).
    cols = ("session_id", "uuid", "parent_uuid", "source_path", "byte_offset",
            "timestamp_utc", "entry_type", "text", "blocks_json", "model",
            "msg_id", "req_id", "cwd", "git_branch", "is_sidechain",
            "source_tool_use_id", "stop_reason", "attribution_skill",
            "attribution_plugin")
    row = {k: kw.get(k) for k in cols}
    row["blocks_json"] = kw.get("blocks_json", "[]")
    row["text"] = kw.get("text", "")
    row["is_sidechain"] = kw.get("is_sidechain", 0)
    c.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,is_sidechain,"
        " source_tool_use_id,stop_reason,attribution_skill,attribution_plugin)"
        " VALUES(:session_id,:uuid,:parent_uuid,:source_path,:byte_offset,"
        ":timestamp_utc,:entry_type,:text,:blocks_json,:model,:msg_id,:req_id,"
        ":cwd,:git_branch,:is_sidechain,:source_tool_use_id,:stop_reason,"
        ":attribution_skill,:attribution_plugin)", row)


def _seed_session_s1(c, sid="s1"):
    """Document order:
        human "Refactor auth"  (main)                -> INCLUDED
        assistant "..."        (main)                -> (not a human prompt)
        human "Sub prompt"     (is_sidechain=1)      -> EXCLUDED (subagent thread)
        human ""    (empty)    (main)                -> EXCLUDED (no prose)
        human "Run tests"      (main)                -> INCLUDED
        human "/deploy"        (main, queued cmd)    -> INCLUDED
    The "/deploy" row stands in for a queued_command attachment that the parser
    (``iter_message_rows``) has ALREADY promoted to a plain ``entry_type=human``
    ``conversation_messages`` row with the args in ``text`` — by the time the
    kernel reads the table the normalization has happened, so it is just another
    main-thread human row.
    """
    _msg(c, session_id=sid, uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human",
         text="Refactor auth")
    _msg(c, session_id=sid, uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-12T14:00:05Z", entry_type="assistant",
         text="working on it", model="claude-opus-4-8", msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([{"kind": "text", "text": "working on it"}]))
    # A sidechain (subagent) human turn — EXCLUDED by the main-thread predicate.
    _msg(c, session_id=sid, uuid="sc1",
         source_path="b.jsonl/subagents/x.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:10Z", entry_type="human",
         text="Sub prompt", is_sidechain=1)
    # An empty-prose main human row — EXCLUDED (non-empty-text filter).
    _msg(c, session_id=sid, uuid="h2", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-12T14:00:15Z", entry_type="human", text="")
    _msg(c, session_id=sid, uuid="h3", source_path="a.jsonl", byte_offset=3,
         timestamp_utc="2026-06-12T14:00:20Z", entry_type="human",
         text="Run tests")
    # A promoted queued-command human row — INCLUDED.
    _msg(c, session_id=sid, uuid="q1", parent_uuid="p0", source_path="a.jsonl",
         byte_offset=4, timestamp_utc="2026-06-12T14:00:25Z", entry_type="human",
         text="/deploy")


def test_get_conversation_prompts_main_thread_only():
    c = _conn()
    _seed_session_s1(c)
    out = cq.get_conversation_prompts(c, "s1")
    assert out["session_id"] == "s1"
    texts = [p["text"] for p in out["prompts"]]
    assert texts == ["Refactor auth", "Run tests", "/deploy"]   # order, no sidechain/empty
    assert all(p["uuid"] for p in out["prompts"])               # every entry carries its uuid


def test_get_conversation_prompts_unknown_id_returns_none():
    c = _conn()
    assert cq.get_conversation_prompts(c, "does-not-exist") is None
