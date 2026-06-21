"""Kernel tests for ``get_conversation_outline`` (#177 S5, spec §1 / §8).

The outline shares ``get_conversation``'s assembly pass (``_assemble_session``),
so the contract under test is dual: (a) every outline turn corresponds 1:1 with
a reader item (same anchor uuid + member_uuids, same order), and (b) the
session-level stats (turn counts, tool counts, error count, models, duration,
tokens, cost) derive from that SAME assembled item list — never a parallel
aggregation (Codex F8). Standalone-by-convention: the ``_conn``/``_msg``/
``_entry`` helpers are copied verbatim from ``test_conversation_query.py``.
"""
import sqlite3, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db
import _lib_conversation_query as cq
import json as _json
import time

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
    # attribution_plugin) are TAIL-APPENDED, matching the production INSERT
    # tuple. (#217 S1 / U7a: the documented-dead search_aux column was dropped
    # from the live schema by migration 016, so it is no longer inserted here.)
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


# ---------------------------------------------------------------------------
# A rich session reused across several tests: human; an assistant turn split
# across two fragments (same msg_id/req_id) carrying a thinking block, a text
# block, a Bash tool_use t1, and (second fragment) an AskUserQuestion tool_use
# t2; an interleaved errored tool_result for t1; a sidechain assistant row
# whose parent_uuid points at a main turn member uuid; an orphan errored
# tool_result; and a (m1,r1) session_entries cost row.
# ---------------------------------------------------------------------------
def _seed_rich(c, sid="s5"):
    _msg(c, session_id=sid, uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human",
         text="please fix the race\nsecond line ignored")
    # assistant turn (m1,r1) fragment A: thinking + text + Bash tool_use t1.
    _msg(c, session_id=sid, uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-12T14:00:05Z", entry_type="assistant",
         text="here is the plan", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             {"kind": "thinking", "text": "the race is in stdin\nmore reasoning"},
             {"kind": "text", "text": "here is the plan"},
             {"kind": "tool_use", "name": "Bash", "input_summary": "{}",
              "id": "t1", "preview": "make test"}]))
    # interleaved tool_result for t1, errored -> folds into the turn's tool_call.
    _msg(c, session_id=sid, uuid="tr1", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-12T14:00:07Z", entry_type="tool_result", text="",
         blocks_json=_json.dumps([
             {"kind": "tool_result", "text": "boom", "truncated": False,
              "is_error": True, "tool_use_id": "t1"}]))
    # assistant turn (m1,r1) fragment B: AskUserQuestion tool_use t2 (no result).
    _msg(c, session_id=sid, uuid="a2", source_path="a.jsonl", byte_offset=3,
         timestamp_utc="2026-06-12T14:00:09Z", entry_type="assistant",
         text="", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             {"kind": "tool_use", "name": "AskUserQuestion", "input_summary": "{}",
              "id": "t2", "preview": "which?"}]))
    # sidechain assistant row (separate agent file); parent_uuid -> a turn member.
    _msg(c, session_id=sid, uuid="sc1", parent_uuid="a1",
         source_path="/agents/agent-abc.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:12Z", entry_type="assistant",
         text="subagent reply", model=_MODEL, msg_id="m2", req_id="r2",
         is_sidechain=1)
    # orphan errored tool_result (no matching tool_use in session -> standalone).
    _msg(c, session_id=sid, uuid="orph1", source_path="a.jsonl", byte_offset=4,
         timestamp_utc="2026-06-12T14:00:15Z", entry_type="tool_result", text="",
         blocks_json=_json.dumps([
             {"kind": "tool_result", "text": "stale", "truncated": False,
              "is_error": True, "tool_use_id": "ghost"}]))
    # cost row for the main (m1,r1) turn.
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=1200, out=4800, cc=0, cr=310000)
    # cost row for the sidechain (m2,r2) turn (tokens but trivial counts).
    _entry(c, source_path="/agents/agent-abc.jsonl", line_offset=0, model=_MODEL,
           msg_id="m2", req_id="r2", inp=10, out=20, cc=0, cr=0)


def test_outline_unknown_session_is_none():
    c = _conn()
    assert cq.get_conversation_outline(c, "nope") is None


def test_outline_turns_match_reader_one_to_one():
    c = _conn()
    _seed_rich(c)
    outline = cq.get_conversation_outline(c, "s5")
    detail = cq.get_conversation(c, "s5", limit=1000)
    # Anchor uuids + member_uuids match the reader items 1:1, in order.
    assert [t["uuid"] for t in outline["turns"]] == \
        [it["anchor"]["uuid"] for it in detail["items"]]
    assert [t["member_uuids"] for t in outline["turns"]] == \
        [it["member_uuids"] for it in detail["items"]]
    # Cost + subagent_meta parity with the detail response.
    assert outline["stats"]["cost_usd"] == detail["cost_usd"]
    assert outline["subagent_meta"] == detail["subagent_meta"]


