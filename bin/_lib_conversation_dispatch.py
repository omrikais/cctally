"""#294 S6 — provider-neutral conversation dispatch + Claude envelope adapter.

The thin identity-keyed dispatch S7 wires to routes (§5.1, §5.6). A bare Claude
``session_id`` routes to the EXISTING Claude query kernels (``_lib_conversation``
/ ``_lib_conversation_query`` — byte-untouched); an opaque ``IdentityV1``
conversation key routes by its ``source`` (``codex`` → the Codex kernels;
``claude`` → the bare-session path). Both providers return the SAME neutral
envelope family (browse / detail / outline / search), with provider-truthful
item/block kinds and a source-tagged ``tokens`` union.

The Claude side is a PURE per-field adapter (no Claude kernel edits): it re-shapes
the existing kernel outputs into the neutral envelopes and performs the
bidirectional cursor translation between uuid-based neutral ``item_key`` cursors
and Claude's internal rowid anchors (§5.6). All reads are adapter-level over the
untouched Claude tables via the public kernel helpers.

Public names (imported by the S7 wiring layer — do not rename):
``ConversationRef``, ``resolve_conversation_ref``, ``neutral_browse``,
``neutral_detail``, ``neutral_outline``, ``neutral_search``.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import re
import sqlite3

import _lib_codex_conversation_query as q
import _lib_conversation_query as lcq
from _lib_source_identity import canonical_identity_from_root_key

# ── constants ────────────────────────────────────────────────────────────────

# A bare Claude session id is a canonical UUID (Claude Code's sessionId shape).
# Anything that is neither an IdentityV1 key nor a UUID is garbage -> None.
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# Opaque, privacy-safe encoding of Claude's ``(session_id, uuid)`` anchor into a
# neutral ``item_key`` (§5.6 adapter table). Domain-separated + truncated hash —
# never a reversible wrap of the raw ids. Resolution is by matching against the
# assembled items' anchors (canonical rendered-anchor rule), never a decode.
CLAUDE_ITEM_KEY_DOMAIN = b"cctally-claude-item-key-v1\0"

_DEFAULT_SPEED = "standard"


# ── ConversationRef + resolution (§5.1) ───────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ConversationRef:
    """A resolved conversation reference.

    ``source``           — ``"claude"`` | ``"codex"``.
    ``conversation_key`` — opaque IdentityV1 form (minted for bare Claude session
                           ids; the input key echoed for IdentityV1 inputs).
    ``native_key``       — bare ``session_id`` (claude) / native thread id (codex).
    """

    source: str
    conversation_key: str
    native_key: str


def _mint_claude_conversation_key(session_id: str) -> str:
    """IdentityV1 ``claude``/``conversation`` key around a bare session id (§5.6).
    Unqualified (no source root / parent) so S7 can mint qualified variants
    later without a kernel change; both resolve to the same bare-session path."""
    return canonical_identity_from_root_key(
        "claude", "conversation", None, session_id, None)


def _decode_identity_v1(key: str):
    """Decode a ``v1.<b64url>`` IdentityV1 key to its payload dict, or ``None``
    when it is not a well-formed IdentityV1 (§5.1). Never raises."""
    if not isinstance(key, str) or not key.startswith("v1."):
        return None
    b64 = key[3:]
    padded = b64 + "=" * (-len(b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return None
    return payload


def resolve_conversation_ref(ref: str) -> "ConversationRef | None":
    """Resolve a neutral reference to its provider (§5.1).

    - a bare Claude ``session_id`` (UUID shape) → ``claude`` (key minted);
    - an ``IdentityV1`` ``codex``/``conversation`` key → ``codex``;
    - an ``IdentityV1`` ``claude``/``conversation`` key → ``claude`` (resolved to
      the bare-session path via ``nativeKey``);
    - invalid / non-``conversation`` / cross-kind / garbage → ``None``.

    There is NEVER a cross-provider fallback: a Codex key whose ``nativeKey`` UUID
    also exists as a Claude session resolves ``codex``-only, because routing keys
    solely on the decoded ``source`` field — collisions cannot cross-route by
    construction."""
    if not isinstance(ref, str) or not ref:
        return None
    payload = _decode_identity_v1(ref)
    if payload is not None:
        source = payload.get("source")
        kind = payload.get("resourceKind")
        native = payload.get("nativeKey")
        if kind != "conversation" or source not in ("claude", "codex"):
            return None
        if not isinstance(native, str) or not native:
            return None
        # Both codex and claude IdentityV1 inputs echo the input key; a claude
        # opaque key resolves to its bare-session native id (no re-mint).
        return ConversationRef(source=source, conversation_key=ref, native_key=native)
    if _UUID_RE.match(ref):
        return ConversationRef(
            source="claude",
            conversation_key=_mint_claude_conversation_key(ref),
            native_key=ref,
        )
    return None


# ── Claude adapter: item key + tokens union (§5.6) ────────────────────────────


def _claude_item_key(session_id: str, uuid: str | None) -> str:
    """Opaque neutral ``item_key`` over Claude's ``(session_id, uuid)`` anchor
    (§5.6). Same anchor Claude's kernels already use — the dispatch layer
    guarantees Codex and Claude keys never mix (distinct domain + prefix)."""
    raw = "\x00".join(("claude-row", session_id or "", uuid or "")).encode("utf-8")
    return "cliv1_" + hashlib.sha256(CLAUDE_ITEM_KEY_DOMAIN + raw).hexdigest()[:40]


def _claude_tokens_union(tokens: dict | None):
    """Source-tagged Claude ``tokens`` union member (§5.6). Native Claude cache
    vocabulary only — ``cache_create``/``cache_read``, never Codex fields. The
    assembly stamps ``cache_creation``; the neutral field is ``cache_create``."""
    if not isinstance(tokens, dict):
        return None
    return {
        "source": "claude",
        "input": tokens.get("input", 0) or 0,
        "output": tokens.get("output", 0) or 0,
        "cache_create": tokens.get("cache_creation", 0) or 0,
        "cache_read": tokens.get("cache_read", 0) or 0,
    }


def _sum_claude_tokens(items: list) -> dict:
    """Conversation-level Claude ``tokens`` union, summed over every assistant
    turn's per-turn ``tokens`` (§5.6)."""
    acc = {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0}
    for it in items:
        tok = it.get("tokens")
        if not isinstance(tok, dict):
            continue
        acc["input"] += tok.get("input", 0) or 0
        acc["output"] += tok.get("output", 0) or 0
        acc["cache_create"] += tok.get("cache_creation", 0) or 0
        acc["cache_read"] += tok.get("cache_read", 0) or 0
    return {"source": "claude", **acc}


