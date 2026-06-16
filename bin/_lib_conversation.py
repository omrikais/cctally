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

# Mirror of dashboard/web/src/conversations/systemMarkers.ts::MARKER_RE — anchored
# whole-string (fullmatch), unrolled-lazy body for linear time (no ReDoS), \1
# backref forces each close tag to match its open tag. Used to SKIP slash-command
# plumbing when deriving a conversation title (#165 Q2) AND — at the parser layer
# (#186) — to classify a slash-command echo carried as a plain user line as META
# at ingest. MUST stay equivalent to the TS predicate over ASCII whitespace
# (parity-tested); exotic Unicode/control whitespace is an explicit non-goal.
# The query kernel re-exports these names for back-compat. See
# docs/dashboard-gotchas.md.
_MARKER_TAGS = ("command-name", "command-message", "command-args",
                "local-command-caveat", "local-command-stdout",
                "local-command-stderr")
_MARKER_RE = re.compile(
    r"\s*(?:<(" + "|".join(_MARKER_TAGS) + r")>(?:(?!</\1>)[\s\S])*</\1>\s*)+"
)


def _is_system_marker(text) -> bool:
    """True iff `text` is ONLY concatenated command-marker wrappers (slash-command
    plumbing) — the title-derivation skip predicate AND the parser-layer ingest
    classifier (#186). `fullmatch` reproduces the TS `^\\s*…\\s*$` anchor (no
    `$`-before-trailing-`\\n` foot-gun)."""
    return bool(text) and _MARKER_RE.fullmatch(text) is not None


# #188: a slash-command invocation carries the user's real prompt inside
# <command-args>. The <command-name>/<command-message> wrappers are plumbing.
# Anchored mid-string is fine — _is_system_marker already proved the whole text
# is ONLY markers before these run, so the first match is the lone occurrence.
_CMD_NAME_RE = re.compile(r"<command-name>([\s\S]*?)</command-name>")
_CMD_ARGS_RE = re.compile(r"<command-args>([\s\S]*?)</command-args>")


def _join_text_blocks(blocks):
    """'\\n'-join the text-block bodies of a normalized blocks list (mirrors
    _blocks_and_text's prose join + the query kernel's _join_text_blocks). The
    migration-011 consumer rebuilds the marker text from blocks_json to feed
    _extract_command_invocation, so this lives in the parser kernel (the one
    _cctally_cache already imports) to keep the two derivations identical."""
    if not blocks:
        return ""
    return "\n".join(b.get("text", "") or ""
                     for b in blocks if b.get("kind") == "text")


def _extract_command_invocation(blocks, text):
    """If a row is a pure slash-command marker whose <command-args> is non-empty
    (after strip), return ``{"name": <command-name or "">, "args": <args>}``;
    else ``None`` (#188 bug 4).

    Block-aware: promotes ONLY when every block is text (mirrors the all-text
    guard in ``_normalize`` / ``_meta_classify`` so a marker text block PLUS an
    attachment — e.g. an image — is never promoted) AND ``text`` is a pure
    command-marker block (``_is_system_marker``) AND ``<command-args>`` holds a
    real prompt. ``/clear``, ``/exit``, ``/compact``, ``/model`` (empty args) and
    stdout-only markers all return ``None`` so they stay hidden as system
    markers. ``name`` is taken from ``<command-name>`` for the reader's command
    badge; it is purely cosmetic (``""`` when the marker omits the name tag)."""
    if not blocks or not all(b.get("kind") == "text" for b in blocks):
        return None
    if not _is_system_marker(text):
        return None
    am = _CMD_ARGS_RE.search(text)
    if am is None:
        return None
    args = am.group(1).strip()
    if not args:
        return None
    nm = _CMD_NAME_RE.search(text)
    return {"name": (nm.group(1).strip() if nm else ""), "args": args}


# #186: strip ANSI terminal control sequences from captured prose/thinking so a
# slash-command stdout echo (which keeps its SGR styling, e.g. `\x1b[1mFable
# 5\x1b[22m`) never leaks literal `^[[1m` control codes into a title, an outline
# label, or body text — and so FTS indexes clean tokens (`Fable`, not
# `1mFable`). Three alternatives: CSI (covers all SGR `…m`), OSC (BEL- or
# ST-terminated, or unterminated), and a lone/truncated ESC. Ordinary `[`,
# digits, `m` in prose are untouched — they match only as part of a real
# `\x1b[`-led sequence. Conceptually mirrors the client `parseAnsi` cleanup.
# Deliberately NOT applied in `_stringify` / tool_result text: `AnsiText` (#177
# S3) renders Bash stdout/stderr SGR colors, so that path keeps raw ANSI.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;:?]*[ -/]*[@-~]"           # CSI (covers all SGR …m)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC (BEL/ST terminated or unterminated)
    r"|\x1b"                                # lone/truncated ESC
)


