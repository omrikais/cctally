"""#294 S6 — pure Codex conversation normalization kernel.

Maps the S1 ``CodexPhysicalEvent`` batch for one file window to normalized rows +
file touches (the §3.1 storage shape), and provides the shared assembly helpers
(mirror pairing, canonical item grouping, rollup item count, title derivation)
that detail assembly, browse rollups, and search reuse.

Pure: no I/O, no DB, no config reads. The digest contract, taxonomy mapping,
sticky turn/model state, caps, and pairing are all deterministic functions of the
event batch, so ingest and the migration-025 replay converge.

Interface names here are imported verbatim by later S6 tasks — do not rename.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from typing import Any, Iterable

# Reuse the Claude display helpers by IMPORT (never move them). _strip_ansi lives
# in _lib_conversation; the title cap + first-non-blank-line helper in
# _lib_conversation_query. These modules are stdlib-only pure kernels.
from _lib_conversation import _strip_ansi
from _lib_conversation_query import _TITLE_MAX, _first_nonblank_line
from _lib_codex_harness_preamble import parse_harness_preamble
from _lib_codex_js_scan import scan_tool_invocations


# ── digest contract (§3.1) ────────────────────────────────────────────────────

CODEX_CONVERSATION_DIGEST_DOMAIN = b"cctally-codex-conversation-digest-v1\0"


def canonical_content(text: str | None) -> str:
    """Canonicalize row content for the digest: line-ending normalization ONLY
    (``\\r\\n`` / ``\\r`` → ``\\n``). Internal whitespace, indentation, and ANSI
    sequences are PRESERVED so differently-formatted prose or code can never share
    a digest. ``None`` canonicalizes to ``""``."""
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def content_digest(text: str | None) -> str:
    """Versioned, domain-separated digest of the exact pre-cap canonical text:
    first 32 hex chars of ``sha256(DOMAIN + utf8(canonical_content(text)))``."""
    canonical = canonical_content(text)
    return hashlib.sha256(
        CODEX_CONVERSATION_DIGEST_DOMAIN + canonical.encode("utf-8")
    ).hexdigest()[:32]


def content_len(text: str | None) -> int:
    """UTF-8 byte length of the canonical (pre-cap) text (§3.1)."""
    return len(canonical_content(text).encode("utf-8"))


# ── caps + display (§4.4 / §4.3) ──────────────────────────────────────────────

# Display/search extract cap. Mirrors _lib_conversation._TOOL_RESULT_CAP (16000):
# the ``text`` / ``search_*`` columns are capped, but the digest + content_len are
# taken over the PRE-cap canonical text (§3.1), so a stale cursor can never point
# at silently-different content. Equal-by-test to the Claude tool cap.
CODEX_TEXT_CAP = 16000

# Display title cap. Equal-by-test to the Claude first-prompt titler's _TITLE_MAX.
CODEX_TITLE_MAX = _TITLE_MAX

# Bumped when the normalized row/item contract changes in a way that requires
# deterministic replay of retained Codex events.  ``sync_codex_conversations``
# stores this in conversations.db only after a successful full sync; a missing
# or older value arms the existing byte-zero rebuild path without a schema
# migration (the store is wholly re-derivable).
CODEX_CONVERSATION_CONTRACT_VERSION = "5"

# Byte-zero Codex replay markers (spec
# docs/superpowers/specs/2026-07-30-codex-thread-source-inference-design.md
# §4.3). One `cache_meta` key per store, armed by cache migration 035 /
# conversations migration 002 and consumed inside the sync function that owns
# the correct replay semantics. They live in this kernel because three layers
# read them — the cache glue that consumes them, the read-side authority probe,
# and the doctor I/O shell — and a SQL string literal in any one of them would
# silently outlive a rename of the others.
CODEX_REPLAY_FROM_ZERO_KEY = "codex_replay_from_zero_pending"
CODEX_CONVERSATION_REPLAY_FROM_ZERO_KEY = (
    "codex_conversation_replay_from_zero_pending"
)
# Written when a whole-tree Codex sync completed and still could NOT consume
# `CODEX_REPLAY_FROM_ZERO_KEY` — the replay is stalled, not merely pending, and
# Codex transcript ingest is deferred behind it. Read by `doctor`.
CODEX_REPLAY_BLOCKED_KEY = "codex_replay_from_zero_blocked"
# Written when a BUDGETED tick declined the replay outright (public #5 §4). A
# byte-zero replay is not sliceable, so the hook path never attempts one — but
# that decline returns before the ingest-backlog record is written, so without
# this key a hook-only install whose every Codex sync is frozen reads as a
# drained store on `doctor`, in the lifecycle line and in the dashboard
# envelope. `{since, at}`: `since` is when the freeze started and carries
# forward across ticks, `at` is the most recent decline.
CODEX_REPLAY_DEFERRED_KEY = "codex_replay_from_zero_deferred"

# Structural wrapper prefixes skipped during title selection (§4.3), pinned from
# the corpus (title-wrapper-window). Prefix-structural, never content heuristics.
CODEX_TITLE_SKIP_PREFIXES: tuple[str, ...] = (
    "<environment_context>",
    "<user_instructions>",
)

_MIRROR_KINDS = frozenset({"user", "assistant", "reasoning", "meta"})


def _cap(text: str) -> tuple[str, bool]:
    """Return (capped_text, was_truncated) at CODEX_TEXT_CAP."""
    if len(text) > CODEX_TEXT_CAP:
        return text[:CODEX_TEXT_CAP], True
    return text, False


def _display_title(text: str | None) -> str:
    """Whitespace-collapsed, ANSI-stripped, capped display title (§4.3)."""
    if not text:
        return ""
    collapsed = " ".join(_strip_ansi(text).split())
    if not collapsed:
        return ""
    if len(collapsed) > CODEX_TITLE_MAX:
        return collapsed[:CODEX_TITLE_MAX].rstrip() + "…"
    return collapsed


# ── normalized-row / file-touch / sticky-state dataclasses ────────────────────


@dataclasses.dataclass(frozen=True)
class CodexNormalizedRow:
    """Mirrors codex_conversation_messages (§3.1) minus the ``id`` rowid alias."""

    conversation_key: str
    source_root_key: str
    source_path: str
    line_offset: int
    timestamp_utc: str | None
    turn_id: str | None
    call_id: str | None
    kind: str
    event_type: str | None
    record_family: str
    model: str | None
    text: str
    content_digest: str
    content_len: int
    detail_json: str | None
    search_tool: str
    search_thinking: str


@dataclasses.dataclass(frozen=True)
class CodexFileTouch:
    """A write-class file touch (§3.3). Message linkage is resolved at write time
    via ``(source_path, line_offset)`` of the owning normalized row."""

    conversation_key: str
    source_path: str
    file_path: str
    tool: str
    line_offset: int


@dataclasses.dataclass
class CodexStickyState:
    """Sticky per-file turn + model state (§4.2). Terminal value persists to
    codex_session_files for delta resumes."""

    turn_id: str | None = None
    model: str | None = None


@dataclasses.dataclass
class CodexNormalizationResult:
    rows: list[CodexNormalizedRow]
    touches: list[CodexFileTouch]
    terminal: CodexStickyState


# ── taxonomy mapping (§4.1) ───────────────────────────────────────────────────

# response_item payload subtype -> (kind, is_tool_call, is_tool_output)
_RESPONSE_TOOL_CALLS = frozenset(
    {"function_call", "custom_tool_call", "tool_search_call", "web_search_call"})
_RESPONSE_TOOL_OUTPUTS = frozenset(
    {"function_call_output", "custom_tool_call_output", "tool_search_output"})
# event_msg payload subtype -> kind (prose families + provider-truthful cards)
_EVENT_PROSE_KIND = {
    "user_message": "user",
    "agent_message": "assistant",
    "agent_reasoning": "reasoning",
}
_EVENT_CARD_TYPES = frozenset({
    "task_started", "task_complete", "context_compacted", "patch_apply_end",
    "mcp_tool_call_end", "web_search_end",
})


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return ""


def _reject_json_constant(value: str) -> None:
    """Reject NaN/Infinity tokens so card JSON stays standards-compliant."""
    raise ValueError(f"non-finite JSON constant: {value}")


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _canonical_json(value)


def _join_content_texts(content: Any) -> str:
    """Join the ``text`` leaves of a response_item message/reasoning content list."""
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict):
            txt = item.get("text")
            if isinstance(txt, str) and txt:
                parts.append(txt)
    return "\n".join(parts)


# ── card-ready native tool shaping (#331 Task A) ─────────────────────────────

CODEX_CARD_SCHEMA_VERSION = 1
_CARD_MAX_COMMANDS = 8
_CARD_MAX_PARTS = 128
_CARD_MAX_FILES = 128
_CARD_MAX_RESULTS = 50
_CARD_MAX_COLLECTION = 64
_CARD_HARNESS_PARSE_CAP = 1_000_000
_AGENT_OPERATIONS = frozenset({
    "spawn_agent", "wait_agent", "send_message", "list_agents",
    "followup_task", "interrupt_agent",
})
_EXEC_METADATA_KEYS = frozenset({
    "justification", "login", "max_output_tokens", "prefix_rule",
    "sandbox_permissions", "shell", "tty", "yield_time_ms",
})
# The FROZEN v1 ingest projection, kept deliberately rather than deleted.
#
# Live decoding moved to `_lib_codex_harness_preamble.parse_harness_preamble`
# (#463 S3, spec section 4.1), which this regex could never do: it requires a
# blank line before `Output:` and end-of-string after it, and 0 of the 39,942
# production outputs carrying the preamble it targets match it. But `_extract`
# must keep storing byte-identical `detail_json` (spec section 3.0), because
# `_row_source_bytes` feeds `PAGE_SOURCE_BYTE_BUDGET` and a richer stored card
# would move page boundaries for newly ingested rows only. So this stays as the
# stored contract until `CODEX_CONVERSATION_CONTRACT_VERSION` is bumped and the
# history is replayed. It is reached ONLY through `for_storage=True`.
_HARNESS_STATUS_RE = re.compile(
    r"\A(Script completed|Script failed)\nWall time ([^\n]+)\n\nOutput:\Z")


class _LiteralError(ValueError):
    """Closed-parser rejection; callers preserve the raw provider payload."""


class _TextBudget:
    def __init__(self, limit: int):
        self.remaining = max(0, int(limit))
        self.truncated = False

    def take(self, value: str) -> str:
        if len(value) <= self.remaining:
            self.remaining -= len(value)
            return value
        kept = value[:self.remaining]
        self.remaining = 0
        self.truncated = True
        return kept


class _HarnessLiteralParser:
    """Tiny non-executing parser for the JSON-like values in Codex harnesses.

    It accepts only objects, arrays, double-quoted JSON strings, finite JSON
    numbers, booleans and null.  Object keys may be unquoted harness tokens
    (including ``yield_time-ms``-style hyphens).  Expressions, identifiers as
    values, templates, comments and trailing code reject the shape.
    """

    def __init__(self, source: str, pos: int = 0, *, max_depth: int = 8):
        self.source = source
        self.pos = pos
        self.max_depth = max_depth

    def _ws(self) -> None:
        size = len(self.source)
        while self.pos < size and self.source[self.pos].isspace():
            self.pos += 1

    def _consume(self, token: str) -> None:
        self._ws()
        if not self.source.startswith(token, self.pos):
            raise _LiteralError(token)
        self.pos += len(token)

    def _string(self) -> str:
        self._ws()
        if self.pos >= len(self.source) or self.source[self.pos] != '"':
            raise _LiteralError("string")
        try:
            value, end = json.JSONDecoder().raw_decode(self.source, self.pos)
        except (json.JSONDecodeError, TypeError) as exc:
            raise _LiteralError("string") from exc
        if not isinstance(value, str):
            raise _LiteralError("string")
        self.pos = end
        return value

    def _key(self) -> str:
        self._ws()
        if self.pos < len(self.source) and self.source[self.pos] == '"':
            return self._string()
        match = re.match(r"[A-Za-z_$][A-Za-z0-9_$-]*", self.source[self.pos:])
        if match is None:
            raise _LiteralError("key")
        self.pos += len(match.group(0))
        return match.group(0)

    def value(self, depth: int = 0) -> Any:
        if depth > self.max_depth:
            raise _LiteralError("depth")
        self._ws()
        if self.pos >= len(self.source):
            raise _LiteralError("value")
        char = self.source[self.pos]
        if char == '"':
            return self._string()
        if char == "{":
            self.pos += 1
            result: dict[str, Any] = {}
            self._ws()
            if self.pos < len(self.source) and self.source[self.pos] == "}":
                self.pos += 1
                return result
            while len(result) < 64:
                key = self._key()
                if key in result:
                    raise _LiteralError("duplicate key")
                self._consume(":")
                result[key] = self.value(depth + 1)
                self._ws()
                if self.pos < len(self.source) and self.source[self.pos] == "}":
                    self.pos += 1
                    return result
                self._consume(",")
            raise _LiteralError("object size")
        if char == "[":
            self.pos += 1
            result_list: list[Any] = []
            self._ws()
            if self.pos < len(self.source) and self.source[self.pos] == "]":
                self.pos += 1
                return result_list
            while len(result_list) < 64:
                result_list.append(self.value(depth + 1))
                self._ws()
                if self.pos < len(self.source) and self.source[self.pos] == "]":
                    self.pos += 1
                    return result_list
                self._consume(",")
            raise _LiteralError("array size")
        for token, value in (("true", True), ("false", False), ("null", None)):
            if self.source.startswith(token, self.pos):
                self.pos += len(token)
                return value
        match = re.match(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?",
                         self.source[self.pos:])
        if match is None:
            raise _LiteralError("literal")
        token = match.group(0)
        if len(token) > 256:
            raise _LiteralError("number length")
        self.pos += len(token)
        try:
            value = float(token) if any(c in token for c in ".eE") else int(token)
        except (OverflowError, ValueError) as exc:
            raise _LiteralError("number") from exc
        if isinstance(value, float) and not (-float("inf") < value < float("inf")):
            raise _LiteralError("finite number")
        return value


def _bounded_metadata(value: Any, budget: _TextBudget) -> Any:
    if isinstance(value, str):
        return budget.take(value)
    if isinstance(value, (bool, int, float)):
        return value
    if (isinstance(value, list) and len(value) <= 32
            and all(isinstance(part, str) for part in value)):
        return [budget.take(part) for part in value]
    return None


def _bounded_json(value: Any, budget: _TextBudget, *, depth: int = 0) -> Any:
    """Retain JSON structure under one shared text/shape budget.

    Native payload readback remains authoritative; this projection is only the
    bounded card wire. Collection overflow is explicit through the caller's
    ``budget.truncated`` flag rather than silently expanding the detail route.
    """
    if depth > 8:
        budget.truncated = True
        return None
    if isinstance(value, str):
        return budget.take(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        budget.truncated = True
        return None
    if isinstance(value, list):
        if len(value) > _CARD_MAX_COLLECTION:
            budget.truncated = True
        return [
            _bounded_json(part, budget, depth=depth + 1)
            for part in value[:_CARD_MAX_COLLECTION]
        ]
    if isinstance(value, dict):
        if len(value) > _CARD_MAX_COLLECTION:
            budget.truncated = True
        result = {}
        for key, part in list(value.items())[:_CARD_MAX_COLLECTION]:
            if not isinstance(key, str):
                budget.truncated = True
                continue
            result[budget.take(key)] = _bounded_json(
                part, budget, depth=depth + 1)
        return result
    budget.truncated = True
    return None


def _json_object(value: Any) -> dict | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value) > _CARD_HARNESS_PARSE_CAP:
        return None
    try:
        parsed = json.loads(value, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _decode_shell_family_card(
    payload: dict, name: Any, budget: _TextBudget, text_cap: int,
) -> dict | None:
    """The four uncarded `function_call` families (spec section 3.2).

    `exec_command` maps onto the EXISTING `terminal` card rather than a new type,
    so `BashCard` renders it unchanged. `write_stdin` and `wait` produce
    `session_ref`; grouping happens only at `shell` scope, and a cell reference
    is never presented as a shell session. `js` produces the `program` card of
    section 3.3 carrying its authored `title`, which is the only authored
    description of a program available anywhere in the payload.
    """
    if name == "exec_command":
        arguments = _json_object(payload.get("arguments"))
        command = _command_invocation(arguments, budget)
        if command is None:
            return None
        card = {
            "schema_version": CODEX_CARD_SCHEMA_VERSION,
            "type": "terminal",
            "status": (payload.get("status")
                       if isinstance(payload.get("status"), str) else "unknown"),
            "commands": [command],
        }
        if budget.truncated:
            card["truncated"] = True
        return card
    if name in _SESSION_TOOLS:
        arguments = _json_object(payload.get("arguments"))
        session = _session_invocation(name, arguments, budget)
        if session is None:
            return None
        return {
            "schema_version": CODEX_CARD_SCHEMA_VERSION,
            "type": "session_ref", **session, "truncated": budget.truncated,
        }
    if name == "js":
        arguments = _json_object(payload.get("arguments"))
        if arguments is None:
            return None
        title = arguments.get("title")
        if title is not None and not isinstance(title, str):
            return None
        return _program_card(
            arguments.get("code"),
            title=budget.take(title) if isinstance(title, str) else None,
            text_cap=text_cap)
    if name == "tool_search_call":
        arguments = _json_object(payload.get("arguments"))
        if arguments is None or not isinstance(arguments.get("query"), str):
            return None
        limit = arguments.get("limit")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool)):
            return None
        card = {
            "schema_version": CODEX_CARD_SCHEMA_VERSION,
            "type": "tool_search", "query": budget.take(arguments["query"]),
            "limit": limit,
        }
        if budget.truncated:
            card["truncated"] = True
        return card
    return None


def decode_secondary_tool_call_card(
    payload: dict, *, text_cap: int = CODEX_TEXT_CAP, for_storage: bool = False,
) -> dict | None:
    """Bounded additive wire for plans, web actions, agent operations, and the
    shell/program families #463 S3 adds.

    Unknown or malformed shapes deliberately return ``None`` so the existing
    provider-name + raw-argument fallback remains visible and payload readback
    stays authoritative.

    ``for_storage=True`` produces the FROZEN v1 projection ``_extract`` persists
    (spec section 3.0), which means the five S3 families decode to nothing: they
    reach 34,935 rows, more than any other S3 change, and persisting them would
    move `PAGE_SOURCE_BYTE_BUDGET` boundaries for newly ingested rows only.
    """
    if not isinstance(payload, dict):
        return None
    ptype = payload.get("type")
    name = payload.get("name")
    status = payload.get("status") if isinstance(payload.get("status"), str) else "requested"
    budget = _TextBudget(text_cap)
    if not for_storage and ptype == "function_call":
        s3_card = _decode_shell_family_card(payload, name, budget, text_cap)
        if s3_card is not None:
            return s3_card
    if ptype == "function_call" and name == "update_plan":
        arguments = _json_object(payload.get("arguments"))
        plan = arguments.get("plan") if isinstance(arguments, dict) else None
        if not isinstance(plan, list) or len(plan) > _CARD_MAX_COLLECTION:
            return None
        items = []
        for item in plan:
            if not (isinstance(item, dict)
                    and isinstance(item.get("step"), str)
                    and isinstance(item.get("status"), str)):
                return None
            items.append({
                "step": budget.take(item["step"]),
                "status": budget.take(item["status"]),
            })
        card = {
            "schema_version": CODEX_CARD_SCHEMA_VERSION,
            "type": "plan", "source": "update_plan",
            "call_status": status, "items": items,
        }
        explanation = arguments.get("explanation")
        if explanation is not None:
            if not isinstance(explanation, str):
                return None
            card["explanation"] = budget.take(explanation)
        if budget.truncated:
            card["truncated"] = True
        return card
    if ptype == "web_search_call":
        action = payload.get("action")
        if not isinstance(action, (dict, str)):
            return None
        bounded_action = _bounded_json(action, budget)
        card = {
            "schema_version": CODEX_CARD_SCHEMA_VERSION,
            "type": "web_search", "source": "web_search_call",
            "call_status": status, "action": bounded_action,
        }
        if isinstance(action, dict) and isinstance(action.get("query"), str):
            card["query"] = budget.take(action["query"])
        if budget.truncated:
            card["truncated"] = True
        return card
    if ptype == "function_call" and name in _AGENT_OPERATIONS:
        arguments = _json_object(payload.get("arguments"))
        if arguments is None:
            return None
        bounded = _bounded_json(arguments, budget)
        card = {
            "schema_version": CODEX_CARD_SCHEMA_VERSION,
            "type": "agent", "operation": name,
            "call_status": status, "arguments": bounded,
        }
        if budget.truncated:
            card["truncated"] = True
        return card
    return None


def decode_secondary_tool_result(
    payload: dict, *, text_cap: int = CODEX_TEXT_CAP,
) -> dict | None:
    if not isinstance(payload, dict) or payload.get("type") not in _RESPONSE_TOOL_OUTPUTS:
        return None
    value = payload["output"] if "output" in payload else payload.get("tools")
    if isinstance(value, str) and len(value) <= _CARD_HARNESS_PARSE_CAP:
        try:
            parsed = json.loads(value, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed = value
    else:
        parsed = value
    budget = _TextBudget(text_cap)
    bounded = _bounded_json(parsed, budget)
    status = payload.get("status") if isinstance(payload.get("status"), str) else "returned"
    return {"status": status, "value": bounded, "truncated": budget.truncated}


def decode_secondary_event_card(
    payload: dict, *, text_cap: int = CODEX_TEXT_CAP,
) -> dict | None:
    if not isinstance(payload, dict):
        return None
    ptype = payload.get("type")
    budget = _TextBudget(text_cap)
    if ptype == "web_search_end":
        action = payload.get("action")
        query = payload.get("query")
        results = payload.get("results", [])
        if not isinstance(action, (dict, str)) or not isinstance(query, str):
            return None
        if not isinstance(results, list):
            return None
        if len(results) > _CARD_MAX_RESULTS:
            budget.truncated = True
        card = {
            "schema_version": CODEX_CARD_SCHEMA_VERSION,
            "type": "web_search_completion", "source": "web_search_end",
            "status": (payload.get("status")
                       if isinstance(payload.get("status"), str) else "returned"),
            "query": budget.take(query),
            "action": _bounded_json(action, budget),
            "results": [
                _bounded_json(result, budget)
                for result in results[:_CARD_MAX_RESULTS]
            ],
        }
        if "error" in payload:
            card["error"] = _bounded_json(payload.get("error"), budget)
        if budget.truncated:
            card["truncated"] = True
        return card
    if ptype == "mcp_tool_call_end":
        invocation = payload.get("invocation")
        if not isinstance(invocation, dict):
            return None
        server = invocation.get("server")
        tool = invocation.get("tool") or invocation.get("name")
        if server is not None and not isinstance(server, str):
            return None
        if not isinstance(tool, str):
            return None
        result = payload.get("result")
        if isinstance(result, dict) and "Ok" in result:
            ok_result = result.get("Ok")
            status = (
                "error"
                if isinstance(ok_result, dict) and ok_result.get("isError") is True
                else "ok"
            )
        elif isinstance(result, dict) and any(key in result for key in ("Err", "Error")):
            status = "error"
        else:
            status = (payload.get("status")
                      if isinstance(payload.get("status"), str) else "returned")
        card = {
            "schema_version": CODEX_CARD_SCHEMA_VERSION,
            "type": "mcp_completion", "source": "mcp_tool_call_end",
            "status": status,
            "server": budget.take(server) if isinstance(server, str) else None,
            "tool": budget.take(tool),
            "arguments": _bounded_json(invocation.get("arguments"), budget),
            "result": _bounded_json(result, budget),
            "duration": _bounded_json(payload.get("duration"), budget),
        }
        if budget.truncated:
            card["truncated"] = True
        return card
    return None


# The two tools that name a session rather than run a command, and what each
# names. `session_id` and `cell_id` are NOT two spellings of one thing: the
# namespaces have zero overlapping values, and a `cell_id` is a per-conversation
# sandbox-cell ordinal. `wait` polls a cell; it does not poll a shell session.
_SESSION_TOOLS = {
    "write_stdin": ("shell", "write"),
    "wait": ("cell", "poll"),
}

# `_exec_chain` outcomes. The distinction matters: a source that is not the
# strict chain grammar at all may still be a legible PROGRAM, while one that has
# the chain's shape and was refused by the literal parser or the command cap must
# keep producing no card, which is what the shipped closed-parser regression
# requires for `process.env.SECRET`, a template argument and nine invocations.
_EXEC_OK = "ok"
_EXEC_SHAPE = "shape"
_EXEC_REFUSED = "refused"


def _command_invocation(value: Any, budget: _TextBudget) -> dict | None:
    """One `exec_command` argument object as the shared `terminal` command."""
    if not isinstance(value, dict) or not isinstance(value.get("cmd"), str):
        return None
    workdir = value.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        return None
    command = {
        "workdir": budget.take(workdir) if isinstance(workdir, str) else None,
        "command": "",
        "metadata": {},
    }
    command["command"] = budget.take(value["cmd"])
    for key in sorted(_EXEC_METADATA_KEYS):
        if key not in value:
            continue
        bounded = _bounded_metadata(value[key], budget)
        if bounded is not None:
            command["metadata"][key] = bounded
    return command


def _session_invocation(name: str, value: Any, budget: _TextBudget) -> dict | None:
    """One `write_stdin`/`wait` argument object as a session reference.

    ``ref`` carries the provider's own identifier here, because the kernel has no
    conversation context. The query layer replaces a ``shell`` ref with the
    conversation-local ordinal before publishing, so no raw provider session id
    ever reaches a reader (spec section 4.3).
    """
    if not isinstance(value, dict):
        return None
    scope, operation = _SESSION_TOOLS[name]
    raw = value.get("session_id") if scope == "shell" else value.get("cell_id")
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    chars = value.get("chars")
    if chars is not None and not isinstance(chars, str):
        return None
    return {
        "scope": scope, "ref": budget.take(str(raw)), "operation": operation,
        "chars": budget.take(chars) if isinstance(chars, str) else None,
    }


def _exec_chain(source: str, *, budget: _TextBudget) -> tuple[str, list[dict] | None]:
    """Decode only the complete current exec harness statement grammar.

    Returns ``(_EXEC_OK, commands)``, ``(_EXEC_SHAPE, None)`` when the source is
    not that grammar, or ``(_EXEC_REFUSED, None)`` when it HAS the grammar's
    shape but a literal or the command cap refused it.
    """
    commands: list[dict] = []
    pos = 0
    while True:
        prefix = re.match(
            r"\s*const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*"
            r"await\s+tools\.exec_command",
            source[pos:],
        )
        if prefix is None:
            return _EXEC_SHAPE, None
        if len(commands) >= _CARD_MAX_COMMANDS:
            return _EXEC_REFUSED, None
        variable = prefix.group(1)
        parser = _HarnessLiteralParser(source, pos + prefix.end())
        try:
            parser._consume("(")
            value = parser.value()
            parser._consume(")")
        except _LiteralError:
            return _EXEC_REFUSED, None
        command = _command_invocation(value, budget)
        if command is None:
            return _EXEC_REFUSED, None
        commands.append(command)
        suffix = re.match(
            r"\s*;\s*text\s*\(\s*" + re.escape(variable)
            + r"\.output\s*\)\s*;?",
            source[parser.pos:],
        )
        if suffix is None:
            return _EXEC_SHAPE, None
        pos = parser.pos + suffix.end()
        if not source[pos:].strip():
            return _EXEC_OK, commands


def _scanned_invocations(
    source: str, *, budget: _TextBudget,
) -> tuple[list[dict], bool] | None:
    """``(invocations, over_cap)`` for a program, or ``None`` if it cannot be lexed.

    Arguments are read by the existing ``_HarnessLiteralParser``, so a non-literal
    such as ``process.env.SECRET`` is still refused — the entry degrades to
    ``other``, which names the tool and claims nothing about what it was given.
    """
    located = scan_tool_invocations(source, limit=_CARD_MAX_COMMANDS + 1)
    if located is None:
        return None
    invocations: list[dict] = []
    for name, paren in located[:_CARD_MAX_COMMANDS]:
        parser = _HarnessLiteralParser(source, paren)
        try:
            parser._consume("(")
            value = parser.value()
        except _LiteralError:
            value = None
        entry: dict | None = None
        if name == "exec_command":
            command = _command_invocation(value, budget)
            if command is not None:
                entry = {"kind": "command", **command}
        elif name in _SESSION_TOOLS:
            session = _session_invocation(name, value, budget)
            if session is not None:
                entry = {"kind": "session", **session}
        if entry is None:
            entry = {"kind": "other", "name": budget.take(name)}
        invocations.append(entry)
    return invocations, len(located) > _CARD_MAX_COMMANDS


def _program_card_from_scan(
    source: str, *, title: str | None, budget: _TextBudget,
) -> dict | None:
    """A `program` card for a source the strict chain grammar did not match."""
    scanned = _scanned_invocations(source, budget=budget)
    if scanned is None:
        return None
    invocations, over_cap = scanned
    if not invocations:
        return None
    return {
        "schema_version": CODEX_CARD_SCHEMA_VERSION,
        "type": "program", "title": title,
        # Never claim the program did only what is listed: the scanner models
        # `tools.<name>(` and nothing else, so anything reaching here contains
        # statements it did not read.
        "complete": False,
        "invocations": invocations,
        "truncated": over_cap or budget.truncated,
    }


def _program_card(
    source: Any, *, title: str | None, text_cap: int,
) -> dict | None:
    """A `program` card for an arbitrary authored program body.

    A body that IS the complete recognized chain is still a program card — the
    `js` family always renders as one — but it is marked `complete`.
    """
    if not isinstance(source, str) or len(source) > _CARD_HARNESS_PARSE_CAP:
        return None
    budget = _TextBudget(text_cap)
    outcome, commands = _exec_chain(source, budget=budget)
    if outcome == _EXEC_REFUSED:
        return None
    if outcome == _EXEC_OK:
        return {
            "schema_version": CODEX_CARD_SCHEMA_VERSION,
            "type": "program", "title": title, "complete": True,
            "invocations": [{"kind": "command", **command} for command in commands],
            "truncated": budget.truncated,
        }
    return _program_card_from_scan(
        source, title=title, budget=_TextBudget(text_cap))


def _decode_apply_patch_program(source: str) -> str | None:
    """Recognize the exact current ``const patch`` apply_patch harness."""
    match = re.match(r"\A\s*const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*", source)
    if match is None:
        return None
    parser = _HarnessLiteralParser(source, match.end())
    try:
        patch = parser._string()
    except _LiteralError:
        return None
    variable = match.group(1)
    tail = source[parser.pos:]
    pattern = (
        r"\s*;\s*text\s*\(\s*await\s+tools\.apply_patch\s*\(\s*"
        + re.escape(variable)
        + r"\s*\)\s*\)\s*;?\s*\Z"
    )
    return patch if re.fullmatch(pattern, tail) else None


def _apply_patch_heredoc(command: str) -> str | None:
    for pattern in (
        r"\A\s*apply_patch\s+<<'([A-Za-z_][A-Za-z0-9_]*)'\n(.*)\n\1\s*\Z",
        r'\A\s*apply_patch\s+<<"([A-Za-z_][A-Za-z0-9_]*)"\n(.*)\n\1\s*\Z',
        r"\A\s*apply_patch\s+<<([A-Za-z_][A-Za-z0-9_]*)\n(.*)\n\1\s*\Z",
    ):
        match = re.fullmatch(pattern, command, re.DOTALL)
        if match is not None:
            return match.group(2)
    return None


_APPLY_PATCH_ACTION_RE = re.compile(r"\*\*\* (Add|Update|Delete) File: (.+)\Z")
_APPLY_PATCH_MOVE_RE = re.compile(r"\*\*\* Move to: (.+)\Z")


def _apply_patch_row_kind(line: str) -> str:
    """Classify one V4A body row the way the client's diff parser does."""
    head = line[:1]
    if head == "+":
        return "add"
    if head == "-":
        return "del"
    if head == "\\":
        return "marker"
    return "context"