# ── Claude adapter: project attribution (§5.6) ────────────────────────────────


def _claude_project_attribution(
    cwd: object, cache: dict | None = None
) -> tuple[str | None, str | None]:
    """``(project_key, project_label)`` for a Claude session's canonical project
    identity (§5.6 adapter table). Derives the OPAQUE qualified key from the
    observed ``cwd`` git-root via the ``opaque_project_key`` precedent — two
    Claude projects sharing a basename resolve to the SAME display label but
    DISTINCT ``project_key``s (their git-root bucket paths differ), so they never
    merge in neutral facets/filters. The basename label stays display-only.

    ``cache`` is ``_resolve_project_key``'s memo dict; callers resolving many
    rows MUST share one dict across the batch, or every row repeats the
    realpath + parent-``.git`` stat walk the memo exists to avoid.

    Degrades to ``(None, None)`` when ``cwd`` is absent or the S3 kernel is
    unavailable — never guesses."""
    if not isinstance(cwd, str) or not cwd:
        return None, None
    try:
        from _cctally_cache import _resolve_project_key
        from _cctally_source_analytics import _project_label
        from _lib_source_identity import source_root_key
        from _lib_source_analytics import opaque_project_key
    except Exception:
        return None, None
    project = _resolve_project_key(cwd, "git-root", cache if cache is not None else {})
    resolved_key = project.bucket_path
    cwd_label = _project_label(cwd)
    project_label = (
        cwd_label if cwd_label in {"(home)", "(root)"}
        else _project_label(project.display_key)
    )
    try:
        # The git-root bucket path fully identifies the project; deriving the
        # root key from it keeps same-basename/different-root projects distinct.
        root_key = source_root_key(resolved_key)
        return opaque_project_key("claude", root_key, resolved_key), project_label
    except ValueError:
        return None, None