def _strip_ansi(text):
    """Remove ANSI control sequences (terminal SGR/CSI/OSC). Ordinary '[',
    digits, 'm' in prose are untouched — they match only inside a real \\x1b[
    run. Empty / None passes through unchanged. (#186)"""
    return _ANSI_RE.sub("", text) if text else text


# ---- #191: harness-injected user-line discriminators (shared with the query
# kernel via re-export). All key on a SELF-IDENTIFYING body shape so the
# read-time recovery half can rescue already-ingested `human` rows. ----

# Compaction summary preamble. The ingest authority is the `isCompactSummary`
# JSONL flag (see _normalize); this body match is the read-time meta_kind label
# authority (and rescues the rare legacy sentinel-without-flag row).
_COMPACT_SENTINEL = "This session is being continued from a previous conversation"


def _is_compaction_body(text) -> bool:
    """True iff `text` is a compaction-summary body."""
    return bool(text) and text.lstrip().startswith(_COMPACT_SENTINEL)


# Full open+close-tag wrapper (NOT a bare startswith — Codex P1a): a real prompt
# that merely begins with the literal tag but isn't a well-formed closed block
# stays human. Unrolled-lazy body = linear time; \1 backref forces the close tag
# to match the open. A trailing harness line after the close tag is allowed.
_NOTIFICATION_RE = re.compile(
    r"\s*<(task|bash)-notification>(?:(?!</\1-notification>)[\s\S])*</\1-notification>")


def _is_notification_body(text) -> bool:
    """True iff `text` opens with a complete <task-notification>…</task-notification>
    or <bash-notification>…</bash-notification> background-completion wrapper."""
    return bool(text) and _NOTIFICATION_RE.match(text) is not None


# `!`-mode local shell echoes. A DEDICATED detector, NOT a `_MARKER_TAGS` entry
# (Codex P1b) — so a bash echo containing a literal <command-args> can never
# reach the #188 `_extract_command_invocation` promotion path.
_BASH_ECHO_RE = re.compile(
    r"\s*<(bash-input|bash-stdout|bash-stderr)>(?:(?!</\1>)[\s\S])*</\1>")


def _is_bash_echo_body(text) -> bool:
    """True iff `text` opens with a complete <bash-input>/<bash-stdout>/
    <bash-stderr> echo wrapper."""
    return bool(text) and _BASH_ECHO_RE.match(text) is not None


# Remote-control replies (sent from the Claude mobile/remote app) arrive as a
# real user turn prefixed with a `Message sent at <ts> UTC.` system-reminder
# stamp. NARROW (Codex / approved decision): only this exact leading shape is
# stripped; no other <system-reminder> is touched.
_REMOTE_CONTROL_RE = re.compile(
    r"\A\s*<system-reminder>Message sent at [^<]*UTC\.</system-reminder>\s*")


def _strip_remote_control_prefix(text):
    """Remove a leading remote-control `Message sent at … UTC.` system-reminder
    block, returning the real user reply. No-op when absent; None/'' pass through."""
    if not text:
        return text
    return _REMOTE_CONTROL_RE.sub("", text, count=1)


_TOOL_RESULT_CAP = 16000   # was 4000; full text always re-derivable from JSONL
_INPUT_LEAF_CAP = 8000     # max chars per string leaf in a bounded tool input
_INPUT_TOTAL_CAP = 32000   # honesty backstop on the serialized bounded input
_INPUT_MAX_NODES = 2000    # max dict-values + list-elements kept before tail elision
_INPUT_MAX_DEPTH = 12      # max nesting depth before subtree elision (RecursionError guard)
_INPUT_KEY_CAP = 512       # max chars per dict key (else keys are stored verbatim, unbounded)
_INPUT_ELISION = "…"       # sentinel for elided leaves / subtrees

