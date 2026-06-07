"""Pure query kernel for the conversation viewer endpoints (Plan 2, spec §3).

Takes a sqlite3.Connection over a cache.db that already holds Plan 1's
conversation_messages (+ FTS) and session_entries. No clock, no network, no
global mutation — unit-tested against an in-memory cache.db seeded by
_apply_cache_schema. Three entry points back the three GET routes:
list_conversations (rail), get_conversation (reader), search_conversations.

Cost is joined ONCE per logical assistant turn (msg_id, req_id) to the single
deduped session_entries row (idx_entries_dedup), via the shared pricing helper
— never per physical fragment and never from cost_usd_raw (often NULL).
"""
from __future__ import annotations
import json as _json
import os
import sqlite3

# Public surface (Plan 2): shipped in the npm tarball + brew formula + public
# mirror — imported by the dashboard's conversation endpoints at runtime.

from _lib_pricing import _calculate_entry_cost


def _project_label(cwd) -> str:
    """Basename of the project cwd (dashboard label posture — no reveal). Falls
    back to the raw path for root-ish cwds, '' when absent."""
    if not cwd:
        return ""
    return os.path.basename(cwd.rstrip("/")) or cwd


def _subagent_key(source_path):
    """Privacy-safe subagent-thread identity for the reader. Each subagent (Task)
    invocation writes its own ``agent-<hash>.jsonl``; the main session is
    ``<session_id>.jsonl``. Returns the agent hash (``agent-`` prefix + ``.jsonl``
    suffix stripped; an ``acompact-`` middle is kept), or ``None`` for the main
    file / a non-agent path. We expose ONLY this derived key — never the raw
    absolute ``source_path`` (which leaks home dir / username / encoded project,
    and the conversation routes are LAN-exposable via dashboard.expose_transcripts)."""
    if not source_path:
        return None
    base = os.path.basename(source_path)
    if not base.startswith("agent-"):
        return None
    stem = base[len("agent-"):]
    if stem.endswith(".jsonl"):
        stem = stem[: -len(".jsonl")]
    return stem or None


def _entry_cost(model, inp, out, cc, cr, cost_usd_raw) -> float:
    """Cost for one session_entries row via the shared pricing helper. Tokens →
    the helper's usage dict. cost_usd_raw is passed as the optional override the
    helper already understands (it is often NULL — never the primary source)."""
    usage = {
        "input_tokens": inp or 0,
        "output_tokens": out or 0,
        "cache_creation_input_tokens": cc or 0,
        "cache_read_input_tokens": cr or 0,
    }
    return _calculate_entry_cost(model or "", usage, cost_usd=cost_usd_raw)


def _session_cost_map(conn, session_ids):
    """{session_id: total_cost_usd} for the given sessions. Joins
    conversation_messages turn keys to the single deduped session_entries row
    per (msg_id, req_id), so a turn replayed across files contributes once.
    (msg_id, req_id) is globally unique in session_entries and maps to exactly
    one session_id, so per-session sums are clean."""
    costs = {sid: 0.0 for sid in session_ids}
    if not session_ids:
        return costs
    placeholders = ",".join("?" for _ in session_ids)
    sql = (
        "SELECT cm.session_id, se.model, se.input_tokens, se.output_tokens, "
        "       se.cache_create_tokens, se.cache_read_tokens, se.cost_usd_raw "
        "FROM (SELECT DISTINCT session_id, msg_id, req_id "
        "      FROM conversation_messages "
        "      WHERE session_id IN (%s) AND msg_id IS NOT NULL AND req_id IS NOT NULL) cm "
        "JOIN session_entries se ON se.msg_id = cm.msg_id AND se.req_id = cm.req_id"
        % placeholders
    )
    for sid, model, inp, out, cc, cr, raw in conn.execute(sql, list(session_ids)):
        costs[sid] = costs.get(sid, 0.0) + _entry_cost(model, inp, out, cc, cr, raw)
    return costs


def _session_models_map(conn, session_ids):
    """{session_id: sorted distinct non-null models}."""
    out = {sid: [] for sid in session_ids}
    if not session_ids:
        return out
    placeholders = ",".join("?" for _ in session_ids)
    sql = (
        "SELECT DISTINCT session_id, model FROM conversation_messages "
        "WHERE session_id IN (%s) AND model IS NOT NULL AND model != '' "
        "ORDER BY model" % placeholders
    )
    for sid, model in conn.execute(sql, list(session_ids)):
        out.setdefault(sid, []).append(model)
    return out


