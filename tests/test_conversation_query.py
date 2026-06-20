import sqlite3, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db
import _cctally_cache as cc
import _lib_conversation_query as cq

# A real model id from CLAUDE_MODEL_PRICING so token-derived cost is genuinely
# non-zero (the plan's placeholder "opus" resolves to None -> $0, which can't
# satisfy the cost_usd > 0 assertions). The kernel logic is identical; only the
# fixture literal needs to be a recognized model.
_MODEL = "claude-opus-4-8"


def _conn():
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)
    return c


def _list_conversations(c, **kw):
    """list_conversations with the browse-rail rollup populated first, so these
    direct-seed tests exercise the FAST rollup read path (the production read
    when conversation_sessions is authoritative) rather than only the live
    GROUP-BY fallback. In production sync_cache maintains the rollup; here we
    direct-insert conversation_messages and never run sync_cache, so we recompute
    it explicitly. No backfill flag is armed, so _rollup_authoritative is True
    and list_conversations reads conversation_sessions (the production fast
    path)."""
    cc._recompute_conversation_sessions(c)
    return cq.list_conversations(c, **kw)


def _msg(c, **kw):
    # The #177 enrichment columns (stop_reason / attribution_skill /
    # attribution_plugin / search_tool / search_thinking) are TAIL-APPENDED,
    # matching the production INSERT tuple. #177 S6: search_tool / search_thinking
    # carry the split non-prose index. (#217 S1 / U7a: the documented-dead
    # search_aux column was dropped from the live schema by migration 016, so it
    # is no longer inserted here.)
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
    row["id"] = kw.get("id")   # explicit rowid when a test pins it (else autoinc)
    id_col = "id," if row["id"] is not None else ""
    id_val = ":id," if row["id"] is not None else ""
    c.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        f"({id_col}session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,is_sidechain,"
        " source_tool_use_id,stop_reason,attribution_skill,attribution_plugin,"
        " search_tool,search_thinking)"
        f" VALUES({id_val}:session_id,:uuid,:parent_uuid,:source_path,:byte_offset,"
        ":timestamp_utc,:entry_type,:text,:blocks_json,:model,:msg_id,:req_id,"
        ":cwd,:git_branch,:is_sidechain,:source_tool_use_id,:stop_reason,"
        ":attribution_skill,:attribution_plugin,"
        ":search_tool,:search_thinking)", row)


def _entry(c, *, source_path, line_offset, model, msg_id, req_id,
           inp=0, out=0, cc=0, cr=0, cost_usd_raw=None):
    # cost_usd_raw is the vendor-provided override the cost helper honors when
    # present (bypassing token-derived math) — the #177 "same source row, not
    # same arithmetic" guard seeds it to prove tokens surface independently.
    c.execute(
        "INSERT OR IGNORE INTO session_entries "
        "(source_path,line_offset,timestamp_utc,model,msg_id,req_id,"
        " input_tokens,output_tokens,cache_create_tokens,cache_read_tokens,cost_usd_raw)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (source_path, line_offset, "t", model, msg_id, req_id,
         inp, out, cc, cr, cost_usd_raw))


def test_list_conversations_groups_by_session_with_cost():
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="hi",
         cwd="/home/u/proj", git_branch="main")
    _msg(c, session_id="s1", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant", text="hello",
         model=_MODEL, msg_id="m1", req_id="r1")
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=1000, out=500)
    _msg(c, session_id="s2", uuid="h2", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-02T00:00:00Z", entry_type="human", text="yo",
         cwd="/home/u/other")
    out = _list_conversations(c, sort="recent", limit=50, offset=0)
    rows = out["conversations"]
    assert [r["session_id"] for r in rows] == ["s2", "s1"]          # recent first
    s1 = next(r for r in rows if r["session_id"] == "s1")
    assert s1["project_label"] == "proj"                            # basename only
    assert s1["git_branch"] == "main"
    assert s1["msg_count"] == 2
    assert s1["cost_usd"] > 0
    assert s1["started_utc"] == "2026-06-01T00:00:00Z"
    assert s1["last_activity_utc"] == "2026-06-01T00:00:05Z"
    assert out["page"]["has_more"] is False


def test_list_conversations_pagination():
    c = _conn()
    for i in range(5):
        _msg(c, session_id=f"s{i}", uuid=f"u{i}", source_path=f"{i}.jsonl",
             byte_offset=0, timestamp_utc=f"2026-06-0{i+1}T00:00:00Z",
             entry_type="human", text="x")
    page = _list_conversations(c, sort="recent", limit=2, offset=0)
    assert len(page["conversations"]) == 2
    assert page["page"]["has_more"] is True
    assert page["page"]["next_offset"] == 2
    last = _list_conversations(c, sort="recent", limit=2, offset=4)
    assert len(last["conversations"]) == 1
    assert last["page"]["has_more"] is False


# ---------------------------------------------------------------------------
# Task 5: reader (get_conversation)
# ---------------------------------------------------------------------------
import json as _json


def test_get_conversation_dedups_and_groups_turns_cost_once():
    c = _conn()
    # human prompt
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="question",
         cwd="/home/u/proj", git_branch="main")
    # assistant turn (m1,r1) split into 2 fragments: thinking-only + prose
    _msg(c, session_id="s1", uuid="a1a", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:04Z", entry_type="assistant", text="",
         blocks_json=_json.dumps([{"kind": "thinking", "text": "..."}]),
         model=_MODEL, msg_id="m1", req_id="r1")
    _msg(c, session_id="s1", uuid="a1b", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant", text="answer",
         blocks_json=_json.dumps([{"kind": "text", "text": "answer"}]),
         model=_MODEL, msg_id="m1", req_id="r1")
    # replay of the SAME turn in b.jsonl (resume) — must NOT double the cost
    _msg(c, session_id="s1", uuid="a1b", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant", text="answer",
         blocks_json=_json.dumps([{"kind": "text", "text": "answer"}]),
         model=_MODEL, msg_id="m1", req_id="r1")
    _entry(c, source_path="a.jsonl", line_offset=2, model=_MODEL,
           msg_id="m1", req_id="r1", inp=1000, out=500)   # ONE session_entries row
    out = cq.get_conversation(c, "s1", after=None, limit=500)
    items = out["items"]
    assert [it["kind"] for it in items] == ["human", "assistant"]   # 1 human + 1 turn
    turn = items[1]
    assert turn["model"] == _MODEL
    assert turn["text"] == "answer"                                 # merged prose
    assert turn["anchor"]["uuid"] == "a1b"                          # prose-bearing fragment
    assert turn["anchor"]["session_id"] == "s1"                     # anchor carries session
    assert set(turn["member_uuids"]) == {"a1a", "a1b"}              # all fragments map here
    assert turn["cost_usd"] > 0
    # cost counted ONCE despite the cross-file replay
    assert abs(sum(it.get("cost_usd", 0.0) for it in items) - out["cost_usd"]) < 1e-9
    assert out["project_label"] == "proj" and out["git_branch"] == "main"


def test_get_conversation_interleaved_tool_result_coalesces_one_turn_cost_once():
    # C1 regression (#164-updated): a tool-using turn interleaves a tool_result
    # (user) line BETWEEN two assistant fragments sharing the SAME (msg_id,
    # req_id). The turn coalesces to exactly ONE assistant item (grouping over
    # the whole logical list, NOT by adjacency). With the id-bearing tool pair
    # the tool_result now FOLDS into that turn's tool_call.result (#164 Task A3)
    # — it is NOT a standalone item — and its uuid joins the turn's
    # member_uuids. Cost must be counted ONCE — proven with MULTIPLE turns so
    # the cardinality invariant sum(assistant item cost) == header cost is
    # non-vacuous.
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="do a thing",
         cwd="/home/u/proj", git_branch="main")
    # turn 1 (m1,r1) fragment A: thinking + tool_use (no prose), id-bearing
    _msg(c, session_id="s1", uuid="t1a", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant", text="",
         blocks_json=_json.dumps([{"kind": "thinking", "text": "plan"},
                                  {"kind": "tool_use", "name": "Bash",
                                   "input_summary": "{}", "id": "t1",
                                   "preview": "ls"}]),
         model=_MODEL, msg_id="m1", req_id="r1")
    # tool_result (user) BREAKS the adjacency run within turn 1; its
    # tool_use_id matches t1 so it folds into turn 1's tool_call.
    _msg(c, session_id="s1", uuid="tr1", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-01T00:00:02Z", entry_type="tool_result",
         text="",
         blocks_json=_json.dumps([{"kind": "tool_result", "text": "out",
                                   "truncated": False, "is_error": False,
                                   "tool_use_id": "t1"}]))
    # turn 1 (m1,r1) fragment B: SAME msg_id/req_id, carries the prose
    _msg(c, session_id="s1", uuid="t1b", source_path="a.jsonl", byte_offset=3,
         timestamp_utc="2026-06-01T00:00:03Z", entry_type="assistant", text="done",
         blocks_json=_json.dumps([{"kind": "text", "text": "done"}]),
         model=_MODEL, msg_id="m1", req_id="r1")
    _entry(c, source_path="a.jsonl", line_offset=3, model=_MODEL,
           msg_id="m1", req_id="r1", inp=2000, out=800)   # ONE row for turn 1
    # a SECOND distinct turn (m2,r2) so the invariant is multi-turn
    _msg(c, session_id="s1", uuid="t2", source_path="a.jsonl", byte_offset=4,
         timestamp_utc="2026-06-01T00:00:04Z", entry_type="assistant", text="follow up",
         blocks_json=_json.dumps([{"kind": "text", "text": "follow up"}]),
         model=_MODEL, msg_id="m2", req_id="r2")
    _entry(c, source_path="a.jsonl", line_offset=4, model=_MODEL,
           msg_id="m2", req_id="r2", inp=1000, out=400)   # ONE row for turn 2

    out = cq.get_conversation(c, "s1", after=None, limit=500)
    items = out["items"]
    # human, turn-1 (coalesced; the tool_result FOLDED in), turn-2. The
    # tool_result is no longer a standalone item (#164).
    assert [it["kind"] for it in items] == ["human", "assistant", "assistant"]
    assert all(it["kind"] != "tool_result" for it in items)
    turn1 = items[1]
    # exactly ONE assistant item for the interleaved turn, with ALL fragment
    # uuids PLUS the folded tool_result uuid (#160 anchor).
    assert set(turn1["member_uuids"]) == {"t1a", "t1b", "tr1"}
    assert turn1["text"] == "done"                 # prose-bearing fragment
    assert turn1["anchor"]["uuid"] == "t1b"
    # the tool_use became a tool_call carrying the folded result.
    call = next(b for b in turn1["blocks"] if b["kind"] == "tool_call")
    assert call["tool_use_id"] == "t1"
    assert call["result"]["text"] == "out"
    # exactly two distinct assistant items, each with its own non-zero cost
    asst = [it for it in items if it["kind"] == "assistant"]
    assert len(asst) == 2
    assert all(it["cost_usd"] > 0 for it in asst)
    # the cardinality invariant: cost counted ONCE per turn, header == sum, multi-turn
    assert abs(sum(it["cost_usd"] for it in asst) - out["cost_usd"]) < 1e-9
    assert out["cost_usd"] > 0


def test_get_conversation_null_msg_id_assistant_does_not_crash():
    # I2 regression: an assistant row with msg_id=None routes to _build_simple
    # (kind="assistant", no internal _msg_id/_req_id). The cost loop must NOT
    # KeyError on it; the item carries an explicit cost_usd of 0.0.
    c = _conn()
    _msg(c, session_id="s5", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="hi")
    _msg(c, session_id="s5", uuid="anull", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="no turn key here", model=_MODEL, msg_id=None, req_id=None)
    # a real turn so the header is non-zero and the invariant is non-vacuous
    _msg(c, session_id="s5", uuid="areal", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-01T00:00:02Z", entry_type="assistant", text="answer",
         model=_MODEL, msg_id="m1", req_id="r1")
    _entry(c, source_path="a.jsonl", line_offset=2, model=_MODEL,
           msg_id="m1", req_id="r1", inp=1000, out=500)

    out = cq.get_conversation(c, "s5", after=None, limit=500)   # must NOT raise
    items = out["items"]
    null_item = next(it for it in items if it["anchor"]["uuid"] == "anull")
    assert null_item["kind"] == "assistant"
    assert null_item["cost_usd"] == 0.0
    assert "_msg_id" not in null_item and "_req_id" not in null_item
    asst = [it for it in items if it["kind"] == "assistant"]
    assert abs(sum(it["cost_usd"] for it in asst) - out["cost_usd"]) < 1e-9
    assert out["cost_usd"] > 0


def test_get_conversation_stale_cursor_returns_empty_page():
    # M1 regression: a non-None `after` matching no item's anchor (stale/deleted
    # cursor) must return an EMPTY page, never silently re-serve the head.
    c = _conn()
    for i in range(3):
        _msg(c, session_id="s1", uuid=f"u{i}", source_path="a.jsonl", byte_offset=i,
             timestamp_utc=f"2026-06-01T00:00:0{i}Z", entry_type="human", text=f"m{i}")
    out = cq.get_conversation(c, "s1", after="9999999", limit=500)
    assert out["items"] == []
    assert out["page"]["has_more"] is False
    assert out["page"]["next_after"] is None
    # session metadata still populated (not None) — only the page is empty
    assert out["session_id"] == "s1"


def test_get_conversation_synthetic_null_req_id_is_zero_cost():
    c = _conn()
    _msg(c, session_id="s3", uuid="a9", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="t", entry_type="assistant", text="synthetic note",
         model="<synthetic>", msg_id="m9", req_id=None)   # NULL req_id, no session_entries row
    out = cq.get_conversation(c, "s3", after=None, limit=500)
    turn = out["items"][0]
    assert turn["cost_usd"] == 0.0          # by construction, no join
    assert out["cost_usd"] == 0.0


def test_get_conversation_unknown_session_is_none():
    assert cq.get_conversation(_conn(), "nope", after=None, limit=500) is None


def test_get_conversation_cursor_pagination():
    c = _conn()
    for i in range(4):
        _msg(c, session_id="s1", uuid=f"u{i}", source_path="a.jsonl", byte_offset=i,
             timestamp_utc=f"2026-06-01T00:00:0{i}Z", entry_type="human", text=f"m{i}")
    p1 = cq.get_conversation(c, "s1", after=None, limit=2)
    assert len(p1["items"]) == 2 and p1["page"]["has_more"] is True
    p2 = cq.get_conversation(c, "s1", after=p1["page"]["next_after"], limit=2)
    assert len(p2["items"]) == 2 and p2["page"]["has_more"] is False
    # no overlap
    seen = {it["anchor"]["uuid"] for it in p1["items"]} | {it["anchor"]["uuid"] for it in p2["items"]}
    assert len(seen) == 4


# ---------------------------------------------------------------------------
# #164 Task A3: two-phase tool_use<->tool_result pairing into merged tool_call
# blocks. Pairing is by tool_use_id (robust to parallel + reordered results);
# a folded tool_result row's uuid joins the owning turn's member_uuids; orphan
# / multi-owner / mixed rows stay standalone; id-less rows degrade to
# request-only (result:null) + standalone, never crash. Cost-once is preserved.
# ---------------------------------------------------------------------------
_TS = "2026-06-01T00:00:0{}Z"


def _seed_assistant(conn, *, sid, uuid, msg_id, req_id, blocks,
                    ts="2026-06-01T00:00:01Z", model=None, source_path="a.jsonl",
                    byte_offset=0, stop_reason=None, attribution_skill=None,
                    attribution_plugin=None):
    """An assistant turn fragment carrying tool_use blocks (and optionally
    prose). Mirrors the existing _msg INSERT shape. The #177 enrichment fields
    (stop_reason / attribution_*) are optional tail args."""
    _msg(conn, session_id=sid, uuid=uuid, source_path=source_path,
         byte_offset=byte_offset,
         timestamp_utc=ts, entry_type="assistant",
         text="".join(b.get("text", "") for b in blocks if b.get("kind") == "text"),
         blocks_json=_json.dumps(blocks), model=model, msg_id=msg_id, req_id=req_id,
         stop_reason=stop_reason, attribution_skill=attribution_skill,
         attribution_plugin=attribution_plugin)


def _seed_tool_result(conn, *, sid, uuid, blocks,
                      ts="2026-06-01T00:00:02Z", source_path="a.jsonl"):
    """A user/tool_result row carrying one or more tool_result blocks."""
    _msg(conn, session_id=sid, uuid=uuid, source_path=source_path, byte_offset=1,
         timestamp_utc=ts, entry_type="tool_result", text="",
         blocks_json=_json.dumps(blocks))


def _seed_assistant_with_cost(conn, *, sid, uuid, msg_id, req_id, blocks, model,
                              ts="2026-06-01T00:00:01Z", source_path="a.jsonl",
                              inp=1000, out=500, cc=0, cr=0, cost_usd_raw=None,
                              stop_reason=None, attribution_skill=None,
                              attribution_plugin=None):
    """An assistant turn + its single deduped session_entries cost row. The
    token counts and the optional cost_usd_raw override are passed through so a
    raw-cost-override turn can surface tokens AND the override cost (#177)."""
    _seed_assistant(conn, sid=sid, uuid=uuid, msg_id=msg_id, req_id=req_id,
                    blocks=blocks, ts=ts, model=model, source_path=source_path,
                    stop_reason=stop_reason, attribution_skill=attribution_skill,
                    attribution_plugin=attribution_plugin)
    _entry(conn, source_path=source_path, line_offset=0, model=model,
           msg_id=msg_id, req_id=req_id, inp=inp, out=out, cc=cc, cr=cr,
           cost_usd_raw=cost_usd_raw)


def test_pairs_tool_use_with_result_by_id():
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}",
                 "id": "t1", "preview": "/x.py"}])
    _seed_tool_result(conn, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "file body", "truncated": False,
                 "is_error": False, "tool_use_id": "t1"}])
    out = cq.get_conversation(conn, "s1")
    items = out["items"]
    assert all(it["kind"] != "tool_result" for it in items)
    turn = next(it for it in items if it["kind"] == "assistant")
    call = next(b for b in turn["blocks"] if b["kind"] == "tool_call")
    assert call["tool_use_id"] == "t1"
    assert call["preview"] == "/x.py"
    assert call["result"]["text"] == "file body"
    assert call["result"]["is_error"] is False


def test_pairs_parallel_tools_reordered_results():
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}", "id": "t1", "preview": "a"},
                {"kind": "tool_use", "name": "Read", "input_summary": "{}", "id": "t2", "preview": "b"}])
    _seed_tool_result(conn, sid="s1", uuid="u2", ts="2026-06-01T00:00:02Z",
        blocks=[{"kind": "tool_result", "text": "R2", "truncated": False, "is_error": False, "tool_use_id": "t2"}])
    _seed_tool_result(conn, sid="s1", uuid="u1", ts="2026-06-01T00:00:03Z", source_path="b.jsonl",
        blocks=[{"kind": "tool_result", "text": "R1", "truncated": False, "is_error": False, "tool_use_id": "t1"}])
    turn = next(it for it in cq.get_conversation(conn, "s1")["items"] if it["kind"] == "assistant")
    calls = {b["tool_use_id"]: b["result"]["text"] for b in turn["blocks"] if b["kind"] == "tool_call"}
    assert calls == {"t1": "R1", "t2": "R2"}   # paired by id, not by order


def test_result_before_use_in_order_still_pairs():
    # the empirical edge Codex found: a result row sorts BEFORE its use
    conn = _conn()
    _seed_tool_result(conn, sid="s1", uuid="u1", ts="2026-01-01T00:00:00Z",
        blocks=[{"kind": "tool_result", "text": "R", "truncated": False, "is_error": False, "tool_use_id": "t1"}])
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1", ts="2026-01-01T00:00:01Z",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}", "id": "t1", "preview": "a"}])
    turn = next(it for it in cq.get_conversation(conn, "s1")["items"] if it["kind"] == "assistant")
    call = next(b for b in turn["blocks"] if b["kind"] == "tool_call")
    assert call["result"]["text"] == "R"   # two-phase resolves regardless of order


def test_folded_result_uuid_joins_owning_turn_member_uuids():
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}", "id": "t1", "preview": "a"}])
    _seed_tool_result(conn, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "R", "truncated": False, "is_error": False, "tool_use_id": "t1"}])
    turn = next(it for it in cq.get_conversation(conn, "s1")["items"] if it["kind"] == "assistant")
    assert "u1" in turn["member_uuids"]              # #160 anchor preserved
    assert "a1" in turn["member_uuids"]


def test_orphan_result_stays_standalone():
    conn = _conn()
    _seed_tool_result(conn, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "R", "truncated": False, "is_error": False, "tool_use_id": "NOPE"}])
    items = cq.get_conversation(conn, "s1")["items"]
    orphan = next(it for it in items if it["kind"] == "tool_result")
    assert orphan["member_uuids"] == ["u1"]


def test_idless_rows_degrade_request_only_and_standalone():
    # pre-migration data: tool_use has no id, tool_result has no tool_use_id
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}", "id": None, "preview": "a"}])
    _seed_tool_result(conn, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "R", "truncated": False, "is_error": False, "tool_use_id": None}])
    items = cq.get_conversation(conn, "s1")["items"]
    turn = next(it for it in items if it["kind"] == "assistant")
    call = next(b for b in turn["blocks"] if b["kind"] == "tool_call")
    assert call["result"] is None                    # request-only, never crashes
    assert any(it["kind"] == "tool_result" for it in items)  # result stays standalone