# #198: cap the O(n·m) LCS pass that stamps edit_stat from the FULL (unbounded)
# input. Realistic truncated edits have a few hundred lines per side (n·m ~ 1e4–1e5);
# this bound only excludes a pathological multi-thousand-line edit, in which case
# edit_stat is omitted and the client falls back to its bounded recompute.
_EDIT_STAT_LCS_CELL_BUDGET = 4_000_000

# #177 S4: WebSearch link-list capture bounds + the media item types whose
# placeholders the ordinal chokepoint (iter_media_items) addresses.
_WEB_SEARCH_LINK_CAP = 50
_WEB_LINK_TITLE_CAP = 300
_WEB_LINK_URL_CAP = 2000
_WEB_FETCH_CODE_TEXT_CAP = 100   # HTTP reason phrases are short; generous bound
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
    # #177 S6: split non-prose search columns, derived by _derive_search_columns
    # on the FINAL post-augment blocks (see _normalize). ``search_aux`` is kept
    # physically (legacy column) but NEVER assigned a non-empty value — the
    # consolidated multi-column FTS reads search_tool/search_thinking instead.
    search_tool: str = ""
    search_thinking: str = ""
    search_aux: str = ""   # documented-dead (#177 S6); always "" on new rows


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
        # A message typed while the agent (the main session OR a subagent) is
        # still working is QUEUED and persisted as an ``attachment`` row, never a
        # ``type:"user"`` turn — so the user/assistant gate above would drop it and
        # it would never reach conversation_messages (the reader bug). Promote the
        # user-typed ones here; everything else stays dropped.
        return _queued_prompt_row(obj, t, offset)
    if not obj.get("uuid"):
        return None
    return _normalize(obj, t, offset)


def _queued_prompt_row(obj, t, offset):
    """A queued user prompt -> a synthetic HUMAN ``MessageRow``, else ``None``.

    Claude Code persists a message typed while the agent is busy as
    ``{"type":"attachment","attachment":{"type":"queued_command",
    "commandMode":"prompt","prompt":<text>}}`` — carrying its OWN
    uuid/parentUuid/timestamp, with the text in ``attachment.prompt`` rather than
    ``message.content``. Only ``commandMode=="prompt"`` is promoted: a queued
    ``task-notification`` (``commandMode=="task-notification"``) is harness-injected
    background plumbing — the same ``<task-notification>`` content already
    classifies META when it arrives as a regular line — not something the user
    typed, so it stays dropped."""
    if t != "attachment" or not obj.get("uuid"):
        return None
    att = obj.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "queued_command":
        return None
    if att.get("commandMode") != "prompt":
        return None
    prompt = att.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    # Route a synthesized user message through _normalize so the queued prompt
    # gets the SAME classification a typed turn would (HUMAN, the #188
    # slash-command-args promotion, system-marker / notification folding, the
    # remote-control prefix strip, and the split search-column derivation). Every
    # top-level field (uuid / parentUuid / sessionId / timestamp / cwd / gitBranch
    # / isSidechain) rides along on the shallow copy; _normalize keys off the ``t``
    # arg, never ``obj["type"]``, so the "attachment" type is inert here.
    synth = dict(obj)
    synth["message"] = {"role": "user", "content": prompt}
    return _normalize(synth, "user", offset)


@dataclass
class AiTitleRow:
    """Pure per-line AI-title record (no I/O). Parallels MessageRow but for the
    main-session ``{"type":"ai-title","aiTitle":...,"sessionId":...}`` lines that
    parse_message_row drops (type not in user/assistant). #193."""
    session_id: "str | None"
    ai_title: str
    byte_offset: int


def parse_ai_title(obj, offset):
    """Return an AiTitleRow when ``obj`` is an ai-title line with BOTH a non-empty
    string sessionId and a non-empty string aiTitle, else None. Skips the null /
    blank rewrites CC emits as the title evolves, and any malformed line. #193."""
    if obj.get("type") != "ai-title":
        return None
    sid = obj.get("sessionId")
    title = obj.get("aiTitle")
    if not (isinstance(sid, str) and sid.strip()):
        return None
    if not (isinstance(title, str) and title.strip()):
        return None
    return AiTitleRow(sid, title, offset)


def iter_ai_titles(fh, path_str):
    """Yield AiTitleRow for each ai-title line from ``fh``'s current position.
    Mirrors iter_message_rows: caller owns the open file handle; pure parse.
    ``path_str`` is accepted for signature-parity (ai-title rows don't use it)."""
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
        row = parse_ai_title(obj, offset)
        if row is not None:
            yield row