def _session_latest_meta_map(conn, session_ids):
    """{session_id: (cwd, git_branch)} using the most-recent NON-NULL value per
    column — the SAME posture as get_conversation's _latest, so the rail and the
    reader agree on a session whose cwd/branch changed over its lifetime (a plain
    MAX() picks the lexical max, not the latest). Bounded to the page's sessions
    via per-session correlated lookups over idx (session_id, timestamp_utc, id),
    mirroring _session_cost_map / _session_models_map."""
    meta = {sid: (None, None) for sid in session_ids}
    if not session_ids:
        return meta
    placeholders = ",".join("?" for _ in session_ids)
    sql = (
        "SELECT s.session_id, "
        "  (SELECT c.cwd FROM conversation_messages c "
        "   WHERE c.session_id = s.session_id AND c.cwd IS NOT NULL "
        "   ORDER BY c.timestamp_utc DESC, c.id DESC LIMIT 1), "
        "  (SELECT b.git_branch FROM conversation_messages b "
        "   WHERE b.session_id = s.session_id AND b.git_branch IS NOT NULL "
        "   ORDER BY b.timestamp_utc DESC, b.id DESC LIMIT 1) "
        "FROM (SELECT DISTINCT session_id FROM conversation_messages "
        "      WHERE session_id IN (%s)) s" % placeholders
    )
    for sid, cwd, branch in conn.execute(sql, list(session_ids)):
        meta[sid] = (cwd, branch)
    return meta


_SORTS = {
    "recent": "MAX(timestamp_utc) DESC, session_id DESC",
    "oldest": "MIN(timestamp_utc) ASC, session_id ASC",
}


