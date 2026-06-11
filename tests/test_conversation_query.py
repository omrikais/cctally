import sqlite3, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db
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


def _msg(c, **kw):
    # The #177 enrichment columns (stop_reason / attribution_skill /
    # attribution_plugin / search_aux) are TAIL-APPENDED, matching the
    # production INSERT tuple. search_aux defaults to '' (the NOT NULL DEFAULT).
    cols = ("session_id", "uuid", "parent_uuid", "source_path", "byte_offset",
            "timestamp_utc", "entry_type", "text", "blocks_json", "model",
            "msg_id", "req_id", "cwd", "git_branch", "is_sidechain",
            "source_tool_use_id", "stop_reason", "attribution_skill",
            "attribution_plugin")
    row = {k: kw.get(k) for k in cols}
    row["blocks_json"] = kw.get("blocks_json", "[]")
    row["text"] = kw.get("text", "")
    row["is_sidechain"] = kw.get("is_sidechain", 0)
    row["search_aux"] = kw.get("search_aux", "")
    c.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,is_sidechain,"
        " source_tool_use_id,stop_reason,attribution_skill,attribution_plugin,search_aux)"
        " VALUES(:session_id,:uuid,:parent_uuid,:source_path,:byte_offset,"
        ":timestamp_utc,:entry_type,:text,:blocks_json,:model,:msg_id,:req_id,"
        ":cwd,:git_branch,:is_sidechain,:source_tool_use_id,:stop_reason,"
        ":attribution_skill,:attribution_plugin,:search_aux)", row)


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
    out = cq.list_conversations(c, sort="recent", limit=50, offset=0)
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
    page = cq.list_conversations(c, sort="recent", limit=2, offset=0)
    assert len(page["conversations"]) == 2
    assert page["page"]["has_more"] is True
    assert page["page"]["next_offset"] == 2
    last = cq.list_conversations(c, sort="recent", limit=2, offset=4)
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
    seen = {}
    real = cq._fts_snippets
    def spy(conn, fts_q, ids):
        seen["ids"] = list(ids)
        return real(conn, fts_q, ids)
    monkeypatch.setattr(cq, "_fts_snippets", spy)
    out = cq.search_conversations(c, "beta", limit=3, offset=0)
    assert out["mode"] == "fts"
    assert len(out["hits"]) == 3 and out["total"] == 8
    assert len(seen["ids"]) == 3     # snippet batch covers only the page


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
    rows = cq.list_conversations(c)["conversations"]
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
    assert out["subagent_meta"] == {"aaaa1111": {"kind": "Explore"}}
    entry = out["subagent_meta"]["aaaa1111"]
    assert "total_tokens" not in entry and "total_duration_ms" not in entry
    assert "total_tool_use_count" not in entry and "status" not in entry


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
    assert out["subagent_meta"] == {"aaaa1111": {
        "kind": "Explore", "total_tokens": 23285, "total_duration_ms": 10668,
        "total_tool_use_count": 1, "status": "completed"}}
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
    title = cq.list_conversations(c)["conversations"][0]["title"]
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
    title = cq.list_conversations(c)["conversations"][0]["title"]
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