def _normalize(obj, t, offset):
    msg = obj.get("message")
    if not isinstance(msg, dict):
        msg = {}
    blocks, text = _blocks_and_text(msg.get("content"))
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
    elif obj.get("isCompactSummary"):
        # #191: compaction summary injected as a user line — authoritative flag.
        entry_type = META
        text = ""
    elif _is_notification_body(text):
        # #191: <task-notification>/<bash-notification> background completion.
        entry_type = META
        text = ""
    elif _is_bash_echo_body(text):
        # #191: <bash-input>/<bash-stdout>/<bash-stderr> `!`-mode echo. Dedicated
        # branch (NOT _MARKER_TAGS) so it can't reach the #188 promotion path.
        entry_type = META
        text = ""
    elif (blocks and all(b["kind"] == "text" for b in blocks)
          and (_inv := _extract_command_invocation(blocks, text)) is not None):
        # #188: a slash-command invocation carrying a real user prompt in
        # <command-args> IS a user turn — the wrapper is plumbing but the args
        # are what the user typed. Promote it (entry_type=HUMAN, text=args) so it
        # enters FTS / title derivation / the entry_type='human' prompts facet.
        # blocks_json is UNTOUCHED (still the raw <command-name>…), so the read
        # path derives the command-name badge from the blocks. Ordered BEFORE the
        # empty-args/stdout-only system-marker fold below — /clear, /exit,
        # /compact and friends (empty args) fall through to META there. The
        # all-text guard mirrors the marker fold so a marker+attachment row is
        # never folded NOR promoted (it stays a plain HUMAN turn in the else).
        entry_type = HUMAN
        text = _inv["args"]
    elif blocks and all(b["kind"] == "text" for b in blocks) and _is_system_marker(text):
        # A slash-command echo carried as a plain user line (NOT isMeta): the
        # user did not type it. `<local-command-stdout>…</local-command-stdout>`
        # & friends arrive as ordinary user content, so without this branch they
        # render as a "YOU" prompt and poison the conversation title (#186).
        # Classify META + text="" exactly like the isMeta branch above so the
        # body stays out of FTS, title derivation, and the entry_type='human'
        # prompts facet; the body survives in blocks_json for rendering as a
        # "System marker" pill. The all-text guard mirrors the tool_result
        # block-shape gate (Codex P1b) so an attachment-bearing row — a marker
        # text block PLUS an image — is never folded.
        entry_type = META
        text = ""
    else:
        # #191: a leading remote-control `Message sent at … UTC.` system-reminder
        # stamp is stripped from the real user reply (narrow). No-op when absent.
        entry_type = HUMAN
        text = _strip_remote_control_prefix(text)
    is_asst = t == "assistant"
    # #177 S6: derive the split search columns on the FINAL post-augment blocks
    # (every _attach_* pass above has already merged bash_stderr / answers /
    # annotations into the blocks). This is the SAME chokepoint the migration-010
    # backfill runs on json.loads(blocks_json), so live ingest and backfill
    # produce byte-identical values (the parity invariant). Computed immediately
    # before json.dumps(blocks) so blocks_json and the columns agree by
    # construction.
    search_tool, search_thinking = _derive_search_columns(blocks)
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
        search_tool=search_tool,
        search_thinking=search_thinking,
        # search_aux stays "" (documented-dead, #177 S6) — the consolidated
        # multi-column FTS reads search_tool/search_thinking instead.
    )