def _apply_patch_unified_diff(
    old_path: str, new_path: str, body: list[str],
) -> str | None:
    """One section of a proven ``apply_patch`` envelope as a unified diff.

    The V4A envelope states no line offsets — its ``@@`` markers carry context
    TEXT, not numbers — so each hunk header counts the rows it contains and
    numbers them relative to the file's first hunk. That is the same relative
    numbering the edit-diff card has always rendered under, and the running
    counts continue across a file's hunks so two hunks never show one gutter
    number twice.

    A section with no body row at all — a V4A ``*** Delete File:`` names the
    file and carries nothing — yields ``None``, so the card claims no diff
    rather than publishing an empty one.
    """
    hunks: list[list[str]] = []
    current: list[str] | None = None
    for line in body:
        if line.startswith("@@"):
            current = []
            hunks.append(current)
            continue
        if current is None:
            current = []
            hunks.append(current)
        current.append(line)
    hunks = [hunk for hunk in hunks if hunk]
    if not hunks:
        return None
    out = [f"--- {old_path}\n", f"+++ {new_path}\n"]
    old_at = new_at = 1
    for hunk in hunks:
        kinds = [_apply_patch_row_kind(line) for line in hunk]
        old_count = sum(1 for kind in kinds if kind in ("context", "del"))
        new_count = sum(1 for kind in kinds if kind in ("context", "add"))
        out.append(
            f"@@ -{old_at if old_count else 0},{old_count} "
            f"+{new_at if new_count else 0},{new_count} @@\n")
        out.extend(f"{line}\n" for line in hunk)
        old_at += old_count
        new_at += new_count
    return "".join(out)