# ── Claude adapter: browse (§5.6 / §6.1) ──────────────────────────────────────


def _all_claude_conversations(conn: sqlite3.Connection) -> list[dict]:
    """Every Claude browse row (all history), paging the existing rollup/live
    ``list_conversations`` kernel to completion. Reused so cost/title/models stay
    the kernel's exact values (no re-implementation)."""
    out: list[dict] = []
    offset = 0
    while True:
        res = lcq.list_conversations(conn, sort="recent", limit=200, offset=offset)
        out.extend(res["conversations"])
        page = res["page"]
        if not page.get("has_more"):
            break
        offset = page.get("next_offset")
        if offset is None:
            break
    return out


def _claude_browse_row(
    conn: sqlite3.Connection, conv: dict, cwd: object, attribution_cache: dict
) -> dict:
    project_key, project_label = _claude_project_attribution(cwd, attribution_cache)
    return {
        "conversation_key": _mint_claude_conversation_key(conv["session_id"]),
        "title": conv["title"],
        # project_key is the opaque qualified identity; project_label is the
        # display-only basename (may collide across roots — the key does not).
        "project_key": project_key,
        "project_label": project_label if project_label is not None else conv["project_label"],
        "started_utc": conv["started_utc"],
        "last_activity_utc": conv["last_activity_utc"],
        # count = Claude's existing physical message count (provider-defined
        # count semantics, §5.6 adapter table) — NOT the Codex rendered-item count.
        "count": conv["msg_count"],
        "cost_usd": conv["cost_usd"],
        "models": list(conv.get("models") or []),
        # Claude has no native conversation threading — no parent, never a fork.
        "parent": None,
        "is_fork": False,
    }