def _blocks_and_text(content):
    """Return (normalized blocks list, indexed-prose string).

    Prose (``text``) = joined ``text`` blocks only (thinking / tool_use /
    tool_result excluded — those go to the split search columns via
    ``_derive_search_columns``, which runs in ``_normalize`` on the FINAL
    post-augment blocks). #177 S6 dropped the in-loop ``search_aux`` accumulation
    that used to be the third return element: deriving the search columns here
    (pre-augment) would miss bash_stderr / answers / annotations and break the
    ingest==backfill parity invariant, so the derivation moved to the
    post-augment chokepoint."""
    if isinstance(content, str):
        # #186: strip ANSI on the str-content path (the slash-command-stdout
        # carrier) so neither the indexed prose nor the rendered block leaks SGR.
        content = _strip_ansi(content)
        return (([{"kind": "text", "text": content}] if content else []), content)
    blocks, texts = [], []
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
                txt = _strip_ansi(b.get("text", "") or "")   # #186: strip SGR
                blocks.append({"kind": "text", "text": txt})
                texts.append(txt)
            elif bt == "thinking":
                think = _strip_ansi(b.get("thinking", "") or "")  # #186: strip SGR
                blocks.append({"kind": "thinking", "text": think})  # FULL text for render
            elif bt == "tool_use":
                bounded, input_trunc = _bound_input(b.get("input"))
                block = {"kind": "tool_use", "name": b.get("name"),
                         "input_summary": _summarize(b.get("input")),
                         "input": bounded, "input_truncated": input_trunc,
                         "id": b.get("id"),
                         "preview": tool_preview(b.get("name"), b.get("input"))}
                # #198: stamp the true edit-family stat from the FULL input ONLY when
                # the bounded copy was clipped — the one case where the client can't
                # recount the header from `block["input"]`. Additive; omitted
                # otherwise (non-truncated cards recount from their live jsdiff hunks).
                if input_trunc:
                    edit_stat = _edit_stat_for(b.get("name"), b.get("input"))
                    if edit_stat is not None:
                        block["edit_stat"] = edit_stat
                inp = b.get("input")
                st = inp.get("subagent_type") if isinstance(inp, dict) else None
                if isinstance(st, str) and st:        # #166: spawn kind (Agent/Task)
                    block["subagent_type"] = st
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
            elif bt in _MEDIA_BLOCK_TYPES:
                blocks.append({"kind": bt, **_media(b.get("source")),
                               "index": media_index[id(b)]})
            elif bt == "tool_reference":
                blocks.append({"kind": "tool_reference", "name": b.get("name")})
    return (blocks, "\n".join(t for t in texts if t))


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
        meta["code_text"] = code_text[:_WEB_FETCH_CODE_TEXT_CAP]
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


# ---------------------------------------------------------------------------
# #198: true edit-family stat, stamped at ingest from the FULL (un-bounded) input.
#
# DiffCard's header badge (`wrote N lines` for Write, `+A −D` for Edit/MultiEdit)
# was computed client-side from `call.input`, which `_bound_input` clips to
# _INPUT_LEAF_CAP per string leaf — so a large Write/Edit reported the post-clip
# count, not the document's true total. We stamp the true {add, del} here (where
# the full input is still in hand) on TRUNCATED edit-family calls only; the client
# prefers it for the header solely while truncated-and-not-yet-loaded, so a
# non-truncated card keeps header==body parity with its rendered jsdiff hunks.
#
# Parity with the client's jsdiff (dashboard/web/src/conversations/computeDiff.ts):
# jsdiff `diffLines` is Myers-minimal, so added rows = |new_lines| − LCS and
# removed = |old_lines| − LCS. The LCS *length* is unique, so a plain LCS pass
# reproduces jsdiff's counts WITHOUT replicating its alignment. Line tokens carry
# their trailing newline (jsdiff's tokenization), so a no-newline-at-eof line is a
# distinct token from its newline-terminated twin — matching the rendered diff.
# ---------------------------------------------------------------------------
def _line_count(s):
    """Number of lines in `s`, matching computeDiff.ts::splitLines length (split on
    '\\n', drop the phantom blank from a trailing newline). Drives Write's
    `wrote N lines`, which the client builds from `computeWrite(content).length`."""
    if not s:
        return 0
    parts = s.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return len(parts)


def _line_tokens(s):
    """Tokenize `s` into jsdiff line tokens (each line keeps its trailing newline),
    mirroring jsdiff's LineDiff.tokenize so the LCS below counts what `diffLines`
    counts. `re.split(r"(\\n|\\r\\n)")` keeps separators; the trailing empty from a
    final newline is dropped, then each separator is folded onto its line."""
    if not s:
        return []
    parts = re.split(r"(\n|\r\n)", s)
    if parts and parts[-1] == "":
        parts.pop()
    tokens = []
    for i, p in enumerate(parts):
        if i % 2:                  # captured separator → fold onto the preceding line
            tokens[-1] += p
        else:
            tokens.append(p)
    return tokens