def test_outline_turn_fields_and_caps():
    c = _conn()
    _seed_rich(c)
    turns = cq.get_conversation_outline(c, "s5")["turns"]
    by_uuid = {t["uuid"]: t for t in turns}

    human = next(t for t in turns if t["kind"] == "human")
    assert human["label"] == "please fix the race"   # first line only

    # The assistant turn anchors on its prose fragment (a1).
    asst = by_uuid["a1"]
    assert asst["kind"] == "assistant"
    assert asst["thinking"] == ["the race is in stdin"]   # first line, capped
    assert {"name": "Bash", "is_error": True} in asst["tools"]
    assert {"name": "AskUserQuestion", "is_error": False} in asst["tools"]
    assert asst["model"] == _MODEL
    assert asst["tokens"] == {"input": 1200, "output": 4800,
                              "cache_creation": 0, "cache_read": 310000}

    orph = by_uuid["orph1"]
    assert orph["tools"] == [{"name": None, "is_error": True}]

    sc = by_uuid["sc1"]
    assert sc["subagent_key"] is not None
    assert sc["parent_uuid"] == "a1"
    assert sc["is_sidechain"] is True


def test_outline_label_cap_120():
    c = _conn()
    _msg(c, session_id="cap", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human",
         text="x" * 500)
    turns = cq.get_conversation_outline(c, "cap")["turns"]
    label = next(t for t in turns if t["kind"] == "human")["label"]
    assert len(label) == 120


def test_outline_stats():
    c = _conn()
    _seed_rich(c)
    outline = cq.get_conversation_outline(c, "s5")
    detail = cq.get_conversation(c, "s5", limit=1000)
    stats = outline["stats"]
    n = len(outline["turns"])

    # Turn kind counts add up over all grouped turns.
    tc = stats["turns"]
    assert tc["total"] == n
    assert tc["total"] == tc["human"] + tc["assistant"] + tc["tool_result"] + tc["meta"]
    asst_turns = [t for t in outline["turns"] if t["kind"] == "assistant"]
    assert tc["assistant"] == len(asst_turns)

    # Tool counts: one Bash, one AskUserQuestion (orphan tool_result name is None
    # so it does not contribute to the histogram).
    assert stats["tool_counts"] == {"Bash": 1, "AskUserQuestion": 1}

    # Errors: the folded Bash error + the orphan errored tool_result.
    assert stats["error_count"] == 2

    # Models: every assistant turn carries _MODEL.
    assert stats["models"] == {_MODEL: len(asst_turns)}

    # Duration: last ts − first ts of the session (15s span: 14:00:00 → 14:00:15).
    assert stats["duration_seconds"] == 15

    # Token sums over assistant turns include the big cache_read.
    assert stats["tokens"]["cache_read"] == 310000
    assert stats["cost_usd"] == detail["cost_usd"]
    assert stats["cost_usd"] > 0


def test_outline_null_ts_tolerated():
    c = _conn()
    _msg(c, session_id="nt", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc=None, entry_type="human", text="no timestamp here")
    outline = cq.get_conversation_outline(c, "nt")
    assert outline["turns"][0]["ts"] is None
    assert outline["stats"]["duration_seconds"] is None