def _patch_files_from_apply_patch(
    patch: str, budget: _TextBudget | None = None, *, with_diffs: bool = False,
) -> list[dict]:
    """The file entries of a proven ``apply_patch`` envelope.

    ``with_diffs`` is READ TIME only. The stored v1 projection is the file LIST
    it has always been, because `detail_json` bytes feed `_row_source_bytes` and
    therefore page boundaries; publishing the diffs there would paginate a newly
    ingested conversation differently from an identical historical one.

    At read time the entries gain the same field vocabulary the
    ``patch_apply_end`` card publishes — ``unified_diff``, ``diff_source`` and
    per-file ``truncated`` — through the same `_allocate_diff_budget`
    allocation, so one patch does not read two ways depending on which side of
    it the reader is looking at.
    """
    files: list[dict] = []
    raw_paths: list[str] = []
    bodies: list[list[str]] = []
    # Past the file cap a section is dropped whole, so its body must not be
    # attributed to the previous file's diff.
    collecting = False
    for line in patch.splitlines():
        match = _APPLY_PATCH_ACTION_RE.match(line)
        if match is not None:
            collecting = len(files) < _CARD_MAX_FILES
            if collecting:
                status = {"Add": "added", "Update": "modified", "Delete": "deleted"}[
                    match.group(1)]
                path = (budget.take(match.group(2)) if budget is not None
                        else match.group(2))
                files.append({"path": path, "status": status})
                raw_paths.append(match.group(2))
                bodies.append([])
            continue
        move = _APPLY_PATCH_MOVE_RE.match(line)
        if move is not None and collecting and files:
            files[-1]["move_path"] = (
                budget.take(move.group(1)) if budget is not None else move.group(1))
            files[-1]["status"] = "moved"
            continue
        if line.startswith("*** "):
            continue
        if collecting and bodies:
            bodies[-1].append(line)
    if not with_diffs:
        return files
    diffs: list[str | None] = []
    for entry, raw_path, body in zip(files, raw_paths, bodies):
        # The header names the PROVIDER's own path, which can differ from the
        # entry's when the budget clipped the latter — the same rule the
        # synthesized event-side diff follows.
        move_path = entry.get("move_path")
        if entry["status"] == "added":
            old_path, new_path = "/dev/null", raw_path
        elif entry["status"] == "deleted":
            old_path, new_path = raw_path, "/dev/null"
        else:
            old_path, new_path = raw_path, move_path or raw_path
        diff = _apply_patch_unified_diff(old_path, new_path, body)
        # `truncated` on EVERY entry, so a client never has to read an absent
        # key as false; `_allocate_diff_budget` overwrites it where a diff runs.
        entry["truncated"] = False
        if diff is not None:
            # The provider transmitted a patch envelope, not a unified diff, so
            # this is a rendering of retained content — never `retained`.
            entry["diff_source"] = "derived"
        diffs.append(diff)
    if budget is not None:
        _allocate_diff_budget(files, diffs, budget)
    else:
        for entry, diff in zip(files, diffs):
            if diff is not None:
                entry["unified_diff"] = diff
    return files


def _complete_apply_patch(patch: str) -> bool:
    """Require one closed apply_patch envelope with at least one file action."""
    lines = patch.splitlines()
    if (not lines or lines[0] != "*** Begin Patch"
            or lines[-1] != "*** End Patch"):
        return False
    if lines.count("*** Begin Patch") != 1 or lines.count("*** End Patch") != 1:
        return False
    return any(re.fullmatch(r"\*\*\* (?:Add|Update|Delete) File: .+", line)
               for line in lines[1:-1])