def test_multi_owner_result_row_stays_standalone():
    # a single tool_result row whose blocks resolve to TWO different turns must
    # NOT fold (a uuid may join exactly one item's member_uuids).
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1", ts="2026-06-01T00:00:01Z",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}", "id": "t1", "preview": "a"}])
    _seed_assistant(conn, sid="s1", uuid="a2", msg_id="m2", req_id="r2", ts="2026-06-01T00:00:02Z",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}", "id": "t2", "preview": "b"}])
    _seed_tool_result(conn, sid="s1", uuid="u1", ts="2026-06-01T00:00:03Z",
        blocks=[{"kind": "tool_result", "text": "R1", "truncated": False, "is_error": False, "tool_use_id": "t1"},
                {"kind": "tool_result", "text": "R2", "truncated": False, "is_error": False, "tool_use_id": "t2"}])
    items = cq.get_conversation(conn, "s1")["items"]
    assert any(it["kind"] == "tool_result" and it["member_uuids"] == ["u1"] for it in items)
    # neither owning turn got a folded result (request-only)
    for turn in (it for it in items if it["kind"] == "assistant"):
        for b in turn["blocks"]:
            if b["kind"] == "tool_call":
                assert b["result"] is None


def test_cost_once_unchanged_after_folding():
    # one turn with a session_entries cost row + a folded result must keep
    # header == sum of rounded per-item assistant costs.
    conn = _conn()
    _seed_assistant_with_cost(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}", "id": "t1", "preview": "a"}],
        model=_MODEL)
    _seed_tool_result(conn, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "R", "truncated": False, "is_error": False, "tool_use_id": "t1"}])
    out = cq.get_conversation(conn, "s1")
    summed = round(sum(it.get("cost_usd", 0.0) for it in out["items"]), 6)
    assert summed == out["cost_usd"] and out["cost_usd"] > 0


def test_null_msg_id_assistant_tool_use_paired_and_swept():
    # Codex P2: a _build_simple assistant item (null msg_id) must ALSO have its
    # tool_use indexed (Phase 1) + swept to tool_call (Phase 3), and pair.
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="anull", msg_id=None, req_id=None,
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}", "id": "tn", "preview": "p"}])
    _seed_tool_result(conn, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "RN", "truncated": False, "is_error": False, "tool_use_id": "tn"}])
    items = cq.get_conversation(conn, "s1")["items"]
    assert all(it["kind"] != "tool_result" for it in items)
    null_item = next(it for it in items if it["anchor"]["uuid"] == "anull")
    call = next(b for b in null_item["blocks"] if b["kind"] == "tool_call")
    assert call["result"]["text"] == "RN"
    assert "u1" in null_item["member_uuids"]
    # no bare tool_use block survives the sweep on any assistant item
    for it in items:
        if it["kind"] == "assistant":
            assert all(b["kind"] != "tool_use" for b in it["blocks"])


# ---------------------------------------------------------------------------
# Task 6: search (search_conversations) + FTS/LIKE parity
# ---------------------------------------------------------------------------
def _seed_search_corpus(c):
    _msg(c, session_id="s1", uuid="a1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="the token limit window resets every five hours", model=_MODEL,
         msg_id="m1", req_id="r1", cwd="/home/u/proj")
    _entry(c, source_path="a.jsonl", line_offset=0, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=5)
    _msg(c, session_id="s2", uuid="b1", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-02T00:00:00Z", entry_type="human",
         text="how do I budget my weekly usage", cwd="/home/u/proj")
    # replay of a1 in c.jsonl — search must dedup to ONE hit
    _msg(c, session_id="s1", uuid="a1", source_path="c.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="the token limit window resets every five hours", model=_MODEL,
         msg_id="m1", req_id="r1", cwd="/home/u/proj")


def test_search_fts_dedups_and_costs():
    c = _conn()
    if not db._fts5_available(c):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    _seed_search_corpus(c)
    out = cq.search_conversations(c, "token", limit=50, offset=0)
    assert out["mode"] == "fts"
    assert len(out["hits"]) == 1                       # replay deduped to one
    hit = out["hits"][0]
    assert hit["session_id"] == "s1" and hit["uuid"] == "a1"
    assert "token" in hit["snippet"].lower()
    assert hit["project_label"] == "proj"
    assert hit["cost_usd"] > 0


def test_search_like_fallback_same_hit_set():
    c = _conn()
    _seed_search_corpus(c)
    fts = cq.search_conversations(c, "token", limit=50, offset=0) if db._fts5_available(c) else None
    like = cq.search_conversations(c, "token", limit=50, offset=0, fts_available=False)
    assert like["mode"] == "like"
    assert {(h["session_id"], h["uuid"]) for h in like["hits"]} == {("s1", "a1")}
    if fts is not None:
        assert {(h["session_id"], h["uuid"]) for h in fts["hits"]} == \
               {(h["session_id"], h["uuid"]) for h in like["hits"]}   # parity (modulo rank)


def test_search_dedup_is_load_bearing():
    # Non-vacuity proof (#149): dedup now lives in the SQL window (PARTITION BY
    # session_id, uuid), not a Python _dedup_hits pass. The corpus genuinely
    # contains a replayed physical row (s1/a1 in BOTH a.jsonl and c.jsonl), so a
    # naive query WOULD return 2 rows for that one logical message; the window
    # dedup must collapse them to a single hit with total == 1.
    c = _conn()
    _seed_search_corpus(c)
    # smoking gun: two physical conversation_messages rows match for (s1, a1).
    raw = c.execute(
        "SELECT COUNT(*) FROM conversation_messages "
        "WHERE text LIKE '%token%' AND session_id='s1' AND uuid='a1'"
    ).fetchone()[0]
    assert raw == 2
    out = cq.search_conversations(c, "token", limit=50, offset=0,
                                  fts_available=False)
    assert len(out["hits"]) == 1 and out["total"] == 1        # collapsed in SQL
    assert [(h["session_id"], h["uuid"]) for h in out["hits"]] == [("s1", "a1")]


def test_search_empty_query_is_empty():
    c = _conn()
    _seed_search_corpus(c)
    out = cq.search_conversations(c, "   ", limit=50, offset=0, fts_available=False)
    assert out["hits"] == [] and out["total"] == 0


def test_search_fts_punctuation_does_not_error():
    c = _conn()
    if not db._fts5_available(c):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    _seed_search_corpus(c)
    # raw FTS operators / punctuation in user input must not raise — _fts_query
    # quotes each term as a string literal.
    for q in ('token AND', 'token "OR', 'token*', '"', '(token)'):
        res = cq.search_conversations(c, q, limit=50, offset=0)
        assert res["mode"] == "fts"   # did not raise / fall through to error


# ---------------------------------------------------------------------------
# #149: SQL-bounded search pagination — dedup + page + total all in SQL so the
# Python side never materializes more than one page of hits/snippets.
# ---------------------------------------------------------------------------
def _seed_distinct_hits(c, n, *, term="needle"):
    """n distinct logical hits (distinct session_id/uuid + source_path), each
    matching `term`, with strictly descending-sortable timestamps."""
    for i in range(n):
        _msg(c, session_id=f"s{i}", uuid=f"u{i}", source_path=f"f{i}.jsonl",
             byte_offset=0,
             timestamp_utc=f"2026-06-01T00:{i // 60:02d}:{i % 60:02d}Z",
             entry_type="human", text=f"row {i} has the {term} keyword here",
             cwd="/home/u/proj")


def _search_modes(c):
    """Modes to exercise: always LIKE; FTS too when the build supports it."""
    modes = [False]
    if db._fts5_available(c):
        modes.append(True)
    return modes


def test_search_pagination_disjoint_ordered_stable_total():
    c = _conn()
    n = 5
    _seed_distinct_hits(c, n, term="needle")
    for fa in _search_modes(c):
        p1 = cq.search_conversations(c, "needle", limit=2, offset=0, fts_available=fa)
        p2 = cq.search_conversations(c, "needle", limit=2, offset=2, fts_available=fa)
        p3 = cq.search_conversations(c, "needle", limit=2, offset=4, fts_available=fa)
        # total is the exact post-dedup count and stable across every page.
        assert p1["total"] == p2["total"] == p3["total"] == n, fa
        assert [len(p["hits"]) for p in (p1, p2, p3)] == [2, 2, 1], fa
        keys = [(h["session_id"], h["uuid"])
                for p in (p1, p2, p3) for h in p["hits"]]
        assert len(set(keys)) == n, fa          # pages disjoint + fully covering
        # deferred snippet attach is correct on every page (incl. offset > 0).
        for p in (p1, p2, p3):
            for h in p["hits"]:
                assert "needle" in h["snippet"].lower(), fa
    # LIKE order is deterministic (timestamp DESC) — assert the exact boundary.
    like1 = cq.search_conversations(c, "needle", limit=2, offset=0, fts_available=False)
    like2 = cq.search_conversations(c, "needle", limit=2, offset=2, fts_available=False)
    assert [h["uuid"] for h in like1["hits"]] == ["u4", "u3"]
    assert [h["uuid"] for h in like2["hits"]] == ["u2", "u1"]


def test_search_total_is_post_dedup_logical_count():
    c = _conn()
    _seed_distinct_hits(c, 3, term="alpha")
    # Two extra physical rows replaying the SAME logical message (s0, u0).
    for dup in ("dup1.jsonl", "dup2.jsonl"):
        _msg(c, session_id="s0", uuid="u0", source_path=dup, byte_offset=0,
             timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
             text="row 0 has the alpha keyword here", cwd="/home/u/proj")
    for fa in _search_modes(c):
        out = cq.search_conversations(c, "alpha", limit=50, offset=0, fts_available=fa)
        assert out["total"] == 3, fa                       # logical, not 5 physical
        assert len({(h["session_id"], h["uuid"]) for h in out["hits"]}) == 3, fa


def test_search_snippet_generation_bounded_to_page_like(monkeypatch):
    # Non-vacuity for the bound (LIKE): _manual_snippet must run only for the
    # page, not once per corpus match. Pre-fix it was called for every match.
    c = _conn()
    _seed_distinct_hits(c, 8, term="beta")
    calls = {"n": 0}
    real = cq._manual_snippet
    def counting(text, q, width=80):
        calls["n"] += 1
        return real(text, q, width)
    monkeypatch.setattr(cq, "_manual_snippet", counting)
    out = cq.search_conversations(c, "beta", limit=3, offset=0, fts_available=False)
    assert len(out["hits"]) == 3 and out["total"] == 8
    assert calls["n"] <= 3            # snippet bounded to the page, not all 8


def test_search_snippet_generation_bounded_to_page_fts(monkeypatch):
    # Non-vacuity for the bound (FTS): the snippet batch must be issued for only
    # the page's rowids, not every match.
    c = _conn()
    if not db._fts5_available(c):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    _seed_distinct_hits(c, 8, term="beta")
    seen = {"max": 0}
    real = cq._fts_snippets
    def spy(conn, fts_q, ids, col=0):
        # #177 S6: _fts_snippets may be called more than once per page (col-0
        # prose pass + a tool/thinking preference pass); each call still covers
        # AT MOST one page of rowids. Assert the per-call bound, not the count.
        seen["max"] = max(seen["max"], len(list(ids)))
        return real(conn, fts_q, ids, col=col)
    monkeypatch.setattr(cq, "_fts_snippets", spy)
    out = cq.search_conversations(c, "beta", limit=3, offset=0)
    assert out["mode"] == "fts"
    assert len(out["hits"]) == 3 and out["total"] == 8
    assert seen["max"] <= 3     # each snippet batch bounded to the page


def _seed_tool_only_hits(c, n, *, term="needle"):
    """n distinct logical assistant hits, each matching `term` ONLY in
    search_tool (so all are badged 'tool' with NO prose match — the pre-fix code
    issues one rowid=? prose probe per badged hit)."""
    for i in range(n):
        _msg(c, session_id=f"s{i}", uuid=f"u{i}", source_path=f"f{i}.jsonl",
             byte_offset=0, timestamp_utc=f"2026-06-01T00:00:{i:02d}Z",
             entry_type="assistant", text="", model=_MODEL,
             msg_id=f"m{i}", req_id=f"r{i}",
             search_tool=f"row {i} ran the {term} command", cwd="/home/u/proj")


class _ExecCountingConn:
    """A thin proxy over a sqlite3.Connection that records every `.execute` SQL
    string (sqlite3.Connection.execute is read-only, so it can't be monkeypatched
    in place). Forwards every other attribute to the wrapped connection."""
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "sqls", [])

    def execute(self, sql, *a, **k):
        self.sqls.append(" ".join(str(sql).split()))
        return self._conn.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _count_fts_query_shapes(conn, fn):
    """Run `fn(proxy)` with the connection's `.execute` recorded, then classify
    the FTS5 round-trips that U3 batches:

      - corpus_match: a ``conversation_fts MATCH`` query JOINing
        conversation_messages with NO ``rowid`` restriction — the corpus-wide
        scan (the COUNT, the page CTE, and the pre-fix third
        ``_all_matched_rids_by_group`` scan all have this shape; we count them
        and assert U3 removed exactly the third).
      - single_rowid_match: a ``conversation_fts.rowid = ?`` probe — the pre-fix
        per-hit ``_prefer_snippet_columns`` prose probe (collapsed to one IN-list
        MATCH by U3, so this count must drop to 0).
    """
    proxy = _ExecCountingConn(conn)
    fn(proxy)
    sqls = proxy.sqls
    # A corpus-wide MATCH has NO row-set restriction bound to a PARAMETER —
    # i.e. none of `rowid IN (?…)`, `cm.id IN (?…)`, or `rowid = ?`. (The JOIN's
    # `ON cm.id = conversation_fts.rowid` is structural, NOT a row restriction,
    # so it must not count.) Only the COUNT, the page CTE, and the pre-fix third
    # rids-by-group scan are truly corpus-wide.
    def _bounded(s):
        return ("rowid IN (" in s or ".id IN (" in s
                or "conversation_fts.rowid = ?" in s)
    corpus_match = sum(
        1 for s in sqls
        if "conversation_fts MATCH" in s
        and "conversation_messages" in s
        and not _bounded(s))
    single_rowid_match = sum(
        1 for s in sqls
        if "conversation_fts MATCH" in s and "conversation_fts.rowid = ?" in s)
    return {"corpus_match": corpus_match,
            "single_rowid_match": single_rowid_match, "all": sqls}


def test_search_query_count_batched(monkeypatch):
    """U3 (#217 S1): a search page build must NOT run a third full-corpus FTS
    MATCH to recover the page groups' rows, and must NOT issue a per-hit prose
    probe. Pre-fix: COUNT + page-CTE + the third corpus scan = 3 corpus MATCHes,
    plus one rowid=? prose probe per badged hit. Post-fix: 2 corpus MATCHes
    (COUNT + page CTE) and ONE bounded IN-list prose probe (not per-hit)."""
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _seed_tool_only_hits(c, 6, term="needle")
    stats = _count_fts_query_shapes(
        c, lambda px: cq.search_conversations(px, "needle", kind="all",
                                              limit=200, offset=0))
    # The third corpus-wide MATCH (the rids-by-group scan) is gone: only the
    # COUNT and the page CTE remain corpus-wide.
    assert stats["corpus_match"] <= 2, stats["all"]
    # The per-hit prose probe loop collapsed: at most ONE single-rowid MATCH
    # survives (and U3 replaces it with an IN-list, so ideally zero).
    assert stats["single_rowid_match"] == 0, stats["all"]


def test_search_query_count_batched_byte_identical_results():
    """U3: the batched path returns byte-identical hits/badges to a baseline the
    existing badge/snippet units pin — re-asserted here for the tool-only corpus
    the query-count test exercises (all badged 'tool', drawn from the tool
    snippet column)."""
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _seed_tool_only_hits(c, 6, term="needle")
    out = cq.search_conversations(c, "needle", kind="all", limit=200, offset=0)
    assert out["total"] == 6
    assert {h["uuid"] for h in out["hits"]} == {f"u{i}" for i in range(6)}
    for h in out["hits"]:
        assert h["match_kinds"] == ["tool"]           # badged off search_tool
        assert "needle" in h["snippet"].lower()       # snippet from the tool col


# ---------------------------------------------------------------------------
# #155: subagent_key derivation + reader passthrough
# ---------------------------------------------------------------------------
def test_subagent_key_derivation():
    assert cq._subagent_key("/p/agent-a34de77e.jsonl") == "a34de77e"
    assert cq._subagent_key("/p/agent-acompact-a572884f.jsonl") == "acompact-a572884f"
    assert cq._subagent_key("/p/16b00b55-2e61.jsonl") is None      # main session file
    assert cq._subagent_key("") is None
    assert cq._subagent_key(None) is None


def test_get_conversation_exposes_subagent_key_and_parent_uuid_no_source_path():
    c = _conn()
    # main human (main file -> subagent_key None), then a sidechain human from an
    # agent file (subagent_key derived), parented to the main item (cross-file).
    _msg(c, session_id="s1", uuid="h1", parent_uuid=None,
         source_path="/p/s1.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="main")
    _msg(c, session_id="s1", uuid="g1", parent_uuid="h1",
         source_path="/p/agent-aaaa1111.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="human",
         text="subagent task", is_sidechain=1)
    out = cq.get_conversation(c, "s1", after=None, limit=500)
    items = out["items"]
    by = {it["anchor"]["uuid"]: it for it in items}
    # main item: subagent_key None, parent_uuid None
    assert by["h1"]["subagent_key"] is None
    assert by["h1"]["parent_uuid"] is None
    # sidechain item: derived key + raw parent_uuid passthrough
    assert by["g1"]["subagent_key"] == "aaaa1111"
    assert by["g1"]["parent_uuid"] == "h1"
    # NEGATIVE privacy guarantee: no raw source_path leaks on ANY item
    for it in items:
        assert "source_path" not in it
        assert "subagent_key" in it and "parent_uuid" in it


def test_get_conversation_turn_parent_uuid_is_seed_sourced_not_prose_anchor():
    # Codex P1: a multi-fragment sidechain assistant turn whose SEED fragment
    # parents to a MAIN uuid (the real entry point) and whose prose fragment
    # parents intra-turn. The emitted turn item must carry the SEED parent_uuid,
    # so cross-file nesting keys on the entry point — NOT the intra-turn link.
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", parent_uuid=None,
         source_path="/p/s1.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="main")
    # seed fragment (thinking-only): parent = main h1 (cross-file entry point)
    _msg(c, session_id="s1", uuid="g1a", parent_uuid="h1",
         source_path="/p/agent-bbbb2222.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant", text="",
         blocks_json=_json.dumps([{"kind": "thinking", "text": "..."}]),
         model=_MODEL, msg_id="mb", req_id="rb", is_sidechain=1)
    # prose fragment: parent = g1a (intra-turn)
    _msg(c, session_id="s1", uuid="g1b", parent_uuid="g1a",
         source_path="/p/agent-bbbb2222.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:02Z", entry_type="assistant",
         text="subagent answer",
         blocks_json=_json.dumps([{"kind": "text", "text": "subagent answer"}]),
         model=_MODEL, msg_id="mb", req_id="rb", is_sidechain=1)
    out = cq.get_conversation(c, "s1", after=None, limit=500)
    turn = next(it for it in out["items"] if it["kind"] == "assistant")
    assert set(turn["member_uuids"]) == {"g1a", "g1b"}      # coalesced turn
    assert turn["anchor"]["uuid"] == "g1b"                   # prose-bearing anchor
    assert turn["subagent_key"] == "bbbb2222"
    assert turn["parent_uuid"] == "h1"                       # SEED parent, not "g1a"


# --- title derivation: _is_system_marker parity (#165 Q2) -----------------
# Parity scoped to ASCII whitespace + the marker vectors, mirroring
# dashboard/web/src/conversations/systemMarkers.test.ts. Exotic Unicode/control
# whitespace (BOM, FS-US) is an explicit non-goal (spec §4.1 caveat).
def test_is_system_marker_parity():
    M = cq._is_system_marker
    # positive: each single wrapper
    assert M("<command-name>clear</command-name>")
    assert M("<local-command-caveat>do not respond</local-command-caveat>")
    # #186: the two new command-family tags (slash-command stdout/stderr echo)
    assert M("<local-command-stdout>Set model to Fable 5</local-command-stdout>")
    assert M("<local-command-stderr>boom</local-command-stderr>")
    # positive: concatenated wrappers (the /clear shape)
    assert M("<command-name>clear</command-name>"
             "<command-message>clear</command-message>"
             "<command-args></command-args>")
    # positive: ASCII-whitespace wrapped + between
    assert M("  <command-name>clear</command-name>\n  ")
    assert M("<command-name>a</command-name>\t<command-args>b</command-args>")
    # negative: a sentence merely QUOTING a tag is not a marker
    assert not M("See <command-name>clear</command-name> for details.")
    # negative: a marker embedded in prose
    assert not M("prefix <command-name>clear</command-name>")
    # negative: empty / plain prose
    assert not M("")
    assert not M("how does the reset work")
    # ReDoS guard: a valid prefix then trailing prose terminates (no hang)
    assert not M("<command-name>x</command-name>" + "y" * 5000)


# --- title derivation: _session_titles_map (#165 §4.1) --------------------
def test_session_titles_basic_and_marker_skip():
    c = _conn()
    # s1: clean first human → title is its first non-blank line
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="how does the reset work\nsecond line", cwd="/home/u/proj")
    # s2: marker-first → skip to the SECOND human
    _msg(c, session_id="s2", uuid="h2a", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-02T00:00:00Z", entry_type="human",
         text="<command-name>clear</command-name>", cwd="/home/u/proj")
    _msg(c, session_id="s2", uuid="h2b", source_path="b.jsonl", byte_offset=1,
         timestamp_utc="2026-06-02T00:00:01Z", entry_type="human",
         text="the real prompt", cwd="/home/u/proj")
    # s3: a SIDECHAIN human (subagent Task line) must be ignored
    _msg(c, session_id="s3", uuid="h3", source_path="agent-x.jsonl", byte_offset=0,
         timestamp_utc="2026-06-03T00:00:00Z", entry_type="human",
         text="subagent task prompt", cwd="/home/u/proj", is_sidechain=1)
    titles = cq._session_titles_map(c, ["s1", "s2", "s3"])
    assert titles["s1"] == "how does the reset work"   # first non-blank LINE only
    assert titles["s2"] == "the real prompt"            # marker skipped
    assert "s3" not in titles                           # sidechain-only → no title

