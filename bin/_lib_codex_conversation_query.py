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
``list_codex_conversations``, ``list_codex_conversation_facets``,
``search_codex_conversations``,
``CODEX_SEARCH_KINDS``.
"""
from __future__ import annotations

import base64
import binascii
import collections
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
from collections import deque

import _lib_codex_conversation as kern
import _lib_codex_landmarks as landmarks
import _lib_codex_segments as segkern
from _lib_codex_find_projection import (
    CODEX_FIND_PROJECTION_VERSION,
    ProjectedLeaf,
    RenderLeaf,
    iter_literal_ranges,
    iter_regex_ranges,
    literal_ranges,
    project_context,
    project_markdown,
    project_plain,
    regex_ranges,
    slice_range_to_leaves,
)
from _lib_codex_title_clean import clean_codex_title
from _lib_conversation import _strip_ansi
from _lib_conversation_query import (
    _FULL_PAYLOAD_CEILING, _first_nonblank_line, _parse_outline_ts)
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


def _iter_row_payloads(
    conn: sqlite3.Connection, conversation_key: str, positions=None,
):
    """Yield ``(position, record_type, payload)`` for retained physical payloads.

    ONE copy of the event-table read: the per-source-path chunking, the bound
    ``IN (…)`` construction, the defensive re-filter and the
    ``{"payload": …}`` unwrap. ``_load_row_payloads`` collects this into a dict
    and ``_derive_outline_events`` consumes it one row at a time, which is the
    only difference between them — duplicating the read to get the streaming
    form would mean a later correction to the unwrap or the chunking has to be
    made twice, and the second is easy to miss.

    ``positions`` scopes the read to a known set (#463 S1, Phase C). Passing
    ``None`` reads the whole conversation, which the export path still wants and
    which #463 S4 §4.1 forbids on the outline route.

    A row whose ``payload_json`` does not parse, or whose ``payload`` member is
    not an object, yields nothing — the caller therefore sees no entry for that
    position rather than an empty one.
    """
    def _batches():
        """One cursor per bound chunk, opened only when its turn comes."""
        if positions is None:
            yield conn.execute(
                "SELECT source_path,line_offset,record_type,payload_json "
                "FROM codex_conversation_events WHERE conversation_key = ?",
                (conversation_key,)), None
            return
        scope = set(positions)
        for path, chunks in _chunk_positions(scope).items():
            for chunk in chunks:
                yield conn.execute(
                    "SELECT source_path,line_offset,record_type,payload_json "
                    "FROM codex_conversation_events "
                    "WHERE conversation_key = ? AND source_path = ? "
                    f"AND line_offset IN ({','.join('?' for _ in chunk)})",
                    (conversation_key, path, *chunk)), scope

    for cursor, wanted in _batches():
        for source_path, line_offset, record_type, payload_json in cursor:
            position = (source_path, line_offset)
            if wanted is not None and position not in wanted:
                continue
            try:
                obj = json.loads(payload_json or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            payload = obj.get("payload") if isinstance(obj, dict) else None
            if isinstance(payload, dict):
                yield position, record_type, payload


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
    return {
        position: (record_type, payload)
        for position, record_type, payload
        in _iter_row_payloads(conn, conversation_key, positions)
    }


def _row_payload(row, payloads: dict) -> tuple[str | None, dict] | None:
    return payloads.get((row.source_path, row.line_offset))


# The three lifecycle events that carry an outcome the §6.4 classification reads.
# A `tool_output` row is the fourth source and is selected by `kind`, not by
# event type, because it is a response item rather than an event.
_S4_OUTCOME_EVENTS = frozenset(
    {"patch_apply_end", "web_search_end", "mcp_tool_call_end"})


# ── the outline derivation cache (#463 S4, D2's fallback) ────────────────────
#
# Task 11's re-measurement breached §4.7's first ceiling. On the heaviest
# production conversation the warm outline costs 332 ms, of which the event
# payload pass is 242 ms, against 229 ms for the detail page that opens beside
# it — so the outline HAD become the critical path on open, and the route is
# refetched on every live-tail growth push. §4.7 says a breach escalates to D2's
# fallback inside the session rather than deferring it, and this is that.
#
# **Why a position-keyed cache is sound.** A derivation entry is keyed by
# ``(source_path, line_offset)`` — a byte offset into an append-only rollout
# file. A line at a given offset is immutable, so the verdict for a position
# cannot change while the row exists, and a re-ingest rewrites the same bytes at
# the same offsets. The cache therefore EXTENDS on append rather than only
# hitting or missing: a growth push decodes the newly-in-scope positions and
# reuses every earlier one.
#
# **What the watermark is for.** Immutability covers append; it does not cover
# DELETION, and a deleted event row is exactly the case where the outline must
# stop reporting a verdict it can no longer support. The stored watermark is the
# cached prefix's ``(row count, max id)``, and reuse requires the count of rows
# still at ``id <= max id`` to equal it. Ids are ``AUTOINCREMENT`` and only
# increase, so no later insert can land inside that prefix — a preserved count
# therefore proves no row of the prefix was deleted. The check is one covering
# index read, measured at 0.18 ms on a conversation with 5,950 event rows.
#
# **Absence stays absence.** A position that was in scope and produced no entry
# — payload gone, unparseable, or a shape no decoder recognises — is recorded as
# covered and is not retried. That is deliberate: ``_outline_error_count`` reads
# absence as the third state "could not classify", and a payload does not appear
# later at an offset it was missing from.
#
# In-process only, so no cross-version staleness is possible: the binary that
# filled an entry is the binary that reads it.
_OUTLINE_DERIVATION_CACHE_MAX = 4
_outline_derivation_cache: "collections.OrderedDict[str, dict]" = (
    collections.OrderedDict())
_outline_derivation_lock = threading.Lock()


def reset_outline_derivation_cache() -> None:
    """Drop every cached derivation. For tests and for measurement runs."""
    with _outline_derivation_lock:
        _outline_derivation_cache.clear()


def _outline_event_watermark(
    conn: sqlite3.Connection, conversation_key: str, prefix_max_id: int | None,
) -> tuple[int, int | None, int]:
    """``(row count, max id, rows still at id <= prefix_max_id)`` in one read."""
    count, max_id, prefix = conn.execute(
        "SELECT COUNT(*), MAX(id), COALESCE(SUM(id <= ?), 0) "
        "FROM codex_conversation_events WHERE conversation_key = ?",
        (prefix_max_id if prefix_max_id is not None else -1, conversation_key),
    ).fetchone()
    return count, max_id, prefix


def _derive_outline_events(
    conn: sqlite3.Connection, conversation_key: str, rows: list,
) -> landmarks.EventDerivation:
    """The outline's read-time pass over the retained payloads (#463 S4, §4.1).

    SCOPED and STREAMING, and Task 1 measured both halves of that on the
    production store rather than assuming them.

    *Scoped*: the position set comes from ``rows`` — the wide
    ``codex_conversation_messages`` read the outline has already performed — so
    the pass never selects an event row no landmark can come from. That is the
    idiom ``get_codex_conversation`` already uses two call sites above.
    ``positions=None`` would decode every event row of the conversation, which on
    the heaviest one is 92.1 MB of JSON. Measured, scoping is worth 24% of that
    read (109-122 ms and 183.1 MB of Python heap against 160 ms and 211.7 MB) —
    real, but far less than the spec's framing implies, because 93.7% of that
    conversation's payload bytes ARE the rows S4 needs. Do not treat scoping as
    having solved the cost.

    *Streaming*: each payload is decoded to the small fact the outline wants and
    then dropped, rather than being retained in a dict the way
    ``_load_row_payloads`` retains it. Both go through the one
    ``_iter_row_payloads`` read; retaining is the only thing that differs.
    Measured like for like — load AND decode,
    to completion — the retaining form costs 254 ms and 182.9 MB of Python heap
    on the heaviest conversation and this one costs 232 ms and 22.7 MB. That is
    9% faster and 8x less memory, and peak RSS is in scope for §4.7's gate
    precisely because this route is refetched on every live-tail growth push
    rather than once per open.

    Read-time only. Every decoder call takes ``for_storage=False`` and nothing
    here is written back.
    """
    outcomes: dict[tuple[str, int], object] = {}
    headings_wanted: dict[tuple[str, int], object] = {}
    for row in rows:
        position = (row.source_path, row.line_offset)
        if row.kind == "tool_output":
            outcomes[position] = "output"
        elif row.kind == "event" and row.event_type in _S4_OUTCOME_EVENTS:
            outcomes[position] = row.event_type
        elif row.kind == "reasoning":
            # The same stored-detail gate `_reasoning_headings` applies, so the
            # outline's headings are the set the reader route publishes and not
            # a second, wider one.
            detail = _parse_detail(row.detail_json)
            if isinstance(detail, dict) and isinstance(detail.get("reasoning"), dict):
                headings_wanted[position] = True
    wanted = set(outcomes) | set(headings_wanted)
    derivation = landmarks.EventDerivation()
    if not wanted:
        return derivation

    with _outline_derivation_lock:
        entry = _outline_derivation_cache.get(conversation_key)
    count, max_id, prefix = _outline_event_watermark(
        conn, conversation_key, entry["max_id"] if entry else None)
    reusable = (entry is not None
                and entry["count"] == prefix
                and count >= entry["count"])
    covered: set = set()
    if reusable:
        covered = entry["covered"]
        derivation.errors_by_position.update(entry["derivation"].errors_by_position)
        derivation.patch_files_by_position.update(
            entry["derivation"].patch_files_by_position)
        derivation.headings_by_position.update(
            entry["derivation"].headings_by_position)
    missing = wanted - covered

    for position, _record_type, payload in (
            _iter_row_payloads(conn, conversation_key, missing) if missing else ()):
        kind = outcomes.get(position)
        if kind == "output":
            decoded = kern.decode_tool_output_card(payload, for_storage=False)
            if decoded is not None:
                derivation.errors_by_position[position] = (
                    landmarks.classify_tool_failure(
                        {"terminal_output": decoded[0]}))
        elif kind == "patch_apply_end":
            card = kern.decode_patch_event_card(payload, for_storage=False)
            if card is not None:
                derivation.errors_by_position[position] = (
                    landmarks.classify_tool_failure({"patch": card}))
            # Counted from the UNBOUNDED raw `changes`, not off the card above,
            # whose shared 16,000-character budget silently undercounts a large
            # diff (§4.5).
            derivation.patch_files_by_position[position] = (
                landmarks.patch_file_touches(payload))
        elif kind in _S4_OUTCOME_EVENTS:
            card = kern.decode_secondary_event_card(payload)
            if card is not None:
                family = "web" if kind == "web_search_end" else "mcp"
                derivation.errors_by_position[position] = (
                    landmarks.classify_tool_failure(
                        {family: {"completion": card}}))
        if position in headings_wanted:
            texts = landmarks.reasoning_heading_texts(payload)
            if texts:
                derivation.headings_by_position[position] = texts

    # Stored AFTER the pass, so a raising derivation leaves the previous entry
    # rather than a half-filled one. The value is the object just returned: it
    # is never mutated again, because the next extension copies its three maps
    # into a fresh `EventDerivation` above rather than adding to this one.
    with _outline_derivation_lock:
        _outline_derivation_cache[conversation_key] = {
            "count": count, "max_id": max_id,
            "covered": covered | wanted, "derivation": derivation,
        }
        _outline_derivation_cache.move_to_end(conversation_key)
        while len(_outline_derivation_cache) > _OUTLINE_DERIVATION_CACHE_MAX:
            _outline_derivation_cache.popitem(last=False)
    return derivation


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


def _item_model(item: dict) -> str | None:
    """The model a canonical tier-1 item states, or ``None`` (§4.2).

    The anchor row first, then the item's own rows in order. Reading the anchor
    row ALONE under-reported Codex model usage about sixfold: most Codex
    response items anchor on a ``reasoning`` row, which carries no model, so a
    conversation with 13 outline turns holding assistant rows and 82 assistant
    rows in total rendered ``gpt-5.6-sol x2``, and 182 of 200 Codex
    conversations in the production store reported a model total under a third
    of their turn count. §4.2 defines the counting unit as the canonical tier-1
    assistant TURN, mirroring the Claude side, whose histogram sums to exactly
    ``stats.turns.assistant``; the anchor row is one row of that turn.
    """
    anchor = item["anchor_row"].model
    if anchor:
        return anchor
    for row in item["rows"]:
        if row.model:
            return row.model
    return None


def _outline_outcome_positions(rows: list) -> set:
    """Every position whose row could carry an outcome verdict this request."""
    return {(row.source_path, row.line_offset) for row in rows
            if row.kind == "tool_output"
            or (row.kind == "event" and row.event_type in _S4_OUTCOME_EVENTS)}


def _outline_failing_calls(derivation, outcome_positions: set,
                           call_by_position: dict) -> set:
    """The calls this request found failing, charged to the call they fold into.

    Intersected with ``outcome_positions`` because ``errors_by_position`` is
    CACHED: the derivation is retained per conversation and EXTENDED on a
    growth push, so the map can hold verdicts for positions the current request
    never read, while every other consumer indexes it by a current position.
    Defensive rather than a reproduction of an observed miscount.
    """
    return {call_by_position.get(position, position)
            for position in derivation.failing_positions()
            if position in outcome_positions}


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

    #463 S4 — the summary parse itself moved to
    ``_lib_codex_landmarks.reasoning_heading_texts``, so the reader route here
    and the outline's landmark derivation decompose by ONE rule. This function
    keeps the two things the outline does not want: the stored-detail gate, and
    the ``<block_key>#<ordinal>`` identity, which the outline mints from its own
    block keys rather than from the reader's.
    """
    if not isinstance(detail, dict) or not isinstance(detail.get("reasoning"), dict):
        return None
    headings = landmarks.reasoning_heading_texts(payload)
    if not headings:
        return None
    return [{"key": f"{block_key}#{ordinal}", "text": text}
            for ordinal, text in enumerate(headings)]


# ── the conversation-level session index (#463 S3, spec section 3.2) ─────────
#
# Page-local adaptation cannot decide whether a session label is unique across
# the conversation or whether an opener exists, because later pages adapt
# independently and live-tail can append. So the server publishes a bounded
# conversation-scoped index and the client never computes either fact itself.
#
# 870 sessions across 223 conversations, roughly four per conversation, so this
# cap is generous. A conversation past it publishes what fits and marks itself
# truncated rather than publishing a partial map that looks complete.
_SESSION_INDEX_MAX = 64


def _stored_write_stdin_session(detail) -> str | None:
    """The session id a `write_stdin` call names, from its STORED arguments.

    Phase A must not load event payloads, and it does not need to: `detail.args`
    is in the narrow index, and it is the provider's own argument JSON.
    """
    if not isinstance(detail, dict) or detail.get("name") != "write_stdin":
        return None
    args = detail.get("args")
    if not isinstance(args, str) or not args:
        return None
    try:
        parsed = json.loads(args)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    raw = parsed.get("session_id") if isinstance(parsed, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    return str(raw)


def _stored_session_announcement(detail) -> str | None:
    """The session id a tool output ANNOUNCES, from its stored card.

    `Process running with session ID <id>` is the evidence linking a shell
    session to the call that opened it — 698 of 870 sessions, 80.2%. The line is
    read through the same anchored preamble reader the card path uses rather than
    by searching the text, so a user's own output cannot be mistaken for one.

    Spec section 3.2 names `search_tool` as the column this comes from. It is
    read from the stored card in `detail_json` instead, which carries the same
    bytes at the head of its first part and IS in the narrow index —
    `_load_conversation_index_rows` excludes `search_tool` along with the other
    two bulk columns, and adding it back would undo S1's Phase A saving.
    """
    card = detail.get("card") if isinstance(detail, dict) else None
    if not isinstance(card, dict) or card.get("type") != "terminal_output":
        return None
    parts = card.get("parts")
    if not isinstance(parts, list) or not parts:
        return None
    head = parts[0]
    text = head.get("text") if isinstance(head, dict) else None
    if not isinstance(text, str):
        return None
    parsed = kern.parse_harness_preamble(text)
    return parsed[0]["session_announcement"] if parsed is not None else None


def _build_session_index(rows) -> tuple[dict, dict[str, str]]:
    """``(envelope, ordinal_by_provider_session_id)`` over the WHOLE conversation.

    Ordinals are assigned in first-appearance order over the conversation's
    physical row order, so they are stable across pages and live-tail appends and
    no client-side uniqueness decision is made from a partial window.

    The envelope's `sessions` map is keyed by the ordinal in decimal, which is
    exactly what a `session_ref` card's `ref` carries, so the client's lookup is
    direct. Nothing in the envelope is derived from the provider's own session
    id — that token is removed rather than scrubbed (spec section 4.3).
    """
    owners: dict[str, list] = {}
    for row in rows:
        if row.kind == "tool_call" and row.call_id:
            owners.setdefault(row.call_id, []).append(row)
    ordinals: dict[str, int] = {}
    openers: dict[str, str | None] = {}
    truncated = False
    for row in rows:
        session = None
        opener_row = None
        detail = _parse_detail(row.detail_json)
        if row.kind == "tool_call":
            session = _stored_write_stdin_session(detail)
        elif row.kind == "tool_output":
            session = _stored_session_announcement(detail)
            if session is not None:
                # The opener is the CALL that owns the announcing output, not the
                # output row: a uniquely-owned output folds into its call and has
                # no block of its own, so its key would name nothing on the page.
                owning = owners.get(row.call_id or "", [])
                opener_row = owning[0] if len(owning) == 1 else row
        if session is None:
            continue
        if session not in ordinals:
            if len(ordinals) >= _SESSION_INDEX_MAX:
                truncated = True
                continue
            ordinals[session] = len(ordinals) + 1
            openers[session] = None
        if opener_row is not None and openers.get(session) is None:
            openers[session] = _block_key_for_row(opener_row)
    envelope = {
        "sessions": {
            str(ordinal): {"ordinal": ordinal,
                           "opener_block_key": openers.get(session)}
            for session, ordinal in ordinals.items()
        },
        "truncated": truncated,
    }
    return envelope, {session: str(ordinal) for session, ordinal in ordinals.items()}


def _apply_session_ordinals(card, ordinals: dict[str, str]) -> None:
    """Replace every SHELL session reference with its conversation-local ordinal.

    Fails closed: a reference the index does not know becomes ``None`` rather
    than falling back to the provider's id. `cell` scope is left alone — a cell
    id is a small per-conversation sandbox ordinal that identifies nothing
    outside the sandbox, and it is never presented as a shell session.
    """
    if not isinstance(card, dict):
        return
    if card.get("type") == "session_ref" and card.get("scope") == "shell":
        card["ref"] = ordinals.get(card.get("ref"))
        return
    if card.get("type") == "program":
        for entry in card.get("invocations") or []:
            if (isinstance(entry, dict) and entry.get("kind") == "session"
                    and entry.get("scope") == "shell"):
                entry["ref"] = ordinals.get(entry.get("ref"))


def _item_blocks_with_rows(
    item: dict, payloads: dict | None = None, *, preserve_marker_text: bool = False,
    call_owner_count: dict | None = None, decompose_headings: bool = False,
    session_ordinals: dict[str, str] | None = None,
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
        if r.kind == "assistant":
            # #463 S3 section 5.5. Read-time detection over the row's own stored
            # text, so it reaches every historical marker with no payload load.
            # It is written to `external_call` and never to `markers`, which
            # selects export payload hydration.
            external = kern._external_call_from_text(text)
            # Fail closed on the span: it is published as offsets into the very
            # string served as `block["text"]`, and a span that does not resolve
            # would make the client hide the wrong run of prose. `text` is the
            # same object the block below carries, including the
            # `preserve_marker_text` replacement, so the check is against what is
            # actually served rather than against what was read.
            if external is not None and kern.external_call_span_resolves(
                    text, external):
                detail = dict(detail) if isinstance(detail, dict) else {}
                detail["external_call"] = external
        if r.kind == "tool_call" and isinstance(payload, dict):
            card = kern.decode_tool_call_card(payload)
            if card is None:
                card = kern.decode_secondary_tool_call_card(payload)
            if card is not None:
                _apply_session_ordinals(card, session_ordinals or {})
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
    session_ordinals: dict[str, str] | None = None,
) -> list[dict]:
    """Assemble an item's blocks, folding each ``tool_output`` into its
    ``tool_call`` block via ``call_id`` when that call_id has exactly one owner
    (§5.2). Physical order within the item is preserved. Thin projection of
    ``_item_blocks_with_rows`` — the single source of truth for the folding rule."""
    return [entry[0] for entry in _item_blocks_with_rows(
        item, payloads, preserve_marker_text=preserve_marker_text,
        call_owner_count=call_owner_count,
        decompose_headings=decompose_headings,
        session_ordinals=session_ordinals)]


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


def _conversation_totals(
    conn: sqlite3.Connection, conversation_key: str, effective_speed: str,
) -> tuple[float, dict]:
    """Lean priced and token totals over one conversation's accounting rows.

    Unlike ``_attribute_costs``, this does not reconstruct the event-to-turn map:
    callers that need only conversation totals (outline, browse, child summaries)
    can sum the compact accounting rows directly.  The row order and pricing
    primitive stay identical to the detail envelope's whole-conversation pass.
    """
    total = 0.0
    tokens = _zero_tokens()
    for model, inp, cin, out, rout in conn.execute(
        "SELECT model, input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens FROM codex_session_entries WHERE conversation_key = ? "
        "ORDER BY source_path, line_offset",
        (conversation_key,),
    ):
        total += _calculate_codex_entry_cost(
            model or "", inp or 0, cin or 0, out or 0, rout or 0, speed=effective_speed)
        _add_tokens(tokens, inp, out, cin, rout)
    return total, _tokens_union(tokens)


def _conversation_total_cost(conn: sqlite3.Connection, conversation_key: str, effective_speed: str) -> float:
    """Lean priced total for browse rows and child summaries (§5.4)."""
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
    native-thread-id prefix.

    #463 S4 §5 — the stored title is cleaned of recognized harness markup here.
    This is ONE of three read paths that need it, not the universal chokepoint
    the first draft assumed: the outline turn label is built independently from
    anchor-row text and the `kind=title` search path reads rollup titles
    directly, so both clean through the same helper rather than through this
    call. A construct that strips to nothing falls through the chain below on
    its own, which is what makes `strip` expressible at read time at all.
    """
    return (clean_codex_title(fields.get("title"))
            or fields.get("project_label")
            or _short_native(fields.get("native_thread_id")) or "")


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
    fold_groups: bool = False,
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

    ``fold_groups`` publishes the fold-group membership as ``_fold_groups``. Only
    the outline reads it (#463 S4 — a ``tool_error`` landmark anchors on the call
    a failure folds into); the detail route S1 bounded and the search position
    map do not, and building it for them costs a tuple per row per request for a
    value nobody reads.
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
                # #463 S4 — the fold-group membership, as physical positions.
                # `_fold_groups_for_item` computes it payload-free and this
                # index discarded it, so nothing downstream could say WHICH
                # `tool_call` a failing `tool_output` belongs to — only that the
                # segment contained one. A `tool_error` landmark anchors on the
                # call, so the membership has to survive Phase A.
                "_fold_groups": [
                    [(row.source_path, row.line_offset) for row in group.rows]
                    for group in segment.groups
                ] if fold_groups else (),
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
    # #463 S3 section 3.2. Built here, in Phase A, from the narrow index that is
    # already loaded for the whole conversation: it needs no extra payload read
    # and no extra column, and it must be whole-conversation because ordinals and
    # opener presence are global facts a page cannot decide.
    session_index, session_ordinals = _build_session_index(rows)

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
                decompose_headings=not legacy_export,
                session_ordinals=session_ordinals),
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
        "session_index": session_index,
        "children": _children_of(conn, conversation_key, effective_speed),
        "parent": _parent_of(conn, conversation_key),
        "total_cost_usd": total,
        "unattributed_cost_usd": unattributed_cost,
        "tokens": _tokens_union(conv_tokens),
    }


# ── outline assembly (§5.6) ───────────────────────────────────────────────────


def _tool_call_name(row) -> str | None:
    """The tool a ``tool_call`` row invoked, from its STORED detail.

    Ingest writes ``{"name": payload["name"] or <record type>, …}`` for every
    tool call, so this needs no payload and stays outside the scoped pass.
    """
    detail = _parse_detail(row.detail_json)
    name = detail.get("name") if isinstance(detail, dict) else None
    return name if isinstance(name, str) and name else None


def _conversation_duration_seconds(rows) -> int | None:
    """Wall span of the conversation, as MIN to MAX over row timestamps (§4.2).

    Never last minus first. §3.4 records that ``timestamp_utc`` is monotone
    within a turn only — item and segment emission is physical order, not
    timestamp order — and Task 1 found five decreases across turns in the
    corpus, on which the naive form returns a negative duration.

    The outline's own caller cannot exhibit that today, because
    ``_load_conversation_rows`` reads ``ORDER BY timestamp_utc, source_path,
    line_offset`` and the two forms therefore coincide on the rows it passes.
    The rule is stated for the next caller, which is much more likely to hand
    over an item-anchor list.
    """
    stamps = [row.timestamp_utc for row in rows if row.timestamp_utc]
    if not stamps:
        return None
    first = _parse_outline_ts(min(stamps))
    last = _parse_outline_ts(max(stamps))
    if first is None or last is None:
        return None
    return int((last - first).total_seconds())


def _outline_error_count(
    failed_calls: set, outcome_positions: set,
    derivation: landmarks.EventDerivation,
) -> int | None:
    """How many calls failed, or ``None`` when that cannot be answered (D3).

    Three states, because 0 and null are different claims. A conversation with
    no outcome-bearing row at all reports 0: nothing failed because nothing ran,
    and that is determinable. A conversation whose outcome rows produced no
    verdict at all — every retained payload gone, unparseable, or of a shape no
    decoder recognises — reports null, because the stored projection answers
    nothing here (Task 1 measured stored ``is_error`` true for 0 of 63,150
    production ``tool_output`` rows) and 0 would assert an absence nobody proved.

    A PARTIAL read reports what it found rather than declining. The alternative
    would suppress real failures the pass did see because of one unreadable
    neighbour, which is the worse error of the two.
    """
    if not outcome_positions:
        return 0
    if not (outcome_positions & set(derivation.errors_by_position)):
        return None
    return len(failed_calls)


# The Codex tool names that open the `plan` landmark family. Codex's decoded
# plan card is named `update_plan`, and both existing CLIENT plan predicates
# recognise only Claude's `ExitPlanMode` and `AskUserQuestion` — so publishing
# raw Codex tool names into tier-1 `tools` would NOT have made the plan jump
# work, it would have been a silent no-op (§3.2). The mapping is explicit here
# and the outline target derivation reads landmark KINDS rather than inferring
# from names.
_S4_PLAN_TOOLS = frozenset({"update_plan"})


def _landmark_label(row) -> str:
    """What a landmark row says. Never a raw provider identifier (§8).

    §3.6 enumerates exactly TWO sources for a landmark label — reasoning heading
    text, or a tool name — and rests the decision not to scrub these labels on
    that enumeration. So a row that is neither a named ``tool_call`` nor a typed
    ``event`` falls back to its own KIND, which is normalizer vocabulary, and
    never to the row's stored text.

    That branch is reachable: a failing ``tool_output`` whose ``call_id`` is
    owned by two or more ``tool_call`` rows in its turn does not fold, becomes
    its own group head, and enters ``failed_calls`` directly. Its ``text`` column
    is the harness preamble that ``decode_tool_output_card(for_storage=False)``
    exists to remove, and ``test_s3_no_raw_session_id_reaches_any_served_route``
    documents that preamble as carrying the provider ``session_id``.
    """
    if row.kind == "tool_call":
        name = _tool_call_name(row)
        if name is not None:
            return name
    elif row.kind == "event" and row.event_type:
        return row.event_type
    return row.kind or ""


def _clean_outline_label(text: str) -> str:
    """An outline turn label, cleaned, and never cleaned away to nothing (§5.1).

    Unlike ``_display_chain``, which §5.3 leans on as "already a fallback chain"
    when it justifies the ``strip`` disposition, this path has no chain: it
    cleans the anchor row's first non-blank line and publishes the result. Two
    allowlisted grammars can consume the whole string — ``<recommended_plugins>``
    (6 of the census's 438 titles) and ``<command-name>`` with no sibling tag —
    and the client's ``cleanQualifiedTitle(turn.label) ?? turn.label`` passes an
    empty string straight through, so the reader would get a row with no text at
    all. The uncleaned line is the pre-S4 label, which is legible.
    """
    cleaned = clean_codex_title(text)
    return cleaned if cleaned.strip() else text


def _build_landmarks(
    index: list[dict], derivation: landmarks.EventDerivation,
    failed_calls: set,
) -> list[dict]:
    """Tier 2 — the landmarks a jump can reach (§3.2).

    Three kinds and deliberately NOT one entry per tool call: a 523-call turn
    would contribute 523 rows, which is noise rather than navigation.

    Emission is PHYSICAL order — the segment index in order, and each segment's
    rows in order — because §3.4 records that ``timestamp_utc`` is monotone
    within a turn only and no consumer sorts by it.

    ``landmark_key`` is always COMPOUND — ``<block_key>#<discriminator>`` — and
    unique across every kind. A reasoning heading discriminates by its zero-based
    ordinal, which is the identity the reader route already mints for the same
    heading, because one block yields several headings and ``block_key`` alone
    would collide. Every other kind discriminates by the kind itself.

    That is what lets one ``tool_call`` block carry BOTH a ``tool_error`` and a
    ``plan`` landmark. It has to: §3.2 gives the plan kind one entry per plan
    call, and a failed plan call is one, so filing it only as the error made the
    jump cluster's plan family report zero — asserting no plan activity in a
    conversation that has some, which is the claim the spec's own "0 is a claim,
    hiding is not" rule forbids. The error is emitted first, because it is the
    more urgent of the two claims about the same call.

    A block carrying ``detail.external_call`` produces no landmark of any kind
    (§3.2). That holds by construction rather than by a filter here: the marker
    is published on ``assistant`` blocks only, and no kind below comes from an
    assistant row. ``test_external_call_block_produces_no_landmark`` pins it.
    """
    out: list[dict] = []
    for entry in index:
        for row in entry["_rows"]:
            position = (row.source_path, row.line_offset)
            block_key = _block_key_for_row(row)
            common = {
                "block_key": block_key,
                "item_key": entry["item_key"],
                "parent_item_key": entry["turn_item_key"],
                "timestamp_utc": row.timestamp_utc,
            }
            if row.kind == "reasoning":
                for ordinal, text in enumerate(
                        derivation.headings_by_position.get(position, ())):
                    out.append({"landmark_key": f"{block_key}#{ordinal}",
                                "kind": "reasoning", "label": text, **common})
                continue
            if position in failed_calls:
                out.append({"landmark_key": f"{block_key}#tool_error",
                            "kind": "tool_error",
                            "label": _landmark_label(row), **common})
            if (row.kind == "tool_call"
                    and _tool_call_name(row) in _S4_PLAN_TOOLS):
                out.append({"landmark_key": f"{block_key}#plan", "kind": "plan",
                            "label": _landmark_label(row), **common})
    return out


# The literal ingest writes into `codex_conversation_file_touches.tool`, kept so
# the outline wire field and the file-search projection mean the same thing.
# Every touch S4 derives comes from a `patch_apply_end`, which is the completion
# of an `apply_patch` call.
_PATCH_TOUCH_TOOL = "apply_patch"


def _conversation_files(
    segment_index: list[dict], derivation: landmarks.EventDerivation,
) -> list[dict]:
    """The whole-conversation file list, DERIVED read-time (§1.2, §4.3).

    The stored ``codex_conversation_file_touches`` table is the source for
    cross-conversation ``kind=files`` search after #489 repaired dict-shaped
    ingest and backfilled retained history. It is deliberately not the outline
    source: this payload pass has the richer evidence the outline contract needs.

    Deriving it instead buys three things the table could not have supplied: a
    real segment anchor per touch, so a file row jumps to its change rather than
    to the top of a turn; the true per-file count; and first-touch DOCUMENT
    order, which is what ``OutlineFile`` has always promised while the SQL
    ordered alphabetically by path.

    ``added``/``removed`` are summed over the touches, and go ``None`` as soon as
    ONE touch of that file cannot be counted. Summing only the countable touches
    would publish a number for a file that changed more — a file edited once with
    a real diff and then moved by a count-free ``update`` would report the first
    figure — and nothing in ``touches[]`` marks such a total as partial. §4.5 is
    explicit that an undeterminable count is null and the badge renders nothing
    rather than an understated number; that rule has to reach the aggregate, not
    only the individual touch. The per-touch counts themselves come from the
    UNBOUNDED raw ``changes`` entry (§4.5) — see ``landmarks.patch_file_touches``.
    """
    files: dict[str, dict] = {}
    undetermined: dict[str, set[str]] = {}
    for entry in segment_index:
        for row in entry["_rows"]:
            position = (row.source_path, row.line_offset)
            for touch in derivation.patch_files_by_position.get(position, ()):
                record = files.get(touch["path"])
                if record is None:
                    record = files[touch["path"]] = {
                        "file_path": touch["path"], "tool": _PATCH_TOUCH_TOOL,
                        "count": 0, "touches": [],
                        "added": None, "removed": None}
                record["count"] += 1
                record["touches"].append({
                    "item_key": entry["item_key"],
                    "timestamp_utc": row.timestamp_utc,
                    # The raw change KIND — `add`/`delete`/`update` from the dict
                    # shape, `modified` from the list one — never the tool name.
                    "op": touch["op"],
                })
                for field in ("added", "removed"):
                    if touch[field] is None:
                        undetermined.setdefault(touch["path"], set()).add(field)
                    else:
                        record[field] = (record[field] or 0) + touch[field]
    for path, fields in undetermined.items():
        for field in fields:
            files[path][field] = None
    return list(files.values())


# Connections whose read snapshot THIS module opened, by identity. A
# ``sqlite3.Connection`` supports neither attribute assignment nor a weak
# reference, so ownership cannot be recorded on the object; ``id`` is unique
# among live objects and the connection is alive for the whole ``with`` body, so
# the token cannot be confused with another connection's while it is registered.
_OWNED_READ_SNAPSHOTS: set[int] = set()


@contextlib.contextmanager
def _read_snapshot(conn: sqlite3.Connection):
    """One consistent read snapshot across a multi-query envelope (#463 S4 §4.1).

    The outline route uses one connection but opened no explicit read
    transaction, so its several queries each took their own snapshot. Concurrent
    APPEND is benign there — extra raw event rows have no normalized mapping yet
    — but a concurrent delete or truncation between the wide message read and the
    payload read can expose a message row whose payload is already gone, and the
    derivation would then report an absence that never existed.

    A deferred ``BEGIN`` takes the snapshot on the first read and holds it for
    every later one. It is released with ``rollback``, which is the honest end of
    a transaction that wrote nothing.

    **A transaction this module did not open is refused, not inherited.**
    ``conn.in_transaction`` is true for an outer WRITE transaction exactly as it
    is for an outer read snapshot, and Python's ``sqlite3`` exposes no
    ``txn_state``, so the two cannot be told apart here. Only one of them is safe
    to borrow: inside a write, the envelope would read that writer's uncommitted
    and possibly half-applied state — a message row whose events are already
    deleted — with no snapshot of its own and no way to notice. Treating both
    alike is silent; refusing is not. A caller that wants several envelopes on
    one snapshot opens it through this same helper, which nests without issuing
    the second ``BEGIN`` SQLite would refuse.

    The caller sweep behind that decision, pinned by
    ``test_every_outline_caller_arrives_outside_a_transaction``: the three call
    paths into ``get_codex_conversation_outline`` are
    ``_lib_conversation_dispatch.neutral_outline`` (the dashboard route, on a
    connection ``open_conversations_db`` returns fresh per request and closes
    after), ``bin/build-codex-reader-fixtures.py``, and the tests. None holds a
    transaction at the call.
    """
    token = id(conn)
    if token in _OWNED_READ_SNAPSHOTS:
        yield
        return
    if conn.in_transaction:
        raise RuntimeError(
            "this envelope needs its own read snapshot, and the connection is "
            "already inside a transaction it did not open; wrap the outer "
            "scope in _read_snapshot instead")
    conn.execute("BEGIN")
    _OWNED_READ_SNAPSHOTS.add(token)
    try:
        yield
    finally:
        _OWNED_READ_SNAPSHOTS.discard(token)
        conn.rollback()


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

    #463 S4 — the route now makes TWO conversation reads under one snapshot: the
    wide message read below, and a scoped read-time pass over the retained event
    payloads (``_derive_outline_events``) whose position set comes from that
    first read. The pass is what gives the outline a failure verdict per call,
    the authored reasoning headings, and the per-file patch touches; §1.2 records
    why the stored ``codex_conversation_file_touches`` search projection is not
    the OUTLINE source, and Task 1 measured that the stored card carries 0 of the
    corpus's 896 tool failures, so read-time is not a preference here.
    """
    with _read_snapshot(conn):
        return _outline_envelope(
            conn, conversation_key, effective_speed=effective_speed)


def _outline_envelope(
    conn: sqlite3.Connection, conversation_key: str, *, effective_speed: str
) -> dict:
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
    fold_groups: list[list[tuple[str, int]]] = []
    segment_index = _build_segment_index(
        conversation_key, items, detail_bytes, segmented=True, fold_groups=True)
    for entry in segment_index:
        segment_keys.setdefault(entry["_item_index"], []).append(entry["item_key"])
        fold_groups.extend(entry["_fold_groups"])
    # `rows` here is the wide read directly above, and that is what makes the
    # payload pass SCOPED rather than a second whole-conversation decode (§4.1).
    derivation = _derive_outline_events(conn, conversation_key, rows)
    call_by_position = landmarks.fold_owner_by_position(fold_groups)
    outcome_positions = _outline_outcome_positions(rows)
    # A failing outcome row is charged to the `tool_call` it folds into, so the
    # same failure cannot be counted twice when a call and its output both carry
    # one, and so a turn's `tools` entry can say WHICH call failed.
    failed_calls = _outline_failing_calls(
        derivation, outcome_positions, call_by_position)
    turns: list[dict] = []
    kind_totals: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    models: dict[str, int] = {}
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
            # Built from anchor-row TEXT, which is why it does not reach
            # `_display_chain` and has to clean through the shared helper here
            # (§5.1). A label with no recognized markup passes through byte for
            # byte, so this cannot move an ordinary prose label.
            label = _clean_outline_label(
                _first_nonblank_line(_strip_ansi(anchor_text))) if anchor_text else ""
        kinds: dict[str, int] = {}
        tools: list[dict] = []
        tool_slot: dict[str | None, int] = {}
        tool_call_count = 0
        first_failure_name: str | None = None
        thinking: list[str] = []
        for r in it["rows"]:
            kinds[r.kind] = kinds.get(r.kind, 0) + 1
            kind_totals[r.kind] = kind_totals.get(r.kind, 0) + 1
            position = (r.source_path, r.line_offset)
            if r.kind == "tool_call":
                tool_call_count += 1
                name = _tool_call_name(r)
                failed = position in failed_calls
                if failed and first_failure_name is None:
                    first_failure_name = name
                if name is not None:
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                slot = tool_slot.get(name)
                if slot is None:
                    tool_slot[name] = len(tools)
                    tools.append({"name": name, "is_error": failed})
                elif failed:
                    tools[slot]["is_error"] = True
            elif r.kind == "reasoning":
                thinking.extend(derivation.headings_by_position.get(position, ()))
        item_key = _item_key_for_item(conversation_key, it)
        turn = {
            "item_key": item_key,
            "member_item_keys": _member_item_keys(conversation_key, it),
            "segment_item_keys": segment_keys.get(index, [item_key]),
            "label": label,
            "timestamp_utc": it["anchor_row"].timestamp_utc,
            "kinds": kinds,
        }
        # Additive, and only where there is something to say: a turn with no
        # calls publishes neither an empty array nor a zero count, matching how
        # the Claude outline omits `tools` and `thinking`.
        if tools:
            turn["tools"] = tools
            turn["tool_call_count"] = tool_call_count
            turn["first_failure_name"] = first_failure_name
        if thinking:
            turn["thinking"] = thinking
        item_model = _item_model(it) if _item_kind(it) == "assistant" else None
        if item_model:
            turn["model"] = item_model
            models[item_model] = models.get(item_model, 0) + 1
        if meta is not None:
            turn.update(meta)
        turns.append(turn)
    total_cost, tokens = _conversation_totals(
        conn, conversation_key, effective_speed)
    return {
        "status": "ok",
        "conversation_key": conversation_key,
        "turns": turns,
        # Tier 2, deliberately a SEPARATE array (§3.3): `adaptQualifiedOutline`
        # derives `stats.turns.{human,assistant,tool_result,meta}` by filtering
        # `turns[]` on kind, so putting landmarks there would inflate counts
        # meant to describe the conversation's structure.
        "landmarks": _build_landmarks(
            segment_index, derivation, failed_calls),
        "stats": {
            "items": len(items), "kinds": kind_totals,
            "cost_usd": total_cost, "tokens": tokens,
            "tool_counts": tool_counts, "models": models,
            "duration_seconds": _conversation_duration_seconds(rows),
            "error_count": _outline_error_count(
                failed_calls, outcome_positions, derivation),
        },
        "files": _conversation_files(segment_index, derivation),
        "children": _children_of(conn, conversation_key, effective_speed),
    }


# ── browse (§6.1) ─────────────────────────────────────────────────────────────


def _is_fork(fields: dict) -> bool:
    parent = fields.get("parent_thread_id")
    return bool(parent) and parent != fields.get("native_thread_id")


def _browse_row(conn: sqlite3.Connection, conversation_key: str, effective_speed: str, fields: dict) -> dict:
    return _browse_row_from_fields(
        conversation_key, fields,
        cost_usd=_conversation_total_cost(conn, conversation_key, effective_speed),
        parent=_parent_of(conn, conversation_key),
    )


def _browse_row_from_fields(
    conversation_key: str, fields: dict, *, cost_usd: float, parent,
) -> dict:
    return {
        "conversation_key": conversation_key,
        "title": _display_chain(fields),
        "project_key": fields["project_key"],
        "project_label": fields["project_label"],
        "started_utc": fields["started"],
        "last_activity_utc": fields["last"],
        "count": fields["item_count"],
        "cost_usd": cost_usd,
        "models": list(fields["models"]),
        "parent": parent,
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


def _stored_rollups_present(conn: sqlite3.Connection) -> bool:
    """Whether the authoritative stored-rollup branch has been materialized.

    Normal writes update normalized rows and their rollups in one transaction.
    The only supported no-rollup state is the pre-first-recompute window, where
    the live branch keeps the rail available.  This constant-time probe avoids
    reintroducing the whole-message-table DISTINCT scan on every cold browse.
    """
    return conn.execute(
        "SELECT 1 FROM codex_conversation_rollups LIMIT 1").fetchone() is not None


def _live_browse_fields(conn: sqlite3.Connection) -> list[tuple[str, dict]]:
    """Live-recompute fallback used only while no stored rollups exist."""
    out = []
    for (conversation_key,) in conn.execute(
            "SELECT DISTINCT conversation_key FROM codex_conversation_messages"):
        fields = _rollup_fields(conn, conversation_key)
        if fields is not None:
            out.append((conversation_key, fields))
    return out


def _facets_from_fields(fields_rows: list[tuple[str, dict]]) -> dict:
    return _browse_facets([
        {
            "project_key": fields["project_key"],
            "project_label": fields["project_label"],
            "models": list(fields["models"]),
        }
        for _conversation_key, fields in fields_rows
    ])


def _stored_browse_facets(conn: sqlite3.Connection) -> dict:
    fields_rows = []
    for conversation_key, project_key, project_label, models_json in conn.execute(
        "SELECT conversation_key, project_key, project_label, models_json "
        "FROM codex_conversation_rollups"
    ):
        try:
            parsed = json.loads(models_json) if models_json else []
            models = parsed if isinstance(parsed, list) else []
        except (TypeError, json.JSONDecodeError):
            models = []
        fields_rows.append((conversation_key, {
            "project_key": project_key,
            "project_label": project_label,
            "models": models,
        }))
    return _facets_from_fields(fields_rows)


def _stored_filter_sql(alias: str, project_key: str | None, model: str | None):
    clauses = []
    params = []
    if project_key is not None:
        clauses.append(f"{alias}.project_key = ?")
        params.append(project_key)
    if model is not None:
        # models_json is the writer's canonical JSON array.  Searching for the
        # complete JSON string literal is exact and does not require JSON1.
        clauses.append(f"instr(COALESCE({alias}.models_json, ''), ?) > 0")
        params.append(json.dumps(model))
    return (" AND ".join(clauses) if clauses else "1"), params


def _page_costs(
    conn: sqlite3.Connection, conversation_keys: list[str], effective_speed: str,
) -> dict[str, float]:
    if not conversation_keys:
        return {}
    placeholders = ",".join("?" for _ in conversation_keys)
    totals = {key: 0.0 for key in conversation_keys}
    for ck, model, inp, cin, out, rout in conn.execute(
        "SELECT conversation_key, model, input_tokens, cached_input_tokens, "
        "output_tokens, reasoning_output_tokens FROM codex_session_entries "
        f"WHERE conversation_key IN ({placeholders}) "
        "ORDER BY conversation_key, id",
        conversation_keys,
    ):
        totals[ck] += _calculate_codex_entry_cost(
            model or "", inp or 0, cin or 0, out or 0, rout or 0,
            speed=effective_speed)
    return totals


def _stored_browse_page(
    conn: sqlite3.Connection, *, effective_speed: str,
    project_key: str | None, model: str | None, limit: int,
    cursor: str | None,
):
    where_sql, filter_params = _stored_filter_sql("r", project_key, model)
    total = conn.execute(
        f"SELECT COUNT(*) FROM codex_conversation_rollups r WHERE {where_sql}",
        filter_params,
    ).fetchone()[0]

    cursor_row = None
    if cursor is not None:
        cursor_where, cursor_params = _stored_filter_sql("c", project_key, model)
        cursor_row = conn.execute(
            "SELECT COALESCE(c.last_activity_utc, ''), c.conversation_key "
            "FROM codex_conversation_rollups c "
            f"WHERE c.conversation_key = ? AND {cursor_where}",
            [cursor, *cursor_params],
        ).fetchone()

    page_where = [where_sql]
    page_params = list(filter_params)
    if cursor_row is not None:
        cursor_last, cursor_key = cursor_row
        page_where.append(
            "(COALESCE(r.last_activity_utc, '') < ? OR "
            "(COALESCE(r.last_activity_utc, '') = ? AND r.conversation_key < ?))")
        page_params.extend((cursor_last, cursor_last, cursor_key))

    sql = (
        "SELECT r.conversation_key, r.item_count, r.started_utc, "
        "r.last_activity_utc, r.project_key, r.project_label, r.models_json, "
        "r.title, r.parent_thread_id, r.source_root_key, t.native_thread_id, "
        "pt.conversation_key, pr.title, pr.project_label, pt.native_thread_id "
        "FROM codex_conversation_rollups r "
        "LEFT JOIN codex_conversation_threads t "
        "ON t.conversation_key = r.conversation_key "
        "LEFT JOIN codex_conversation_threads pt "
        "ON pt.source_root_key = r.source_root_key "
        "AND pt.native_thread_id = r.parent_thread_id "
        "AND pt.conversation_key != r.conversation_key "
        "LEFT JOIN codex_conversation_rollups pr "
        "ON pr.conversation_key = pt.conversation_key "
        f"WHERE {' AND '.join(page_where)} "
        "ORDER BY COALESCE(r.last_activity_utc, '') DESC, r.conversation_key DESC"
    )
    if limit:
        sql += " LIMIT ?"
        page_params.append(limit + 1)
    raw_rows = list(conn.execute(sql, page_params))
    has_more = bool(limit and len(raw_rows) > limit)
    if has_more:
        raw_rows = raw_rows[:limit]

    keys = [row[0] for row in raw_rows]
    costs = _page_costs(conn, keys, effective_speed)
    rows = []
    for row in raw_rows:
        (conversation_key, item_count, started, last, row_project_key,
         project_label, models_json, title, parent_thread_id, source_root_key,
         native_thread_id, parent_key, parent_title, parent_project_label,
         parent_native_thread_id) = row
        try:
            parsed = json.loads(models_json) if models_json else []
            models = parsed if isinstance(parsed, list) else []
        except (TypeError, json.JSONDecodeError):
            models = []
        fields = {
            "item_count": item_count,
            "started": started,
            "last": last,
            "project_key": row_project_key,
            "project_label": project_label,
            "models": models,
            "title": title,
            "parent_thread_id": parent_thread_id,
            "source_root_key": source_root_key,
            "native_thread_id": native_thread_id,
        }
        parent = None
        if parent_key is not None:
            parent = {
                "conversation_key": parent_key,
                "title": _display_chain({
                    "title": parent_title,
                    "project_label": parent_project_label,
                    "native_thread_id": parent_native_thread_id,
                }),
            }
        rows.append(_browse_row_from_fields(
            conversation_key, fields, cost_usd=costs.get(conversation_key, 0.0),
            parent=parent))
    next_cursor = rows[-1]["conversation_key"] if (rows and has_more) else None
    return rows, {"total": total, "returned": len(rows), "cursor": next_cursor}


def list_codex_conversation_facets(conn: sqlite3.Connection) -> dict:
    """Facet-only browse projection; never builds or prices a discarded page."""
    if not codex_normalization_authoritative(conn):
        return {"status": "normalization_pending",
                "facets": {"projects": [], "models": []}}
    facets = (_stored_browse_facets(conn) if _stored_rollups_present(conn)
              else _facets_from_fields(_live_browse_fields(conn)))
    return {"status": "ok", "facets": facets}


def list_codex_conversations(
    conn: sqlite3.Connection,
    *,
    effective_speed: str,
    project_key: str | None = None,
    model: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    selected: str | None = None,
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
    if _stored_rollups_present(conn):
        facets = _stored_browse_facets(conn)
        page_rows, page = _stored_browse_page(
            conn, effective_speed=effective_speed, project_key=project_key,
            model=model, limit=limit, cursor=cursor)
        result = {"status": "ok", "rows": page_rows, "facets": facets, "page": page}
        if selected is not None:
            fields = _rollup_fields(conn, selected)
            if fields is not None:
                result["selected"] = _browse_row(
                    conn, selected, effective_speed, fields)
        return result

    fields_rows = _live_browse_fields(conn)
    rows = [
        _browse_row(conn, conversation_key, effective_speed, fields)
        for conversation_key, fields in fields_rows
    ]
    facets = _facets_from_fields(fields_rows)
    filtered = [row for row in rows
                if (project_key is None or row["project_key"] == project_key)
                and (model is None or model in (row["models"] or []))]
    filtered.sort(key=_recent_sort_key, reverse=True)
    page_rows, page = _paginate_rows(filtered, cursor=cursor, limit=limit)
    result = {"status": "ok", "rows": page_rows, "facets": facets, "page": page}
    if selected is not None:
        selected_row = next(
            (row for row in rows if row["conversation_key"] == selected), None)
        if selected_row is not None:
            result["selected"] = selected_row
    return result


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


# ── #482 visible render-leaf projection ─────────────────────────────────────

_COMPLETION_EVENT_TYPES = {
    "patch_apply_end",
    "web_search_end",
    "mcp_tool_call_end",
}


def _find_surface(row) -> str | None:
    if row.kind in {"user", "assistant", "reasoning", "meta"}:
        return "body"
    if row.kind == "tool_call":
        return "call"
    if row.kind == "tool_output":
        return "output"
    if row.kind == "event" and row.event_type in _COMPLETION_EVENT_TYPES:
        return "completion"
    return None


def _project_plain_leaves(leaves: list[RenderLeaf]):
    """Project structured-card leaves with a non-searchable visual boundary.

    Native card fields render in separate block/inline containers.  A newline
    between fields prevents a match from crossing that visual boundary while
    keeping each leaf's offsets local to the exact string its React component
    receives.
    """
    text_parts: list[str] = []
    projected: list[ProjectedLeaf] = []
    cursor = 0
    for leaf in leaves:
        if not leaf.text:
            continue
        if text_parts:
            text_parts.append("\n")
            cursor += 1
        start = cursor
        text_parts.append(leaf.text)
        cursor += len(leaf.text)
        projected.append(ProjectedLeaf(leaf.key, start, cursor))
    return "".join(text_parts), tuple(projected)


def _project_markdown_fields(fields: list[tuple[str, str]]):
    text_parts: list[str] = []
    leaves: list[ProjectedLeaf] = []
    cursor = 0
    for field, source in fields:
        if not source:
            continue
        if text_parts:
            text_parts.append("\n")
            cursor += 1
        projected_text, projected_leaves = project_markdown(source)
        text_parts.append(projected_text)
        leaves.extend(
            ProjectedLeaf(f"{field}/{leaf.key}", cursor + leaf.start, cursor + leaf.end)
            for leaf in projected_leaves
        )
        cursor += len(projected_text)
    return "".join(text_parts), tuple(leaves)


def _patch_diff_leaves(files) -> list[RenderLeaf]:
    leaves: list[RenderLeaf] = []
    for file_index, file in enumerate(files or []):
        if not isinstance(file, dict):
            continue
        for field in ("path", "move_path"):
            value = file.get(field)
            if isinstance(value, str) and value:
                leaves.append(RenderLeaf(f"files.{file_index}.{field}", value))
        diff = file.get("unified_diff")
        if not isinstance(diff, str):
            continue
        hunk_index = -1
        row_index = 0
        for line in diff.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if line.startswith("@@"):
                hunk_index += 1
                row_index = 0
                continue
            if hunk_index < 0 or not line or line.startswith(("--- ", "+++ ", "\\")):
                continue
            if line[0] not in {"+", "-", " "}:
                continue
            leaves.append(RenderLeaf(
                f"files.{file_index}.diff.{hunk_index}.{row_index}", line[1:]))
            row_index += 1
    return leaves


def _json_card_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, indent=2, separators=(",", ": "))


def _project_completion_payload(payload: dict):
    patch = kern.decode_patch_event_card(payload)
    if patch is not None:
        leaves = _patch_diff_leaves(patch.get("files"))
        for field in ("stdout", "stderr"):
            value = patch.get(field)
            if isinstance(value, str) and value:
                leaves.append(RenderLeaf(field, _strip_ansi(value)))
        return _project_plain_leaves(leaves) if leaves else None

    completion = kern.decode_secondary_event_card(payload)
    if completion is None:
        return None
    if completion.get("type") == "web_search_completion":
        leaves: list[RenderLeaf] = []
        for index, result in enumerate(completion.get("results") or []):
            if not isinstance(result, dict):
                continue
            for field in ("title", "domain", "snippet", "ref_id"):
                value = result.get(field)
                if isinstance(value, str) and value:
                    leaves.append(RenderLeaf(f"results.{index}.{field}", value))
        error = completion.get("error")
        if error is not None:
            leaves.append(RenderLeaf("error", _json_card_text(error)))
        return _project_plain_leaves(leaves) if leaves else None
    if completion.get("type") == "mcp_completion":
        leaves = [
            RenderLeaf("arguments", _json_card_text(completion.get("arguments"))),
            RenderLeaf("result", _json_card_text(completion.get("result"))),
        ]
        return _project_plain_leaves(leaves)
    return None


def _project_find_row(row, *, payload: dict | None = None, block: dict | None = None):
    if row.kind == "event" and row.event_type in _COMPLETION_EVENT_TYPES and payload:
        completion = _project_completion_payload(payload)
        if completion is not None:
            return completion
    text = _row_display(row)
    if not text:
        return None
    if row.kind == "meta":
        try:
            detail = json.loads(row.detail_json or "{}")
        except (TypeError, json.JSONDecodeError):
            detail = {}
        meta_kind = detail.get("meta_kind") if isinstance(detail, dict) else None
        if meta_kind == "command":
            return project_plain((RenderLeaf("t0", text),))
        if meta_kind == "context":
            return project_context(text)
        return project_markdown(text)
    if row.kind == "reasoning" and isinstance(block, dict):
        detail = block.get("detail")
        reasoning = detail.get("reasoning") if isinstance(detail, dict) else None
        if isinstance(reasoning, dict):
            visible_headings = block.get("_find_visible_headings")
            if isinstance(visible_headings, list):
                leaves = [
                    RenderLeaf(leaf_key, text)
                    for leaf_key, text in visible_headings
                    if isinstance(leaf_key, str) and isinstance(text, str) and text
                ]
                if leaves:
                    return _project_plain_leaves(leaves)
                if reasoning.get("body") is None:
                    return None
            fields = [
                (field, reasoning[field])
                for field in ("title", "summary", "body")
                if isinstance(reasoning.get(field), str) and reasoning[field]
            ]
            if fields:
                return _project_markdown_fields(fields)
    if row.kind in {"user", "assistant", "reasoning"}:
        return project_markdown(text)
    if row.kind == "tool_output" and isinstance(block, dict):
        detail = block.get("detail")
        card = detail.get("card") if isinstance(detail, dict) else None
        if isinstance(card, dict) and card.get("type") == "terminal":
            output = card.get("output")
            parts = output.get("parts") if isinstance(output, dict) else None
            if isinstance(parts, list):
                stdout = "".join(
                    part.get("text", "") for part in parts
                    if isinstance(part, dict) and part.get("type") == "text"
                    and part.get("stream") != "stderr"
                )
                stderr = "".join(
                    part.get("text", "") for part in parts
                    if isinstance(part, dict) and part.get("type") == "text"
                    and part.get("stream") == "stderr"
                )
                leaves = []
                if stdout:
                    leaves.append(RenderLeaf("stdout", _strip_ansi(stdout)))
                if stderr:
                    leaves.append(RenderLeaf("stderr", _strip_ansi(stderr)))
                leaves.extend(
                    RenderLeaf(f"raw.{index}", part["text"])
                    for index, part in enumerate(parts)
                    if isinstance(part, dict) and part.get("type") == "raw"
                    and isinstance(part.get("text"), str) and part["text"]
                )
                if leaves:
                    return _project_plain_leaves(leaves)
    if row.kind == "tool_call" and isinstance(block, dict):
        detail = block.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        card = detail.get("card")
        if isinstance(card, dict):
            if card.get("type") == "patch":
                return None
            if card.get("type") == "web_search" and isinstance(card.get("query"), str):
                return project_plain((RenderLeaf("query", card["query"]),))
            if card.get("type") == "mcp":
                return None
            if card.get("type") == "terminal":
                commands = [
                    RenderLeaf(f"commands.{index}", entry["command"])
                    for index, entry in enumerate(card.get("commands") or [])
                    if isinstance(entry, dict) and isinstance(entry.get("command"), str)
                ]
                if commands:
                    return _project_plain_leaves(commands)
        args = detail.get("args")
        if isinstance(args, str) and args:
            return project_plain((RenderLeaf("t0", args),))
    return project_plain((RenderLeaf("t0", text),))


def materialize_codex_find_projection(
    conn: sqlite3.Connection,
    conversation_keys,
) -> None:
    """Replace #482 projection rows for the affected conversations.

    The existing item/block builder is the only authority for native folds.
    Every searchable physical row keeps its own block key; a folded output or
    completion separately records the visual call block that owns it.
    """
    keys = sorted({key for key in conversation_keys if key})
    if not keys:
        return
    for conversation_key in keys:
        conn.execute(
            "DELETE FROM codex_find_projection WHERE conversation_key=?",
            (conversation_key,),
        )
        rows = [
            kern.CodexNormalizedRow(*row)
            for row in conn.execute(
                "SELECT " + _ROW_COLS + " FROM codex_conversation_messages "
                "WHERE conversation_key=? "
                "ORDER BY timestamp_utc,source_path,line_offset",
                (conversation_key,),
            )
        ]
        if not rows:
            continue
        kept, _suppressed = kern.pair_mirrors(rows)
        items = kern.canonical_items(kept)
        payloads = _load_row_payloads(conn, conversation_key)
        pos_to_item = _pos_to_item_key(conn, conversation_key)
        row_ids = {
            (source_path, line_offset): message_id
            for message_id, source_path, line_offset in conn.execute(
                "SELECT id,source_path,line_offset "
                "FROM codex_conversation_messages WHERE conversation_key=?",
                (conversation_key,),
            )
        }
        render_order = 0
        seen: set[tuple[str, int]] = set()
        seen_reasoning_by_turn: dict[str, set[str]] = {}

        def store(row, *, container_block_key: str, block: dict | None = None) -> None:
            nonlocal render_order
            position = (row.source_path, row.line_offset)
            if position in seen:
                return
            surface = _find_surface(row)
            retained = _row_payload(row, payloads)
            payload = retained[1] if retained is not None else None
            projected = _project_find_row(row, payload=payload, block=block)
            message_id = row_ids.get(position)
            if surface is None or projected is None or message_id is None:
                return
            text, leaves = projected
            if not text:
                return
            physical_block_key = _block_key_for_row(row)
            item_key = pos_to_item.get(position)
            if item_key is None:
                return
            disclosure = (
                [container_block_key]
                if row.kind in {"reasoning", "meta"} or surface != "body"
                else []
            )
            conn.execute(
                "INSERT INTO codex_find_projection "
                "(message_id,conversation_key,item_key,block_key,"
                "container_block_key,surface,render_order,projected_text,"
                "leaves_json,disclosure_json,projection_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    conversation_key,
                    item_key,
                    physical_block_key,
                    container_block_key,
                    surface,
                    render_order,
                    text,
                    json.dumps(
                        [
                            {"key": leaf.key, "start": leaf.start, "end": leaf.end}
                            for leaf in leaves
                        ],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(disclosure, separators=(",", ":")),
                    CODEX_FIND_PROJECTION_VERSION,
                ),
            )
            seen.add(position)
            render_order += 1

        for item in items:
            built_entries = _item_blocks_with_rows(
                item, payloads, decompose_headings=True,
            )
            completion_owner: dict[str, str] = {}
            for candidate_block, _candidate_primary, _candidate_output in built_entries:
                candidate_container = (
                    candidate_block.get("block_key")
                    or _block_key_for_row(_candidate_primary)
                )
                candidate_detail = candidate_block.get("detail")
                candidate_card = (
                    candidate_detail.get("card")
                    if isinstance(candidate_detail, dict) else None
                )
                completion = (
                    candidate_card.get("completion")
                    if isinstance(candidate_card, dict) else None
                )
                event_key = (
                    completion.get("event_block_key")
                    if isinstance(completion, dict) else None
                )
                if isinstance(event_key, str):
                    completion_owner[event_key] = candidate_container

            for block, primary, output in built_entries:
                container = block.get("block_key") or _block_key_for_row(primary)
                container = completion_owner.get(_block_key_for_row(primary), container)
                if primary.kind == "reasoning":
                    detail = block.get("detail")
                    reasoning = (
                        detail.get("reasoning") if isinstance(detail, dict) else None
                    )
                    headings = (
                        reasoning.get("headings")
                        if isinstance(reasoning, dict) else None
                    )
                    if isinstance(headings, list):
                        turn_key = primary.turn_id or item.get("turn_id") or ""
                        prior = seen_reasoning_by_turn.setdefault(turn_key, set())
                        visible = []
                        for heading_index, heading in enumerate(headings):
                            text = heading.get("text") if isinstance(heading, dict) else None
                            if not isinstance(text, str) or text in prior:
                                continue
                            prior.add(text)
                            visible.append((f"headings.{heading_index}", text))
                        block["_find_visible_headings"] = visible
                store(primary, container_block_key=container, block=block)
                if output is not None:
                    store(output, container_block_key=container, block=block)
            # Completion folds that intentionally produce no standalone block
            # still own a searchable physical surface and point at their call.
            for row in item["rows"]:
                if row.event_type not in _COMPLETION_EVENT_TYPES:
                    continue
                physical_key = _block_key_for_row(row)
                container = completion_owner.get(physical_key, physical_key)
                store(row, container_block_key=container)

    conn.execute(
        "INSERT INTO cache_meta(key,value) VALUES"
        "('codex_find_projection_generation','1') "
        "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1"
    )


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


def _search_display_text(text: str | None) -> str:
    """Readable search projection for retained structured content arrays.

    Tool outputs must retain their provider JSON in ``search_tool`` so every
    leaf stays searchable.  The rail, however, needs the same ordered text
    leaves a reader sees—not the serialized wrapper.  Unknown JSON and future
    shapes fall back byte-for-byte to the retained string.
    """
    if not text:
        return ""
    raw = str(text)
    if not raw.lstrip().startswith("["):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        # Search columns are capped.  A large content array can therefore end
        # mid-string and cease to be valid JSON even though one or more leading
        # text parts are complete.  `_canonical_json` sorts object keys, so a
        # text-bearing part starts as `{\"text\":...}`. Decode only those
        # complete JSON string literals; never regex-unescape provider bytes.
        parts = []
        for match in re.finditer(r'(?:\A\[\{|,\{)"text":', raw):
            try:
                value, _end = json.JSONDecoder().raw_decode(raw, match.end())
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, str) and value:
                parts.append(value)
        return "\n".join(parts) + ("\n…" if parts else "") or raw
    joined = kern._join_content_texts(parsed)
    return joined if joined else raw


def _search_excerpt(text: str | None, query: str, width: int = 200) -> str:
    """Whitespace-collapsed, match-centred excerpt from readable search text."""
    collapsed = " ".join(_search_display_text(text).split())
    if not collapsed:
        return ""
    needle = " ".join(query.split())
    found = collapsed.casefold().find(needle.casefold()) if needle else -1
    if found < 0 or len(collapsed) <= width:
        return collapsed[:width]
    start = max(0, found - (width // 3))
    end = min(len(collapsed), start + width)
    start = max(0, end - width)
    excerpt = collapsed[start:end]
    return (("… " if start else "") + excerpt
            + (" …" if end < len(collapsed) else ""))


def _collapse_message_hits(
    conn: sqlite3.Connection, matched_rows: list, query: str,
) -> list[dict]:
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
                hit["snippet"] = _search_excerpt(disp, query)
    return [
        {"conversation_key": h["conversation_key"], "item_key": h["item_key"],
         "title": h["title"], "snippet": h["snippet"], "badges": sorted(h["_badges"]),
         "last_activity_utc": h["last_activity_utc"], "project_label": h["project_label"]}
        for h in collapsed.values()
    ]


def _search_title(conn: sqlite3.Connection, query: str) -> list[dict]:
    """Title search over the rollup table — identical LIKE semantics in both FTS
    and LIKE modes (§6.2). Conversation-level hits (no item anchor).

    #463 S4 §5.1 — the third read path that needs cleaning, and the one that is
    user-facing on the CLI: `cctally transcript search --source codex
    --kind title` prints this `snippet` and emits this `title` in its JSON. The
    MATCH still runs against the stored value, so a query that names markup
    still finds its conversation; only what is shown is cleaned.
    """
    like = f"%{query}%"
    hits = []
    for ck, title, last_act, project_label in conn.execute(
            "SELECT conversation_key, title, last_activity_utc, project_label "
            "FROM codex_conversation_rollups WHERE title LIKE ?", (like,)):
        cleaned = clean_codex_title(title)
        hits.append(
            {"conversation_key": ck, "item_key": None, "title": cleaned,
             "snippet": _excerpt(cleaned), "badges": ["title"],
             "last_activity_utc": last_act, "project_label": project_label})
    return hits


def _search_files(conn: sqlite3.Connection, query: str) -> list[dict]:
    """File-touch search — matches file paths, collapsed to the owning message's
    canonical item_key (§6.2). ``message_id`` is an application-level link, so
    an orphan is skipped and cannot suppress valid rows."""
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
            conn, _matched_message_rows(conn, query, kind, mode), query)
    hits.sort(key=lambda h: (h["conversation_key"], h["item_key"] or ""))
    total = len(hits)
    page_hits, page = _paginate_hits(hits, cursor=cursor, limit=limit)
    return {
        "status": "ok", "query": query, "hits": page_hits, "total": total,
        "mode": mode, "depth": "full", "page": page,
    }


# ── in-conversation find (§3.1) ───────────────────────────────────────────────

_CODEX_EXACT_FIND_SCHEMA_VERSION = 2
_CODEX_EXACT_FIND_DEFAULT_LIMIT = 100
_CODEX_EXACT_FIND_MAX_LIMIT = 200
_CODEX_EXACT_FIND_CURSOR_PREFIX = "ofc1."
_CODEX_EXACT_FIND_QUERY_DOMAIN = b"cctally-codex-find-query-v1\0"
_CODEX_EXACT_FIND_OCCURRENCE_DOMAIN = b"cctally-codex-find-occurrence-v1\0"


class InvalidFindCursor(ValueError):
    """The external exact-find cursor is malformed."""


class StaleFindCursor(ValueError):
    """The exact-find cursor belongs to another query or projection generation."""


def _exact_find_query_id(
    query: str, *, regex: bool, case_sensitive: bool, kind: str
) -> str:
    payload = json.dumps(
        {
            "case": case_sensitive,
            "kind": kind,
            "projection": CODEX_FIND_PROJECTION_VERSION,
            "query": query,
            "regex": regex,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(_CODEX_EXACT_FIND_QUERY_DOMAIN + payload).hexdigest()


def _exact_find_occurrence_id(
    query_id: str,
    *,
    block_key: str,
    surface: str,
    ordinal: int,
    start: int,
    end: int,
) -> str:
    payload = json.dumps(
        [
            CODEX_FIND_PROJECTION_VERSION,
            query_id,
            block_key,
            surface,
            ordinal,
            start,
            end,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(_CODEX_EXACT_FIND_OCCURRENCE_DOMAIN + payload).digest()
    return "o1." + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _encode_exact_find_cursor(
    *,
    query_id: str,
    generation: int,
    start_index: int,
    direction: str,
    boundary: tuple[int, int, str, int],
) -> str:
    payload = json.dumps(
        {
            "b": list(boundary),
            "d": direction,
            "g": generation,
            "i": start_index,
            "q": query_id,
            "v": CODEX_FIND_PROJECTION_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _CODEX_EXACT_FIND_CURSOR_PREFIX + base64.urlsafe_b64encode(
        payload
    ).decode("ascii").rstrip("=")


def _decode_exact_find_cursor(cursor: str) -> dict[str, object]:
    if not isinstance(cursor, str) or not cursor.startswith(
        _CODEX_EXACT_FIND_CURSOR_PREFIX
    ):
        raise InvalidFindCursor(cursor)
    encoded = cursor[len(_CODEX_EXACT_FIND_CURSOR_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        if canonical != encoded:
            raise InvalidFindCursor(cursor)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidFindCursor(cursor) from None
    if not isinstance(payload, dict) or set(payload) != {"b", "d", "g", "i", "q", "v"}:
        raise InvalidFindCursor(cursor)
    boundary = payload.get("b")
    if (
        payload.get("d") not in {"next", "previous"}
        or type(payload.get("g")) is not int
        or type(payload.get("i")) is not int
        or payload["i"] < 0
        or not isinstance(payload.get("q"), str)
        or payload.get("v") != CODEX_FIND_PROJECTION_VERSION
        or not isinstance(boundary, list)
        or len(boundary) != 4
        or type(boundary[0]) is not int
        or type(boundary[1]) is not int
        or not isinstance(boundary[2], str)
        or type(boundary[3]) is not int
    ):
        raise InvalidFindCursor(cursor)
    return payload


def _exact_find_base(
    query_id: str,
    *,
    status: str,
    regex: bool,
    kind: str,
) -> dict[str, object]:
    return {
        "schema_version": _CODEX_EXACT_FIND_SCHEMA_VERSION,
        "semantics": "occurrence",
        "status": status,
        "query_id": query_id,
        "selection_stale": False,
        "mode": "regex" if regex else "literal",
        "kind": kind,
        "search_depth": "full",
    }


def find_occurrences_in_codex_conversation(
    conn: sqlite3.Connection,
    conversation_key: str,
    query: str,
    *,
    regex: bool,
    case_sensitive: bool,
    kind: str,
    limit: int = _CODEX_EXACT_FIND_DEFAULT_LIMIT,
    cursor: str | None = None,
    direction: str = "next",
    around: str | None = None,
) -> dict[str, object]:
    """Return occurrence-exact matches over the materialized visible projection.

    Matching never crosses a physical projection surface. Coordinates are
    Unicode-scalar offsets into stable render leaves, while paging cursors are
    bound to both query semantics and the current projection generation.
    """
    if kind not in CODEX_FIND_KINDS:
        raise ValueError(f"unknown kind: {kind}")
    if not isinstance(limit, int) or not 1 <= limit <= _CODEX_EXACT_FIND_MAX_LIMIT:
        raise ValueError("find limit must be between 1 and 200")
    if direction not in {"next", "previous"}:
        raise ValueError("find direction must be next or previous")
    if cursor is not None and around is not None:
        raise ValueError("find cursor and around are mutually exclusive")
    q = (query or "").strip()
    query_id = _exact_find_query_id(
        q, regex=regex, case_sensitive=case_sensitive, kind=kind
    )
    exists = conn.execute(
        "SELECT 1 FROM codex_conversation_messages WHERE conversation_key=? LIMIT 1",
        (conversation_key,),
    ).fetchone()
    if exists is None:
        return {"status": "not_found", "conversation_key": conversation_key}
    complete = conn.execute(
        "SELECT 1 FROM cache_meta WHERE "
        "key='codex_find_projection_complete_version' AND value=?",
        (str(CODEX_FIND_PROJECTION_VERSION),),
    ).fetchone()
    base = _exact_find_base(query_id, status="ready", regex=regex, kind=kind)
    empty_page = {
        "start_index": 0,
        "previous_cursor": None,
        "next_cursor": None,
        "occurrences": [],
    }
    if complete is None:
        return {**base, "status": "indexing", "page": empty_page}
    generation_row = conn.execute(
        "SELECT value FROM cache_meta WHERE key='codex_find_projection_generation'"
    ).fetchone()
    try:
        generation = int(generation_row[0]) if generation_row else 0
    except (TypeError, ValueError):
        generation = 0

    decoded_cursor = None
    if cursor is not None:
        decoded_cursor = _decode_exact_find_cursor(cursor)
        if (
            decoded_cursor["q"] != query_id
            or decoded_cursor["g"] != generation
            or decoded_cursor["d"] != direction
        ):
            raise StaleFindCursor(cursor)

    if not q or (regex and len(q) > _CODEX_FIND_REGEX_MAX_LEN):
        return {**base, "total": 0, "page": empty_page}
    pattern = re.compile(q, 0 if case_sensitive else re.IGNORECASE) if regex else None
    kind_predicate = {
        "all": "1=1",
        "prompts": "m.kind='user'",
        "assistant": "m.kind='assistant'",
        "tools": "p.surface IN ('call','output','completion')",
        "thinking": "m.kind='reasoning'",
    }[kind]
    rows = conn.execute(
        "SELECT p.message_id,p.item_key,p.block_key,p.container_block_key,"
        "p.surface,p.render_order,"
        "p.projected_text,p.leaves_json,p.disclosure_json,m.kind "
        "FROM codex_find_projection p "
        "JOIN codex_conversation_messages m ON m.id=p.message_id "
        "WHERE p.conversation_key=? AND p.projection_version=? AND "
        + kind_predicate
        + " ORDER BY p.render_order,p.message_id,p.surface",
        (conversation_key, CODEX_FIND_PROJECTION_VERSION),
    )
    requested: list[tuple[dict[str, object], tuple[int, int, str, int]]] = []
    around_page: list[tuple[dict[str, object], tuple[int, int, str, int]]] = []
    head: list[tuple[dict[str, object], tuple[int, int, str, int]]] = []
    tail: deque[tuple[dict[str, object], tuple[int, int, str, int]]] = deque(
        maxlen=limit
    )
    head_next = None
    requested_next = None
    around_next = None
    around_index = None
    cursor_valid = decoded_cursor is None
    cursor_index = int(decoded_cursor["i"]) if decoded_cursor is not None else None
    requested_start = None
    requested_end = None
    if decoded_cursor is not None:
        if direction == "previous":
            requested_end = cursor_index
            requested_start = max(0, cursor_index - limit)
        else:
            requested_start = cursor_index
            requested_end = cursor_index + limit
    total = 0
    for (
        message_id,
        item_key,
        block_key,
        container_block_key,
        surface,
        render_order,
        text,
        leaves_json,
        disclosure_json,
        row_kind,
    ) in rows:
        try:
            leaves = tuple(ProjectedLeaf(**leaf) for leaf in json.loads(leaves_json))
            disclosure = json.loads(disclosure_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        ranges = (
            iter_regex_ranges(text, pattern)
            if pattern is not None
            else iter_literal_ranges(text, q, case_sensitive=case_sensitive)
        )
        for ordinal, match in enumerate(ranges):
            fragments = slice_range_to_leaves(match, leaves)
            if not fragments:
                continue
            occurrence_id = _exact_find_occurrence_id(
                query_id,
                block_key=block_key,
                surface=surface,
                ordinal=ordinal,
                start=match.start,
                end=match.end,
            )
            match_kinds = []
            if surface != "body":
                match_kinds.append("tool")
            if row_kind == "reasoning":
                match_kinds.append("thinking")
            occurrence = {
                    "occurrence_id": occurrence_id,
                    "item_key": item_key,
                    "block_key": block_key,
                    "container_block_key": container_block_key,
                    "surface": surface,
                    "match_kinds": match_kinds,
                    "disclosure": disclosure if isinstance(disclosure, list) else [],
                    "fragments": [
                        {
                            "leaf_key": fragment.leaf_key,
                            "start": fragment.start,
                            "end": fragment.end,
                        }
                        for fragment in fragments
                    ],
                }
            boundary = (render_order, message_id, surface, ordinal)
            pair = (occurrence, boundary)
            index = total
            if len(head) < limit:
                head.append(pair)
            elif index == limit:
                head_next = pair
            tail.append(pair)
            if cursor_index == index:
                if tuple(decoded_cursor["b"]) != boundary:
                    raise StaleFindCursor(cursor)
                cursor_valid = True
            if (
                requested_start is not None
                and requested_end is not None
                and requested_start <= index < requested_end
            ):
                requested.append(pair)
            elif requested_end is not None and index == requested_end:
                requested_next = pair
            if around is not None and around_index is None:
                if occurrence["occurrence_id"] == around:
                    around_index = index
                    around_page.append(pair)
            elif around_index is not None:
                if len(around_page) < limit:
                    around_page.append(pair)
                elif index == around_index + limit:
                    around_next = pair
            total += 1

    if not cursor_valid:
        raise StaleFindCursor(cursor)
    selection_stale = around is not None and around_index is None
    next_pair = None
    if around is not None and around_index is not None:
        start_index = around_index
        page_pairs = around_page
        next_pair = around_next
    elif around is not None:
        start_index = 0
        page_pairs = head
        next_pair = head_next
    elif decoded_cursor is not None:
        start_index = min(requested_start or 0, total)
        page_pairs = requested
        next_pair = requested_next
    elif direction == "previous":
        page_pairs = list(tail)
        start_index = max(0, total - len(page_pairs))
    else:
        start_index = 0
        page_pairs = head
        next_pair = head_next
    page_occurrences = [occurrence for occurrence, _boundary in page_pairs]

    def cursor_for(
        index: int,
        cursor_direction: str,
        pair: tuple[dict[str, object], tuple[int, int, str, int]] | None,
    ) -> str | None:
        if not 0 <= index < total or pair is None:
            return None
        return _encode_exact_find_cursor(
            query_id=query_id,
            generation=generation,
            start_index=index,
            direction=cursor_direction,
            boundary=pair[1],
        )

    previous_cursor = (
        cursor_for(start_index, "previous", page_pairs[0])
        if start_index > 0 and page_pairs else None
    )
    next_index = start_index + len(page_occurrences)
    next_cursor = cursor_for(next_index, "next", next_pair)
    return {
        **base,
        "total": total,
        "selection_stale": selection_stale,
        "page": {
            "start_index": start_index,
            "previous_cursor": previous_cursor,
            "next_cursor": next_cursor,
            "occurrences": page_occurrences,
        },
    }

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
        # The SAME ordinal substitution the paged assembly applies (spec sections
        # 4.3 and 6.5). Without it this route published the provider's own
        # session id while the paged detail published the conversation-local
        # ordinal, so one field carried two meanings depending on which route
        # served it — and a client validator can only be written against one.
        # A session the index does not know becomes `ref: null`, never the raw
        # id, because `_apply_session_ordinals` fails closed.
        index_rows, _detail_bytes = _load_conversation_index_rows(
            conn, conversation_key)
        _envelope, ordinals = _build_session_index(index_rows)
        _apply_session_ordinals(card, ordinals)
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
