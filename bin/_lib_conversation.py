"""Pure parser kernel for the conversation viewer (Plan 1).

Turns Claude Code transcript JSONL lines into normalized conversation_messages
rows. No DB, no clock, no I/O beyond the passed text-mode file handle — directly
unit-testable. Mirrors _lib_jsonl.py's readline()+tell() byte-offset discipline
so the message walk can share sync_cache's per-file cursor and rewind a partial
mid-write tail line. Spec §1, §2.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass

HUMAN = "human"
ASSISTANT = "assistant"
TOOL_RESULT = "tool_result"
META = "meta"

_TOOL_RESULT_CAP = 16000   # was 4000; full text always re-derivable from JSONL
_INPUT_LEAF_CAP = 8000     # max chars per string leaf in a bounded tool input
_INPUT_TOTAL_CAP = 32000   # honesty backstop on the serialized bounded input
_INPUT_MAX_NODES = 2000    # max dict-values + list-elements kept before tail elision
_INPUT_MAX_DEPTH = 12      # max nesting depth before subtree elision (RecursionError guard)
_INPUT_KEY_CAP = 512       # max chars per dict key (else keys are stored verbatim, unbounded)
_INPUT_ELISION = "…"       # sentinel for elided leaves / subtrees

# #177 S4: WebSearch link-list capture bounds + the media item types whose
# placeholders the ordinal chokepoint (iter_media_items) addresses.
_WEB_SEARCH_LINK_CAP = 50
_WEB_LINK_TITLE_CAP = 300
_WEB_LINK_URL_CAP = 2000
_MEDIA_BLOCK_TYPES = ("image", "document")


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
    source_tool_use_id: "str | None" = None
    stop_reason: "str | None" = None
    attribution_skill: "str | None" = None
    attribution_plugin: "str | None" = None
    search_aux: str = ""


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
    blocks, text, aux = _blocks_and_text(msg.get("content"))
    if t == "assistant":
        entry_type = ASSISTANT
    elif any(b["kind"] == "tool_result" for b in blocks):
        entry_type = TOOL_RESULT
        _attach_subagent_result(blocks, obj)   # #166: record-level toolUseResult
        _attach_ask_answers(blocks, obj)       # #177 S2: AskUserQuestion answers
        _attach_bash_streams(blocks, obj)      # #177 S3: Bash stderr/interrupted
        _attach_web_search(blocks, obj)        # #177 S4: WebSearch link list
        _attach_web_fetch(blocks, obj)         # #177 S4: WebFetch HTTP status
        _attach_task_meta(blocks, obj)         # task checklist identity
        # tool_result rows are stored but NOT indexed as prose (spec §2). A
        # user line that mixes a text block with a tool_result block must not
        # leak that text into the FTS index; the full content stays in
        # blocks_json for rendering.
        text = ""
    elif obj.get("isMeta"):
        # Injected, harness-authored content carried as a user line: skill
        # bodies (Skill tool + SessionStart), git-context blocks, "Continue
        # from where you left off.", pasted-image placeholders, slash-command
        # caveats, check-review "## Task" blocks. The user did NOT type these,
        # so the reader must not render them as a "YOU" prompt. We classify
        # them META here; text="" keeps the body out of the FTS index and out
        # of title derivation (which filters entry_type='human'), exactly like
        # tool_result. The body survives in blocks_json; the skill-vs-context
        # discrimination is a read-time concern (the query kernel, keyed on the
        # body). Ordered AFTER tool_result so an isMeta line that also carries a
        # tool_result block still folds as a result.
        entry_type = META
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
        source_tool_use_id=obj.get("sourceToolUseID"),
        # #177: message-level enrichment. stop_reason is assistant-only;
        # attribution is top-level on the JSONL object. search_aux is kept even
        # for tool_result/meta rows — only `text` is zeroed for prose FTS;
        # search_aux is the non-prose index (tool content stays searchable).
        stop_reason=msg.get("stop_reason") if is_asst else None,
        attribution_skill=obj.get("attributionSkill"),
        attribution_plugin=obj.get("attributionPlugin"),
        search_aux=aux,
    )


def _blocks_and_text(content):
    """Return (normalized blocks list, indexed-prose string, search_aux string).

    Prose (``text``) = joined ``text`` blocks only (thinking / tool_use /
    tool_result excluded — those go to the prose FTS via the ``text`` column).
    ``search_aux`` (#177) = the non-prose searchable content: bounded tool-input
    string leaves, the (capped) tool_result ``text``, and the thinking text
    capped at ``_TOOL_RESULT_CAP`` (code-review I2 — the FULL thinking still
    lives in ``blocks_json`` for rendering; only this aux index entry is capped
    so the second FTS index doesn't double the at-rest cost of large thinking) —
    indexed by the parallel ``conversation_fts_aux`` so tool content stays
    searchable without polluting prose FTS. Prose is deliberately excluded from
    aux (it is already in ``text``)."""
    if isinstance(content, str):
        return (([{"kind": "text", "text": content}] if content else []), content, "")
    blocks, texts, aux_parts = [], [], []
    if isinstance(content, list):
        # #177 S4: ordinal among media items at THIS list level, keyed by the
        # object identity of each image/document item, so the placeholder writers
        # below stamp the same ``index`` the media-route reader recomputes.
        media_index = {id(item): idx for idx, item in iter_media_items(content)}
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                txt = b.get("text", "") or ""
                blocks.append({"kind": "text", "text": txt})
                texts.append(txt)
            elif bt == "thinking":
                think = b.get("thinking", "") or ""
                blocks.append({"kind": "thinking", "text": think})  # FULL text for render
                aux_parts.append(think[:_TOOL_RESULT_CAP])          # aux index capped (I2)
            elif bt == "tool_use":
                bounded, input_trunc = _bound_input(b.get("input"))
                block = {"kind": "tool_use", "name": b.get("name"),
                         "input_summary": _summarize(b.get("input")),
                         "input": bounded, "input_truncated": input_trunc,
                         "id": b.get("id"),
                         "preview": tool_preview(b.get("name"), b.get("input"))}
                inp = b.get("input")
                st = inp.get("subagent_type") if isinstance(inp, dict) else None
                if isinstance(st, str) and st:        # #166: spawn kind (Agent/Task)
                    block["subagent_type"] = st
                aux_parts.extend(_aux_strings(bounded))
                blocks.append(block)
            elif bt == "tool_result":
                raw = _stringify(b.get("content"))
                clipped = raw[:_TOOL_RESULT_CAP]
                block = {"kind": "tool_result", "text": clipped,
                         "truncated": len(raw) > _TOOL_RESULT_CAP,
                         "full_length": len(raw),
                         "is_error": bool(b.get("is_error")),
                         "tool_use_id": b.get("tool_use_id")}
                # #177 S4: media placeholders for image/document items inside the
                # tool_result content array (where every MCP screenshot lives) —
                # ordinals from the shared iter_media_items chokepoint.
                media = [{"kind": item.get("type"), **_media(item.get("source")),
                          "index": idx}
                         for idx, item in iter_media_items(b.get("content"))]
                if media:                      # omitted when empty (additive)
                    block["media"] = media
                blocks.append(block)
                aux_parts.append(clipped)
            elif bt in ("image", "document"):
                blocks.append({"kind": bt, **_media(b.get("source")),
                               "index": media_index[id(b)]})
            elif bt == "tool_reference":
                blocks.append({"kind": "tool_reference", "name": b.get("name")})
    return (blocks, "\n".join(t for t in texts if t),
            "\n".join(a for a in aux_parts if a))


_SUBAGENT_META_KEYS = (
    ("totalTokens", "total_tokens"),
    ("totalDurationMs", "total_duration_ms"),
    ("totalToolUseCount", "total_tool_use_count"),
    ("status", "status"),
)


def _attach_subagent_result(blocks, obj):
    """Attach the record-level ``toolUseResult`` agentId + meta (#166) onto the
    tool_result block, but ONLY when the record carries exactly one tool_result
    block — the unambiguous subagent-spawn result shape. Zero or >1 tool_result
    blocks: no-op (the kernel then degrades that subagent card to title-only).
    The kind (subagent_type) is captured separately on the spawn tool_use block;
    the kernel joins the two on tool_use_id. ``agentId`` == the subagent file's
    ``_subagent_key``. Meta keys are normalized to snake_case here so the kernel
    stays a pure pass-through (same posture as is_error / tool_use_id)."""
    tur = obj.get("toolUseResult")
    if not isinstance(tur, dict):
        return
    agent_id = tur.get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        return
    results = [b for b in blocks if b.get("kind") == "tool_result"]
    if len(results) != 1:
        return
    block = results[0]
    block["agent_id"] = agent_id
    meta = {}
    for src, dst in _SUBAGENT_META_KEYS:
        v = tur.get(src)
        if v is not None:
            meta[dst] = v
    if meta:
        block["subagent_meta"] = meta


def _attach_ask_answers(blocks, obj):
    """Stash an AskUserQuestion's structured answers (+ annotations) onto its
    single tool_result block (#177 S2), so the chosen option(s) have a robust
    source the client can highlight without parsing the harness result string.
    Self-identifying: fires only when toolUseResult carries an ``answers`` dict
    (a key distinctive to AskUserQuestion), so no cross-record tool-name lookup
    is needed. Same exactly-one-result-block guard as _attach_subagent_result.

    answers/annotations are BOUNDED through _bound_input before storage — a
    free-form "Other" answer or a long annotation note is attacker-controlled
    free text, and every other free-text payload in this parser is capped
    before it reaches blocks_json. Reusing _bound_input applies the same
    five-axis cap (leaf/key/node/depth/total). The bound's truncation flag is
    intentionally NOT surfaced (no answer-level "truncated" affordance — a
    >8000-char option answer is not a realistic shape; the cap is a backstop).

    An EMPTY answers dict is treated as no-capture (symmetric with the empty-
    annotations drop below): emitting ``ask_answers={}`` would set a falsy
    ``answers`` on the tool_call and suppress the client's result-text
    fallback, so a degenerate empty payload must no-op here instead."""
    tur = obj.get("toolUseResult")
    if not isinstance(tur, dict):
        return
    answers = tur.get("answers")
    if not isinstance(answers, dict) or not answers:   # require a non-empty dict
        return
    results = [b for b in blocks if b.get("kind") == "tool_result"]
    if len(results) != 1:
        return
    bounded_ans, _ = _bound_input(answers)
    results[0]["ask_answers"] = bounded_ans
    anno = tur.get("annotations")
    if isinstance(anno, dict) and anno:
        bounded_anno, _ = _bound_input(anno)
        results[0]["ask_annotations"] = bounded_anno


def _attach_bash_streams(blocks, obj):
    """Stash a Bash tool_result's structured stderr + interrupted onto its single
    tool_result block (#177 S3). Self-identifying: fires only when toolUseResult
    is a dict carrying a ``stdout``/``stderr`` key — a shape distinctive to Bash —
    so no cross-record tool-name lookup is needed (same posture as
    _attach_ask_answers' ``answers`` gate). We do NOT store stdout: the existing
    ``result.text`` already equals stdout+stderr (the merged Bash output), so
    storing stdout would roughly double the at-rest payload; the stdout/stderr
    split is derived client-side by stripping the stderr suffix.

    Parser-private keys ``bash_stderr`` / ``bash_interrupted`` are popped in the
    query layer's Phase 1 so they never leak into emitted/orphan blocks. stderr is
    bounded with the same cap as result.text (_TOOL_RESULT_CAP). Empty stderr +
    not-interrupted is a no-op (the common case — stderr is empty in ~99% of
    results), keeping the additive contract: absent on every non-Bash result and
    on old rows. Same exactly-one-result-block guard as _attach_subagent_result."""
    tur = obj.get("toolUseResult")
    if not isinstance(tur, dict) or ("stdout" not in tur and "stderr" not in tur):
        return
    results = [b for b in blocks if b.get("kind") == "tool_result"]
    if len(results) != 1:
        return
    stderr = tur.get("stderr")
    if isinstance(stderr, str) and stderr:
        results[0]["bash_stderr"] = stderr[:_TOOL_RESULT_CAP]
    if bool(tur.get("interrupted")):
        results[0]["bash_interrupted"] = True


def _attach_web_search(blocks, obj):
    """Stash a WebSearch toolUseResult's structured link list onto its single
    tool_result block (#177 S4). Self-identifying: fires only on the WebSearch
    shape (string ``query`` + list ``results``); the query kernel additionally
    joins NAME-KEYED (only onto name=='WebSearch'), so a shape-coincident
    toolUseResult from another tool never decorates the wrong card (Codex F3).
    Links flatten from results[].content[]; items lacking string title+url are
    skipped; bounded (<=_WEB_SEARCH_LINK_CAP links, title/url char caps) with
    ``links_truncated`` when links were dropped. Parser-private key
    ``web_search`` is popped in the query layer's Phase 1 so it never leaks on
    orphan blocks. Same exactly-one-result-block guard as
    _attach_subagent_result."""
    tur = obj.get("toolUseResult")
    if not isinstance(tur, dict):
        return
    query = tur.get("query")
    raw_results = tur.get("results")
    if not isinstance(query, str) or not isinstance(raw_results, list):
        return
    results = [b for b in blocks if b.get("kind") == "tool_result"]
    if len(results) != 1:
        return
    links, dropped = [], False
    for r in raw_results:
        content = r.get("content") if isinstance(r, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if not (isinstance(item, dict) and isinstance(item.get("title"), str)
                    and isinstance(item.get("url"), str)):
                continue
            if len(links) >= _WEB_SEARCH_LINK_CAP:
                dropped = True
                break
            links.append({"title": item["title"][:_WEB_LINK_TITLE_CAP],
                          "url": item["url"][:_WEB_LINK_URL_CAP]})
        if dropped:
            break
    payload = {"query": query[:_WEB_LINK_TITLE_CAP], "links": links}
    if dropped:
        payload["links_truncated"] = True
    results[0]["web_search"] = payload


def _attach_web_fetch(blocks, obj):
    """Stash a WebFetch toolUseResult's HTTP status onto its single tool_result
    block (#177 S4). Self-identifying on the WebFetch triple — ``code`` +
    ``codeText`` + ``result`` keys all present and ``code`` an int; the query
    kernel additionally joins NAME-KEYED (only onto name=='WebFetch'). A bare
    error-string toolUseResult never matches (the card then renders without a
    status chip — the documented degrade). Only the status is stored (the
    summary already IS result.text). Parser-private key ``web_fetch`` is popped
    in Phase 1. Same exactly-one-result-block guard as
    _attach_subagent_result."""
    tur = obj.get("toolUseResult")
    if not isinstance(tur, dict):
        return
    if "code" not in tur or "codeText" not in tur or "result" not in tur:
        return
    code = tur.get("code")
    if not isinstance(code, int):
        return
    results = [b for b in blocks if b.get("kind") == "tool_result"]
    if len(results) != 1:
        return
    meta = {"code": code}
    code_text = tur.get("codeText")
    if isinstance(code_text, str) and code_text:
        meta["code_text"] = code_text[:100]
    results[0]["web_fetch"] = meta


# Subagent Task tools record toolUseResult=null and put the identity in the
# human-readable result text instead; the id is the only thing the fold needs
# from the result (subject/status come from the call input). Anchored to the
# line start so unrelated output mentioning a task id mid-sentence never matches.
# Shapes verified against real subagent transcripts (Claude Code 2.1.173).
_TASK_CREATE_RESULT_RE = re.compile(r"^Task #(\d+) created\b")
_TASK_UPDATE_RESULT_RE = re.compile(r"^Updated task #(\d+)\b")


def _attach_task_meta(blocks, obj):
    """Stash a Task* tool's record-level identity onto its single tool_result
    block so the query-kernel fold has a robust id. Task ids are monotonic +
    never reused, so the explicit id is the only stable fold key. Two result
    shapes, both self-identifying off the single tool_result block:

    Structured (MAIN-session Task tools) — toolUseResult carries the identity:
      TaskCreate -> {"task": {"id": ...}}            -> block["task_id"]
      TaskUpdate -> {"taskId": ...}                   -> block["task_id"]
      TaskList   -> {"tasks": [{id,subject,status}]}  -> block["task_list"]

    String-content (SUBAGENT Task tools) — toolUseResult is null and the id
    lives in the result text ("Task #7 created successfully: ..." / "Updated
    task #3 status"); we recover the id from block["text"]. Subagent-driven
    workflows make this the dominant shape, so missing it left every subagent
    Task run rendering as an empty "0 / 0" card.

    Same exactly-one-result-block guard as _attach_subagent_result. Subjects
    bounded through _bound_input.

    The ``task.id`` (not ``task.task_id``) gate deliberately ignores the
    look-alike local_bash spawn result {"task": {"task_id": ..., ...}}, which is
    a different tool family and carries no checklist id."""
    results = [b for b in blocks if b.get("kind") == "tool_result"]
    if len(results) != 1:
        return
    block = results[0]
    tur = obj.get("toolUseResult")
    if isinstance(tur, dict):
        task = tur.get("task")
        if isinstance(task, dict) and task.get("id") is not None:
            block["task_id"] = str(task["id"])
            return
        if tur.get("taskId") is not None:
            block["task_id"] = str(tur["taskId"])
            return
        tasks = tur.get("tasks")
        if isinstance(tasks, list):
            snap = []
            for t in tasks:
                if not isinstance(t, dict) or t.get("id") is None:
                    continue
                bounded, _ = _bound_input({"subject": t.get("subject") or ""})
                snap.append({"id": str(t["id"]),
                             "subject": bounded.get("subject", ""),
                             "status": t.get("status") or "pending"})
            block["task_list"] = snap
            return
    # String-content fallback (subagent Task tools): no structured identity.
    m = (_TASK_CREATE_RESULT_RE.match(block.get("text") or "")
         or _TASK_UPDATE_RESULT_RE.match(block.get("text") or ""))
    if m:
        block["task_id"] = m.group(1)


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


def _bound_input(inp):
    """Return (bounded_structured_input, truncated) for a tool_use input dict, or
    (None, False) for a non-dict (the same non-dict contract as _summarize /
    tool_preview). Hard-bounds the result on five axes so a pathological input
    can't bloat blocks_json (Codex P1 + code-review I1): string leaves clip to
    _INPUT_LEAF_CAP; dict keys clip to _INPUT_KEY_CAP (keys were stored verbatim
    otherwise — the last unbounded axis); non-string scalars pass through; once
    _INPUT_MAX_NODES dict-values/list-elements are kept the remainder elides to
    _INPUT_ELISION; recursion past _INPUT_MAX_DEPTH elides the subtree
    (RecursionError guard). A final _INPUT_TOTAL_CAP serialized-size check is the
    honesty backstop. Structure is preserved for the kept prefix so downstream
    renderers can read tool params."""
    if not isinstance(inp, dict):
        return (None, False)
    state = {"nodes": 0, "truncated": False}

    def walk(v, depth):
        if depth > _INPUT_MAX_DEPTH:
            state["truncated"] = True
            return _INPUT_ELISION
        if isinstance(v, str):
            if len(v) > _INPUT_LEAF_CAP:
                state["truncated"] = True
                return v[:_INPUT_LEAF_CAP]
            return v
        if isinstance(v, dict):
            out = {}
            for k, vv in v.items():
                ks = str(k)
                if len(ks) > _INPUT_KEY_CAP:   # clip pathological long keys (I1)
                    state["truncated"] = True
                    ks = ks[:_INPUT_KEY_CAP]
                if state["nodes"] >= _INPUT_MAX_NODES:
                    state["truncated"] = True
                    out[ks] = _INPUT_ELISION
                    break
                state["nodes"] += 1
                out[ks] = walk(vv, depth + 1)
            return out
        if isinstance(v, list):
            out = []
            for vv in v:
                if state["nodes"] >= _INPUT_MAX_NODES:
                    state["truncated"] = True
                    out.append(_INPUT_ELISION)
                    break
                state["nodes"] += 1
                out.append(walk(vv, depth + 1))
            return out
        # int / float / bool / None — bounded-width scalars, pass through
        return v

    bounded = walk(inp, 0)
    if len(json.dumps(bounded, separators=(",", ":"))) > _INPUT_TOTAL_CAP:
        state["truncated"] = True
    return (bounded, state["truncated"])


def _aux_strings(v):
    """Yield string leaves from a bounded input value (for the search_aux blob)."""
    if isinstance(v, str):
        if v:
            yield v
    elif isinstance(v, dict):
        for vv in v.values():
            yield from _aux_strings(vv)
    elif isinstance(v, list):
        for vv in v:
            yield from _aux_strings(vv)


_PREVIEW_FIELDS = {
    "Read": "file_path", "Write": "file_path", "Edit": "file_path",
    "MultiEdit": "file_path", "NotebookEdit": "file_path",
    "Bash": "command", "Grep": "pattern", "Glob": "pattern",
    "Task": "description", "WebFetch": "url", "WebSearch": "query",
}


def tool_preview(name, inp):
    """One-line, full-fidelity preview for a tool call's collapsed chip (#164,
    C5). Runs on the RAW input dict before _summarize truncates to 200 chars.
    Known tools map to their primary arg; Bash takes the first command line;
    Task falls back to subagent_type; unknown/mcp tools take the first
    string-valued arg, else the tool name. Always returns a single-line str."""
    if not isinstance(inp, dict):
        return ""
    field = _PREVIEW_FIELDS.get(name or "")
    val = None
    if field is not None:
        val = inp.get(field)
        if val is None and name == "Task":
            val = inp.get("subagent_type")
    if val is None:
        # generic fallback: first string-valued arg, else the tool name
        for v in inp.values():
            if isinstance(v, str) and v:
                val = v
                break
    if not isinstance(val, str) or not val:
        return name or ""
    return val.splitlines()[0]


def _media(source):
    if not isinstance(source, dict):
        return {"media_type": None, "bytes": 0}
    data = source.get("data") or ""
    return {"media_type": source.get("media_type"), "bytes": len(data)}


def iter_media_items(content):
    """Yield ``(index, item)`` for every image/document item in a content list,
    in document order. ``index`` is the ordinal AMONG MEDIA ITEMS (not the list
    position) — the stable address shared by the ingest placeholder writer here
    and the media-route reader (read_media_bytes), so "media item N" can never
    mean two different things (the _canonical_5h_window_key lesson applied to
    media addressing — do NOT write a second walk). Non-list input and
    non-dict / non-media entries are skipped without consuming an ordinal."""
    if not isinstance(content, list):
        return
    idx = 0
    for item in content:
        if isinstance(item, dict) and item.get("type") in _MEDIA_BLOCK_TYPES:
            yield idx, item
            idx += 1