def test_session_titles_skips_unknown_command_family_tag():
    # #186 belt-and-suspenders: an UNKNOWN command-family tag (not in
    # _MARKER_TAGS) — e.g. a future `<local-command-future>` — must still be
    # skipped for the title via the broader, title-only command-family predicate,
    # falling through to the next real prompt. Strict `_is_system_marker` does
    # NOT know the tag, so this proves the broader predicate is wired in.
    c = _conn()
    _msg(c, session_id="s", uuid="h0", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="<local-command-future>whatever</local-command-future>",
         cwd="/home/u/proj")
    _msg(c, session_id="s", uuid="h1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="human",
         text="the real prompt", cwd="/home/u/proj")
    titles = cq._session_titles_map(c, ["s"])
    assert titles["s"] == "the real prompt"


def test_looks_like_command_plumbing_predicate():
    # The title-only liberal predicate: prefix shape (command-* / local-command-*),
    # NOT the strict known-tag list. Whole-string only; mid-sentence quoting and
    # plain prose are NOT plumbing.
    P = cq._looks_like_command_plumbing
    assert P("<command-name>clear</command-name>")                 # known tag
    assert P("<local-command-future>x</local-command-future>")     # unknown family tag
    assert P("<command-foo>a</command-foo><command-bar>b</command-bar>")  # concatenated
    assert P("  <local-command-stdout>x</local-command-stdout>\n")  # whitespace-wrapped
    assert not P("see <command-name>clear</command-name> here")    # mid-sentence
    assert not P("the real prompt")                                # plain prose
    assert not P("")                                               # empty
    assert not P("<notcommand>x</notcommand>")                     # non-command tag


def test_session_titles_truncation_with_ellipsis():
    c = _conn()
    long = "x" * 200
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text=long)
    t = cq._session_titles_map(c, ["s1"])["s1"]
    assert len(t) == 121 and t.endswith("…") and t[:120] == "x" * 120

def test_session_titles_empty_for_unknown():
    assert cq._session_titles_map(_conn(), []) == {}


def test_list_conversations_includes_title_with_fallback():
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="design the rail title", cwd="/home/u/proj")
    # s2: NO human (assistant-only) → title falls back to project_label
    _msg(c, session_id="s2", uuid="a2", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-02T00:00:00Z", entry_type="assistant",
         text="hi", model=_MODEL, msg_id="m2", req_id="r2", cwd="/home/u/other")
    rows = _list_conversations(c)["conversations"]
    s1 = next(r for r in rows if r["session_id"] == "s1")
    s2 = next(r for r in rows if r["session_id"] == "s2")
    assert s1["title"] == "design the rail title"
    assert s2["title"] == "other"        # fallback → project_label basename


def test_search_hits_include_title():
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="how does the reset work", cwd="/home/u/proj")
    _msg(c, session_id="s1", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="the reset uses a sliding token window", model=_MODEL,
         msg_id="m1", req_id="r1", cwd="/home/u/proj")
    out = cq.search_conversations(c, "sliding token", fts_available=False)
    assert out["hits"], "expected at least one hit"
    assert out["hits"][0]["title"] == "how does the reset work"


def test_search_hits_include_title_fts():
    # Sibling of test_search_hits_include_title for the FTS branch: pins
    # _attach_titles directly on the fts_available=True path (otherwise covered
    # only transitively by the golden harness). Skips cleanly when the sqlite
    # build lacks FTS5.
    c = _conn()
    if not db._fts5_available(c):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="how does the reset work", cwd="/home/u/proj")
    _msg(c, session_id="s1", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="the reset uses a sliding token window", model=_MODEL,
         msg_id="m1", req_id="r1", cwd="/home/u/proj")
    out = cq.search_conversations(c, "sliding token")
    assert out["mode"] == "fts"
    assert out["hits"], "expected at least one hit"
    assert out["hits"][0]["title"] == "how does the reset work"


# ---------------------------------------------------------------------------
# #166: subagent-kind correlation scan in get_conversation. Isolated kernel
# units for the edge branches of the spawn<->result join that builds the
# top-level `subagent_meta` map and pop-strips the parser-only block keys.
# The blocks_json seeded here carries the POST-PARSER shape: `subagent_type`
# on the tool_use block; `agent_id` + `subagent_meta` on the tool_result block
# (i.e. what _lib_conversation.py would have emitted).
# ---------------------------------------------------------------------------
def test_subagent_meta_spawn_without_result_is_title_only():
    # A spawn tool_use with a null `id` (so the spawn never enters spawn_kind):
    # produces NO subagent_meta entry — the card degrades to title-only. Also
    # covers the "result absent" arm: even a kept spawn would title-only when
    # agent_link has no matching tool_use_id.
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Task", "input_summary": "{}",
                 "id": None, "preview": "audit", "subagent_type": "Explore"}])
    out = cq.get_conversation(conn, "s1")
    assert out["subagent_meta"] == {}        # null id -> not keyed -> no entry


def test_subagent_meta_empty_meta_kind_only():
    # Spawn + result where the toolUseResult carried ONLY agentId (no metric
    # fields). The `meta or {}` path keys the entry; the entry is just
    # {kind: ...} with NO total_tokens/total_duration_ms/etc.
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Task", "input_summary": "{}",
                 "id": "t1", "preview": "audit", "subagent_type": "Explore"}])
    _seed_tool_result(conn, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "done", "truncated": False,
                 "is_error": False, "tool_use_id": "t1",
                 "agent_id": "aaaa1111", "subagent_meta": {}}])
    out = cq.get_conversation(conn, "s1")
    # §4 1b adds parent linkage to EVERY linked spawn (additive): the holding
    # item is the main turn a1 (subagent_key=None, anchor uuid a1), the spawn id
    # is t1. No aaaa1111 thread bucket exists here, so 1c derives nothing.
    assert out["subagent_meta"] == {"aaaa1111": {
        "kind": "Explore", "parent_subagent_key": None,
        "spawn_uuid": "a1", "spawn_tool_use_id": "t1"}}
    entry = out["subagent_meta"]["aaaa1111"]
    assert "total_tokens" not in entry and "total_duration_ms" not in entry
    assert "total_tool_use_count" not in entry and "status" not in entry
    assert "totals_derived" not in entry


def test_subagent_meta_happy_path_and_block_keys_stripped():
    # Happy path: spawn + result with full meta. subagent_meta[<agent_id>]
    # carries the kind + every present metric field. The returned items'
    # tool_call/tool_result blocks must NOT leak the parser-only keys
    # (subagent_type / agent_id / subagent_meta) — the pop-strip.
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Task", "input_summary": "{}",
                 "id": "t1", "preview": "audit", "subagent_type": "Explore"}])
    _seed_tool_result(conn, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "done", "truncated": False,
                 "is_error": False, "tool_use_id": "t1",
                 "agent_id": "aaaa1111",
                 "subagent_meta": {"total_tokens": 23285,
                                   "total_duration_ms": 10668,
                                   "total_tool_use_count": 1,
                                   "status": "completed"}}])
    out = cq.get_conversation(conn, "s1")
    # §4 1b adds the parent-linkage fields (additive) to the authoritative-totals
    # entry; status/totals stay authoritative (no totals_derived).
    assert out["subagent_meta"] == {"aaaa1111": {
        "kind": "Explore", "total_tokens": 23285, "total_duration_ms": 10668,
        "total_tool_use_count": 1, "status": "completed",
        "parent_subagent_key": None, "spawn_uuid": "a1",
        "spawn_tool_use_id": "t1"}}
    assert "totals_derived" not in out["subagent_meta"]["aaaa1111"]
    # the spawn folded its result into a tool_call; NO parser-only keys leak on
    # any block of any item.
    items = out["items"]
    turn = next(it for it in items if it["kind"] == "assistant")
    call = next(b for b in turn["blocks"] if b["kind"] == "tool_call")
    assert call["result"]["text"] == "done"      # result folded in
    for it in items:
        for b in it["blocks"]:
            assert "subagent_type" not in b
            assert "agent_id" not in b
            assert "subagent_meta" not in b


# ---- injected-meta classification (skill / command / context) ------------

def _meta_blocks(text):
    return _json.dumps([{"kind": "text", "text": text}])