def _lcs_len(a, b):
    """Length of the longest common subsequence of token lists `a`, `b` (rolling
    1-D DP, O(len(a)·len(b)) time / O(min) space). The length is unique even when
    the LCS itself is not, which is exactly why it reproduces jsdiff's add/del
    counts."""
    if not a or not b:
        return 0
    if len(b) > len(a):
        a, b = b, a                # keep the inner row short
    prev = [0] * (len(b) + 1)
    for x in a:
        diag = 0                   # prev[j-1] before this row overwrote it
        for j in range(1, len(b) + 1):
            cur = prev[j]
            prev[j] = diag + 1 if x == b[j - 1] else (prev[j] if prev[j] >= prev[j - 1] else prev[j - 1])
            diag = cur
    return prev[len(b)]


def _diff_stat(old, new):
    """{"add", "del"} for a single old→new line diff, or None when the LCS would
    exceed the cell budget. Non-string sides coerce to '' (mirroring
    computeMultiEdit's leaf coercion)."""
    ot = _line_tokens(old if isinstance(old, str) else "")
    nt = _line_tokens(new if isinstance(new, str) else "")
    if len(ot) * len(nt) > _EDIT_STAT_LCS_CELL_BUDGET:
        return None
    lcs = _lcs_len(ot, nt)
    return {"add": len(nt) - lcs, "del": len(ot) - lcs}


def _edit_stat_for(name, inp):
    """True {"add", "del"} for an edit-family tool computed from its FULL input, or
    None when not an edit-family tool / not computable / over the LCS budget. Write
    is a pure line count (no prior content); Edit diffs old→new; MultiEdit sums per
    edit. Mirrors computeWrite/computeDiff/computeMultiEdit COUNTS exactly."""
    if not isinstance(inp, dict):
        return None
    nm = (name or "").lower()
    if nm == "write":
        content = inp.get("content")
        if not isinstance(content, str):
            return None
        return {"add": _line_count(content), "del": 0}
    if nm == "edit":
        return _diff_stat(inp.get("old_string"), inp.get("new_string"))
    if nm == "multiedit":
        edits = inp.get("edits")
        if not isinstance(edits, list):
            return None
        add = dele = 0
        for e in edits:
            e = e if isinstance(e, dict) else {}
            st = _diff_stat(e.get("old_string"), e.get("new_string"))
            if st is None:
                return None        # one over-budget edit → omit the whole stamp
            add += st["add"]
            dele += st["del"]
        return {"add": add, "del": dele}
    return None


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


def _derive_search_columns(blocks):
    """(search_tool, search_thinking) from the FINAL normalized blocks list.

    MUST run post-augmentation (bash_stderr / answers / annotations already
    merged into the blocks) so live ingest and the migration-010 backfill from
    blocks_json produce byte-identical values (#177 S6 parity invariant — the
    chokepoint runs in _normalize right before json.dumps(blocks), and the
    backfill runs it on json.loads(blocks_json)). Caps are PER-BLOCK
    (_TOOL_RESULT_CAP each), matching the old search_aux semantics — NOT a
    whole-column total. Prose is excluded (already in the ``text`` column).

    ``search_tool`` = bounded tool-input string leaves + clipped tool_result
    ``text`` + ``bash_stderr`` + bounded AskUserQuestion answers/annotations
    (real ingest stamps these as ``ask_answers``/``ask_annotations``; the raw
    ``answers``/``annotations`` keys are also read for forward compatibility and
    the synthetic-block unit tests). ``search_thinking`` = thinking text."""
    tool_parts, think_parts = [], []
    for b in blocks if isinstance(blocks, list) else []:
        if not isinstance(b, dict):
            continue
        k = b.get("kind")
        if k == "thinking":
            t = b.get("text") or ""
            if t:
                think_parts.append(t[:_TOOL_RESULT_CAP])
        elif k == "tool_use":
            tool_parts.extend(
                s[:_TOOL_RESULT_CAP] for s in _aux_strings(b.get("input")))
        elif k == "tool_result":
            t = b.get("text") or ""
            if t:
                tool_parts.append(t[:_TOOL_RESULT_CAP])
            stderr = b.get("bash_stderr") or ""
            if stderr:
                tool_parts.append(stderr[:_TOOL_RESULT_CAP])
            # answers/annotations: real ingest uses the ``ask_``-prefixed keys;
            # the bare keys are read too (forward-compat + synthetic unit tests).
            for key in ("ask_answers", "ask_annotations", "answers", "annotations"):
                tool_parts.extend(
                    s[:_TOOL_RESULT_CAP] for s in _aux_strings(b.get(key)))
    return ("\n".join(tool_parts), "\n".join(think_parts))


def _aux_strings(v):
    """Yield string leaves from a bounded input value (for the search columns)."""
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
