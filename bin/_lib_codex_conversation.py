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
from typing import Any, Iterable

# Reuse the Claude display helpers by IMPORT (never move them). _strip_ansi lives
# in _lib_conversation; the title cap + first-non-blank-line helper in
# _lib_conversation_query. These modules are stdlib-only pure kernels.
from _lib_conversation import _strip_ansi
from _lib_conversation_query import _TITLE_MAX, _first_nonblank_line


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

# Structural wrapper prefixes skipped during title selection (§4.3), pinned from
# the corpus (title-wrapper-window). Prefix-structural, never content heuristics.
CODEX_TITLE_SKIP_PREFIXES: tuple[str, ...] = (
    "<environment_context>",
    "<user_instructions>",
)

_PROSE_KINDS = frozenset({"user", "assistant", "reasoning"})


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


@dataclasses.dataclass
class _Extracted:
    kind: str
    content_text: str          # pre-cap, feeds digest + content_len
    column: str                # "text" | "search_tool" | "search_thinking"
    detail: dict | None
    touches: list[tuple[str, str]]  # (file_path, tool)


def _extract(record_type: str | None, payload: dict) -> _Extracted | None:
    """Map one physical record to (kind, content, column, detail, touches), or
    None when the record produces no normalized row (§4.1). Malformed fields
    degrade to empty; the record is never aborted."""
    ptype = payload.get("type") if isinstance(payload, dict) else None
    if record_type == "response_item":
        if ptype == "message":
            role = payload.get("role")
            kind = "user" if role == "user" else "assistant"
            text = _join_content_texts(payload.get("content"))
            return _Extracted(kind, text, "text", None, [])
        if ptype == "reasoning":
            summary = _join_content_texts(payload.get("summary"))
            body = _join_content_texts(payload.get("content"))
            text = "\n".join(p for p in (summary, body) if p)
            return _Extracted("reasoning", text, "search_thinking", None, [])
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
            return _Extracted("tool_call", text, "search_tool", detail, [])
        if ptype in _RESPONSE_TOOL_OUTPUTS:
            body = _stringify(payload.get("output") or payload.get("tools"))
            return _Extracted("tool_output", body, "search_tool", None, [])
        return None  # unknown response_item subtype: version tolerance
    if record_type == "event_msg":
        if ptype in _EVENT_PROSE_KIND:
            kind = _EVENT_PROSE_KIND[ptype]
            text = _stringify(payload.get("message") or payload.get("text"))
            column = "search_thinking" if kind == "reasoning" else "text"
            return _Extracted(kind, text, column, None, [])
        if ptype in _EVENT_CARD_TYPES:
            text, touches = _event_card(ptype, payload)
            detail = {"event": ptype}
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
        paths = []
        changes = payload.get("changes")
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, dict) and isinstance(change.get("path"), str):
                    paths.append(change["path"])
                    touches.append((change["path"], "apply_patch"))
        text = " ".join(["patch_apply", *paths]) if paths else "patch_apply"
    elif ptype == "mcp_tool_call_end":
        invocation = payload.get("invocation")
        name = invocation.get("name") if isinstance(invocation, dict) else None
        text = f"mcp_tool_call {name}" if name else "mcp_tool_call"
    elif ptype == "web_search_end":
        query = _stringify(payload.get("query"))
        text = f"web_search {query}".strip()
    else:  # task_started, context_compacted
        text = ptype
    return text, touches


def normalize_codex_events(
    events: Iterable[Any], *, initial: CodexStickyState
) -> CodexNormalizationResult:
    """Map one file window's CodexPhysicalEvent batch (offset order) to normalized
    rows + file touches, replaying sticky turn/model state seeded from ``initial``.

    Returns the terminal sticky state for persistence to codex_session_files."""
    sticky = CodexStickyState(turn_id=initial.turn_id, model=initial.model)
    rows: list[CodexNormalizedRow] = []
    touches: list[CodexFileTouch] = []
    for event in events:
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
            sticky.turn_id = own if own is not None else payload.get("turn_id")
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
        own_turn = getattr(event, "turn_id", None)
        eff_turn = own_turn if own_turn is not None else sticky.turn_id
        text_full = extracted.content_text or ""
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
        rows.append(CodexNormalizedRow(
            conversation_key=conversation_key,
            source_root_key=getattr(event, "source_root_key", None) or "",
            source_path=source_path,
            line_offset=line_offset,
            timestamp_utc=getattr(event, "timestamp_utc", None),
            turn_id=eff_turn,
            call_id=getattr(event, "call_id", None),
            kind=extracted.kind,
            event_type=getattr(event, "event_type", None),
            record_family="event_msg" if record_type == "event_msg" else "response_item",
            model=sticky.model,
            text=text_col,
            content_digest=content_digest(text_full),
            content_len=content_len(text_full),
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
        terminal=CodexStickyState(turn_id=sticky.turn_id, model=sticky.model),
    )


# ── mirror pairing + canonical items (§5.3 / §5.2) ────────────────────────────


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
        if row.kind not in _PROSE_KINDS or row.turn_id is None:
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
        if row.kind not in _PROSE_KINDS or row.turn_id is not None:
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


def canonical_items(rows: list[CodexNormalizedRow]) -> list[dict]:
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
