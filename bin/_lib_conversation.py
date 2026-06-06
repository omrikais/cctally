"""Pure parser kernel for the conversation viewer (Plan 1).

Turns Claude Code transcript JSONL lines into normalized conversation_messages
rows. No DB, no clock, no I/O beyond the passed text-mode file handle — directly
unit-testable. Mirrors _lib_jsonl.py's readline()+tell() byte-offset discipline
so the message walk can share sync_cache's per-file cursor and rewind a partial
mid-write tail line. Spec §1, §2.
"""
from __future__ import annotations
import json
from dataclasses import dataclass

HUMAN = "human"
ASSISTANT = "assistant"
TOOL_RESULT = "tool_result"

_TOOL_RESULT_CAP = 4000  # chars; full text always re-derivable from JSONL


@dataclass
class MessageRow:
    byte_offset: int
    session_id: "str | None"
    uuid: "str | None"
    parent_uuid: "str | None"
    timestamp_utc: "str | None"
    entry_type: str
    text: str
    blocks_json: str
    model: "str | None"
    msg_id: "str | None"
    req_id: "str | None"
    cwd: "str | None"
    git_branch: "str | None"
    is_sidechain: int


def iter_message_rows(fh, path_str):
    """Yield one MessageRow per user/assistant JSONL line from fh's current
    position. summary / file-history-snapshot / malformed / uuid-less lines are
    skipped (offset still advances). A partial tail line (no trailing newline)
    rewinds the handle and stops, so the next sync re-reads it once complete.

    ``path_str`` is accepted for caller symmetry — the sync ingest threads
    ``source_path`` into each row at write time — but the kernel itself does
    not use it (the returned MessageRow carries only ``byte_offset``)."""
    while True:
        offset = fh.tell()
        line = fh.readline()
        if not line:
            return
        if not line.endswith("\n"):
            fh.seek(offset)
            return
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        row = parse_message_row(obj, offset)
        if row is not None:
            yield row


def parse_message_row(obj, offset):
    """Pure per-line message parser: given a parsed JSONL object and its byte
    offset, return a ``MessageRow`` when it is a user/assistant turn carrying a
    ``uuid``, or ``None`` otherwise (summary / file-history-snapshot / uuid-less
    lines). No I/O — the caller owns the readline()+tell() loop.

    Extracted (#138) so ``iter_message_rows`` and the fused single-pass sync
    walker (``_cctally_cache._iter_sync_entries``) share ONE classification —
    each JSONL line is parsed once and the conversation index is no longer
    populated by a separate second seek-and-walk over the same byte span."""
    t = obj.get("type")
    if t not in ("user", "assistant"):
        return None
    if not obj.get("uuid"):
        return None
    return _normalize(obj, t, offset)


def _normalize(obj, t, offset):
    msg = obj.get("message")
    if not isinstance(msg, dict):
        msg = {}
    blocks, text = _blocks_and_text(msg.get("content"))
    if t == "assistant":
        entry_type = ASSISTANT
    elif any(b["kind"] == "tool_result" for b in blocks):
        entry_type = TOOL_RESULT
        # tool_result rows are stored but NOT indexed as prose (spec §2). A
        # user line that mixes a text block with a tool_result block must not
        # leak that text into the FTS index; the full content stays in
        # blocks_json for rendering.
        text = ""
    else:
        entry_type = HUMAN
    is_asst = t == "assistant"
    return MessageRow(
        byte_offset=offset,
        session_id=obj.get("sessionId"),
        uuid=obj.get("uuid"),
        parent_uuid=obj.get("parentUuid"),
        timestamp_utc=obj.get("timestamp"),
        entry_type=entry_type,
        text=text,
        blocks_json=json.dumps(blocks, separators=(",", ":")),
        model=msg.get("model") if is_asst else None,
        msg_id=msg.get("id") if is_asst else None,
        req_id=obj.get("requestId") if is_asst else None,
        cwd=obj.get("cwd"),
        git_branch=obj.get("gitBranch"),
        is_sidechain=1 if obj.get("isSidechain") else 0,
    )


def _blocks_and_text(content):
    """Return (normalized blocks list, indexed-prose string). Prose = joined
    `text` blocks only (thinking / tool_use / tool_result excluded)."""
    if isinstance(content, str):
        return ([{"kind": "text", "text": content}] if content else []), content
    blocks, texts = [], []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                txt = b.get("text", "") or ""
                blocks.append({"kind": "text", "text": txt})
                texts.append(txt)
            elif bt == "thinking":
                blocks.append({"kind": "thinking", "text": b.get("thinking", "") or ""})
            elif bt == "tool_use":
                blocks.append({"kind": "tool_use", "name": b.get("name"),
                               "input_summary": _summarize(b.get("input"))})
            elif bt == "tool_result":
                raw = _stringify(b.get("content"))
                blocks.append({"kind": "tool_result", "text": raw[:_TOOL_RESULT_CAP],
                               "truncated": len(raw) > _TOOL_RESULT_CAP,
                               "is_error": bool(b.get("is_error"))})
            elif bt in ("image", "document"):
                blocks.append({"kind": bt, **_media(b.get("source"))})
            elif bt == "tool_reference":
                blocks.append({"kind": "tool_reference", "name": b.get("name")})
    return blocks, "\n".join(t for t in texts if t)


def _stringify(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", "") or "")
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(out)
    return "" if c is None else json.dumps(c, separators=(",", ":"))


def _summarize(inp):
    if not isinstance(inp, dict):
        return ""
    s = json.dumps(inp, separators=(",", ":"))
    return s[:200]


def _media(source):
    if not isinstance(source, dict):
        return {"media_type": None, "bytes": 0}
    data = source.get("data") or ""
    return {"media_type": source.get("media_type"), "bytes": len(data)}
