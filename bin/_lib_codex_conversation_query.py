"""#294 S6 — Codex conversation query kernels (detail, outline, browse, search).

Builds on the pure normalization kernel ``_lib_codex_conversation`` (mirror
pairing, canonical item grouping, title derivation) to assemble the read-side
neutral envelopes S7 wires to routes. Every public function returns a complete
``status``-tagged envelope per the §5.6 per-kernel status matrix.

Reads only (no ingest, no config reads). ``effective_speed`` is an explicit
kernel parameter — pricing edits and the fast-tier multiplier are resolved by
the caller at its I/O boundary, never here (§5.4).

Public names imported verbatim by the S7 dispatch layer — do not rename:
``codex_normalization_authoritative``, ``codex_item_key``,
``get_codex_conversation``, ``get_codex_conversation_outline``,
``list_codex_conversations``, ``search_codex_conversations``,
``CODEX_SEARCH_KINDS``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3

import _lib_codex_conversation as kern
import _lib_codex_segments as segkern
from _lib_codex_reasoning_headings import decompose_reasoning_headings
from _lib_conversation import _strip_ansi
from _lib_conversation_query import _FULL_PAYLOAD_CEILING, _first_nonblank_line
from _lib_pricing import _calculate_codex_entry_cost

# ── constants ────────────────────────────────────────────────────────────────

# Migration whose applied marker makes the normalized corpus authoritative
# (§3.5). Fresh caches stamp the full registry at creation, so they are always
# authoritative; a held-lock deferral leaves it pending.
CODEX_NORMALIZATION_MIGRATION = "025_codex_conversation_normalization"

# The provider-local rebuild marker migration 028's byte-zero replay arms. Its
# sibling — the thread_source-inference replay marker — is
# ``kern.CODEX_CONVERSATION_REPLAY_FROM_ZERO_KEY``; both are named, never
# inlined as SQL literals, so a rename cannot leave this probe testing a key
# nothing writes any more.
CODEX_CONTRACT_REBUILD_MARKER = "conversation_rebuild_codex_pending"

# Domain separations for the opaque item-key encoding (§5.2). The source-path
# fingerprint is a domain-separated hash, NEVER a raw path (privacy-safe).
CODEX_ITEM_KEY_DOMAIN = b"cctally-codex-item-key-v1\0"
CODEX_ITEM_PATH_DOMAIN = b"cctally-codex-item-path-v1\0"
# S7 §3.4: opaque payload-block anchor over a tool_call row's row-class identity.
# Same domain-separated hash family as codex_item_key's row class, distinct domain.
CODEX_BLOCK_KEY_DOMAIN = b"cctally-codex-block-key-v1\0"

CODEX_SEARCH_KINDS = ("all", "prompts", "assistant", "tools", "thinking", "title", "files")

# In-conversation find taxonomy (S7 §3.1) — byte-equal to the Claude _FIND_KINDS
# tuple (no title/files: those are cross-conversation search axes only).
CODEX_FIND_KINDS = ("all", "prompts", "assistant", "tools", "thinking")

# Normalized-message column order — matches CodexNormalizedRow field order so a
# SELECT row splats straight into the dataclass.
_ROW_COLS = (
    "conversation_key, source_root_key, source_path, line_offset, timestamp_utc, "
    "turn_id, call_id, kind, event_type, record_family, model, text, "
    "content_digest, content_len, detail_json, search_tool, search_thinking"
)

_SEARCH_BADGE = {
    "user": "prompt",
    "assistant": "assistant",
    "reasoning": "thinking",
    "tool_call": "tools",
    "tool_output": "tools",
    "event": "event",
    "meta": "context",
}

_META_LABEL_TEXT = {
    "agents": "Project instructions",
    "context_bundle": "Session context",
    "delegation": "Delegation context",
    "environment": "Environment context",
    "heartbeat": "Harness heartbeat",
    "instructions": "User instructions",
    "memory": "Memory context",
    "mode": "Agent mode",
    "model_switch": "Model switch",
    "permissions": "Permissions",
    "plugins": "Available plugins",
    "role": "Harness role",
    "skill": "Skill context",
}


# ── authority probe (§3.5) ────────────────────────────────────────────────────


def codex_normalization_authoritative(conn: sqlite3.Connection) -> bool:
    """True iff the normalized Codex corpus is authoritative (§3.5).

    Split stores use their provider-local rebuild marker: current schema alone
    is not authority while migration 028's byte-zero replay is pending. Legacy
    monolithic/bare connections retain the migration-025 stamp contract.

    EITHER pending marker withholds authority. The thread_source-inference
    replay (conversations migration 002) is armed by its own key precisely
    because the contract replay must not consume it, so a probe that tested only
    the contract marker would report a not-yet-repaired store as authoritative
    to every ``--no-sync`` read.
    """
    try:
        split = conn.execute(
            "SELECT 1 FROM cache_meta "
            "WHERE key='conversation_schema_version'"
        ).fetchone() is not None
        if split:
            pending = conn.execute(
                "SELECT 1 FROM cache_meta WHERE key IN (?,?) LIMIT 1",
                (CODEX_CONTRACT_REBUILD_MARKER,
                 kern.CODEX_CONVERSATION_REPLAY_FROM_ZERO_KEY),
            ).fetchone() is not None
            version = conn.execute(
                "SELECT value FROM cache_meta "
                "WHERE key='codex_conversation_contract_version'"
            ).fetchone()
            return (
                not pending
                and version is not None
                and version[0] == kern.CODEX_CONVERSATION_CONTRACT_VERSION
            )
    except sqlite3.OperationalError:
        pass
    try:
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (CODEX_NORMALIZATION_MIGRATION,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


# ── item key (§5.2) ───────────────────────────────────────────────────────────


def _source_path_fingerprint(source_path: str | None) -> str:
    """Domain-separated hash of a source path — never the raw path (§5.2)."""
    return hashlib.sha256(
        CODEX_ITEM_PATH_DOMAIN + (source_path or "").encode("utf-8")
    ).hexdigest()[:16]


def codex_item_key(
    conversation_key: str,
    *,
    klass: str,
    turn_id: str | None,
    source_path: str | None,
    line_offset: int | None,
    content_digest: str | None,
) -> str:
    """Opaque, qualified, ordinal-free item anchor (§5.2).

    Response items key on durable native-turn identity ``(conversation_key,
    "turn", turn_id)`` — same-turn content replacement keeps the key. Prompt /
    event / unturned items key on the canonical member's stable physical
    identity + content: ``(conversation_key, "row", fingerprint(source_path),
    line_offset, content_digest)`` — no population-relative ordinals, so
    deleting an earlier duplicate or an out-of-order multi-file append never
    moves an existing key, and a same-offset content replacement changes it.

    Segments 1..N of a split turn use a third shape, ``(conversation_key, "seg",
    fingerprint(source_path), line_offset, content_digest)``, computed from the
    segment's anchor row (#463 S1). ``turn_id`` is deliberately NOT an input:
    hashing the row alone is ordinal-free, so the key never changes while that
    row's content stays at the same offset. Segment 0 does NOT take this shape —
    it inherits its turn's ``"response"`` key unchanged, which is why every deep
    link, permalink, bookmark, reading position and outline entry issued before
    segmentation still resolves to the head of its turn by construction, with no
    alias table and no migration.

    The ``"seg"`` domain separator is what keeps a segment key from colliding
    with the ``"row"`` key of the same anchor row.
    """
    if klass == "response":
        parts = ("turn", conversation_key or "", turn_id or "")
    elif klass == "segment":
        parts = (
            "seg",
            conversation_key or "",
            _source_path_fingerprint(source_path),
            "" if line_offset is None else str(line_offset),
            content_digest or "",
        )
    else:
        parts = (
            "row",
            conversation_key or "",
            _source_path_fingerprint(source_path),
            "" if line_offset is None else str(line_offset),
            content_digest or "",
        )
    raw = "\x00".join(parts).encode("utf-8")
    return "civ1_" + hashlib.sha256(CODEX_ITEM_KEY_DOMAIN + raw).hexdigest()[:40]


def _item_key_for_item(conversation_key: str, item: dict) -> str:
    if item["klass"] == "response":
        return codex_item_key(
            conversation_key, klass="response", turn_id=item["turn_id"],
            source_path=None, line_offset=None, content_digest=None)
    anchor = item["anchor_row"]
    return codex_item_key(
        conversation_key, klass=item["klass"], turn_id=item["turn_id"],
        source_path=anchor.source_path, line_offset=anchor.line_offset,
        content_digest=anchor.content_digest)


def _member_item_keys(conversation_key: str, item: dict) -> list[str]:
    """Durable aliases for logical items folded by a later contract version."""
    return [
        _item_key_for_item(conversation_key, folded)
        for folded in item.get("folded_items", [])
    ]


def codex_block_key(
    conversation_key: str,
    *,
    source_path: str | None,
    line_offset: int | None,
    content_digest: str | None,
) -> str:
    """Opaque, ordinal-free anchor over a physical row's row-class identity (§3.4).

    Carried by EVERY row-backed block since #463 S2. Stable identity and
    payload-capability are DIFFERENT properties: a prose block has a key and no
    retained payload, so a consumer must never infer payload availability from
    the presence of a key. Payload readback is unchanged and gains no surface —
    ``_locate_payload_block`` still resolves only tool_call rows and the marker
    and lifecycle event families, and refuses everything else.

    Same domain-separated hash family as ``codex_item_key``'s row class —
    ``(conversation_key, fingerprint(source_path), line_offset,
    content_digest)`` — with a DISTINCT domain, so a block key never collides
    with an item key. Stable per block, unique per physical row: a same-offset
    content replacement changes it (content_digest moves), an out-of-order
    append elsewhere leaves it (no population-relative ordinals). Segment
    boundaries never affect it, because nothing in the inputs is
    population-relative.
    """
    parts = (
        conversation_key or "",
        _source_path_fingerprint(source_path),
        "" if line_offset is None else str(line_offset),
        content_digest or "",
    )
    raw = "\x00".join(parts).encode("utf-8")
    return "cbk1_" + hashlib.sha256(CODEX_BLOCK_KEY_DOMAIN + raw).hexdigest()[:40]


def _block_key_for_row(row) -> str:
    return codex_block_key(
        row.conversation_key, source_path=row.source_path,
        line_offset=row.line_offset, content_digest=row.content_digest)


# ── row loading + display helpers ─────────────────────────────────────────────


def _load_conversation_rows(conn: sqlite3.Connection, conversation_key: str) -> list:
    """A conversation's normalized rows (all files) in physical order — the same
    ``(timestamp_utc, source_path, line_offset)`` order the ingest/rollup writer
    feeds the kernel, so pairing/grouping converge with the stored rollup."""
    return [
        kern.CodexNormalizedRow(*row)
        for row in conn.execute(
            "SELECT " + _ROW_COLS + " FROM codex_conversation_messages "
            "WHERE conversation_key = ? "
            "ORDER BY timestamp_utc, source_path, line_offset",
            (conversation_key,),
        )
    ]


# Narrow index columns (#463 S1, spec section 3, Phase A). Everything except
# ``text`` — and the event payloads are not touched at all. ``detail_json`` is
# REQUIRED, not optional: all three fold passes inside ``canonical_items`` parse
# it, and the reasoning title boundary comes from the stored projection, which
# ``search_thinking`` cannot supply (it is capped at 16,000 characters and stores
# ``summary + "\n" + body`` for a response item, so a title and a title-plus-body
# are indistinguishable there).
#
# ``length(CAST(detail_json AS BLOB))`` rather than ``length(detail_json)``:
# SQLite's ``length()`` on TEXT counts CHARACTERS, and the stored JSON is emitted
# with ``ensure_ascii=False``, so a character count understates a non-ASCII
# detail and would let a segment exceed its stated ceiling.
_NARROW_ROW_COLS = (
    "source_path, line_offset, timestamp_utc, turn_id, call_id, kind, "
    "event_type, record_family, model, content_digest, content_len, "
    "detail_json, length(CAST(detail_json AS BLOB))"
)

# Bound on the ``line_offset`` list bound into one hydration query. SQLite's
# default host-parameter limit is 999 on older builds, and a full page can carry
# more positions than that, so the reads are chunked per source file.
_HYDRATE_CHUNK = 400


def _load_conversation_index_rows(
    conn: sqlite3.Connection, conversation_key: str,
) -> tuple[list, dict[tuple[str, int], int]]:
    """Phase A's narrow read: ``(rows, detail_bytes_by_position)``.

    Rows come back as ordinary ``CodexNormalizedRow`` objects with ``text``,
    ``search_tool`` and ``search_thinking`` blanked, so ``pair_mirrors`` and
    ``canonical_items`` run unchanged — both key on ``turn_id``, ``kind``,
    ``content_digest``, ``content_len``, ``record_family`` and the physical
    position, none of which lives in the excluded columns.

    Excluding ``text`` defers the bulk: on the heaviest conversation in the
    corpus ``content_len`` totals 84.6 MB against 7.1 MB of ``detail_json``.
    """
    rows: list = []
    detail_bytes: dict[tuple[str, int], int] = {}
    for (source_path, line_offset, timestamp_utc, turn_id, call_id, kind,
         event_type, record_family, model, content_digest, content_len,
         detail_json, detail_len) in conn.execute(
        "SELECT " + _NARROW_ROW_COLS + " FROM codex_conversation_messages "
        "WHERE conversation_key = ? "
        "ORDER BY timestamp_utc, source_path, line_offset",
        (conversation_key,),
    ):
        rows.append(kern.CodexNormalizedRow(
            conversation_key=conversation_key, source_root_key="",
            source_path=source_path, line_offset=line_offset,
            timestamp_utc=timestamp_utc, turn_id=turn_id, call_id=call_id,
            kind=kind, event_type=event_type, record_family=record_family,
            model=model, text="", content_digest=content_digest,
            content_len=content_len, detail_json=detail_json,
            search_tool="", search_thinking=""))
        detail_bytes[(source_path, line_offset)] = detail_len or 0
    return rows, detail_bytes


def _detail_bytes_of(rows) -> dict[tuple[str, int], int]:
    """``detail_json`` byte sizes for callers that already hold WIDE rows.

    The same quantity ``_load_conversation_index_rows`` gets from
    ``length(CAST(detail_json AS BLOB))`` — BYTES, not characters, because the
    stored JSON is emitted with ``ensure_ascii=False``.
    """
    return {
        (row.source_path, row.line_offset):
            len((row.detail_json or "").encode("utf-8"))
        for row in rows
    }


def _chunk_positions(positions) -> dict[str, list[list[int]]]:
    """Group physical positions by source file, chunked for a bound IN clause."""
    by_path: dict[str, list[int]] = {}
    for source_path, line_offset in positions:
        by_path.setdefault(source_path, []).append(line_offset)
    return {
        path: [offsets[i:i + _HYDRATE_CHUNK]
               for i in range(0, len(offsets), _HYDRATE_CHUNK)]
        for path, offsets in by_path.items()
    }


def _load_rows_at_positions(
    conn: sqlite3.Connection, conversation_key: str, positions,
) -> dict[tuple[str, int], object]:
    """Phase C's wide read, scoped to one page's physical positions."""
    hydrated: dict[tuple[str, int], object] = {}
    for path, chunks in _chunk_positions(positions).items():
        for chunk in chunks:
            marks = ",".join("?" for _ in chunk)
            for row in conn.execute(
                "SELECT " + _ROW_COLS + " FROM codex_conversation_messages "
                f"WHERE conversation_key = ? AND source_path = ? "
                f"AND line_offset IN ({marks})",
                (conversation_key, path, *chunk),
            ):
                built = kern.CodexNormalizedRow(*row)
                hydrated[(built.source_path, built.line_offset)] = built
    return hydrated


def _load_row_payloads(
    conn: sqlite3.Connection, conversation_key: str, positions=None,
) -> dict[tuple[str, int], tuple[str | None, dict]]:
    """Retained physical payloads for query-time card shaping.

    The retained payload remains the authoritative source used for full-payload
    readback and a defensive read-time re-shape; contract v3 also persists the
    same bounded card so replay-derived rollups and logical item counts converge.

    ``positions`` scopes the read to one page (#463 S1, Phase C). Passing None
    keeps the whole-conversation behaviour, which the export path still wants.
    """
    result: dict[tuple[str, int], tuple[str | None, dict]] = {}
    if positions is None:
        cursor = conn.execute(
            "SELECT source_path,line_offset,record_type,payload_json "
            "FROM codex_conversation_events WHERE conversation_key = ?",
            (conversation_key,))
        rows = list(cursor)
    else:
        wanted = set(positions)
        rows = []
        for path, chunks in _chunk_positions(wanted).items():
            for chunk in chunks:
                marks = ",".join("?" for _ in chunk)
                rows.extend(
                    row for row in conn.execute(
                        "SELECT source_path,line_offset,record_type,payload_json "
                        "FROM codex_conversation_events "
                        "WHERE conversation_key = ? AND source_path = ? "
                        f"AND line_offset IN ({marks})",
                        (conversation_key, path, *chunk))
                    if (row[0], row[1]) in wanted)
    for source_path, line_offset, record_type, payload_json in rows:
        try:
            obj = json.loads(payload_json or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if isinstance(payload, dict):
            result[(source_path, line_offset)] = (record_type, payload)
    return result


def _row_payload(row, payloads: dict) -> tuple[str | None, dict] | None:
    return payloads.get((row.source_path, row.line_offset))


def _row_display(row) -> str:
    """The row's display/search text from whichever column carries it."""
    return row.text or row.search_thinking or row.search_tool or ""


def _parse_detail(detail_json: str | None):
    if not detail_json:
        return None
    try:
        return json.loads(detail_json)
    except (json.JSONDecodeError, TypeError):
        return None


def _public_detail(detail):
    if not isinstance(detail, dict):
        return detail
    return {key: value for key, value in detail.items() if not key.startswith("_")}


def _item_kind(item: dict) -> str:
    klass = item["klass"]
    if klass == "prompt":
        return "user"
    if klass == "response":
        return "assistant"
    if klass == "event":
        return "event"
    if klass == "meta":
        return "meta"
    return item["anchor_row"].kind  # unturned: the row's own provider kind


def _item_meta(item: dict) -> dict | None:
    if item["klass"] != "meta":
        return None
    detail = _parse_detail(item["anchor_row"].detail_json)
    if not isinstance(detail, dict):
        return {"meta_kind": "context", "meta_label": "role", "skill_name": None}
    meta = {
        "meta_kind": detail.get("meta_kind") or "context",
        "meta_label": detail.get("meta_label") or "role",
        "skill_name": detail.get("skill_name"),
    }
    sections = detail.get("meta_sections")
    if isinstance(sections, list) and all(isinstance(value, str) for value in sections):
        meta["meta_sections"] = sections
    return meta


def _turn_scoped_call_owner_count(rows: list) -> dict[str, int]:
    """How many ``tool_call`` rows own each call id, counted over the WHOLE turn.

    This must stay turn-scoped (#463 S1, spec section 1). Recomputing it over a
    page-local segment would make a call id that appears twice in a turn but once
    in the page look uniquely owned, and a ``tool_output`` would then fold into
    the wrong call.
    """
    counts: dict[str, int] = {}
    for row in rows:
        if row.kind == "tool_call" and row.call_id:
            counts[row.call_id] = counts.get(row.call_id, 0) + 1
    return counts


def _reasoning_headings(detail, payload, block_key: str):
    """The additive ``headings`` array for one reasoning block, or ``None``.

    #463 S2 §2.3/§2.5. Read-time decomposition of the retained payload's
    ``summary`` entries into the individual authored headings, each addressed by
    ``<block_key>#<zero-based ordinal>``. The stored projection is NOT consulted
    and NOT modified: it feeds ``_row_is_reasoning_title``, which is a
    segmentation-boundary input.

    Headings come from ``payload["summary"]`` ONLY. ``payload["content"]`` is the
    body, which stays disclosure content and is never decomposed.

    All-or-nothing. When the payload is absent, unreadable or malformed, this
    returns ``None`` and the caller omits the field entirely, so the client falls
    back to today's ``title``/``summary`` rendering. Decomposition never fails the
    request and never partially populates.
    """
    if not isinstance(detail, dict) or not isinstance(detail.get("reasoning"), dict):
        return None
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary")
    if not isinstance(summary, list) or not summary:
        return None
    entries = []
    for entry in summary:
        if not isinstance(entry, dict):
            return None
        text = entry.get("text")
        # Mirror `_join_content_texts`, which is what produced the stored
        # summary: it keeps non-empty string `text` leaves and ignores the rest.
        if text is None:
            continue
        if not isinstance(text, str):
            return None
        if text:
            entries.append(text)
    headings = decompose_reasoning_headings(entries)
    if not headings:
        return None
    return [{"key": f"{block_key}#{ordinal}", "text": text}
            for ordinal, text in enumerate(headings)]


def _item_blocks_with_rows(
    item: dict, payloads: dict | None = None, *, preserve_marker_text: bool = False,
    call_owner_count: dict | None = None, decompose_headings: bool = False,
) -> list[list]:
    """Assemble an item's blocks (the historical ``_build_item_blocks`` behaviour)
    AND expose each block's underlying rows, so the detail renderer and the payload
    locator (§3.4) share ONE folding rule. Each entry is a 3-list
    ``[block_dict, primary_row, output_row_or_None]``: a ``tool_output`` folds into
    a preceding ``tool_call`` block only when its ``call_id`` is non-empty, owned by
    exactly one tool_call, and that call was already seen (call precedes output).
    Physical order within the item is preserved.

    EVERY block backed by a physical row carries an opaque ``block_key`` (§3.4)
    since #463 S2 §1; before it, only ``tool_call`` and a few event families did.
    The far smaller payload-readable set is marked separately by
    ``payload_which``, because a stable anchor and a retained payload are
    different properties (§1.1).

    ``decompose_headings`` adds the additive ``detail.reasoning.headings`` array
    (#463 S2 §2.5). It is OFF by default and off on the export path, because
    ``legacy_export`` loads only marker-bearing payloads and populating the field
    there would force a whole-conversation payload read to fill something the
    exporter never reads."""
    rows = item["rows"]
    payloads = payloads or {}
    lifecycle_positions = {
        (row.source_path, row.line_offset) for row in item.get("lifecycle_rows", [])
    }
    row_order = {(row.source_path, row.line_offset): index
                 for index, row in enumerate(rows)}
    # Turn-scoped when the caller supplies it (#463 S1 Phase C); otherwise
    # computed over this item's own rows, which is the same thing for an
    # unsegmented item.
    if call_owner_count is None:
        call_owner_count = _turn_scoped_call_owner_count(rows)
    entries: list[list] = []
    tool_entry_by_call: dict[str, int] = {}
    for r in rows:
        if (r.source_path, r.line_offset) in lifecycle_positions:
            continue
        text = _row_display(r)
        stored_detail = _parse_detail(r.detail_json)
        detail = _public_detail(stored_detail)
        retained = _row_payload(r, payloads)
        payload = retained[1] if retained is not None else None
        if (preserve_marker_text and isinstance(stored_detail, dict)
                and stored_detail.get("markers") and isinstance(payload, dict)):
            if retained[0] == "response_item":
                text = kern._join_content_texts(payload.get("content"))
            elif retained[0] == "event_msg":
                text = kern._stringify(payload.get("message"))
        if r.kind == "tool_call" and isinstance(payload, dict):
            card = kern.decode_tool_call_card(payload)
            if card is None:
                card = kern.decode_secondary_tool_call_card(payload)
            if card is not None:
                detail = dict(detail) if isinstance(detail, dict) else {}
                detail["card"] = card
        output_card = (kern.decode_tool_output_card(payload)
                       if r.kind == "tool_output" and isinstance(payload, dict)
                       else None)
        if output_card is not None:
            card, text = output_card
            detail = dict(detail) if isinstance(detail, dict) else {}
            detail["card"] = card
        if r.kind == "event" and isinstance(payload, dict):
            card = kern.decode_patch_event_card(payload)
            if card is None:
                card = kern.decode_secondary_event_card(payload)
            if card is not None:
                detail = dict(detail) if isinstance(detail, dict) else {}
                detail["card"] = card
        if (r.kind == "tool_output" and r.call_id
                and call_owner_count.get(r.call_id, 0) == 1
                and r.call_id in tool_entry_by_call):
            owner = entries[tool_entry_by_call[r.call_id]]
            if isinstance(detail, dict) and isinstance(detail.get("card"), dict):
                call_card = (owner[0].get("detail") or {}).get("card")
                if (detail["card"].get("status") == "unknown"
                        and isinstance(call_card, dict)
                        and isinstance(call_card.get("status"), str)):
                    detail["card"]["status"] = call_card["status"]
                    detail["card"]["is_error"] = call_card["status"] in {
                        "failed", "error"}
            owner[0]["output"] = {"text": text, "detail": detail}
            owner_card = (owner[0].get("detail") or {}).get("card")
            if isinstance(owner_card, dict) and owner_card.get("type") in {
                    "plan", "agent"} and isinstance(payload, dict):
                result = kern.decode_secondary_tool_result(payload)
                if result is not None:
                    owner_card["result"] = result
            owner[2] = r
            continue
        block_key = _block_key_for_row(r)
        if decompose_headings and r.kind == "reasoning":
            headings = _reasoning_headings(detail, payload, block_key)
            if headings is not None:
                detail = dict(detail)
                detail["reasoning"] = dict(detail["reasoning"])
                detail["reasoning"]["headings"] = headings
        block = {
            "kind": r.kind, "text": text, "detail": detail,
            "call_id": r.call_id, "timestamp_utc": r.timestamp_utc,
            # #463 S2 §1 — EVERY row-backed block carries the anchor, not only
            # tool_call. `payload_which` below still marks the far smaller set
            # that is payload-readable, because those are different properties.
            "block_key": block_key,
        }
        if (r.kind == "event" and r.event_type in {
                "web_search_end", "mcp_tool_call_end", "task_started", "task_complete"}
                or isinstance(stored_detail, dict) and stored_detail.get("markers")):
            block["payload_which"] = "event"
        if r.kind == "tool_call":
            if r.call_id and call_owner_count.get(r.call_id, 0) == 1:
                tool_entry_by_call[r.call_id] = len(entries)
        entries.append([block, r, None])

    # A native patch completion may carry an inner call id distinct from the
    # outer custom-tool call. Correlate only on unique same-id ownership, or on
    # one strictly bracketed single-patch call (call < event < its output).
    # Anything ambiguous remains its own event card.
    patch_calls = [
        (index, call_owner_count.get(entry[1].call_id, 0))
        for index, entry in enumerate(entries)
        if entry[1].kind == "tool_call"
        and isinstance((entry[0].get("detail") or {}).get("card"), dict)
        and entry[0]["detail"]["card"].get("type") == "patch"
    ]
    matched_calls: set[int] = set()
    suppress_events: set[int] = set()
    for event_index, event_entry in enumerate(entries):
        event_block, event_row, _unused = event_entry
        event_card = (event_block.get("detail") or {}).get("card")
        if not (event_row.kind == "event" and isinstance(event_card, dict)
                and event_card.get("source") == "patch_apply_end"):
            continue
        event_key = event_block["block_key"]
        event_block["payload_which"] = "event"
        same_id = [
            index for index, owner_count in patch_calls
            if index not in matched_calls and event_row.call_id
            and owner_count == 1
            and entries[index][1].call_id == event_row.call_id
        ]
        candidates = same_id if len(same_id) == 1 else []
        if not candidates:
            event_pos = row_order[(event_row.source_path, event_row.line_offset)]
            candidates = []
            for index, _owner_count in patch_calls:
                if index in matched_calls:
                    continue
                _call_block, call_row, output_row = entries[index]
                call_pos = row_order[(call_row.source_path, call_row.line_offset)]
                output_pos = (row_order.get((output_row.source_path, output_row.line_offset))
                              if output_row is not None else None)
                if (call_row.source_path == event_row.source_path
                        and output_row is not None
                        and output_row.source_path == event_row.source_path
                        and call_pos < event_pos and output_pos is not None
                        and event_pos < output_pos):
                    candidates.append(index)
        if len(candidates) != 1:
            continue
        owner_index = candidates[0]
        owner_card = entries[owner_index][0]["detail"]["card"]
        completion = dict(event_card)
        completion["event_block_key"] = event_key
        owner_card["completion"] = completion
        matched_calls.add(owner_index)
        suppress_events.add(event_index)
    if suppress_events:
        entries = [entry for index, entry in enumerate(entries)
                   if index not in suppress_events]

    # Web/MCP completion events fold only by one exact same-turn call id. The
    # kernel already uses this proof for logical item count; repeat it here to
    # produce the additive card body while preserving payload selectors.
    calls_by_id: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        row = entry[1]
        if row.kind == "tool_call" and row.call_id:
            calls_by_id.setdefault(row.call_id, []).append(index)
    suppress_secondary: set[int] = set()
    matched_secondary: set[int] = set()
    for event_index, event_entry in enumerate(entries):
        event_block, event_row, _unused = event_entry
        event_card = (event_block.get("detail") or {}).get("card")
        if not (event_row.kind == "event" and event_row.call_id
                and isinstance(event_card, dict)
                and event_card.get("type") in {
                    "web_search_completion", "mcp_completion"}):
            continue
        candidates = [
            index for index in calls_by_id.get(event_row.call_id, [])
            if index not in matched_secondary
            and entries[index][1].turn_id == event_row.turn_id
        ]
        if event_card.get("type") == "web_search_completion":
            candidates = [
                index for index in candidates
                if ((entries[index][0].get("detail") or {}).get("name")
                    == "web_search_call")
            ]
        if len(candidates) != 1:
            event_block["payload_which"] = "event"
            continue
        owner_index = candidates[0]
        owner_block = entries[owner_index][0]
        owner_detail = owner_block.get("detail")
        if not isinstance(owner_detail, dict):
            owner_detail = {}
            owner_block["detail"] = owner_detail
        owner_card = owner_detail.get("card")
        if event_card.get("type") == "mcp_completion":
            if not isinstance(owner_card, dict) or owner_card.get("type") != "mcp":
                owner_card = {
                    "schema_version": kern.CODEX_CARD_SCHEMA_VERSION,
                    "type": "mcp", "source": "function_call",
                    "call_status": "requested",
                    "name": owner_detail.get("name"),
                }
                owner_retained = _row_payload(entries[owner_index][1], payloads)
                owner_payload = owner_retained[1] if owner_retained is not None else None
                if isinstance(owner_payload, dict) and isinstance(
                        owner_payload.get("status"), str):
                    owner_card["call_status"] = owner_payload["status"]
                owner_detail["card"] = owner_card
        if not isinstance(owner_card, dict):
            event_block["payload_which"] = "event"
            continue
        owner_card["completion"] = {
            key: value for key, value in event_card.items()
            if key not in {"schema_version", "type", "source"}
        }
        owner_card["completion"]["event_block_key"] = event_block["block_key"]
        matched_secondary.add(owner_index)
        suppress_secondary.add(event_index)
    if suppress_secondary:
        entries = [entry for index, entry in enumerate(entries)
                   if index not in suppress_secondary]
    return entries


def _build_item_blocks(
    item: dict, payloads: dict | None = None, *, preserve_marker_text: bool = False,
    call_owner_count: dict | None = None, decompose_headings: bool = False,
) -> list[dict]:
    """Assemble an item's blocks, folding each ``tool_output`` into its
    ``tool_call`` block via ``call_id`` when that call_id has exactly one owner
    (§5.2). Physical order within the item is preserved. Thin projection of
    ``_item_blocks_with_rows`` — the single source of truth for the folding rule."""
    return [entry[0] for entry in _item_blocks_with_rows(
        item, payloads, preserve_marker_text=preserve_marker_text,
        call_owner_count=call_owner_count,
        decompose_headings=decompose_headings)]


def _item_lifecycle(item: dict) -> dict | None:
    lifecycle = item.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return None
    result = {
        key: (dict(value) if isinstance(value, dict) else value)
        for key, value in lifecycle.items()
    }
    result["events"] = [
        {
            "event": (_parse_detail(row.detail_json) or {}).get(
                "lifecycle", {}).get("event"),
            "payload_which": "event",
            "block_key": _block_key_for_row(row),
        }
        for row in item.get("lifecycle_rows", [])
    ]
    return result


# ── tokens union (§5.6) ───────────────────────────────────────────────────────


def _zero_tokens() -> dict:
    return {"input": 0, "output": 0, "cached_input": 0, "reasoning_output": 0}


def _add_tokens(acc: dict, inp: int, out: int, cin: int, rout: int) -> None:
    acc["input"] += inp or 0
    acc["output"] += out or 0
    acc["cached_input"] += cin or 0
    acc["reasoning_output"] += rout or 0


def _tokens_union(tokens: dict) -> dict:
    """Source-tagged provider union — native Codex fields only, never Claude
    cache vocabulary (§5.6 / S0)."""
    return {
        "source": "codex",
        "input": tokens["input"],
        "output": tokens["output"],
        "cached_input": tokens["cached_input"],
        "reasoning_output": tokens["reasoning_output"],
    }


# ── cost attribution (§5.4) ───────────────────────────────────────────────────


def _file_turn_map(conn: sqlite3.Connection, source_path: str) -> dict[int, str | None]:
    """Exact physical-offset → canonical logical turn for one retained file.

    Cost attribution and normalized prose use the same pure lifecycle inference,
    including resumed segments whose native proof arrives on task completion.
    """
    from _lib_jsonl import CodexPhysicalEvent

    events = [
        CodexPhysicalEvent(*row)
        for row in conn.execute(
            "SELECT source_path, line_offset, source_root_key, conversation_key, "
            "native_thread_id, root_thread_id, parent_thread_id, timestamp_utc, "
            "record_type, event_type, turn_id, call_id, payload_json "
            "FROM codex_conversation_events WHERE source_path = ? "
            "ORDER BY line_offset",
            (source_path,),
        )
    ]
    turns, _terminal = kern.infer_codex_event_turns(events)
    return {event.line_offset: turn for event, turn in zip(events, turns)}


def _attribute_costs(conn: sqlite3.Connection, conversation_key: str, effective_speed: str):
    """Attribute each ``codex_session_entries`` row (selected by
    ``conversation_key``) to its nearest-preceding turn, priced unrounded under
    ``effective_speed`` (§5.4). Rows preceding any turn land in the explicit
    unattributed bucket. Returns
    ``(turn_cost, turn_tokens, unattributed_cost, unattributed_tokens, total, conv_tokens)``.
    """
    entries = conn.execute(
        "SELECT source_path, line_offset, model, input_tokens, cached_input_tokens, "
        "output_tokens, reasoning_output_tokens FROM codex_session_entries "
        "WHERE conversation_key = ? ORDER BY source_path, line_offset",
        (conversation_key,),
    ).fetchall()
    turn_maps: dict[str, dict[int, str | None]] = {}
    for source_path in {e[0] for e in entries}:
        turn_maps[source_path] = _file_turn_map(conn, source_path)
    turn_cost: dict[str, float] = {}
    turn_tokens: dict[str, dict] = {}
    unattr_cost = 0.0
    unattr_tokens = _zero_tokens()
    total = 0.0
    conv_tokens = _zero_tokens()
    for source_path, offset, model, inp, cin, out, rout in entries:
        priced = _calculate_codex_entry_cost(
            model or "", inp or 0, cin or 0, out or 0, rout or 0, speed=effective_speed)
        total += priced
        _add_tokens(conv_tokens, inp, out, cin, rout)
        turn = turn_maps.get(source_path, {}).get(offset)
        if turn is not None:
            turn_cost[turn] = turn_cost.get(turn, 0.0) + priced
            _add_tokens(turn_tokens.setdefault(turn, _zero_tokens()), inp, out, cin, rout)
        else:
            unattr_cost += priced
            _add_tokens(unattr_tokens, inp, out, cin, rout)
    return turn_cost, turn_tokens, unattr_cost, unattr_tokens, total, conv_tokens


def _conversation_total_cost(conn: sqlite3.Connection, conversation_key: str, effective_speed: str) -> float:
    """Lean priced total over a conversation's accounting rows (browse rows,
    child summaries) — same primitive as ``_attribute_costs`` (§5.4)."""
    total = 0.0
    for model, inp, cin, out, rout in conn.execute(
        "SELECT model, input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens FROM codex_session_entries WHERE conversation_key = ?",
        (conversation_key,),
    ):
        total += _calculate_codex_entry_cost(
            model or "", inp or 0, cin or 0, out or 0, rout or 0, speed=effective_speed)
    return total


# ── rollup fields (dual-branch: stored fast path vs live recompute) ───────────


def _thread_facts(conn: sqlite3.Connection, conversation_key: str):
    """``(native, root, parent, source_root_key, cwd, git_json)`` for a
    conversation's thread, or ``None`` (no thread row / not-yet-linked)."""
    return conn.execute(
        "SELECT native_thread_id, root_thread_id, parent_thread_id, source_root_key, "
        "cwd, git_json FROM codex_conversation_threads WHERE conversation_key = ?",
        (conversation_key,),
    ).fetchone()


def _rollup_fields(conn: sqlite3.Connection, conversation_key: str, rows: list | None = None):
    """Rollup fields for a conversation — the stored rollup row when present
    (fast path), else a LIVE recompute that reproduces ``_recompute_codex_rollups``
    EXACTLY (§3.2 / §6.1): same kernel helpers (``rollup_item_count``,
    ``derive_title``), same min/max/sorted, and the SAME
    ``_codex_conversation_project_attribution`` the writer uses. Returns ``None``
    when the conversation has no normalized rows."""
    stored = conn.execute(
        "SELECT item_count, started_utc, last_activity_utc, project_key, project_label, "
        "models_json, title, parent_thread_id, source_root_key "
        "FROM codex_conversation_rollups WHERE conversation_key = ?",
        (conversation_key,),
    ).fetchone()
    thread = _thread_facts(conn, conversation_key)
    native = thread[0] if thread else None
    if stored is not None:
        item_count, started, last, project_key, project_label, models_json, title, parent, srk = stored
        models = json.loads(models_json) if models_json else []
        return {
            "item_count": item_count, "started": started, "last": last,
            "project_key": project_key, "project_label": project_label,
            "models": models, "title": title, "parent_thread_id": parent,
            "source_root_key": srk, "native_thread_id": native,
        }
    # Live recompute — MUST mirror _recompute_codex_rollups in _cctally_cache.
    if rows is None:
        rows = _load_conversation_rows(conn, conversation_key)
    if not rows:
        return None
    item_count = kern.rollup_item_count(rows)
    title = kern.derive_title(rows)
    timestamps = [r.timestamp_utc for r in rows if r.timestamp_utc]
    started = min(timestamps) if timestamps else None
    last = max(timestamps) if timestamps else None
    models = sorted({r.model for r in rows if r.model})
    source_root_key = rows[0].source_root_key
    cwd = git_json = parent = None
    if thread is not None:
        native, _root, parent, thread_root, cwd, git_json = thread
        if thread_root:
            source_root_key = thread_root
    from _cctally_cache import _codex_conversation_project_attribution
    project_key, project_label = _codex_conversation_project_attribution(
        source_root_key, cwd, git_json)
    return {
        "item_count": item_count, "started": started, "last": last,
        "project_key": project_key, "project_label": project_label,
        "models": models, "title": title, "parent_thread_id": parent,
        "source_root_key": source_root_key, "native_thread_id": native,
    }


def _short_native(native: str | None) -> str:
    return (native or "")[:8]


def _display_chain(fields: dict) -> str:
    """Read-time display fallback (§4.3): stored title → project_label → short
    native-thread-id prefix."""
    return fields.get("title") or fields.get("project_label") or _short_native(
        fields.get("native_thread_id")) or ""


def _conversation_display_title(conn: sqlite3.Connection, conversation_key: str, rows: list | None = None) -> str:
    fields = _rollup_fields(conn, conversation_key, rows=rows)
    if fields is None:
        return ""
    return _display_chain(fields)


def _conversation_hit_fields(conn: sqlite3.Connection, conversation_key: str):
    """``(title, last_activity_utc, project_label)`` for a search hit's conversation
    (§3.7). ONE ``_rollup_fields`` resolution (stored fast path or the identical
    live recompute), so the neutral search hit carries the conversation-level
    last-activity time (explicitly NOT the matched row's own timestamp) and a
    nullable project label without a per-row lookup."""
    fields = _rollup_fields(conn, conversation_key)
    if fields is None:
        return "", None, None
    return _display_chain(fields), fields.get("last"), fields.get("project_label")


# ── threading (§5.5) ──────────────────────────────────────────────────────────


def _child_summary(conn: sqlite3.Connection, conversation_key: str, effective_speed: str) -> dict:
    fields = _rollup_fields(conn, conversation_key)
    return {
        "conversation_key": conversation_key,
        "title": _display_chain(fields) if fields else "",
        "started_utc": fields["started"] if fields else None,
        "last_activity_utc": fields["last"] if fields else None,
        "item_count": fields["item_count"] if fields else 0,
        "cost_usd": _conversation_total_cost(conn, conversation_key, effective_speed),
    }


def _children_of(conn: sqlite3.Connection, conversation_key: str, effective_speed: str) -> list[dict]:
    """Same-root threads whose ``parent_thread_id`` equals this thread's native
    id (§5.5). Never a filename inference — metadata only."""
    thread = _thread_facts(conn, conversation_key)
    if thread is None:
        return []
    native, _root, _parent, source_root_key, _cwd, _git = thread
    children = [
        _child_summary(conn, child_ck, effective_speed)
        for (child_ck,) in conn.execute(
            "SELECT conversation_key FROM codex_conversation_threads "
            "WHERE source_root_key = ? AND parent_thread_id = ? AND conversation_key != ?",
            (source_root_key, native, conversation_key),
        )
    ]
    children.sort(key=lambda c: (c["last_activity_utc"] or "", c["conversation_key"]))
    return children


def _parent_of(conn: sqlite3.Connection, conversation_key: str):
    """Parent pointer (§5.5): the same-root thread whose native id equals this
    thread's ``parent_thread_id``. A root (parent == self, or absent) has none;
    a fork whose parent is not ingested also returns ``None`` (no key to point
    at)."""
    thread = _thread_facts(conn, conversation_key)
    if thread is None:
        return None
    native, _root, parent, source_root_key, _cwd, _git = thread
    if not parent or parent == native:
        return None
    prow = conn.execute(
        "SELECT conversation_key FROM codex_conversation_threads "
        "WHERE source_root_key = ? AND native_thread_id = ? AND conversation_key != ?",
        (source_root_key, parent, conversation_key),
    ).fetchone()
    if prow is None:
        return None
    parent_ck = prow[0]
    return {"conversation_key": parent_ck, "title": _conversation_display_title(conn, parent_ck)}


def _consistent_meta_value(direct, nested):
    present = [value for value in (direct, nested) if value is not None]
    if any(not isinstance(value, str) or not value for value in present):
        return False
    values = present
    if not values:
        return None
    return values[0] if len(set(values)) == 1 else False


def _agent_session_meta(payload: dict) -> dict | None:
    """Extract current retained child facts without persisting or exposing paths."""
    source = payload.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    spawn = spawn if isinstance(spawn, dict) else {}
    parent = _consistent_meta_value(
        payload.get("parent_thread_id"), spawn.get("parent_thread_id"))
    agent_path = _consistent_meta_value(payload.get("agent_path"), spawn.get("agent_path"))
    if parent is False or agent_path is False or not parent or not agent_path:
        return None
    return {
        "parent_thread_id": parent,
        "agent_path": agent_path,
        "role": _consistent_meta_value(payload.get("agent_role"), spawn.get("agent_role")),
        "nickname": _consistent_meta_value(
            payload.get("agent_nickname"), spawn.get("agent_nickname")),
    }


def _safe_agent_label(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or len(value) > 120 or "/" in value or "\\" in value:
        return None
    return value


def _spawn_child_link(
    conn: sqlite3.Connection, conversation_key: str, canonical_task_name: str,
) -> dict | None:
    """Return one opaque same-root child only on exact retained identity proof."""
    thread = _thread_facts(conn, conversation_key)
    if thread is None:
        return None
    native, _root, _parent, source_root_key, _cwd, _git = thread
    if not native or not source_root_key:
        return None
    matches: dict[str, list[dict]] = {}
    for child_key, payload_json in conn.execute(
        "SELECT conversation_key,payload_json FROM codex_conversation_events "
        "WHERE source_root_key=? AND record_type='session_meta' "
        "AND conversation_key IS NOT NULL AND conversation_key != ?",
        (source_root_key, conversation_key),
    ):
        try:
            record = json.loads(payload_json or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        facts = _agent_session_meta(payload) if isinstance(payload, dict) else None
        if not (facts and facts["parent_thread_id"] == native
                and facts["agent_path"] == canonical_task_name):
            continue
        if not codex_conversation_exists(conn, child_key):
            continue
        matches.setdefault(child_key, []).append(facts)
    if len(matches) != 1:
        return None
    child_key, facts_list = next(iter(matches.items()))
    link = {"conversation_key": child_key}
    for field in ("role", "nickname"):
        labels = {_safe_agent_label(facts.get(field)) for facts in facts_list}
        labels.discard(None)
        if len(labels) == 1:
            link[field] = next(iter(labels))
    return link


def _attach_spawn_child_links(
    conn: sqlite3.Connection, conversation_key: str, items: list[dict],
) -> None:
    cache: dict[str, dict | None] = {}
    for item in items:
        for block in item.get("blocks", []):
            detail = block.get("detail")
            card = detail.get("card") if isinstance(detail, dict) else None
            if not (isinstance(card, dict) and card.get("type") == "agent"
                    and card.get("operation") == "spawn_agent"):
                continue
            result = card.get("result")
            value = result.get("value") if isinstance(result, dict) else None
            task_name = value.get("task_name") if isinstance(value, dict) else None
            if not isinstance(task_name, str) or not task_name:
                continue
            if task_name not in cache:
                cache[task_name] = _spawn_child_link(
                    conn, conversation_key, task_name)
            if cache[task_name] is not None:
                card["child_conversation"] = cache[task_name]


def codex_conversation_exists(conn: sqlite3.Connection, conversation_key: str) -> bool:
    """Cheap existence probe (spec §5.2) — True iff any normalized
    ``codex_conversation_messages`` row carries ``conversation_key``. Used by the
    live-tail SSE preflight for the neutral existence decision. A missing table
    (bare ``_apply_cache_schema`` conn) reads as absent."""
    try:
        row = conn.execute(
            "SELECT 1 FROM codex_conversation_messages "
            "WHERE conversation_key = ? LIMIT 1",
            (conversation_key,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def codex_conversation_source_paths(
    conn: sqlite3.Connection, conversation_key: str
) -> list[str]:
    """Distinct ``source_path``s backing one Codex conversation (spec §5.3): its
    OWN normalized rows plus its CURRENT children's (children resolved via
    ``codex_conversation_threads`` parent links — same-root threads whose
    ``parent_thread_id`` equals this thread's native id, never a filename
    inference). This is the file set the live-tail watch loop polls; it widens as
    a child thread is ingested. Empty for an unknown / not-yet-normalized
    conversation."""
    keys = [conversation_key]
    thread = _thread_facts(conn, conversation_key)
    if thread is not None:
        native, _root, _parent, source_root_key, _cwd, _git = thread
        if native is not None:
            keys.extend(
                child_ck
                for (child_ck,) in conn.execute(
                    "SELECT conversation_key FROM codex_conversation_threads "
                    "WHERE source_root_key = ? AND parent_thread_id = ? "
                    "AND conversation_key != ?",
                    (source_root_key, native, conversation_key),
                )
            )
    placeholders = ",".join("?" for _ in keys)
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_path FROM codex_conversation_messages "
            f"WHERE conversation_key IN ({placeholders}) "
            "AND source_path IS NOT NULL",
            keys,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for (sp,) in rows:
        if sp not in seen:
            seen.add(sp)
            paths.append(sp)
    return paths


# ── detail assembly (§5.2 / §5.4 / §5.6) ──────────────────────────────────────


def _stale_codex_page(total: int) -> dict:
    """The page a cursor resolving to nothing returns (#463 S1 / F4).

    Mirrors the Claude kernel's ``_stale_empty_page`` contract: a stale or
    deleted cursor yields an EMPTY page, never a silent re-serve of the head or
    the tail. The old Codex kernel left the bound at the edge of the list, which
    is the second path by which ``before`` returned the wrong window.
    """
    return {"total": total, "returned": 0, "before": None, "after": None,
            "has_before": False, "has_after": False}


def _paginate_items(items: list[dict], *, after, before, tail: bool, limit: int,
                    block_budget: int | None = None,
                    byte_budget: int | None = None):
    """Cut one page from the assembled item list (#463 S1 / F4).

    ``tail`` is the flag the HTTP layer parses out of ``?tail=1`` — which page
    of ``limit`` to cut, never how many items to return.

    The four cursor branches mirror the Claude kernel's structure
    (``_lib_conversation_query.py`` :2372-2391) so the two can be read against
    each other, which is what stops them diverging again. TWO deliberate
    Codex-only differences, both pinned by ``tests/test_codex_pagination.py``:

      * ``limit == 0`` means UNBOUNDED, and the export path depends on that
        sentinel (``get_codex_conversation_export`` passes ``limit=0``). Claude's
        default branch computes ``end = min(limit, N)``, which for zero yields an
        empty page — ported literally, every Codex export would contain no
        conversation items. The unbounded case is therefore explicit, ahead of
        the four branches.
      * Cursor resolution covers BOTH primary item keys and ``member_item_keys``
        aliases, so a cursor naming an item folded by a later contract version
        still resolves. Claude's ``_idx`` checks only its primary anchor id, and
        taking its resolver along with its branch arithmetic would make every
        folded-item cursor stale. The branch arithmetic is what is ported; the
        resolver is not.

    The prior implementation computed ``lo``/``hi`` and then sliced, so a
    ``before`` request returned ``items[0:hi][:limit]`` — the conversation's
    OPENING items — with ``has_before`` False.

    TWO per-page budgets apply alongside ``limit``, and the first of them to be
    reached closes the page (spec section 2). They bound different costs and
    neither substitutes for the other:

      * ``block_budget`` bounds DOM construction cost, which the F2 profile
        shows dominates mounting.
      * ``byte_budget`` bounds transfer and parse cost, which is a byte cost a
        block count does not express, because a Codex block is far heavier than
        a Claude block.

    Neither is optional in production. The profiled response was
    ``total: 78, returned: 78, has_after: false`` — 13.3 MB served in one page,
    because 78 is fewer than the requested 500 — so a change that capped items
    alone would not have bounded that conversation at all. And after
    segmentation that same conversation serves 1,713 blocks, BELOW
    ``PAGE_BLOCK_BUDGET``, so the block bound never fires on it either and the
    response is still 13.24 MB: the byte budget is what actually closes it.

    Both are deliberately NOT applied to the unbounded ``limit == 0`` export
    case, which must stay whole. The trim comes off whichever end is not
    anchored to the cursor, so a reverse page still ends where the caller asked
    it to, and a page never shrinks below one item however large that item is.
    """
    index_by_key: dict[str, int] = {}
    for index, item in enumerate(items):
        index_by_key.setdefault(item["item_key"], index)
    for index, item in enumerate(items):
        for alias in item.get("member_item_keys", []):
            index_by_key.setdefault(alias, index)

    n = len(items)

    if not limit:
        start, end = 0, n
    elif tail:
        end = n
        start = max(0, n - limit)
    elif before is not None:
        b = index_by_key.get(before)
        if b is None:
            return [], _stale_codex_page(n)
        end = b
        start = max(0, end - limit)
    elif after is not None:
        a = index_by_key.get(after)
        if a is None:
            return [], _stale_codex_page(n)
        start = a + 1
        end = min(start + limit, n)
    else:
        start = 0
        end = min(limit, n)

    if limit and end > start and (block_budget or byte_budget):
        blocks = sum(item.get("block_count", 0) for item in items[start:end])
        source = sum(item.get("source_bytes", 0) for item in items[start:end])

        def _over() -> bool:
            return ((bool(block_budget) and blocks > block_budget)
                    or (bool(byte_budget) and source > byte_budget))

        anchored_at_end = tail or before is not None
        while end - start > 1 and _over():
            drop = items[start] if anchored_at_end else items[end - 1]
            blocks -= drop.get("block_count", 0)
            source -= drop.get("source_bytes", 0)
            if anchored_at_end:
                start += 1
            else:
                end -= 1

    window = items[start:end]
    has_before = start > 0
    has_after = end < n
    page = {
        "total": n, "returned": len(window),
        "before": window[0]["item_key"] if (window and has_before) else None,
        "after": window[-1]["item_key"] if (window and has_after) else None,
        "has_before": has_before, "has_after": has_after,
    }
    return window, page


def _row_source_bytes(row, detail_bytes: dict) -> int:
    """One row's source size: its content length plus its stored detail's BYTES.

    Source bytes deliberately overstate wire bytes. The server clips block
    payloads, so the measured ratio is roughly six to eight times — a
    conversation holding 14.6 MB of ``content_len`` serves 1.8 MB, and one
    holding 84.6 MB serves 13.3 MB. Any byte threshold must therefore be stated
    in source bytes and derived from a wire target through that ratio; S1
    computes the figure and exposes it, and gates only on block count.
    """
    return (row.content_len or 0) + detail_bytes.get(
        (row.source_path, row.line_offset), 0)


_UNSET = object()


def _row_is_reasoning_title(row, detail=_UNSET) -> bool:
    """True when this row's stored reasoning projection produces a ``title``.

    The first of the two semantic boundaries. Read from ``detail_json``, because
    ``search_thinking`` stores ``summary + "\\n" + body`` for a response item and
    so cannot tell ``summary="**T**", body="x"`` (a title) from
    ``summary="**T**\\nx", body=""`` (not one).

    ``detail`` lets a caller that has already parsed the row's ``detail_json``
    pass it in. Phase A runs over every row of the conversation, so parsing the
    same JSON twice per row is worth avoiding.
    """
    if row.kind != "reasoning":
        return False
    if detail is _UNSET:
        detail = _parse_detail(row.detail_json)
    reasoning = detail.get("reasoning") if isinstance(detail, dict) else None
    return isinstance(reasoning, dict) and bool(reasoning.get("title"))


def _fold_groups_for_item(item: dict, call_owner_count: dict,
                          detail_bytes: dict) -> list:
    """Derive one item's atomic fold groups (#463 S1, spec section 1).

    A group is a row together with every later row in the item that could fold
    into it. Membership is decided from ``call_id`` and the STORED card in
    ``detail_json``, deliberately without reading the retained event payloads —
    Phase A must not touch them. That makes the grouping a conservative SUPERSET
    of what the block builder will actually fold: a completion event whose card
    turns out not to fold stays grouped with its call anyway. A superset is the
    safe direction, because the only thing a group guarantees is that no boundary
    is drawn between a call and something that might fold into it.

    ``_item_blocks_with_rows`` performs THREE folds, not one, and grouping covers
    all three:

      * the id-matched fold — a ``tool_output`` or completion event whose
        ``call_id`` is owned by exactly one ``tool_call`` in the turn;
      * the bracketed native patch completion — a patch completion event may
        carry an INNER call id distinct from the outer custom-tool call, which
        the block builder folds by positional bracketing
        (``call_pos < event_pos < output_pos``) rather than by id. This is the
        common shape: 3,441 of the production corpus's 4,690 patch completion
        events carry a call id no ``tool_call`` in their turn owns. Such an event
        joins the most recent patch call whose output has not yet arrived;
      * the ``web_search_completion`` narrowing — that path filters its
        candidates by ``detail.name == "web_search_call"`` BEFORE requiring a
        unique candidate, and it bounds nothing about how many calls share the
        id, so it folds at ANY owner count. The registration below therefore
        imposes no owner ceiling either: it registers the web-search arm at
        ``owners >= 1``. Naming a fixed count leaves the pair ungrouped at every
        other count — an ``== 1`` gate never covers a two-owner id, and an
        ``== 2`` gate never covers a call id owned by three calls of which one
        is the web search.

    A boundary that split any of the three would make the page-local builder emit
    a standalone event card where the whole-turn builder emits a folded
    ``completion`` — the structural divergence spec section 1 forbids.

    Block counts are overestimated for the same reason. A ``tool_output`` whose
    call id is uniquely owned provably folds and contributes nothing; every other
    grouped row is counted as its own block even though it may fold. Over-
    counting shrinks segments slightly, which keeps the budget an upper bound.

    Each group also carries ``first_pos``/``last_pos``, its physical row range
    inside the item, which ``plan_segments`` uses to keep a segment contiguous.

    Lifecycle rows are excluded entirely: they never produce a block, and they
    are carried on segment 0 instead, where ``_item_lifecycle`` renders them from
    the narrow row alone.
    """
    lifecycle_positions = {
        (row.source_path, row.line_offset) for row in item.get("lifecycle_rows", [])
    }
    groups: list[dict] = []
    open_group_by_call: dict[str, dict] = {}
    open_patch_groups: list[dict] = []
    previous_kind = None
    position = 0
    for row in item["rows"]:
        if (row.source_path, row.line_offset) in lifecycle_positions:
            continue
        detail = _parse_detail(row.detail_json)
        card = detail.get("card") if isinstance(detail, dict) else None
        if not isinstance(card, dict):
            card = None
        owner = (open_group_by_call.get(row.call_id)
                 if row.call_id and row.kind in {"tool_output", "event"} else None)
        if (owner is None and row.kind == "event" and card is not None
                and card.get("source") == "patch_apply_end" and open_patch_groups):
            # The most RECENT still-open patch call is the one the block
            # builder's bracket resolves to in the single-patch case; taking an
            # older one would leave the true owner ungrouped.
            owner = open_patch_groups.pop()
        if owner is not None:
            owner["rows"].append(row)
            owner["last_pos"] = position
            owner["source_bytes"] += _row_source_bytes(row, detail_bytes)
            folds = (row.kind == "tool_output"
                     and call_owner_count.get(row.call_id, 0) == 1)
            if not folds:
                owner["block_count"] += 1
            # Identity, not equality. ``in`` and ``list.remove`` compare with
            # ``==``, so they would match — and delete — the FIRST group whose
            # dict merely compares equal to this one. That is correct today only
            # because two distinct groups can never hold equal contents, which is
            # an accident of the data rather than a property of this loop.
            if row.kind == "tool_output" and any(g is owner for g in open_patch_groups):
                open_patch_groups[:] = [g for g in open_patch_groups if g is not owner]
            position += 1
            continue
        group = {
            "rows": [row],
            "block_count": 1,
            "source_bytes": _row_source_bytes(row, detail_bytes),
            "is_title_boundary": _row_is_reasoning_title(row, detail),
            "is_tool_transition": (row.kind == "tool_call"
                                   and previous_kind in {"assistant", "reasoning"}),
            "first_pos": position,
            "last_pos": position,
        }
        groups.append(group)
        if row.kind == "tool_call" and row.call_id:
            owners = call_owner_count.get(row.call_id, 0)
            name = detail.get("name") if isinstance(detail, dict) else None
            # The web-search arm must not impose an owner ceiling the block
            # builder does not. `_pair_web_search_completions` filters the
            # candidates by `detail.name == "web_search_call"` and then requires a
            # unique survivor, with no bound on how many calls share the id — so a
            # call id owned by three calls of which exactly one is a web search
            # folds there while `owners == 2` refused to group it here, and a
            # segment boundary could fall between the call and its completion.
            # `owners >= 1` matches the builder and stays a conservative superset:
            # grouping only ever keeps rows together that the builder folds.
            if owners == 1 or (owners >= 1 and name == "web_search_call"):
                open_group_by_call.setdefault(row.call_id, group)
        if row.kind == "tool_call" and card is not None and card.get("type") == "patch":
            open_patch_groups.append(group)
        previous_kind = row.kind
        position += 1
    return [
        segkern.FoldGroup(
            rows=group["rows"], block_count=group["block_count"],
            source_bytes=group["source_bytes"],
            is_title_boundary=group["is_title_boundary"],
            is_tool_transition=group["is_tool_transition"],
            first_pos=group["first_pos"], last_pos=group["last_pos"])
        for group in groups
    ]


def _build_segment_index(
    conversation_key: str, items: list[dict], detail_bytes: dict, *,
    segmented: bool, block_budget: int | None = None,
) -> list[dict]:
    """Phase A's output: an ordered index of segments, with no block content.

    Each entry carries its keys, turn membership, ordinal, the physical rows it
    covers, its sizes, and the two structural facts Phase C requires and cannot
    recompute correctly on its own — the fold-group membership that makes a
    boundary legal, and the TURN-scoped ``call_owner_count``.

    ``segmented=False`` gives each item exactly one segment holding all of its
    groups, which is what the export path uses so its item grouping stays
    byte-identical.

    ``block_budget`` resolves to ``segkern.SEGMENT_BLOCK_BUDGET`` at CALL time
    when omitted, never as a default argument value: a default argument binds
    once at import, so a test that lowered the budget would silently keep the
    imported figure and pass vacuously.
    """
    index: list[dict] = []
    for item_index, item in enumerate(items):
        turn_key = _item_key_for_item(conversation_key, item)
        call_owner_count = _turn_scoped_call_owner_count(item["rows"])
        groups = _fold_groups_for_item(item, call_owner_count, detail_bytes)
        if not groups:
            segments = [segkern.Segment(
                ordinal=0, groups=[], block_count=0, source_bytes=0,
                anchor_row=item["anchor_row"])]
        elif segmented and item["klass"] == "response":
            segments = segkern.plan_segments(groups, block_budget=block_budget)
        else:
            segments = [segkern.Segment(
                ordinal=0, groups=groups,
                block_count=sum(group.block_count for group in groups),
                source_bytes=sum(group.source_bytes for group in groups),
                anchor_row=groups[0].rows[0])]
        for segment in segments:
            head = segment.ordinal == 0
            anchor = item["anchor_row"] if head else segment.anchor_row
            # PHYSICAL order, not group-flatten order. A folded row is appended
            # to an earlier group, so flattening the groups would move it next to
            # its call — and the patch-completion fold decides ambiguous cases by
            # positional bracketing (call < event < its output) over the item's
            # row order, which that reordering silently breaks.
            member = {(row.source_path, row.line_offset)
                      for group in segment.groups for row in group.rows}
            segment_rows = [row for row in item["rows"]
                            if (row.source_path, row.line_offset) in member]
            entry = {
                "item_key": turn_key if head else codex_item_key(
                    conversation_key, klass="segment", turn_id=item["turn_id"],
                    source_path=segment.anchor_row.source_path,
                    line_offset=segment.anchor_row.line_offset,
                    content_digest=segment.anchor_row.content_digest),
                "member_item_keys": (
                    _member_item_keys(conversation_key, item) if head else []),
                "turn_item_key": turn_key,
                "segment_ordinal": segment.ordinal,
                "kind": _item_kind(item),
                "timestamp_utc": anchor.timestamp_utc,
                "model": anchor.model,
                "block_count": segment.block_count,
                "source_bytes": segment.source_bytes,
                # Phase C inputs — never serialized.
                "_item_index": item_index,
                "_klass": item["klass"],
                "_turn_id": item["turn_id"],
                "_anchor_row": anchor,
                "_rows": segment_rows,
                "_call_owner_count": call_owner_count,
                "_meta": _item_meta(item) if head else None,
                "_lifecycle": _item_lifecycle(item) if head else None,
                "_lifecycle_rows": item.get("lifecycle_rows", []) if head else [],
            }
            index.append(entry)
    return index


def get_codex_conversation(
    conn: sqlite3.Connection,
    conversation_key: str,
    *,
    effective_speed: str,
    after: str | None = None,
    before: str | None = None,
    tail: bool = False,
    limit: int = 200,
    legacy_export: bool = False,
) -> dict:
    """Detail envelope (§5.6): status ``ok`` | ``normalization_pending`` |
    ``not_found``. ``ok`` carries canonical items (mirror-paired, tool-folded),
    per-turn cost with an explicit unattributed bucket, threading, and a page
    over ``item_key``.

    Assembly runs in three phases (#463 S1, finding F3). Before this change every
    step from row loading through block building processed the WHOLE
    conversation, and ``_paginate_items`` ran last, so pagination reduced
    serialization and transfer but bounded no work.

      * **Phase A** reads every row narrowly — everything except ``text``, and no
        event payloads at all — pairs mirrors, groups canonical items, derives
        fold groups and segment boundaries, and emits a segment index.
      * **Phase B** paginates that index. It is arithmetic over a narrow list.
      * **Phase C** hydrates ONLY the requested page: the wide ``text`` read and
        the events-table payload scan are scoped to the page's physical
        positions, and blocks are built per segment.

    What stays proportional to the conversation is the narrow index pass, because
    segment boundaries and the ``has_before``/``has_after`` flags are global
    facts. What becomes proportional to the page is everything expensive.

    Cost attribution deliberately stays whole-conversation: ``_attribute_costs``
    reconciles per-turn costs against an unattributed bucket and the envelope
    reports conversation-level totals, so scoping it to a page would change the
    reported total. It reads ``codex_session_entries``, not the large message
    table.

    ``page.total`` is now a count of SEGMENTS rather than of items.
    """
    if not codex_normalization_authoritative(conn):
        return {"status": "normalization_pending", "conversation_key": conversation_key,
                "items": [], "children": []}
    # ── Phase A: the narrow index pass ───────────────────────────────────────
    rows, detail_bytes = _load_conversation_index_rows(conn, conversation_key)
    if not rows:
        return {"status": "not_found", "conversation_key": conversation_key}
    kept, _suppressed = kern.pair_mirrors(rows)
    items = kern.canonical_items(
        kept, fold_patch_completions=not legacy_export)
    turn_cost, turn_tokens, unattr_cost, unattr_tokens, total, conv_tokens = _attribute_costs(
        conn, conversation_key, effective_speed)
    # Carrier item per turn: prefer the response item, else the first item of the
    # turn — so every priced turn's cost lands on exactly one item (§5.4 reconcile).
    # Segmentation must not move the carrier, so the selection stays keyed on the
    # ITEM index and the cost lands on that item's segment 0. Every other segment
    # carries null rather than zero, because a zero is indistinguishable from a
    # genuinely free turn.
    carriers: dict[str, int] = {}
    for idx, it in enumerate(items):
        if it["klass"] == "response" and it["turn_id"] is not None and it["turn_id"] not in carriers:
            carriers[it["turn_id"]] = idx
    for idx, it in enumerate(items):
        if it["turn_id"] is not None and it["turn_id"] not in carriers:
            carriers[it["turn_id"]] = idx
    # Turns with cost but no carrier item fold into the unattributed bucket.
    leftover_cost = 0.0
    for turn, cost in turn_cost.items():
        if turn not in carriers:
            leftover_cost += cost
    unattributed_cost = unattr_cost + leftover_cost
    # Segmentation is disabled under legacy_export, so item grouping there stays
    # byte-identical to what the export golden already pins.
    index = _build_segment_index(
        conversation_key, items, detail_bytes, segmented=not legacy_export)

    # ── Phase B: paginate the index ──────────────────────────────────────────
    page_index, page = _paginate_items(
        index, after=after, before=before, tail=tail, limit=limit,
        block_budget=None if legacy_export else segkern.PAGE_BLOCK_BUDGET,
        byte_budget=None if legacy_export else segkern.PAGE_SOURCE_BYTE_BUDGET)

    # ── Phase C: hydrate only the page ───────────────────────────────────────
    page_positions = {
        (row.source_path, row.line_offset)
        for entry in page_index for row in entry["_rows"]
    }
    hydrated = _load_rows_at_positions(conn, conversation_key, page_positions)
    if len(hydrated) != len(page_positions):
        # A miss here is a bug, not a degradation. The narrow index pass and this
        # wide read select from the SAME table on the same conversation key, so a
        # position that appears in one and not the other means the two reads
        # disagree. Falling back to the narrow row would silently render an empty
        # block, because the narrow row carries no ``text``.
        missing = sorted(page_positions - set(hydrated))[:5]
        raise RuntimeError(
            f"codex detail hydration missed {len(page_positions) - len(hydrated)} "
            f"of {len(page_positions)} page rows for {conversation_key}; "
            f"first missing positions: {missing}")
    # Detail/API callers receive the exact card display projection from retained
    # provider payloads. Export deliberately renders the byte-frozen legacy text
    # while retaining the same additive card metadata, so it scopes the payload
    # read to marker-bearing rows across the WHOLE conversation rather than to
    # the page — a different set from page_positions, hence a different name.
    if legacy_export:
        marker_positions = {
            (row.source_path, row.line_offset)
            for row in rows
            if isinstance(_parse_detail(row.detail_json), dict)
            and bool(_parse_detail(row.detail_json).get("markers"))
        }
        payloads = _load_row_payloads(conn, conversation_key, marker_positions)
    else:
        payloads = _load_row_payloads(conn, conversation_key, page_positions)
    built: list[dict] = []
    for entry in page_index:
        page_rows = [
            hydrated[(row.source_path, row.line_offset)]
            for row in entry["_rows"]
        ]
        page_item = {
            "klass": entry["_klass"], "rows": page_rows,
            "turn_id": entry["_turn_id"], "anchor_row": entry["_anchor_row"],
        }
        item_index = entry["_item_index"]
        turn = entry["_turn_id"]
        cost = tokens = None
        if (entry["segment_ordinal"] == 0 and turn is not None
                and carriers.get(turn) == item_index and turn in turn_cost):
            cost = turn_cost[turn]
            tokens = _tokens_union(turn_tokens[turn])
        item = {
            "item_key": entry["item_key"],
            "member_item_keys": entry["member_item_keys"],
            "turn_item_key": entry["turn_item_key"],
            "segment_ordinal": entry["segment_ordinal"],
            "kind": entry["kind"],
            "timestamp_utc": entry["timestamp_utc"],
            "model": entry["model"],
            "blocks": _build_item_blocks(
                page_item, payloads, preserve_marker_text=legacy_export,
                call_owner_count=entry["_call_owner_count"],
                # #463 S2 §2.5 — never under legacy_export: that path loads only
                # marker-bearing payloads, so populating `headings` there would
                # force a whole-conversation payload read for a field the
                # exporter never reads.
                decompose_headings=not legacy_export),
            "cost_usd": cost,
            "tokens": tokens,
        }
        if entry["_meta"] is not None:
            item.update(entry["_meta"])
        if entry["_lifecycle"] is not None:
            item["lifecycle"] = entry["_lifecycle"]
        built.append(item)
    _attach_spawn_child_links(conn, conversation_key, built)
    page_items = built
    return {
        "status": "ok",
        "conversation_key": conversation_key,
        # NOT the Phase A rows: those carry no ``text``, and the live-recompute
        # fallback inside _rollup_fields derives the title from it. Passing None
        # keeps the stored fast path unchanged and lets the rare no-rollup case
        # do its own wide read rather than titling the conversation "".
        "title": _conversation_display_title(conn, conversation_key),
        "items": page_items,
        "page": page,
        "children": _children_of(conn, conversation_key, effective_speed),
        "parent": _parent_of(conn, conversation_key),
        "total_cost_usd": total,
        "unattributed_cost_usd": unattributed_cost,
        "tokens": _tokens_union(conv_tokens),
    }


# ── outline assembly (§5.6) ───────────────────────────────────────────────────


def _conversation_files(conn: sqlite3.Connection, conversation_key: str) -> list[dict]:
    return [
        {"file_path": fp, "tool": tool, "count": count}
        for fp, tool, count in conn.execute(
            "SELECT file_path, tool, COUNT(*) FROM codex_conversation_file_touches "
            "WHERE conversation_key = ? GROUP BY file_path, tool ORDER BY file_path, tool",
            (conversation_key,),
        )
    ]


def get_codex_conversation_outline(
    conn: sqlite3.Connection, conversation_key: str, *, effective_speed: str
) -> dict:
    """Outline envelope (§5.6): one ``turns[]`` entry per canonical item (label
    via the shared first-non-blank-line helper), plus stats, file touches, and
    child summaries.

    The outline stays TURN-granular: ``turns[].item_key`` remains a turn key,
    which is still valid because it is segment 0's key.

    Turn-granular keys alone are not sufficient, though (#463 S1). On a cold
    jump ``loadToTarget`` resolves the target through the outline and does
    nothing when the identifier is absent, and outline membership carried only
    folded-item aliases — no segment keys at all — so a deep link into any
    segment but a turn's first would silently fail to navigate. Each turn
    therefore also carries ``segment_item_keys``, where entry ``i`` is the key of
    segment ``i``.

    That channel is deliberately DISTINCT from ``member_item_keys``. Putting
    segment keys there would make ``loadToTarget``'s "is it already loaded" test
    report true for a segment that has not been fetched, so the drain would never
    run and the jump would land nowhere. Membership for navigation and membership
    for "this item subsumes that key" are different relations.
    """
    if not codex_normalization_authoritative(conn):
        return {"status": "normalization_pending", "conversation_key": conversation_key,
                "turns": [], "files": [], "children": []}
    # Deliberately the WIDE read: an outline label is the first non-blank line of
    # its anchor row's display text, which the narrow index pass does not carry.
    # The outline is not the route F3 bounds, and it stays turn-granular.
    rows = _load_conversation_rows(conn, conversation_key)
    if not rows:
        return {"status": "not_found", "conversation_key": conversation_key}
    detail_bytes = _detail_bytes_of(rows)
    kept, _suppressed = kern.pair_mirrors(rows)
    items = kern.canonical_items(kept)
    segment_keys: dict[int, list[str]] = {}
    for entry in _build_segment_index(
            conversation_key, items, detail_bytes, segmented=True):
        segment_keys.setdefault(entry["_item_index"], []).append(entry["item_key"])
    turns: list[dict] = []
    kind_totals: dict[str, int] = {}
    # Keyed on the ITEM index, which is what _build_segment_index records. Using
    # ``len(turns)`` would be correct only for as long as this loop appends a
    # turn for every item without exception; a later ``continue`` would misalign
    # every subsequent turn's segment keys, and the plausible-looking
    # ``[item_key]`` fallback would hide it by returning a well-formed answer.
    for index, it in enumerate(items):
        meta = _item_meta(it)
        anchor_text = _row_display(it["anchor_row"])
        if meta is not None:
            label = _META_LABEL_TEXT.get(meta["meta_label"], "Harness context")
        else:
            label = _first_nonblank_line(_strip_ansi(anchor_text)) if anchor_text else ""
        kinds: dict[str, int] = {}
        for r in it["rows"]:
            kinds[r.kind] = kinds.get(r.kind, 0) + 1
            kind_totals[r.kind] = kind_totals.get(r.kind, 0) + 1
        item_key = _item_key_for_item(conversation_key, it)
        turn = {
            "item_key": item_key,
            "member_item_keys": _member_item_keys(conversation_key, it),
            "segment_item_keys": segment_keys.get(index, [item_key]),
            "label": label,
            "timestamp_utc": it["anchor_row"].timestamp_utc,
            "kinds": kinds,
        }
        if meta is not None:
            turn.update(meta)
        turns.append(turn)
    return {
        "status": "ok",
        "conversation_key": conversation_key,
        "turns": turns,
        "stats": {"items": len(items), "kinds": kind_totals},
        "files": _conversation_files(conn, conversation_key),
        "children": _children_of(conn, conversation_key, effective_speed),
    }


# ── browse (§6.1) ─────────────────────────────────────────────────────────────


def _is_fork(fields: dict) -> bool:
    parent = fields.get("parent_thread_id")
    return bool(parent) and parent != fields.get("native_thread_id")


def _browse_row(conn: sqlite3.Connection, conversation_key: str, effective_speed: str, fields: dict) -> dict:
    return {
        "conversation_key": conversation_key,
        "title": _display_chain(fields),
        "project_key": fields["project_key"],
        "project_label": fields["project_label"],
        "started_utc": fields["started"],
        "last_activity_utc": fields["last"],
        "count": fields["item_count"],
        "cost_usd": _conversation_total_cost(conn, conversation_key, effective_speed),
        "models": list(fields["models"]),
        "parent": _parent_of(conn, conversation_key),
        "is_fork": _is_fork(fields),
    }


def _browse_facets(rows: list[dict]) -> dict:
    """Projects grouped by opaque ``project_key`` (S3 collision-safe — same-label
    distinct roots never merge), models by native model name (§6.1)."""
    projects: dict[str, list] = {}
    models: dict[str, int] = {}
    for row in rows:
        pkey = row["project_key"]
        if pkey:
            entry = projects.setdefault(pkey, [row["project_label"], 0])
            entry[1] += 1
        for model in row["models"] or []:
            models[model] = models.get(model, 0) + 1
    project_facets = [
        {"project_key": pkey, "project_label": label, "count": count}
        for pkey, (label, count) in sorted(
            projects.items(), key=lambda kv: ((kv[1][0] or ""), kv[0]))
    ]
    model_facets = [
        {"model": model, "count": count} for model, count in sorted(models.items())
    ]
    return {"projects": project_facets, "models": model_facets}


def _recent_sort_key(row: dict):
    return (row["last_activity_utc"] or "", row["conversation_key"])


def _paginate_rows(rows: list[dict], *, cursor: str | None, limit: int):
    lo = 0
    if cursor is not None:
        keys = [r["conversation_key"] for r in rows]
        if cursor in keys:
            lo = keys.index(cursor) + 1
    window = rows[lo:lo + limit] if limit else rows[lo:]
    has_more = (lo + len(window)) < len(rows)
    next_cursor = window[-1]["conversation_key"] if (window and has_more) else None
    page = {"total": len(rows), "returned": len(window), "cursor": next_cursor}
    return window, page


def list_codex_conversations(
    conn: sqlite3.Connection,
    *,
    effective_speed: str,
    project_key: str | None = None,
    model: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    """Browse envelope (§5.6 / §6.1): a page of conversation rows ordered by last
    activity, with project/model facets. Dual-branch — the stored rollup fast
    path when a rollup row is present, else a live recompute that reproduces the
    writer exactly (never an empty rail). Facets are computed over the full set
    (before filtering) so filter options stay available. Pending status while
    migration 025 has not run."""
    if not codex_normalization_authoritative(conn):
        return {"status": "normalization_pending", "rows": [],
                "facets": {"projects": [], "models": []}, "page": {"total": 0}}
    keys = [r[0] for r in conn.execute(
        "SELECT DISTINCT conversation_key FROM codex_conversation_messages")]
    rows: list[dict] = []
    for conversation_key in keys:
        fields = _rollup_fields(conn, conversation_key)
        if fields is None:
            continue
        rows.append(_browse_row(conn, conversation_key, effective_speed, fields))
    facets = _browse_facets(rows)
    filtered = [
        row for row in rows
        if (project_key is None or row["project_key"] == project_key)
        and (model is None or model in (row["models"] or []))
    ]
    filtered.sort(key=_recent_sort_key, reverse=True)
    page_rows, page = _paginate_rows(filtered, cursor=cursor, limit=limit)
    return {"status": "ok", "rows": page_rows, "facets": facets, "page": page}


# ── search (§6.2) ─────────────────────────────────────────────────────────────


def _search_mode(conn: sqlite3.Connection) -> str:
    """Honest search mode: ``like`` when the Codex FTS marker is set or the FTS
    vtable is unusable, else ``fts`` (§3.4 / §6.2)."""
    try:
        unavailable = conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='codex_fts_unavailable'").fetchone() is not None
    except sqlite3.OperationalError:
        unavailable = True
    if unavailable:
        return "like"
    try:
        conn.execute("SELECT 1 FROM codex_conversation_fts LIMIT 1")
    except sqlite3.OperationalError:
        return "like"
    return "fts"


def _pos_to_item_key_and_order(
    conn: sqlite3.Connection, conversation_key: str,
) -> tuple[dict, list[str]]:
    """``_pos_to_item_key``'s map, plus the segment keys in detail document order.

    Any caller that turns matched POSITIONS back into an ordered anchor list needs
    both halves, and it must take the order from the SAME segment index the map
    came from. Rebuilding the order from ``kern.canonical_items`` instead yields
    turn keys, which agree with the map only for segment 0 — so every hit past the
    first segment of a turn silently disappears.

    Suppressed mirror members fold to their canonical partner's key, so both
    members of a pair share one key and can never double-count.

    Resolving to the turn rather than to the segment is the defect that most
    nearly shipped: search and find derive their anchors here, so a find hit
    would jump to the head of a turn instead of to the matching content.

    The narrow index read is enough — the map needs positions and keys, not
    ``text``.
    """
    rows, detail_bytes = _load_conversation_index_rows(conn, conversation_key)
    partners = kern.pair_mirror_partners(rows)
    kept, _suppressed = kern.pair_mirrors(rows)
    items = kern.canonical_items(kept)
    pos_map: dict[tuple, str] = {}
    order: list[str] = []
    for entry in _build_segment_index(
            conversation_key, items, detail_bytes, segmented=True):
        order.append(entry["item_key"])
        for r in entry["_rows"]:
            pos_map[(r.source_path, r.line_offset)] = entry["item_key"]
        # Lifecycle rows produce no block and so belong to no fold group, but
        # they are still physical rows a search hit can name. They resolve to
        # their turn's head segment.
        for r in entry["_lifecycle_rows"]:
            pos_map.setdefault((r.source_path, r.line_offset), entry["item_key"])
    for sup_idx, canon_idx in partners.items():
        sup = rows[sup_idx]
        canon = rows[canon_idx]
        canon_key = pos_map.get((canon.source_path, canon.line_offset))
        if canon_key is not None:
            pos_map[(sup.source_path, sup.line_offset)] = canon_key
    return pos_map, order


def _pos_to_item_key(conn: sqlite3.Connection, conversation_key: str) -> dict:
    """Map every physical row ``(source_path, line_offset)`` of a conversation to
    the ``item_key`` of the SEGMENT that contains it (§6.2, #463 S1)."""
    return _pos_to_item_key_and_order(conn, conversation_key)[0]


def _fts_query(query: str, column: str | None) -> str:
    """A safe FTS5 query: each whitespace term becomes a quoted phrase, joined by
    implicit AND (term-wise AND — the documented divergence from LIKE's single
    contiguous substring, §6.2). Each term is optionally scoped to one column."""
    terms = [t for t in query.split() if t]
    if not terms:
        return '""'
    def _term(t: str) -> str:
        phrase = '"' + t.replace('"', '""') + '"'
        return f'{column} : {phrase}' if column else phrase
    return " ".join(_term(t) for t in terms)


_FTS_COLUMN_BY_KIND = {
    "all": None, "prompts": "text", "assistant": "text",
    "tools": "search_tool", "thinking": "search_thinking",
}


def _matched_message_rows(conn: sqlite3.Connection, query: str, kind: str, mode: str) -> list:
    """Physical message rows matching ``query`` for a message-oriented kind, via
    the FTS path (MATCH with per-kind column scope) or the SQL-bounded LIKE
    mirror. ``prompts``/``assistant`` add the kind filter after the text match."""
    cols = "m.id, m.conversation_key, m.source_path, m.line_offset, m.kind, m.text, m.search_tool, m.search_thinking"
    if mode == "fts":
        fts_query = _fts_query(query, _FTS_COLUMN_BY_KIND[kind])
        rows = list(conn.execute(
            "SELECT " + cols + " FROM codex_conversation_fts f "
            "JOIN codex_conversation_messages m ON m.id = f.rowid "
            "WHERE f.codex_conversation_fts MATCH ?",
            (fts_query,),
        ))
    else:
        like = f"%{query}%"
        if kind == "all":
            cond = "(m.text LIKE ? OR m.search_tool LIKE ? OR m.search_thinking LIKE ?)"
            params: tuple = (like, like, like)
        elif kind in ("prompts", "assistant"):
            cond, params = "m.text LIKE ?", (like,)
        elif kind == "tools":
            cond, params = "m.search_tool LIKE ?", (like,)
        else:  # thinking
            cond, params = "m.search_thinking LIKE ?", (like,)
        rows = list(conn.execute(
            "SELECT " + cols + " FROM codex_conversation_messages m WHERE " + cond, params))
    if kind == "prompts":
        rows = [r for r in rows if r[4] == "user"]
    elif kind == "assistant":
        rows = [r for r in rows if r[4] == "assistant"]
    return rows


def _badge_for_kind(kind: str) -> str:
    return _SEARCH_BADGE.get(kind, kind)


def _excerpt(text: str | None) -> str:
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    return collapsed[:200]


def _collapse_message_hits(conn: sqlite3.Connection, matched_rows: list) -> list[dict]:
    """Collapse matched physical rows to canonical ``item_key`` BEFORE totals /
    badges (§6.2) — both members of a mirror pair map to one item_key, so mirror
    rows never double-count (turned or unturned)."""
    by_conv: dict[str, list] = {}
    for _id, ck, source_path, line_offset, kind, text, stool, sthink in matched_rows:
        by_conv.setdefault(ck, []).append(
            (source_path, line_offset, kind, text or stool or sthink))
    collapsed: dict[tuple, dict] = {}
    for ck, mrows in by_conv.items():
        pos_map = _pos_to_item_key(conn, ck)
        title, last_act, project_label = _conversation_hit_fields(conn, ck)
        for source_path, line_offset, kind, disp in mrows:
            item_key = pos_map.get((source_path, line_offset))
            if item_key is None:
                continue
            hit = collapsed.setdefault(
                (ck, item_key),
                {"conversation_key": ck, "item_key": item_key, "title": title,
                 "snippet": None, "_badges": set(),
                 "last_activity_utc": last_act, "project_label": project_label})
            hit["_badges"].add(_badge_for_kind(kind))
            if hit["snippet"] is None:
                hit["snippet"] = _excerpt(disp)
    return [
        {"conversation_key": h["conversation_key"], "item_key": h["item_key"],
         "title": h["title"], "snippet": h["snippet"], "badges": sorted(h["_badges"]),
         "last_activity_utc": h["last_activity_utc"], "project_label": h["project_label"]}
        for h in collapsed.values()
    ]


def _search_title(conn: sqlite3.Connection, query: str) -> list[dict]:
    """Title search over the rollup table — identical LIKE semantics in both FTS
    and LIKE modes (§6.2). Conversation-level hits (no item anchor)."""
    like = f"%{query}%"
    hits = []
    for ck, title, last_act, project_label in conn.execute(
            "SELECT conversation_key, title, last_activity_utc, project_label "
            "FROM codex_conversation_rollups WHERE title LIKE ?", (like,)):
        hits.append(
            {"conversation_key": ck, "item_key": None, "title": title,
             "snippet": _excerpt(title), "badges": ["title"],
             "last_activity_utc": last_act, "project_label": project_label})
    return hits


def _search_files(conn: sqlite3.Connection, query: str) -> list[dict]:
    """File-touch search — matches file paths, collapsed to the owning message's
    canonical item_key (§6.2)."""
    like = f"%{query}%"
    pos_cache: dict[str, dict] = {}
    fields_cache: dict[str, tuple] = {}
    collapsed: dict[tuple, dict] = {}
    for ck, message_id, file_path in conn.execute(
        "SELECT t.conversation_key, t.message_id, t.file_path "
        "FROM codex_conversation_file_touches t WHERE t.file_path LIKE ?", (like,),
    ):
        member = conn.execute(
            "SELECT source_path, line_offset FROM codex_conversation_messages WHERE id = ?",
            (message_id,)).fetchone()
        if member is None:
            continue
        if ck not in pos_cache:
            pos_cache[ck] = _pos_to_item_key(conn, ck)
            fields_cache[ck] = _conversation_hit_fields(conn, ck)
        item_key = pos_cache[ck].get((member[0], member[1]))
        title, last_act, project_label = fields_cache[ck]
        hit = collapsed.setdefault(
            (ck, item_key),
            {"conversation_key": ck, "item_key": item_key, "title": title,
             "snippet": _excerpt(file_path), "badges": ["files"],
             "last_activity_utc": last_act, "project_label": project_label})
    return list(collapsed.values())


def _paginate_hits(hits: list[dict], *, cursor: str | None, limit: int):
    lo = 0
    if cursor is not None:
        cursor_keys = [f'{h["conversation_key"]}\x00{h["item_key"] or ""}' for h in hits]
        if cursor in cursor_keys:
            lo = cursor_keys.index(cursor) + 1
    window = hits[lo:lo + limit] if limit else hits[lo:]
    has_more = (lo + len(window)) < len(hits)
    next_cursor = None
    if window and has_more:
        last = window[-1]
        next_cursor = f'{last["conversation_key"]}\x00{last["item_key"] or ""}'
    return window, {"returned": len(window), "cursor": next_cursor}


def search_codex_conversations(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str = "all",
    effective_speed: str,
    limit: int = 20,
    cursor: str | None = None,
) -> dict:
    """Search envelope (§5.6 / §6.2): kind → column mapping over the FTS path
    (per-kind MATCH + column scope) or the SQL-bounded LIKE mirror (single
    substring). Both paths collapse physical matches to ``item_key`` before
    totals/badges/pagination, so mirror rows never double-count. ``mode`` is
    honest (``fts``/``like``); ``depth`` is ``full`` unconditionally (the Codex
    corpus is born-full). ``query`` is echoed verbatim. Pending status while
    migration 025 has not run.

    ``effective_speed`` is accepted for signature parity across the kernels;
    search does not price (results are navigation, not cost).
    """
    del effective_speed  # search does not price
    mode = _search_mode(conn)
    if not codex_normalization_authoritative(conn):
        return {"status": "normalization_pending", "query": query, "hits": [],
                "total": 0, "mode": mode, "depth": "full"}
    if kind not in CODEX_SEARCH_KINDS:
        kind = "all"
    if kind == "title":
        hits = _search_title(conn, query)
    elif kind == "files":
        hits = _search_files(conn, query)
    else:
        hits = _collapse_message_hits(
            conn, _matched_message_rows(conn, query, kind, mode))
    hits.sort(key=lambda h: (h["conversation_key"], h["item_key"] or ""))
    total = len(hits)
    page_hits, page = _paginate_hits(hits, cursor=cursor, limit=limit)
    return {
        "status": "ok", "query": query, "hits": page_hits, "total": total,
        "mode": mode, "depth": "full", "page": page,
    }


# ── in-conversation find (§3.1) ───────────────────────────────────────────────

# Claude cap parity: the anchor list caps at 500 (bin/_lib_conversation_query.py
# ::_FIND_ANCHOR_CAP), with anchors_truncated when more anchors exist pre-cap.
_CODEX_FIND_ANCHOR_CAP = 500
# Bound the regex/case Python scan (ReDoS/perf), mirroring the Claude find guard.
_CODEX_FIND_REGEX_MAX_LEN = 1000
_CODEX_FIND_SCAN_TEXT_CAP = 200_000

# Per-kind (column, badge-label) probes over the normalized message columns, and
# the per-kind row-kind filter. ``text`` maps to the synthetic ``prose`` label so a
# prose-only match anchors a turn but never badges (Claude find parity).
_CODEX_FIND_COLUMNS = {
    "all": (("text", "prose"), ("search_tool", "tool"),
            ("search_thinking", "thinking")),
    "prompts": (("text", "prose"),),
    "assistant": (("text", "prose"),),
    "tools": (("search_tool", "tool"),),
    "thinking": (("search_thinking", "thinking"),),
}
_CODEX_FIND_ROWKIND = {"prompts": "user", "assistant": "assistant"}


def _codex_find_matched_fts(conn, conversation_key, query, cols, rowkind):
    """``{(source_path, line_offset) -> {labels}}`` for one conversation's rows
    matching ``query`` via the FTS path (per-column MATCH, conversation-scoped)."""
    out: dict[tuple, set] = {}
    for col, label in cols:
        fts_query = _fts_query(query, col)
        rk_pred = " AND m.kind = ?" if rowkind else ""
        rk_args = (rowkind,) if rowkind else ()
        rows = conn.execute(
            "SELECT m.source_path, m.line_offset FROM codex_conversation_fts f "
            "JOIN codex_conversation_messages m ON m.id = f.rowid "
            "WHERE f.codex_conversation_fts MATCH ? AND m.conversation_key = ?" + rk_pred,
            (fts_query, conversation_key, *rk_args)).fetchall()
        for sp, lo in rows:
            out.setdefault((sp, lo), set()).add(label)
    return out


def _codex_find_matched_like(conn, conversation_key, query, cols, rowkind):
    """LIKE mirror of ``_codex_find_matched_fts`` — single contiguous substring,
    conversation-scoped. Plain ``%query%`` (matching the Codex search kernel, which
    does not ESCAPE), so find and search stay consistent for one provider."""
    like = f"%{query}%"
    out: dict[tuple, set] = {}
    for col, label in cols:
        rk_pred = " AND kind = ?" if rowkind else ""
        rk_args = (rowkind,) if rowkind else ()
        rows = conn.execute(
            f"SELECT source_path, line_offset FROM codex_conversation_messages "
            f"WHERE conversation_key = ? AND {col} LIKE ? AND {col} != ''" + rk_pred,
            (conversation_key, like, *rk_args)).fetchall()
        for sp, lo in rows:
            out.setdefault((sp, lo), set()).add(label)
    return out


def _codex_find_matched_scan(conn, conversation_key, query, cols, rowkind, regex, case):
    """Physical-row regex/case scan over one conversation's normalized columns —
    honest parity with the Claude find scan. Each scanned value is clipped to
    ``_CODEX_FIND_SCAN_TEXT_CAP`` before the predicate. Precondition: ``regex or
    case`` (the FTS/LIKE path owns plain case-insensitive substring)."""
    if regex:
        rx = re.compile(query, 0 if case else re.IGNORECASE)
        pred = lambda text: rx.search(text) is not None
    else:  # case-sensitive substring
        pred = lambda text: query in text
    rk_pred = " AND kind = ?" if rowkind else ""
    rk_args = (rowkind,) if rowkind else ()
    col_list = ", ".join(c for c, _ in cols)
    rows = conn.execute(
        f"SELECT source_path, line_offset, {col_list} FROM codex_conversation_messages "
        f"WHERE conversation_key = ?" + rk_pred,
        (conversation_key, *rk_args)).fetchall()
    out: dict[tuple, set] = {}
    for row in rows:
        sp, lo = row[0], row[1]
        for idx, (_col, label) in enumerate(cols):
            val = row[2 + idx]
            if val and pred(val[:_CODEX_FIND_SCAN_TEXT_CAP]):
                out.setdefault((sp, lo), set()).add(label)
    return out


def find_in_codex_conversation(
    conn: sqlite3.Connection,
    conversation_key: str,
    query: str,
    *,
    kind: str = "all",
    cap: int = _CODEX_FIND_ANCHOR_CAP,
    regex: bool = False,
    case: bool = False,
) -> dict:
    """Document-ordered rendered-item anchors for in-conversation find (§3.1).

    The Codex analogue of ``find_in_conversation``: the SAME kind taxonomy
    (``CODEX_FIND_KINDS`` == Claude ``_FIND_KINDS``), the same result-cap
    semantics, honest FTS-vs-LIKE mode selection (``_search_mode``), and hits
    anchored by ``item_key`` values byte-equal to the ones detail serves — so S8's
    FindBar navigates both providers with one contract. Mirror-paired physical hits
    collapse to their canonical item (via ``_pos_to_item_key``), so a find never
    surfaces a suppressed duplicate detail never renders.

    Status-tagged envelope: ``ok`` | ``normalization_pending`` | ``not_found``.
    ``regex``/``case`` (parity with the Claude find) bypass FTS/LIKE for a bounded
    physical-row scan of the normalized columns; an unknown ``kind`` raises
    ``ValueError`` (the route maps to 400)."""
    if kind not in CODEX_FIND_KINDS:
        raise ValueError(f"unknown kind: {kind}")
    scan = bool(regex or case)
    mode = ("regex" if regex else "like") if scan else _search_mode(conn)
    base = {"status": "ok", "conversation_key": conversation_key, "total": 0,
            "anchors": [], "anchors_truncated": False, "search_depth": "full",
            "kind": kind, "mode": mode}
    if not codex_normalization_authoritative(conn):
        return {**base, "status": "normalization_pending"}
    rows = _load_conversation_rows(conn, conversation_key)
    if not rows:
        return {"status": "not_found", "conversation_key": conversation_key}
    q = (query or "").strip()
    if not q:
        return base
    cols = _CODEX_FIND_COLUMNS[kind]
    rowkind = _CODEX_FIND_ROWKIND.get(kind)
    if scan:
        if len(q) > _CODEX_FIND_REGEX_MAX_LEN:
            return base
        matched = _codex_find_matched_scan(conn, conversation_key, q, cols, rowkind, regex, case)
    elif mode == "fts":
        try:
            matched = _codex_find_matched_fts(conn, conversation_key, q, cols, rowkind)
        except sqlite3.OperationalError:
            mode = "like"
            matched = _codex_find_matched_like(conn, conversation_key, q, cols, rowkind)
    else:
        matched = _codex_find_matched_like(conn, conversation_key, q, cols, rowkind)
    base["mode"] = mode
    if not matched:
        return base
    # Collapse matched physical positions to canonical item_key (mirror-safe), then
    # emit anchors in detail document order.
    # The ORDER must come from the same segment index as the map (#463 S1). It
    # used to be rebuilt by walking `kern.canonical_items` and keying each entry
    # with `_item_key_for_item`, which produces a TURN key; a follower segment's
    # key can never equal one, so every hit past segment 0 of a turn was dropped
    # from the anchor list and from `total`. The FindBar then reported fewer
    # matches than exist and could navigate to none of the missing ones.
    pos_map, order = _pos_to_item_key_and_order(conn, conversation_key)
    by_item: dict[str, set] = {}
    for pos, labels in matched.items():
        item_key = pos_map.get(pos)
        if item_key is None:
            continue
        by_item.setdefault(item_key, set()).update(labels)
    anchors = [
        {"item_key": item_key,
         "match_kinds": sorted(l for l in by_item[item_key] if l != "prose")}
        for item_key in order if item_key in by_item
    ]
    total = len(anchors)
    return {**base, "total": total, "anchors": anchors[:cap],
            "anchors_truncated": total > cap}


# ── prompts spine (§3.2) ──────────────────────────────────────────────────────


def codex_conversation_prompts(conn: sqlite3.Connection, conversation_key: str) -> dict:
    """Prompt-class canonical items → ``{conversation_key, prompts:[{item_key,
    text}]}`` (§3.2) — ``item_key`` where Claude has ``uuid``. Prompt class = the
    same predicate ``derive_title`` uses (a ``prompt`` item, or an un-turned ``user``
    item). Status-tagged: ``ok`` | ``normalization_pending`` | ``not_found``."""
    if not codex_normalization_authoritative(conn):
        return {"status": "normalization_pending",
                "conversation_key": conversation_key, "prompts": []}
    rows = _load_conversation_rows(conn, conversation_key)
    if not rows:
        return {"status": "not_found", "conversation_key": conversation_key}
    kept, _suppressed = kern.pair_mirrors(rows)
    items = kern.canonical_items(kept)
    prompts = []
    for it in items:
        if it["klass"] == "prompt" or (
                it["klass"] == "unturned" and it["anchor_row"].kind == "user"):
            prompts.append({
                "item_key": _item_key_for_item(conversation_key, it),
                "text": it["anchor_row"].text or ""})
    return {"status": "ok", "conversation_key": conversation_key, "prompts": prompts}


# ── payload locate + full re-read (§3.4) ──────────────────────────────────────


def _codex_source_root_path(conn: sqlite3.Connection, source_root_key: str | None):
    """``canonical_root_path`` for a source-root key, or ``None`` when unknown."""
    if not source_root_key:
        return None
    row = conn.execute(
        "SELECT canonical_root_path FROM codex_source_roots WHERE source_root_key = ?",
        (source_root_key,)).fetchone()
    return row[0] if row else None


def _within_root(source_path: str | None, root_path: str | None) -> bool:
    """True iff the ``realpath``-resolved ``source_path`` stays strictly inside the
    ``realpath``-resolved ``root_path`` (§3.4 containment guard). A symlink escaping
    the canonical root resolves outside and fails; a miss is a 404, never a read."""
    if not source_path or not root_path:
        return False
    try:
        real_file = os.path.realpath(source_path)
        real_root = os.path.realpath(root_path)
        return os.path.commonpath([real_file, real_root]) == real_root
    except (OSError, ValueError):
        return False


def _reread_codex_full_content(conn: sqlite3.Connection, row):
    """Re-read the physical line at ``(row.source_path, row.line_offset)``, validate
    it against the stored ``codex_conversation_events.payload_json`` for that exact
    position (§3.4 structural gone-check — the canonical FULL record, not
    ``content_digest``, which hashes only extracted text and misses a structural
    mutation such as a changed ``call_id``), and return ``(full_content, truncated)``
    for the row's normalized side, or ``None`` when gone (missing file, truncation
    below the stored offset, or a canonical-record mismatch).

    The full pre-cap content is re-derived through the SAME ``_extract`` the
    normalizer uses — which is how payload serves content beyond the normalized
    ``CODEX_TEXT_CAP``. Truncation/``truncated`` is against ``_FULL_PAYLOAD_CEILING``
    (1,000,000 Python characters), the same ceiling the Claude payload path uses."""
    stored = conn.execute(
        "SELECT payload_json FROM codex_conversation_events "
        "WHERE source_path = ? AND line_offset = ?",
        (row.source_path, row.line_offset)).fetchone()
    if stored is None:
        return None
    try:
        with open(row.source_path, "rb") as fh:
            fh.seek(row.line_offset)
            line = fh.readline()
    except OSError:
        return None
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    try:
        if kern._canonical_json(obj) != stored[0]:
            return None
    except (TypeError, ValueError):
        return None
    record_type = obj.get("type") or obj.get("record_type")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    extracted = kern._extract(record_type, payload)
    if extracted is None:
        return None
    card = None
    if record_type == "event_msg" and payload.get("type") in {
            "patch_apply_end", "web_search_end", "mcp_tool_call_end",
            "task_started", "task_complete"}:
        content = kern._canonical_json(payload)
        card = (kern.decode_patch_event_card(
            payload, text_cap=_FULL_PAYLOAD_CEILING)
            or kern.decode_secondary_event_card(
                payload, text_cap=_FULL_PAYLOAD_CEILING))
    else:
        content = (extracted.identity_text if extracted.identity_text is not None
                   else extracted.content_text or "")
        if record_type == "response_item" and payload.get("type") in kern._RESPONSE_TOOL_CALLS:
            card = (kern.decode_tool_call_card(
                payload, text_cap=_FULL_PAYLOAD_CEILING)
                or kern.decode_secondary_tool_call_card(
                    payload, text_cap=_FULL_PAYLOAD_CEILING))
        elif record_type == "response_item" and payload.get("type") in kern._RESPONSE_TOOL_OUTPUTS:
            shaped = kern.decode_tool_output_card(
                payload, text_cap=_FULL_PAYLOAD_CEILING)
            card = shaped[0] if shaped is not None else None
    truncated = len(content) > _FULL_PAYLOAD_CEILING
    return content[:_FULL_PAYLOAD_CEILING], truncated, card


def _locate_payload_block(conn: sqlite3.Connection, conversation_key: str, block_key: str):
    """``(call_row, output_row_or_None)`` for the tool_call block addressed by
    ``block_key`` (§3.4), or ``None`` when no block matches. The output partner
    follows EXACTLY ``_item_blocks_with_rows``' folding rule (same canonical item,
    unique nonempty ``call_id``, call precedes output)."""
    rows = _load_conversation_rows(conn, conversation_key)
    if not rows:
        return None
    # Event/marker payload keys are row-scoped and remain addressable even when
    # their bounded presentation folds into a proven owning interaction.
    for row in rows:
        detail = _parse_detail(row.detail_json)
        payload_addressable = (
            row.kind == "event" and row.event_type in {
                "patch_apply_end", "web_search_end", "mcp_tool_call_end",
                "task_started", "task_complete"}
            or isinstance(detail, dict) and bool(detail.get("markers"))
        )
        if payload_addressable and _block_key_for_row(row) == block_key:
            return {"event": row}
    kept, _suppressed = kern.pair_mirrors(rows)
    items = kern.canonical_items(kept)
    payloads = _load_row_payloads(conn, conversation_key)
    for item in items:
        for _block, call_row, output_row in _item_blocks_with_rows(item, payloads):
            if call_row.kind != "tool_call":
                continue
            if _block_key_for_row(call_row) == block_key:
                return {"call": call_row, "output": output_row}
    return None


def read_codex_payload(
    conn: sqlite3.Connection, conversation_key: str, block_key: str, which: str
) -> dict:
    """Locate + full re-read for a Codex detail payload block (§3.4).

    Selector: ``block_key`` (required) + ``which ∈ {call, output, event}``. A call-id-less
    (or unpaired) call is call-only — ``which=output`` for it → ``not_found`` (no
    adjacency pairing is introduced). Success envelope (pinned):
    ``{"status":"ok","block_key","which","content","truncated"}`` where ``content``
    is the selected side's full text from the re-read record, patch events add a
    full bounded ``card``, and ``truncated`` reflects ``_FULL_PAYLOAD_CEILING``.
    ``gone`` (→ HTTP 410) means the physical
    record moved/mutated; ``not_found`` (→ 404) means no such block, no output
    partner, or a containment miss (a read is never attempted outside the root)."""
    miss = {"status": "not_found", "block_key": block_key, "which": which}
    if which not in ("call", "output", "event"):
        return miss
    located = _locate_payload_block(conn, conversation_key, block_key)
    if located is None:
        return miss
    target = located.get(which)
    if target is None:  # which=output for a call-id-less / unpaired call
        return miss
    # Containment guard (Codex-only; the Claude path has no equivalent) BEFORE any
    # read: a symlink/traversal escaping the canonical root is a 404, never a read.
    root_path = _codex_source_root_path(conn, target.source_root_key)
    if not _within_root(target.source_path, root_path):
        return miss
    outcome = _reread_codex_full_content(conn, target)
    if outcome is None:
        return {"status": "gone", "block_key": block_key, "which": which}
    content, truncated, card = outcome
    response = {"status": "ok", "block_key": block_key, "which": which,
                "content": content, "truncated": truncated}
    if card is not None:
        response["card"] = card
    return response


# ── whole-conversation export (§3.3) ──────────────────────────────────────────


def get_codex_conversation_export(
    conn: sqlite3.Connection, conversation_key: str, *, effective_speed: str
) -> dict:
    """Whole-conversation Markdown export envelope (§3.3). Assembles the full
    detail with NO pagination (``limit=0``), then renders via the pure Codex export
    module. Status-tagged: ``ok`` (carries ``markdown``) | ``normalization_pending``
    | ``not_found`` — the dispatch/transport layers map those to bytes/HTTP."""
    detail = get_codex_conversation(
        conn, conversation_key, effective_speed=effective_speed, limit=0,
        legacy_export=True)
    if detail.get("status") != "ok":
        return {"status": detail.get("status"), "conversation_key": conversation_key}
    from _lib_codex_conversation_export import render_codex_conversation_markdown
    return {"status": "ok", "conversation_key": conversation_key,
            "markdown": render_codex_conversation_markdown(detail)}