def test_outline_thousand_turn_session():
    c = _conn()
    for i in range(1000):
        ts = "2026-06-12T%02d:%02d:00Z" % (i // 60, i % 60)
        if i % 2 == 0:
            _msg(c, session_id="big", uuid="h%d" % i, source_path="a.jsonl",
                 byte_offset=i, timestamp_utc=ts, entry_type="human",
                 text="prompt %d" % i)
        else:
            _msg(c, session_id="big", uuid="a%d" % i, source_path="a.jsonl",
                 byte_offset=i, timestamp_utc=ts, entry_type="assistant",
                 text="reply %d" % i, model=_MODEL,
                 msg_id="m%d" % i, req_id="r%d" % i,
                 blocks_json=_json.dumps([{"kind": "text", "text": "reply %d" % i}]))
    t0 = time.monotonic()
    outline = cq.get_conversation_outline(c, "big")
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, elapsed
    assert len(outline["turns"]) == 1000
    detail = cq.get_conversation(c, "big", limit=1000)
    assert [t["uuid"] for t in outline["turns"]] == \
        [it["anchor"]["uuid"] for it in detail["items"]]


def test_outline_counts_recovered_compaction_as_meta_not_human():
    # #191: a stale-ingested compaction row (entry_type='human', text=the body)
    # is recovered to kind='meta' in the shared _assemble_session pass, so the
    # outline's stats.turns counts it as meta — NEVER human. (Spec Testing item;
    # the behavior follows from the shared assembly, this pins it literally.)
    c = _conn()
    body = ("This session is being continued from a previous conversation that "
            "ran out of context.")
    _msg(c, session_id="s191", uuid="c1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text=body,
         blocks_json=_json.dumps([{"kind": "text", "text": body}]))
    out = cq.get_conversation_outline(c, "s191")
    assert out["stats"]["turns"]["meta"] == 1
    assert out["stats"]["turns"]["human"] == 0
    assert out["turns"][0]["kind"] == "meta"
    assert out["turns"][0]["meta_kind"] == "compaction"


# ---------------------------------------------------------------------------
# Task 2: cache-failure flag copied onto OutlineTurn + stats.cache_failures
# aggregate. A healthy prime turn + a collapse turn through the shared assembly.
# ---------------------------------------------------------------------------
def _seed_cache_failure(c, sid="cfo"):
    # prime: healthy turn establishing a high running-max cache_read.
    _msg(c, session_id=sid, uuid="a1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="primed", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([{"kind": "text", "text": "primed"}]))
    _entry(c, source_path="a.jsonl", line_offset=0, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20, cc=1_000, cr=130_000)
    # collapse: cache_read -> 0, cache_creation balloons -> a cache failure.
    _msg(c, session_id=sid, uuid="a2", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="rebuilt", model=_MODEL, msg_id="m2", req_id="r2",
         blocks_json=_json.dumps([{"kind": "text", "text": "rebuilt"}]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m2", req_id="r2", inp=10, out=20, cc=134_000, cr=0)


def test_outline_copies_cache_failure_onto_failing_turn():
    c = _conn()
    _seed_cache_failure(c)
    out = cq.get_conversation_outline(c, "cfo")
    by = {t["uuid"]: t for t in out["turns"]}
    assert "cache_failure" not in by["a1"]            # healthy: absent (not zero)
    cf = by["a2"]["cache_failure"]
    assert cf["prev_cached"] == 130_000
    assert cf["tokens_recreated"] == 130_000
    assert cf["est_wasted_usd"] > 0


def test_outline_stats_cache_failures_aggregate():
    c = _conn()
    _seed_cache_failure(c)
    out = cq.get_conversation_outline(c, "cfo")
    detail = cq.get_conversation(c, "cfo", limit=1000)
    fail = next(it for it in detail["items"] if it["anchor"]["uuid"] == "a2")
    cf = fail["cache_failure"]
    agg = out["stats"]["cache_failures"]
    assert agg["count"] == 1
    assert agg["tokens_recreated"] == cf["tokens_recreated"]
    assert abs(agg["est_wasted_usd"] - cf["est_wasted_usd"]) < 1e-12


def test_outline_stats_cache_failures_absent_when_none():
    # A clean session (no failures) must NOT carry a cache_failures aggregate at
    # all (mirrors the per-turn "absent, not zero" convention; ~65% of sessions
    # have zero, so a perpetual zero row would be clutter).
    c = _conn()
    _seed_rich(c)
    out = cq.get_conversation_outline(c, "s5")
    assert "cache_failures" not in out["stats"]


def test_outline_stats_cache_failures_multiple():
    # Two independent failures sum into the aggregate count + tokens + usd.
    c = _conn()
    # prime
    _msg(c, session_id="m2f", uuid="a0", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="p", model=_MODEL, msg_id="m0", req_id="r0",
         blocks_json=_json.dumps([{"kind": "text", "text": "p"}]))
    _entry(c, source_path="a.jsonl", line_offset=0, model=_MODEL,
           msg_id="m0", req_id="r0", inp=10, out=20, cc=1_000, cr=130_000)
    # failure 1
    _msg(c, session_id="m2f", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="f1", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([{"kind": "text", "text": "f1"}]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20, cc=140_000, cr=5_000)
    # healthy re-prime (rm back up to 150k)
    _msg(c, session_id="m2f", uuid="a2", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-01T00:00:10Z", entry_type="assistant",
         text="rp", model=_MODEL, msg_id="m2", req_id="r2",
         blocks_json=_json.dumps([{"kind": "text", "text": "rp"}]))
    _entry(c, source_path="a.jsonl", line_offset=2, model=_MODEL,
           msg_id="m2", req_id="r2", inp=10, out=20, cc=2_000, cr=150_000)
    # failure 2
    _msg(c, session_id="m2f", uuid="a3", source_path="a.jsonl", byte_offset=3,
         timestamp_utc="2026-06-01T00:00:15Z", entry_type="assistant",
         text="f2", model=_MODEL, msg_id="m3", req_id="r3",
         blocks_json=_json.dumps([{"kind": "text", "text": "f2"}]))
    _entry(c, source_path="a.jsonl", line_offset=3, model=_MODEL,
           msg_id="m3", req_id="r3", inp=10, out=20, cc=160_000, cr=1_000)
    out = cq.get_conversation_outline(c, "m2f")
    detail = cq.get_conversation(c, "m2f", limit=1000)
    fails = [it["cache_failure"] for it in detail["items"] if "cache_failure" in it]
    assert len(fails) == 2
    agg = out["stats"]["cache_failures"]
    assert agg["count"] == 2
    assert agg["tokens_recreated"] == sum(f["tokens_recreated"] for f in fails)
    assert abs(agg["est_wasted_usd"] - sum(f["est_wasted_usd"] for f in fails)) < 1e-12


# ---------------------------------------------------------------------------
# Session-modal cache-rebuilds (2026-06-16 spec): per-rebuild list + cache_saved
# ---------------------------------------------------------------------------
def test_outline_rebuilds_list_single():
    c = _conn()
    _seed_cache_failure(c)                      # one failure on turn "a2"
    agg = cq.get_conversation_outline(c, "cfo")["stats"]["cache_failures"]
    rb = agg["rebuilds"]
    assert len(rb) == 1
    r = rb[0]
    assert r["uuid"] == "a2"                     # the flagged turn's anchor uuid
    assert r["subagent_key"] is None             # main-session rebuild
    assert r["ts"] == "2026-06-01T00:00:05Z"
    assert r["tokens_recreated"] == 130_000
    assert r["est_wasted_usd"] == agg["est_wasted_usd"]   # single -> equals total


def test_outline_rebuilds_sorted_worst_first():
    # Reuse the two-failure fixture: failure 1 (a1) loses 125k, failure 2 (a3)
    # loses 149k -> a3 is the worse (higher wasted $) and must sort FIRST.
    c = _conn()
    _msg(c, session_id="m2f", uuid="a0", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="p", model=_MODEL, msg_id="m0", req_id="r0",
         blocks_json=_json.dumps([{"kind": "text", "text": "p"}]))
    _entry(c, source_path="a.jsonl", line_offset=0, model=_MODEL,
           msg_id="m0", req_id="r0", inp=10, out=20, cc=1_000, cr=130_000)
    _msg(c, session_id="m2f", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="f1", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([{"kind": "text", "text": "f1"}]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20, cc=140_000, cr=5_000)
    _msg(c, session_id="m2f", uuid="a2", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-01T00:00:10Z", entry_type="assistant",
         text="rp", model=_MODEL, msg_id="m2", req_id="r2",
         blocks_json=_json.dumps([{"kind": "text", "text": "rp"}]))
    _entry(c, source_path="a.jsonl", line_offset=2, model=_MODEL,
           msg_id="m2", req_id="r2", inp=10, out=20, cc=2_000, cr=150_000)
    _msg(c, session_id="m2f", uuid="a3", source_path="a.jsonl", byte_offset=3,
         timestamp_utc="2026-06-01T00:00:15Z", entry_type="assistant",
         text="f2", model=_MODEL, msg_id="m3", req_id="r3",
         blocks_json=_json.dumps([{"kind": "text", "text": "f2"}]))
    _entry(c, source_path="a.jsonl", line_offset=3, model=_MODEL,
           msg_id="m3", req_id="r3", inp=10, out=20, cc=160_000, cr=1_000)
    rb = cq.get_conversation_outline(c, "m2f")["stats"]["cache_failures"]["rebuilds"]
    assert [r["uuid"] for r in rb] == ["a3", "a1"]                  # worst-first
    assert rb[0]["est_wasted_usd"] >= rb[1]["est_wasted_usd"]


def test_outline_cache_saved_usd_present_and_positive():
    c = _conn()
    _seed_cache_failure(c)                      # a1 has cr=130_000
    stats = cq.get_conversation_outline(c, "cfo")["stats"]
    assert "cache_saved_usd" in stats
    expected = cq._cache_read_saved_usd(_MODEL, 130_000)   # a2 has cr=0 -> no add
    assert expected > 0
    assert abs(stats["cache_saved_usd"] - expected) < 1e-12


def test_outline_cache_saved_usd_zero_without_cache_reads():
    c = _conn()
    _msg(c, session_id="nocache", uuid="z1", source_path="z.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="x", model=_MODEL, msg_id="zm", req_id="zr",
         blocks_json=_json.dumps([{"kind": "text", "text": "x"}]))
    _entry(c, source_path="z.jsonl", line_offset=0, model=_MODEL,
           msg_id="zm", req_id="zr", inp=10, out=20, cc=0, cr=0)
    stats = cq.get_conversation_outline(c, "nocache")["stats"]
    assert stats["cache_saved_usd"] == 0.0
    assert "cache_failures" not in stats         # unchanged absent-when-none contract


def test_outline_rebuilds_null_ts_sorts_last_on_tie():
    # Two failures with IDENTICAL wasted $ (same model + same lost=130k); the one
    # whose turn has a NULL timestamp must sort LAST (the `ts is None` tiebreak).
    #
    # Ordering note: `_assemble_session` orders turns by `(timestamp_utc, id)`, and
    # SQLite sorts NULL FIRST, so a null-ts turn is walked at the HEAD of the
    # session — before any real-ts prime. The running-max cache-failure detector
    # is therefore seeded with a null-ts prime (smaller id, also walked first) so
    # the null-ts collapse `fb` is flagged; the real-ts prime + `fa` form an
    # independent prime->collapse pair walked afterward. Both lose 130k -> tie.
    c = _conn()
    # null-ts prime (inserted first -> smallest id -> walked first), rm -> 130k
    _msg(c, session_id="nt2", uuid="np", source_path="a.jsonl", byte_offset=0,
         timestamp_utc=None, entry_type="assistant",
         text="np", model=_MODEL, msg_id="npm", req_id="npr",
         blocks_json=_json.dumps([{"kind": "text", "text": "np"}]))
    _entry(c, source_path="a.jsonl", line_offset=0, model=_MODEL,
           msg_id="npm", req_id="npr", inp=10, out=20, cc=1_000, cr=130_000)
    # failure B: ts NULL, lost = min(140k, 130k-0) = 130k
    _msg(c, session_id="nt2", uuid="fb", source_path="a.jsonl", byte_offset=1,
         timestamp_utc=None, entry_type="assistant",
         text="fb", model=_MODEL, msg_id="mb", req_id="rb",
         blocks_json=_json.dumps([{"kind": "text", "text": "fb"}]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="mb", req_id="rb", inp=10, out=20, cc=140_000, cr=0)
    # real-ts prime, rm -> 130k (independent prime->collapse pair)
    _msg(c, session_id="nt2", uuid="p1", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="p", model=_MODEL, msg_id="pm1", req_id="pr1",
         blocks_json=_json.dumps([{"kind": "text", "text": "p"}]))
    _entry(c, source_path="a.jsonl", line_offset=2, model=_MODEL,
           msg_id="pm1", req_id="pr1", inp=10, out=20, cc=1_000, cr=130_000)
    # failure A: ts SET, lost = min(140k, 130k-0) = 130k (same wasted as B)
    _msg(c, session_id="nt2", uuid="fa", source_path="a.jsonl", byte_offset=3,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="fa", model=_MODEL, msg_id="ma", req_id="ra",
         blocks_json=_json.dumps([{"kind": "text", "text": "fa"}]))
    _entry(c, source_path="a.jsonl", line_offset=3, model=_MODEL,
           msg_id="ma", req_id="ra", inp=10, out=20, cc=140_000, cr=0)
    rb = cq.get_conversation_outline(c, "nt2")["stats"]["cache_failures"]["rebuilds"]
    assert len(rb) == 2
    assert rb[0]["est_wasted_usd"] == rb[1]["est_wasted_usd"]    # tie
    assert rb[0]["uuid"] == "fa" and rb[0]["ts"] is not None     # ts-set first
    assert rb[1]["uuid"] == "fb" and rb[1]["ts"] is None         # null-ts last


# ---------------------------------------------------------------------------
# #217 S3 E6(a) — per-subagent cost map (`subagent_costs`).
#
# Display-only, read-time outline enrichment: a top-level `subagent_costs`
# dict mapping every subagent_key present in the session (INCLUDING buckets
# whose `subagent_meta` is empty — the s7 case) to a float cost, summed
# cost-once over the assembled items (the same cost-once-per-turn discipline
# as the items themselves carry). It lives in a SEPARATE top-level map, NOT
# inside `subagent_meta`, so the outline↔reader `subagent_meta` parity assert
# (bin/cctally-conversation-test:779) stays byte-for-byte unchanged.
# ---------------------------------------------------------------------------
def test_outline_subagent_costs_keystone_cost_once():
    # The s5 fixture's sole subagent (sc1, source /agents/agent-abc.jsonl →
    # subagent_key "abc") carries cost row (m2,r2): inp=10,out=20,cr=0. Its
    # subagent_costs entry must equal that turn's independently-computed cost,
    # summed cost-once (the item already carries the cost-once-per-turn value).
    c = _conn()
    _seed_rich(c)
    out = cq.get_conversation_outline(c, "s5")
    sc = out["subagent_costs"]
    assert "abc" in sc
    # Independent: the cost helper over the subagent turn's exact usage row.
    expected = round(cq._entry_cost(_MODEL, 10, 20, 0, 0, None), 6)
    assert expected > 0
    assert abs(sc["abc"] - expected) < 1e-9
    # The main-session cost is NOT in the map (None subagent_key is excluded).
    assert None not in sc
    assert "None" not in sc


def test_outline_subagent_costs_covers_empty_meta_bucket():
    # A subagent whose `subagent_meta` is ABSENT (the s7 case: a sidechain turn
    # with no spawn/result meta plumbed) must STILL get a subagent_costs entry —
    # deriveOutline emits a row for it, so the cost must be present to render.
    c = _conn()
    # main human + assistant (cost), and a sidechain subagent with NO meta but a
    # real cost row (mirrors s7's `dddd4444` bucket).
    _msg(c, session_id="emc", uuid="h1", source_path="m.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="go")
    _msg(c, session_id="emc", uuid="a1", source_path="m.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="ok", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([{"kind": "text", "text": "ok"}]))
    _entry(c, source_path="m.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=100, out=200, cc=0, cr=0)
    # sidechain subagent (agent-xyz → key "xyz"), parent points at a main member,
    # with its own cost row but NO subagent_meta (no spawn tool_use linkage).
    _msg(c, session_id="emc", uuid="sc1", parent_uuid="a1",
         source_path="/agents/agent-xyz.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:02Z", entry_type="assistant",
         text="sub", model=_MODEL, msg_id="m2", req_id="r2", is_sidechain=1,
         blocks_json=_json.dumps([{"kind": "text", "text": "sub"}]))
    _entry(c, source_path="/agents/agent-xyz.jsonl", line_offset=0, model=_MODEL,
           msg_id="m2", req_id="r2", inp=50, out=60, cc=0, cr=0)
    out = cq.get_conversation_outline(c, "emc")
    # The bucket has NO subagent_meta entry...
    assert "xyz" not in (out["subagent_meta"] or {})
    # ...but its cost is still covered.
    assert "xyz" in out["subagent_costs"]
    expected = round(cq._entry_cost(_MODEL, 50, 60, 0, 0, None), 6)
    assert abs(out["subagent_costs"]["xyz"] - expected) < 1e-9


def test_outline_subagent_costs_absent_without_subagents():
    # A session with NO subagents emits an empty map (not absent), so the client
    # can read `subagent_costs[key]` uniformly.
    c = _conn()
    _msg(c, session_id="ns", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="hi")
    _msg(c, session_id="ns", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="x", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([{"kind": "text", "text": "x"}]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20, cc=0, cr=0)
    out = cq.get_conversation_outline(c, "ns")
    assert out["subagent_costs"] == {}


def test_outline_subagent_costs_sum_matches_bucket_total():
    # Two cost-bearing turns in ONE subagent bucket sum cost-once into a single
    # subagent_costs value (no double-count across the bucket's turns).
    c = _conn()
    _msg(c, session_id="ms", uuid="h1", source_path="m.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="go")
    # subagent turn A
    _msg(c, session_id="ms", uuid="sa", parent_uuid="h1",
         source_path="/agents/agent-q.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="A", model=_MODEL, msg_id="ma", req_id="ra", is_sidechain=1,
         blocks_json=_json.dumps([{"kind": "text", "text": "A"}]))
    _entry(c, source_path="/agents/agent-q.jsonl", line_offset=0, model=_MODEL,
           msg_id="ma", req_id="ra", inp=100, out=200, cc=0, cr=0)
    # subagent turn B (same agent file → same bucket "q")
    _msg(c, session_id="ms", uuid="sb", parent_uuid="h1",
         source_path="/agents/agent-q.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:02Z", entry_type="assistant",
         text="B", model=_MODEL, msg_id="mb", req_id="rb", is_sidechain=1,
         blocks_json=_json.dumps([{"kind": "text", "text": "B"}]))
    _entry(c, source_path="/agents/agent-q.jsonl", line_offset=1, model=_MODEL,
           msg_id="mb", req_id="rb", inp=300, out=400, cc=0, cr=0)
    out = cq.get_conversation_outline(c, "ms")
    a = round(cq._entry_cost(_MODEL, 100, 200, 0, 0, None), 6)
    b = round(cq._entry_cost(_MODEL, 300, 400, 0, 0, None), 6)
    assert abs(out["subagent_costs"]["q"] - (a + b)) < 1e-9


# ---------------------------------------------------------------------------
# #217 S5 F2 — outline `files` aggregation (Edit / MultiEdit / Write only;
# NOT NotebookEdit — the deliberate narrower set vs `_FILE_TOUCH_TOOLS`,
# Codex P2-4). Per-path summed +N/-M with a document-ordered `touches[]`;
# nullable stats when neither the stamped `edit_stat` nor a recompute are
# available (Codex P1-7 — the touch is STILL listed).
# ---------------------------------------------------------------------------
def _tool_call(name, inp, *, tid, edit_stat=None):
    b = {"kind": "tool_use", "name": name, "input_summary": "{}",
         "input": inp, "id": tid, "preview": ""}
    if edit_stat is not None:
        b["edit_stat"] = edit_stat
    return b


def test_outline_files_aggregates_by_path_summed():
    # Two Edits to foo.py (computed from old/new) + one Write to bar.py.
    c = _conn()
    _msg(c, session_id="fa", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human", text="edit")
    _msg(c, session_id="fa", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-12T14:00:01Z", entry_type="assistant",
         text="ok", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             {"kind": "text", "text": "ok"},
             # foo.py edit #1: old 1 line -> new 3 lines => add 3, del 1.
             _tool_call("Edit", {"file_path": "/repo/foo.py",
                                 "old_string": "a", "new_string": "x\ny\nz"}, tid="t1"),
             # bar.py write: 2-line content => add 2, del 0.
             _tool_call("Write", {"file_path": "/repo/bar.py",
                                  "content": "one\ntwo\n"}, tid="t2")]))
    _msg(c, session_id="fa", uuid="a2", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-12T14:00:02Z", entry_type="assistant",
         text="more", model=_MODEL, msg_id="m2", req_id="r2",
         blocks_json=_json.dumps([
             {"kind": "text", "text": "more"},
             # foo.py edit #2: old 2 lines -> new 1 line => add 1, del 2.
             _tool_call("Edit", {"file_path": "/repo/foo.py",
                                 "old_string": "p\nq", "new_string": "r"}, tid="t3")]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20)
    _entry(c, source_path="a.jsonl", line_offset=2, model=_MODEL,
           msg_id="m2", req_id="r2", inp=10, out=20)
    out = cq.get_conversation_outline(c, "fa")
    files = {f["path"]: f for f in out["files"]}
    # foo.py: edit1 (add 3, del 1) + edit2 (add 1, del 2) => add 4, del 3.
    assert files["/repo/foo.py"]["add"] == 4
    assert files["/repo/foo.py"]["del"] == 3
    assert len(files["/repo/foo.py"]["touches"]) == 2
    # bar.py: Write => del 0.
    assert files["/repo/bar.py"]["add"] == 2
    assert files["/repo/bar.py"]["del"] == 0
    assert len(files["/repo/bar.py"]["touches"]) == 1
    # Document order: foo.py touched first (turn a1) -> appears before bar.py.
    assert [f["path"] for f in out["files"]] == ["/repo/foo.py", "/repo/bar.py"]
    # Per-touch shape carries the jump anchor + tool_use_id + op + signed stat.
    t = files["/repo/foo.py"]["touches"][0]
    assert t["uuid"] == "a1" and t["tool_use_id"] == "t1" and t["op"] == "edit"
    assert t["add"] == 3 and t["del"] == 1


def test_outline_files_multiedit_sums():
    c = _conn()
    _msg(c, session_id="me", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human", text="multi")
    _msg(c, session_id="me", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-12T14:00:01Z", entry_type="assistant",
         text="ok", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             _tool_call("MultiEdit", {"file_path": "/repo/baz.py", "edits": [
                 {"old_string": "a", "new_string": "x\ny"},   # add 2, del 1
                 {"old_string": "b\nc", "new_string": "z"}]}, tid="t1")]))  # add 1, del 2
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20)
    out = cq.get_conversation_outline(c, "me")
    f = {x["path"]: x for x in out["files"]}["/repo/baz.py"]
    assert f["add"] == 3 and f["del"] == 3
    assert f["touches"][0]["op"] == "multiedit"


def test_outline_files_uses_stamped_edit_stat_when_truncated():
    # A truncated edit input carries a stamped edit_stat (the recompute path
    # can't run on the bounded copy); the aggregation must use it.
    c = _conn()
    _msg(c, session_id="tr", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human", text="big edit")
    _msg(c, session_id="tr", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-12T14:00:01Z", entry_type="assistant",
         text="ok", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             # input clipped so old/new can't be recounted; edit_stat is stamped.
             {"kind": "tool_use", "name": "Edit", "input_summary": "{}",
              "input": {"file_path": "/repo/huge.py", "old_string": "<clipped>"},
              "input_truncated": True, "edit_stat": {"add": 99, "del": 12},
              "id": "t1", "preview": ""}]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20)
    out = cq.get_conversation_outline(c, "tr")
    f = {x["path"]: x for x in out["files"]}["/repo/huge.py"]
    assert f["add"] == 99 and f["del"] == 12
    assert f["touches"][0]["add"] == 99 and f["touches"][0]["del"] == 12


def test_outline_files_nullable_stat_when_unknown():
    # An edit whose stat is neither stamped nor recomputable is STILL listed,
    # with add/del = None (Codex P1-7). A MultiEdit whose `edits` is NOT a list
    # (clipped to the bounding elision) makes `_edit_stat_for` return None.
    c = _conn()
    _msg(c, session_id="un", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human", text="edit")
    _msg(c, session_id="un", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-12T14:00:01Z", entry_type="assistant",
         text="ok", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             {"kind": "tool_use", "name": "MultiEdit", "input_summary": "{}",
              "input": {"file_path": "/repo/unknown.py", "edits": "…"},
              "input_truncated": True, "id": "t1", "preview": ""}]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20)
    out = cq.get_conversation_outline(c, "un")
    f = out["files"][0]
    t = f["touches"][0]
    assert t["add"] is None and t["del"] is None
    assert t["uuid"] == "a1"                      # still listed
    # Every touch unknown -> file totals are null too.
    assert f["add"] is None and f["del"] is None


def test_outline_files_mixed_known_and_unknown_sums_known():
    # One known + one unknown touch on the same path: totals sum only the known.
    c = _conn()
    _msg(c, session_id="mx", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human", text="edit")
    _msg(c, session_id="mx", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-12T14:00:01Z", entry_type="assistant",
         text="ok", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             _tool_call("Edit", {"file_path": "/repo/m.py",
                                 "old_string": "a", "new_string": "x\ny"}, tid="t1"),
             {"kind": "tool_use", "name": "MultiEdit", "input_summary": "{}",
              "input": {"file_path": "/repo/m.py", "edits": "…"},
              "input_truncated": True, "id": "t2", "preview": ""}]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20)
    out = cq.get_conversation_outline(c, "mx")
    f = {x["path"]: x for x in out["files"]}["/repo/m.py"]
    # Known touch: add 2, del 1; unknown touch contributes nothing.
    assert f["add"] == 2 and f["del"] == 1
    assert len(f["touches"]) == 2


def test_outline_files_excludes_non_edit_tools():
    # Read + Bash + NotebookEdit contribute nothing -> empty files list.
    c = _conn()
    _msg(c, session_id="nx", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human", text="look")
    _msg(c, session_id="nx", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-12T14:00:01Z", entry_type="assistant",
         text="ok", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             _tool_call("Read", {"file_path": "/repo/r.py"}, tid="t1"),
             _tool_call("Bash", {"command": "ls"}, tid="t2"),
             # NotebookEdit is in _FILE_TOUCH_TOOLS but NOT the export set (P2-4).
             _tool_call("NotebookEdit", {"file_path": "/repo/n.ipynb",
                                         "new_source": "x"}, tid="t3")]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20)
    out = cq.get_conversation_outline(c, "nx")
    assert out["files"] == []


def test_outline_files_present_key_always():
    # The `files` key is always present (possibly []), so the client reads it
    # uniformly.
    c = _conn()
    _seed_rich(c)
    out = cq.get_conversation_outline(c, "s5")
    assert "files" in out and out["files"] == []


# ---------------------------------------------------------------------------
# #217 S5 F7 — outline `task_completion` detection. The LAST main-thread task
# snapshot (Task* `task_snapshot` stamped by `_fold_task_runs`, OR legacy
# TodoWrite `input.todos`), scanned in document order, MAIN THREAD ONLY
# (`subagent_key is None`, not `is_sidechain` — Codex P1-1, because
# `_fold_task_runs` keeps task state PER subagent thread). Emits
# {all_done, total, completed, anchor_uuid} or None (no tasks).
# ---------------------------------------------------------------------------
def _todowrite(todos, *, tid):
    # A legacy TodoWrite tool_call carrying its full `todos` list on `input`.
    return {"kind": "tool_use", "name": "TodoWrite", "input_summary": "{}",
            "input": {"todos": todos}, "id": tid, "preview": ""}


def _task_snap_block(snapshot, *, name="TaskUpdate", tid):
    # A Task* tool_call pre-stamped with the folded `task_snapshot` (mirrors
    # what `_fold_task_runs` writes onto the first Task* call of a run).
    return {"kind": "tool_use", "name": name, "input_summary": "{}",
            "input": {}, "id": tid, "preview": "", "task_snapshot": snapshot}


def test_outline_task_completion_all_done():
    # Last main-thread snapshot is fully completed -> all_done True; totals match.
    c = _conn()
    _msg(c, session_id="td", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human", text="do work")
    _msg(c, session_id="td", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-12T14:00:01Z", entry_type="assistant",
         text="working", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             {"kind": "text", "text": "working"},
             _todowrite([{"content": "step a", "status": "completed"},
                         {"content": "step b", "status": "completed"}], tid="t1")]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20)
    out = cq.get_conversation_outline(c, "td")
    tc = out["task_completion"]
    assert tc["all_done"] is True
    assert tc["total"] == 2 and tc["completed"] == 2
    assert tc["total"] == tc["completed"]
    assert tc["anchor_uuid"] == "a1"


def test_outline_task_completion_not_done():
    # Last snapshot has an incomplete item -> all_done False (still emitted).
    c = _conn()
    _msg(c, session_id="tp", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human", text="do work")
    _msg(c, session_id="tp", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-12T14:00:01Z", entry_type="assistant",
         text="working", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             _todowrite([{"content": "step a", "status": "completed"},
                         {"content": "step b", "status": "in_progress"},
                         {"content": "step c", "status": "pending"}], tid="t1")]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20)
    tc = cq.get_conversation_outline(c, "tp")["task_completion"]
    assert tc["all_done"] is False
    assert tc["total"] == 3 and tc["completed"] == 1


def test_outline_task_completion_takes_last_snapshot():
    # An EARLIER all-done snapshot followed by a LATER partial one -> the LAST
    # (document order) wins, so all_done False at the later anchor.
    c = _conn()
    _msg(c, session_id="tl", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human", text="go")
    _msg(c, session_id="tl", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-12T14:00:01Z", entry_type="assistant",
         text="first", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             _todowrite([{"content": "x", "status": "completed"}], tid="t1")]))
    _msg(c, session_id="tl", uuid="a2", source_path="a.jsonl", byte_offset=2,
         timestamp_utc="2026-06-12T14:00:02Z", entry_type="assistant",
         text="second", model=_MODEL, msg_id="m2", req_id="r2",
         blocks_json=_json.dumps([
             _todowrite([{"content": "x", "status": "completed"},
                         {"content": "y", "status": "pending"}], tid="t2")]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20)
    _entry(c, source_path="a.jsonl", line_offset=2, model=_MODEL,
           msg_id="m2", req_id="r2", inp=10, out=20)
    tc = cq.get_conversation_outline(c, "tl")["task_completion"]
    assert tc["anchor_uuid"] == "a2"
    assert tc["all_done"] is False and tc["total"] == 2 and tc["completed"] == 1


def test_outline_task_completion_task_snapshot_family():
    # The Task* family carries a stamped `task_snapshot` (not TodoWrite) — it is
    # detected identically.
    c = _conn()
    _msg(c, session_id="ts", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-12T14:00:00Z", entry_type="human", text="go")
    _msg(c, session_id="ts", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-12T14:00:01Z", entry_type="assistant",
         text="done", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([
             _task_snap_block([{"content": "a", "status": "completed"},
                               {"content": "b", "status": "completed"}], tid="t1")]))
    _entry(c, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20)
    tc = cq.get_conversation_outline(c, "ts")["task_completion"]
    assert tc["all_done"] is True and tc["total"] == 2 and tc["anchor_uuid"] == "a1"


def test_outline_task_completion_ignores_subagent_only():
    # A session where ONLY a subagent (sidechain, non-null subagent_key) carries
    # a task snapshot -> main-thread detection -> None (Codex P1-1).
    c = _conn()
    _msg(c, session_id="so", uuid="h1", source_path="m.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="go")
    _msg(c, session_id="so", uuid="a1", source_path="m.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:01Z", entry_type="assistant",
         text="ok", model=_MODEL, msg_id="m1", req_id="r1",
         blocks_json=_json.dumps([{"kind": "text", "text": "ok"}]))
    _entry(c, source_path="m.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=10, out=20)
    # subagent (sidechain) turn carrying a completed task snapshot.
    _msg(c, session_id="so", uuid="sc1", parent_uuid="a1",
         source_path="/agents/agent-sub.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:02Z", entry_type="assistant",
         text="sub", model=_MODEL, msg_id="m2", req_id="r2", is_sidechain=1,
         blocks_json=_json.dumps([
             _todowrite([{"content": "sub task", "status": "completed"}], tid="t1")]))
    _entry(c, source_path="/agents/agent-sub.jsonl", line_offset=0, model=_MODEL,
           msg_id="m2", req_id="r2", inp=10, out=20)
    assert cq.get_conversation_outline(c, "so")["task_completion"] is None


def test_outline_task_completion_none_when_no_tasks():
    # No task snapshot anywhere -> None.
    c = _conn()
    _seed_rich(c)
    assert cq.get_conversation_outline(c, "s5")["task_completion"] is None


def test_outline_task_completion_present_key_always():
    # The `task_completion` key is always present (None when no tasks), so the
    # client reads it uniformly.
    c = _conn()
    _seed_rich(c)
    out = cq.get_conversation_outline(c, "s5")
    assert "task_completion" in out and out["task_completion"] is None