def test_get_conversation_meta_skill_row_classified_with_name():
    c = _conn()
    body = "Base directory for this skill: /x/skills/brainstorming\n\n# Brainstorming Ideas"
    # a true meta row: entry_type='meta', text='' (parser), body in blocks
    _msg(c, session_id="s", uuid="m1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="t", entry_type="meta", text="", blocks_json=_meta_blocks(body))
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "meta"
    assert it["meta_kind"] == "skill"
    assert it["skill_name"] == "brainstorming"
    assert "Brainstorming Ideas" in it["text"]            # render body populated from blocks


def test_get_conversation_meta_command_and_context():
    c = _conn()
    _msg(c, session_id="s", uuid="m1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="t1", entry_type="meta", text="",
         blocks_json=_meta_blocks("<command-name>clear</command-name>"))
    _msg(c, session_id="s", uuid="m2", source_path="/p/s.jsonl", byte_offset=1,
         timestamp_utc="t2", entry_type="meta", text="",
         blocks_json=_meta_blocks("## Git Context\n- branch: main"))
    items = cq.get_conversation(c, "s")["items"]
    by_uuid = {it["anchor"]["uuid"]: it for it in items}
    assert by_uuid["m1"]["meta_kind"] == "command" and by_uuid["m1"]["skill_name"] is None
    assert by_uuid["m2"]["meta_kind"] == "context" and by_uuid["m2"]["skill_name"] is None


# #186: a PRE-FIX human row whose body is a command-marker echo (seeded directly
# as entry_type='human', simulating a row ingested before the parser-level
# classification shipped) must be reclassified read-time to meta/command so it
# renders as a System-marker pill, drops from the rail, and never becomes the
# title — the read-time fallback for already-indexed rows.
def test_pre_fix_human_command_marker_reclassified_read_time():
    c = _conn()
    body = "<local-command-stdout>Set model to Fable 5</local-command-stdout>"
    _msg(c, session_id="s", uuid="h0", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text=body,
         blocks_json=_meta_blocks(body))
    _msg(c, session_id="s", uuid="h1", source_path="/p/s.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="human",
         text="the real first prompt", blocks_json=_meta_blocks("the real first prompt"))
    items = cq.get_conversation(c, "s")["items"]
    by_uuid = {it["anchor"]["uuid"]: it for it in items}
    # the pre-fix human command echo folds to meta/command read-time
    assert by_uuid["h0"]["kind"] == "meta"
    assert by_uuid["h0"]["meta_kind"] == "command"
    assert by_uuid["h0"]["skill_name"] is None
    # the next REAL human prompt is untouched and wins the title
    assert by_uuid["h1"]["kind"] == "human"
    title = cq._session_titles_map(c, ["s"])["s"]
    assert title == "the real first prompt"


# #188 Task A3: a slash-command invocation carrying a real prompt in
# <command-args> presents at READ TIME as a "You" turn (kind='human',
# text=args), with a `command_name` field derived from the BLOCKS (not the
# scalar text, which after migration holds args). This fixes display + outline +
# reader-title for EXISTING cached META rows with NO rebuild. /clear (empty args)
# stays a hidden command meta.
def test_read_time_promotes_legacy_meta_command_with_args():
    c = _conn()
    raw = ("<command-message>review:review</command-message>"
           "<command-name>/review</command-name>"
           "<command-args>Review feat/x vs main.</command-args>")
    # legacy shape: entry_type='meta', text='' (parser), raw wrapper in blocks
    _msg(c, session_id="s", uuid="u1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="meta", text="",
         blocks_json=_meta_blocks(raw))
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "human"
    assert it["text"] == "Review feat/x vs main."
    assert it["command_name"] == "/review"


def test_read_time_post_migration_human_command_keeps_badge():
    # idempotent with ingest/migration: a row already promoted to entry_type=
    # 'human' with text=args still derives the command_name badge from its blocks
    c = _conn()
    raw = ("<command-name>/frontend-design</command-name>"
           "<command-args>do the thing</command-args>")
    _msg(c, session_id="s", uuid="u1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="do the thing", blocks_json=_meta_blocks(raw))
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "human"
    assert it["text"] == "do the thing"
    assert it["command_name"] == "/frontend-design"


def test_read_time_empty_args_command_stays_meta():
    c = _conn()
    _msg(c, session_id="s", uuid="u1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="meta", text="",
         blocks_json=_meta_blocks("<command-name>/clear</command-name>"
                                  "<command-args></command-args>"))
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "meta" and it["meta_kind"] == "command"
    # a non-promoted item carries no command_name (the badge is promotion-only)
    assert "command_name" not in it or it.get("command_name") is None


def test_read_time_plain_human_has_no_command_name():
    c = _conn()
    _msg(c, session_id="s", uuid="u1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="a normal prompt", blocks_json=_meta_blocks("a normal prompt"))
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "human"
    assert it.get("command_name") is None


def test_outline_includes_promoted_command_as_human():
    c = _conn()
    raw = ("<command-name>/frontend-design</command-name>"
           "<command-args>Audit the reader UI.</command-args>")
    _msg(c, session_id="s", uuid="u1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="meta", text="",
         blocks_json=_meta_blocks(raw))
    outline = cq.get_conversation_outline(c, "s")
    t0 = outline["turns"][0]
    assert t0["kind"] == "human"
    assert t0["label"] == "Audit the reader UI."


def test_reader_title_from_promoted_command_read_time():
    # the in-conversation reader title (read-time) picks the promoted args even
    # for a legacy META row, with no rebuild (the LIST title needs migration 011
    # to flip entry_type — that split is intentional and covered separately).
    c = _conn()
    raw = ("<command-name>/frontend-design</command-name>"
           "<command-args>Audit the reader UI and file issues.</command-args>")
    _msg(c, session_id="s", uuid="u1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="meta", text="",
         blocks_json=_meta_blocks(raw))
    items = cq.get_conversation(c, "s")["items"]
    # the assembled first item presents as human with text=args — the client
    # deriveReaderTitle picks it (it's no longer a marker).
    assert items[0]["kind"] == "human"
    assert items[0]["text"] == "Audit the reader UI and file issues."


# #186 Task 1F: read-time ANSI stripping for rows already in the DB with raw
# `\x1b` (pre-fix, ingested before the ingest-layer strip). No ANSI survives into
# get_conversation prose / command <pre> body, get_conversation_outline labels,
# or _session_titles_map titles — but a Bash tool_result's ANSI is PRESERVED
# (the documented BashCard/AnsiText scope boundary).
def test_read_time_strips_ansi_but_preserves_bash_tool_result():
    c = _conn()
    # a PRE-FIX dirty human prompt: raw ESC[1m in the stored text + blocks
    dirty_title = "the \x1b[1mreal\x1b[22m first prompt"
    _msg(c, session_id="s", uuid="h1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text=dirty_title, blocks_json=_meta_blocks(dirty_title), cwd="/home/u/proj")
    # an assistant turn carrying dirty prose + a Bash tool_call whose folded
    # tool_result text carries SGR (must be PRESERVED for AnsiText).
    _msg(c, session_id="s", uuid="a1", source_path="/p/s.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="answer with \x1b[32mgreen\x1b[0m prose",
         blocks_json=_json.dumps([
             {"kind": "text", "text": "answer with \x1b[32mgreen\x1b[0m prose"},
             {"kind": "thinking", "text": "ponder \x1b[1mhard\x1b[22m"},
             {"kind": "tool_use", "name": "Bash", "id": "tu1",
              "input_summary": '{"command":"ls"}', "preview": "ls"},
         ]),
         model=_MODEL, msg_id="m1", req_id="r1", cwd="/home/u/proj")
    _msg(c, session_id="s", uuid="tr1", source_path="/p/s.jsonl", byte_offset=2,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="tool_result", text="",
         blocks_json=_json.dumps([
             {"kind": "tool_result", "text": "out \x1b[31mERROR\x1b[0m line",
              "truncated": False, "is_error": False, "tool_use_id": "tu1"}]),
         cwd="/home/u/proj")
    _entry(c, source_path="/p/s.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=100, out=50)

    # title: no ANSI
    title = cq._session_titles_map(c, ["s"])["s"]
    assert "\x1b" not in title and "real" in title

    # outline labels: no ANSI
    outline = cq.get_conversation_outline(c, "s")
    for t in outline["turns"]:
        assert "\x1b" not in (t.get("label") or "")
        for ln in (t.get("thinking") or []):
            assert "\x1b" not in ln

    # reader page: prose / thinking stripped, Bash tool_result PRESERVED
    items = cq.get_conversation(c, "s")["items"]
    for it in items:
        assert "\x1b" not in (it.get("text") or "")
        for b in it["blocks"]:
            if b.get("kind") in ("text", "thinking"):
                assert "\x1b" not in (b.get("text") or "")
    # the Bash tool_result block text still carries its SGR (AnsiText boundary)
    asst = next(it for it in items if it["kind"] == "assistant")
    tool_call = next(b for b in asst["blocks"] if b.get("kind") == "tool_call")
    assert "\x1b[31m" in tool_call["result"]["text"], "Bash tool_result ANSI preserved"


def test_pre_fix_human_command_marker_with_media_stays_human():
    # the read-time all-text guard (Codex P1b): a human row mixing a marker text
    # block with a non-text (image) block must NOT reclassify — its user-authored
    # content is preserved.
    c = _conn()
    blocks = _json.dumps([
        {"kind": "text", "text": "<local-command-stdout>x</local-command-stdout>"},
        {"kind": "image", "media_type": "image/png", "bytes": 2, "index": 0},
    ])
    _msg(c, session_id="s", uuid="h0", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="<local-command-stdout>x</local-command-stdout>", blocks_json=blocks)
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "human"
    assert "meta_kind" not in it


def _set_reingest_pending(c):
    c.execute("INSERT INTO cache_meta(key, value) VALUES('conversation_reingest_pending','1') "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    c.commit()


def test_get_conversation_human_skill_preamble_read_time_fallback_only_while_pending():
    # A NOT-yet-reingested row: still entry_type='human' with the body in `text`.
    # The read-time fallback renders it as skill-meta ONLY while the 005 reingest
    # flag is pending (Codex code-review P1).
    c = _conn()
    _set_reingest_pending(c)
    body = "Base directory for this skill: /a/b/systematic-debugging\n\nbody"
    _msg(c, session_id="s", uuid="h1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="t", entry_type="human", text=body, blocks_json=_meta_blocks(body))
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "meta"
    assert it["meta_kind"] == "skill" and it["skill_name"] == "systematic-debugging"


def test_human_skill_preamble_stays_human_after_reingest_consumed():
    # Flag NOT pending (post-reingest steady state): a genuine human prompt that
    # merely STARTS WITH the skill preamble must stay a "You" turn, never a hidden
    # collapsed skill pill (the Codex code-review P1 false-positive guard).
    c = _conn()
    body = "Base directory for this skill: is what I'd name the function — thoughts?"
    _msg(c, session_id="s", uuid="h1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="t", entry_type="human", text=body, blocks_json=_meta_blocks(body))
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "human"
    assert "meta_kind" not in it
    # ...but a TRUE meta row still classifies even with the flag cleared.
    c2 = _conn()
    skill = "Base directory for this skill: /x/skills/brainstorming\n\nbody"
    _msg(c2, session_id="s", uuid="m1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="t", entry_type="meta", text="", blocks_json=_meta_blocks(skill))
    it2 = cq.get_conversation(c2, "s")["items"][0]
    assert it2["kind"] == "meta" and it2["meta_kind"] == "skill"


def test_get_conversation_human_non_skill_stays_human():
    c = _conn()
    _msg(c, session_id="s", uuid="h1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="t", entry_type="human", text="a real prompt",
         blocks_json=_meta_blocks("a real prompt"))
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "human"
    assert "meta_kind" not in it


def test_meta_row_excluded_from_session_title():
    # title derivation must skip a meta skill body even if it sorts first
    c = _conn()
    skill = "Base directory for this skill: /x/skills/brainstorming\n\nbody"
    _msg(c, session_id="s", uuid="m1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="t1", entry_type="meta", text="", blocks_json=_meta_blocks(skill))
    _msg(c, session_id="s", uuid="h1", source_path="/p/s.jsonl", byte_offset=1,
         timestamp_utc="t2", entry_type="human", text="Resolve the cache bug",
         blocks_json=_meta_blocks("Resolve the cache bug"))
    title = _list_conversations(c)["conversations"][0]["title"]
    assert title == "Resolve the cache bug"


def test_pre_reingest_human_skill_body_skipped_as_title_while_pending():
    # A not-yet-reingested skill body (still entry_type='human') that LEADS the
    # transcript must not become the rail title while the 005 flag is pending
    # (Codex code-review P2); derivation falls through to the real prompt.
    c = _conn()
    _set_reingest_pending(c)
    skill = "Base directory for this skill: /x/skills/using-superpowers\n\nbody"
    _msg(c, session_id="s", uuid="h0", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="t1", entry_type="human", text=skill, blocks_json=_meta_blocks(skill))
    _msg(c, session_id="s", uuid="h1", source_path="/p/s.jsonl", byte_offset=1,
         timestamp_utc="t2", entry_type="human", text="Resolve the cache bug",
         blocks_json=_meta_blocks("Resolve the cache bug"))
    title = _list_conversations(c)["conversations"][0]["title"]
    assert title == "Resolve the cache bug"


# ---- skill-content nesting: fold a skill body into its Skill tool chip -------

_SKILL_BODY = "Base directory for this skill: /x/skills/brainstorming\n\n# Brainstorming"


def _seed_skill_triple(conn, *, sid="s1", tool_id="toolu_S",
                       source_tool_use_id="toolu_S", body=_SKILL_BODY):
    """A real Skill triple: an assistant turn with a Skill tool_use, its
    "Launching skill" tool_result, and the isMeta skill body row linking back
    via source_tool_use_id. Returns the body row's uuid."""
    _seed_assistant(conn, sid=sid, uuid="a1", msg_id="m1", req_id="r1",
        ts="2026-06-01T00:00:01Z",
        blocks=[{"kind": "tool_use", "name": "Skill",
                 "input_summary": '{"skill":"brainstorming"}',
                 "id": tool_id, "preview": "brainstorming"}])
    _seed_tool_result(conn, sid=sid, uuid="u-res", ts="2026-06-01T00:00:02Z",
        blocks=[{"kind": "tool_result", "text": "Launching skill: brainstorming",
                 "truncated": False, "is_error": False, "tool_use_id": tool_id}])
    _msg(conn, session_id=sid, uuid="u-body", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-01T00:00:03Z", entry_type="meta", text="",
         blocks_json=_meta_blocks(body), source_tool_use_id=source_tool_use_id)
    return "u-body"


def _find_skill_chip(items, tool_use_id):
    for it in items:
        if it["kind"] != "assistant":
            continue
        for b in it["blocks"]:
            if b.get("kind") == "tool_call" and b.get("tool_use_id") == tool_use_id:
                return it, b
    return None, None


def test_skill_body_folds_into_matching_tool_call():
    conn = _conn()
    body_uuid = _seed_skill_triple(conn)
    out = cq.get_conversation(conn, "s1")
    items = out["items"]
    # the standalone skill meta item is GONE (folded into the chip)
    assert not any(it["kind"] == "meta" and it.get("meta_kind") == "skill"
                   for it in items)
    # the Skill tool_call carries the body + skill name, "Launching" result cleared
    owner, chip = _find_skill_chip(items, "toolu_S")
    assert chip is not None
    assert chip["skill_body"].startswith("Base directory for this skill:")
    assert chip["skill_name"] == "brainstorming"
    assert chip["result"] is None
    # the body uuid joined the owner turn's member_uuids (jump anchor)
    assert body_uuid in owner["member_uuids"]
    # the internal threading field never leaks into the public item JSON
    assert all("_source_tool_use_id" not in it for it in items)


def test_unlinked_skill_body_stays_standalone():
    # A SessionStart-injected skill body: a true meta skill row with NO
    # source_tool_use_id -> no tooluse_index hit -> standalone pill (permanent).
    conn = _conn()
    _msg(conn, session_id="s1", uuid="m1", source_path="/p/s.jsonl", byte_offset=0,
         timestamp_utc="t", entry_type="meta", text="",
         blocks_json=_meta_blocks(_SKILL_BODY), source_tool_use_id=None)
    items = cq.get_conversation(conn, "s1")["items"]
    assert any(it["kind"] == "meta" and it.get("meta_kind") == "skill"
               for it in items)
    assert all("_source_tool_use_id" not in it for it in items)


def test_skill_body_no_matching_tool_use_stays_standalone():
    # source_tool_use_id present but resolves to no tool_use in the session
    # (the id-collision / orphan posture) -> standalone pill, never crash.
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        ts="2026-06-01T00:00:01Z",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}",
                 "id": "toolu_OTHER", "preview": "/x.py"}])
    _msg(conn, session_id="s1", uuid="u-body", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-01T00:00:03Z", entry_type="meta", text="",
         blocks_json=_meta_blocks(_SKILL_BODY), source_tool_use_id="toolu_MISSING")
    items = cq.get_conversation(conn, "s1")["items"]
    assert any(it["kind"] == "meta" and it.get("meta_kind") == "skill"
               for it in items)


def test_pre_reingest_null_column_skill_body_stays_standalone():
    # Pre-006 steady state: a paired skill body still has NULL source_tool_use_id
    # (the column add landed but the reingest hasn't run). It must fall back to
    # the standalone pill purely on the NULL column (no flag gates this).
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        ts="2026-06-01T00:00:01Z",
        blocks=[{"kind": "tool_use", "name": "Skill",
                 "input_summary": '{"skill":"brainstorming"}',
                 "id": "toolu_S", "preview": "brainstorming"}])
    _msg(conn, session_id="s1", uuid="u-body", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-01T00:00:03Z", entry_type="meta", text="",
         blocks_json=_meta_blocks(_SKILL_BODY), source_tool_use_id=None)
    items = cq.get_conversation(conn, "s1")["items"]
    assert any(it["kind"] == "meta" and it.get("meta_kind") == "skill"
               for it in items)
    # and the Skill chip keeps its own (request-only) shape, no skill_body
    _owner, chip = _find_skill_chip(items, "toolu_S")
    assert chip is not None
    assert "skill_body" not in chip


# ---------------------------------------------------------------------------
# #177 Session 1: enriched data contract surfaced in the reader payload.
# Per-turn token usage (a NEW _turn_usage_map, NOT touching _turn_cost_map),
# stop_reason / attribution (tail-appended columns), structured tool input
# pass-through, and result.full_length. All ADDITIVE — no existing key changes.
# ---------------------------------------------------------------------------

# ---- 2.1: _turn_usage_map + token stamping --------------------------------
def test_turn_item_carries_tokens_from_session_entries():
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="q")
    _msg(c, session_id="s1", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="answer", model=_MODEL, msg_id="m1", req_id="r1")
    # the SAME deduped session_entries row cost is computed from
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=100, out=20, cc=5, cr=3)
    out = cq.get_conversation(c, "s1")
    turn = [it for it in out["items"]
            if it["kind"] == "assistant" and "tokens" in it][0]
    assert turn["tokens"] == {"input": 100, "output": 20,
                              "cache_creation": 5, "cache_read": 3}


def test_turn_without_session_entries_omits_tokens():
    # An assistant turn whose (msg_id, req_id) has NO session_entries row: no
    # tokens key (absent, not zero-filled), and cost_usd stays 0.0.
    c = _conn()
    _msg(c, session_id="s1", uuid="a1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="answer", model=_MODEL, msg_id="m1", req_id="r1")
    out = cq.get_conversation(c, "s1")
    turn = [it for it in out["items"] if it["kind"] == "assistant"][0]
    assert "tokens" not in turn
    assert turn["cost_usd"] == 0.0


def test_null_msg_id_assistant_never_carries_tokens():
    # A _build_simple assistant (null msg_id) has no turn key -> no usage join ->
    # never a tokens key (the usage stamp lives only in the turn-cost loop).
    c = _conn()
    _msg(c, session_id="s1", uuid="anull", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="no turn key", model=_MODEL, msg_id=None, req_id=None)
    out = cq.get_conversation(c, "s1")
    turn = [it for it in out["items"] if it["kind"] == "assistant"][0]
    assert "tokens" not in turn
    assert turn["cost_usd"] == 0.0


_RAW_COST_OVERRIDE = 0.4242


def test_raw_cost_override_row_still_surfaces_tokens_and_raw_cost():
    # The Codex P1 "same source row, NOT same arithmetic" guard: a
    # session_entries row carrying cost_usd_raw AND token columns. tokens come
    # from the row; cost_usd equals the raw override (token-derived math is
    # bypassed by the helper) — so the two are deliberately NOT equal, and this
    # test asserts NEITHER a token-derived cost NOR token==cost.
    c = _conn()
    _seed_assistant_with_cost(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "text", "text": "answer"}], model=_MODEL,
        inp=100, out=20, cc=5, cr=3, cost_usd_raw=_RAW_COST_OVERRIDE)
    out = cq.get_conversation(c, "s1")
    turn = [it for it in out["items"]
            if it["kind"] == "assistant" and "tokens" in it][0]
    assert turn["tokens"]["input"] == 100 and turn["tokens"]["output"] == 20
    assert turn["cost_usd"] == round(_RAW_COST_OVERRIDE, 6)   # the raw override, not f(tokens)


def test_search_hit_still_returns_numeric_cost_usd():
    # Sibling-map isolation (#177): _turn_usage_map is separate from
    # _turn_cost_map, which the search path's _attach_costs still consumes for a
    # numeric cost. A search hit must still carry a numeric cost_usd.
    c = _conn()
    _msg(c, session_id="s1", uuid="a1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="the token limit resets hourly", model=_MODEL,
         msg_id="m1", req_id="r1", cwd="/home/u/proj")
    _entry(c, source_path="a.jsonl", line_offset=0, model=_MODEL,
           msg_id="m1", req_id="r1", inp=1000, out=500)
    out = cq.search_conversations(c, "token", fts_available=False)
    assert out["hits"], "expected at least one hit"
    hit = out["hits"][0]
    assert isinstance(hit["cost_usd"], (int, float))
    assert hit["cost_usd"] > 0


# ---- 2.2: stop_reason / attribution threading (tail-appended) -------------
def test_turn_surfaces_stop_reason_and_attribution():
    c = _conn()
    _seed_assistant_with_cost(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "text", "text": "answer"}], model=_MODEL,
        stop_reason="end_turn", attribution_skill="superpowers:brainstorming",
        attribution_plugin="superpowers")
    turn = [it for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"][0]
    assert turn["stop_reason"] == "end_turn"
    assert turn["attribution_skill"] == "superpowers:brainstorming"
    assert turn["attribution_plugin"] == "superpowers"


def test_stop_reason_is_last_non_null_across_fragments():
    # Two fragments share (m1,r1): the SEED fragment has stop_reason=None, the
    # later (prose) fragment carries the terminal 'tool_use'. last-non-null wins.
    c = _conn()
    _msg(c, session_id="s1", uuid="a1a", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant", text="",
         blocks_json=_json.dumps([{"kind": "thinking", "text": "..."}]),
         model=_MODEL, msg_id="m1", req_id="r1", stop_reason=None)
    _msg(c, session_id="s1", uuid="a1b", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:02Z", entry_type="assistant",
         text="done",
         blocks_json=_json.dumps([{"kind": "text", "text": "done"}]),
         model=_MODEL, msg_id="m1", req_id="r1", stop_reason="tool_use")
    turn = [it for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"][0]
    assert turn["stop_reason"] == "tool_use"


def test_stop_reason_last_non_null_keeps_earlier_when_later_is_null():
    # Reverse ordering: the SEED fragment carries the reason; a LATER fragment is
    # null. last-non-null must KEEP the seed value, never blank it.
    c = _conn()
    _msg(c, session_id="s1", uuid="a1a", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="first",
         blocks_json=_json.dumps([{"kind": "text", "text": "first"}]),
         model=_MODEL, msg_id="m1", req_id="r1", stop_reason="end_turn")
    _msg(c, session_id="s1", uuid="a1b", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:02Z", entry_type="assistant", text="",
         blocks_json=_json.dumps([{"kind": "thinking", "text": "..."}]),
         model=_MODEL, msg_id="m1", req_id="r1", stop_reason=None)
    turn = [it for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"][0]
    assert turn["stop_reason"] == "end_turn"


def test_stop_reason_and_attribution_absent_when_null():
    # No stop_reason / attribution on the row -> the keys are omitted, never
    # emitted as null (the absent-when-absent contract).
    c = _conn()
    _seed_assistant_with_cost(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "text", "text": "answer"}], model=_MODEL)
    turn = [it for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"][0]
    assert "stop_reason" not in turn
    assert "attribution_skill" not in turn
    assert "attribution_plugin" not in turn


def test_build_simple_null_msg_id_assistant_surfaces_stop_reason():
    # A null-msg_id assistant routes to _build_simple; its stop_reason /
    # attribution must still surface (the _build_simple tail-unpack path).
    c = _conn()
    _msg(c, session_id="s1", uuid="anull", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="no turn key", model=_MODEL, msg_id=None, req_id=None,
         stop_reason="max_tokens", attribution_skill="sk", attribution_plugin="pl")
    turn = [it for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"][0]
    assert turn["stop_reason"] == "max_tokens"
    assert turn["attribution_skill"] == "sk"
    assert turn["attribution_plugin"] == "pl"


def test_human_item_never_carries_stop_reason():
    # stop_reason / attribution are assistant-only — a human item must not carry
    # them even if the columns were somehow populated.
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="hi",
         stop_reason="end_turn")
    item = cq.get_conversation(c, "s1")["items"][0]
    assert item["kind"] == "human"
    assert "stop_reason" not in item


def test_tail_appended_columns_do_not_shift_cwd_branch_positions():
    # The tail-append must NOT disturb _latest(logical, 10/11) (cwd/git_branch).
    # A turn whose cwd/branch are populated must still surface project_label +
    # git_branch correctly.
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="hi",
         cwd="/home/u/myproj", git_branch="feature-x")
    out = cq.get_conversation(c, "s1")
    assert out["project_label"] == "myproj"
    assert out["git_branch"] == "feature-x"


# ---- 2.3: input / input_truncated pass-through + result.full_length -------
def test_input_keys_survive_to_tool_call():
    # input / input_truncated ride through blocks_json -> the Phase-3
    # tool_use->tool_call sweep (renames kind, moves id) and Phase-4b (skill
    # fold) must NOT strip them.
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Edit", "input_summary": "{}",
                 "input": {"file_path": "/a.py", "old_string": "x"},
                 "input_truncated": False, "id": "t1", "preview": "/a.py"}])
    out = cq.get_conversation(c, "s1")
    calls = [b for it in out["items"] if it["kind"] == "assistant"
             for b in it["blocks"] if b.get("kind") == "tool_call"]
    assert calls, "expected a tool_call block"
    assert calls[0]["input"] == {"file_path": "/a.py", "old_string": "x"}
    assert calls[0]["input_truncated"] is False


def test_input_truncated_flag_survives():
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Write", "input_summary": "{}",
                 "input": {"content": "clipped…"}, "input_truncated": True,
                 "id": "t1", "preview": "x"}])
    call = [b for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"
            for b in it["blocks"] if b.get("kind") == "tool_call"][0]
    assert call["input_truncated"] is True


def test_result_carries_full_length():
    # A tool_use + matching tool_result whose block carries full_length: the
    # Phase-2 fold must copy full_length into use_block["result"].
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}",
                 "input": {"file_path": "/x"}, "input_truncated": False,
                 "id": "t1", "preview": "/x"}])
    _seed_tool_result(c, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "BODY", "truncated": True,
                 "full_length": 99999, "is_error": False, "tool_use_id": "t1"}])
    call = [b for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"
            for b in it["blocks"]
            if b.get("kind") == "tool_call" and b.get("result")][0]
    assert call["result"]["full_length"] == 99999
    assert call["result"]["truncated"] is True
    assert call["result"]["text"] == "BODY"


def test_result_full_length_none_when_absent():
    # A pre-enrichment tool_result block lacking full_length -> the folded
    # result carries full_length: None (the .get default), never KeyErrors.
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}",
                 "id": "t1", "preview": "/x"}])
    _seed_tool_result(c, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "B", "truncated": False,
                 "is_error": False, "tool_use_id": "t1"}])
    call = [b for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"
            for b in it["blocks"]
            if b.get("kind") == "tool_call" and b.get("result")][0]
    assert call["result"]["full_length"] is None


def test_ask_answers_surface_on_tool_call():
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "AskUserQuestion", "input_summary": "{}",
                 "input": {"questions": []}, "input_truncated": False,
                 "id": "t1", "preview": "Q?"}])
    _seed_tool_result(c, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "...", "is_error": False,
                 "tool_use_id": "t1",
                 "ask_answers": {"Q?": "Comprehensive"},
                 "ask_annotations": {"Q?": {"notes": "n"}}}])
    call = [b for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"
            for b in it["blocks"] if b.get("kind") == "tool_call"][0]
    assert call["answers"] == {"Q?": "Comprehensive"}
    assert call["annotations"] == {"Q?": {"notes": "n"}}


def test_ask_answers_never_leak_on_orphan_result():
    # A tool_result whose tool_use_id matches NO request stays a standalone
    # orphan item; its internal ask_* keys must be stripped from public JSON.
    c = _conn()
    _seed_tool_result(c, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "x", "is_error": False,
                 "tool_use_id": "nomatch",
                 "ask_answers": {"Q": "A"}}])
    out = cq.get_conversation(c, "s1")
    for it in out["items"]:
        for b in it["blocks"]:
            assert "ask_answers" not in b
            assert "ask_annotations" not in b


def test_tool_call_without_ask_answers_has_no_answers_key():
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}",
                 "input": {"file_path": "/x"}, "input_truncated": False,
                 "id": "t1", "preview": "/x"}])
    call = [b for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"
            for b in it["blocks"] if b.get("kind") == "tool_call"][0]
    assert "answers" not in call
    assert "annotations" not in call


# ---------------------------------------------------------------------------
# #177 S3: Bash stderr/interrupted Phase-1-pop / Phase-3-stamp onto tool_call.
# Mirrors the ask_answers join: the parser stashed bash_stderr/bash_interrupted
# on the tool_result block; Phase 1 pops them into bash_link keyed by
# tool_use_id; Phase 3 stamps call.stderr/call.interrupted as siblings of
# `result`. The split must survive even when the Phase-2 fold SKIPS the result
# row (orphan / multi-owner / mixed) — exactly the class the pattern exists for.
# ---------------------------------------------------------------------------
def test_bash_streams_surface_on_tool_call():
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Bash", "input_summary": "{}",
                 "input": {"command": "ls"}, "input_truncated": False,
                 "id": "t1", "preview": "ls"}])
    _seed_tool_result(c, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "out\nboom", "is_error": True,
                 "tool_use_id": "t1",
                 "bash_stderr": "boom", "bash_interrupted": True}])
    call = [b for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"
            for b in it["blocks"] if b.get("kind") == "tool_call"][0]
    assert call["stderr"] == "boom"
    assert call["interrupted"] is True


def test_bash_streams_stamped_on_tool_call_even_when_fold_skipped():
    # The tool_result row carries a NON-result block alongside the result block,
    # so the Phase-2 fold SKIPS it (result stays standalone). The Phase-1-pop /
    # Phase-3-stamp must still put stderr/interrupted on the matching tool_call —
    # this is the whole reason for the pattern (the fold is the wrong carrier).
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Bash", "input_summary": "{}",
                 "input": {"command": "x"}, "input_truncated": False,
                 "id": "toolu_b", "preview": "x"}])
    _seed_tool_result(c, sid="s1", uuid="r1",
        blocks=[{"kind": "tool_result", "text": "out\nerr", "is_error": False,
                 "tool_use_id": "toolu_b",
                 "bash_stderr": "err", "bash_interrupted": True},
                {"kind": "text", "text": "sibling block defeats the fold"}])
    detail = cq.get_conversation(c, "s1")
    call = [b for it in detail["items"]
            if it["kind"] == "assistant"
            for b in it["blocks"] if b.get("kind") == "tool_call"][0]
    assert call["stderr"] == "err"
    assert call["interrupted"] is True
    # the result row did NOT fold (mixed row) — it stays standalone, and the
    # request-only tool_call carries the stamp despite result being None.
    assert call["result"] is None
    assert any(it["kind"] == "tool_result" for it in detail["items"])
    # private keys never leak into ANY emitted block:
    dumped = _json.dumps(detail)
    assert "bash_stderr" not in dumped
    assert "bash_interrupted" not in dumped


def test_bash_streams_never_leak_on_orphan_result():
    # A tool_result whose tool_use_id matches NO request stays standalone; its
    # internal bash_* keys must be stripped from public JSON (Phase-1 pop).
    c = _conn()
    _seed_tool_result(c, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "x", "is_error": False,
                 "tool_use_id": "nomatch",
                 "bash_stderr": "boom", "bash_interrupted": True}])
    out = cq.get_conversation(c, "s1")
    for it in out["items"]:
        for b in it["blocks"]:
            assert "bash_stderr" not in b
            assert "bash_interrupted" not in b


