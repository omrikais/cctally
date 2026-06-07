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
    cols = ("session_id", "uuid", "parent_uuid", "source_path", "byte_offset",
            "timestamp_utc", "entry_type", "text", "blocks_json", "model",
            "msg_id", "req_id", "cwd", "git_branch", "is_sidechain")
    row = {k: kw.get(k) for k in cols}
    row["blocks_json"] = kw.get("blocks_json", "[]")
    row["text"] = kw.get("text", "")
    row["is_sidechain"] = kw.get("is_sidechain", 0)
    c.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,is_sidechain)"
        " VALUES(:session_id,:uuid,:parent_uuid,:source_path,:byte_offset,"
        ":timestamp_utc,:entry_type,:text,:blocks_json,:model,:msg_id,:req_id,"
        ":cwd,:git_branch,:is_sidechain)", row)


def _entry(c, *, source_path, line_offset, model, msg_id, req_id,
           inp=0, out=0, cc=0, cr=0):
    c.execute(
        "INSERT OR IGNORE INTO session_entries "
        "(source_path,line_offset,timestamp_utc,model,msg_id,req_id,"
        " input_tokens,output_tokens,cache_create_tokens,cache_read_tokens)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (source_path, line_offset, "t", model, msg_id, req_id, inp, out, cc, cr))


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
    # C1 regression: a tool-using turn interleaves a tool_result (user) line
    # BETWEEN two assistant fragments sharing the SAME (msg_id, req_id). The
    # turn must coalesce to exactly ONE assistant item (grouping over the whole
    # logical list, NOT by adjacency), the tool_result must be its own item, and
    # cost must be counted ONCE — proven with MULTIPLE turns so the cardinality
    # invariant sum(assistant item cost) == header cost is non-vacuous.
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="do a thing",
         cwd="/home/u/proj", git_branch="main")
    # turn 1 (m1,r1) fragment A: thinking + tool_use (no prose)
    _msg(c, session_id="s1", uuid="t1a", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant", text="",
         blocks_json=_json.dumps([{"kind": "thinking", "text": "plan"},
                                  {"kind": "tool_use", "name": "Bash"}]),
         model=_MODEL, msg_id="m1", req_id="r1")
    # tool_result (user) BREAKS the adjacency run within turn 1
    _msg(c, session_id="s1", uuid="tr1", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-01T00:00:02Z", entry_type="tool_result",
         text="command output",
         blocks_json=_json.dumps([{"kind": "tool_result", "text": "out"}]))
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
    # human, turn-1 (coalesced), tool_result, turn-2 — the tool_result sits
    # AFTER the coalesced turn-1 item (positioned at its first fragment).
    assert [it["kind"] for it in items] == \
           ["human", "assistant", "tool_result", "assistant"]
    turn1 = items[1]
    # exactly ONE assistant item for the interleaved turn, with ALL fragment uuids
    assert set(turn1["member_uuids"]) == {"t1a", "t1b"}
    assert turn1["text"] == "done"                 # prose-bearing fragment
    assert turn1["anchor"]["uuid"] == "t1b"
    # the in-between tool_result is its own separate item
    assert items[2]["kind"] == "tool_result"
    assert items[2]["anchor"]["uuid"] == "tr1"
    assert items[2]["member_uuids"] == ["tr1"]
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
