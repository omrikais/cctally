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
from _lib_conversation import _strip_ansi
from _lib_conversation_query import _FULL_PAYLOAD_CEILING, _first_nonblank_line
from _lib_pricing import _calculate_codex_entry_cost

# ── constants ────────────────────────────────────────────────────────────────

# Migration whose applied marker makes the normalized corpus authoritative
# (§3.5). Fresh caches stamp the full registry at creation, so they are always
# authoritative; a held-lock deferral leaves it pending.
CODEX_NORMALIZATION_MIGRATION = "025_codex_conversation_normalization"

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
}


# ── authority probe (§3.5) ────────────────────────────────────────────────────


def codex_normalization_authoritative(conn: sqlite3.Connection) -> bool:
    """True iff the normalized Codex corpus is authoritative (§3.5).

    Split stores use their provider-local rebuild marker: current schema alone
    is not authority while migration 028's byte-zero replay is pending. Legacy
    monolithic/bare connections retain the migration-025 stamp contract.
    """
    try:
        split = conn.execute(
            "SELECT 1 FROM cache_meta "
            "WHERE key='conversation_schema_version'"
        ).fetchone() is not None
        if split:
            pending = conn.execute(
                "SELECT 1 FROM cache_meta "
                "WHERE key='conversation_rebuild_codex_pending'"
            ).fetchone() is not None
            return not pending
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
    """
    if klass == "response":
        parts = ("turn", conversation_key or "", turn_id or "")
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


def codex_block_key(
    conversation_key: str,
    *,
    source_path: str | None,
    line_offset: int | None,
    content_digest: str | None,
) -> str:
    """Opaque, ordinal-free payload-block anchor over a tool_call row's row-class
    identity (§3.4). Same domain-separated hash family as ``codex_item_key``'s row
    class — ``(conversation_key, fingerprint(source_path), line_offset,
    content_digest)`` — with a DISTINCT domain, so a block key never collides with
    an item key. Stable per block, unique per tool_call physical row: a same-offset
    content replacement changes it (content_digest moves), an out-of-order append
    elsewhere leaves it (no population-relative ordinals)."""
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


def _item_kind(item: dict) -> str:
    klass = item["klass"]
    if klass == "prompt":
        return "user"
    if klass == "response":
        return "assistant"
    if klass == "event":
        return "event"
    return item["anchor_row"].kind  # unturned: the row's own provider kind


def _item_blocks_with_rows(item: dict) -> list[list]:
    """Assemble an item's blocks (the historical ``_build_item_blocks`` behaviour)
    AND expose each block's underlying rows, so the detail renderer and the payload
    locator (§3.4) share ONE folding rule. Each entry is a 3-list
    ``[block_dict, primary_row, output_row_or_None]``: a ``tool_output`` folds into
    a preceding ``tool_call`` block only when its ``call_id`` is non-empty, owned by
    exactly one tool_call, and that call was already seen (call precedes output).
    Physical order within the item is preserved.

    Every ``tool_call`` block additionally carries an opaque ``block_key`` (§3.4) —
    the payload-capable anchor. Non-tool blocks carry no ``block_key``."""
    rows = item["rows"]
    call_owner_count: dict[str, int] = {}
    for r in rows:
        if r.kind == "tool_call" and r.call_id:
            call_owner_count[r.call_id] = call_owner_count.get(r.call_id, 0) + 1
    entries: list[list] = []
    tool_entry_by_call: dict[str, int] = {}
    for r in rows:
        text = _row_display(r)
        detail = _parse_detail(r.detail_json)
        if (r.kind == "tool_output" and r.call_id
                and call_owner_count.get(r.call_id, 0) == 1
                and r.call_id in tool_entry_by_call):
            owner = entries[tool_entry_by_call[r.call_id]]
            owner[0]["output"] = {"text": text, "detail": detail}
            owner[2] = r
            continue
        block = {
            "kind": r.kind, "text": text, "detail": detail,
            "call_id": r.call_id, "timestamp_utc": r.timestamp_utc,
        }
        if r.kind == "tool_call":
            block["block_key"] = _block_key_for_row(r)
            if r.call_id and call_owner_count.get(r.call_id, 0) == 1:
                tool_entry_by_call[r.call_id] = len(entries)
        entries.append([block, r, None])
    return entries


def _build_item_blocks(item: dict) -> list[dict]:
    """Assemble an item's blocks, folding each ``tool_output`` into its
    ``tool_call`` block via ``call_id`` when that call_id has exactly one owner
    (§5.2). Physical order within the item is preserved. Thin projection of
    ``_item_blocks_with_rows`` — the single source of truth for the folding rule."""
    return [entry[0] for entry in _item_blocks_with_rows(item)]


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


def _file_boundaries(conn: sqlite3.Connection, source_path: str) -> list[tuple[int, str | None]]:
    """Ordered ``(line_offset, active_turn)`` boundaries for one file — a
    ``turn_context`` sets the effective turn, a ``session_meta`` resets it to
    ``None`` (a new un-turned segment). Read from the retained physical events so
    an accounting row that precedes the first normalized message still attributes
    to the turn ``turn_context`` already opened (§5.4)."""
    boundaries: list[tuple[int, str | None]] = []
    for off, rtype, tid, payload_json in conn.execute(
        "SELECT line_offset, record_type, turn_id, payload_json "
        "FROM codex_conversation_events "
        "WHERE source_path = ? AND record_type IN ('turn_context','session_meta') "
        "ORDER BY line_offset",
        (source_path,),
    ):
        if rtype == "session_meta":
            boundaries.append((off, None))
            continue
        turn = tid
        if turn is None:
            try:
                payload = (json.loads(payload_json or "{}").get("payload") or {})
                turn = payload.get("turn_id")
            except (json.JSONDecodeError, TypeError, AttributeError):
                turn = None
        boundaries.append((off, turn))
    return boundaries


def _turn_at(boundaries: list[tuple[int, str | None]], offset: int) -> str | None:
    """Nearest preceding turn boundary by file offset (§5.4); ``None`` when the
    row precedes any turn (or sits in an un-turned segment)."""
    turn: str | None = None
    for boff, bturn in boundaries:
        if boff <= offset:
            turn = bturn
        else:
            break
    return turn


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
    boundaries: dict[str, list] = {}
    for source_path in {e[0] for e in entries}:
        boundaries[source_path] = _file_boundaries(conn, source_path)
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
        turn = _turn_at(boundaries.get(source_path, []), offset)
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


def _paginate_items(items: list[dict], *, after, before, tail, limit):
    keys = [it["item_key"] for it in items]
    lo, hi = 0, len(items)
    if after is not None and after in keys:
        lo = keys.index(after) + 1
    if before is not None and before in keys:
        hi = keys.index(before)
    window = items[lo:hi]
    if tail is not None:
        cap = min(tail, limit) if limit else tail
        window = window[-cap:] if cap else window
    elif limit:
        window = window[:limit]
    first_key = window[0]["item_key"] if window else None
    last_key = window[-1]["item_key"] if window else None
    has_before = bool(window) and keys.index(first_key) > 0
    has_after = bool(window) and keys.index(last_key) < len(items) - 1
    page = {
        "total": len(items), "returned": len(window),
        "before": first_key if has_before else None,
        "after": last_key if has_after else None,
        "has_before": has_before, "has_after": has_after,
    }
    return window, page


def get_codex_conversation(
    conn: sqlite3.Connection,
    conversation_key: str,
    *,
    effective_speed: str,
    after: str | None = None,
    before: str | None = None,
    tail: int | None = None,
    limit: int = 200,
) -> dict:
    """Detail envelope (§5.6): status ``ok`` | ``normalization_pending`` |
    ``not_found``. ``ok`` carries canonical items (mirror-paired, tool-folded),
    per-turn cost with an explicit unattributed bucket, threading, and a page
    over ``item_key``."""
    if not codex_normalization_authoritative(conn):
        return {"status": "normalization_pending", "conversation_key": conversation_key,
                "items": [], "children": []}
    rows = _load_conversation_rows(conn, conversation_key)
    if not rows:
        return {"status": "not_found", "conversation_key": conversation_key}
    kept, _suppressed = kern.pair_mirrors(rows)
    items = kern.canonical_items(kept)
    turn_cost, turn_tokens, unattr_cost, unattr_tokens, total, conv_tokens = _attribute_costs(
        conn, conversation_key, effective_speed)
    # Carrier item per turn: prefer the response item, else the first item of the
    # turn — so every priced turn's cost lands on exactly one item (§5.4 reconcile).
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
    built: list[dict] = []
    for idx, it in enumerate(items):
        turn = it["turn_id"]
        cost = None
        tokens = None
        if turn is not None and carriers.get(turn) == idx and turn in turn_cost:
            cost = turn_cost[turn]
            tokens = _tokens_union(turn_tokens[turn])
        built.append({
            "item_key": _item_key_for_item(conversation_key, it),
            "kind": _item_kind(it),
            "timestamp_utc": it["anchor_row"].timestamp_utc,
            "model": it["anchor_row"].model,
            "blocks": _build_item_blocks(it),
            "cost_usd": cost,
            "tokens": tokens,
        })
    page_items, page = _paginate_items(built, after=after, before=before, tail=tail, limit=limit)
    return {
        "status": "ok",
        "conversation_key": conversation_key,
        "title": _conversation_display_title(conn, conversation_key, rows),
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
    child summaries."""
    if not codex_normalization_authoritative(conn):
        return {"status": "normalization_pending", "conversation_key": conversation_key,
                "turns": [], "files": [], "children": []}
    rows = _load_conversation_rows(conn, conversation_key)
    if not rows:
        return {"status": "not_found", "conversation_key": conversation_key}
    kept, _suppressed = kern.pair_mirrors(rows)
    items = kern.canonical_items(kept)
    turns: list[dict] = []
    kind_totals: dict[str, int] = {}
    for it in items:
        anchor_text = _row_display(it["anchor_row"])
        label = _first_nonblank_line(_strip_ansi(anchor_text)) if anchor_text else ""
        kinds: dict[str, int] = {}
        for r in it["rows"]:
            kinds[r.kind] = kinds.get(r.kind, 0) + 1
            kind_totals[r.kind] = kind_totals.get(r.kind, 0) + 1
        turns.append({
            "item_key": _item_key_for_item(conversation_key, it),
            "label": label,
            "timestamp_utc": it["anchor_row"].timestamp_utc,
            "kinds": kinds,
        })
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