def test_tool_call_without_bash_streams_has_no_stderr_key():
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}",
                 "input": {"file_path": "/x"}, "input_truncated": False,
                 "id": "t1", "preview": "/x"}])
    call = [b for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"
            for b in it["blocks"] if b.get("kind") == "tool_call"][0]
    assert "stderr" not in call
    assert "interrupted" not in call


# ---------------------------------------------------------------------------
# Task* checklist fold (_fold_task_runs): the live to-do mechanism. State spans
# the whole session, keyed on the explicit (never-reused) task id; the running
# todos[] snapshot is stamped onto each Task* run's FIRST tool_call.
# ---------------------------------------------------------------------------
def _task_use(name, tuid, **inp):
    return {"kind": "tool_use", "name": name, "input_summary": "{}",
            "input": inp, "input_truncated": False, "id": tuid,
            "preview": inp.get("subject") or inp.get("status") or ""}


def _task_res(tuid, **extra):
    return {"kind": "tool_result", "text": "ok", "is_error": False,
            "tool_use_id": tuid, **extra}


def test_task_fold_stamps_snapshot_on_first_call_of_run():
    # NOTE: conversation_messages has UNIQUE(source_path, byte_offset) and
    # _seed_tool_result hardcodes byte_offset=1, so distinct result rows must
    # carry distinct source_path values (same disambiguation the existing
    # parallel-result test uses) or the second INSERT OR IGNORE silently drops.
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1", ts="t1",
        blocks=[_task_use("TaskCreate", "c1", subject="Alpha", activeForm="Alphaing"),
                _task_use("TaskCreate", "c2", subject="Beta", activeForm="Betaing")])
    _seed_tool_result(c, sid="s1", uuid="u1", ts="t2", source_path="r1.jsonl", blocks=[_task_res("c1", task_id="1")])
    _seed_tool_result(c, sid="s1", uuid="u2", ts="t3", source_path="r2.jsonl", blocks=[_task_res("c2", task_id="2")])
    _seed_assistant(c, sid="s1", uuid="a2", msg_id="m2", req_id="r2", ts="t4", source_path="t2.jsonl",
        blocks=[_task_use("TaskUpdate", "u3id", taskId="1", status="in_progress")])
    _seed_tool_result(c, sid="s1", uuid="u3", ts="t5", source_path="r3.jsonl", blocks=[_task_res("u3id", task_id="1")])
    items = cq.get_conversation(c, "s1")["items"]
    calls = [b for it in items if it["kind"] == "assistant"
             for b in it["blocks"] if b.get("kind") == "tool_call"]
    snap1 = calls[0]["task_snapshot"]
    assert [(t["content"], t["status"]) for t in snap1] == [("Alpha", "pending"), ("Beta", "pending")]
    assert snap1[0]["activeForm"] == "Alphaing"
    assert "task_snapshot" not in calls[1]
    snap2 = calls[2]["task_snapshot"]
    assert [(t["content"], t["status"]) for t in snap2] == [("Alpha", "in_progress"), ("Beta", "pending")]


def test_task_fold_drops_deleted_tasks():
    # distinct source_path per row to dodge UNIQUE(source_path, byte_offset).
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1", ts="t1",
        blocks=[_task_use("TaskCreate", "c1", subject="Alpha", activeForm="A"),
                _task_use("TaskCreate", "c2", subject="Beta", activeForm="B")])
    _seed_tool_result(c, sid="s1", uuid="u1", ts="t2", source_path="r1.jsonl", blocks=[_task_res("c1", task_id="1")])
    _seed_tool_result(c, sid="s1", uuid="u2", ts="t3", source_path="r2.jsonl", blocks=[_task_res("c2", task_id="2")])
    _seed_assistant(c, sid="s1", uuid="a2", msg_id="m2", req_id="r2", ts="t4", source_path="t2.jsonl",
        blocks=[_task_use("TaskUpdate", "d1", taskId="1", status="deleted")])
    _seed_tool_result(c, sid="s1", uuid="u3", ts="t5", source_path="r3.jsonl", blocks=[_task_res("d1", task_id="1")])
    calls = [b for it in cq.get_conversation(c, "s1")["items"] if it["kind"] == "assistant"
             for b in it["blocks"] if b.get("kind") == "tool_call"]
    assert [t["content"] for t in calls[-1]["task_snapshot"]] == ["Beta"]


def test_task_fold_tasklist_seeds_snapshot_from_result():
    # reviewer adj. 1: TaskList toolUseResult shape VERIFIED against real data
    # ({"tasks":[{id,subject,status,blockedBy}]}); the reseed path is exercised
    # here through the parser-stashed task_list link.
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1", ts="t1",
        blocks=[_task_use("TaskList", "l1")])
    _seed_tool_result(c, sid="s1", uuid="u1", ts="t2",
        blocks=[_task_res("l1", task_list=[
            {"id": "1", "subject": "X", "status": "completed"},
            {"id": "2", "subject": "Y", "status": "pending"}])])
    call = [b for it in cq.get_conversation(c, "s1")["items"] if it["kind"] == "assistant"
            for b in it["blocks"] if b.get("kind") == "tool_call"][0]
    assert [(t["content"], t["status"]) for t in call["task_snapshot"]] == [("X", "completed"), ("Y", "pending")]


def test_task_internal_keys_never_leak():
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1", ts="t1",
        blocks=[_task_use("TaskCreate", "c1", subject="Alpha", activeForm="A")])
    _seed_tool_result(c, sid="s1", uuid="u1", ts="t2", blocks=[_task_res("c1", task_id="1")])
    for it in cq.get_conversation(c, "s1")["items"]:
        for b in it["blocks"]:
            assert "task_id" not in b and "task_list" not in b


def test_non_task_run_has_no_snapshot():
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1", ts="t1",
        blocks=[{"kind": "tool_use", "name": "Read", "input_summary": "{}",
                 "input": {"file_path": "/x"}, "input_truncated": False, "id": "t1", "preview": "/x"}])
    call = [b for it in cq.get_conversation(c, "s1")["items"] if it["kind"] == "assistant"
            for b in it["blocks"] if b.get("kind") == "tool_call"][0]
    assert "task_snapshot" not in call


def test_task_fold_scopes_per_subagent():
    # Two parallel subagents each run their OWN checklist with DISJOINT task ids
    # (the real shape: agent A creates #7..#8, agent B creates #1..#2). A single
    # shared fold state would bleed one subagent's tasks into the other's card;
    # scoping by subagent_key keeps each thread's snapshot to its own tasks.
    c = _conn()
    # subagent A (source_path agent-aaaa1111.jsonl -> subagent_key "aaaa1111")
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1", ts="t1",
        source_path="agent-aaaa1111.jsonl",
        blocks=[_task_use("TaskCreate", "ca7", subject="Alpha7"),
                _task_use("TaskCreate", "ca8", subject="Alpha8")])
    _seed_tool_result(c, sid="s1", uuid="ua7", ts="t2", source_path="ra7.jsonl", blocks=[_task_res("ca7", task_id="7")])
    _seed_tool_result(c, sid="s1", uuid="ua8", ts="t3", source_path="ra8.jsonl", blocks=[_task_res("ca8", task_id="8")])
    # subagent B (source_path agent-bbbb2222.jsonl -> subagent_key "bbbb2222")
    _seed_assistant(c, sid="s1", uuid="b1", msg_id="m2", req_id="r2", ts="t4",
        source_path="agent-bbbb2222.jsonl",
        blocks=[_task_use("TaskCreate", "cb1", subject="Beta1"),
                _task_use("TaskCreate", "cb2", subject="Beta2")])
    _seed_tool_result(c, sid="s1", uuid="ub1", ts="t5", source_path="rb1.jsonl", blocks=[_task_res("cb1", task_id="1")])
    _seed_tool_result(c, sid="s1", uuid="ub2", ts="t6", source_path="rb2.jsonl", blocks=[_task_res("cb2", task_id="2")])
    items = cq.get_conversation(c, "s1")["items"]
    snaps = {}
    for it in items:
        if it["kind"] != "assistant":
            continue
        for b in it["blocks"]:
            if b.get("kind") == "tool_call" and "task_snapshot" in b:
                snaps[it["subagent_key"]] = [t["content"] for t in b["task_snapshot"]]
                break
    assert snaps["aaaa1111"] == ["Alpha7", "Alpha8"]
    assert snaps["bbbb2222"] == ["Beta1", "Beta2"]


def test_task_fold_omits_snapshot_when_no_create_recognized():
    # Degradation guard: a Task* run whose create results carry NO id (a future
    # unhandled result shape, or pre-fix legacy rows) must NOT stamp an empty
    # snapshot — the frontend then falls back to generic chips instead of a
    # misleading "0 / 0" card. The tell is the ABSENCE of the task_snapshot key.
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1", ts="t1",
        blocks=[_task_use("TaskCreate", "c1", subject="Alpha")])
    # result row carries no task_id (unrecognized shape) -> empty task_link
    _seed_tool_result(c, sid="s1", uuid="u1", ts="t2", source_path="r1.jsonl", blocks=[_task_res("c1")])
    _seed_assistant(c, sid="s1", uuid="a2", msg_id="m2", req_id="r2", ts="t3", source_path="t2.jsonl",
        blocks=[_task_use("TaskUpdate", "u2", taskId="1", status="completed")])
    _seed_tool_result(c, sid="s1", uuid="u3", ts="t4", source_path="r3.jsonl", blocks=[_task_res("u2")])
    calls = [b for it in cq.get_conversation(c, "s1")["items"] if it["kind"] == "assistant"
             for b in it["blocks"] if b.get("kind") == "tool_call"]
    assert calls, "expected the Task* tool_calls to survive"
    assert all("task_snapshot" not in b for b in calls)


# ---------------------------------------------------------------------------
# #178: on-demand "load full" kernels. locate_tool_payload finds the JSONL line
# holding a tool_use (which='input') or tool_result (which='result') by an
# instr() prefilter (NOT LIKE — tool_use_ids contain '_', a LIKE wildcard) +
# exact match; read_full_payload re-reads that raw line from disk and returns
# the full un-capped input dict or result text + Bash stderr. The cache stores
# only capped text, so the full body is always re-derived here (the #178 point).
# ---------------------------------------------------------------------------
def test_locate_tool_payload_uses_instr_not_like():
    # tool_use_id contains '_' (a LIKE wildcard). instr() must match it
    # literally and NOT match a near-miss id where '_' stood in for another char.
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a", msg_id="m1", req_id="r1",
        source_path="/p.jsonl", byte_offset=10,
        blocks=[{"kind": "tool_use", "name": "Edit", "input_summary": "{}",
                 "input": {"file_path": "/f.py", "old_string": "a", "new_string": "b"},
                 "input_truncated": False, "id": "toolu_abc", "preview": "/f.py"}])
    assert cq.locate_tool_payload(c, "s1", "toolu_abc", "input") == ("/p.jsonl", 10)
    # 'tooluXabc' would match 'toolu_abc' under LIKE (the '_' wildcard); instr
    # is literal so it must NOT resolve.
    assert cq.locate_tool_payload(c, "s1", "tooluXabc", "input") is None
    # no tool_result row carries this id -> which='result' is None.
    assert cq.locate_tool_payload(c, "s1", "toolu_abc", "result") is None


def test_locate_tool_payload_finds_result_row():
    c = _conn()
    _seed_tool_result(c, sid="s1", uuid="u1", source_path="/r.jsonl",
        blocks=[{"kind": "tool_result", "text": "out", "is_error": False,
                 "tool_use_id": "toolu_res"}])
    # _seed_tool_result hard-codes byte_offset=1.
    assert cq.locate_tool_payload(c, "s1", "toolu_res", "result") == ("/r.jsonl", 1)
    assert cq.locate_tool_payload(c, "s1", "toolu_res", "input") is None


def test_locate_tool_payload_unknown_id_is_none():
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a", msg_id="m1", req_id="r1",
        source_path="/p.jsonl", byte_offset=0,
        blocks=[{"kind": "tool_use", "name": "Bash", "input_summary": "{}",
                 "input": {"command": "ls"}, "input_truncated": False,
                 "id": "toolu_b", "preview": "ls"}])
    assert cq.locate_tool_payload(c, "s1", "toolu_missing", "input") is None


def test_read_full_payload_input_beyond_leaf_cap(tmp_path):
    # The full input dict is re-derived from the raw JSONL line, so the
    # 8000-char _INPUT_LEAF_CAP that bounds the cached input never applies here.
    line = _json.dumps({"message": {"content": [
        {"type": "tool_use", "id": "toolu_e",
         "input": {"file_path": "/f.py", "old_string": "X" * 9000, "new_string": "Y"}},
    ]}}).encode() + b"\n"
    p = tmp_path / "s.jsonl"
    with open(p, "wb") as fh:
        fh.write(line)
    got = cq.read_full_payload(str(p), 0, "toolu_e", "input")
    assert got["which"] == "input"
    assert got["tool_use_id"] == "toolu_e"
    assert got["input"]["old_string"] == "X" * 9000   # FULL, beyond the leaf cap
    assert got["full_length"] > 9000
    assert got["truncated"] is False


def test_read_full_payload_result_with_bash_stderr(tmp_path):
    line = _json.dumps({
        "toolUseResult": {"stdout": "out\n", "stderr": "boom", "interrupted": False},
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_b",
             "content": [{"type": "text", "text": "out\nboom"}], "is_error": True},
        ]}}).encode() + b"\n"
    p = tmp_path / "r.jsonl"
    with open(p, "wb") as fh:
        fh.write(line)
    got = cq.read_full_payload(str(p), 0, "toolu_b", "result")
    assert got["which"] == "result"
    assert got["text"] == "out\nboom"
    assert got["is_error"] is True
    assert got["stderr"] == "boom"
    assert got["truncated"] is False


def test_read_full_payload_seeks_to_byte_offset(tmp_path):
    # Two lines in one file; the second is the target. read_full_payload must
    # seek the given byte_offset and read THAT line.
    first = _json.dumps({"message": {"content": []}}).encode() + b"\n"
    second = _json.dumps({"message": {"content": [
        {"type": "tool_use", "id": "toolu_2", "input": {"k": "v"}}]}}).encode() + b"\n"
    p = tmp_path / "multi.jsonl"
    with open(p, "wb") as fh:
        fh.write(first)
        fh.write(second)
    got = cq.read_full_payload(str(p), len(first), "toolu_2", "input")
    assert got is not None and got["input"] == {"k": "v"}


def test_read_full_payload_source_gone_returns_none(tmp_path):
    assert cq.read_full_payload(str(tmp_path / "missing.jsonl"), 0, "x", "result") is None


def test_read_full_payload_id_absent_in_line_returns_none(tmp_path):
    line = _json.dumps({"message": {"content": [
        {"type": "tool_use", "id": "toolu_other", "input": {}}]}}).encode() + b"\n"
    p = tmp_path / "s.jsonl"
    with open(p, "wb") as fh:
        fh.write(line)
    assert cq.read_full_payload(str(p), 0, "toolu_missing", "input") is None


def test_read_full_payload_result_huge_hits_ceiling(tmp_path):
    big = "z" * (cq._FULL_PAYLOAD_CEILING + 100)
    line = _json.dumps({"message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_big",
         "content": [{"type": "text", "text": big}]}]}}).encode() + b"\n"
    p = tmp_path / "big.jsonl"
    with open(p, "wb") as fh:
        fh.write(line)
    got = cq.read_full_payload(str(p), 0, "toolu_big", "result")
    assert got["full_length"] == len(big)
    assert len(got["text"]) == cq._FULL_PAYLOAD_CEILING
    assert got["truncated"] is True


def test_read_full_payload_input_single_giant_leaf_hits_ceiling(tmp_path):
    # A single string leaf larger than the ceiling: the returned input dict MUST
    # serialize within _FULL_PAYLOAD_CEILING and truncated MUST be True.
    big = "X" * (cq._FULL_PAYLOAD_CEILING + 5000)
    line = _json.dumps({"message": {"content": [
        {"type": "tool_use", "id": "toolu_e",
         "input": {"file_path": "/f.py", "old_string": big, "new_string": "Y"}},
    ]}}).encode() + b"\n"
    p = tmp_path / "s.jsonl"
    with open(p, "wb") as fh:
        fh.write(line)
    got = cq.read_full_payload(str(p), 0, "toolu_e", "input")
    assert len(_json.dumps(got["input"], ensure_ascii=False)) <= cq._FULL_PAYLOAD_CEILING
    assert got["truncated"] is True
    # full_length describes the UN-capped input (so the UI can show "X of Y").
    assert got["full_length"] > cq._FULL_PAYLOAD_CEILING


def test_read_full_payload_input_many_sub_ceiling_leaves_aggregate(tmp_path):
    # Many leaves that are EACH below the ceiling but TOGETHER serialize past it.
    # This is the aggregate-guard case: a pure per-leaf clip (the pre-fix
    # behavior) would leave every leaf un-clipped and the dict would serialize to
    # ~2 MB while only flipping truncated=True (no actual shrink). The shared
    # remaining-char budget must shrink it to <= the ceiling.
    leaf = "Q" * (cq._FULL_PAYLOAD_CEILING // 2)   # 2 of these alone exceed the ceiling
    line = _json.dumps({"message": {"content": [
        {"type": "tool_use", "id": "toolu_m",
         "input": {"a": leaf, "b": leaf, "c": leaf}},   # ~1.5 MB of leaves
    ]}}).encode() + b"\n"
    p = tmp_path / "m.jsonl"
    with open(p, "wb") as fh:
        fh.write(line)
    got = cq.read_full_payload(str(p), 0, "toolu_m", "input")
    assert len(_json.dumps(got["input"], ensure_ascii=False)) <= cq._FULL_PAYLOAD_CEILING
    assert got["truncated"] is True


# ---------------------------------------------------------------------------
# #177 S4: web_search / web_fetch Phase-1-pop / Phase-3 NAME-KEYED stamp, and
# result.media ride-through. Mirrors the ask_answers / bash join exactly: the
# parser stashed web_search/web_fetch on the tool_result block, Phase 1 pops
# them, Phase 3 stamps the owning tool_call ONLY when the call's NAME matches
# (WebSearch / WebFetch) so a shape-coincident toolUseResult on some other tool
# never decorates the wrong card (Codex F3). `media` is a PUBLIC key and rides
# the fold onto result.media (and survives on orphan blocks).
# ---------------------------------------------------------------------------
def test_web_search_folds_onto_websearch_call():
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "WebSearch", "input_summary": "{}",
                 "input": {"query": "q"}, "input_truncated": False,
                 "id": "tw", "preview": "q"}])
    _seed_tool_result(c, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "x", "is_error": False,
                 "tool_use_id": "tw",
                 "web_search": {"query": "q",
                                "links": [{"title": "T", "url": "https://e/x"}]}}])
    detail = cq.get_conversation(c, "s1")
    call = [b for it in detail["items"]
            if it["kind"] == "assistant"
            for b in it["blocks"] if b.get("kind") == "tool_call"][0]
    assert call["web_search"] == {"query": "q",
                                  "links": [{"title": "T", "url": "https://e/x"}]}
    # the folded result dict must NOT carry the parser-private key:
    assert call["result"] is not None and "web_search" not in call["result"]
    assert "web_search" not in _json.dumps(call["result"])


def test_web_fetch_folds_onto_webfetch_call():
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "WebFetch", "input_summary": "{}",
                 "input": {"url": "https://e/x"}, "input_truncated": False,
                 "id": "tf", "preview": "https://e/x"}])
    _seed_tool_result(c, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "# md", "is_error": False,
                 "tool_use_id": "tf",
                 "web_fetch": {"code": 200, "code_text": "OK"}}])
    call = [b for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"
            for b in it["blocks"] if b.get("kind") == "tool_call"][0]
    assert call["web_fetch"] == {"code": 200, "code_text": "OK"}


def test_web_keys_never_stamp_on_other_tool_names():
    # Same result-block keys, but the OWNING tool is not WebSearch/WebFetch ->
    # the name-keyed Phase-3 join must refuse to decorate it (Codex F3).
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Grep", "input_summary": "{}",
                 "input": {"pattern": "x"}, "input_truncated": False,
                 "id": "tg", "preview": "x"}])
    _seed_tool_result(c, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "x", "is_error": False,
                 "tool_use_id": "tg",
                 "web_search": {"query": "q", "links": []},
                 "web_fetch": {"code": 200, "code_text": "OK"}}])
    detail = cq.get_conversation(c, "s1")
    call = [b for it in detail["items"]
            if it["kind"] == "assistant"
            for b in it["blocks"] if b.get("kind") == "tool_call"][0]
    assert "web_search" not in call
    assert "web_fetch" not in call
    # and never leak anywhere in the emitted JSON either:
    dumped = _json.dumps(detail)
    assert "web_search" not in dumped and "web_fetch" not in dumped


