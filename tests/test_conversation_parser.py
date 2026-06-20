import io, json, sys, pathlib
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _lib_conversation as lc

def _jsonl(*objs):
    return io.StringIO("".join(json.dumps(o) + "\n" for o in objs))

def test_human_prompt():
    fh = _jsonl({"type": "user", "uuid": "u1", "sessionId": "s1",
                 "timestamp": "2026-06-01T00:00:00Z", "cwd": "/x",
                 "message": {"role": "user", "content": "hello world"}})
    rows = list(lc.iter_message_rows(fh, "f.jsonl"))
    assert len(rows) == 1
    r = rows[0]
    assert r.entry_type == lc.HUMAN
    assert r.text == "hello world"
    assert r.uuid == "u1" and r.session_id == "s1"
    assert r.byte_offset == 0

def test_assistant_text_only_no_thinking_in_text():
    fh = _jsonl({"type": "assistant", "uuid": "a1", "sessionId": "s1",
                 "requestId": "req1", "timestamp": "t",
                 "message": {"role": "assistant", "id": "msg1", "model": "opus",
                             "content": [{"type": "thinking", "thinking": "secret plan"},
                                         {"type": "text", "text": "visible answer"},
                                         {"type": "tool_use", "name": "Bash",
                                          "input": {"command": "ls"}}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    assert r.entry_type == lc.ASSISTANT
    assert r.text == "visible answer"            # thinking + tool_use NOT in indexed text
    assert r.msg_id == "msg1" and r.req_id == "req1" and r.model == "opus"
    kinds = [b["kind"] for b in json.loads(r.blocks_json)]
    assert kinds == ["thinking", "text", "tool_use"]

def test_user_role_tool_result_is_tool_result_not_human():
    fh = _jsonl({"type": "user", "uuid": "u2", "sessionId": "s1", "timestamp": "t",
                 "message": {"role": "user",
                             "content": [{"type": "tool_result", "content": "OUTPUT", "is_error": False}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    assert r.entry_type == lc.TOOL_RESULT
    assert r.text == ""                           # tool_result not indexed as prose
    b = json.loads(r.blocks_json)[0]
    assert b["kind"] == "tool_result" and b["text"] == "OUTPUT"

def test_tool_result_capped():
    big = "x" * (lc._TOOL_RESULT_CAP + 50)
    fh = _jsonl({"type": "user", "uuid": "u3", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user", "content": [{"type": "tool_result", "content": big}]}})
    b = json.loads(list(lc.iter_message_rows(fh, "f"))[0].blocks_json)[0]
    assert len(b["text"]) == lc._TOOL_RESULT_CAP and b["truncated"] is True

def test_image_and_document_are_placeholders_no_base64():
    fh = _jsonl({"type": "user", "uuid": "u4", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user", "content": [
                     {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                  "data": "AAAABBBB"}},
                     {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                     "data": "CCCCDDDD"}}]}})
    blocks = json.loads(list(lc.iter_message_rows(fh, "f"))[0].blocks_json)
    img, doc = blocks[0], blocks[1]
    assert img["kind"] == "image" and img["media_type"] == "image/png" and img["bytes"] == len("AAAABBBB")
    assert img["index"] == 0                      # #177 S4: ordinal among media items
    assert doc["kind"] == "document" and doc["media_type"] == "application/pdf"
    assert doc["index"] == 1
    assert "data" not in json.dumps(blocks) and "AAAABBBB" not in json.dumps(blocks)

def test_summary_and_file_history_skipped():
    fh = _jsonl({"type": "summary", "summary": "...", "leafUuid": "l"},
                {"type": "file-history-snapshot"},
                {"type": "user", "uuid": "u5", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user", "content": "kept"}})
    rows = list(lc.iter_message_rows(fh, "f"))
    assert [r.uuid for r in rows] == ["u5"]

def test_partial_tail_line_rewinds():
    # a line without a trailing newline is a mid-write tail: must not be consumed
    buf = io.StringIO(json.dumps({"type": "user", "uuid": "u6", "sessionId": "s",
                                  "timestamp": "t", "message": {"role": "user", "content": "a"}}) + "\n"
                      + '{"type":"user","uuid":"partial"')   # no newline
    rows = list(lc.iter_message_rows(buf, "f"))
    assert [r.uuid for r in rows] == ["u6"]
    # cursor rewound to the start of the partial line
    assert buf.readline().startswith('{"type":"user","uuid":"partial"')


def test_ismeta_user_line_classified_meta_text_empty_blocks_kept():
    # a skill body arrives as a type:user, isMeta:true line with text blocks
    body = "Base directory for this skill: /x/skills/brainstorming\n\n# Brainstorming"
    fh = _jsonl({"type": "user", "uuid": "m1", "sessionId": "s", "timestamp": "t",
                 "isMeta": True, "sourceToolUseID": "toolu_x",
                 "message": {"role": "user", "content": [{"type": "text", "text": body}]}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.META
    assert r.text == ""                                  # not FTS-indexed, not a title candidate
    blocks = json.loads(r.blocks_json)
    assert blocks and blocks[0]["kind"] == "text" and "Brainstorming" in blocks[0]["text"]


def test_ismeta_with_tool_result_block_stays_tool_result():
    # tool_result precedence is checked BEFORE isMeta
    fh = _jsonl({"type": "user", "uuid": "m2", "sessionId": "s", "timestamp": "t",
                 "isMeta": True,
                 "message": {"role": "user",
                             "content": [{"type": "tool_result", "content": "OUT"}]}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.TOOL_RESULT


def test_ismeta_assistant_line_stays_assistant():
    # only USER isMeta lines are reclassified; an isMeta assistant line is still
    # an assistant turn (it is not attributed to the user either way)
    fh = _jsonl({"type": "assistant", "uuid": "m3", "sessionId": "s", "requestId": "r",
                 "timestamp": "t", "isMeta": True,
                 "message": {"role": "assistant", "id": "msg", "model": "opus",
                             "content": [{"type": "text", "text": "x"}]}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.ASSISTANT


def test_non_meta_user_line_stays_human():
    fh = _jsonl({"type": "user", "uuid": "m4", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user", "content": "a real prompt"}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.HUMAN and r.text == "a real prompt"

def test_non_scalar_content_yields_empty_text_and_no_blocks():
    # content that is neither str nor list (defensive) must not crash and must keep
    # text='' (the NOT NULL column) with blocks_json '[]'.
    fh = _jsonl({"type": "user", "uuid": "u7", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user", "content": 123}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.HUMAN and r.text == "" and r.blocks_json == "[]"

def test_summarize_caps_tool_use_input_at_200():
    fh = _jsonl({"type": "assistant", "uuid": "a9", "sessionId": "s", "requestId": "r",
                 "timestamp": "t", "message": {"role": "assistant", "id": "m", "model": "o",
                 "content": [{"type": "tool_use", "name": "Bash",
                              "input": {"command": "x" * 500}}]}})
    b = json.loads(list(lc.iter_message_rows(fh, "f"))[0].blocks_json)[0]
    assert b["kind"] == "tool_use" and b["name"] == "Bash" and len(b["input_summary"]) == 200

def test_tool_result_block_list_is_stringified_multiline():
    fh = _jsonl({"type": "user", "uuid": "u8", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "content": [
                         {"type": "text", "text": "line1"},
                         {"type": "text", "text": "line2"}]}]}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.TOOL_RESULT
    assert json.loads(r.blocks_json)[0]["text"] == "line1\nline2"


# ──────────────────────────────────────────────────────────────────────────
# parse_message_row — the pure per-line parser extracted in #138 so the fused
# single-pass sync walker can share it. iter_message_rows now delegates to it.
# ──────────────────────────────────────────────────────────────────────────

def test_parse_message_row_user_with_uuid_returns_row():
    obj = {"type": "user", "uuid": "u1", "sessionId": "s", "timestamp": "t",
           "message": {"role": "user", "content": "hi there"}}
    row = lc.parse_message_row(obj, 42)
    assert row is not None
    assert row.byte_offset == 42
    assert row.uuid == "u1"
    assert row.entry_type == lc.HUMAN
    assert row.text == "hi there"


def test_parse_message_row_assistant_carries_model_and_ids():
    obj = {"type": "assistant", "uuid": "a1", "sessionId": "s", "requestId": "r1",
           "timestamp": "t", "message": {"role": "assistant", "id": "m1",
           "model": "claude-opus-4-7", "content": [{"type": "text", "text": "ans"}]}}
    row = lc.parse_message_row(obj, 0)
    assert row is not None
    assert row.entry_type == lc.ASSISTANT
    assert row.model == "claude-opus-4-7" and row.msg_id == "m1" and row.req_id == "r1"


def test_parse_message_row_non_user_assistant_returns_none():
    assert lc.parse_message_row({"type": "summary", "leafUuid": "l"}, 0) is None
    assert lc.parse_message_row({"type": "file-history-snapshot"}, 0) is None


def test_parse_message_row_missing_uuid_returns_none():
    obj = {"type": "user", "sessionId": "s", "timestamp": "t",
           "message": {"role": "user", "content": "x"}}
    assert lc.parse_message_row(obj, 0) is None


# ──────────────────────────────────────────────────────────────────────────
# #164 Task A1: the parser keeps the tool_use.id / tool_result.tool_use_id
# linkage ids (currently dropped) so the query kernel can pair request+result.
# ──────────────────────────────────────────────────────────────────────────
def test_tool_use_block_keeps_id():
    from _lib_conversation import _blocks_and_text
    content = [{"type": "tool_use", "id": "toolu_abc", "name": "Read",
                "input": {"file_path": "/x/y.py"}}]
    blocks, text = _blocks_and_text(content)
    assert blocks[0]["kind"] == "tool_use"
    assert blocks[0]["id"] == "toolu_abc"


def test_tool_result_block_keeps_tool_use_id():
    from _lib_conversation import _blocks_and_text
    content = [{"type": "tool_result", "tool_use_id": "toolu_abc",
                "content": "ok"}]
    blocks, text = _blocks_and_text(content)
    assert blocks[0]["kind"] == "tool_result"
    assert blocks[0]["tool_use_id"] == "toolu_abc"


def test_missing_ids_default_to_none_not_keyerror():
    from _lib_conversation import _blocks_and_text
    blocks, _ = _blocks_and_text([{"type": "tool_use", "name": "Bash",
                                  "input": {"command": "ls"}}])
    assert blocks[0]["id"] is None
    blocks2, _ = _blocks_and_text([{"type": "tool_result", "content": "x"}])
    assert blocks2[0]["tool_use_id"] is None


# ──────────────────────────────────────────────────────────────────────────
# #164 Task A2: parse-time tool_preview() — a faithful one-line preview built
# from the RAW input dict (before _summarize truncates to 200 chars).
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,inp,expected", [
    ("Read",      {"file_path": "/a/b.py", "limit": 10}, "/a/b.py"),
    ("Write",     {"file_path": "/a/b.py", "content": "x"}, "/a/b.py"),
    ("Edit",      {"file_path": "/a/b.py"}, "/a/b.py"),
    ("MultiEdit", {"file_path": "/a/b.py"}, "/a/b.py"),
    ("NotebookEdit", {"file_path": "/a/nb.ipynb"}, "/a/nb.ipynb"),
    ("Bash",      {"command": "git status\n--porcelain"}, "git status"),
    ("Grep",      {"pattern": "foo", "path": "src"}, "foo"),
    ("Glob",      {"pattern": "**/*.py"}, "**/*.py"),
    ("Task",      {"description": "do thing", "subagent_type": "x"}, "do thing"),
    ("Task",      {"subagent_type": "x"}, "x"),
    ("WebFetch",  {"url": "https://e.com"}, "https://e.com"),
    ("WebSearch", {"query": "q"}, "q"),
    ("mcp__srv__do", {"arg": "val"}, "val"),
    ("Unknown",   {"alpha": 1, "beta": "bee"}, "bee"),  # first string-valued arg
    ("Unknown",   {"alpha": 1}, "Unknown"),             # no string arg -> name
])
def test_tool_preview(name, inp, expected):
    from _lib_conversation import tool_preview
    assert tool_preview(name, inp) == expected


def test_tool_preview_non_dict_input_is_empty():
    from _lib_conversation import tool_preview
    assert tool_preview("Read", None) == ""
    assert tool_preview(None, {"file_path": "/x"}) == "/x"  # name None -> generic fallback


def test_tool_use_block_keeps_preview():
    from _lib_conversation import _blocks_and_text
    blocks, _ = _blocks_and_text([{"type": "tool_use", "id": "t1", "name": "Read",
                                  "input": {"file_path": "/a/b.py"}}])
    assert blocks[0]["preview"] == "/a/b.py"


# --- #166: subagent kind (subagent_type) + record-level toolUseResult meta ----

def test_spawn_tool_use_captures_subagent_type():
    fh = _jsonl({"type": "assistant", "uuid": "a1", "sessionId": "s1",
                 "requestId": "r1", "timestamp": "t",
                 "message": {"role": "assistant", "id": "m1", "model": "opus",
                             "content": [{"type": "tool_use", "name": "Task", "id": "tu1",
                                          "input": {"description": "audit",
                                                    "subagent_type": "Explore"}}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    tu = [b for b in json.loads(r.blocks_json) if b["kind"] == "tool_use"][0]
    assert tu["subagent_type"] == "Explore"
    assert tu["id"] == "tu1"

def test_tool_result_captures_agent_id_and_snake_meta():
    fh = _jsonl({"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "t",
                 "toolUseResult": {"agentId": "aaaa1111", "status": "completed",
                                   "totalTokens": 23285, "totalDurationMs": 10668,
                                   "totalToolUseCount": 1, "prompt": "do it"},
                 "message": {"role": "user",
                             "content": [{"type": "tool_result", "content": "done",
                                          "tool_use_id": "tu1", "is_error": False}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    tr = [b for b in json.loads(r.blocks_json) if b["kind"] == "tool_result"][0]
    assert tr["agent_id"] == "aaaa1111"
    assert tr["subagent_meta"] == {"total_tokens": 23285, "total_duration_ms": 10668,
                                   "total_tool_use_count": 1, "status": "completed"}

# ─── #217 S1 / U6: nested (grandchild) subagent result — agentId in the result
# STRING content (no record-level toolUseResult). The id (+ optional <usage>) is
# parsed from the FULL raw content at INGEST, before the _TOOL_RESULT_CAP clip,
# and stamped as structured block["agent_id"]/["subagent_meta"] — so a result
# whose agentId: trailer lands PAST the 16 KB cut still links (vs. degrading to a
# flat card when only the read-time regex over the clipped text was available).

def test_nested_grandchild_agent_id_stamped_from_string_content():
    # A grandchild spawn result as STRING content with a trailing agentId + usage.
    body = ("subagent done\n"
            "agentId: bbbb2222 (use SendMessage to continue)\n"
            "<usage>subagent_tokens: 4242 tool_uses: 7 duration_ms: 9001</usage>")
    fh = _jsonl({"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "t",
                 "message": {"role": "user",
                             "content": [{"type": "tool_result", "content": body,
                                          "tool_use_id": "tu1"}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    tr = [b for b in json.loads(r.blocks_json) if b["kind"] == "tool_result"][0]
    assert tr["agent_id"] == "bbbb2222"
    assert tr["subagent_meta"] == {"total_tokens": 4242, "total_tool_use_count": 7,
                                   "total_duration_ms": 9001, "status": "completed"}

def test_nested_grandchild_over_16kb_agent_id_past_clip_still_stamped():
    # The load-bearing #217 S1 / U6 case: the agentId: trailer lands PAST the
    # 16 KB _TOOL_RESULT_CAP. The clipped block["text"] no longer contains it, so
    # the read-time regex (over text) would yield None and the grandchild would
    # degrade to a flat card. The INGEST stamp (over the full raw content) must
    # still recover the id so the link survives.
    filler = "F" * (lc._TOOL_RESULT_CAP + 500)   # pushes the trailer past the cut
    body = (filler + "\nagentId: cccc3333 (use SendMessage to continue)\n"
            "<usage>subagent_tokens: 100 tool_uses: 1 duration_ms: 5</usage>")
    fh = _jsonl({"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "t",
                 "message": {"role": "user",
                             "content": [{"type": "tool_result", "content": body,
                                          "tool_use_id": "tu1"}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    tr = [b for b in json.loads(r.blocks_json) if b["kind"] == "tool_result"][0]
    # text is clipped (the trailer is gone from it) but the structured stamp survives.
    assert len(tr["text"]) == lc._TOOL_RESULT_CAP and tr["truncated"] is True
    assert "agentId" not in tr["text"]
    assert tr["agent_id"] == "cccc3333"
    assert tr["subagent_meta"]["total_tokens"] == 100

def test_nested_grandchild_usage_absent_but_id_present_stamps_empty_meta():
    # The agentId: line is present but the result carries NO well-formed <usage>
    # block (the source itself emitted only the id, or its usage trailer was cut
    # off mid-emit so the regex can't match). The id still stamps; subagent_meta
    # degrades to {} — never lose the whole link just because usage is missing.
    body = ("agentId: dddd4444 (use SendMessage to continue)\n"
            "<usage>subagent_tokens: 9 tool_uses:")   # truncated <usage> — won't match
    fh = _jsonl({"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "t",
                 "message": {"role": "user",
                             "content": [{"type": "tool_result", "content": body,
                                          "tool_use_id": "tu1"}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    tr = [b for b in json.loads(r.blocks_json) if b["kind"] == "tool_result"][0]
    assert tr["agent_id"] == "dddd4444"
    assert tr["subagent_meta"] == {}   # id survives, malformed/absent usage degrades to {}

def test_ordinary_tool_result_string_does_not_stamp_agent_id():
    # A plain result with no agentId: trailer must NOT acquire an agent_id key
    # (the stamp is gated on the regex matching the agentId line).
    fh = _jsonl({"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "t",
                 "message": {"role": "user",
                             "content": [{"type": "tool_result",
                                          "content": "just a normal result body",
                                          "tool_use_id": "tu1"}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    tr = [b for b in json.loads(r.blocks_json) if b["kind"] == "tool_result"][0]
    assert "agent_id" not in tr and "subagent_meta" not in tr

def test_tool_result_without_tooluseresult_has_no_agent_id():
    fh = _jsonl({"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "t",
                 "message": {"role": "user",
                             "content": [{"type": "tool_result", "content": "x",
                                          "tool_use_id": "tu1"}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    tr = [b for b in json.loads(r.blocks_json) if b["kind"] == "tool_result"][0]
    assert "agent_id" not in tr and "subagent_meta" not in tr

def test_non_spawn_tool_use_has_no_subagent_type():
    fh = _jsonl({"type": "assistant", "uuid": "a1", "sessionId": "s1", "requestId": "r1",
                 "timestamp": "t",
                 "message": {"role": "assistant", "id": "m1", "model": "opus",
                             "content": [{"type": "tool_use", "name": "Bash", "id": "tu1",
                                          "input": {"command": "ls"}}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    tu = [b for b in json.loads(r.blocks_json) if b["kind"] == "tool_use"][0]
    assert "subagent_type" not in tu

def test_multiple_tool_result_blocks_attach_nothing():
    fh = _jsonl({"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "t",
                 "toolUseResult": {"agentId": "aaaa1111", "status": "completed"},
                 "message": {"role": "user",
                             "content": [{"type": "tool_result", "content": "a",
                                          "tool_use_id": "tu1"},
                                         {"type": "tool_result", "content": "b",
                                          "tool_use_id": "tu2"}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    trs = [b for b in json.loads(r.blocks_json) if b["kind"] == "tool_result"]
    assert all("agent_id" not in b for b in trs)

def test_empty_subagent_type_and_agentid_treated_absent():
    fh = _jsonl({"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "t",
                 "toolUseResult": {"agentId": "", "status": "completed"},
                 "message": {"role": "user",
                             "content": [{"type": "tool_result", "content": "x",
                                          "tool_use_id": "tu1"}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    tr = [b for b in json.loads(r.blocks_json) if b["kind"] == "tool_result"][0]
    assert "agent_id" not in tr

def test_empty_subagent_type_treated_absent():
    # Directly exercises the SPAWN-side empty-string guard
    # (`if isinstance(st, str) and st:` in _lib_conversation._blocks_and_text):
    # an empty `input.subagent_type` must NOT add a subagent_type key to the
    # tool_use block (the kernel then degrades that card to title-only).
    fh = _jsonl({"type": "assistant", "uuid": "a1", "sessionId": "s1",
                 "requestId": "r1", "timestamp": "t",
                 "message": {"role": "assistant", "id": "m1", "model": "opus",
                             "content": [{"type": "tool_use", "name": "Task", "id": "tu1",
                                          "input": {"description": "audit",
                                                    "subagent_type": ""}}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    tu = [b for b in json.loads(r.blocks_json) if b["kind"] == "tool_use"][0]
    assert "subagent_type" not in tu
    assert tu["id"] == "tu1"


# --- skill-content nesting: capture sourceToolUseID on the skill body ---------

def test_normalize_captures_source_tool_use_id_on_skill_body():
    # An isMeta skill body carries sourceToolUseID linking back to the Skill tool_use.
    obj = {
        "type": "user", "uuid": "u-skill", "parentUuid": "p1",
        "isMeta": True, "sessionId": "s1", "timestamp": "2026-06-01T00:00:00Z",
        "sourceToolUseID": "toolu_ABC",
        "message": {"content": [{"type": "text",
                                 "text": "Base directory for this skill: /x/skills/brainstorming"}]},
    }
    row = lc._normalize(obj, "user", 0)
    assert row.entry_type == "meta"
    assert row.source_tool_use_id == "toolu_ABC"


def test_normalize_source_tool_use_id_null_without_field():
    # A plain human turn has no sourceToolUseID.
    obj = {
        "type": "user", "uuid": "u-h", "sessionId": "s1",
        "timestamp": "2026-06-01T00:00:00Z",
        "message": {"content": [{"type": "text", "text": "hello"}]},
    }
    row = lc._normalize(obj, "user", 0)
    assert row.source_tool_use_id is None


# ──────────────────────────────────────────────────────────────────────────
# #177 Session 1: _bound_input — leaf-bounded structured tool input, hard-
# bounded on FOUR axes (leaf clip / node budget / depth cap / total-size
# backstop) so a pathological input can't bloat blocks_json (Codex P1).
# ──────────────────────────────────────────────────────────────────────────

def test_bound_input_small_dict_roundtrips_whole():
    obj, trunc = lc._bound_input({"file_path": "/a/b.py", "limit": 10, "ok": True})
    assert obj == {"file_path": "/a/b.py", "limit": 10, "ok": True}
    assert trunc is False

def test_bound_input_clips_long_string_leaf():
    big = "x" * (lc._INPUT_LEAF_CAP + 500)
    obj, trunc = lc._bound_input({"old_string": big})
    assert len(obj["old_string"]) == lc._INPUT_LEAF_CAP
    assert trunc is True

def test_bound_input_non_string_leaves_pass_through():
    obj, trunc = lc._bound_input({"n": 5, "f": 1.5, "b": False, "z": None})
    assert obj == {"n": 5, "f": 1.5, "b": False, "z": None}
    assert trunc is False

def test_bound_input_node_budget_tail_elides():
    many = {f"k{i}": "v" for i in range(lc._INPUT_MAX_NODES + 50)}
    obj, trunc = lc._bound_input(many)
    assert trunc is True
    assert lc._INPUT_ELISION in obj.values()
    # serialized size is bounded
    assert len(json.dumps(obj)) <= lc._INPUT_TOTAL_CAP * 2

def test_bound_input_clips_long_dict_key():
    # code-review I1: keys were stored verbatim, so a pathological many-long-keys
    # input was unbounded (~7× over _INPUT_TOTAL_CAP). The key axis is now clipped.
    long_key = "K" * (lc._INPUT_KEY_CAP + 50)
    obj, trunc = lc._bound_input({long_key: "v"})
    (only_key,) = obj.keys()
    assert len(only_key) == lc._INPUT_KEY_CAP
    assert trunc is True

def test_bound_input_depth_cap_no_recursion():
    node = {"leaf": "ok"}
    for _ in range(lc._INPUT_MAX_DEPTH + 20):
        node = {"child": node}
    obj, trunc = lc._bound_input(node)   # must NOT raise RecursionError
    assert trunc is True

def test_bound_input_nested_lists_and_dicts():
    obj, trunc = lc._bound_input({"edits": [{"old": "a", "new": "b"}, {"old": "c"}]})
    assert obj["edits"][0] == {"old": "a", "new": "b"}
    assert trunc is False

def test_bound_input_non_dict_returns_none():
    assert lc._bound_input("just a string") == (None, False)
    assert lc._bound_input(None) == (None, False)


# ──────────────────────────────────────────────────────────────────────────
# #217 S1 / U8-G4: the LCS edit-stat over-budget guard. _diff_stat returns None
# (no edit_stat stamped) when len(old_tokens) * len(new_tokens) exceeds
# _EDIT_STAT_LCS_CELL_BUDGET, so a pathological Edit on two huge multi-line sides
# can't peg a CPU on the O(n*m) LCS DP. Untested before #217 S1.
# ──────────────────────────────────────────────────────────────────────────

def _line_token_count(s):
    return len(lc._line_tokens(s))

def test_diff_stat_within_budget_returns_stat():
    # A small Edit is well under the cell budget -> a real {add, del} stat.
    st = lc._diff_stat("alpha\nbeta\n", "alpha\ngamma\n")
    assert st is not None
    assert set(st) == {"add", "del"}

def test_diff_stat_over_budget_returns_none():
    # Build two sides whose token-count PRODUCT exceeds _EDIT_STAT_LCS_CELL_BUDGET
    # (4_000_000 cells). Each side is many short lines; the product of the two line
    # counts blows the budget, so _diff_stat must short-circuit to None (no LCS DP,
    # no edit_stat) rather than allocate/scan a multi-million-cell table.
    budget = lc._EDIT_STAT_LCS_CELL_BUDGET
    side_lines = int(budget ** 0.5) + 50   # so side_lines**2 > budget
    old = "\n".join(f"o{i}" for i in range(side_lines))
    new = "\n".join(f"n{i}" for i in range(side_lines))
    assert _line_token_count(old) * _line_token_count(new) > budget   # genuinely over
    assert lc._diff_stat(old, new) is None

def test_edit_stat_for_edit_over_budget_omits_stamp():
    # End-to-end through _edit_stat_for: a single Edit whose old/new product is
    # over the LCS budget yields no stat (None) -> _normalize stamps no edit_stat.
    budget = lc._EDIT_STAT_LCS_CELL_BUDGET
    side_lines = int(budget ** 0.5) + 50
    old = "\n".join(f"o{i}" for i in range(side_lines))
    new = "\n".join(f"n{i}" for i in range(side_lines))
    assert lc._edit_stat_for("Edit", {"old_string": old, "new_string": new}) is None

def test_multiedit_one_over_budget_edit_omits_whole_stamp():
    # MultiEdit sums per-edit stats, but ONE over-budget edit drops the WHOLE
    # stamp (the header would otherwise undercount the omitted edit).
    budget = lc._EDIT_STAT_LCS_CELL_BUDGET
    side_lines = int(budget ** 0.5) + 50
    big_old = "\n".join(f"o{i}" for i in range(side_lines))
    big_new = "\n".join(f"n{i}" for i in range(side_lines))
    edits = [{"old_string": "a", "new_string": "b"},        # tiny, in-budget
             {"old_string": big_old, "new_string": big_new}]  # over-budget
    assert lc._edit_stat_for("MultiEdit", {"edits": edits}) is None

def test_bound_input_truly_clips_past_total_cap():
    # #217 S1 / U5 (data-contract honesty): the total-cap backstop must actually
    # CLIP, not merely flag. Many leaves EACH below _INPUT_LEAF_CAP but TOGETHER
    # serializing well past _INPUT_TOTAL_CAP previously returned truncated=True
    # while STILL serving the over-cap payload. The bound is now real: the
    # returned dict serializes to <= _INPUT_TOTAL_CAP whenever truncated is True.
    leaf = "L" * (lc._INPUT_LEAF_CAP - 100)   # each leaf is UNDER the per-leaf cap
    n = (lc._INPUT_TOTAL_CAP // len(leaf)) + 5   # but their sum blows the total cap
    many = {f"k{i}": leaf for i in range(n)}
    # sanity: the RAW input is genuinely over the total cap (the test is non-trivial)
    assert len(json.dumps(many, separators=(",", ":"))) > lc._INPUT_TOTAL_CAP
    obj, trunc = lc._bound_input(many)
    assert trunc is True
    # the load-bearing invariant: the SERVED payload is within the total cap.
    assert len(json.dumps(obj, separators=(",", ":"))) <= lc._INPUT_TOTAL_CAP

def test_bound_input_single_giant_leaf_clips_to_total_cap():
    # A single string leaf far past the total cap: the served payload must
    # serialize within _INPUT_TOTAL_CAP (the per-leaf clip alone caps it at
    # _INPUT_LEAF_CAP < _INPUT_TOTAL_CAP, so this stays within cap by leaf-clip;
    # the assertion still holds and guards the post-clip invariant).
    big = "Z" * (lc._INPUT_TOTAL_CAP + 5000)
    obj, trunc = lc._bound_input({"old_string": big})
    assert trunc is True
    assert len(json.dumps(obj, separators=(",", ":"))) <= lc._INPUT_TOTAL_CAP

def test_bound_input_clips_non_ascii_to_total_cap():
    # #217 S1 / U5 round-2 P1: the total-cap clip MUST measure in the SAME
    # serialization form _bound_input checks against and the blocks_json wire uses
    # (separators=(",",":"), DEFAULT ensure_ascii=True). The old clip measured in
    # the ensure_ascii=False form, so for NON-ASCII payloads (each char escapes to
    # 6 wire chars, \uXXXX) the clip stopped ~5x too early — `truncated=True` while
    # the SERVED payload serialized far past the cap. ASCII payloads coincided,
    # which is why the ASCII-only clip tests passed while the invariant was broken.
    leaf = "é" * 100   # 'é' — each escapes to 'é' (6 chars) in the wire form
    many = {f"k{i}": leaf for i in range(400)}
    # sanity: the RAW input is genuinely over the total cap in the WIRE form
    assert len(json.dumps(many, separators=(",", ":"))) > lc._INPUT_TOTAL_CAP
    obj, trunc = lc._bound_input(many)
    assert trunc is True
    # the load-bearing invariant, measured in the wire form (= the blocks_json
    # serialization at _lib_conversation.py:456 and _bound_input's own check):
    assert len(json.dumps(obj, separators=(",", ":"))) <= lc._INPUT_TOTAL_CAP

def test_bound_input_single_giant_non_ascii_leaf_clips_to_total_cap():
    # A single non-ASCII leaf far past the total cap: the per-leaf clip caps it at
    # _INPUT_LEAF_CAP CHARACTERS, but in the wire form each non-ASCII char is 6
    # chars, so a _INPUT_LEAF_CAP-char 'é' leaf serializes to ~6x _INPUT_LEAF_CAP —
    # WELL past the total cap. Only a wire-form total-cap clip brings it within cap.
    big = "é" * (lc._INPUT_TOTAL_CAP + 5000)
    obj, trunc = lc._bound_input({"old_string": big})
    assert trunc is True
    assert len(json.dumps(obj, separators=(",", ":"))) <= lc._INPUT_TOTAL_CAP


# ──────────────────────────────────────────────────────────────────────────
# #177 Session 1: enriched tool_use / tool_result blocks, message-level
# stop_reason + attribution, and the search_aux non-prose index blob.
# ──────────────────────────────────────────────────────────────────────────

def _asst_line(content, **extra):
    base = {"type": "assistant", "uuid": "u1", "sessionId": "s1",
            "message": {"model": "claude", "stop_reason": "end_turn",
                        "content": content}}
    base.update(extra)
    return base

def test_tool_use_block_carries_structured_input_and_summary():
    row = lc.parse_message_row(_asst_line([
        {"type": "tool_use", "name": "Edit", "id": "t1",
         "input": {"file_path": "/a.py", "old_string": "x", "new_string": "y"}}]), 0)
    blocks = json.loads(row.blocks_json)
    tu = [b for b in blocks if b["kind"] == "tool_use"][0]
    assert tu["input"] == {"file_path": "/a.py", "old_string": "x", "new_string": "y"}
    assert tu["input_truncated"] is False
    assert tu["input_summary"]            # legacy field still present
    assert tu["preview"] == "/a.py"       # preview still present

def test_tool_use_input_truncated_flag_on_large_input():
    big = "z" * (lc._INPUT_LEAF_CAP + 10)
    row = lc.parse_message_row(_asst_line([
        {"type": "tool_use", "name": "Write", "id": "t2",
         "input": {"content": big}}]), 0)
    tu = [b for b in json.loads(row.blocks_json) if b["kind"] == "tool_use"][0]
    assert tu["input_truncated"] is True

def test_tool_use_non_dict_input_yields_none_input():
    # a tool_use whose input is not a dict keeps input=None, input_truncated=False
    row = lc.parse_message_row(_asst_line([
        {"type": "tool_use", "name": "X", "id": "t9", "input": "not a dict"}]), 0)
    tu = [b for b in json.loads(row.blocks_json) if b["kind"] == "tool_use"][0]
    assert tu["input"] is None and tu["input_truncated"] is False

def test_tool_result_full_length_and_raised_cap():
    big = "r" * (lc._TOOL_RESULT_CAP + 1234)
    line = {"type": "user", "uuid": "u2", "sessionId": "s1",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": big}]}}
    row = lc.parse_message_row(line, 0)
    tr = [b for b in json.loads(row.blocks_json) if b["kind"] == "tool_result"][0]
    assert len(tr["text"]) == lc._TOOL_RESULT_CAP
    assert tr["truncated"] is True
    assert tr["full_length"] == lc._TOOL_RESULT_CAP + 1234

def test_message_level_fields_land_on_row():
    row = lc.parse_message_row(_asst_line(
        [{"type": "text", "text": "hi"}],
        attributionSkill="superpowers:brainstorming",
        attributionPlugin="superpowers"), 0)
    assert row.stop_reason == "end_turn"
    assert row.attribution_skill == "superpowers:brainstorming"
    assert row.attribution_plugin == "superpowers"

def test_search_columns_include_tool_and_thinking_exclude_prose():
    # #177 S6: the non-prose index split into search_tool (tool input/result) +
    # search_thinking (thinking). Prose stays in text only. (#217 S1 / U7a: the
    # legacy documented-dead search_aux field was removed from MessageRow.)
    row = lc.parse_message_row(_asst_line([
        {"type": "text", "text": "PROSE_ONLY_TOKEN"},
        {"type": "thinking", "thinking": "THINK_TOKEN"},
        {"type": "tool_use", "name": "Bash", "id": "t3",
         "input": {"command": "CMD_TOKEN"}}]), 0)
    assert "THINK_TOKEN" in row.search_thinking
    assert "CMD_TOKEN" in row.search_tool
    assert "PROSE_ONLY_TOKEN" not in row.search_tool   # prose excluded
    assert "PROSE_ONLY_TOKEN" not in row.search_thinking
    assert "PROSE_ONLY_TOKEN" in row.text              # prose still indexed via text
    assert not hasattr(row, "search_aux")              # #217 S1 / U7a: field removed

def test_search_tool_includes_tool_result_text():
    line = {"type": "user", "uuid": "u3", "sessionId": "s1",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "RESULT_TOKEN"}]}}
    row = lc.parse_message_row(line, 0)
    assert "RESULT_TOKEN" in row.search_tool
    assert row.text == ""                              # tool_result zeroes prose, NOT search_tool

def test_search_thinking_caps_thinking_but_block_keeps_full():
    # The thinking search-column entry is capped at _TOOL_RESULT_CAP so the FTS
    # index doesn't double the at-rest cost of large thinking — but the
    # blocks_json thinking block keeps the FULL text for rendering.
    full = "T" * (lc._TOOL_RESULT_CAP + 500)
    row = lc.parse_message_row(_asst_line([{"type": "thinking", "thinking": full}]), 0)
    # the thinking run is the sole search_thinking source here, capped to the cap.
    assert len(row.search_thinking) == lc._TOOL_RESULT_CAP
    # the stored thinking block keeps the FULL text for rendering
    blocks = json.loads(row.blocks_json)
    think = [b for b in blocks if b["kind"] == "thinking"][0]
    assert len(think["text"]) == lc._TOOL_RESULT_CAP + 500


def test_attach_ask_answers_stashes_bounded_answers():
    blocks = [{"kind": "tool_result", "tool_use_id": "t1",
               "text": "...", "is_error": False}]
    obj = {"toolUseResult": {
        "questions": [{"question": "Q?", "header": "H", "options": [], "multiSelect": False}],
        "answers": {"Q?": "Comprehensive"},
        "annotations": {}}}
    lc._attach_ask_answers(blocks, obj)
    assert blocks[0]["ask_answers"] == {"Q?": "Comprehensive"}
    assert "ask_annotations" not in blocks[0]   # empty annotations dropped


def test_attach_ask_answers_noop_without_answers_dict():
    blocks = [{"kind": "tool_result", "tool_use_id": "t1", "is_error": False}]
    lc._attach_ask_answers(blocks, {"toolUseResult": {"foo": "bar"}})
    assert "ask_answers" not in blocks[0]


def test_attach_ask_answers_noop_on_empty_answers():
    # An empty answers dict must NOT stash ask_answers={} — that would set a
    # falsy `answers` on the tool_call and suppress the client's result-text
    # fallback. Symmetric with the empty-annotations drop.
    blocks = [{"kind": "tool_result", "tool_use_id": "t1", "is_error": False}]
    lc._attach_ask_answers(blocks, {"toolUseResult": {"answers": {}}})
    assert "ask_answers" not in blocks[0]


def test_attach_ask_answers_requires_exactly_one_result_block():
    blocks = [{"kind": "tool_result", "tool_use_id": "t1"},
              {"kind": "tool_result", "tool_use_id": "t2"}]
    lc._attach_ask_answers(blocks, {"toolUseResult": {"answers": {"Q": "A"}}})
    assert all("ask_answers" not in b for b in blocks)   # ambiguous -> no-op


def test_attach_ask_answers_bounds_pathological_value():
    big = "x" * 50_000
    blocks = [{"kind": "tool_result", "tool_use_id": "t1"}]
    lc._attach_ask_answers(blocks, {"toolUseResult": {"answers": {"Q": big}}})
    # _bound_input clips a string leaf to _INPUT_LEAF_CAP (8000)
    assert len(blocks[0]["ask_answers"]["Q"]) == lc._INPUT_LEAF_CAP


def test_attach_ask_answers_keeps_nonempty_annotations():
    blocks = [{"kind": "tool_result", "tool_use_id": "t1"}]
    lc._attach_ask_answers(blocks, {"toolUseResult": {
        "answers": {"Q": "A"}, "annotations": {"Q": {"notes": "n"}}}})
    assert blocks[0]["ask_annotations"] == {"Q": {"notes": "n"}}


def test_attach_task_meta_create_stashes_task_id():
    blocks = [{"kind": "tool_result", "tool_use_id": "t1", "text": "Task #1 created"}]
    obj = {"toolUseResult": {"task": {"id": "1", "subject": "Explore project context"}}}
    lc._attach_task_meta(blocks, obj)
    assert blocks[0]["task_id"] == "1"
    assert "task_list" not in blocks[0]


def test_attach_task_meta_update_stashes_task_id():
    blocks = [{"kind": "tool_result", "tool_use_id": "t2", "text": "Updated task #1 status"}]
    obj = {"toolUseResult": {"success": True, "taskId": "1",
                             "statusChange": {"from": "pending", "to": "in_progress"}}}
    lc._attach_task_meta(blocks, obj)
    assert blocks[0]["task_id"] == "1"


def test_attach_task_meta_list_stashes_snapshot():
    # NOTE (reviewer adj. 1): TaskList toolUseResult shape VERIFIED against real
    # transcripts (e.g. 82f63fb2-.../2a66a114-...): {"tasks":[{id,subject,status,
    # blockedBy}]}, ids are strings, status in pending|in_progress|completed.
    blocks = [{"kind": "tool_result", "tool_use_id": "t3", "text": "..."}]
    obj = {"toolUseResult": {"tasks": [
        {"id": "1", "subject": "A", "status": "completed", "blockedBy": []},
        {"id": "2", "subject": "B", "status": "in_progress", "blockedBy": []}]}}
    lc._attach_task_meta(blocks, obj)
    assert blocks[0]["task_list"] == [
        {"id": "1", "subject": "A", "status": "completed"},
        {"id": "2", "subject": "B", "status": "in_progress"}]


def test_attach_task_meta_noop_without_task_fields():
    blocks = [{"kind": "tool_result", "tool_use_id": "t4"}]
    lc._attach_task_meta(blocks, {"toolUseResult": {"foo": "bar"}})
    assert "task_id" not in blocks[0] and "task_list" not in blocks[0]


def test_attach_task_meta_requires_single_result_block():
    blocks = [{"kind": "tool_result", "tool_use_id": "a"},
              {"kind": "tool_result", "tool_use_id": "b"}]
    lc._attach_task_meta(blocks, {"toolUseResult": {"task": {"id": "1"}}})
    assert all("task_id" not in b for b in blocks)


# Subagent Task tools record toolUseResult=null and put the identity in the
# human-readable result string ("Task #7 created successfully: ..." / "Updated
# task #3 status"); the structured toolUseResult shape is main-session only. The
# string-content fallback recovers the id from block["text"] (subject/status are
# read from the call input at fold time). Shapes verified against real subagent
# transcripts (Claude Code 2.1.173, e.g. bce455df-.../subagents/agent-*.jsonl).
def test_attach_task_meta_create_string_shape_stashes_id():
    blocks = [{"kind": "tool_result", "tool_use_id": "c7",
               "text": "Task #7 created successfully: Read path: _turn_usage_map"}]
    lc._attach_task_meta(blocks, {"toolUseResult": None})
    assert blocks[0]["task_id"] == "7"
    assert "task_list" not in blocks[0]


def test_attach_task_meta_update_string_shape_stashes_id():
    blocks = [{"kind": "tool_result", "tool_use_id": "u3", "text": "Updated task #3 status"}]
    lc._attach_task_meta(blocks, {"toolUseResult": None})
    assert blocks[0]["task_id"] == "3"


def test_attach_task_meta_structured_precedes_string():
    # When the structured shape is present it wins, even if the result text also
    # looks like a string-shape line with a DIFFERENT id.
    blocks = [{"kind": "tool_result", "tool_use_id": "c1",
               "text": "Task #99 created successfully: decoy"}]
    lc._attach_task_meta(blocks, {"toolUseResult": {"task": {"id": "5", "subject": "real"}}})
    assert blocks[0]["task_id"] == "5"


def test_attach_task_meta_string_shape_ignores_unrelated_text():
    blocks = [{"kind": "tool_result", "tool_use_id": "x1",
               "text": "Some unrelated tool output mentioning Task #7 mid-sentence"}]
    lc._attach_task_meta(blocks, {"toolUseResult": None})
    assert "task_id" not in blocks[0] and "task_list" not in blocks[0]


# --- #177 S3: additive Bash stderr/interrupted capture --------------------
# Self-identifying off the Bash-shaped toolUseResult ({stdout,stderr,...}); we
# store only the DELTA over the merged result.text (stderr + interrupted), never
# stdout (result.text already == stdout+stderr, so storing it would double the
# payload). Parser-private keys bash_stderr/bash_interrupted are popped in the
# query layer's Phase 1, so they never leak into emitted/orphan blocks.
def test_attach_bash_streams_captures_stderr_only_not_interrupted():
    blocks = [{"kind": "tool_result", "tool_use_id": "toolu_bash1",
               "text": "out\nboom", "is_error": True}]
    obj = {"toolUseResult": {"stdout": "out\n", "stderr": "boom",
                             "interrupted": False}}
    lc._attach_bash_streams(blocks, obj)
    assert blocks[0]["bash_stderr"] == "boom"
    assert "bash_interrupted" not in blocks[0]   # interrupted False -> not stamped


def test_attach_bash_streams_captures_interrupted():
    blocks = [{"kind": "tool_result", "tool_use_id": "toolu_bash2", "text": "x"}]
    obj = {"toolUseResult": {"stdout": "x", "stderr": "", "interrupted": True}}
    lc._attach_bash_streams(blocks, obj)
    # empty stderr -> no bash_stderr; interrupted True -> stamped
    assert "bash_stderr" not in blocks[0]
    assert blocks[0]["bash_interrupted"] is True


def test_attach_bash_streams_noop_on_non_bash_tooluseresult():
    # An AskUserQuestion-shaped toolUseResult (no stdout/stderr keys) must NOT
    # add any bash_* keys.
    blocks = [{"kind": "tool_result", "tool_use_id": "t", "text": "x"}]
    lc._attach_bash_streams(blocks, {"toolUseResult": {"answers": {"Q": "A"}}})
    assert "bash_stderr" not in blocks[0] and "bash_interrupted" not in blocks[0]


def test_attach_bash_streams_noop_on_empty_stderr_and_not_interrupted():
    blocks = [{"kind": "tool_result", "tool_use_id": "t", "text": "x"}]
    lc._attach_bash_streams(blocks, {"toolUseResult": {
        "stdout": "x", "stderr": "", "interrupted": False}})
    assert "bash_stderr" not in blocks[0] and "bash_interrupted" not in blocks[0]


def test_attach_bash_streams_requires_single_result_block():
    blocks = [{"kind": "tool_result", "tool_use_id": "a"},
              {"kind": "tool_result", "tool_use_id": "b"}]
    lc._attach_bash_streams(blocks, {"toolUseResult": {
        "stdout": "o", "stderr": "boom"}})
    assert all("bash_stderr" not in b for b in blocks)   # ambiguous -> no-op


def test_attach_bash_streams_bounds_pathological_stderr():
    big = "e" * (lc._TOOL_RESULT_CAP + 500)
    blocks = [{"kind": "tool_result", "tool_use_id": "t"}]
    lc._attach_bash_streams(blocks, {"toolUseResult": {
        "stdout": "o", "stderr": big}})
    assert len(blocks[0]["bash_stderr"]) == lc._TOOL_RESULT_CAP


def test_attach_bash_streams_no_stdout_stored():
    # We deliberately never store stdout (result.text already covers it).
    blocks = [{"kind": "tool_result", "tool_use_id": "t", "text": "out\nboom"}]
    lc._attach_bash_streams(blocks, {"toolUseResult": {
        "stdout": "out\n", "stderr": "boom", "interrupted": False}})
    assert "bash_stdout" not in blocks[0] and "stdout" not in blocks[0]


def test_iter_message_rows_attaches_bash_streams_at_call_site():
    # Full ingest path: the call site in _normalize's tool_result branch must
    # fire _attach_bash_streams (mirrors test_tool_result_captures_agent_id_…).
    fh = _jsonl({"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "t",
                 "toolUseResult": {"stdout": "out\n", "stderr": "boom",
                                   "interrupted": True},
                 "message": {"role": "user",
                             "content": [{"type": "tool_result",
                                          "content": "out\nboom",
                                          "tool_use_id": "tu1", "is_error": True}]}})
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    tr = [b for b in json.loads(r.blocks_json) if b["kind"] == "tool_result"][0]
    assert tr["bash_stderr"] == "boom"
    assert tr["bash_interrupted"] is True


# ---- #177 S4: media placeholders + web captures ----

def test_iter_media_items_ordinals_skip_non_media():
    content = [{"type": "text", "text": "t"},
               {"type": "image", "source": {"media_type": "image/png", "data": "AA=="}},
               "junk", {"type": "tool_use"},
               {"type": "document", "source": {"media_type": "application/pdf", "data": "BB=="}},
               {"type": "image", "source": {"media_type": "image/jpeg", "data": "CC=="}}]
    got = list(lc.iter_media_items(content))
    assert [(i, m["type"]) for i, m in got] == [(0, "image"), (1, "document"), (2, "image")]
    assert lc.iter_media_items("not a list") is not None  # generator; yields nothing
    assert list(lc.iter_media_items("not a list")) == []


def test_tool_result_media_placeholders_with_ordinals():
    fh = _jsonl({"type": "user", "uuid": "u9", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "tu1", "content": [
                         {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 100}},
                         {"type": "text", "text": "took the screenshot"},
                         {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "B" * 40}}]}]}})
    b = json.loads(list(lc.iter_message_rows(fh, "f"))[0].blocks_json)[0]
    assert b["kind"] == "tool_result" and b["text"] == "took the screenshot"
    assert b["media"] == [
        {"kind": "image", "media_type": "image/png", "bytes": 100, "index": 0},
        {"kind": "document", "media_type": "application/pdf", "bytes": 40, "index": 1}]


def test_tool_result_without_media_omits_key():
    fh = _jsonl({"type": "user", "uuid": "u10", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "tu1", "content": "plain"}]}})
    b = json.loads(list(lc.iter_message_rows(fh, "f"))[0].blocks_json)[0]
    assert "media" not in b


def test_user_content_media_carry_index():
    fh = _jsonl({"type": "user", "uuid": "u11", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user", "content": [
                     {"type": "text", "text": "see attached"},
                     {"type": "image", "source": {"media_type": "image/png", "data": "AAAA"}},
                     {"type": "document", "source": {"media_type": "application/pdf", "data": "BBBB"}}]}})
    blocks = json.loads(list(lc.iter_message_rows(fh, "f"))[0].blocks_json)
    assert blocks[1] == {"kind": "image", "media_type": "image/png", "bytes": 4, "index": 0}
    assert blocks[2] == {"kind": "document", "media_type": "application/pdf", "bytes": 4, "index": 1}


def _web_search_line(tur, content="results text"):
    return {"type": "user", "uuid": "w1", "sessionId": "s", "timestamp": "t",
            "toolUseResult": tur,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tw1", "content": content}]}}


def test_web_search_capture_flattens_and_bounds():
    tur = {"query": "q1", "results": [
        {"content": [{"title": "T1", "url": "https://a.example/x"},
                     {"notalink": True},
                     {"title": "T2", "url": "https://b.example/y"}]},
        "stray string",
        {"content": [{"title": "T3" * 200, "url": "https://c.example/" + "z" * 3000}]}]}
    b = json.loads(list(lc.iter_message_rows(_jsonl(_web_search_line(tur)), "f"))[0].blocks_json)[0]
    ws = b["web_search"]
    assert ws["query"] == "q1"
    assert [l["title"] for l in ws["links"][:2]] == ["T1", "T2"]
    assert len(ws["links"][2]["title"]) == lc._WEB_LINK_TITLE_CAP
    assert len(ws["links"][2]["url"]) == lc._WEB_LINK_URL_CAP
    assert "links_truncated" not in ws


def test_web_search_capture_link_cap_sets_truncated():
    links = [{"title": f"T{i}", "url": f"https://e.example/{i}"} for i in range(60)]
    tur = {"query": "q", "results": [{"content": links}]}
    b = json.loads(list(lc.iter_message_rows(_jsonl(_web_search_line(tur)), "f"))[0].blocks_json)[0]
    assert len(b["web_search"]["links"]) == lc._WEB_SEARCH_LINK_CAP
    assert b["web_search"]["links_truncated"] is True


def test_web_search_no_stamp_on_shape_mismatch_or_multi_result():
    # non-dict toolUseResult
    line = _web_search_line("Error: nope")
    b = json.loads(list(lc.iter_message_rows(_jsonl(line), "f"))[0].blocks_json)[0]
    assert "web_search" not in b
    # two tool_result blocks -> exactly-one guard refuses
    line2 = {"type": "user", "uuid": "w2", "sessionId": "s", "timestamp": "t",
             "toolUseResult": {"query": "q", "results": []},
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "a", "content": "1"},
                 {"type": "tool_result", "tool_use_id": "b", "content": "2"}]}}
    blocks = json.loads(list(lc.iter_message_rows(_jsonl(line2), "f"))[0].blocks_json)
    assert all("web_search" not in b for b in blocks)


def test_web_fetch_capture_triple_and_mismatch():
    line = _web_search_line({"bytes": 13218, "code": 200, "codeText": "OK", "result": "# md"})
    b = json.loads(list(lc.iter_message_rows(_jsonl(line), "f"))[0].blocks_json)[0]
    assert b["web_fetch"] == {"code": 200, "code_text": "OK"}
    # missing codeText -> no stamp; bare string -> no stamp
    for tur in ({"code": 200, "result": "x"}, "Error: Request failed"):
        b2 = json.loads(list(lc.iter_message_rows(_jsonl(_web_search_line(tur)), "f"))[0].blocks_json)[0]
        assert "web_fetch" not in b2


# === #177 S6: _derive_search_columns chokepoint (Task 1a) ===

def test_derive_search_columns_splits_kinds():
    blocks = [
        {"kind": "text", "text": "prose stays out"},
        {"kind": "thinking", "text": "let me reason"},
        {"kind": "tool_use", "name": "Bash", "input": {"command": "npm run build"}},
        {"kind": "tool_result", "text": "vite built ok", "bash_stderr": "warn: chunk big"},
    ]
    tool, think = lc._derive_search_columns(blocks)
    assert "npm run build" in tool and "vite built ok" in tool and "warn: chunk big" in tool
    assert think == "let me reason"
    assert "prose stays out" not in tool and "prose stays out" not in think


def test_derive_search_columns_caps_per_block_and_handles_answers():
    big = "x" * (lc._TOOL_RESULT_CAP + 100)
    blocks = [
        {"kind": "thinking", "text": big},
        {"kind": "thinking", "text": "second"},
        {"kind": "tool_result", "text": "r", "answers": {"Q": "picked option B"}},
    ]
    tool, think = lc._derive_search_columns(blocks)
    assert len(think.split("\n")[0]) == lc._TOOL_RESULT_CAP   # per-block cap, not whole-column
    assert "second" in think
    assert "picked option B" in tool


def test_derive_search_columns_indexes_real_ask_answers_key():
    # Real ingest stamps `ask_answers`/`ask_annotations` (not raw `answers`); the
    # chokepoint must index those too so AskUserQuestion choices stay searchable.
    blocks = [
        {"kind": "tool_result", "text": "r",
         "ask_answers": {"Q": "chose path A"},
         "ask_annotations": {"note": "rationale text"}},
    ]
    tool, _ = lc._derive_search_columns(blocks)
    assert "chose path A" in tool and "rationale text" in tool


def test_derive_search_columns_empty_and_malformed():
    assert lc._derive_search_columns([]) == ("", "")
    assert lc._derive_search_columns([None, 42, {"kind": "text"}]) == ("", "")
    assert lc._derive_search_columns("not a list") == ("", "")


def test_ingest_search_columns_match_chokepoint_post_augment():
    # Parity pin: row.search_tool/search_thinking equal _derive_search_columns on
    # the FINAL post-augment blocks (blocks_json). A Bash tool_result carrying
    # toolUseResult.stderr proves the derivation runs AFTER _attach_bash_streams
    # (a pre-augment call site would drop the stderr and make this RED).
    line = {"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "t",
            "toolUseResult": {"stdout": "out\n", "stderr": "stderr-needle",
                              "interrupted": False},
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tb",
                 "content": [{"type": "text", "text": "out\nstderr-needle"}],
                 "is_error": True}]}}
    row = list(lc.iter_message_rows(_jsonl(line), "f"))[0]
    exp_tool, exp_think = lc._derive_search_columns(json.loads(row.blocks_json))
    assert row.search_tool == exp_tool
    assert row.search_thinking == exp_think
    assert "stderr-needle" in row.search_tool   # post-augment: stderr indexed


def test_ingest_thinking_and_tool_use_columns():
    line = {"type": "assistant", "uuid": "a1", "sessionId": "s1",
            "requestId": "r1", "timestamp": "t",
            "message": {"role": "assistant", "id": "m1", "model": "opus",
                        "content": [
                            {"type": "thinking", "thinking": "ponder deeply"},
                            {"type": "text", "text": "visible"},
                            {"type": "tool_use", "name": "Bash",
                             "input": {"command": "rg needle"}}]}}
    row = list(lc.iter_message_rows(_jsonl(line), "f"))[0]
    assert "rg needle" in row.search_tool
    assert row.search_thinking == "ponder deeply"
    assert row.text == "visible"
    assert "visible" not in row.search_tool and "visible" not in row.search_thinking


def test_compaction_summary_is_meta_not_human():
    body = ("This session is being continued from a previous conversation that "
            "ran out of context.\n\nSummary:\n1. ...")
    fh = _jsonl({"type": "user", "uuid": "c1", "sessionId": "s", "timestamp": "t",
                 "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
                 "message": {"role": "user", "content": body}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.META
    assert r.text == ""                              # kept out of FTS / titles
    assert "continued from a previous" in json.loads(r.blocks_json)[0]["text"]


def test_task_notification_is_meta_not_human():
    body = ("<task-notification>\n<task-id>behvnfnjj</task-id>\n<status>completed</status>\n"
            "<summary>Background command \"Run tests\" completed (exit code 0)</summary>\n"
            "</task-notification>\nRead the output file to retrieve the result: /tmp/x")
    fh = _jsonl({"type": "user", "uuid": "n1", "sessionId": "s", "timestamp": "t",
                 "origin": {"kind": "task-notification"}, "promptSource": "system",
                 "message": {"role": "user", "content": body}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.META and r.text == ""


def test_bash_notification_is_meta_not_human():
    body = ("<bash-notification>\n<shell-id>b0b7c55</shell-id>\n<status>completed</status>\n"
            "<summary>Background command \"Start dev server\" completed (exit code 0).</summary>\n"
            "Read the output file to retrieve the output.\n</bash-notification>")
    fh = _jsonl({"type": "user", "uuid": "n2", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user", "content": body}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.META and r.text == ""


def test_bash_input_echo_is_meta_not_human():
    fh = _jsonl({"type": "user", "uuid": "b1", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user", "content": "<bash-input>pwd</bash-input>"}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.META and r.text == ""


def test_remote_control_prefix_stripped_but_stays_human():
    body = "<system-reminder>Message sent at Sat 2026-06-13 10:47:42 UTC.</system-reminder>\nApproved."
    fh = _jsonl({"type": "user", "uuid": "rc1", "sessionId": "s", "timestamp": "t",
                 "promptSource": "queued",
                 "message": {"role": "user", "content": body}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.HUMAN
    assert r.text == "Approved."                     # the system-reminder stamp is gone


def test_p1a_unclosed_task_notification_tag_stays_human():
    # a real prompt that merely STARTS with the literal tag but is not a
    # well-formed closed wrapper is NOT folded (Codex P1a)
    fh = _jsonl({"type": "user", "uuid": "neg1", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user",
                             "content": "<task-notification> what does this tag even do?"}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.HUMAN


def test_p1b_bash_echo_with_command_args_substring_not_promoted():
    # Codex P1b regression: keep bash echoes OUT of _MARKER_TAGS. A bash echo
    # whose body merely CONTAINS a literal <command-args> substring must classify
    # META via the dedicated bash-echo branch. (If bash-* were instead added to
    # _MARKER_TAGS — the rejected approach — _is_system_marker would be True and
    # _extract_command_invocation would find the inner <command-args> and
    # mis-promote this to a HUMAN turn with text="x". The dedicated branch plus
    # the unchanged _MARKER_TAGS is what prevents that.)
    fh = _jsonl({"type": "user", "uuid": "neg2", "sessionId": "s", "timestamp": "t",
                 "message": {"role": "user",
                             "content": "<bash-input>echo '<command-args>x</command-args>'</bash-input>"}})
    r = list(lc.iter_message_rows(fh, "f"))[0]
    assert r.entry_type == lc.META and r.text == ""


# ---------------------------------------------------------------------------
# #193: ai-title parser (parse_ai_title + iter_ai_titles)
# ---------------------------------------------------------------------------

def test_parse_ai_title_accepts_nonempty():
    row = lc.parse_ai_title({"type": "ai-title", "aiTitle": "My Title", "sessionId": "s1"}, 42)
    assert row is not None
    assert (row.session_id, row.ai_title, row.byte_offset) == ("s1", "My Title", 42)


def test_parse_ai_title_rejects_null_blank_and_non_aititle():
    assert lc.parse_ai_title({"type": "ai-title", "aiTitle": None, "sessionId": "s1"}, 0) is None
    assert lc.parse_ai_title({"type": "ai-title", "aiTitle": "  ", "sessionId": "s1"}, 0) is None
    assert lc.parse_ai_title({"type": "ai-title", "aiTitle": "T", "sessionId": ""}, 0) is None
    assert lc.parse_ai_title({"type": "ai-title", "aiTitle": "T"}, 0) is None          # no sessionId
    assert lc.parse_ai_title({"type": "assistant", "aiTitle": "T", "sessionId": "s"}, 0) is None


def test_iter_ai_titles_yields_in_file_order():
    fh = io.StringIO(
        '{"type":"ai-title","aiTitle":"first","sessionId":"s1"}\n'
        '{"type":"user","uuid":"u1","message":{"content":"hi"}}\n'
        '{"type":"ai-title","aiTitle":null,"sessionId":"s1"}\n'
        '{"type":"ai-title","aiTitle":"second","sessionId":"s1"}\n'
    )
    titles = [r.ai_title for r in lc.iter_ai_titles(fh, "s.jsonl")]
    assert titles == ["first", "second"]   # null skipped; file order preserved


# --- #198: true edit-family stat stamped at ingest under truncation ----------
# DiffCard's header badge (`wrote N lines` for Write, `+A −D` for Edit/MultiEdit)
# was computed client-side from the BOUNDED input, so a >_INPUT_LEAF_CAP leaf made
# it report the post-clip count. The parser now stamps `edit_stat` (computed from
# the FULL input) on truncated edit-family tool_use blocks. Counts match jsdiff's
# Myers-minimal diff exactly: add = |new_lines| − LCS, del = |old_lines| − LCS,
# and the LCS *length* is unique (a plain LCS pass reproduces jsdiff's counts).

def test_line_count_matches_frontend_splitLines():
    # Mirror dashboard/web/src/conversations/computeDiff.ts::splitLines length.
    assert lc._line_count("") == 0
    assert lc._line_count("a") == 1
    assert lc._line_count("a\n") == 1          # trailing newline drops the phantom blank
    assert lc._line_count("a\nb") == 2
    assert lc._line_count("a\nb\n") == 2
    assert lc._line_count("\n") == 1           # a lone newline is one (blank) line
    assert lc._line_count("a\r\nb") == 2       # CRLF still splits on \n


def test_diff_stat_matches_jsdiff_counts():
    assert lc._diff_stat("a\nb\nc", "a\nb\nc") == {"add": 0, "del": 0}
    # one line changed: LCS = {a, c} = 2 → add 1, del 1.
    assert lc._diff_stat("a\nb\nc", "a\nX\nc") == {"add": 1, "del": 1}
    assert lc._diff_stat("", "a\nb") == {"add": 2, "del": 0}
    assert lc._diff_stat("a\nb", "") == {"add": 0, "del": 2}
    # no-newline-at-eof edge: jsdiff line tokens carry the trailing \n, so "a\n"
    # and "a" are distinct tokens → 1 add + 1 del (NOT a no-op).
    assert lc._diff_stat("a\n", "a") == {"add": 1, "del": 1}


def _ingest_one_assistant(content_blocks):
    fh = _jsonl({"type": "assistant", "uuid": "a1", "sessionId": "s1",
                 "requestId": "r1", "timestamp": "t",
                 "message": {"role": "assistant", "id": "m1", "model": "opus",
                             "content": content_blocks}})
    row = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    return json.loads(row.blocks_json)


def test_write_truncated_stamps_full_line_count():
    full = "\n".join("L%04d" % i + "x" * 40 for i in range(300))   # 300 lines, >8 KB
    assert len(full) > lc._INPUT_LEAF_CAP
    blocks = _ingest_one_assistant([
        {"type": "tool_use", "name": "Write", "id": "t1",
         "input": {"file_path": "/big.md", "content": full}}])
    b = blocks[0]
    assert b["input_truncated"] is True                 # leaf was clipped
    assert len(b["input"]["content"]) == lc._INPUT_LEAF_CAP
    # The bug: header would show the clipped line count; the fix stamps the true total.
    assert b["edit_stat"] == {"add": 300, "del": 0}


def test_edit_truncated_stamps_full_diff_stat():
    new = "\n".join("N%04d" % i + "y" * 40 for i in range(250))    # 250 fresh lines, >8 KB
    assert len(new) > lc._INPUT_LEAF_CAP
    blocks = _ingest_one_assistant([
        {"type": "tool_use", "name": "Edit", "id": "t2",
         "input": {"file_path": "/x.py", "old_string": "a\nb\nc", "new_string": new}}])
    b = blocks[0]
    assert b["input_truncated"] is True
    # disjoint line sets → LCS 0 → add = 250, del = 3.
    assert b["edit_stat"] == {"add": 250, "del": 3}


def test_multiedit_truncated_sums_full_per_edit_stats():
    big_new = "\n".join("M%04d" % i + "z" * 20 for i in range(400))  # 400 fresh lines, >8 KB
    assert len(big_new) > lc._INPUT_LEAF_CAP
    blocks = _ingest_one_assistant([
        {"type": "tool_use", "name": "MultiEdit", "id": "t3",
         "input": {"file_path": "/x.py", "edits": [
             {"old_string": "p\n", "new_string": "p\nq\n"},        # +1 / -0 (LCS keeps "p\n")
             {"old_string": "u\nv\n", "new_string": big_new},      # +400 / -2 (disjoint)
         ]}}])
    b = blocks[0]
    assert b["input_truncated"] is True
    assert b["edit_stat"] == {"add": 401, "del": 2}


def test_untruncated_edit_omits_edit_stat():
    # Small input → not truncated → no stamp (client uses the live jsdiff hunks so
    # header==body parity holds; the stamp exists ONLY where the body is partial).
    blocks = _ingest_one_assistant([
        {"type": "tool_use", "name": "Write", "id": "t4",
         "input": {"file_path": "/s.txt", "content": "a\nb\nc"}}])
    assert "edit_stat" not in blocks[0]


def test_truncated_non_edit_tool_omits_edit_stat():
    # A non-edit tool (Bash) with a >cap leaf is truncated but carries no edit_stat.
    blocks = _ingest_one_assistant([
        {"type": "tool_use", "name": "Bash", "id": "t5",
         "input": {"command": "x" * (lc._INPUT_LEAF_CAP + 10)}}])
    assert blocks[0]["input_truncated"] is True
    assert "edit_stat" not in blocks[0]


# --- queued-while-busy user prompts (attachment / queued_command) -------------
# A message typed while the agent (main session OR a subagent) is still working
# is QUEUED and persisted as an `attachment` row — never a `type:"user"` turn —
# with the text in attachment.prompt. The parser promotes the user-typed ones
# (commandMode=="prompt") to a synthetic HUMAN turn so the reader renders them.

def _queued_command(prompt, command_mode="prompt", uuid="q1"):
    return {"type": "attachment", "uuid": uuid, "parentUuid": "p0",
            "isSidechain": False, "sessionId": "s1",
            "timestamp": "2026-06-16T08:01:37.588Z", "cwd": "/x", "gitBranch": "main",
            "attachment": {"type": "queued_command", "prompt": prompt,
                           "commandMode": command_mode}}

def test_queued_prompt_promoted_to_human():
    fh = _jsonl(_queued_command("Don't run the Codex review. I'll do it this time."))
    rows = list(lc.iter_message_rows(fh, "f.jsonl"))
    assert len(rows) == 1
    r = rows[0]
    assert r.entry_type == lc.HUMAN
    assert r.text == "Don't run the Codex review. I'll do it this time."
    assert r.uuid == "q1" and r.session_id == "s1" and r.parent_uuid == "p0"
    assert r.is_sidechain == 0
    # The text lives in attachment.prompt, not message.content — the parser
    # synthesizes the prose block so the reader renders the "YOU" turn.
    blocks = json.loads(r.blocks_json)
    assert blocks == [{"kind": "text",
                       "text": "Don't run the Codex review. I'll do it this time."}]

def test_queued_task_notification_dropped():
    # commandMode=="task-notification" is harness-injected background plumbing,
    # NOT user-typed — it stays dropped (the proof the prompt gate is non-vacuous:
    # flip commandMode and the row vanishes).
    notif = "<task-notification>\n<task-id>ac20b</task-id>\n<status>completed</status>\n</task-notification>"
    fh = _jsonl(_queued_command(notif, command_mode="task-notification"))
    assert list(lc.iter_message_rows(fh, "f.jsonl")) == []

def test_queued_blank_prompt_dropped():
    fh = _jsonl(_queued_command("   "))
    assert list(lc.iter_message_rows(fh, "f.jsonl")) == []

def test_non_queued_attachment_dropped():
    # Other attachment subtypes (hook_success, task_reminder, file, …) are not
    # user turns and must not be promoted.
    fh = _jsonl({"type": "attachment", "uuid": "a1", "sessionId": "s1",
                 "attachment": {"type": "hook_success", "content": "ok"}})
    assert list(lc.iter_message_rows(fh, "f.jsonl")) == []

def test_queued_slash_command_args_promoted():
    # A queued prompt routes through the SAME _normalize as a typed turn, so a
    # slash-command invocation in the queued text still promotes its args (#188).
    fh = _jsonl(_queued_command(
        "<command-name>/commit</command-name><command-args>ship it</command-args>"))
    r = list(lc.iter_message_rows(fh, "f.jsonl"))[0]
    assert r.entry_type == lc.HUMAN
    assert r.text == "ship it"