def decode_tool_call_card(
    payload: dict, *, text_cap: int = CODEX_TEXT_CAP, for_storage: bool = False,
) -> dict | None:
    """Return the additive card contract for a structurally proven call.

    ``for_storage=True`` produces the FROZEN v1 projection ``_extract`` persists
    (spec section 3.0): the `program` card is a read-time addition and is not
    stored, because it would newly appear on 17,777 rows and move
    `PAGE_SOURCE_BYTE_BUDGET` boundaries for whichever binary ingested them.
    """
    if not isinstance(payload, dict) or payload.get("type") != "custom_tool_call":
        return None
    name = payload.get("name")
    value = payload.get("input")
    if not isinstance(name, str) or not isinstance(value, str):
        return None
    if len(value) > _CARD_HARNESS_PARSE_CAP:
        return None
    budget = _TextBudget(text_cap)
    status = payload.get("status") if isinstance(payload.get("status"), str) else "unknown"
    if name == "apply_patch" and _complete_apply_patch(value):
        # The derived diffs are taken from the shared budget BEFORE the raw
        # `patch`: `patch` is the payload disclosure's copy and no card renders
        # it, while the diffs are the only thing on this card a reader reads.
        files = _patch_files_from_apply_patch(
            value, budget, with_diffs=not for_storage)
        patch = budget.take(value)
        return {
            "schema_version": CODEX_CARD_SCHEMA_VERSION,
            "type": "patch", "source": "apply_patch", "status": status,
            "patch": patch, "files": files,
            "truncated": budget.truncated,
        }
    if name != "exec":
        return None
    patch_value = _decode_apply_patch_program(value)
    if patch_value is not None and _complete_apply_patch(patch_value):
        files = _patch_files_from_apply_patch(
            patch_value, budget, with_diffs=not for_storage)
        patch = budget.take(patch_value)
        return {
            "schema_version": CODEX_CARD_SCHEMA_VERSION,
            "type": "patch", "source": "tools.apply_patch", "status": status,
            "patch": patch, "files": files,
            "truncated": budget.truncated,
        }
    outcome, commands = _exec_chain(value, budget=budget)
    if outcome == _EXEC_REFUSED:
        # The chain's SHAPE with a literal or the command cap refusing it. This
        # is the arm the shipped closed-parser regression pins: a
        # `process.env.SECRET` argument, a template argument and nine
        # invocations must all keep producing no card at all.
        return None
    if outcome == _EXEC_SHAPE:
        # Not the chain grammar — but 17,777 uncarded `exec` calls are programs
        # that declare constants, filter collections and invoke two different
        # tool families, and one `terminal` card cannot express that.
        if for_storage:
            return None
        return _program_card_from_scan(
            value, title=None, budget=_TextBudget(text_cap))
    if len(commands) == 1:
        heredoc = _apply_patch_heredoc(commands[0]["command"])
        if heredoc is not None and _complete_apply_patch(heredoc):
            files = _patch_files_from_apply_patch(
                heredoc, budget, with_diffs=not for_storage)
            return {
                "schema_version": CODEX_CARD_SCHEMA_VERSION,
                "type": "patch", "source": "exec_apply_patch", "status": status,
                "patch": budget.take(heredoc), "files": files,
                "workdir": commands[0]["workdir"], "truncated": budget.truncated,
            }
    card = {
        "schema_version": CODEX_CARD_SCHEMA_VERSION,
        "type": "terminal", "status": status, "commands": commands,
    }
    if budget.truncated:
        card["truncated"] = True
    return card


def _output_head_text(value: Any) -> str | None:
    """The text of an output's FIRST part, for the two shapes that carry one.

    Both the array and the bare-string envelope occur for both record types, and
    the preamble is positionally guaranteed to be at the head of whichever one
    arrives, so this is the only place it may be looked for (spec section 4.1).
    """
    if isinstance(value, str):
        return value
    if (isinstance(value, dict) and value.get("type") == "input_text"
            and isinstance(value.get("text"), str)):
        return value["text"]
    return None


def decode_tool_output_card(
    payload: dict, *, text_cap: int = CODEX_TEXT_CAP, for_storage: bool = False,
) -> tuple[dict, str] | None:
    """Unwrap supported output envelopes without losing malformed parts.

    ``for_storage=True`` produces the FROZEN v1 projection ``_extract`` persists
    (spec section 3.0): no ``exit_code``, no ``wall_time_seconds``, the status
    resolved only by ``_HARNESS_STATUS_RE``, and the preamble left in the part.
    Every reader path takes the default, which resolves the status from the five
    real grammars and removes the consumed lines from the rendered parts (spec
    sections 4.3 and 4.4).
    """
    if not isinstance(payload, dict) or payload.get("type") not in _RESPONSE_TOOL_OUTPUTS:
        return None
    value = payload["output"] if "output" in payload else payload.get("tools")
    values = value if isinstance(value, list) else [value]
    budget = _TextBudget(text_cap)
    parts: list[dict] = []
    status = payload.get("status") if isinstance(payload.get("status"), str) else "unknown"
    fields: dict = {}
    preamble = None
    drop_head = False
    head = _output_head_text(values[0]) if values else None
    if head is not None:
        if for_storage:
            match = _HARNESS_STATUS_RE.fullmatch(head)
            if match is not None:
                status = ("completed" if match.group(1) == "Script completed"
                          else "failed")
                drop_head = True
        else:
            preamble = parse_harness_preamble(head)
            if preamble is not None:
                fields = preamble[0]
                # A resolved state is evidence and wins. `unknown` adds nothing,
                # so it leaves `status` alone — which is what keeps the query
                # layer's unknown-gated backfill covering the remainder (4.6).
                if fields["status"] != "unknown":
                    status = fields["status"]
                drop_head = not preamble[1]

    for index, part in enumerate(values[:_CARD_MAX_PARTS]):
        if isinstance(part, str) or (
                isinstance(part, dict) and part.get("type") == "input_text"
                and isinstance(part.get("text"), str)):
            text = part if isinstance(part, str) else part["text"]
            if index == 0 and head is not None:
                if drop_head:
                    continue
                if preamble is not None:
                    text = preamble[1]
            stream = "output"
            if isinstance(part, dict) and part.get("stream") in {"stdout", "stderr"}:
                stream = part["stream"]
            parts.append({"type": "text", "stream": stream, "text": budget.take(text)})
            continue
        raw = _canonical_json(part)
        parts.append({"type": "raw", "stream": "output", "text": budget.take(raw)})
    if len(values) > _CARD_MAX_PARTS:
        budget.truncated = True
    card = {
        "schema_version": CODEX_CARD_SCHEMA_VERSION,
        "type": "terminal_output", "status": status,
        "is_error": status in {"failed", "error"},
        "parts": parts, "truncated": budget.truncated,
    }
    if not for_storage:
        # Null when the grammar did not supply it. No `chunk_id` and no
        # `session_id`: a per-call hash has no reading value, and the session
        # identity a reader sees is the conversation-local ordinal from the
        # detail envelope's index, never the provider's own id (section 4.3).
        card["exit_code"] = fields.get("exit_code")
        card["wall_time_seconds"] = fields.get("wall_time_seconds")
    return card, "".join(part["text"] for part in parts)


# The keys the dict-shaped `changes` entry is known to carry. Anything else goes
# to `raw_extra`, so a provider addition is preserved rather than dropped.
_PATCH_CHANGE_KEYS = frozenset({"type", "unified_diff", "content", "move_path"})


def _synthesized_unified_diff(path: str, kind: str, content: str) -> str:
    """A unified diff for an ``add`` or ``delete`` entry, from its `content`.

    The bytes are specified exactly because ``NativePatchCard`` does not render
    prefixed lines — it requires a parseable hunk. An empty file emits a
    zero-length hunk rather than a malformed one, and content that does not end
    in a newline gets the ``\\ No newline at end of file`` marker so the stated
    line count and the rendered result agree.

    This is a rendering of retained content, not something the provider sent,
    which is why every entry built from it is marked ``diff_source: derived``.
    """
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
        complete = True
    else:
        complete = False
    count = len(lines)
    if kind == "add":
        header = f"--- /dev/null\n+++ {path}\n"
        hunk = "@@ -0,0 +0,0 @@\n" if count == 0 else f"@@ -0,0 +1,{count} @@\n"
        body = "".join(f"+{line}\n" for line in lines)
    else:
        header = f"--- {path}\n+++ /dev/null\n"
        hunk = "@@ -0,0 +0,0 @@\n" if count == 0 else f"@@ -1,{count} +0,0 @@\n"
        body = "".join(f"-{line}\n" for line in lines)
    marker = "" if complete or count == 0 else "\\ No newline at end of file\n"
    return header + hunk + body + marker


def _clip_to_line_boundary(text: str, limit: int) -> tuple[str, bool]:
    """``(text, was_cut)``, cut at a line boundary so the result still parses."""
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    return (text[:cut + 1] if cut >= 0 else ""), True


def _diff_body_survived(diff: str) -> bool:
    """True iff ``diff`` still carries a line a hunk can render.

    `has_diff` means a diff is RENDERABLE (spec section 3.1), and a clip that
    keeps only the `---`/`+++`/`@@` headers renders nothing at all — the very
    shape a minified single-line file produces, because the whole file body is
    one physical line and the clip cuts at a line boundary. The same clip can
    also keep nothing at all, when the first line is already longer than the
    share.

    A diff that was NOT cut is never tested against this, so the deliberate
    zero-length hunk an empty file emits (`@@ -0,0 +0,0 @@` with no body, spec
    section 3.1) keeps its diff.
    """
    seen_hunk = False
    for line in diff.split("\n"):
        if line.startswith("@@"):
            seen_hunk = True
        elif seen_hunk and line[:1] in ("+", "-", " ", "\\"):
            return True
    return False


def _allocate_diff_budget(entries: list[dict], diffs: list[str | None],
                          budget: _TextBudget) -> None:
    """Divide what is left of the shared budget equally among the files.

    The single 16,000-character budget covers every file plus stdout and stderr,
    so a per-file cap is an ALLOCATION of that budget rather than an addition to
    it. A file needing less returns the remainder to the pool for later files; a
    file exceeding its share is cut at a line boundary and sets its own
    `truncated`. This is what stops the average 20,859-character deletion from
    consuming the budget belonging to the other files in the 487 events that
    touch two or more files.

    A cut that leaves nothing renderable publishes NO ``unified_diff`` at all,
    only `truncated` and `diff_source`, so the client never receives a key it
    cannot render while `has_diff` claims it can.
    """
    pending = sum(1 for diff in diffs if diff is not None)
    for entry, diff in zip(entries, diffs):
        if diff is None:
            continue
        share = budget.remaining // pending if pending else 0
        kept, was_cut = _clip_to_line_boundary(diff, share)
        pending -= 1
        entry["truncated"] = was_cut
        if was_cut:
            budget.truncated = True
            if not _diff_body_survived(kept):
                continue
        entry["unified_diff"] = budget.take(kept)


def _patch_files_from_changes_map(changes: dict, budget: _TextBudget) -> list[dict]:
    """The dict-shaped ``changes`` branch (spec section 3.1).

    The path comes from the dict KEY and the status from the entry's ``type``,
    not from ``path``/``status`` — the two shapes disagree on both, which is why
    a direct port of the list loop would yield `(unknown file)` and a null status
    on every entry.
    """
    entries: list[dict] = []
    diffs: list[str | None] = []
    for path, change in list(changes.items())[:_CARD_MAX_FILES]:
        if not isinstance(path, str):
            continue
        # Per-file `truncated` is a wire field on EVERY entry (spec section 3.1),
        # so it is set here rather than only where a diff exists. Without it a
        # client would have to treat absent as false on the entries that carry no
        # diff, which is not what the spec describes.
        entry: dict[str, Any] = {"path": budget.take(path), "truncated": False}
        if not isinstance(change, dict):
            entry["raw"] = budget.take(_canonical_json(change))
            entries.append(entry)
            diffs.append(None)
            continue
        kind = change.get("type")
        if isinstance(kind, str):
            entry["status"] = budget.take(kind)
        if isinstance(change.get("move_path"), str):
            entry["move_path"] = budget.take(change["move_path"])
        diff: str | None = None
        if isinstance(change.get("unified_diff"), str):
            diff = change["unified_diff"]
            entry["diff_source"] = "retained"
        elif kind in {"add", "delete"} and isinstance(change.get("content"), str):
            # The PROVIDER's path, never `entry["path"]`, which the shared budget
            # may have clipped. A `---`/`+++` header naming a truncated path
            # would make the one card family whose purpose is provider-truth
            # assert a file that does not exist; the synthesized diff is clipped
            # by the shared allocation below, exactly as a retained one is.
            diff = _synthesized_unified_diff(path, kind, change["content"])
            entry["diff_source"] = "derived"
        unknown = {key: value for key, value in change.items()
                   if key not in _PATCH_CHANGE_KEYS}
        if unknown:
            entry["raw_extra"] = budget.take(_canonical_json(unknown))
        entries.append(entry)
        diffs.append(diff)
    _allocate_diff_budget(entries, diffs, budget)
    return entries


