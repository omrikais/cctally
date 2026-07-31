"""`background_result` full-payload mode (spec 2026-07-31 §3).

A public ``which="result"`` lookup finds the PLACEHOLDER tool_result first and
``read_full_payload`` then reads ``message.content``, which an attachment record
does not have — so a recovered background result had no working "load full
response" route at all. ``background_result`` is an INTERNAL mode: the response
keeps the public ``which: "result"`` discriminant, so the client contract and
the endpoint's input surface are unchanged.
"""
import json
import sqlite3
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db          # noqa: E402
import _lib_conversation_query as cq   # noqa: E402
import _lib_conversation as lc    # noqa: E402
import _lib_conversation_dispatch as disp   # noqa: E402

import pytest                     # noqa: E402


@pytest.fixture(autouse=True)
def _clear_assembly_memo():
    cq._assemble_memo_clear()
    yield


def _conn():
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)
    return c


_COLS = ("session_id", "uuid", "source_path", "byte_offset", "timestamp_utc",
         "entry_type", "text", "blocks_json", "msg_id", "req_id", "search_tool")


def _msg(c, **kw):
    row = {k: kw.get(k) for k in _COLS}
    row["text"] = kw.get("text", "")
    row["search_tool"] = kw.get("search_tool", "")
    c.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,"
        " text,blocks_json,msg_id,req_id,is_sidechain,search_tool,search_thinking)"
        " VALUES(:session_id,:uuid,:source_path,:byte_offset,:timestamp_utc,"
        ":entry_type,:text,:blocks_json,:msg_id,:req_id,0,:search_tool,'')", row)


def _placeholder(task_id):
    return (
        f'MCP tool "codex/codex" is still running after 120s. It was moved to '
        f'the background as task {task_id} and keeps running; you\'ll receive a '
        f'notification with the result when it completes. To stop it, use '
        f'TaskStop with task_id "{task_id}".'
    )


def _notification_body(task_id, result, status="completed"):
    return ("<task-notification>\n"
            f"<task-id>{task_id}</task-id>\n"
            f"<status>{status}</status>\n"
            f"<summary>MCP task {task_id[:-1]} done.</summary>\n"
            f"<result>{result}</result>\n"
            "</task-notification>")


def _attachment_line(task_id, result, uuid="u_notif", status="completed"):
    return json.dumps({
        "type": "attachment", "uuid": uuid, "sessionId": "s1",
        "timestamp": "2026-07-30T20:51:16.312Z",
        "attachment": {"type": "queued_command",
                       "commandMode": "task-notification",
                       "prompt": _notification_body(task_id, result, status)},
    }, ensure_ascii=False)


def _notification_blocks(task_id, result, status="completed"):
    """The bounded block `_background_notification_row` writes at ingest."""
    row = lc.parse_message_row(
        json.loads(_attachment_line(task_id, result, status=status)), 0)
    return row.blocks_json