def _pos_to_item_key(conn: sqlite3.Connection, conversation_key: str) -> dict:
    """Map every physical row ``(source_path, line_offset)`` of a conversation to
    its canonical ``item_key`` (§6.2). Suppressed mirror members fold to their
    canonical partner's key, so both members of a pair share one item_key and can
    never double-count."""
    rows = _load_conversation_rows(conn, conversation_key)
    partners = kern.pair_mirror_partners(rows)
    kept, _suppressed = kern.pair_mirrors(rows)
    items = kern.canonical_items(kept)
    pos_map: dict[tuple, str] = {}
    for item in items:
        item_key = _item_key_for_item(conversation_key, item)
        for r in item["rows"]:
            pos_map[(r.source_path, r.line_offset)] = item_key
    for sup_idx, canon_idx in partners.items():
        sup = rows[sup_idx]
        canon = rows[canon_idx]
        canon_key = pos_map.get((canon.source_path, canon.line_offset))
        if canon_key is not None:
            pos_map[(sup.source_path, sup.line_offset)] = canon_key
    return pos_map


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
    pos_map = _pos_to_item_key(conn, conversation_key)
    by_item: dict[str, set] = {}
    for pos, labels in matched.items():
        item_key = pos_map.get(pos)
        if item_key is None:
            continue
        by_item.setdefault(item_key, set()).update(labels)
    kept, _suppressed = kern.pair_mirrors(rows)
    items = kern.canonical_items(kept)
    anchors = []
    for it in items:
        item_key = _item_key_for_item(conversation_key, it)
        if item_key in by_item:
            anchors.append({
                "item_key": item_key,
                "match_kinds": sorted(l for l in by_item[item_key] if l != "prose")})
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
    content = extracted.content_text or ""
    truncated = len(content) > _FULL_PAYLOAD_CEILING
    return content[:_FULL_PAYLOAD_CEILING], truncated