def decode_patch_event_card(
    payload: dict, *, text_cap: int = CODEX_TEXT_CAP, for_storage: bool = False,
) -> dict | None:
    """Bounded, provider-truthful ``patch_apply_end`` projection.

    ``for_storage=True`` produces the FROZEN v1 projection ``_extract``
    persists (spec section 3.0): a dict-shaped ``changes`` object becomes one
    ``{"raw": …}`` entry, as it does today. Persisting the decoded per-file
    diffs would be the largest `detail_json` growth in S3 and would move
    `PAGE_SOURCE_BYTE_BUDGET` boundaries for newly ingested rows only.
    """
    if not isinstance(payload, dict) or payload.get("type") != "patch_apply_end":
        return None
    budget = _TextBudget(text_cap)
    files: list[dict] = []
    changes = payload.get("changes")
    stdout: str | None = None
    stderr: str | None = None
    streams_taken = False
    if not for_storage and isinstance(changes, dict):
        # stdout and stderr come off the shared budget FIRST here, because what
        # is left is what the per-file allocation divides.
        stdout = (budget.take(payload["stdout"])
                  if isinstance(payload.get("stdout"), str) else None)
        stderr = (budget.take(payload["stderr"])
                  if isinstance(payload.get("stderr"), str) else None)
        streams_taken = True
        files = _patch_files_from_changes_map(changes, budget)
        if len(changes) > _CARD_MAX_FILES:
            budget.truncated = True
    elif isinstance(changes, list):
        for change in changes[:_CARD_MAX_FILES]:
            if not isinstance(change, dict):
                blob = _canonical_json(change)
                entry = {"raw": budget.take(blob)}
                if not for_storage:
                    entry["truncated"] = len(entry["raw"]) != len(blob)
                files.append(entry)
                continue
            entry: dict[str, Any] = {}
            entry_cut = False
            for key in ("path", "move_path", "status"):
                if isinstance(change.get(key), str):
                    entry[key] = budget.take(change[key])
                    entry_cut = entry_cut or len(entry[key]) != len(change[key])
            if isinstance(change.get("unified_diff"), str):
                entry["unified_diff"] = budget.take(change["unified_diff"])
                entry_cut = entry_cut or (
                    len(entry["unified_diff"]) != len(change["unified_diff"]))
                # The provider transmitted this one; the card must not claim a
                # synthesized diff and a retained diff are the same thing.
                # Read time ONLY: this is an added field, and adding it to the
                # stored card would grow `detail_json` for newly ingested rows
                # while historical rows kept their old estimates, which is
                # precisely the page-boundary drift spec section 3.0 forbids.
                if not for_storage:
                    entry["diff_source"] = "retained"
            unknown = {key: value for key, value in change.items()
                       if key not in {"path", "move_path", "status", "unified_diff"}}
            if unknown:
                blob = _canonical_json(unknown)
                entry["raw_extra"] = budget.take(blob)
                entry_cut = entry_cut or len(entry["raw_extra"]) != len(blob)
            # Per-file `truncated` on EVERY entry (spec section 3.1), read time
            # only for the same reason `diff_source` is.
            if not for_storage:
                entry["truncated"] = entry_cut
            files.append(entry)
        if len(changes) > _CARD_MAX_FILES:
            budget.truncated = True
    elif "changes" in payload:
        blob = _canonical_json(changes)
        entry = {"raw": budget.take(blob)}
        if not for_storage:
            entry["truncated"] = len(entry["raw"]) != len(blob)
        files.append(entry)
    if not streams_taken:
        stdout = (budget.take(payload["stdout"])
                  if isinstance(payload.get("stdout"), str) else None)
        stderr = (budget.take(payload["stderr"])
                  if isinstance(payload.get("stderr"), str) else None)
    status = payload.get("status") if isinstance(payload.get("status"), str) else "unknown"
    success = payload.get("success") if isinstance(payload.get("success"), bool) else None
    return {
        "schema_version": CODEX_CARD_SCHEMA_VERSION,
        "type": "patch", "source": "patch_apply_end",
        "files": files,
        # RENDERABLE, not merely present (spec section 3.1). An entry whose clip
        # kept nothing a hunk can render publishes no `unified_diff` at all, and
        # a provider that transmits an empty one must not flip this to true.
        "has_diff": any(entry.get("unified_diff") for entry in files),
        "status": status, "success": success, "stdout": stdout, "stderr": stderr,
        "truncated": budget.truncated,
    }


# ── the external-agent marker (F9, spec section 5.5) ─────────────────────────
#
# Anchored at the start of a line. The name is 1-128 characters excluding `]` and
# newline; the next line must open with `input: ` and carry a JSON value.
# Validation is all-or-nothing: a name that does not match, an absent `input:`
# line, or JSON that does not parse leaves the block as ordinary prose.
_EXTERNAL_CALL_RE = re.compile(
    r"^\[external_agent_tool_call: ([^\]\n]{1,128})\]\n", re.MULTILINE)
_EXTERNAL_CALL_INPUT = "input: "


def _inside_fenced_block(text: str, position: int) -> bool:
    """True when ``position`` falls inside an open Markdown fence.

    A marker inside a fenced code block is authored content — someone writing
    ABOUT the grammar — rather than a serialized call, so it must stay prose.
    Same toggle rule `_segment_harness_markers` already uses.
    """
    fence: str | None = None
    for line in text[:position].splitlines():
        opened = re.match(r"\s*(`{3,}|~{3,})", line)
        if opened is None:
            continue
        char = opened.group(1)[0]
        if fence is None:
            fence = char
        elif fence == char:
            fence = None
    return fence is not None


def _external_call_from_text(
    text: str, *, text_cap: int = CODEX_TEXT_CAP,
) -> dict | None:
    """The `external_agent_tool_call` marker on an assistant row, or ``None``.

    This runs at READ time over the row's stored text and needs no payload load,
    which is the only way it can reach the data it targets: all 3,789 markers are
    historical, dated 2026-07-11 across 46 conversations, and the grammar has not
    recurred — so an ingest-time implementation would detect nothing at all and
    would ship as a no-op against the only rows it exists for.

    The caller writes this to ``detail.external_call`` and must NEVER write it to
    ``detail.markers``, because that key selects which rows the export path
    hydrates and 729 assistant rows already carry it.

    ``span`` is the half-open ``[start, end)`` character range of the marker run
    the card consumed, measured in the SAME string this was handed — which is the
    string the caller serves as ``block["text"]``. The export keeps that prose
    verbatim and its bytes are frozen, so the client has no other way to locate
    what the card already renders, and without the span the viewer would show the
    marker twice. The range covers the ``[external_agent_tool_call: …]`` line,
    the ``input:`` line and the JSON value, plus one immediately following
    newline when there is one, so removing it leaves no orphaned blank line.
    """
    if not isinstance(text, str) or not text:
        return None
    if len(text) > _CARD_HARNESS_PARSE_CAP:
        return None
    match = _EXTERNAL_CALL_RE.search(text)
    if match is None:
        return None
    if _inside_fenced_block(text, match.start()):
        return None
    rest = text[match.end():]
    if not rest.startswith(_EXTERNAL_CALL_INPUT):
        return None
    try:
        value, end = json.JSONDecoder().raw_decode(rest, len(_EXTERNAL_CALL_INPUT))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    start = match.start()
    end += match.end()
    if end < len(text) and text[end] == "\n":
        end += 1
    if not 0 <= start < end <= len(text):
        # Fail closed. A span that does not resolve is worse than no card: the
        # client would hide the wrong run of text, or none at all.
        return None
    budget = _TextBudget(text_cap)
    return {
        "schema_version": CODEX_CARD_SCHEMA_VERSION,
        "name": budget.take(match.group(1)),
        "input": _bounded_json(value, budget),
        "truncated": budget.truncated,
        "span": [start, end],
    }


def external_call_span_resolves(text: Any, external: Any) -> bool:
    """True iff ``external``'s span addresses its own marker inside ``text``.

    The assembler calls this against the exact string it is about to serve as
    ``block["text"]``, and withholds the card when it returns False. That is the
    fail-closed half of the span contract: a marker the served text does not
    wholly contain — because the row's text was capped at ingest, or because a
    later stage replaced it — yields no ``external_call`` at all rather than a
    span pointing at the wrong characters.
    """
    if not isinstance(text, str) or not isinstance(external, dict):
        return False
    span = external.get("span")
    if not isinstance(span, list) or len(span) != 2:
        return False
    start, end = span
    for bound in (start, end):
        if not isinstance(bound, int) or isinstance(bound, bool):
            return False
    if not 0 <= start < end <= len(text):
        return False
    return text[start:end].startswith("[external_agent_tool_call: ")


@dataclasses.dataclass
class _Extracted:
    kind: str
    content_text: str          # pre-cap, feeds digest + content_len
    column: str                # "text" | "search_tool" | "search_thinking"
    detail: dict | None
    touches: list[tuple[str, str]]  # (file_path, tool)
    identity_text: str | None = None  # raw pre-segmentation mirror/payload identity


_REASONING_TITLE_RE = re.compile(r"\A\*\*([^\n]+)\*\*\Z")
_MARKER_DIRECTIVE_RE = re.compile(r"\A::([a-z0-9-]+)\{(.*)\}\Z")
_MARKER_ATTR_RE = re.compile(
    r'([A-Za-z][A-Za-z0-9]*)=("(?:[^"\\]|\\.)*"|true|false)')
_MARKER_DIRECTIVES = {
    "git-create-branch": ({"cwd", "branch"}, "create_branch"),
    "git-stage": ({"cwd"}, "stage"),
    "git-commit": ({"cwd"}, "commit"),
    "git-push": ({"cwd", "branch"}, "push"),
    "git-create-pr": ({"cwd", "branch", "url", "isDraft"}, "create_pr"),
}
_MEM_CITATION_RE = re.compile(
    r"\A[^<>\r\n]+:\d+-\d+\|note=\[[^\]\r\n]*\]\Z")
_ROLLOUT_ID_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)


def _reasoning_projection(
    *, source: str, summary: str = "", body: str = "",
) -> dict | None:
    """Provider-truthful title/summary/body projection for non-empty reasoning."""
    summary_nonblank = summary.strip()
    body_nonblank = body.strip()
    if not summary_nonblank and not body_nonblank:
        return None
    result = {"schema_version": 1, "source": source}
    match = _REASONING_TITLE_RE.fullmatch(summary_nonblank)
    if match is not None:
        title = match.group(1)
        if title.strip() == title and "**" not in title:
            result["title"] = title
        else:
            result["summary"] = summary_nonblank
    elif summary_nonblank:
        result["summary"] = summary_nonblank
    if body_nonblank:
        result["body"] = body_nonblank
    return result


def _parse_marker_directive(line: str) -> dict | None:
    matched = _MARKER_DIRECTIVE_RE.fullmatch(line)
    if matched is None or matched.group(1) not in _MARKER_DIRECTIVES:
        return None
    name, source = matched.group(1), matched.group(2)
    attrs: dict[str, object] = {}
    pos = 0
    while pos < len(source):
        while pos < len(source) and source[pos] == " ":
            pos += 1
        token = _MARKER_ATTR_RE.match(source, pos)
        if token is None or token.group(1) in attrs:
            return None
        raw = token.group(2)
        try:
            value = json.loads(raw) if raw.startswith('"') else raw == "true"
        except (json.JSONDecodeError, TypeError):
            return None
        attrs[token.group(1)] = value
        pos = token.end()
        if pos < len(source) and source[pos] != " ":
            return None
    required, action = _MARKER_DIRECTIVES[name]
    if set(attrs) != required:
        return None
    if any(not isinstance(attrs[key], str) or not attrs[key]
           for key in required - {"isDraft"}):
        return None
    if name == "git-create-pr":
        if not isinstance(attrs.get("isDraft"), bool):
            return None
        if not str(attrs.get("url", "")).startswith(("https://", "http://")):
            return None
    marker = {"schema_version": 1, "type": "git", "action": action}
    if name == "git-create-pr":
        marker["draft"] = attrs["isDraft"]
    return marker


def _parse_memory_citation(lines: list[str]) -> dict | None:
    try:
        citation_open = lines.index("<citation_entries>")
        citation_close = lines.index("</citation_entries>")
        rollout_open = lines.index("<rollout_ids>")
        rollout_close = lines.index("</rollout_ids>")
    except ValueError:
        return None
    if not (
        lines[0] == "<oai-mem-citation>"
        and lines[-1] == "</oai-mem-citation>"
        and citation_open == 1
        and citation_open < citation_close < rollout_open < rollout_close
        and rollout_close == len(lines) - 2
        and rollout_open == citation_close + 1
    ):
        return None
    citations = lines[citation_open + 1:citation_close]
    rollouts = lines[rollout_open + 1:rollout_close]
    if not citations or not all(_MEM_CITATION_RE.fullmatch(line) for line in citations):
        return None
    if not all(_ROLLOUT_ID_RE.fullmatch(line) for line in rollouts):
        return None
    return {
        "schema_version": 1, "type": "memory_citation",
        "citation_count": len(citations), "rollout_count": len(rollouts),
    }


def _segment_harness_markers(text: str) -> tuple[str, list[dict]]:
    """Segment only a closed trailing suffix; authored/fenced lookalikes stay text."""
    lines = text.splitlines()
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    reversed_markers: list[dict] = []
    while end:
        marker = _parse_marker_directive(lines[end - 1])
        start = end - 1
        if marker is None and lines[end - 1] == "</oai-mem-citation>":
            starts = [index for index in range(end - 2, -1, -1)
                      if lines[index] == "<oai-mem-citation>"]
            if starts:
                start = starts[0]
                marker = _parse_memory_citation(lines[start:end])
        if marker is None:
            break
        # A suffix that begins inside an open Markdown fence is authored code.
        fence: str | None = None
        for line in lines[:start]:
            opened = re.match(r"\s*(`{3,}|~{3,})", line)
            if opened is None:
                continue
            char = opened.group(1)[0]
            if fence is None:
                fence = char
            elif fence == char:
                fence = None
        if fence is not None:
            break
        reversed_markers.append(marker)
        end = start
        while end and not lines[end - 1].strip():
            end -= 1
    if not reversed_markers:
        return text, []
    return "\n".join(lines[:end]).rstrip(), list(reversed(reversed_markers))