def test_orphan_result_drops_private_web_keys_but_keeps_media():
    # A tool_result with NO owning assistant stays standalone. The parser-private
    # web_search/web_fetch keys are popped in Phase 1 (never leak), but the PUBLIC
    # `media` placeholder + tool_use_id survive so orphaned screenshots render.
    c = _conn()
    _seed_tool_result(c, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "x", "is_error": False,
                 "tool_use_id": "nomatch",
                 "web_search": {"query": "q", "links": []},
                 "web_fetch": {"code": 200, "code_text": "OK"},
                 "media": [{"kind": "image", "media_type": "image/png",
                            "bytes": 4, "index": 0}]}])
    out = cq.get_conversation(c, "s1")
    orphan = [b for it in out["items"] for b in it["blocks"]
              if b.get("kind") == "tool_result"][0]
    assert "web_search" not in orphan and "web_fetch" not in orphan
    assert orphan["tool_use_id"] == "nomatch"
    assert orphan["media"] == [{"kind": "image", "media_type": "image/png",
                                "bytes": 4, "index": 0}]
    assert "web_search" not in _json.dumps(out) and "web_fetch" not in _json.dumps(out)


def test_result_media_rides_fold():
    c = _conn()
    _seed_assistant(c, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "mcp__x__screenshot",
                 "input_summary": "{}", "input": {}, "input_truncated": False,
                 "id": "ts", "preview": "screenshot"}])
    media = [{"kind": "image", "media_type": "image/png", "bytes": 4, "index": 0}]
    _seed_tool_result(c, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "took screenshot", "is_error": False,
                 "tool_use_id": "ts", "media": media}])
    call = [b for it in cq.get_conversation(c, "s1")["items"]
            if it["kind"] == "assistant"
            for b in it["blocks"] if b.get("kind") == "tool_call"][0]
    assert call["result"]["media"] == media


# ---------------------------------------------------------------------------
# #177 S4: locate_media / read_media_bytes kernel pair (mirrors
# locate_tool_payload / read_full_payload). The media walk in read_media_bytes
# IS iter_media_items — the SAME chokepoint the ingest placeholders use, so
# ordinals cannot drift (spec §4.1).
# ---------------------------------------------------------------------------
import base64 as _b64test

PNG_B64 = _b64test.b64encode(b"\x89PNG_fake_pixels").decode()


def _media_line(tmp_path, content_blocks, name="m.jsonl"):
    p = tmp_path / name
    line = _json.dumps({"type": "user", "uuid": "u1", "sessionId": "s1",
                        "timestamp": "t",
                        "message": {"role": "user", "content": content_blocks}})
    p.write_text(line + "\n")
    return str(p)


def test_read_media_bytes_roundtrip_tool_result(tmp_path):
    src = _media_line(tmp_path, [
        {"type": "tool_result", "tool_use_id": "tu1", "content": [
            {"type": "image", "source": {"media_type": "image/png", "data": PNG_B64}}]}])
    status, mt, raw = cq.read_media_bytes(src, 0, tool_use_id="tu1", index=0)
    assert (status, mt, raw) == ("ok", "image/png", b"\x89PNG_fake_pixels")


def test_read_media_bytes_uuid_mode_and_ordinal_agreement(tmp_path):
    # one text + two media items at message level: index 1 must return the SECOND
    src = _media_line(tmp_path, [
        {"type": "text", "text": "x"},
        {"type": "image", "source": {"media_type": "image/png", "data": PNG_B64}},
        {"type": "document", "source": {"media_type": "application/pdf",
                                        "data": _b64test.b64encode(b"%PDF-fake").decode()}}])
    status, mt, raw = cq.read_media_bytes(src, 0, uuid="u1", index=1)
    assert (status, mt) == ("ok", "application/pdf") and raw == b"%PDF-fake"


def test_read_media_bytes_failures(tmp_path):
    src = _media_line(tmp_path, [
        {"type": "image", "source": {"media_type": "image/bmp", "data": PNG_B64}},   # not allowlisted
        {"type": "image", "source": {"media_type": "image/png", "data": "%%%bad%%%"}}])  # invalid b64
    assert cq.read_media_bytes(src, 0, uuid="u1", index=0)[0] == "unsupported"
    assert cq.read_media_bytes(src, 0, uuid="u1", index=1)[0] == "gone"
    assert cq.read_media_bytes(src, 0, uuid="u1", index=9)[0] == "gone"      # ordinal drift
    assert cq.read_media_bytes(str(tmp_path / "nope"), 0, uuid="u1", index=0)[0] == "gone"


def test_read_media_bytes_encoded_precheck_never_decodes(monkeypatch, tmp_path):
    big = "A" * (cq._MEDIA_PAYLOAD_CEILING * 4 // 3 + 8)
    src = _media_line(tmp_path, [{"type": "image",
                                  "source": {"media_type": "image/png", "data": big}}])
    called = []
    monkeypatch.setattr(cq._base64, "b64decode",
                        lambda *a, **k: called.append(1))
    assert cq.read_media_bytes(src, 0, uuid="u1", index=0)[0] == "too_large"
    assert not called          # precheck fired BEFORE any decode (Codex F4)


def test_read_media_bytes_line_ceiling(monkeypatch, tmp_path):
    # An over-cap raw line (here via a tiny test-local ceiling) -> too_large,
    # before any parse/decode.
    src = _media_line(tmp_path, [
        {"type": "image", "source": {"media_type": "image/png", "data": PNG_B64}}])
    monkeypatch.setattr(cq, "_MEDIA_LINE_CEILING", 8)
    assert cq.read_media_bytes(src, 0, uuid="u1", index=0)[0] == "too_large"


def test_locate_media_both_modes(tmp_path):
    c = _conn()
    # tool_result row with a media placeholder + a user-content image row.
    _seed_tool_result(c, sid="s1", uuid="ur",
        blocks=[{"kind": "tool_result", "text": "x", "is_error": False,
                 "tool_use_id": "tu1",
                 "media": [{"kind": "image", "media_type": "image/png",
                            "bytes": 4, "index": 0}]}])
    _msg(c, session_id="s1", uuid="u1", source_path="img.jsonl", byte_offset=7,
         timestamp_utc="2026-06-01T00:00:03Z", entry_type="human", text="",
         blocks_json=_json.dumps([{"kind": "image", "media_type": "image/png",
                                   "bytes": 4, "index": 0}]))
    # _seed_tool_result writes byte_offset=1, source_path="a.jsonl"
    assert cq.locate_media(c, "s1", tool_use_id="tu1", index=0) == ("a.jsonl", 1)
    assert cq.locate_media(c, "s1", uuid="u1", index=0) == ("img.jsonl", 7)
    # misses
    assert cq.locate_media(c, "s1", tool_use_id="tu1", index=9) is None
    assert cq.locate_media(c, "s1", tool_use_id="nope", index=0) is None
    assert cq.locate_media(c, "s1", uuid="nope", index=0) is None


# === #177 S6 Task 1b: split multi-column conversation FTS shape ===

def _legacy_conn():
    """Build an OLD-shape cache DB: single-column conversation_fts(text) + the
    legacy conversation_fts_aux(search_aux) + legacy trigger sets + the
    migration-010 pending flag set. Models a pre-S6 install whose sync-side swap
    has not yet run. The base table still gains search_tool/search_thinking
    (idempotent column-adds) so backfill UPDATEs target real columns."""
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)
    if not db._fts5_available(c):
        return c
    # Tear the split shape down and rebuild the legacy two-table shape.
    db._drop_conversation_fts_triggers(c)
    c.execute("DROP TABLE IF EXISTS conversation_fts")
    c.execute("DROP TABLE IF EXISTS conversation_fts_aux")
    c.execute("CREATE VIRTUAL TABLE conversation_fts "
              "USING fts5(text, content='conversation_messages', content_rowid='id')")
    db._create_conversation_fts_aux_table(c)
    db._create_conversation_fts_legacy_triggers(c)
    db._set_cache_meta(c, "conversation_search_split_pending", "1")
    c.commit()
    return c


def test_fresh_schema_has_split_fts():
    c = _conn()
    if not db._fts5_available(c):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    cols = [r[1] for r in c.execute("PRAGMA table_info(conversation_fts)")]
    assert cols == ["text", "search_tool", "search_thinking"]
    assert c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='conversation_fts_aux'"
    ).fetchone() is None


def test_split_triggers_index_all_three_columns():
    c = _conn()
    if not db._fts5_available(c):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    _msg(c, id=1, session_id="s", uuid="u1", source_path="f", byte_offset=0,
         entry_type="assistant", text="alpha",
         search_tool="beta", search_thinking="gamma")
    for col, needle in (("text", "alpha"), ("search_tool", "beta"),
                        ("search_thinking", "gamma")):
        got = c.execute(
            "SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH ?",
            (f"{{{col}}}: {needle}",)).fetchall()
        assert got == [(1,)], (col, got)


def test_split_fts_rebuildable_external_content_column_names():
    # External-content FTS5 binds columns BY NAME — a mismatch creates fine but
    # breaks 'rebuild'. This pins the names match content-table columns.
    c = _conn()
    if not db._fts5_available(c):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    _msg(c, id=1, session_id="s", uuid="u1", source_path="f", byte_offset=0,
         entry_type="assistant", text="alpha",
         search_tool="beta", search_thinking="gamma")
    c.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('rebuild')")
    c.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")
    assert c.execute(
        "SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH '{search_tool}: beta'"
    ).fetchall() == [(1,)]


def test_legacy_shape_left_alone_when_pending():
    c = _legacy_conn()
    if not db._fts5_available(c):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    db._apply_cache_schema(c)   # re-apply must NOT swap the legacy shape
    cols = [r[1] for r in c.execute("PRAGMA table_info(conversation_fts)")]
    assert cols == ["text"]     # untouched until the sync-side swap
    assert c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='conversation_fts_aux'"
    ).fetchone() is not None
    assert db._conversation_fts_is_split(c) is False


# ===========================================================================
# #177 S6 Task 2 — search kernel: kinds, badges, prefix, find endpoint.
# ===========================================================================
import pytest as _pytest


# --- 2a: _fts_query prefix + _kind_match_expr units ------------------------

def test_fts_query_prefix_last_term():
    assert cq._fts_query("npm ru", prefix_last=True) == '"npm" "ru"*'
    assert cq._fts_query('say "hi', prefix_last=True) == '"say" """hi"*'
    # a '*' inside the quotes is a literal char (FTS5 prefix-* lives OUTSIDE the
    # closing quote), so a lone "lone*" term gets quoted then suffixed.
    assert cq._fts_query("lone*", prefix_last=True) == '"lone*"*'
    assert cq._fts_query("", prefix_last=True) == '""'
    # default (no prefix) is byte-identical to the legacy builder.
    assert cq._fts_query("npm ru") == '"npm" "ru"'


def test_kind_match_expr():
    assert cq._kind_match_expr("tools", '"a" "b"') == '{search_tool}: ("a" "b")'
    assert cq._kind_match_expr("thinking", '"a"') == '{search_thinking}: ("a")'
    assert cq._kind_match_expr("prompts", '"a"') == '{text}: ("a")'
    assert cq._kind_match_expr("assistant", '"a"') == '{text}: ("a")'
    assert cq._kind_match_expr("all", '"a"') == '"a"'


# --- 2b: search_conversations kinds / badges / prefix / prose-only / LIKE --

def _seed_kind_corpus(c):
    """Three rows: prose-only (A), tool-only (B), thinking-only (C)."""
    _msg(c, session_id="s1", uuid="A", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="deploy notes about npm releases", cwd="/home/u/proj")
    _msg(c, session_id="s2", uuid="B", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-02T00:00:00Z", entry_type="assistant",
         text="", model=_MODEL, msg_id="mB", req_id="rB",
         search_tool="npm run build", cwd="/home/u/proj")
    _msg(c, session_id="s3", uuid="C", source_path="c.jsonl", byte_offset=0,
         timestamp_utc="2026-06-03T00:00:00Z", entry_type="assistant",
         text="", model=_MODEL, msg_id="mC", req_id="rC",
         search_thinking="should I rerun npm", cwd="/home/u/proj")


def test_search_kind_tools_finds_tool_content_and_badges():
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _seed_kind_corpus(c)
    out = cq.search_conversations(c, "npm", kind="tools")
    assert [h["uuid"] for h in out["hits"]] == ["B"]
    assert out["total"] == 1
    assert out["search_depth"] == "full" and out["kind"] == "tools"
    out_all = cq.search_conversations(c, "npm", kind="all")
    kinds = {h["uuid"]: h.get("match_kinds", []) for h in out_all["hits"]}
    assert kinds["B"] == ["tool"]
    assert kinds["C"] == ["thinking"]
    assert kinds["A"] == []                       # prose never badges


def test_search_badge_probe_is_marker_based_not_nonempty():
    # A row matching ONLY in prose, but whose search_tool is non-empty (and would
    # yield an unmarked snippet): match_kinds MUST stay empty (spec F3).
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _msg(c, session_id="s1", uuid="P", source_path="p.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="alpha beta gamma", search_tool="totally unrelated tool text",
         cwd="/home/u/proj")
    out = cq.search_conversations(c, "alpha", kind="all")
    assert [h["uuid"] for h in out["hits"]] == ["P"]
    assert out["hits"][0].get("match_kinds", []) == []


def test_search_badges_aggregate_across_group_rows():
    # Two physical rows, SAME (session_id, uuid): one matches in text, the other
    # in search_tool. The single deduped hit badges across BOTH rows (F3).
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _msg(c, id=1, session_id="s1", uuid="G", source_path="g1.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="needle here", model=_MODEL, msg_id="mG", req_id="rG",
         cwd="/home/u/proj")
    _msg(c, id=2, session_id="s1", uuid="G", source_path="g2.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="", model=_MODEL, msg_id="mG", req_id="rG",
         search_tool="needle in tool", cwd="/home/u/proj")
    out = cq.search_conversations(c, "needle", kind="all")
    assert len(out["hits"]) == 1
    assert out["hits"][0]["uuid"] == "G"
    assert out["hits"][0]["match_kinds"] == ["tool"]   # aggregated off the 2nd row


def test_search_badge_facet_scope_carries_entry_type_predicate():
    """U3 facet-scope (Codex P2): the bounded rids-by-group lookup must carry the
    SAME entry_type predicate the page query used. A group (s1, G) has a HUMAN
    physical row matching prose AND an ASSISTANT physical row of the same
    (session_id, uuid) carrying a tool match. Under kind='prompts' (entry_type =
    human) the human row is the hit; the assistant row's tool badge must NOT leak
    into the prompts facet — the lookup's et_pred excludes it. Without the
    predicate the row set would include the assistant row and badge ['tool']."""
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _msg(c, id=1, session_id="s1", uuid="G", source_path="g1.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="needle prompt", cwd="/home/u/proj")
    _msg(c, id=2, session_id="s1", uuid="G", source_path="g2.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="", model=_MODEL, msg_id="mG", req_id="rG",
         search_tool="needle in tool", cwd="/home/u/proj")
    out = cq.search_conversations(c, "needle", kind="prompts")
    assert [h["uuid"] for h in out["hits"]] == ["G"]
    # facet-scoped: the prompts hit must NOT inherit the assistant row's tool badge.
    assert out["hits"][0]["match_kinds"] == []


def test_search_prompts_vs_assistant_entry_type_predicate():
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _msg(c, session_id="s1", uuid="H", source_path="h.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="shared marker word", cwd="/home/u/proj")
    _msg(c, session_id="s1", uuid="AA", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="shared marker word", model=_MODEL, msg_id="m1", req_id="r1",
         cwd="/home/u/proj")
    pr = cq.search_conversations(c, "marker", kind="prompts")
    asst = cq.search_conversations(c, "marker", kind="assistant")
    assert [h["uuid"] for h in pr["hits"]] == ["H"]
    assert [h["uuid"] for h in asst["hits"]] == ["AA"]


def test_search_kind_totals_exact_and_pages_disjoint():
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    n = 5
    for i in range(n):
        _msg(c, session_id=f"s{i}", uuid=f"u{i}", source_path=f"f{i}.jsonl",
             byte_offset=0,
             timestamp_utc=f"2026-06-01T00:00:{i:02d}Z", entry_type="assistant",
             text="", model=_MODEL, msg_id=f"m{i}", req_id=f"r{i}",
             search_tool=f"row {i} has the needle keyword", cwd="/home/u/proj")
    p1 = cq.search_conversations(c, "needle", kind="tools", limit=2, offset=0)
    p2 = cq.search_conversations(c, "needle", kind="tools", limit=2, offset=2)
    p3 = cq.search_conversations(c, "needle", kind="tools", limit=2, offset=4)
    assert p1["total"] == p2["total"] == p3["total"] == n
    assert [len(p["hits"]) for p in (p1, p2, p3)] == [2, 2, 1]
    keys = [(h["session_id"], h["uuid"]) for p in (p1, p2, p3) for h in p["hits"]]
    assert len(set(keys)) == n


def test_search_prefix_last_term_matches_while_typing():
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _msg(c, session_id="s1", uuid="K", source_path="k.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="", model=_MODEL, msg_id="mK", req_id="rK",
         search_tool="cache.db.lock path", cwd="/home/u/proj")
    out = cq.search_conversations(c, "cache.d", kind="tools")
    assert [h["uuid"] for h in out["hits"]] == ["K"]


def test_search_prose_only_mode_when_pending():
    c = _legacy_conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    # legacy single-column FTS still indexes prose; seed a human + assistant row.
    _msg(c, id=1, session_id="s1", uuid="H", source_path="h.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="needle prompt", cwd="/home/u/proj")
    _msg(c, id=2, session_id="s1", uuid="AA", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="needle reply", model=_MODEL, msg_id="m1", req_id="r1",
         cwd="/home/u/proj")
    # kind="all" works via the legacy prose table.
    allout = cq.search_conversations(c, "needle", kind="all")
    assert allout["search_depth"] == "prose-only"
    assert {h["uuid"] for h in allout["hits"]} == {"H", "AA"}
    # tools / thinking short-circuit to empty while prose-only.
    for k in ("tools", "thinking"):
        out = cq.search_conversations(c, "needle", kind=k)
        assert out["hits"] == [] and out["total"] == 0
        assert out["search_depth"] == "prose-only"
    # prompts / assistant still filter by entry_type.
    pr = cq.search_conversations(c, "needle", kind="prompts")
    assert [h["uuid"] for h in pr["hits"]] == ["H"]
    asst = cq.search_conversations(c, "needle", kind="assistant")
    assert [h["uuid"] for h in asst["hits"]] == ["AA"]


def test_search_invalid_kind_raises_value_error():
    c = _conn()
    with _pytest.raises(ValueError):
        cq.search_conversations(c, "x", kind="bogus")


def test_search_like_mode_kinds_and_badges():
    c = _conn()
    _seed_kind_corpus(c)
    # tools kind in LIKE mode scans search_tool only.
    out = cq.search_conversations(c, "npm", kind="tools", fts_available=False)
    assert out["mode"] == "like"
    assert [h["uuid"] for h in out["hits"]] == ["B"]
    # all kind: badges via per-column LIKE probes.
    out_all = cq.search_conversations(c, "npm", kind="all", fts_available=False)
    kinds = {h["uuid"]: h.get("match_kinds", []) for h in out_all["hits"]}
    assert kinds["B"] == ["tool"] and kinds["C"] == ["thinking"]
    assert kinds["A"] == []


def test_search_response_carries_kind_and_depth_on_empty():
    c = _conn()
    out = cq.search_conversations(c, "  ", kind="tools")
    assert out["hits"] == [] and out["total"] == 0
    assert out["kind"] == "tools" and out["search_depth"] == "full"


# --- 2c: find_in_conversation ----------------------------------------------

def _seed_find_session(c):
    """An assistant turn (m1/r1) with prose 'reply' plus a folded tool_result
    row whose search_tool carries 'needle', then a later human row matching
    'needle' in prose. Mirrors a real tool-using transcript."""
    # human kickoff
    _msg(c, id=1, session_id="s1", uuid="hu", source_path="f.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="kick off", cwd="/home/u/proj")
    # assistant turn with a tool_use
    _msg(c, id=2, session_id="s1", uuid="as", source_path="f.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="reply prose", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             {"kind": "text", "text": "reply prose"},
             {"kind": "tool_use", "id": "tu1", "name": "Bash",
              "input": {"command": "rg needle"}}]),
         search_tool="rg needle", cwd="/home/u/proj")
    # tool_result row owned by tu1 (folds into the assistant turn's anchor)
    _msg(c, id=3, session_id="s1", uuid="tr", source_path="f.jsonl", byte_offset=2,
         timestamp_utc="2026-06-01T00:00:02Z", entry_type="tool_result",
         text="found needle line", source_tool_use_id="tu1",
         blocks_json=_json.dumps([
             {"kind": "tool_result", "tool_use_id": "tu1",
              "text": "found needle line"}]),
         search_tool="found needle line", cwd="/home/u/proj")
    # later human row matching 'needle' in prose
    _msg(c, id=4, session_id="s1", uuid="h2", source_path="f.jsonl", byte_offset=3,
         timestamp_utc="2026-06-01T00:00:03Z", entry_type="human",
         text="recheck the needle", cwd="/home/u/proj")


def test_find_returns_rendered_turn_anchors_in_document_order():
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _seed_find_session(c)
    out = cq.find_in_conversation(c, "s1", "needle")
    assert out is not None
    # anchor 0 = the assistant turn (the tool_result folds into it; F1),
    # badged 'tool'; anchor 1 = the later human prose turn (unbadged).
    assert len(out["anchors"]) == 2
    assert out["anchors"][0]["uuid"] == "as"
    assert out["anchors"][0]["match_kinds"] == ["tool"]
    assert out["anchors"][1]["uuid"] == "h2"
    assert out["anchors"][1]["match_kinds"] == []
    assert out["total"] == 2 and out["anchors_truncated"] is False
    assert out["mode"] == "fts" and out["search_depth"] == "full"


def test_find_collapses_multi_member_matches_to_one_anchor():
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    # two assistant fragments sharing (msg_id, req_id): u1 matches in text,
    # u2 matches in search_thinking → ONE anchor, ["thinking"] (prose unbadged).
    _msg(c, id=1, session_id="s1", uuid="u1", source_path="f.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="needle prose", model=_MODEL, msg_id="m1", req_id="r1",
         cwd="/home/u/proj")
    _msg(c, id=2, session_id="s1", uuid="u2", source_path="f.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="", model=_MODEL, msg_id="m1", req_id="r1",
         search_thinking="needle thought", cwd="/home/u/proj")
    out = cq.find_in_conversation(c, "s1", "needle")
    assert len(out["anchors"]) == 1
    assert out["anchors"][0]["uuid"] == "u1"        # prose fragment is the anchor
    assert out["anchors"][0]["match_kinds"] == ["thinking"]
    assert out["total"] == 1


def test_find_cap_and_truncated_flag():
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    for i in range(12):
        _msg(c, id=i + 1, session_id="s1", uuid=f"u{i}", source_path="f.jsonl",
             byte_offset=i, timestamp_utc=f"2026-06-01T00:00:{i:02d}Z",
             entry_type="human", text=f"needle {i}", cwd="/home/u/proj")
    out = cq.find_in_conversation(c, "s1", "needle", cap=10)
    assert len(out["anchors"]) == 10
    assert out["anchors_truncated"] is True
    assert out["total"] == 12


def test_find_unknown_session_returns_none():
    c = _conn()
    assert cq.find_in_conversation(c, "nope", "x") is None


def test_find_empty_query_returns_empty():
    c = _conn()
    _seed_find_session(c)
    out = cq.find_in_conversation(c, "s1", "   ")
    assert out is not None
    assert out["anchors"] == [] and out["total"] == 0


def test_find_empty_query_skips_session_assembly(monkeypatch):
    """Opening the find bar (empty q) must NOT pay the full session assembly —
    the existence probe short-circuits before _assemble_session runs."""
    c = _conn()
    _seed_find_session(c)
    calls = []
    monkeypatch.setattr(cq, "_assemble_session",
                        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
                            AssertionError("assembly should be skipped")))
    out = cq.find_in_conversation(c, "s1", "   ")
    assert out is not None
    assert out["anchors"] == [] and out["total"] == 0
    assert calls == []
    # A prose-only-blocked kind short-circuits the same way (still no assembly).
    blocked = cq.find_in_conversation(c, "s1", "", kind="tools")
    assert blocked["anchors"] == [] and calls == []


def test_find_unknown_session_empty_query_returns_none(monkeypatch):
    """Unknown session → None even for an empty query (the route's 404), and the
    existence probe gets there WITHOUT assembling — precedence pinned."""
    c = _conn()
    _seed_find_session(c)
    monkeypatch.setattr(cq, "_assemble_session",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("assembly should be skipped")))
    assert cq.find_in_conversation(c, "nope", "   ") is None
    assert cq.find_in_conversation(c, "nope", "needle") is None


