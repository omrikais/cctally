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
    assert doc["kind"] == "document" and doc["media_type"] == "application/pdf"
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