_TASK_STARTED_KEYS = frozenset({
    "type", "collaboration_mode_kind", "model_context_window", "started_at", "turn_id",
})
_TASK_COMPLETE_KEYS = frozenset({
    "type", "completed_at", "duration_ms", "last_agent_message", "started_at",
    "time_to_first_token_ms", "turn_id",
})


def _lifecycle_projection(payload: dict) -> dict:
    """Safe lifecycle detail plus internal evidence used by canonical folding."""
    event = payload.get("type")
    public: dict[str, object] = {
        "schema_version": 1, "event": event,
        "state": "started" if event == "task_started" else "completed",
    }
    allowed = _TASK_STARTED_KEYS if event == "task_started" else _TASK_COMPLETE_KEYS
    valid = set(payload) <= allowed
    turn_id = payload.get("turn_id")
    valid = valid and isinstance(turn_id, str) and bool(turn_id)
    if event == "task_started":
        for src, dst in (
            ("started_at", "at"),
            ("collaboration_mode_kind", "collaboration_mode_kind"),
        ):
            value = payload.get(src)
            if value is not None:
                if src == "started_at" and (
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value)
                ):
                    public[dst] = value
                elif not isinstance(value, str):
                    valid = False
                else:
                    public[dst] = value
        window = payload.get("model_context_window")
        if window is not None:
            if not isinstance(window, int) or isinstance(window, bool):
                valid = False
            else:
                public["model_context_window"] = window
        return {"lifecycle": public, "_lifecycle_foldable": valid}

    for src, dst in (("completed_at", "at"), ("started_at", "started_at")):
        value = payload.get(src)
        if value is not None:
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value)):
                public[dst] = value
            elif not isinstance(value, str):
                valid = False
            else:
                public[dst] = value
    for key in ("duration_ms", "time_to_first_token_ms"):
        value = payload.get(key)
        if value is not None:
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value)):
                valid = False
            else:
                public[key] = value
    message = payload.get("last_agent_message")
    if message is not None and not isinstance(message, str):
        valid = False
        message = ""
    message = message or ""
    if message.strip():
        public["message"] = message[:CODEX_TEXT_CAP]
    error = payload.get("error")
    if error is not None:
        valid = False
        public["state"] = "failed"
        public["error"] = _stringify(error)[:CODEX_TEXT_CAP]
    return {
        "lifecycle": public,
        "_lifecycle_foldable": valid,
        "_lifecycle_message_digest": content_digest(message),
        "_lifecycle_message_len": content_len(message),
    }


_SKILL_NAME_RE = re.compile(r"<name>\s*([^<\n]+?)\s*</name>", re.IGNORECASE)
_AGENTS_ENVELOPE_RE = re.compile(
    r"\A\s*# AGENTS\.md instructions for [^\n]+\n\s*"
    r"<INSTRUCTIONS>.*?</INSTRUCTIONS>\s*\Z",
    re.DOTALL,
)
_AGENTS_ENV_BUNDLE_RE = re.compile(
    r"\A\s*# AGENTS\.md instructions for [^\n]+\n\s*"
    r"<INSTRUCTIONS>.*?</INSTRUCTIONS>\s*"
    r"<environment_context>.*?</environment_context>\s*\Z",
    re.DOTALL,
)
_CONTEXT_BUNDLE_RE = re.compile(
    r"\A\s*"
    r"<recommended_plugins>.*?</recommended_plugins>\s*"
    r"# AGENTS\.md instructions for [^\n]+\n\s*"
    r"<INSTRUCTIONS>.*?</INSTRUCTIONS>\s*"
    r"<environment_context>.*?</environment_context>"
    r"\s*\Z",
    re.DOTALL,
)


def _exact_wrapper(text: str, opening: str, closing: str) -> bool:
    stripped = (text or "").strip()
    return stripped.startswith(opening) and stripped.endswith(closing)


def _meta_detail(meta_kind: str, meta_label: str, text: str) -> dict:
    detail = {"meta_kind": meta_kind, "meta_label": meta_label}
    if meta_label == "skill":
        match = _SKILL_NAME_RE.search(text or "")
        if match:
            detail["skill_name"] = match.group(1).strip()
    return detail


def _classify_injected_message(role: str | None, text: str) -> dict | None:
    """Return an explicit neutral meta descriptor for proven harness content.

    Non-human roles are authoritative provider metadata and always context.  A
    user/assistant row is reclassified only by a closed, structural wrapper
    shape; unknown XML and ordinary prose remain provider-authored content.
    """
    stripped = (text or "").strip()
    lower = stripped.lower()

    # Some Codex hosts combine three independently injected envelopes into one
    # user-role message.  Match the complete, ordered grammar so an appended
    # human prompt (or merely XML-looking prose) cannot be hidden as context.
    if role == "user" and _CONTEXT_BUNDLE_RE.fullmatch(text or ""):
        detail = _meta_detail("context", "context_bundle", text)
        detail["meta_sections"] = ["plugins", "agents", "environment"]
        return detail
    if role == "user" and _AGENTS_ENV_BUNDLE_RE.fullmatch(text or ""):
        detail = _meta_detail("context", "context_bundle", text)
        detail["meta_sections"] = ["agents", "environment"]
        return detail

    if role not in (None, "user", "assistant"):
        if _exact_wrapper(lower, "<permissions instructions>",
                          "</permissions instructions>"):
            return _meta_detail("context", "permissions", text)
        if _exact_wrapper(lower, "<multi_agent_mode>", "</multi_agent_mode>"):
            return _meta_detail("context", "mode", text)
        if _exact_wrapper(lower, "<codex_delegation>", "</codex_delegation>"):
            return _meta_detail("context", "delegation", text)
        if _exact_wrapper(lower, "<heartbeat>", "</heartbeat>"):
            return _meta_detail("notification", "heartbeat", text)
        if _exact_wrapper(lower, "<model_switch>", "</model_switch>"):
            return _meta_detail("notification", "model_switch", text)
        return _meta_detail("context", "role", text)

    wrappers = (
        ("<environment_context>", "</environment_context>", "context", "environment"),
        ("<user_instructions>", "</user_instructions>", "context", "instructions"),
        ("<permissions instructions>", "</permissions instructions>", "context", "permissions"),
        ("<multi_agent_mode>", "</multi_agent_mode>", "context", "mode"),
        ("<codex_delegation>", "</codex_delegation>", "context", "delegation"),
        ("<heartbeat>", "</heartbeat>", "notification", "heartbeat"),
        ("<model_switch>", "</model_switch>", "notification", "model_switch"),
        ("<recommended_plugins>", "</recommended_plugins>", "context", "plugins"),
        ("<skill>", "</skill>", "skill", "skill"),
        ("<oai-mem-citation>", "</oai-mem-citation>", "notification", "memory"),
        ("<memory_context>", "</memory_context>", "context", "memory"),
        ("<memory-context>", "</memory-context>", "context", "memory"),
    )
    for opening, closing, meta_kind, label in wrappers:
        if _exact_wrapper(lower, opening, closing):
            return _meta_detail(meta_kind, label, text)
    if role == "user" and _AGENTS_ENVELOPE_RE.fullmatch(text or ""):
        return _meta_detail("context", "agents", text)
    return None


def _extract(record_type: str | None, payload: dict) -> _Extracted | None:
    """Map one physical record to (kind, content, column, detail, touches), or
    None when the record produces no normalized row (§4.1). Malformed fields
    degrade to empty; the record is never aborted."""
    ptype = payload.get("type") if isinstance(payload, dict) else None
    if record_type == "response_item":
        if ptype == "message":
            role = payload.get("role")
            text = _join_content_texts(payload.get("content"))
            meta = _classify_injected_message(
                role if isinstance(role, str) else None, text)
            if meta is not None:
                return _Extracted("meta", text, "text", meta, [])
            kind = "user" if role == "user" else "assistant"
            if kind == "assistant":
                clean, markers = _segment_harness_markers(text)
                if markers:
                    return _Extracted(
                        kind, clean, "text", {"markers": markers}, [], text)
            return _Extracted(kind, text, "text", None, [])
        if ptype == "reasoning":
            summary = _join_content_texts(payload.get("summary"))
            body = _join_content_texts(payload.get("content"))
            reasoning = _reasoning_projection(
                source="response_item", summary=summary, body=body)
            if reasoning is None:
                return None
            text = "\n".join(p for p in (summary, body) if p)
            return _Extracted(
                "reasoning", text, "search_thinking", {"reasoning": reasoning}, [])
        if ptype in _RESPONSE_TOOL_CALLS:
            name = payload.get("name") or ptype
            if ptype == "function_call":
                args = _stringify(payload.get("arguments"))
            elif ptype == "web_search_call":
                args = _stringify(payload.get("action"))
            else:
                args = _stringify(payload.get("input") or payload.get("arguments"))
            text = f"{name}\n{args}" if args else str(name)
            detail = {"name": name, "args": args[:CODEX_TEXT_CAP]}
            # Frozen v1 projection at ingest (spec section 3.0) — see the
            # `for_storage` note on `decode_tool_output_card` above.
            card = decode_tool_call_card(payload, for_storage=True)
            if card is None:
                card = decode_secondary_tool_call_card(payload, for_storage=True)
            if card is not None:
                detail["card"] = card
            return _Extracted("tool_call", text, "search_tool", detail, [])
        if ptype in _RESPONSE_TOOL_OUTPUTS:
            value = payload["output"] if "output" in payload else payload.get("tools")
            # `for_storage=True`: the stored card is the FROZEN v1 projection
            # (spec section 3.0). Every S3 enrichment is computed at read time,
            # because `detail_json` bytes feed `_row_source_bytes` and therefore
            # page boundaries, and a richer stored card would paginate a newly
            # ingested conversation differently from an identical historical one.
            shaped = decode_tool_output_card(payload, for_storage=True)
            if shaped is not None:
                card, _display_body = shaped
                return _Extracted(
                    "tool_output", _stringify(value), "search_tool", {"card": card}, [])
            return _Extracted("tool_output", _stringify(value), "search_tool", None, [])
        return None  # unknown response_item subtype: version tolerance
    if record_type == "event_msg":
        if ptype in _EVENT_PROSE_KIND:
            kind = _EVENT_PROSE_KIND[ptype]
            text = _stringify(payload.get("message") or payload.get("text"))
            role = "user" if kind == "user" else "assistant"
            meta = _classify_injected_message(role, text)
            if meta is not None:
                return _Extracted("meta", text, "text", meta, [])
            column = "search_thinking" if kind == "reasoning" else "text"
            if kind == "reasoning":
                title_like = _REASONING_TITLE_RE.fullmatch(text.strip()) is not None
                reasoning = _reasoning_projection(
                    source="agent_reasoning",
                    summary=text if title_like else "",
                    body="" if title_like else text)
                if reasoning is None:
                    return None
                return _Extracted(
                    kind, text, column, {"reasoning": reasoning}, [])
            if kind == "assistant":
                clean, markers = _segment_harness_markers(text)
                if markers:
                    return _Extracted(
                        kind, clean, column, {"markers": markers}, [], text)
            return _Extracted(kind, text, column, None, [])
        if ptype in _EVENT_CARD_TYPES:
            text, touches = _event_card(ptype, payload)
            detail = {"event": ptype}
            if ptype in {"task_started", "task_complete"}:
                detail.update(_lifecycle_projection(payload))
            # Frozen v1 projection at ingest (spec section 3.0) — see the
            # `for_storage` note on `decode_tool_output_card` above.
            patch_card = decode_patch_event_card(payload, for_storage=True)
            card = patch_card or decode_secondary_event_card(payload)
            if card is not None:
                detail["card"] = card
            return _Extracted("event", text, "search_tool", detail, touches)
        return None  # token_count (accounting) + unknown event types: no row
    # session_meta / turn_context / unknown record types: no normalized row
    return None


def _event_card(ptype: str, payload: dict) -> tuple[str, list[tuple[str, str]]]:
    """Provider-truthful searchable card text + any file touches for an event."""
    touches: list[tuple[str, str]] = []
    if ptype == "task_complete":
        text = " ".join(p for p in ("task_complete", _stringify(
            payload.get("last_agent_message"))) if p)
    elif ptype == "patch_apply_end":
        paths = codex_patch_file_paths(payload)
        touches.extend((path, "apply_patch") for path in paths)
        text = " ".join(["patch_apply", *paths]) if paths else "patch_apply"
    elif ptype == "mcp_tool_call_end":
        invocation = payload.get("invocation")
        server = invocation.get("server") if isinstance(invocation, dict) else None
        tool = ((invocation.get("tool") or invocation.get("name"))
                if isinstance(invocation, dict) else None)
        identity = "/".join(
            value for value in (server, tool) if isinstance(value, str) and value)
        text = f"mcp_tool_call {identity}" if identity else "mcp_tool_call"
    elif ptype == "web_search_end":
        query = _stringify(payload.get("query"))
        text = f"web_search {query}".strip()
    else:  # task_started, context_compacted
        text = ptype
    return text, touches