def test_find_zero_match_skips_assembly(monkeypatch):
    """U2 (#217 S1): a NON-EMPTY query that matches ZERO rows in a KNOWN session
    must return the empty base WITHOUT assembling — the match probe runs first
    and short-circuits before _assemble_session. (Today the assembly runs before
    the match, paying a full session walk for a no-result find.)"""
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _seed_find_session(c)
    calls = []
    real_assemble = cq._assemble_session
    monkeypatch.setattr(
        cq, "_assemble_session",
        lambda *a, **k: calls.append(1) or real_assemble(*a, **k))
    out = cq.find_in_conversation(c, "s1", "zzznotpresentzzz")
    assert out is not None
    assert out["anchors"] == [] and out["total"] == 0
    assert out["anchors_truncated"] is False
    assert calls == [], "zero-match find must not assemble the session"
    # Same for the LIKE fallback path.
    out_like = cq.find_in_conversation(
        c, "s1", "zzznotpresentzzz", fts_available=False)
    assert out_like["anchors"] == [] and out_like["total"] == 0
    assert calls == []


def test_find_nonzero_match_output_unchanged():
    """U2: a query that DOES match still assembles and produces byte-identical
    output to the pre-reorder behavior (anchors, badges, totals, mode/depth).
    Compared field-by-field against the known-good the existing find tests pin."""
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _seed_find_session(c)
    out = cq.find_in_conversation(c, "s1", "needle")
    assert out is not None
    # Byte-identical to test_find_returns_rendered_turn_anchors_in_document_order.
    assert out["anchors"] == [
        {"uuid": "as", "match_kinds": ["tool"]},
        {"uuid": "h2", "match_kinds": []},
    ]
    assert out["total"] == 2
    assert out["anchors_truncated"] is False
    assert out["mode"] == "fts"
    assert out["search_depth"] == "full"
    assert out["kind"] == "all"


def test_find_kind_scoping_and_like_mode():
    c = _conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _seed_find_session(c)
    # kind="thinking" matches nothing here (no thinking column carries 'needle').
    out = cq.find_in_conversation(c, "s1", "needle", kind="thinking")
    assert out["anchors"] == []
    # kind="tools" matches only the assistant-folded tool row → its anchor.
    tools = cq.find_in_conversation(c, "s1", "needle", kind="tools")
    assert [a["uuid"] for a in tools["anchors"]] == ["as"]
    # LIKE mode returns the same anchors as FTS for a simple needle.
    likeout = cq.find_in_conversation(c, "s1", "needle", fts_available=False)
    assert [a["uuid"] for a in likeout["anchors"]] == ["as", "h2"]
    assert likeout["mode"] == "like"


def test_find_prose_only_mode_blocks_tool_thinking():
    c = _legacy_conn()
    if not db._fts5_available(c):
        _pytest.skip("sqlite build lacks FTS5")
    _msg(c, id=1, session_id="s1", uuid="hu", source_path="f.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="needle prompt", cwd="/home/u/proj")
    out = cq.find_in_conversation(c, "s1", "needle", kind="all")
    assert out["search_depth"] == "prose-only"
    assert [a["uuid"] for a in out["anchors"]] == ["hu"]
    blocked = cq.find_in_conversation(c, "s1", "needle", kind="tools")
    assert blocked["anchors"] == [] and blocked["total"] == 0


# ---- #191: read-time recovery of already-ingested injected user lines ----
import json as _json191


def test_stale_human_compaction_recovers_to_meta():
    c = _conn()
    body = "This session is being continued from a previous conversation that ran out of context."
    # already-ingested shape: entry_type='human', text=the raw body
    _msg(c, session_id="s", uuid="c1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text=body,
         blocks_json=_json191.dumps([{"kind": "text", "text": body}]))
    conv = cq.get_conversation(c, "s")
    it = conv["items"][0]
    assert it["kind"] == "meta" and it["meta_kind"] == "compaction"


def test_stale_human_task_notification_recovers_to_meta():
    c = _conn()
    body = "<task-notification>\n<task-id>x</task-id>\n<summary>done</summary>\n</task-notification>"
    _msg(c, session_id="s", uuid="n1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text=body,
         blocks_json=_json191.dumps([{"kind": "text", "text": body}]))
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "meta" and it["meta_kind"] == "notification"


def test_stale_human_bash_echo_recovers_to_command_meta():
    c = _conn()
    body = "<bash-input>pwd</bash-input>"
    _msg(c, session_id="s", uuid="b1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text=body,
         blocks_json=_json191.dumps([{"kind": "text", "text": body}]))
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "meta" and it["meta_kind"] == "command"


def test_remote_control_text_stripped_at_read_time():
    c = _conn()
    body = "<system-reminder>Message sent at Sat 2026-06-13 10:47:42 UTC.</system-reminder>\nApproved."
    _msg(c, session_id="s", uuid="rc1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text=body,
         blocks_json=_json191.dumps([{"kind": "text", "text": body}]))
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "human"
    assert it["text"] == "Approved."


def test_title_skips_compaction_first_row_and_strips_remote_control():
    c = _conn()
    # session A: leads with a compaction summary -> title must NOT be the summary
    comp = "This session is being continued from a previous conversation that ran out of context."
    _msg(c, session_id="A", uuid="a1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text=comp,
         blocks_json=_json191.dumps([{"kind": "text", "text": comp}]))
    _msg(c, session_id="A", uuid="a2", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:01:00Z", entry_type="human", text="the real first prompt",
         blocks_json=_json191.dumps([{"kind": "text", "text": "the real first prompt"}]))
    # session B: leads with a remote-control reply -> title strips the stamp
    rc = "<system-reminder>Message sent at Sat 2026-06-13 10:47:42 UTC.</system-reminder>\nMerge to main."
    _msg(c, session_id="B", uuid="b1", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-02T00:00:00Z", entry_type="human", text=rc,
         blocks_json=_json191.dumps([{"kind": "text", "text": rc}]))
    titles = {cv["session_id"]: cv["title"] for cv in _list_conversations(c)["conversations"]}
    assert titles["A"] == "the real first prompt"
    assert titles["B"] == "Merge to main."


def test_fresh_meta_compaction_excluded_from_prompts_search():
    # P2a: a FRESH compaction row (entry_type='meta', text='') is not in the
    # prose FTS / prompts facet. (Stale human rows are NOT asserted -- they heal
    # on reingest.)
    c = _conn()
    body = "This session is being continued from a previous conversation that ran out of context."
    _msg(c, session_id="s", uuid="c1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="meta", text="",
         blocks_json=_json191.dumps([{"kind": "text", "text": body}]))
    # P1c both-halves: a fresh META row (text='') still recovers its label from
    # the body at read time.
    it = cq.get_conversation(c, "s")["items"][0]
    assert it["kind"] == "meta" and it["meta_kind"] == "compaction"
    # P2a: and it is absent from the prose / prompts search facet.
    res = cq.search_conversations(c, "continued from a previous", kind="prompts")
    assert all("c1" not in (h.get("uuid") or "") for h in res.get("hits", []))


# ---------------------------------------------------------------------------
# #193 Task 2: conversation_ai_titles table + _AI_TITLE_UPSERT_SQL
# ---------------------------------------------------------------------------

def test_conversation_ai_titles_table_and_upsert():
    c = _conn()  # cache.db conn with _apply_cache_schema already run
    cols = {r[1] for r in c.execute("PRAGMA table_info(conversation_ai_titles)")}
    assert cols == {"session_id", "ai_title", "source_path", "byte_offset"}
    import _cctally_cache as cc
    up = cc._AI_TITLE_UPSERT_SQL
    c.execute(up, ("s1", "First", "/p/s1.jsonl", 10)); c.commit()
    c.execute(up, ("s1", "Second", "/p/s1.jsonl", 50)); c.commit()  # later write wins
    assert c.execute(
        "SELECT ai_title FROM conversation_ai_titles WHERE session_id='s1'"
    ).fetchone()[0] == "Second"


# ---------------------------------------------------------------------------
# #193 Task 4: _session_titles_map ai-title precedence + fallback chain
# ---------------------------------------------------------------------------

def test_session_titles_prefers_ai_title():
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="Do the thing")
    c.execute("INSERT INTO conversation_ai_titles VALUES('s1','AI Title','/p',9)")
    c.commit()
    assert cq._session_titles_map(c, ["s1"]).get("s1") == "AI Title"


def test_session_titles_falls_back_to_first_prompt():
    c = _conn()
    _msg(c, session_id="s2", uuid="h2", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="Do the thing")  # NO ai-title for s2
    assert cq._session_titles_map(c, ["s2"]).get("s2") == "Do the thing"


def test_session_titles_mixed_some_ai_some_fallback():
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="first prompt one")
    _msg(c, session_id="s2", uuid="h2", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="first prompt two")
    c.execute("INSERT INTO conversation_ai_titles VALUES('s1','AI One','/p',9)")
    c.commit()
    m = cq._session_titles_map(c, ["s1", "s2"])
    assert m.get("s1") == "AI One"          # ai-title wins
    assert m.get("s2") == "first prompt two"  # falls back


def test_session_titles_table_absent_degrades_to_first_prompt():
    # a :memory: conn WITHOUT the ai-titles table must not raise — degrades to
    # the existing first-prompt scan (table-absent OperationalError swallowed).
    c = sqlite3.connect(":memory:")
    c.executescript(
        "CREATE TABLE conversation_messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, uuid TEXT,"
        " parent_uuid TEXT, source_path TEXT NOT NULL, byte_offset INTEGER NOT NULL,"
        " timestamp_utc TEXT, entry_type TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',"
        " blocks_json TEXT NOT NULL DEFAULT '[]', model TEXT, msg_id TEXT, req_id TEXT,"
        " cwd TEXT, git_branch TEXT, is_sidechain INTEGER NOT NULL DEFAULT 0,"
        " source_tool_use_id TEXT, search_tool TEXT NOT NULL DEFAULT '',"
        " search_thinking TEXT NOT NULL DEFAULT '', search_aux TEXT NOT NULL DEFAULT '');"
    )
    c.execute(
        "INSERT INTO conversation_messages(session_id,source_path,byte_offset,"
        " timestamp_utc,entry_type,text,is_sidechain) VALUES('s3','c.jsonl',0,"
        " '2026-06-01T00:00:00Z','human','only prompt',0)")
    c.commit()
    assert cq._session_titles_map(c, ["s3"]).get("s3") == "only prompt"


# ---------------------------------------------------------------------------
# #193 Task 5: get_conversation returns `title` in BOTH return paths
# ---------------------------------------------------------------------------

def test_get_conversation_includes_title_ai():
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="Hello there", cwd="/home/u/proj")
    c.execute("INSERT INTO conversation_ai_titles VALUES('s1','AI Title','/p',9)")
    c.commit()
    d = cq.get_conversation(c, "s1")
    assert d["title"] == "AI Title"


def test_get_conversation_title_falls_back_to_first_prompt():
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="Hello there", cwd="/home/u/proj")   # no ai-title
    d = cq.get_conversation(c, "s1")
    assert d["title"] == "Hello there"


def test_get_conversation_title_on_empty_page():
    # an after-cursor past the end -> the early empty-page return must still carry title
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="Hello there", cwd="/home/u/proj")
    c.execute("INSERT INTO conversation_ai_titles VALUES('s1','AI Title','/p',9)")
    c.commit()
    d = cq.get_conversation(c, "s1", after="9999999")
    assert d["items"] == []                # empty/stale-cursor page
    assert d["title"] == "AI Title"        # title still stamped on the early return


def test_get_conversation_title_label_then_sid_fallback():
    # no ai-title AND no human first-prompt -> falls to project label, then sid.
    c = _conn()
    _msg(c, session_id="s9", uuid="a1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="only assistant", model=_MODEL, msg_id="m1", req_id="r1",
         cwd="/home/u/proj")
    d = cq.get_conversation(c, "s9")
    assert d["title"] == "proj"            # project label (no human prompt, no ai-title)


# ---------------------------------------------------------------------------
# #193 Task 6: subagent description harvest into subagent_meta
# ---------------------------------------------------------------------------

def test_subagent_meta_carries_spawn_description():
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Task",
                 "input": {"subagent_type": "general-purpose",
                           "description": "Code review Phase A"},
                 "id": "t1", "preview": "audit", "subagent_type": "general-purpose"}])
    _seed_tool_result(conn, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "done", "truncated": False,
                 "is_error": False, "tool_use_id": "t1",
                 "agent_id": "abc", "subagent_meta": {}}])
    d = cq.get_conversation(conn, "s1")
    meta = d["subagent_meta"]["abc"]
    assert meta["kind"] == "general-purpose"
    assert meta["description"] == "Code review Phase A"


def test_subagent_meta_no_description_when_absent():
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Task",
                 "input": {"subagent_type": "Explore"},   # no description
                 "id": "t1", "preview": "audit", "subagent_type": "Explore"}])
    _seed_tool_result(conn, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "done", "truncated": False,
                 "is_error": False, "tool_use_id": "t1",
                 "agent_id": "abc", "subagent_meta": {}}])
    d = cq.get_conversation(conn, "s1")
    assert "description" not in d["subagent_meta"]["abc"]


def test_subagent_meta_blank_description_dropped():
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Task",
                 "input": {"subagent_type": "Explore", "description": "   "},
                 "id": "t1", "preview": "audit", "subagent_type": "Explore"}])
    _seed_tool_result(conn, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "done", "truncated": False,
                 "is_error": False, "tool_use_id": "t1",
                 "agent_id": "abc", "subagent_meta": {}}])
    d = cq.get_conversation(conn, "s1")
    assert "description" not in d["subagent_meta"]["abc"]


def test_bash_description_not_harvested_as_subagent():
    # a Bash tool_use with input.description but NO subagent_type must not create
    # or pollute any subagent_meta entry.
    conn = _conn()
    _seed_assistant(conn, sid="s1", uuid="a1", msg_id="m1", req_id="r1",
        blocks=[{"kind": "tool_use", "name": "Bash",
                 "input": {"command": "ls -la", "description": "List files"},
                 "id": "t1", "preview": "ls -la"}])   # NO subagent_type
    _seed_tool_result(conn, sid="s1", uuid="u1",
        blocks=[{"kind": "tool_result", "text": "out", "truncated": False,
                 "is_error": False, "tool_use_id": "t1"}])
    d = cq.get_conversation(conn, "s1")
    # no subagent entry at all (no subagent_type), and certainly no description leak
    assert d["subagent_meta"] == {}


# ---------------------------------------------------------------------------
# Cache-failure detection kernel (_stamp_cache_failures). Pure-function units
# over synthetic ordered item lists (the shape _assemble_session produces just
# before the cache-failure stamp): each assistant item carries kind / subagent_key
# / model and a `tokens` dict {input, output, cache_creation, cache_read}. The
# rule (spec §1): per (subagent_key, model) key, with rm = running-max cache_read
# BEFORE the turn — flag iff rm>=20_000 and cc>=20_000 and cr<=0.5*rm and
# cc/(cc+cr)>=0.75; then running_max[key] = max(rm, cr). Reset a key at a
# compaction boundary; skip items lacking a `tokens` dict.
# ---------------------------------------------------------------------------
def _aturn(cc, cr, *, sk=None, model="claude-opus-4-8"):
    return {"kind": "assistant", "subagent_key": sk, "model": model,
            "tokens": {"input": 2, "output": 100, "cache_creation": cc, "cache_read": cr}}


def _compaction():
    """A compaction-boundary meta item, as _assemble_session emits it (#191)."""
    return {"kind": "meta", "subagent_key": None, "meta_kind": "compaction"}


def test_cache_failure_flags_clear_mid_session_loss():
    items = [_aturn(50_000, 0),        # first prime — must NOT flag (rm=0)
             _aturn(1_000, 70_000),    # healthy
             _aturn(222_890, 18_888)]  # collapse: rm=70k, cr<=0.5*rm, frac~0.92 -> FLAG
    cq._stamp_cache_failures(items)
    assert "cache_failure" not in items[0]
    assert "cache_failure" not in items[1]
    cf = items[2]["cache_failure"]
    assert cf["prev_cached"] == 70_000
    assert cf["tokens_recreated"] == min(222_890, 70_000 - 18_888)  # lost-prefix basis
    assert cf["est_wasted_usd"] > 0


def test_first_prime_never_flags():
    items = [_aturn(50_000, 0)]
    cq._stamp_cache_failures(items)
    assert "cache_failure" not in items[0]


def test_recreate_fraction_guard_rejects_low_fraction():
    # rm=100k, cr=40k (<=0.5*rm) but cc=20k -> frac=20/60=0.33 < 0.75 -> NO flag.
    # This is the non-vacuity guard test for the cc/(cc+cr)>=0.75 term: drop that
    # term and this turn flags (running-max collapse alone trips).
    items = [_aturn(1_000, 100_000), _aturn(20_000, 40_000)]
    cq._stamp_cache_failures(items)
    assert "cache_failure" not in items[1]


def test_healthy_turn_not_flagged():
    # cr ~ rm (ratio ~1.0): the running-max never collapses -> not a failure.
    items = [_aturn(1_000, 100_000), _aturn(2_000, 99_000)]
    cq._stamp_cache_failures(items)
    assert "cache_failure" not in items[1]


def test_collapse_boundary_cr_equals_half_rm_flags():
    # cr == 0.5*rm is the inclusive `<=` edge. rm=100k, cr=50k, cc=200k ->
    # frac=200/250=0.8 >= 0.75 -> FLAG (the boundary is on the failure side).
    items = [_aturn(1_000, 100_000), _aturn(200_000, 50_000)]
    cq._stamp_cache_failures(items)
    assert items[1]["cache_failure"]["prev_cached"] == 100_000


def test_collapse_boundary_just_above_half_rm_not_flagged():
    # cr just over 0.5*rm fails the collapse predicate -> not flagged (proves the
    # `<=` boundary is real, not a `<` that would also catch the equal case).
    items = [_aturn(1_000, 100_000), _aturn(200_000, 50_001)]
    cq._stamp_cache_failures(items)
    assert "cache_failure" not in items[1]