def list_conversations(conn, *, sort="recent", limit=50, offset=0) -> dict:
    """All-history per-session browse rows (spec §3.1). NOT 365-day bounded."""
    order = _SORTS.get(sort, _SORTS["recent"])
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    rows = conn.execute(
        "SELECT session_id, COUNT(*) AS msg_count, "
        "       MIN(timestamp_utc) AS started, MAX(timestamp_utc) AS last_activity "
        "FROM conversation_messages "
        "WHERE session_id IS NOT NULL "
        "GROUP BY session_id "
        "ORDER BY " + order + " LIMIT ? OFFSET ?",
        (limit + 1, offset),
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    session_ids = [r[0] for r in rows]
    costs = _session_cost_map(conn, session_ids)
    models = _session_models_map(conn, session_ids)
    # cwd/git_branch as the latest non-null (reader posture), NOT a lexical MAX().
    meta = _session_latest_meta_map(conn, session_ids)
    conversations = [
        {
            "session_id": sid,
            "project_label": _project_label(meta.get(sid, (None, None))[0]),
            "git_branch": meta.get(sid, (None, None))[1],
            "started_utc": started,
            "last_activity_utc": last_activity,
            "msg_count": msg_count,
            "cost_usd": round(costs.get(sid, 0.0), 6),
            "models": models.get(sid, []),
        }
        for (sid, msg_count, started, last_activity) in rows
    ]
    return {
        "conversations": conversations,
        "page": {
            "next_offset": offset + len(conversations) if has_more else None,
            "has_more": has_more,
        },
    }


def _turn_cost_map(conn, turn_keys):
    """{(msg_id, req_id): cost_usd} for the given non-null turn keys, joined ONCE
    to the deduped session_entries row. Keys absent from session_entries (e.g.
    <synthetic> walker-skipped rows) are simply not present → cost 0 by omission."""
    costs = {}
    keys = [(m, r) for (m, r) in turn_keys if m is not None and r is not None]
    if not keys:
        return costs
    # Chunk the OR-of-pairs to stay well under SQLite's variable limit.
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        cond = " OR ".join("(msg_id=? AND req_id=?)" for _ in chunk)
        params = [v for pair in chunk for v in pair]
        sql = ("SELECT msg_id, req_id, model, input_tokens, output_tokens, "
               "cache_create_tokens, cache_read_tokens, cost_usd_raw "
               "FROM session_entries WHERE " + cond)
        for m, r, model, inp, out, cc, cr, raw in conn.execute(sql, params):
            costs[(m, r)] = _entry_cost(model, inp, out, cc, cr, raw)
    return costs


def get_conversation(conn, session_id, *, after=None, limit=500):
    """Reader payload for one session (spec §3.2). Returns None for an unknown
    session. Dedups logical messages by (session_id, uuid) (canonical = earliest
    timestamp), groups assistant fragments into turn items by (msg_id, req_id),
    joins cost once, anchors a turn on its prose-bearing fragment, and exposes
    every member fragment uuid for jump resolution. Cursor over (timestamp_utc,
    id); ~500 items/page."""
    limit = max(1, min(int(limit), 1000))
    exists = conn.execute(
        "SELECT 1 FROM conversation_messages WHERE session_id=? LIMIT 1",
        (session_id,)).fetchone()
    if exists is None:
        return None

    # Pull the session ordered; dedup logical messages by (session_id, uuid),
    # canonical row = earliest (timestamp_utc, id). Replays carry the original
    # uuid, so the first occurrence in ascending order is canonical.
    raw = conn.execute(
        "SELECT id, uuid, timestamp_utc, entry_type, text, blocks_json, model, "
        "       msg_id, req_id, is_sidechain, cwd, git_branch, source_path, parent_uuid "
        "FROM conversation_messages WHERE session_id=? "
        "ORDER BY timestamp_utc, id", (session_id,)).fetchall()

    seen_uuid = set()
    logical = []   # canonical physical rows, in order
    for row in raw:
        u = row[1]
        if u in seen_uuid:
            continue
        seen_uuid.add(u)
        logical.append(row)

    # Group assistant fragments sharing (msg_id, req_id) into one turn item over
    # the WHOLE logical list — NOT by adjacency. Real tool-using transcripts
    # interleave a tool_result (a `user`/tool_result item) between fragments of
    # the SAME turn, so the same key recurs non-consecutively. We keep a turn-key
    # → item-index map: first occurrence emits the turn item AT THIS POSITION;
    # later same-key fragments fold their blocks/prose/uuids into the existing
    # item. A turn → exactly ONE item → cost counted exactly once. Humans,
    # tool_results, and assistant rows with a null msg_id emit as simple items at
    # their own position.
    items = []
    turn_index = {}        # (msg_id, req_id) -> index into items
    for row in logical:
        (rid, u, ts, etype, text, blocks, model, msg_id, req_id,
         is_sc, cwd, branch, source_path, parent_uuid) = row
        if etype == "assistant" and msg_id is not None:
            key = (msg_id, req_id)
            idx = turn_index.get(key)
            if idx is None:
                turn_index[key] = len(items)
                items.append(_build_turn([row]))
            else:
                _extend_turn(items[idx], row)
        else:
            items.append(_build_simple(row))

    costs = _turn_cost_map(conn, list(turn_index))
    # Stamp per-item cost first, then derive the header from the SUM of the
    # ROUNDED per-item assistant costs (M2) — so the §6.5 invariant
    # sum(items.cost_usd) == header cost_usd holds EXACTLY to 1e-9 by
    # construction OVER THE FULL ITEM LIST. 6dp is the deliberate JSON display
    # precision. NOTE: the header is the whole-session total; the returned
    # ``items`` is a page subset, so on page 2+ sum(page) < header by design.
    header_cost = 0.0
    for it in items:
        if it["kind"] == "assistant" and "_msg_id" in it:
            turn_cost = round(costs.get((it["_msg_id"], it["_req_id"]), 0.0), 6)
            it["cost_usd"] = turn_cost
            header_cost += turn_cost
            del it["_msg_id"]
            del it["_req_id"]
            it.pop("_has_prose", None)
    header_cost = round(header_cost, 6)

    # Cursor pagination over the item list (anchored to each item's canonical id).
    # A non-None `after` that matches no item's anchor (stale/deleted cursor)
    # yields an EMPTY page — never silently re-serves the head (M1).
    start = 0
    if after is not None:
        start = None
        for k, it in enumerate(items):
            if str(it["anchor"]["id"]) == str(after):
                start = k + 1
                break
        if start is None:
            return {
                "session_id": session_id,
                "project_label": _project_label(_latest(logical, 10)),
                "git_branch": _latest(logical, 11),
                "started_utc": logical[0][2],
                "last_activity_utc": logical[-1][2],
                "cost_usd": header_cost,
                "models": sorted({r[6] for r in logical if r[6]}),
                "items": [],
                "page": {"next_after": None, "has_more": False},
            }
    page = items[start:start + limit]
    has_more = start + limit < len(items)
    next_after = page[-1]["anchor"]["id"] if (page and has_more) else None

    # Stamp the session_id into each anchor (spec anchor is (session_id, uuid);
    # the dict literals are built session-agnostic, so fill it here where the
    # session id is known). NOT a no-op — the endpoint/clients rely on it.
    for it in page:
        it["anchor"]["session_id"] = session_id

    first = logical[0]
    last = logical[-1]
    models = sorted({r[6] for r in logical if r[6]})
    return {
        "session_id": session_id,
        "project_label": _project_label(_latest(logical, 10)),
        "git_branch": _latest(logical, 11),
        "started_utc": first[2],
        "last_activity_utc": last[2],
        "cost_usd": header_cost,
        "models": models,
        "items": page,
        "page": {"next_after": next_after, "has_more": has_more},
    }


def _latest(logical, col):
    """Most-recent non-null value in a column across the session (project/branch
    show the latest, matching the dashboard's session posture)."""
    for row in reversed(logical):
        if row[col]:
            return row[col]
    return "" if col == 10 else None


def _build_turn(members):
    """Seed a turn item from its first fragment(s). Prose = joined non-empty
    fragment text; anchor/model = the prose-bearing fragment (empirically exactly
    one per turn); member_uuids = all fragment uuids. Fragments arriving later
    (possibly non-consecutive — interleaved with a tool_result) fold in via
    _extend_turn, which re-promotes the anchor/model once a prose fragment lands."""
    first = members[0]
    item = {
        "kind": "assistant",
        "anchor": {"session_id": None, "uuid": first[1], "id": first[0]},
        "member_uuids": [first[1]],
        "ts": first[2],
        "text": "",
        "blocks": [],
        "model": first[6],
        "is_sidechain": bool(first[9]),
        # subagent_key / parent_uuid are SEED-sourced (the first fragment, the
        # turn's entry point) and NOT re-promoted in _fold_fragment — the prose
        # anchor's parent_uuid is an intra-turn link, not the entry point (Codex
        # P1). subagent_key is uniform across a turn's fragments (one file).
        "subagent_key": _subagent_key(first[12]),
        "parent_uuid": first[13],
        "_msg_id": first[7],
        "_req_id": first[8],
        "_has_prose": False,
    }
    _fold_fragment(item, first)
    for m in members[1:]:
        _extend_turn(item, m)
    return item


def _extend_turn(item, row):
    """Fold one more same-turn assistant fragment into an existing turn item:
    append its uuid + blocks + non-empty prose. The FIRST fragment carrying prose
    promotes the anchor/model to itself (the prose-bearing fragment is the
    canonical anchor); subsequent prose fragments only extend the joined text."""
    item["member_uuids"].append(row[1])
    _fold_fragment(item, row)


def _fold_fragment(item, row):
    blocks = item["blocks"]
    try:
        blocks.extend(_json.loads(row[5] or "[]"))
    except (ValueError, TypeError):
        pass
    frag_text = (row[4] or "").strip()
    if frag_text:
        if not item["_has_prose"]:
            # First prose fragment becomes the canonical anchor / model.
            item["anchor"]["uuid"] = row[1]
            item["anchor"]["id"] = row[0]
            item["model"] = row[6]
            item["is_sidechain"] = bool(row[9])
            item["_msg_id"] = row[7]
            item["_req_id"] = row[8]
            item["_has_prose"] = True
            item["text"] = frag_text
        else:
            item["text"] = item["text"] + "\n" + frag_text


def _build_simple(row):
    """A human, tool_result, or assistant-with-null-msg_id item (no turn grouping,
    no cost). An assistant row routes here only when its msg_id is NULL (no turn
    key → no session_entries join); it carries an explicit cost_usd of 0.0 and NO
    internal _msg_id/_req_id keys, so the cost loop's KeyError path can never fire
    (I2). The model is preserved for assistant rows."""
    (rid, u, ts, etype, text, blocks, model, msg_id, req_id, is_sc, cwd, branch,
     source_path, parent_uuid) = row
    try:
        parsed = _json.loads(blocks or "[]")
    except (ValueError, TypeError):
        parsed = []
    item = {
        "kind": etype,
        "anchor": {"session_id": None, "uuid": u, "id": rid},
        "member_uuids": [u],
        "ts": ts,
        "text": text,
        "blocks": parsed,
        "is_sidechain": bool(is_sc),
        "subagent_key": _subagent_key(source_path),
        "parent_uuid": parent_uuid,
    }
    if etype == "assistant":
        item["model"] = model
        item["cost_usd"] = 0.0
    return item


def _fts_flag_unavailable(conn) -> bool:
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key='fts5_unavailable'").fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0])


def search_conversations(conn, query, *, limit=50, offset=0,
                         fts_available=None) -> dict:
    """Cross-session search (spec §3.3). Uses FTS5 when available (bm25 rank +
    snippet); else a LIKE scan with a manual snippet. Hits deduped by
    (session_id, uuid); each carries the turn's cost. `fts_available` overrides
    detection (test seam / explicit LIKE)."""
    q = (query or "").strip()
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    if fts_available is None:
        fts_available = not _fts_flag_unavailable(conn)
    if not q:
        return {"query": q, "mode": "fts" if fts_available else "like",
                "hits": [], "total": 0}
    if fts_available:
        try:
            return _search_fts(conn, q, limit, offset)
        except sqlite3.OperationalError:
            pass   # corrupt/missing FTS at query time → fall through to LIKE
    return _search_like(conn, q, limit, offset)


def _row_to_hit(uuid_, sid, ts, cwd, snippet, msg_id, req_id):
    """Build one hit WITHOUT cost — cost is batched onto the FINAL page in
    _attach_costs (I1: no per-hit _turn_cost_map round-trip). The turn key rides
    on the private `_turn_key` field until the batch maps it to `cost_usd`."""
    return {
        "session_id": sid,
        "uuid": uuid_,
        "project_label": _project_label(cwd),
        "ts": ts,
        "snippet": snippet,
        "_turn_key": (msg_id, req_id) if msg_id is not None and req_id is not None
                     else None,
    }


def _attach_costs(conn, page):
    """Compute turn cost for the FINAL page's hits in ONE _turn_cost_map call,
    then map it onto each hit and drop the private `_turn_key`. Off-page and
    duplicate hits never reach here, so we never compute cost for them (I1)."""
    keys = [h["_turn_key"] for h in page if h.get("_turn_key") is not None]
    costs = _turn_cost_map(conn, keys) if keys else {}
    for h in page:
        tk = h.pop("_turn_key", None)
        h["cost_usd"] = round(costs.get(tk, 0.0), 6) if tk is not None else 0.0
    return page


def _like_pattern(q):
    """Build the LIKE pattern for `q`. Escape the ESCAPE char (\\) FIRST, then
    the wildcards — otherwise a query containing a backslash (incl. a trailing
    one) mis-escapes the appended '%' and the LIKE silently matches nothing
    (paired with ESCAPE '\\' in the queries below)."""
    return ("%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            + "%")


def _fts_snippets(conn, fts_q, ids):
    """{rowid: snippet} for the page rowids ONLY (#149). snippet() needs an
    active MATCH, so it can't be deferred to an outer query over the page CTE;
    a second bounded MATCH restricted to the page rowids generates snippets for
    at most one page of hits instead of every corpus match."""
    if not ids:
        return {}
    ph = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT cm.id, snippet(conversation_fts, 0, '[', ']', ' … ', 12) "
        "FROM conversation_fts "
        "JOIN conversation_messages cm ON cm.id = conversation_fts.rowid "
        f"WHERE conversation_fts MATCH ? AND cm.id IN ({ph})",
        (fts_q, *ids),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _texts_for_ids(conn, ids):
    """{rowid: text} for the page rowids ONLY (#149) — the LIKE page query omits
    `text` so we never pull every matched row's body into Python; this fetches
    it for just the page so `_manual_snippet` runs at most `limit` times."""
    if not ids:
        return {}
    ph = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, text FROM conversation_messages WHERE id IN ({ph})",
        tuple(ids),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _search_fts(conn, q, limit, offset):
    # All of dedup + paging + total live in SQL (#149) so Python never holds
    # more than one page of hits/snippets, regardless of corpus match count.
    fts_q = _fts_query(q)
    # Exact post-dedup logical total — counted in C with no snippet generation
    # and no Python row materialization.
    total = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT DISTINCT cm.session_id, cm.uuid "
        "  FROM conversation_fts "
        "  JOIN conversation_messages cm ON cm.id = conversation_fts.rowid "
        "  WHERE conversation_fts MATCH ?)",
        (fts_q,),
    ).fetchone()[0]
    # One row per logical (session_id, uuid): ROW_NUMBER()=1 keeps the SAME row
    # the old Python dedup kept as its FIRST occurrence (order: bm25, ts DESC,
    # id DESC — cm.id is the final deterministic tiebreaker), so the surviving
    # snippet/cost and the page boundary stay byte-stable. bm25 still ranks
    # across all matches (inherent to relevance ordering).
    #
    # bm25 is materialized as a plain `rank` column in the inner `matched` CTE
    # before the window function runs: FTS5 auxiliary functions (bm25/snippet)
    # may only be used directly against the MATCH query, NOT inside a window
    # ORDER BY ("unable to use function bm25 in the requested context").
    page = conn.execute(
        "WITH matched AS ("
        "  SELECT cm.id AS rid, cm.session_id AS sid, cm.uuid AS uuid, "
        "         cm.timestamp_utc AS ts, cm.cwd AS cwd, "
        "         cm.msg_id AS mid, cm.req_id AS rqd, "
        "         bm25(conversation_fts) AS rank "
        "  FROM conversation_fts "
        "  JOIN conversation_messages cm ON cm.id = conversation_fts.rowid "
        "  WHERE conversation_fts MATCH ?), "
        "ranked AS ("
        "  SELECT *, ROW_NUMBER() OVER ("
        "             PARTITION BY sid, uuid ORDER BY rank, ts DESC, rid DESC"
        "           ) AS rn "
        "  FROM matched) "
        "SELECT rid, sid, uuid, ts, cwd, mid, rqd FROM ranked WHERE rn = 1 "
        "ORDER BY rank, ts DESC, rid DESC LIMIT ? OFFSET ?",
        (fts_q, limit, offset),
    ).fetchall()
    snips = _fts_snippets(conn, fts_q, [r[0] for r in page])
    hits = [_row_to_hit(uuid, sid, ts, cwd, snips.get(rid, ""), mid, rqd)
            for (rid, sid, uuid, ts, cwd, mid, rqd) in page]
    return {"query": q, "mode": "fts", "hits": _attach_costs(conn, hits),
            "total": total}


def _search_like(conn, q, limit, offset):
    # SQL-bounded mirror of _search_fts for the no-FTS5 fallback (#149); the
    # COUNT + page each scan the table once (the degraded path already lacks an
    # index for the substring match).
    like = _like_pattern(q)
    total = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT DISTINCT session_id, uuid FROM conversation_messages "
        "  WHERE text LIKE ? ESCAPE '\\' AND text != '')",
        (like,),
    ).fetchone()[0]
    page = conn.execute(
        "WITH ranked AS ("
        "  SELECT id AS rid, session_id AS sid, uuid AS uuid, "
        "         timestamp_utc AS ts, cwd AS cwd, msg_id AS mid, req_id AS rqd, "
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY session_id, uuid "
        "           ORDER BY timestamp_utc DESC, id DESC"
        "         ) AS rn "
        "  FROM conversation_messages "
        "  WHERE text LIKE ? ESCAPE '\\' AND text != '') "
        "SELECT rid, sid, uuid, ts, cwd, mid, rqd FROM ranked WHERE rn = 1 "
        "ORDER BY ts DESC, rid DESC LIMIT ? OFFSET ?",
        (like, limit, offset),
    ).fetchall()
    texts = _texts_for_ids(conn, [r[0] for r in page])
    hits = [_row_to_hit(uuid, sid, ts, cwd,
                        _manual_snippet(texts.get(rid, ""), q), mid, rqd)
            for (rid, sid, uuid, ts, cwd, mid, rqd) in page]
    return {"query": q, "mode": "like", "hits": _attach_costs(conn, hits),
            "total": total}


def _fts_query(q):
    """Quote each whitespace term as an FTS5 string literal so punctuation /
    operators in user input can't error the MATCH or inject FTS syntax."""
    terms = [t for t in q.split() if t]
    return " ".join('"' + t.replace('"', '""') + '"' for t in terms) or '""'


def _manual_snippet(text, q, width=80):
    lo = text.lower().find(q.lower())
    if lo < 0:
        return text[:width]
    start = max(0, lo - width // 2)
    end = min(len(text), lo + len(q) + width // 2)
    s = text[start:end]
    return ("… " if start else "") + s + (" …" if end < len(text) else "")