def _claude_browse(
    conn: sqlite3.Connection,
    *,
    effective_speed: str,
    project_key: str | None = None,
    model: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict:
    """Claude browse envelope (§5.6 / §6.1). Claude is always authoritative — the
    status is always ``ok`` (never ``normalization_pending``). Facets are built
    over the full row set (before filtering); rows are ordered by last activity
    and paginated by ``conversation_key`` cursor — the SAME facet/sort/paginate
    helpers the Codex browse uses (cross-provider envelope parity)."""
    del effective_speed  # Claude cost is already materialized on the rollup rows.
    convs = _all_claude_conversations(conn)
    session_ids = [c["session_id"] for c in convs]
    meta = lcq._session_latest_meta_map(conn, session_ids)
    # One shared memo dict for the whole batch: many sessions share a cwd, and
    # _resolve_project_key's fast-path only helps when the dict persists.
    attribution_cache: dict = {}
    rows = [
        _claude_browse_row(
            conn,
            conv,
            meta.get(conv["session_id"], (None, None))[0],
            attribution_cache,
        )
        for conv in convs
    ]
    facets = q._browse_facets(rows)
    filtered = [
        row for row in rows
        if (project_key is None or row["project_key"] == project_key)
        and (model is None or model in (row["models"] or []))
    ]
    filtered.sort(key=q._recent_sort_key, reverse=True)
    page_rows, page = q._paginate_rows(filtered, cursor=cursor, limit=limit)
    return {"status": "ok", "rows": page_rows, "facets": facets, "page": page}


# ── Claude adapter: detail with cursor translation (§5.6) ─────────────────────


def _map_claude_item(session_id: str, it: dict) -> dict:
    """One Claude assembled item → the neutral detail item shape (§5.6). Claude's
    kinds/blocks pass through untranslated (both vocabularies are provider-truthful
    values of the same required field)."""
    return {
        "item_key": _claude_item_key(session_id, it["anchor"]["uuid"]),
        "kind": it["kind"],
        "timestamp_utc": it.get("ts"),
        "model": it.get("model"),
        "blocks": it.get("blocks", []),
        "cost_usd": it.get("cost_usd"),
        "tokens": _claude_tokens_union(it.get("tokens")),
    }


def _resolve_claude_cursor(items: list, keys: list, cursor: str | None):
    """Translate a neutral ``item_key`` cursor to Claude's internal rowid anchor
    via the canonical rendered-anchor rule (§5.6). Returns
    ``(internal_id, "ok")`` when the cursor matches an emitted item anchor, or
    ``(None, "not_found")`` when the uuid is physically present but folded out of
    the rendered items, or absent entirely (pruned/rewritten history) — never a
    silent restart, never a mistaken-stale from an arbitrary duplicate."""
    if cursor is None:
        return None, "ok"
    try:
        idx = keys.index(cursor)
    except ValueError:
        return None, "not_found"
    return items[idx]["anchor"]["id"], "ok"


def _claude_detail(
    conn: sqlite3.Connection,
    session_id: str,
    conversation_key: str,
    *,
    after: str | None = None,
    before: str | None = None,
    tail: object = None,
    limit: int | None = None,
) -> dict:
    """Claude detail envelope (§5.6). Never emits ``normalization_pending``
    (Claude is always authoritative). ``unattributed_cost_usd`` is absent (Codex
    only); ``children``/``parent`` are empty/None (no native threading). Cursors
    round-trip through the uuid-based ``item_key`` anchor."""
    asm = lcq._assemble_session_memoized(conn, session_id)
    if asm is None:
        return {"status": "not_found", "conversation_key": conversation_key}
    items_all = asm["items"]
    # The canonical rendered anchors (assembly already dedups duplicate uuids and
    # promotes the rendered fragment); the cursor must match one of THESE.
    keys = [_claude_item_key(session_id, it["anchor"]["uuid"]) for it in items_all]
    internal_after, st = _resolve_claude_cursor(items_all, keys, after)
    if st == "not_found":
        return {"status": "not_found", "conversation_key": conversation_key}
    internal_before, st = _resolve_claude_cursor(items_all, keys, before)
    if st == "not_found":
        return {"status": "not_found", "conversation_key": conversation_key}
    res = lcq.get_conversation(
        conn, session_id,
        after=internal_after, before=internal_before,
        tail=bool(tail), limit=limit if limit is not None else 500,
    )
    page_items = res["items"]
    neutral_items = [_map_claude_item(session_id, it) for it in page_items]
    if page_items:
        has_after = bool(res["page"]["has_more"])
        has_before = bool(res["page"]["has_prev"])
        after_cur = _claude_item_key(session_id, page_items[-1]["anchor"]["uuid"]) if has_after else None
        before_cur = _claude_item_key(session_id, page_items[0]["anchor"]["uuid"]) if has_before else None
    else:
        has_after = has_before = False
        after_cur = before_cur = None
    page = {
        "total": len(items_all), "returned": len(page_items),
        "before": before_cur, "after": after_cur,
        "has_before": has_before, "has_after": has_after,
    }
    return {
        "status": "ok",
        "conversation_key": conversation_key,
        "title": res["title"],
        "items": neutral_items,
        "page": page,
        "children": [],
        "parent": None,
        "total_cost_usd": res["cost_usd"],
        "tokens": _sum_claude_tokens(items_all),
    }


# ── Claude adapter: outline (§5.6) ────────────────────────────────────────────


def _claude_outline(
    conn: sqlite3.Connection, session_id: str, conversation_key: str,
) -> dict:
    """Claude outline envelope (§5.6). Reuses the existing kernel outline for
    ``stats``/``files``; the per-turn ``item_key`` + block-kind counts are built
    from the SAME assembled items the detail pages, so outline and detail item
    keys align exactly. ``children`` is empty (no native threading)."""
    o = lcq.get_conversation_outline(conn, session_id)
    if o is None:
        return {"status": "not_found", "conversation_key": conversation_key}
    asm = lcq._assemble_session_memoized(conn, session_id)
    turns = []
    for it in asm["items"]:
        kinds: dict[str, int] = {}
        for b in it.get("blocks", []):
            bk = b.get("kind")
            if bk:
                kinds[bk] = kinds.get(bk, 0) + 1
        turns.append({
            "item_key": _claude_item_key(session_id, it["anchor"]["uuid"]),
            "label": lcq._outline_label(it.get("text", "")),
            "timestamp_utc": it.get("ts"),
            "kinds": kinds,
        })
    return {
        "status": "ok",
        "conversation_key": conversation_key,
        "turns": turns,
        "stats": o["stats"],
        "files": o["files"],
        "children": [],
    }


# ── Claude adapter: search (§5.6 / §6.2) ──────────────────────────────────────


def _claude_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str = "all",
    limit: int = 20,
    cursor: str | None = None,
) -> dict:
    """Claude search envelope (§5.6). Reuses the existing Claude search kernel
    (FTS5/LIKE, the #149 shape); hits are re-shaped to the neutral union with a
    ``(session_id, uuid)`` ``item_key`` and cursor pagination over the kernel's
    offset paging."""
    try:
        offset = int(cursor) if cursor is not None else 0
    except (TypeError, ValueError):
        offset = 0
    res = lcq.search_conversations(
        conn, query, kind=kind, limit=limit, offset=max(0, offset))
    hits = []
    for h in res.get("hits", []):
        sid = h.get("session_id")
        uuid = h.get("uuid")
        hits.append({
            "conversation_key": _mint_claude_conversation_key(sid) if sid else None,
            "item_key": _claude_item_key(sid, uuid) if (sid and uuid) else None,
            "title": h.get("title"),
            "snippet": h.get("snippet"),
            "badges": list(h.get("match_kinds") or []),
        })
    total = res.get("total", len(hits))
    returned = len(hits)
    next_offset = offset + returned
    next_cursor = str(next_offset) if next_offset < total else None
    return {
        "status": "ok",
        "query": res.get("query", query),
        "hits": hits,
        "total": total,
        "mode": res.get("mode"),
        "depth": res.get("search_depth"),
        "page": {"returned": returned, "cursor": next_cursor},
    }