def test_model_switch_not_flagged():
    items = [_aturn(1_000, 100_000, model="claude-opus-4-8"),
             _aturn(60_000, 0, model="claude-haiku-4-5")]  # new model = fresh cache key
    cq._stamp_cache_failures(items)
    assert "cache_failure" not in items[1]


def test_subagent_thread_independent():
    items = [_aturn(1_000, 100_000, sk=None),
             _aturn(5_000, 2_000, sk="agent-1")]  # subagent's own small cache, not a main drop
    cq._stamp_cache_failures(items)
    assert "cache_failure" not in items[1]


def test_genuine_subagent_failure_flags_on_its_own_thread():
    # A subagent thread that primes a real cache then collapses flags on ITS key,
    # independent of the main thread's running-max.
    items = [_aturn(1_000, 100_000, sk=None),            # main healthy
             _aturn(1_000, 80_000, sk="agent-1"),        # subagent prime
             _aturn(120_000, 5_000, sk="agent-1")]       # subagent collapse -> FLAG
    cq._stamp_cache_failures(items)
    assert "cache_failure" not in items[0]
    assert "cache_failure" not in items[1]
    assert items[2]["cache_failure"]["prev_cached"] == 80_000


def test_floors_suppress_tiny_turns():
    items = [_aturn(1_000, 10_000), _aturn(15_000, 0)]  # rm=10k<20k floor AND cc<20k floor
    cq._stamp_cache_failures(items)
    assert "cache_failure" not in items[1]


def test_create_floor_suppresses_small_recreation():
    # rm is meaningful (100k), cr collapses to 0, BUT cc=15k < CREATE_FLOOR ->
    # not a substantial enough re-creation -> not flagged.
    items = [_aturn(1_000, 100_000), _aturn(15_000, 0)]
    cq._stamp_cache_failures(items)
    assert "cache_failure" not in items[1]


def test_total_loss_flags():
    items = [_aturn(1_000, 130_000), _aturn(134_000, 0)]  # cr=0, rm high, frac=1.0
    cq._stamp_cache_failures(items)
    assert items[1]["cache_failure"]["prev_cached"] == 130_000


def test_compaction_resets_running_max():
    # A compaction boundary clears the key's running-max, so the legitimate
    # post-compaction re-prime is NOT read as a loss.
    items = [_aturn(1_000, 130_000),   # primes rm=130k on (None, opus)
             _compaction(),            # compaction boundary -> reset
             _aturn(134_000, 0)]       # re-prime after compaction: rm reset -> NO flag
    cq._stamp_cache_failures(items)
    assert "cache_failure" not in items[2]


def test_multiple_failures_each_flag():
    items = [_aturn(1_000, 130_000),   # prime
             _aturn(140_000, 5_000),   # failure 1
             _aturn(2_000, 150_000),   # healthy re-prime (rm back up to 150k)
             _aturn(160_000, 1_000)]   # failure 2
    cq._stamp_cache_failures(items)
    assert "cache_failure" in items[1]
    assert "cache_failure" in items[3]
    assert items[1]["cache_failure"]["prev_cached"] == 130_000
    assert items[3]["cache_failure"]["prev_cached"] == 150_000


def test_failure_cr_does_not_lower_running_max():
    # After a failure (small cr), the running-max must stay at the pre-failure
    # high so a subsequent low-cr turn is still measured against the real prefix.
    items = [_aturn(1_000, 130_000),   # rm=130k
             _aturn(140_000, 5_000),   # failure; rm must STAY 130k (max(130k,5k))
             _aturn(135_000, 0)]       # measured against 130k, not 5k -> FLAG
    cq._stamp_cache_failures(items)
    assert items[2]["cache_failure"]["prev_cached"] == 130_000


def test_missing_tokens_skipped_and_does_not_move_max():
    items = [_aturn(1_000, 100_000),
             {"kind": "assistant", "subagent_key": None, "model": "claude-opus-4-8"},  # no tokens
             _aturn(150_000, 5_000)]  # rm must still be 100k from item[0]
    cq._stamp_cache_failures(items)
    assert "cache_failure" not in items[1]
    assert items[2]["cache_failure"]["prev_cached"] == 100_000


def test_wasted_cost_is_write_minus_read_on_lost_prefix():
    from _lib_pricing import _calculate_entry_cost
    items = [_aturn(1_000, 130_000), _aturn(140_000, 0)]
    cq._stamp_cache_failures(items)
    cf = items[1]["cache_failure"]
    lost = min(140_000, max(0, 130_000 - 0))   # 130_000
    assert cf["tokens_recreated"] == lost
    write = _calculate_entry_cost("claude-opus-4-8",
                                  {"cache_creation_input_tokens": lost})
    read = _calculate_entry_cost("claude-opus-4-8",
                                 {"cache_read_input_tokens": lost})
    assert abs(cf["est_wasted_usd"] - (write - read)) < 1e-12
    assert cf["est_wasted_usd"] > 0


def test_unknown_model_wasted_cost_zero(capsys):
    # An unrecognized model resolves to $0 pricing on both legs, so the marginal
    # waste is exactly 0. The one-shot `[cost] unknown model` stderr warning is
    # expected (and captured here, not suppressed).
    items = [_aturn(1_000, 130_000, model="totally-unknown-model"),
             _aturn(140_000, 0, model="totally-unknown-model")]
    cq._stamp_cache_failures(items)
    cf = items[1]["cache_failure"]
    assert cf["tokens_recreated"] == 130_000     # still flagged (token rule is price-free)
    assert cf["est_wasted_usd"] == 0.0
    capsys.readouterr()   # drain the expected one-shot warning


def test_get_conversation_passes_cache_failure_through_on_failing_turn():
    # Task 2: get_conversation items are a pass-through, so the cache_failure flag
    # stamped by _assemble_session rides along on EXACTLY the failing turn. Seed a
    # healthy prime turn (high cache_read) then a collapse turn through the REAL
    # assembly path (conversation_messages + session_entries).
    c = _conn()
    # prime: healthy turn, big cache_read -> establishes running-max
    _msg(c, session_id="cf1", uuid="a1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="primed", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([{"kind": "text", "text": "primed"}]))
    _entry(c, source_path="a.jsonl", line_offset=0, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20, cc=1_000, cr=130_000)
    # collapse: cache_read drops to 0, cache_creation balloons -> FAILURE
    _msg(c, session_id="cf1", uuid="a2", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="rebuilt", model=_MODEL, msg_id="m2", req_id="r2",
         blocks_json=_json.dumps([{"kind": "text", "text": "rebuilt"}]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m2", req_id="r2", inp=10, out=20, cc=134_000, cr=0)
    out = cq.get_conversation(c, "cf1", after=None, limit=500)
    items = out["items"]
    prime = next(it for it in items if it["anchor"]["uuid"] == "a1")
    fail = next(it for it in items if it["anchor"]["uuid"] == "a2")
    assert "cache_failure" not in prime            # healthy turn: absent (not zero)
    cf = fail["cache_failure"]
    assert cf["prev_cached"] == 130_000
    assert cf["tokens_recreated"] == min(134_000, 130_000 - 0)
    assert cf["est_wasted_usd"] > 0


# ---------------------------------------------------------------------------
# U1 (#217 S1): the lightweight rebuild-count path off the flock-held rollup.
# `session_cache_rebuild_count` must build an ordered event stream from a narrow
# query (no block-body parse / fold / meta-classify / subagent correlation) and
# feed it to the SAME pure cache-failure predicate the full `_assemble_session`
# path uses — yielding a byte-identical count. The parity fixtures exercise the
# event-stream normalization edges that distinguish the light path from a naive
# assistant-row SELECT: compaction resets, duplicate UUIDs, non-consecutive
# fragments, a model switch, and multi-subagent.
# ---------------------------------------------------------------------------
def _full_assembly_rebuild_count(conn, session_id):
    """The full-assembly cache-failure count: assemble the whole session through
    the reader pipeline, re-stamp, and count flagged items. This is the
    known-good the lightweight path must match (it is the pre-U1 behavior of
    `session_cache_rebuild_count`, computed inline so the parity test never
    depends on the production function under test)."""
    asm = cq._assemble_session(conn, session_id)
    if asm is None:
        return 0
    items = asm["items"]
    cq._stamp_cache_failures(items)
    return sum(1 for it in items if "cache_failure" in it)


def _seed_compaction_row(conn, *, sid, uuid, ts, source_path="a.jsonl",
                         byte_offset=99):
    """A compaction-boundary row as the parser emits it: an entry_type='meta'
    row whose all-text body opens with the compaction sentinel. The assembly
    path classifies it to meta_kind='compaction' and the cache-failure rule
    resets its running-max on it."""
    body = ("This session is being continued from a previous conversation "
            "that ran out of context. The summary follows.")
    _msg(conn, session_id=sid, uuid=uuid, source_path=source_path,
         byte_offset=byte_offset, timestamp_utc=ts, entry_type="meta",
         text="", blocks_json=_json.dumps([{"kind": "text", "text": body}]))


def _seed_rebuild_parity_session(conn):
    """A single session exercising every event-stream edge in one transcript,
    each edge engineered to be LOAD-BEARING for the flag count (so removing the
    corresponding normalization in the lightweight builder flips the count — the
    non-vacuity guard the parity test relies on):

    FLAG #1 (MAIN, compaction-gated): a healthy prime (rm=130k) then a collapse —
      but a compaction boundary sits BETWEEN them, so the collapse is measured
      against the RESET running-max → it must NOT flag. A SECOND prime+collapse
      AFTER the compaction is the real flag. (Drop compaction-reset detection and
      the first collapse spuriously flags too → over-count.)

    DEDUP edge: a duplicate-UUID replay of a turn under a DIFFERENT (msg_id,
      req_id) — the realistic "same logical row re-emitted with a fresh request
      envelope" replay. The replay's tokens are collapse-shaped on the main key.
      With UUID dedup it is dropped (one logical row) → no extra flag; WITHOUT
      dedup it becomes a distinct collapse event → over-count.

    MODEL-PROMOTION edge (FLAG #2): a healthy prime on model HAIKU, then a
      two-fragment turn whose SEED fragment is model OPUS (no prior cache) and
      whose PROSE fragment promotes the turn to model HAIKU. The turn's tokens
      collapse against HAIKU's running-max. With first-prose promotion the event
      keys on (None, HAIKU) → collapses → FLAG; WITHOUT promotion it keys on
      (None, OPUS) which has no prior rm → no flag → under-count.

    SUBAGENT edge (FLAG #3): a subagent thread (agent-<hash>.jsonl) that primes
      then collapses on ITS OWN (agent-hash, OPUS) key. The MAIN OPUS key has NO
      running-max at that point (the main thread only ever primed HAIKU), so
      merging the subagent into the main key (subagent_key dropped → (None, OPUS))
      leaves no prior rm → no flag → under-count. With the subagent_key the
      collapse is measured against the subagent's own prime → FLAG.
    """
    sid = "rb1"
    a = "a.jsonl"
    OPUS, HAIKU, SONNET = _MODEL, "claude-haiku-4-5", "claude-sonnet-4-5"
    # === compaction-gated main flag (#1) ===
    _msg(conn, session_id=sid, uuid="m_prime1", source_path=a, byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="prime1", model=OPUS, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([{"kind": "text", "text": "prime1"}]))
    _entry(conn, source_path=a, line_offset=0, model=OPUS,
           msg_id="m1", req_id="r1", inp=10, out=20, cc=1_000, cr=130_000)
    # compaction BETWEEN the prime and the collapse → resets (None, OPUS) rm.
    _seed_compaction_row(conn, sid=sid, uuid="cmp", ts="2026-06-01T00:00:01Z")
    # collapse-shaped turn right after compaction: rm reset → NOT a flag.
    _msg(conn, session_id=sid, uuid="m_postcmp", source_path=a, byte_offset=1,
         timestamp_utc="2026-06-01T00:00:02Z", entry_type="assistant",
         text="postcmp", model=OPUS, msg_id="m2", req_id="r2",
         blocks_json=_json.dumps([{"kind": "text", "text": "postcmp"}]))
    _entry(conn, source_path=a, line_offset=1, model=OPUS,
           msg_id="m2", req_id="r2", inp=10, out=20, cc=134_000, cr=0)
    # genuine post-compaction prime then collapse on (None, OPUS) → FLAG #1.
    _msg(conn, session_id=sid, uuid="m_prime2", source_path=a, byte_offset=2,
         timestamp_utc="2026-06-01T00:00:03Z", entry_type="assistant",
         text="prime2", model=OPUS, msg_id="m3", req_id="r3",
         blocks_json=_json.dumps([{"kind": "text", "text": "prime2"}]))
    _entry(conn, source_path=a, line_offset=2, model=OPUS,
           msg_id="m3", req_id="r3", inp=10, out=20, cc=1_000, cr=120_000)
    _msg(conn, session_id=sid, uuid="m_collapse", source_path=a, byte_offset=3,
         timestamp_utc="2026-06-01T00:00:04Z", entry_type="assistant",
         text="collapse", model=OPUS, msg_id="m4", req_id="r4",
         blocks_json=_json.dumps([{"kind": "text", "text": "collapse"}]))
    _entry(conn, source_path=a, line_offset=3, model=OPUS,
           msg_id="m4", req_id="r4", inp=10, out=20, cc=130_000, cr=5_000)
    # === DEDUP edge: duplicate-UUID replay under a fresh (msg_id, req_id) ===
    # Same uuid as the real collapse, but a new request envelope. Collapse-shaped
    # on (None, OPUS). UUID dedup drops it (first occurrence wins); without dedup
    # it is a second collapse event → over-count.
    _msg(conn, session_id=sid, uuid="m_collapse", source_path="dup.jsonl",
         byte_offset=0, timestamp_utc="2026-06-01T00:00:05Z",
         entry_type="assistant", text="collapse", model=OPUS,
         msg_id="m4b", req_id="r4b",
         blocks_json=_json.dumps([{"kind": "text", "text": "collapse"}]))
    _entry(conn, source_path="dup.jsonl", line_offset=0, model=OPUS,
           msg_id="m4b", req_id="r4b", inp=10, out=20, cc=130_000, cr=5_000)
    # === MODEL-PROMOTION edge (FLAG #2) ===
    # Prime a HAIKU cache on the main thread.
    _msg(conn, session_id=sid, uuid="m_haiku_prime", source_path=a, byte_offset=4,
         timestamp_utc="2026-06-01T00:00:06Z", entry_type="assistant",
         text="haiku prime", model=HAIKU, msg_id="m5", req_id="r5",
         blocks_json=_json.dumps([{"kind": "text", "text": "haiku prime"}]))
    _entry(conn, source_path=a, line_offset=4, model=HAIKU,
           msg_id="m5", req_id="r5", inp=10, out=20, cc=1_000, cr=140_000)
    # Two-fragment turn: SEED fragment model SONNET (no prose, and SONNET is
    # never primed anywhere), interleaved tool_result, then PROSE fragment model
    # HAIKU. The prose fragment promotes the turn to HAIKU, so the collapse keys
    # on (None, HAIKU) → FLAG (HAIKU has the 140k prime above). Without promotion
    # it keys on (None, SONNET) which has NO prior running-max → no flag. (SONNET,
    # not OPUS, precisely because OPUS retains a 120k running-max here — a fresh
    # model is what makes this edge load-bearing.)
    _msg(conn, session_id=sid, uuid="m_frag1", source_path=a, byte_offset=5,
         timestamp_utc="2026-06-01T00:00:07Z", entry_type="assistant",
         text="", model=SONNET, msg_id="m6", req_id="r6",
         blocks_json=_json.dumps([{"kind": "tool_use", "id": "tu6",
                                   "name": "Read", "input_summary": "{}"}]))
    _seed_tool_result(conn, sid=sid, uuid="tr6",
                      ts="2026-06-01T00:00:08Z",
                      blocks=[{"kind": "tool_result", "tool_use_id": "tu6",
                               "text": "body", "truncated": False,
                               "is_error": False}])
    _msg(conn, session_id=sid, uuid="m_frag2", source_path=a, byte_offset=6,
         timestamp_utc="2026-06-01T00:00:09Z", entry_type="assistant",
         text="frag2 prose", model=HAIKU, msg_id="m6", req_id="r6",
         blocks_json=_json.dumps([{"kind": "text", "text": "frag2 prose"}]))
    _entry(conn, source_path=a, line_offset=6, model=HAIKU,
           msg_id="m6", req_id="r6", inp=10, out=20, cc=140_000, cr=5_000)
    # === SUBAGENT edge (FLAG #3 + the non-vacuity over-count guard) ===
    # The subagent thread primes (120k) then collapses on (agent-hash, SONNET) →
    # FLAG #3 on its OWN key. Crucially, a MAIN-thread SONNET turn is interleaved
    # BETWEEN the subagent's prime and collapse, collapse-shaped (cc=140k, cr=0).
    # On the main (None, SONNET) key that turn has NO prior running-max → no flag.
    # But if the subagent_key is dropped (the subagent merges into (None, SONNET)),
    # the subagent's 120k prime becomes the main turn's running-max → it
    # SPURIOUSLY flags → over-count. So subagent_key is load-bearing: with it the
    # count is 3; without it the count is 4.
    ag = "agent-deadbeef.jsonl"
    _msg(conn, session_id=sid, uuid="sa_prime", source_path=ag, byte_offset=0,
         timestamp_utc="2026-06-01T00:00:10Z", entry_type="assistant",
         text="sa prime", model=SONNET, msg_id="m7", req_id="r7", is_sidechain=1,
         blocks_json=_json.dumps([{"kind": "text", "text": "sa prime"}]))
    _entry(conn, source_path=ag, line_offset=0, model=SONNET,
           msg_id="m7", req_id="r7", inp=10, out=20, cc=1_000, cr=120_000)
    # interleaved MAIN-thread SONNET turn (no prior main-SONNET cache), ordered
    # strictly BETWEEN the subagent prime (:10) and collapse (:12).
    _msg(conn, session_id=sid, uuid="m_sonnet", source_path=a, byte_offset=7,
         timestamp_utc="2026-06-01T00:00:11Z", entry_type="assistant",
         text="main sonnet", model=SONNET, msg_id="m9", req_id="r9",
         blocks_json=_json.dumps([{"kind": "text", "text": "main sonnet"}]))
    _entry(conn, source_path=a, line_offset=7, model=SONNET,
           msg_id="m9", req_id="r9", inp=10, out=20, cc=140_000, cr=0)
    _msg(conn, session_id=sid, uuid="sa_collapse", source_path=ag, byte_offset=1,
         timestamp_utc="2026-06-01T00:00:12Z", entry_type="assistant",
         text="sa collapse", model=SONNET, msg_id="m8", req_id="r8", is_sidechain=1,
         blocks_json=_json.dumps([{"kind": "text", "text": "sa collapse"}]))
    _entry(conn, source_path=ag, line_offset=1, model=SONNET,
           msg_id="m8", req_id="r8", inp=10, out=20, cc=130_000, cr=4_000)
    return sid


def test_session_cache_rebuild_count_lightweight_matches_full_assembly():
    c = _conn()
    sid = _seed_rebuild_parity_session(c)
    full = _full_assembly_rebuild_count(c, sid)
    # Exactly three real collapses flag (compaction-gated main #1, model-promoted
    # #2, subagent #3); the pre-compaction collapse, the duplicate-UUID replay,
    # and every prime are non-flags. Each flag is load-bearing on one
    # normalization edge (see _seed_rebuild_parity_session) so the parity is
    # genuinely non-vacuous.
    assert full == 3, f"fixture sanity: expected 3 flags, got {full}"
    light = cq.session_cache_rebuild_count(c, sid)
    assert light == full
    # The lightweight path also runs through the SAME pure predicate over its
    # event stream (single source of truth) — assert the builder + predicate
    # compose to the same count directly, so a regression in either half shows.
    events = cq._lightweight_rebuild_events(c, sid)
    assert cq._cache_failure_count_over_events(events) == full


def test_lightweight_rebuild_count_unknown_session_is_zero():
    c = _conn()
    assert cq.session_cache_rebuild_count(c, "nope") == 0
    assert cq._cache_failure_count_over_events(
        cq._lightweight_rebuild_events(c, "nope")) == 0


def test_rollup_fill_does_not_call_assemble_session(monkeypatch):
    """The flock-held rollup-fill path must NOT run a full `_assemble_session`
    per session — U1's headline win. Spy the function and assert zero calls when
    `_fill_conversation_sessions_filter_columns` recomputes the rebuild count."""
    import _cctally_cache as _cc
    c = _conn()
    _seed_rebuild_parity_session(c)
    # Materialize the structural rollup columns first (the INSERT pass), then
    # spy assembly across the filter-column fill (which calls the rebuild count).
    calls = []
    real_assemble = cq._assemble_session
    monkeypatch.setattr(
        cq, "_assemble_session",
        lambda *a, **k: calls.append(1) or real_assemble(*a, **k))
    _cc._recompute_conversation_sessions(c)
    assert calls == [], "rollup fill must not call _assemble_session (U1)"
    # And the stored count still equals the full-assembly truth.
    stored = c.execute(
        "SELECT cache_rebuild_count FROM conversation_sessions "
        "WHERE session_id='rb1'").fetchone()[0]
    assert stored == _full_assembly_rebuild_count(c, "rb1") == 3