def codex_patch_file_paths(payload: Any) -> list[str]:
    """Return provider-ordered file paths from one patch completion.

    Current Codex emits ``changes`` as an object keyed by path; the legacy list
    shape carries the path on each entry. Only object-valued change entries are
    touches in either shape, matching the read-time patch derivation contract.
    """
    if not isinstance(payload, dict) or payload.get("type") != "patch_apply_end":
        return []
    changes = payload.get("changes")
    if isinstance(changes, dict):
        items = changes.items()
    elif isinstance(changes, list):
        items = (
            (change.get("path"), change)
            for change in changes
            if isinstance(change, dict)
        )
    else:
        return []
    return [
        path for path, change in items
        if isinstance(path, str) and isinstance(change, dict)
    ]


def infer_codex_event_turns(
    events: Iterable[Any], *, initial_turn: str | None = None
) -> tuple[list[str | None], str | None]:
    """Infer each physical event's logical turn from native lifecycle anchors.

    ``turn_context``/``task_started`` establish a forward turn.  A resumed
    segment can instead begin with response rows and expose its first native
    proof only on a later patch/task-complete record; in that case only the
    unanchored prefix since the latest ``session_meta`` is backfilled.  Distinct
    explicit anchors are never merged.
    """
    materialized = list(events)
    turns: list[str | None] = [None] * len(materialized)
    current = initial_turn
    segment_start = 0
    for index, event in enumerate(materialized):
        record_type = getattr(event, "record_type", None)
        try:
            obj = json.loads(getattr(event, "payload_json", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            obj = {}
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        ptype = payload.get("type")
        if record_type == "session_meta":
            current = None
            segment_start = index + 1
            continue
        explicit = getattr(event, "turn_id", None)
        if explicit is None and record_type == "turn_context":
            candidate = payload.get("turn_id")
            explicit = candidate if isinstance(candidate, str) and candidate else None
        if explicit is not None:
            if current is None and _is_late_turn_anchor(
                    record_type, ptype, explicit):
                for prior in range(segment_start, index):
                    if turns[prior] is None:
                        turns[prior] = explicit
            current = explicit
            turns[index] = explicit
        else:
            turns[index] = current
    return turns, current


def _is_late_turn_anchor(
    record_type: str | None, payload_type: str | None, explicit_turn: str | None,
) -> bool:
    """Whether a native turn id can prove a preceding unanchored prefix."""
    return (
        explicit_turn is not None
        and record_type != "turn_context"
        and payload_type != "task_started"
    )


def codex_event_is_late_turn_anchor(event: Any) -> bool:
    """Public predicate shared by delta persistence and turn inference."""
    try:
        obj = json.loads(getattr(event, "payload_json", "") or "{}")
    except (json.JSONDecodeError, TypeError):
        obj = {}
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    return _is_late_turn_anchor(
        getattr(event, "record_type", None),
        payload.get("type"),
        getattr(event, "turn_id", None),
    )


def normalize_codex_events(
    events: Iterable[Any], *, initial: CodexStickyState
) -> CodexNormalizationResult:
    """Map one file window's CodexPhysicalEvent batch (offset order) to normalized
    rows + file touches, replaying sticky turn/model state seeded from ``initial``.

    Returns the terminal sticky state for persistence to codex_session_files."""
    materialized = list(events)
    inferred_turns, terminal_turn = infer_codex_event_turns(
        materialized, initial_turn=initial.turn_id)
    sticky = CodexStickyState(turn_id=initial.turn_id, model=initial.model)
    rows: list[CodexNormalizedRow] = []
    touches: list[CodexFileTouch] = []
    for event_index, event in enumerate(materialized):
        record_type = getattr(event, "record_type", None)
        if record_type == "session_meta":
            sticky.turn_id = None
            sticky.model = None
            continue
        try:
            obj = json.loads(getattr(event, "payload_json", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            obj = {}
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if record_type == "turn_context":
            own = getattr(event, "turn_id", None)
            candidate_turn = own if own is not None else payload.get("turn_id")
            if isinstance(candidate_turn, str) and candidate_turn:
                sticky.turn_id = candidate_turn
            model = payload.get("model")
            sticky.model = model if isinstance(model, str) and model else None
            continue
        # Identity-less events (before any session_meta / no resume seed) are
        # unaddressable and stay physical-only (§4.1) — never normalized.
        conversation_key = getattr(event, "conversation_key", None)
        if conversation_key is None:
            continue
        extracted = _extract(record_type, payload)
        if extracted is None:
            continue
        eff_turn = inferred_turns[event_index]
        text_full = extracted.content_text or ""
        identity_full = (extracted.identity_text
                         if extracted.identity_text is not None else text_full)
        capped, truncated = _cap(text_full)
        text_col = ""
        search_tool = ""
        search_thinking = ""
        if extracted.column == "text":
            text_col = capped
        elif extracted.column == "search_thinking":
            search_thinking = capped
        else:
            search_tool = capped
        detail = dict(extracted.detail) if extracted.detail else None
        if truncated:
            detail = detail or {}
            detail["truncated"] = True
        source_path = getattr(event, "source_path", "")
        line_offset = getattr(event, "line_offset", 0)
        effective_call_id = getattr(event, "call_id", None)
        if not effective_call_id and record_type == "response_item":
            candidate = payload.get("id")
            if isinstance(candidate, str) and candidate:
                effective_call_id = candidate
        rows.append(CodexNormalizedRow(
            conversation_key=conversation_key,
            source_root_key=getattr(event, "source_root_key", None) or "",
            source_path=source_path,
            line_offset=line_offset,
            timestamp_utc=getattr(event, "timestamp_utc", None),
            turn_id=eff_turn,
            call_id=effective_call_id,
            kind=extracted.kind,
            event_type=getattr(event, "event_type", None),
            record_family="event_msg" if record_type == "event_msg" else "response_item",
            model=sticky.model,
            text=text_col,
            content_digest=content_digest(identity_full),
            content_len=content_len(identity_full),
            detail_json=_canonical_json(detail) if detail else None,
            search_tool=search_tool,
            search_thinking=search_thinking,
        ))
        for file_path, tool in extracted.touches:
            touches.append(CodexFileTouch(
                conversation_key=conversation_key,
                source_path=source_path,
                file_path=file_path,
                tool=tool,
                line_offset=line_offset,
            ))
    return CodexNormalizationResult(
        rows=rows, touches=touches,
        terminal=CodexStickyState(turn_id=terminal_turn, model=sticky.model),
    )


# ── mirror pairing + canonical items (§5.3 / §5.2) ────────────────────────────


_REASONING_WS_RE = re.compile(r"\s+")


def _reasoning_projected_text(row: CodexNormalizedRow) -> str:
    """Normalized projected text of one reasoning row, or ``""``.

    Read from the STORED projection in ``detail_json``, never from
    ``search_thinking``: that column is capped at 16,000 characters and a
    response item stores ``summary + "\\n" + body`` in it, so it cannot
    distinguish a title from a title-plus-body.

    Normalization drops the bold wrapper and collapses whitespace, because the
    two extraction paths disagree about both. A ``response_item`` aggregate
    keeps its parts' ``**`` wrappers inside one ``summary`` string, while an
    ``event_msg`` part whose text matches the title pattern has already had its
    wrapper stripped into ``title``.
    """
    if not row.detail_json:
        return ""
    try:
        detail = json.loads(row.detail_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    reasoning = detail.get("reasoning") if isinstance(detail, dict) else None
    if not isinstance(reasoning, dict):
        return ""
    head = reasoning.get("title") or reasoning.get("summary") or ""
    body = reasoning.get("body") or ""
    joined = "\n".join(part for part in (head, body) if part)
    return _REASONING_WS_RE.sub(" ", joined.replace("**", "")).strip()


def _pair_mirrors_impl(
    rows: list[CodexNormalizedRow],
) -> tuple[set[int], dict[int, int]]:
    """Core mirror-pairing pass (§5.3): returns ``(suppressed_indexes,
    partner_map)`` where ``partner_map[suppressed_idx] = canonical_idx`` — the
    single source of truth for both ``pair_mirrors`` and ``pair_mirror_partners``.
    """
    suppressed: set[int] = set()
    partners: dict[int, int] = {}

    # Turned pairing: group prose rows by (turn_id, kind, digest, len). Multiset
    # one-to-one in physical order — the k-th event member pairs the k-th response
    # member (three identical event copies never collapse into one).
    turned: dict[tuple, dict[str, list[int]]] = {}
    for i, row in enumerate(rows):
        if row.kind not in _MIRROR_KINDS or row.turn_id is None:
            continue
        key = (row.turn_id, row.kind, row.content_digest, row.content_len)
        group = turned.setdefault(key, {"R": [], "E": []})
        group["E" if row.record_family == "event_msg" else "R"].append(i)
    for group in turned.values():
        pair_count = min(len(group["R"]), len(group["E"]))
        for k in range(pair_count):
            event_idx = group["E"][k]
            suppressed.add(event_idx)
            partners[event_idx] = group["R"][k]

    # Unturned pairing: adjacency over the nearest preceding same-kind prose row.
    last_prose_by_kind: dict[str, int] = {}
    paired_response: set[int] = set()
    for j, row in enumerate(rows):
        if row.kind not in _MIRROR_KINDS or row.turn_id is not None:
            continue
        prev_idx = last_prose_by_kind.get(row.kind)
        if row.record_family == "event_msg" and prev_idx is not None:
            prev = rows[prev_idx]
            if (prev.record_family != "event_msg"
                    and prev_idx not in paired_response
                    and prev.content_digest == row.content_digest
                    and prev.content_len == row.content_len
                    and prev.source_path == row.source_path):
                suppressed.add(j)
                partners[j] = prev_idx
                paired_response.add(prev_idx)
        last_prose_by_kind[row.kind] = j

    # Reasoning containment (#463 S1 / F5). Exact-digest pairing structurally
    # cannot collapse the general reasoning case: a response_item reasoning
    # row's identity is `"\n".join([summary, body])` while an event_msg
    # agent_reasoning row's is the raw single string, and the two coincide only
    # when the body is empty and the raw text equals the summary exactly. The
    # real relation is containment — one aggregate carries the parts that each
    # arrived earlier as their own event.
    #
    # Measured against the production store on 2026-08-02: of 17,149 turned
    # event_msg reasoning rows, 4,883 already pair by exact digest and ALL
    # 12,266 that survive are contained in exactly one same-turn aggregate, with
    # no orphans and nothing present-but-not-contained.
    #
    # This runs BESIDE the exact-digest passes rather than replacing them, so no
    # other mirror kind's behaviour changes. Three properties make it safe, and
    # each answers a concrete way a simpler rule would delete real content:
    #
    #   * Per-aggregate, never concatenated. Concatenating a turn's aggregates
    #     admits a match spanning the boundary between two unrelated ones, which
    #     would suppress a part no aggregate actually contains.
    #   * Multiset, not set. An aggregate covers each occurrence of a part once.
    #     Two identical events must not both fold onto an aggregate that carries
    #     that text once — and an aggregate already claimed by the exact-digest
    #     pass above has that occurrence spent, which is why `used` is seeded
    #     from `partners`.
    #   * An explicit partner. `pair_mirror_partners` is the contract search and
    #     find rely on to map a suppressed row to its canonical survivor, so a
    #     containment match names WHICH aggregate owns it — something a
    #     concatenated match could not do.
    #
    # A turn with no aggregate loses nothing, and a part genuinely contained in
    # none is retained.
    reasoning_turns: dict[str, dict[str, list[int]]] = {}
    for i, row in enumerate(rows):
        if row.kind != "reasoning" or row.turn_id is None:
            continue
        group = reasoning_turns.setdefault(row.turn_id, {"R": [], "E": []})
        group["E" if row.record_family == "event_msg" else "R"].append(i)
    for group in reasoning_turns.values():
        if not group["R"]:
            continue
        aggregates = [(index, _reasoning_projected_text(rows[index]), {})
                      for index in group["R"]]
        # An aggregate the exact-digest pass already paired has spent one
        # occurrence of its own text; seed that so the two passes share one
        # budget instead of each spending it independently.
        spent = {index for index in partners.values()}
        for index, text, used in aggregates:
            if index in spent and text:
                used[text] = used.get(text, 0) + 1
        for event_index in group["E"]:
            if event_index in suppressed:
                continue
            part = _reasoning_projected_text(rows[event_index])
            if not part:
                continue
            # Physical order decides, so the earliest aggregate that still has
            # capacity for this part owns it. An aggregate whose whole text IS
            # the part is preferred over one that merely contains it, because it
            # is the more accurate survivor for a search hit to land on.
            owner = None
            for index, text, used in aggregates:
                if text.count(part) <= used.get(part, 0):
                    continue
                if owner is None:
                    owner = (index, text, used)
                if text == part:
                    owner = (index, text, used)
                    break
            if owner is None:
                continue
            index, _text, used = owner
            used[part] = used.get(part, 0) + 1
            suppressed.add(event_index)
            partners[event_index] = index

    return suppressed, partners


def pair_mirrors(rows: list[CodexNormalizedRow]) -> tuple[list[CodexNormalizedRow], set[int]]:
    """Suppress the event_msg member of each digest-exact mirror pair (§5.3).

    Returns ``(kept_rows_in_input_order, suppressed_input_indexes)``. Pairing is
    digest-exact (never on capped text) and correlation-gated:
      * turned rows (turn_id set) pair one-to-one as multisets within the same
        effective turn (three identical event copies never collapse into one);
      * unturned rows require the same source file AND adjacency — no intervening
        same-kind prose row between the two members.
    The response_item member is canonical; the event_msg member is suppressed.
    """
    suppressed, _partners = _pair_mirrors_impl(rows)
    kept = [row for i, row in enumerate(rows) if i not in suppressed]
    return kept, suppressed


def pair_mirror_partners(rows: list[CodexNormalizedRow]) -> dict[int, int]:
    """Map each suppressed mirror-member index to its canonical (kept) partner
    index (§5.3). Used by search's item-level collapse so a suppressed event_msg
    row folds into the same ``item_key`` as its response_item partner (§6.2),
    keeping mirror rows from double-counting."""
    _suppressed, partners = _pair_mirrors_impl(rows)
    return partners


def _item_pos(row: CodexNormalizedRow) -> tuple:
    return (row.timestamp_utc or "", row.source_path, row.line_offset)


def _row_card(row: CodexNormalizedRow) -> dict | None:
    if not row.detail_json:
        return None
    try:
        detail = json.loads(row.detail_json)
    except (json.JSONDecodeError, TypeError):
        return None
    card = detail.get("card") if isinstance(detail, dict) else None
    return card if isinstance(card, dict) else None


def _fold_patch_completion_items(items: list[dict]) -> list[dict]:
    """Fold only proven ``patch_apply_end`` items into their owning response.

    Same-id ownership wins when unique. Current nested ``tools.apply_patch``
    events use an inner id, so the second proof is a unique physical bracket in
    one source file: call < event < that call's uniquely-owned output. Ambiguous
    or repeated shapes stay separate and inspectable.
    """
    patch_calls: list[
        tuple[int, CodexNormalizedRow, CodexNormalizedRow | None, int]
    ] = []
    for item_index, item in enumerate(items):
        if item["klass"] != "response":
            continue
        owner_count: dict[str, int] = {}
        for row in item["rows"]:
            if row.kind == "tool_call" and row.call_id:
                owner_count[row.call_id] = owner_count.get(row.call_id, 0) + 1
        for row in item["rows"]:
            card = _row_card(row)
            if not (row.kind == "tool_call" and isinstance(card, dict)
                    and card.get("type") == "patch"):
                continue
            outputs = [
                candidate for candidate in item["rows"]
                if candidate.kind == "tool_output" and row.call_id
                and candidate.call_id == row.call_id
                and owner_count.get(row.call_id) == 1
                and _item_pos(row) < _item_pos(candidate)
            ]
            patch_calls.append((
                item_index, row, outputs[0] if len(outputs) == 1 else None,
                owner_count.get(row.call_id, 0),
            ))

    matched_calls: set[tuple[str, int]] = set()
    remove_items: set[int] = set()
    for event_index, event_item in enumerate(items):
        if event_item["klass"] != "event" or len(event_item["rows"]) != 1:
            continue
        event_row = event_item["rows"][0]
        event_card = _row_card(event_row)
        if not (isinstance(event_card, dict)
                and event_card.get("source") == "patch_apply_end"):
            continue
        same_id = [
            candidate for candidate in patch_calls
            if event_row.call_id and candidate[1].call_id == event_row.call_id
            and candidate[3] == 1
            and candidate[1].turn_id == event_row.turn_id
            and (candidate[1].source_path, candidate[1].line_offset) not in matched_calls
        ]
        candidates = same_id if len(same_id) == 1 else []
        if not candidates:
            candidates = [
                candidate for candidate in patch_calls
                if candidate[2] is not None
                and candidate[1].turn_id == event_row.turn_id
                and candidate[1].source_path == event_row.source_path
                and candidate[2].source_path == event_row.source_path
                and candidate[1].line_offset < event_row.line_offset < candidate[2].line_offset
                and (candidate[1].source_path, candidate[1].line_offset) not in matched_calls
            ]
        if len(candidates) != 1:
            continue
        owner_index, call_row, _output_row, _owner_count = candidates[0]
        owner = items[owner_index]
        owner["rows"].append(event_row)
        owner["rows"].sort(key=_item_pos)
        owner.setdefault("folded_items", []).append(event_item)
        matched_calls.add((call_row.source_path, call_row.line_offset))
        remove_items.add(event_index)
    return [item for index, item in enumerate(items) if index not in remove_items]


def _fold_secondary_completion_items(items: list[dict]) -> list[dict]:
    """Fold web/MCP end events only onto one exact same-turn call id.

    No name, adjacency, or basename inference is allowed: a reused, empty, or
    unmatched id leaves the native completion visible as its own event card.
    """
    calls_by_id: dict[str, list[tuple[int, CodexNormalizedRow]]] = {}
    for item_index, item in enumerate(items):
        if item["klass"] != "response":
            continue
        for row in item["rows"]:
            if row.kind == "tool_call" and row.call_id:
                calls_by_id.setdefault(row.call_id, []).append((item_index, row))
    remove_items: set[int] = set()
    matched_calls: set[tuple[str, int]] = set()
    for event_index, event_item in enumerate(items):
        if event_item["klass"] != "event" or len(event_item["rows"]) != 1:
            continue
        event_row = event_item["rows"][0]
        event_card = _row_card(event_row)
        if not (event_row.call_id and isinstance(event_card, dict)
                and event_card.get("type") in {
                    "web_search_completion", "mcp_completion"}):
            continue
        candidates = [
            (owner_index, call_row)
            for owner_index, call_row in calls_by_id.get(event_row.call_id, [])
            if call_row.turn_id == event_row.turn_id
            and (call_row.source_path, call_row.line_offset) not in matched_calls
        ]
        if event_card.get("type") == "web_search_completion":
            candidates = [
                candidate for candidate in candidates
                if (_parse_row_detail(candidate[1]) or {}).get("name")
                == "web_search_call"
            ]
        if len(candidates) != 1:
            continue
        owner_index, call_row = candidates[0]
        owner = items[owner_index]
        owner["rows"].append(event_row)
        owner["rows"].sort(key=_item_pos)
        owner.setdefault("folded_items", []).append(event_item)
        matched_calls.add((call_row.source_path, call_row.line_offset))
        remove_items.add(event_index)
    return [item for index, item in enumerate(items) if index not in remove_items]


def _parse_row_detail(row: CodexNormalizedRow) -> dict | None:
    if not row.detail_json:
        return None
    try:
        detail = json.loads(row.detail_json)
    except (json.JSONDecodeError, TypeError):
        return None
    return detail if isinstance(detail, dict) else None


def _fold_lifecycle_items(items: list[dict]) -> list[dict]:
    """Fold only closed, uniquely-owned redundant task lifecycle events.

    The physical rows remain retained and become member-key aliases. A completion
    is redundant only when its last message is empty or digest/length-identical to
    a real assistant row in the one owning logical response. Unknown fields,
    errors, unmatched/ambiguous events, and unique messages stay standalone.
    """
    responses: dict[str, list[tuple[int, dict]]] = {}
    events: dict[tuple[str, str], list[tuple[int, dict, CodexNormalizedRow, dict]]] = {}
    for index, item in enumerate(items):
        turn_id = item.get("turn_id")
        if item["klass"] == "response" and turn_id:
            responses.setdefault(turn_id, []).append((index, item))
        if item["klass"] != "event" or len(item["rows"]) != 1 or not turn_id:
            continue
        row = item["rows"][0]
        detail = _parse_row_detail(row)
        lifecycle = detail.get("lifecycle") if isinstance(detail, dict) else None
        if (isinstance(lifecycle, dict)
                and lifecycle.get("event") in {"task_started", "task_complete"}):
            events.setdefault((turn_id, lifecycle["event"]), []).append(
                (index, item, row, detail))

    remove: set[int] = set()
    for turn_id, owners in responses.items():
        if len(owners) != 1:
            continue
        _owner_index, owner = owners[0]
        for event_name in ("task_started", "task_complete"):
            candidates = events.get((turn_id, event_name), [])
            if len(candidates) != 1:
                continue
            event_index, event_item, row, detail = candidates[0]
            if detail.get("_lifecycle_foldable") is not True:
                continue
            if event_name == "task_complete":
                message_len = detail.get("_lifecycle_message_len")
                message_digest = detail.get("_lifecycle_message_digest")
                if message_len:
                    if not any(
                        candidate.kind == "assistant"
                        and candidate.content_len == message_len
                        and candidate.content_digest == message_digest
                        for candidate in owner["rows"]
                    ):
                        continue
            lifecycle = detail["lifecycle"]
            section = {
                key: value for key, value in lifecycle.items()
                if key not in {"schema_version", "event", "state", "message", "error"}
            }
            folded = owner.setdefault(
                "lifecycle", {"schema_version": 1, "state": "started"})
            folded["started" if event_name == "task_started" else "completed"] = section
            if event_name == "task_complete":
                folded["state"] = "completed"
            owner["rows"].append(row)
            owner["rows"].sort(key=_item_pos)
            owner.setdefault("lifecycle_rows", []).append(row)
            owner["lifecycle_rows"].sort(key=_item_pos)
            owner.setdefault("folded_items", []).append(event_item)
            remove.add(event_index)
    return [item for index, item in enumerate(items) if index not in remove]


def canonical_items(
    rows: list[CodexNormalizedRow], *, fold_patch_completions: bool = True,
) -> list[dict]:
    """Group KEPT (post-pairing) rows into canonical rendered items (§5.2).

    Response items bundle a turn's assistant/reasoning/tool rows; prompt/event/
    unturned items are one-per-row. Ordered by (timestamp_utc, source_path,
    line_offset). Each item dict: {"klass", "rows", "turn_id", "anchor_row"}.
    """
    ordered = sorted(rows, key=_item_pos)
    items: list[dict] = []
    response_by_turn: dict[str, dict] = {}
    for row in ordered:
        if row.turn_id is None:
            items.append({"klass": "unturned", "rows": [row], "turn_id": None,
                          "anchor_row": row})
        elif row.kind == "meta":
            items.append({"klass": "meta", "rows": [row], "turn_id": row.turn_id,
                          "anchor_row": row})
        elif row.kind == "user":
            items.append({"klass": "prompt", "rows": [row], "turn_id": row.turn_id,
                          "anchor_row": row})
        elif row.kind == "event":
            items.append({"klass": "event", "rows": [row], "turn_id": row.turn_id,
                          "anchor_row": row})
        else:
            item = response_by_turn.get(row.turn_id)
            if item is None:
                item = {"klass": "response", "rows": [], "turn_id": row.turn_id,
                        "anchor_row": row}
                response_by_turn[row.turn_id] = item
                items.append(item)
            item["rows"].append(row)
    if fold_patch_completions:
        items = _fold_patch_completion_items(items)
        items = _fold_secondary_completion_items(items)
    items = _fold_lifecycle_items(items)
    items.sort(key=lambda it: _item_pos(it["anchor_row"]))
    return items


def rollup_item_count(rows: list[CodexNormalizedRow]) -> int:
    """Count rendered LOGICAL items (mirror-paired), not physical rows (§3.2)."""
    kept, _ = pair_mirrors(rows)
    return len(canonical_items(kept))


# ── title derivation (§4.3) ───────────────────────────────────────────────────


def derive_title(rows: list[CodexNormalizedRow]) -> str | None:
    """First meaningful user prompt over the first 12 LOGICAL prompts (after
    mirror pairing), skipping structural wrapper noise; display-normalized +
    capped; ``None`` when no meaningful prompt exists in the window (§4.3)."""
    kept, _ = pair_mirrors(rows)
    items = canonical_items(kept)
    prompts = [
        it for it in items
        if it["klass"] == "prompt"
        or (it["klass"] == "unturned" and it["anchor_row"].kind == "user")
    ]
    for item in prompts[:12]:
        raw = item["anchor_row"].text
        first_line = _first_nonblank_line(_strip_ansi(raw or "")) if raw else ""
        if any(first_line.startswith(prefix) for prefix in CODEX_TITLE_SKIP_PREFIXES):
            continue
        candidate = _display_title(raw)
        if candidate:
            return candidate
    return None