def _locate_payload_block(conn: sqlite3.Connection, conversation_key: str, block_key: str):
    """``(call_row, output_row_or_None)`` for the tool_call block addressed by
    ``block_key`` (§3.4), or ``None`` when no block matches. The output partner
    follows EXACTLY ``_item_blocks_with_rows``' folding rule (same canonical item,
    unique nonempty ``call_id``, call precedes output)."""
    rows = _load_conversation_rows(conn, conversation_key)
    if not rows:
        return None
    kept, _suppressed = kern.pair_mirrors(rows)
    items = kern.canonical_items(kept)
    for item in items:
        for _block, call_row, output_row in _item_blocks_with_rows(item):
            if call_row.kind != "tool_call":
                continue
            if _block_key_for_row(call_row) == block_key:
                return call_row, output_row
    return None


def read_codex_payload(
    conn: sqlite3.Connection, conversation_key: str, block_key: str, which: str
) -> dict:
    """Locate + full re-read for a Codex detail payload block (§3.4).

    Selector: ``block_key`` (required) + ``which ∈ {call, output}``. A call-id-less
    (or unpaired) call is call-only — ``which=output`` for it → ``not_found`` (no
    adjacency pairing is introduced). Success envelope (pinned):
    ``{"status":"ok","block_key","which","content","truncated"}`` where ``content``
    is the selected side's full text from the re-read record and ``truncated``
    reflects ``_FULL_PAYLOAD_CEILING``. ``gone`` (→ HTTP 410) means the physical
    record moved/mutated; ``not_found`` (→ 404) means no such block, no output
    partner, or a containment miss (a read is never attempted outside the root)."""
    miss = {"status": "not_found", "block_key": block_key, "which": which}
    if which not in ("call", "output"):
        return miss
    located = _locate_payload_block(conn, conversation_key, block_key)
    if located is None:
        return miss
    call_row, output_row = located
    target = call_row if which == "call" else output_row
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
    content, truncated = outcome
    return {"status": "ok", "block_key": block_key, "which": which,
            "content": content, "truncated": truncated}


# ── whole-conversation export (§3.3) ──────────────────────────────────────────


def get_codex_conversation_export(
    conn: sqlite3.Connection, conversation_key: str, *, effective_speed: str
) -> dict:
    """Whole-conversation Markdown export envelope (§3.3). Assembles the full
    detail with NO pagination (``limit=0``), then renders via the pure Codex export
    module. Status-tagged: ``ok`` (carries ``markdown``) | ``normalization_pending``
    | ``not_found`` — the dispatch/transport layers map those to bytes/HTTP."""
    detail = get_codex_conversation(
        conn, conversation_key, effective_speed=effective_speed, limit=0)
    if detail.get("status") != "ok":
        return {"status": detail.get("status"), "conversation_key": conversation_key}
    from _lib_codex_conversation_export import render_codex_conversation_markdown
    return {"status": "ok", "conversation_key": conversation_key,
            "markdown": render_codex_conversation_markdown(detail)}
