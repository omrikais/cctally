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
import sqlite3

import _lib_codex_conversation as kern
from _lib_conversation import _strip_ansi
from _lib_conversation_query import _first_nonblank_line
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

CODEX_SEARCH_KINDS = ("all", "prompts", "assistant", "tools", "thinking", "title", "files")

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
    """True iff migration 025 is stamped applied — i.e. the normalized corpus is
    authoritative (§3.5). A missing ``schema_migrations`` table (bare
    ``_apply_cache_schema`` conn, or a pre-migration cache) reads as pending."""
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


def _build_item_blocks(item: dict) -> list[dict]:
    """Assemble an item's blocks, folding each ``tool_output`` into its
    ``tool_call`` block via ``call_id`` when that call_id has exactly one owner
    (§5.2). Physical order within the item is preserved."""
    rows = item["rows"]
    call_owner_count: dict[str, int] = {}
    for r in rows:
        if r.kind == "tool_call" and r.call_id:
            call_owner_count[r.call_id] = call_owner_count.get(r.call_id, 0) + 1
    blocks: list[dict] = []
    tool_block_by_call: dict[str, int] = {}
    for r in rows:
        text = _row_display(r)
        detail = _parse_detail(r.detail_json)
        if (r.kind == "tool_output" and r.call_id
                and call_owner_count.get(r.call_id, 0) == 1
                and r.call_id in tool_block_by_call):
            owner = blocks[tool_block_by_call[r.call_id]]
            owner["output"] = {"text": text, "detail": detail}
            continue
        block = {
            "kind": r.kind, "text": text, "detail": detail,
            "call_id": r.call_id, "timestamp_utc": r.timestamp_utc,
        }
        if r.kind == "tool_call" and r.call_id and call_owner_count.get(r.call_id, 0) == 1:
            tool_block_by_call[r.call_id] = len(blocks)
        blocks.append(block)
    return blocks


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
        title = _conversation_display_title(conn, ck)
        for source_path, line_offset, kind, disp in mrows:
            item_key = pos_map.get((source_path, line_offset))
            if item_key is None:
                continue
            hit = collapsed.setdefault(
                (ck, item_key),
                {"conversation_key": ck, "item_key": item_key, "title": title,
                 "snippet": None, "_badges": set()})
            hit["_badges"].add(_badge_for_kind(kind))
            if hit["snippet"] is None:
                hit["snippet"] = _excerpt(disp)
    return [
        {"conversation_key": h["conversation_key"], "item_key": h["item_key"],
         "title": h["title"], "snippet": h["snippet"], "badges": sorted(h["_badges"])}
        for h in collapsed.values()
    ]


def _search_title(conn: sqlite3.Connection, query: str) -> list[dict]:
    """Title search over the rollup table — identical LIKE semantics in both FTS
    and LIKE modes (§6.2). Conversation-level hits (no item anchor)."""
    like = f"%{query}%"
    return [
        {"conversation_key": ck, "item_key": None, "title": title,
         "snippet": _excerpt(title), "badges": ["title"]}
        for ck, title in conn.execute(
            "SELECT conversation_key, title FROM codex_conversation_rollups "
            "WHERE title LIKE ?", (like,))
    ]


def _search_files(conn: sqlite3.Connection, query: str) -> list[dict]:
    """File-touch search — matches file paths, collapsed to the owning message's
    canonical item_key (§6.2)."""
    like = f"%{query}%"
    pos_cache: dict[str, dict] = {}
    title_cache: dict[str, str] = {}
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
            title_cache[ck] = _conversation_display_title(conn, ck)
        item_key = pos_cache[ck].get((member[0], member[1]))
        hit = collapsed.setdefault(
            (ck, item_key),
            {"conversation_key": ck, "item_key": item_key, "title": title_cache[ck],
             "snippet": _excerpt(file_path), "badges": ["files"]})
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