# ── neutral entry points (§5.6) ───────────────────────────────────────────────


def neutral_browse(
    conn: sqlite3.Connection, *, source: str, effective_speed: str | None = None,
    **filters,
) -> dict:
    """Browse envelope for one source (§5.6). Codex routes to
    ``list_codex_conversations`` (with the ``normalization_pending`` status while
    migration 025 has not run); Claude routes to the adapter (always ``ok``).
    ``filters``: ``project_key``, ``model``, ``limit``, ``cursor``."""
    speed = effective_speed or _DEFAULT_SPEED
    if source == "codex":
        return q.list_codex_conversations(conn, effective_speed=speed, **filters)
    if source == "claude":
        return _claude_browse(conn, effective_speed=speed, **filters)
    raise ValueError(f"unknown source: {source!r}")


def neutral_detail(
    conn: sqlite3.Connection, ref: str, *, effective_speed: str | None = None,
    after: str | None = None, before: str | None = None,
    tail: object = None, limit: int | None = None,
) -> dict:
    """Detail envelope for a neutral reference (§5.6). Unknown/garbage refs → the
    ``not_found`` envelope echoing the requested reference."""
    cref = resolve_conversation_ref(ref)
    if cref is None:
        return {"status": "not_found", "conversation_key": ref}
    speed = effective_speed or _DEFAULT_SPEED
    if cref.source == "codex":
        return q.get_codex_conversation(
            conn, cref.conversation_key, effective_speed=speed,
            after=after, before=before, tail=tail,
            limit=limit if limit is not None else 200)
    return _claude_detail(
        conn, cref.native_key, cref.conversation_key,
        after=after, before=before, tail=tail, limit=limit)


def neutral_outline(
    conn: sqlite3.Connection, ref: str, *, effective_speed: str | None = None,
) -> dict:
    """Outline envelope for a neutral reference (§5.6)."""
    cref = resolve_conversation_ref(ref)
    if cref is None:
        return {"status": "not_found", "conversation_key": ref}
    speed = effective_speed or _DEFAULT_SPEED
    if cref.source == "codex":
        return q.get_codex_conversation_outline(
            conn, cref.conversation_key, effective_speed=speed)
    return _claude_outline(conn, cref.native_key, cref.conversation_key)


def neutral_search(
    conn: sqlite3.Connection, query: str, *, source: str, kind: str = "all",
    effective_speed: str | None = None, limit: int = 20, cursor: str | None = None,
) -> dict:
    """Search envelope for one source (§5.6). Search is per-source by
    construction — the kernels never merge providers."""
    speed = effective_speed or _DEFAULT_SPEED
    if source == "codex":
        return q.search_codex_conversations(
            conn, query, kind=kind, effective_speed=speed,
            limit=limit, cursor=cursor)
    if source == "claude":
        return _claude_search(conn, query, kind=kind, limit=limit, cursor=cursor)
    raise ValueError(f"unknown source: {source!r}")