def _placeholder_line(task_id, tool_use_id):
    """The placeholder's own JSONL line. It is a REAL, re-readable tool_result —
    which is exactly why a route that resolves `which="result"` naively serves
    "still running after 120s" as the full response instead of failing."""
    return json.dumps({
        "type": "user", "uuid": "u1", "sessionId": "s1",
        "timestamp": "2026-07-30T20:42:00Z",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": _placeholder(task_id)}]}}, ensure_ascii=False)


def _seed(tmp_path, *, duplicate_rows=False, drop_notification=False,
          result="R", task_id="kravg1b9s", tool_use_id="toolu_A"):
    """A backgrounded call + placeholder + (optionally) its notification row,
    with BOTH real JSONL lines written to disk (separate files, so a test that
    rewrites the notification file cannot disturb the placeholder's offset)."""
    c = _conn()
    ph_jsonl = tmp_path / "placeholder.jsonl"
    ph_jsonl.write_text(_placeholder_line(task_id, tool_use_id) + "\n",
                        encoding="utf-8")
    jsonl = tmp_path / "session.jsonl"
    lines = [_attachment_line(task_id, result)]
    if duplicate_rows:
        # A replay writes the SAME logical message again, physically distinct
        # and carrying DIFFERENT text. Assembly keeps the earliest
        # (timestamp_utc, id) after uuid dedup, so this must never win.
        lines.append(_attachment_line(task_id, "REPLAY-LOSER"))
    with open(jsonl, "w", encoding="utf-8") as fh:
        offsets = []
        for line in lines:
            offsets.append(fh.tell())
            fh.write(line + "\n")

    _msg(c, session_id="s1", uuid="a1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-07-30T20:40:00Z", entry_type="assistant",
         msg_id="m1", req_id="r1",
         blocks_json=json.dumps([{"kind": "tool_use", "name": "mcp__codex__codex",
                                  "input_summary": "{}", "id": tool_use_id,
                                  "preview": "codex"}]))
    _msg(c, session_id="s1", uuid="u1", source_path=str(ph_jsonl), byte_offset=0,
         timestamp_utc="2026-07-30T20:42:00Z", entry_type="tool_result",
         blocks_json=json.dumps([{"kind": "tool_result",
                                  "text": _placeholder(task_id),
                                  "truncated": False, "full_length": 300,
                                  "is_error": False, "tool_use_id": tool_use_id}]))
    if not drop_notification:
        _msg(c, session_id="s1", uuid="u_notif", source_path=str(jsonl),
             byte_offset=offsets[0], timestamp_utc="2026-07-30T20:51:16.312Z",
             entry_type="meta",
             blocks_json=_notification_blocks(task_id, result))
        if duplicate_rows:
            _msg(c, session_id="s1", uuid="u_notif", source_path=str(jsonl),
                 byte_offset=offsets[1], timestamp_utc="2026-07-30T20:59:00Z",
                 entry_type="meta",
                 blocks_json=_notification_blocks(task_id, "REPLAY-LOSER"))
    c.commit()
    return c, str(jsonl), offsets, tool_use_id


def test_background_result_returns_full_text(tmp_path):
    big = "Z" * (lc._TOOL_RESULT_CAP + 5000)
    body = '{"threadId":"t1","content":"' + big + '"}'
    c, jsonl, offsets, tuid = _seed(tmp_path, result=body)
    loc = cq.locate_tool_payload(c, "s1", tuid, "background_result")
    # The task id rides along so the re-read can prove it landed on OUR line.
    assert loc == (jsonl, offsets[0], "kravg1b9s")
    resp = cq.read_located_payload(loc, tuid, "background_result")
    assert resp["which"] == "result", "public discriminant must stay 'result'"
    assert resp["tool_use_id"] == tuid
    assert len(resp["text"]) == resp["full_length"]
    assert resp["text"].startswith('{"threadId"')
    assert resp["text"] == body            # FULL, beyond _TOOL_RESULT_CAP
    assert resp["truncated"] is False
    assert resp["is_error"] is False
    # The cached/assembled copy really is capped — otherwise this test is vacuous.
    call = _assembled_call(c, tuid)
    assert call["result"]["truncated"] is True
    assert len(call["result"]["text"]) == lc._TOOL_RESULT_CAP


def _assembled_call(conn, tool_use_id):
    for it in cq.get_conversation(conn, "s1")["items"]:
        for b in it["blocks"]:
            if b.get("kind") == "tool_call" and b.get("tool_use_id") == tool_use_id:
                return b
    raise AssertionError("tool_call not assembled")


def test_background_result_selects_the_same_winner_assembly_showed(tmp_path):
    c, jsonl, offsets, tuid = _seed(tmp_path, duplicate_rows=True, result="WINNER")
    loc = cq.locate_tool_payload(c, "s1", tuid, "background_result")
    resp = cq.read_located_payload(loc, tuid, "background_result")
    assert resp["text"] == "WINNER"
    assert "REPLAY-LOSER" not in resp["text"]
    # And it is the SAME logical row assembly rendered.
    assert _assembled_call(c, tuid)["result"]["text"] == resp["text"]


def test_missing_notification_row_is_gone_not_unknown(tmp_path):
    c, _jsonl, _offsets, tuid = _seed(tmp_path, drop_notification=True)
    assert cq.locate_tool_payload(c, "s1", tuid, "background_result") is None
    # The KNOWN background placeholder resolves to the gone mode, distinct from
    # the unknown-id 404 an ordinary miss produces.
    mode, loc = cq.locate_result_payload(c, "s1", tuid)
    assert (mode, loc) == ("background_gone", None)


def test_ordinary_result_still_resolves_through_the_public_mode(tmp_path):
    c = _conn()
    _msg(c, session_id="s1", uuid="u1", source_path="/r.jsonl", byte_offset=7,
         timestamp_utc="2026-07-30T20:42:00Z", entry_type="tool_result",
         blocks_json=json.dumps([{"kind": "tool_result", "text": "plain output",
                                  "truncated": False, "full_length": 12,
                                  "is_error": False, "tool_use_id": "toolu_P"}]))
    c.commit()
    statements = []
    c.set_trace_callback(statements.append)
    assert cq.locate_result_payload(c, "s1", "toolu_P") == (
        "result", ("/r.jsonl", 7))
    assert not any("SELECT id, uuid FROM conversation_messages" in sql
                   for sql in statements), (
        "a session with no background placeholder must not pay the logical "
        "whole-session dedup scan")


def test_unknown_id_stays_unknown(tmp_path):
    c, _jsonl, _offsets, _tuid = _seed(tmp_path)
    assert cq.locate_result_payload(c, "s1", "toolu_nope") == ("result", None)


def test_repeated_task_id_has_no_full_payload(tmp_path):
    """The fail-closed selection is shared, so a card that shows the placeholder
    can never load somebody else's full response."""
    c, jsonl, offsets, tuid = _seed(tmp_path, result="R")
    # A SECOND call claiming the same task id.
    _msg(c, session_id="s1", uuid="a2", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-07-30T20:43:00Z", entry_type="assistant",
         msg_id="m2", req_id="r2",
         blocks_json=json.dumps([{"kind": "tool_use", "name": "mcp__codex__codex",
                                  "input_summary": "{}", "id": "toolu_B",
                                  "preview": "codex"}]))
    _msg(c, session_id="s1", uuid="u2", source_path="b.jsonl", byte_offset=1,
         timestamp_utc="2026-07-30T20:44:00Z", entry_type="tool_result",
         blocks_json=json.dumps([{"kind": "tool_result",
                                  "text": _placeholder("kravg1b9s"),
                                  "truncated": False, "full_length": 300,
                                  "is_error": False, "tool_use_id": "toolu_B"}]))
    c.commit()
    for t in (tuid, "toolu_B"):
        assert cq.locate_tool_payload(c, "s1", t, "background_result") is None
        assert cq.locate_result_payload(c, "s1", t)[0] == "background_gone"


def test_conflicting_task_claims_have_no_full_payload(tmp_path):
    c, _jsonl, _offsets, tuid = _seed(tmp_path, result="ONE", task_id="t1")
    _msg(c, session_id="s1", uuid="u2", source_path="b.jsonl", byte_offset=2,
         timestamp_utc="2026-07-30T20:43:00Z", entry_type="tool_result",
         blocks_json=json.dumps([{
             "kind": "tool_result", "text": _placeholder("t2"),
             "truncated": False, "full_length": 300, "is_error": False,
             "tool_use_id": tuid}]))
    _msg(c, session_id="s1", uuid="n2", source_path="n2.jsonl", byte_offset=0,
         timestamp_utc="2026-07-30T20:52:00Z", entry_type="meta",
         blocks_json=_notification_blocks("t2", "TWO"))
    c.commit()
    assert cq.locate_result_payload(c, "s1", tuid) == (
        "background_gone", None)


def test_non_meta_notification_cannot_change_the_payload_winner(tmp_path):
    c, jsonl, offsets, tuid = _seed(tmp_path, result="WINNER", task_id="t1")
    _msg(c, session_id="s1", uuid="not-meta", source_path="foreign.jsonl",
         byte_offset=0, timestamp_utc="2026-07-30T20:52:00Z",
         entry_type="assistant",
         blocks_json=_notification_blocks("t1", "FOREIGN"))
    c.commit()
    assert _assembled_call(c, tuid)["result"]["text"] == "WINNER"
    mode, loc = cq.locate_result_payload(c, "s1", tuid)
    assert (mode, loc) == (
        "background_result", (jsonl, offsets[0], "t1"))


def test_timestamp_less_notification_has_no_full_payload(tmp_path):
    c, _jsonl, _offsets, tuid = _seed(tmp_path, result="NO PROVENANCE",
                                      task_id="t1")
    c.execute(
        "UPDATE conversation_messages SET timestamp_utc=NULL WHERE uuid=?",
        ("u_notif",),
    )
    c.commit()
    assert cq.locate_result_payload(c, "s1", tuid) == (
        "background_gone", None)


def test_read_full_payload_rejects_a_line_that_is_not_an_attachment(tmp_path):
    """A rotated/overwritten byte offset now holding an unrelated line -> None,
    which the handler maps to 410 rather than serving foreign content."""
    p = tmp_path / "other.jsonl"
    p.write_text(json.dumps({"type": "user", "uuid": "x",
                             "message": {"role": "user", "content": "hi"}}) + "\n")
    assert cq.read_full_payload(str(p), 0, "toolu_A", "background_result",
                                expected_task_id="kravg1b9s") is None


def test_read_full_payload_rejects_a_line_carrying_a_different_task_id(tmp_path):
    """The dangerous rotation is not "no longer a notification" — it is "now a
    DIFFERENT notification". That line parses as a perfectly well-formed
    task-notification with a perfectly well-formed <result>, so shape alone
    cannot tell it apart. Only the task id can, and the `result`/`input` branches
    both validate identity already."""
    p = tmp_path / "rotated.jsonl"
    p.write_text(_attachment_line("OTHERTASK", "SOMEONE-ELSES-RESULT") + "\n")
    assert cq.read_full_payload(str(p), 0, "toolu_A", "background_result",
                                expected_task_id="kravg1b9s") is None
    # Non-vacuity: the SAME line served under its OWN task id is a hit.
    ok = cq.read_full_payload(str(p), 0, "toolu_A", "background_result",
                              expected_task_id="OTHERTASK")
    assert ok["text"] == "SOMEONE-ELSES-RESULT"


def test_a_rotated_offset_onto_a_foreign_notification_is_not_served(tmp_path):
    """End to end through the resolver the routes use: the stored offset now
    addresses another task's notification, so nothing is served."""
    c, jsonl, _offsets, tuid = _seed(tmp_path, result="MINE")
    with open(jsonl, "w", encoding="utf-8") as fh:
        fh.write(_attachment_line("OTHERTASK", "SOMEONE-ELSES-RESULT") + "\n")
    mode, loc = cq.locate_result_payload(c, "s1", tuid)
    assert mode == "background_result"
    assert cq.read_located_payload(loc, tuid, mode) is None, (
        "a foreign task's result must never be served under this call's id")


def test_omitting_the_expected_task_id_raises_instead_of_a_silent_410(tmp_path):
    """A caller that forgets `expected_task_id` under `background_result` is a
    WIRING bug, not a rotation. Defaulting it to None made that bug degrade into
    the SAME silent 410 every genuine rotation produces — indistinguishable in a
    log, and it would take every recovered background result with it. It raises
    now; the genuine-rotation paths above still fail closed."""
    p = tmp_path / "notif.jsonl"
    p.write_text(_attachment_line("kravg1b9s", "R") + "\n")
    with pytest.raises(TypeError):
        cq.read_full_payload(str(p), 0, "toolu_A", "background_result")
    with pytest.raises(TypeError):
        cq.read_full_payload(str(p), 0, "toolu_A", "background_result",
                             expected_task_id="")
    # A location that lost its third element is the realistic shape of that bug.
    with pytest.raises(TypeError):
        cq.read_located_payload((str(p), 0), "toolu_A", "background_result")
    # Non-vacuity: the correctly-wired call still succeeds, and the OTHER modes
    # keep their `expected_task_id`-free signature.
    assert cq.read_full_payload(str(p), 0, "toolu_A", "background_result",
                                expected_task_id="kravg1b9s")["text"] == "R"
    assert cq.read_full_payload(str(p), 0, "toolu_A", "result") is None


def test_read_full_payload_background_honors_the_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(cq, "_FULL_PAYLOAD_CEILING", 32)
    p = tmp_path / "big.jsonl"
    p.write_text(_attachment_line("t1", "Q" * 100) + "\n")
    resp = cq.read_full_payload(str(p), 0, "toolu_A", "background_result",
                                expected_task_id="t1")
    assert resp["full_length"] == 100
    assert len(resp["text"]) == 32
    assert resp["truncated"] is True


# ---- the qualified `v1.claude.` route (§3.4) --------------------------------
# Both payload routes address the SAME logical object, so they must agree about
# it. The qualified route resolved `which="result"` straight through
# locate_tool_payload, which finds the PLACEHOLDER row — returning 200 with
# "still running after 120s" as the full response.

def _claude_ref(session_id="s1"):
    return disp._mint_claude_conversation_key(session_id)


def test_qualified_route_serves_the_recovered_background_result(tmp_path):
    big = "Z" * (lc._TOOL_RESULT_CAP + 5000)
    body = '{"threadId":"t1","content":"' + big + '"}'
    c, _jsonl, _offsets, tuid = _seed(tmp_path, result=body)
    got = disp.neutral_payload(c, _claude_ref(), which="result",
                               tool_use_id=tuid)
    assert got["status"] == "ok"
    assert got["which"] == "result"
    assert got["text"] == body, (
        "the qualified route must serve the recovered result, not the "
        "'still running after 120s' placeholder")
    # Byte-identical to what the unqualified route serves for the same object.
    mode, loc = cq.locate_result_payload(c, "s1", tuid)
    assert got["text"] == cq.read_located_payload(loc, tuid, mode)["text"]


def test_qualified_route_reports_gone_for_a_background_placeholder(tmp_path):
    c, _jsonl, _offsets, tuid = _seed(tmp_path, drop_notification=True)
    got = disp.neutral_payload(c, _claude_ref(), which="result",
                               tool_use_id=tuid)
    assert got["status"] == "gone", (
        "a KNOWN background placeholder with no resolvable notification is "
        "gone (410), never the placeholder text served as the full payload")


def test_qualified_route_still_serves_an_ordinary_result(tmp_path):
    p = tmp_path / "plain.jsonl"
    line = json.dumps({
        "type": "user", "uuid": "u1", "sessionId": "s1",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_P",
             "content": "plain output"}]}}, ensure_ascii=False)
    p.write_text(line + "\n")
    c = _conn()
    _msg(c, session_id="s1", uuid="u1", source_path=str(p), byte_offset=0,
         timestamp_utc="2026-07-30T20:42:00Z", entry_type="tool_result",
         blocks_json=json.dumps([{"kind": "tool_result", "text": "plain output",
                                  "truncated": False, "full_length": 12,
                                  "is_error": False, "tool_use_id": "toolu_P"}]))
    c.commit()
    got = disp.neutral_payload(c, _claude_ref(), which="result",
                               tool_use_id="toolu_P")
    assert got["status"] == "ok" and got["text"] == "plain output"


def test_qualified_route_unknown_id_is_not_found(tmp_path):
    c, _jsonl, _offsets, _tuid = _seed(tmp_path)
    got = disp.neutral_payload(c, _claude_ref(), which="result",
                               tool_use_id="toolu_nope")
    assert got["status"] == "not_found"
