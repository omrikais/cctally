import io, json, sys, pathlib
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
