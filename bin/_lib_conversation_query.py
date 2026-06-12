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
import re
import sqlite3

# Public surface (Plan 2): shipped in the npm tarball + brew formula + public
# mirror — imported by the dashboard's conversation endpoints at runtime.

from _lib_pricing import _calculate_entry_cost
# #178: the on-demand load-full re-read helper re-stringifies a raw tool_result
# content block the same way the parser does at ingest — reuse the parser's
# _stringify so the full (un-capped) result text matches the cached/capped one.
from _lib_conversation import _stringify


# Mirror of dashboard/web/src/conversations/systemMarkers.ts::MARKER_RE — anchored
# whole-string (fullmatch), unrolled-lazy body for linear time (no ReDoS), \1
# backref forces each close tag to match its open tag. Used to SKIP slash-command
# plumbing when deriving a conversation title (#165 Q2). MUST stay equivalent to
# the TS predicate over ASCII whitespace (parity-tested); exotic Unicode/control
# whitespace is an explicit non-goal. See docs/dashboard-gotchas.md.
_MARKER_TAGS = ("command-name", "command-message", "command-args", "local-command-caveat")
_MARKER_RE = re.compile(
    r"\s*(?:<(" + "|".join(_MARKER_TAGS) + r")>(?:(?!</\1>)[\s\S])*</\1>\s*)+"
)


def _is_system_marker(text) -> bool:
    """True iff `text` is ONLY concatenated command-marker wrappers (slash-command
    plumbing) — the title-derivation skip predicate. `fullmatch` reproduces the TS
    `^\\s*…\\s*$` anchor (no `$`-before-trailing-`\\n` foot-gun)."""
    return bool(text) and _MARKER_RE.fullmatch(text) is not None


_TITLE_MAX = 120


def _title_from_text(text) -> str:
    """First non-blank LINE of `text`, trimmed, sliced to _TITLE_MAX with a
    trailing '…' ONLY when truncated (rstrip before the ellipsis). '' if none.
    Semantics IDENTICAL to the client deriveReaderTitle (#165 P2.5)."""
    for line in (text or "").split("\n"):
        s = line.strip()
        if s:
            return (s[:_TITLE_MAX].rstrip() + "…") if len(s) > _TITLE_MAX else s
    return ""


# Every Claude Code skill body (Skill-tool-invoked AND SessionStart-injected)
# opens with this preamble line — the entry_type-independent skill discriminator.
_SKILL_PREAMBLE = "Base directory for this skill:"


def _first_nonblank_line(text) -> str:
    """First non-blank, stripped line of `text` ('' if none). Skill detection
    keys on this (NOT a strict body.startswith) so a leading blank text block
    can't hide the preamble (Codex P2.2)."""
    for line in (text or "").split("\n"):
        s = line.strip()
        if s:
            return s
    return ""


def _skill_name_from_preamble(first_line) -> "str | None":
    """`brainstorming` from `Base directory for this skill: …/skills/brainstorming`.
    Basename of the path after the first ':'; None on an empty/degenerate path
    (Codex P2.2) so the client renders a name-less 'Skill content' rather than a
    dangling separator."""
    _, _, rest = first_line.partition(":")
    path = rest.strip().rstrip("/")
    return os.path.basename(path) or None if path else None


def _join_text_blocks(blocks) -> str:
    """Rejoin a row's text-block bodies the way the parser's _blocks_and_text did
    ('\\n'-joined). A true meta row carries text='' (parser) with the body here in
    blocks; a not-yet-reingested human row carries the body in its text column —
    _meta_classify reads whichever is populated."""
    if not blocks:
        return ""
    return "\n".join(b.get("text", "") or "" for b in blocks if b.get("kind") == "text")


def _reingest_pending(conn) -> bool:
    """True iff migration 005's ``conversation_reingest_pending`` flag is still
    set — i.e. existing history has NOT yet been re-ingested under the meta-aware
    parser. While pending, a stale ``human`` row may actually be an injected
    skill body, so the read-time skill fallback (rendering + title-skip) is
    active. Once sync consumes the flag (skill bodies become true ``meta`` rows),
    the fallback turns OFF — so a genuine human prompt that merely *starts with*
    the skill preamble is never misclassified as a collapsed skill pill (Codex
    code-review P1). Missing table / degraded DB -> treated as not pending."""
    try:
        return conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='conversation_reingest_pending'"
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _meta_classify(item, allow_human_fallback):
    """Classify an injected item by its BODY, returning ``(meta_kind, skill_name,
    body)`` or ``None`` to leave it a genuine human turn.

    - skill: first non-blank line is the skill preamble. Fires for a true 'meta'
      row ALWAYS; for a 'human' row ONLY when ``allow_human_fallback`` is set (the
      pre-reingest window — see _reingest_pending). After the reingest a 'human'
      row keeping the preamble is a real user prompt, so it stays a "You" turn
      rather than being hidden in a collapsed skill pill (Codex code-review P1).
    - command/context: ONLY for a true 'meta' row (slash-command plumbing vs the
      rest). A 'human' row that is not a skill body stays human — generic injected
      context can't be recovered read-time without isMeta; it lands on the next
      sync-triggered reingest."""
    is_meta = item["kind"] == "meta"
    body = item.get("text") or _join_text_blocks(item.get("blocks"))
    first = _first_nonblank_line(body)
    if first.startswith(_SKILL_PREAMBLE) and (is_meta or allow_human_fallback):
        return ("skill", _skill_name_from_preamble(first), body)
    if not is_meta:
        return None
    if _is_system_marker(body):
        return ("command", None, body)
    return ("context", None, body)


def _session_titles_map(conn, session_ids):
    """{sid: title} for the first non-marker, non-blank MAIN-session human line
    per session (read-time, no migration). Windowed to the earliest 12 human
    rows/session (rides idx_conv_session_ts); Python skips system markers. A
    session whose first 12 human rows are all markers/blank is simply absent
    (caller falls back). NOTE (Codex P1.2): the window ranks the full per-session
    human partition before rn<=12 — confirmed index-ordered + bounded by the page
    (≤200 sessions); per-session human counts are modest. If EXPLAIN QUERY PLAN
    ever shows a temp B-tree sort here, switch to a per-session correlated
    LIMIT 12 candidate fetch."""
    if not session_ids:
        return {}
    titles = {}
    # While 005's reingest is pending, a stale `human` row may actually be an
    # injected skill body (a SessionStart skill can even lead the transcript) —
    # skip those as title candidates so the rail never shows "Base directory for
    # this skill: …" until the next sync reclassifies them to `meta` (which the
    # entry_type='human' filter below then excludes). Gated on the flag for the
    # same reason as the render fallback: a genuine post-reingest human prompt
    # starting with the preamble stays a normal title (Codex code-review P2).
    skip_skill_titles = _reingest_pending(conn)
    ph = ",".join("?" for _ in session_ids)
    rows = conn.execute(
        "SELECT session_id, text FROM ("
        "  SELECT session_id, text, "
        "         ROW_NUMBER() OVER (PARTITION BY session_id "
        "                            ORDER BY timestamp_utc, id) AS rn "
        f"  FROM conversation_messages "
        f"  WHERE session_id IN ({ph}) AND entry_type='human' "
        "        AND is_sidechain=0 AND COALESCE(text,'') <> ''"
        ") WHERE rn <= 12 ORDER BY session_id, rn",
        tuple(session_ids),
    ).fetchall()
    for sid, text in rows:
        if sid in titles:
            continue                 # already resolved to the first non-marker
        if _is_system_marker(text):
            continue
        if skip_skill_titles and _first_nonblank_line(text).startswith(_SKILL_PREAMBLE):
            continue
        t = _title_from_text(text)
        if t:
            titles[sid] = t
    return titles


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
    titles = _session_titles_map(conn, session_ids)
    conversations = [
        {
            "session_id": sid,
            "title": titles.get(sid) or _project_label(meta.get(sid, (None, None))[0]) or sid,
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


def _turn_usage_map(conn, turn_keys):
    """{(msg_id, req_id): {"input","output","cache_creation","cache_read"}} for
    the given non-null turn keys, read from the SAME deduped session_entries row
    cost is computed from (#177). This is a SEPARATE sibling of _turn_cost_map —
    that one returns a float and is also consumed by the search path
    (_attach_costs), so its shape must NOT change. Tokens here come from the same
    source row as the cost, but they are NOT arithmetically equal to it: cost may
    be the vendor-provided cost_usd_raw override (token math bypassed), so the
    contract is "same source row," never cost == f(tokens). Keys absent from
    session_entries are simply not present (the turn omits ``tokens``)."""
    usage = {}
    keys = [(m, r) for (m, r) in turn_keys if m is not None and r is not None]
    if not keys:
        return usage
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        cond = " OR ".join("(msg_id=? AND req_id=?)" for _ in chunk)
        params = [v for pair in chunk for v in pair]
        sql = ("SELECT msg_id, req_id, input_tokens, output_tokens, "
               "cache_create_tokens, cache_read_tokens "
               "FROM session_entries WHERE " + cond)
        for m, r, inp, out, cc, cr in conn.execute(sql, params):
            usage[(m, r)] = {"input": inp or 0, "output": out or 0,
                             "cache_creation": cc or 0, "cache_read": cr or 0}
    return usage


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
    # #177: stop_reason / attribution_* are TAIL-APPENDED (indices 15/16/17)
    # AFTER source_tool_use_id so the existing positional reads (incl.
    # _latest(logical, 10/11) for cwd/git_branch, r[6] for model, [2] for ts)
    # are all unchanged. Every unpacker below extends its tail in lockstep.
    raw = conn.execute(
        "SELECT id, uuid, timestamp_utc, entry_type, text, blocks_json, model, "
        "       msg_id, req_id, is_sidechain, cwd, git_branch, source_path, parent_uuid, "
        "       source_tool_use_id, stop_reason, attribution_skill, attribution_plugin "
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
    # ---- Phase 1: build items + index every assistant item's tool_use ids ----
    # A tool_result is NOT guaranteed to sort after its tool_use (a grounded
    # transcript scan found a matched result ordered BEFORE its use, plus orphan
    # results with no in-session use), so this is a build-and-index-ALL pass
    # FOLLOWED by a fold pass — never a single forward pass. None ids are never
    # indexed (the id-less degradation guard).
    items = []
    turn_index = {}                # (msg_id, req_id) -> index into items
    tooluse_index = {}             # tool_use id -> (item, block_dict)
    tool_result_items = []         # placeholder items deferred to Phase 2

    def _index_tool_uses(item):
        # Index every tool_use id -> its (item, block). Idempotent: re-scanning
        # a turn's blocks re-maps the same id to the same (item, block). Anthropic
        # tool_use ids are unique within a session; a collision would be
        # last-writer-wins (a result then folds to one deterministic owner).
        for b in item["blocks"]:
            if b.get("kind") == "tool_use" and b.get("id") is not None:
                tooluse_index[b["id"]] = (item, b)

    for row in logical:
        (rid, u, ts, etype, text, blocks, model, msg_id, req_id,
         is_sc, cwd, branch, source_path, parent_uuid, source_tool_use_id,
         stop_reason, attr_skill, attr_plugin) = row
        if etype == "assistant" and msg_id is not None:
            key = (msg_id, req_id)
            idx = turn_index.get(key)
            if idx is None:
                turn_index[key] = len(items)
                it = _build_turn([row])
                items.append(it)
                _index_tool_uses(it)
            else:
                _extend_turn(items[idx], row)
                _index_tool_uses(items[idx])     # re-index the turn (idempotent; new fragment may add ids)
        elif etype == "tool_result":
            it = _build_simple(row)
            items.append(it)
            tool_result_items.append(it)
        else:
            it = _build_simple(row)
            items.append(it)
            if etype == "assistant":             # null-msg_id assistant: index its uses too
                _index_tool_uses(it)

    # ---- Subagent-kind correlation (#166); MUST run before Phase 2 fold ----
    # Reads AND strips (pop) the parser-only keys in one pass, so the returned
    # tool_call/tool_result block shapes are unchanged — the only new output is
    # the top-level subagent_meta map (no undocumented block keys leak). Join is
    # spawn tool_use id <-> tool_result tool_use_id; agent_id == subagent_key.
    spawn_kind = {}     # tool_use id -> subagent_type
    agent_link = {}     # tool_use id -> (agent_id, raw_meta)
    ask_link = {}       # tool_use id -> (answers, annotations)  (#177 S2)
    bash_link = {}      # tool_use id -> (stderr, interrupted)   (#177 S3)
    task_link = {}      # tool_use id -> {"task_id", "task_list"}  (Task* checklist)
    for it in items:
        for b in it["blocks"]:
            k = b.get("kind")
            if k == "tool_use":
                st = b.pop("subagent_type", None)
                if st and b.get("id") is not None:
                    spawn_kind[b["id"]] = st
            elif k == "tool_result":
                aid = b.pop("agent_id", None)
                meta = b.pop("subagent_meta", None)
                if aid and b.get("tool_use_id") is not None:
                    agent_link[b["tool_use_id"]] = (aid, meta or {})
                ans = b.pop("ask_answers", None)            # #177 S2
                anno = b.pop("ask_annotations", None)
                if ans is not None and b.get("tool_use_id") is not None:
                    ask_link[b["tool_use_id"]] = (ans, anno)
                bstderr = b.pop("bash_stderr", None)        # #177 S3
                bintr = b.pop("bash_interrupted", None)
                if b.get("tool_use_id") is not None and (bstderr is not None or bintr):
                    bash_link[b["tool_use_id"]] = (bstderr, bool(bintr))
                tid_ = b.pop("task_id", None)               # Task* checklist
                tlist_ = b.pop("task_list", None)
                if b.get("tool_use_id") is not None and (tid_ is not None or tlist_ is not None):
                    task_link[b["tool_use_id"]] = {"task_id": tid_, "task_list": tlist_}
    subagent_meta = {}
    for _tuid, _kind in spawn_kind.items():
        _link = agent_link.get(_tuid)
        if _link is None:
            continue                       # spawn with no (yet) result -> title-only
        _aid, _raw = _link
        _entry = {"kind": _kind}
        for _f in ("total_tokens", "total_duration_ms", "total_tool_use_count", "status"):
            if _raw.get(_f) is not None:
                _entry[_f] = _raw[_f]
        subagent_meta[_aid] = _entry       # agent_id == subagent_key

    # ---- Phase 2: fold each tool_result item into its owning assistant item ----
    drop = set()                                 # id() of folded placeholder items
    for tr in tool_result_items:
        tr_blocks = [b for b in tr["blocks"] if b.get("kind") == "tool_result"]
        non_result = [b for b in tr["blocks"] if b.get("kind") != "tool_result"]
        owners = []
        resolved = []
        for b in tr_blocks:
            tid = b.get("tool_use_id")
            hit = tooluse_index.get(tid) if tid is not None else None
            if hit is None:
                owners = None                    # an unresolved block -> keep standalone
                break
            owners.append(hit[0])
            resolved.append((hit[1], b))
        # fold iff every result block resolved to exactly ONE owning item, no leftovers
        owner_ids = {id(o) for o in owners} if owners is not None else set()
        if owners and not non_result and len(owner_ids) == 1:
            owner = owners[0]
            for use_block, res_block in resolved:
                # #177: full_length (pre-clip char count) rides through the fold
                # for the "showing X of Y" affordance; None on pre-enrichment
                # rows that lack it (the .get default — never KeyErrors).
                use_block["result"] = {"text": res_block.get("text", ""),
                                       "truncated": bool(res_block.get("truncated")),
                                       "full_length": res_block.get("full_length"),
                                       "is_error": bool(res_block.get("is_error"))}
            owner["member_uuids"].append(tr["anchor"]["uuid"])
            drop.add(id(tr))
        # else: leave tr standalone (orphan / multi-owner / mixed) — a folded
        # row's uuid then joins EXACTLY ONE item's member_uuids (the #160 anchor).

    if drop:
        items = [it for it in items if id(it) not in drop]

    # ---- Phase 3: sweep every assistant item's tool_use -> tool_call ----
    # Covers turn items AND _build_simple null-msg_id assistant items. Matched
    # requests already carry `result`; unmatched get `result: None`
    # (request-only). Post-migration the client never receives a bare tool_use.
    for it in items:
        if it["kind"] == "assistant":
            for b in it["blocks"]:
                if b.get("kind") == "tool_use":
                    b["kind"] = "tool_call"
                    b["tool_use_id"] = b.pop("id", None)
                    b.setdefault("result", None)
                    link = ask_link.get(b["tool_use_id"])   # #177 S2
                    if link is not None:
                        b["answers"] = link[0]
                        if link[1]:
                            b["annotations"] = link[1]
                    blink = bash_link.get(b["tool_use_id"])  # #177 S3
                    if blink is not None:
                        if blink[0] is not None:
                            b["stderr"] = blink[0]
                        if blink[1]:
                            b["interrupted"] = True

    # ---- Phase 3b: fold the Task* op stream into per-run checklist snapshots ----
    _fold_task_runs(items, task_link)

    # ---- Phase 4: classify injected meta items (skill / command / context) ----
    # `meta` rows (the parser's isMeta classification) AND — only while the 005
    # reingest is still pending — not-yet-reingested `human` rows whose body is a
    # skill preamble (the read-time fallback) become kind='meta' with a meta_kind
    # + skill_name, so the client renders a collapsed skill/system-marker/context
    # disclosure instead of a "YOU" prompt. `text` is set to the rendered body
    # (the DB text column stays '' for FTS); genuine human turns are untouched.
    allow_human_fallback = _reingest_pending(conn)
    for it in items:
        if it["kind"] in ("meta", "human"):
            cls = _meta_classify(it, allow_human_fallback)
            if cls is not None:
                meta_kind, skill_name, body = cls
                it["kind"] = "meta"
                it["meta_kind"] = meta_kind
                it["skill_name"] = skill_name
                it["text"] = body

    # ---- Phase 4b: fold a Skill-invoked skill body into its Skill tool chip ----
    # A Skill invocation's injected body (now meta_kind='skill') links to its
    # Skill tool_use via source_tool_use_id (threaded as the internal
    # _source_tool_use_id). Resolve it against the SAME tooluse_index the Phase 2
    # tool_result fold uses (ids unique per session; last-writer-wins). The index
    # value is (item, block) holding the LIVE block dict — Phase 3 mutated that
    # same dict in place to a `tool_call`, so block["skill_body"]=… mutates the
    # live chip. On a hit: the body becomes the chip's expandable content
    # (skill_body/skill_name), the trivial "Launching skill" result is dropped
    # (result=None), the body uuid joins the owner's member_uuids (#160 jump
    # anchor), and the standalone item is removed. NO hit (SessionStart skills;
    # pre-006 NULL column; orphan id) -> the standalone pill stays. NULL-driven
    # and flag-INDEPENDENT (it does NOT key on _reingest_pending). Runs before
    # pagination so a match never depends on page boundaries.
    _skill_drop = set()
    for it in items:
        if it.get("meta_kind") != "skill":
            continue
        stid = it.get("_source_tool_use_id")
        if not stid:
            continue
        hit = tooluse_index.get(stid)
        if hit is None:
            continue
        owner, block = hit
        block["skill_body"] = it["text"]
        block["skill_name"] = it.get("skill_name")
        block["result"] = None
        owner["member_uuids"].append(it["anchor"]["uuid"])
        _skill_drop.add(id(it))
    if _skill_drop:
        items = [it for it in items if id(it) not in _skill_drop]

    costs = _turn_cost_map(conn, list(turn_index))
    # #177: per-turn token usage from the SAME deduped session_entries row cost
    # uses (a separate map; _turn_cost_map is unchanged for the search path).
    usage = _turn_usage_map(conn, list(turn_index))
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
            # #177: stamp tokens from the same source row; absent when the turn
            # key has no session_entries row (omitted, not zero-filled).
            tok = usage.get((it["_msg_id"], it["_req_id"]))
            if tok is not None:
                it["tokens"] = tok
            del it["_msg_id"]
            del it["_req_id"]
            it.pop("_has_prose", None)
    header_cost = round(header_cost, 6)

    # Strip the internal Phase-4b threading key from EVERY item (meta/human items
    # carry it too, not just assistant turns) so it never surfaces in the public
    # item JSON.
    for it in items:
        it.pop("_source_tool_use_id", None)

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
                "subagent_meta": subagent_meta,
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
        "subagent_meta": subagent_meta,
        "page": {"next_after": next_after, "has_more": has_more},
    }


_TASK_TRIO = ("TaskCreate", "TaskUpdate", "TaskList")


def _fold_task_runs(items, task_link):
    """Reconstruct the running to-do list from the chronological Task* op stream
    and stamp the resulting todos[] snapshot onto the FIRST tool_call of each
    Task* run. Key on the explicit task id (never reused). `deleted` drops a
    task; a TaskList result reseeds the whole snapshot.

    Scoped PER subagent thread (``subagent_key``): the main session (key None)
    and each subagent keep INDEPENDENT running checklists, so parallel subagents
    with disjoint task-id ranges never bleed into one another's cards. Within a
    thread, state still spans the whole session.

    Degradation guard: a thread's run is stamped only once that thread has
    recognized a real create/list (``seen``). A Task* run with no recognizable
    create — a future result shape we don't parse, or pre-fix legacy rows —
    leaves ``task_snapshot`` ABSENT, so the frontend falls back to generic chips
    instead of a misleading empty "0 / 0" card.

    Mirrors the ask_answers join: the parser stashed the record-level identity
    onto the tool_result block, the Phase-1 sweep popped it into ``task_link``
    keyed by tool_use_id, and this fold joins it back. The frontend stays a pure
    todos[] renderer — all running-list state lives here."""
    threads = {}      # subagent_key -> {"order": [...], "state": {...}, "seen": bool}

    def snapshot(th):
        st, order = th["state"], th["order"]
        return [dict(content=st[i]["content"], status=st[i]["status"],
                     **({"activeForm": st[i]["activeForm"]} if st[i].get("activeForm") else {}))
                for i in order if i in st]

    for it in items:
        if it.get("kind") != "assistant":
            continue
        th = threads.setdefault(it.get("subagent_key"),
                                {"order": [], "state": {}, "seen": False})
        first_task_call = None
        for b in it["blocks"]:
            if b.get("kind") != "tool_call" or b.get("name") not in _TASK_TRIO:
                continue
            if first_task_call is None:
                first_task_call = b
            link = task_link.get(b.get("tool_use_id")) or {}
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            name = b["name"]
            if name == "TaskCreate":
                tid = link.get("task_id")
                if tid is not None:
                    th["seen"] = True
                    if tid not in th["state"]:
                        th["order"].append(tid)
                    th["state"][tid] = {"content": inp.get("subject") or "", "status": "pending",
                                        "activeForm": inp.get("activeForm") or ""}
            elif name == "TaskUpdate":
                tid = str(inp.get("taskId")) if inp.get("taskId") is not None else link.get("task_id")
                status = inp.get("status")
                if tid is not None:
                    if status == "deleted":
                        th["state"].pop(tid, None)
                    elif tid in th["state"] and status:
                        th["state"][tid]["status"] = status
            elif name == "TaskList":
                snap = link.get("task_list")
                if snap is not None:
                    th["seen"] = True
                    th["order"] = []
                    th["state"] = {}
                    for t in snap:
                        tid = t["id"]
                        th["order"].append(tid)
                        th["state"][tid] = {"content": t.get("subject") or "",
                                            "status": t.get("status") or "pending", "activeForm": ""}
        if first_task_call is not None and th["seen"]:
            first_task_call["task_snapshot"] = snapshot(th)


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
        # Internal threading for the Phase 4b skill-body fold (analogous to
        # _msg_id/_req_id); consumed + del'd before items are returned, never in
        # the public JSON. Meaningful only on meta skill items, harmless here.
        "_source_tool_use_id": first[14],
        "_has_prose": False,
    }
    # #177: stop_reason / attribution_* (tail-appended cols 15/16/17) are seeded
    # by the _fold_fragment(item, first) call below — the same seed-then-fold path
    # model/text/is_sidechain already use. _fold_fragment applies the last-non-null
    # guard, so a single-fragment turn is covered there and omitted keys (never
    # None) preserve the absent-when-absent contract.
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
    # #177: stop_reason = last-non-null fragment value (the terminal fragment
    # carries the real reason); attribution_* = last-non-null (turn-level
    # constant). A later null fragment must NOT blank an earlier value — hence
    # the `is not None` guard rather than an unconditional assign.
    if row[15] is not None:
        item["stop_reason"] = row[15]
    if row[16] is not None:
        item["attribution_skill"] = row[16]
    if row[17] is not None:
        item["attribution_plugin"] = row[17]


def _build_simple(row):
    """A human, tool_result, or assistant-with-null-msg_id item (no turn grouping,
    no cost). An assistant row routes here only when its msg_id is NULL (no turn
    key → no session_entries join); it carries an explicit cost_usd of 0.0 and NO
    internal _msg_id/_req_id keys, so the cost loop's KeyError path can never fire
    (I2). The model is preserved for assistant rows."""
    (rid, u, ts, etype, text, blocks, model, msg_id, req_id, is_sc, cwd, branch,
     source_path, parent_uuid, source_tool_use_id,
     stop_reason, attr_skill, attr_plugin) = row
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
        # Internal threading for the Phase 4b skill-body fold (consumed + del'd
        # before return). Carried on every simple item; meaningful only on the
        # meta skill body row.
        "_source_tool_use_id": source_tool_use_id,
    }
    if etype == "assistant":
        item["model"] = model
        item["cost_usd"] = 0.0
        # #177: stop_reason / attribution are assistant-only — a null-msg_id
        # assistant turn still carries them. Omitted when null (absent-when-
        # absent). Human / tool_result simple items never get these keys.
        if stop_reason is not None:
            item["stop_reason"] = stop_reason
        if attr_skill is not None:
            item["attribution_skill"] = attr_skill
        if attr_plugin is not None:
            item["attribution_plugin"] = attr_plugin
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


def _attach_titles(conn, page):
    """Stamp each final-page hit with its session's derived title — ONE batched
    _session_titles_map over the distinct page session_ids (parallel to
    _attach_costs). Fallback project_label → session_id, matching
    list_conversations (#165 Q4)."""
    sids = list({h["session_id"] for h in page})
    titles = _session_titles_map(conn, sids)
    for h in page:
        sid = h["session_id"]
        h["title"] = titles.get(sid) or h.get("project_label") or sid
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
    return {"query": q, "mode": "fts",
            "hits": _attach_titles(conn, _attach_costs(conn, hits)),
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
    return {"query": q, "mode": "like",
            "hits": _attach_titles(conn, _attach_costs(conn, hits)),
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


# ---------------------------------------------------------------------------
# #178: on-demand "load full result/input" kernels. Back the dashboard's
# /api/conversation/<sid>/payload route. The cache stores only CAPPED tool text
# (result clipped to _TOOL_RESULT_CAP, input leaves clipped to _INPUT_LEAF_CAP),
# so the full body is re-derived from the source JSONL line here — the cache at
# rest never grows. Pure (sqlite3.Connection + filesystem); no clock/network.
# ---------------------------------------------------------------------------
_FULL_PAYLOAD_CEILING = 1_000_000   # serve up to ~1 MB; protects the HTTP server / browser


def locate_tool_payload(conn, session_id, tool_use_id, which):
    """``(source_path, byte_offset)`` for the JSONL line holding the tool_use
    (``which='input'``) or tool_result (``which='result'``) carrying this
    ``tool_use_id`` in this session, else ``None``.

    The prefilter uses ``instr(blocks_json, ?) > 0`` — NOT ``LIKE`` (Codex P1.4):
    tool_use_ids contain ``_`` (e.g. ``toolu_01SEQ…``), which ``LIKE`` treats as a
    single-char wildcard, so a near-miss id would false-match. ``instr`` is a
    literal substring test. It is also NOT a ``≤2 rows`` situation — the DB holds
    duplicate physical rows for one logical message (``get_conversation`` dedups
    by uuid) — so every candidate is parsed and EXACT-matched on the block id
    (``tool_use.id`` for input / ``tool_result.tool_use_id`` for result), under the
    same deterministic ``ORDER BY timestamp_utc, id`` as ``get_conversation``. The
    SELECT runs here (not via ``get_conversation``) because that reader omits
    ``byte_offset`` (Codex P2.5)."""
    rows = conn.execute(
        "SELECT source_path, byte_offset, blocks_json FROM conversation_messages "
        "WHERE session_id=? AND instr(blocks_json, ?) > 0 "
        "ORDER BY timestamp_utc, id", (session_id, tool_use_id)).fetchall()
    for source_path, byte_offset, blocks_json in rows:
        try:
            blocks = _json.loads(blocks_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(blocks, list):
            continue
        for b in blocks:
            if not isinstance(b, dict):
                continue
            k = b.get("kind")
            if which == "input" and k == "tool_use" and b.get("id") == tool_use_id:
                return source_path, byte_offset
            if which == "result" and k == "tool_result" and b.get("tool_use_id") == tool_use_id:
                return source_path, byte_offset
    return None


def _clip_payload_input(inp, ceiling):
    """Clip a structured input so the returned dict serializes to ``ceiling`` chars
    or fewer, and report whether anything was clipped — the input-side analogue of
    the result-side ceiling. A degenerate multi-MB input (one giant leaf OR many
    sub-ceiling leaves that sum past the ceiling) is bounded so the HTTP server /
    browser is protected, while every real payload returns whole.

    The guarantee is AGGREGATE, not merely per-leaf: a shared remaining-char budget
    is threaded through the walk (mirroring ``_bound_input``'s total-size backstop) —
    each string leaf is clipped against the running budget and the budget is
    decremented as we go, so once it is exhausted later leaves clip to ''.
    ``truncated`` is True iff any leaf was clipped (or the post-walk serialized size
    still exceeds ``ceiling``, the structural-overhead backstop). Post-condition:
    ``len(json.dumps(clipped, ensure_ascii=False)) <= ceiling`` always."""
    truncated = False
    remaining = [ceiling]   # boxed so the nested walk can decrement it

    def walk(v):
        nonlocal truncated
        if isinstance(v, str):
            if len(v) > remaining[0]:
                truncated = True
                v = v[:remaining[0]]
            remaining[0] -= len(v)
            return v
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    clipped = walk(inp)
    # Backstop: structural JSON overhead (braces/quotes/keys) can push a
    # budget-exact payload a few chars past the ceiling. Hard-clip the largest
    # remaining string leaf(s) until the whole dict serializes within the ceiling.
    while len(_json.dumps(clipped, ensure_ascii=False)) > ceiling:
        truncated = True
        if not _shrink_largest_leaf(clipped):
            break   # no string leaf left to shrink (e.g. pure numeric/structural)
    return clipped, truncated


def _shrink_largest_leaf(obj):
    """Halve the longest string leaf reachable in ``obj`` (a dict/list/scalar),
    in place, and return True if one was shrunk — the post-walk backstop for the
    rare structural-overhead overshoot. A leaf already at length 1 is truncated to
    ''. Returns False when no non-empty string leaf exists."""
    best = {"len": 0, "container": None, "key": None}

    def scan(v, container, key):
        if isinstance(v, str):
            if len(v) > best["len"]:
                best.update(len=len(v), container=container, key=key)
        elif isinstance(v, dict):
            for k, x in v.items():
                scan(x, v, k)
        elif isinstance(v, list):
            for i, x in enumerate(v):
                scan(x, v, i)

    scan(obj, None, None)
    if best["container"] is None or best["len"] == 0:
        return False
    s = best["container"][best["key"]]
    best["container"][best["key"]] = s[: len(s) // 2]
    return True


def read_full_payload(source_path, byte_offset, tool_use_id, which):
    """Re-read the raw JSONL line at ``(source_path, byte_offset)`` and return the
    FULL (un-capped) payload for ``tool_use_id``:

    - ``which='input'`` -> ``{"which":"input", "tool_use_id", "input", "full_length",
      "truncated"}`` — the matching tool_use block's complete ``input`` dict, so the
      DiffCard can pull old/new strings straight into computeDiff.
    - ``which='result'`` -> ``{"which":"result", "tool_use_id", "text", "full_length",
      "truncated", "is_error", [stderr]}`` — the full ``_stringify(content)`` plus,
      for Bash, the full ``toolUseResult.stderr``.

    ``None`` when the source is gone / the line is unparseable (rotated or deleted
    JSONL — the documented 410 path) or the id is no longer present in that line.
    ``full_length``/``truncated`` describe the payload against ``_FULL_PAYLOAD_CEILING``
    — honoring #178's "un-capped" spirit for real payloads while bounding the
    degenerate multi-MB case."""
    try:
        with open(source_path, "rb") as fh:
            fh.seek(byte_offset)
            line = fh.readline()
        obj = _json.loads(line)
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    content = (obj.get("message") or {}).get("content")
    if not isinstance(content, list):
        return None
    if which == "input":
        for b in content:
            if (isinstance(b, dict) and b.get("type") == "tool_use"
                    and b.get("id") == tool_use_id):
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                full_length = len(_json.dumps(inp, ensure_ascii=False))
                clipped, truncated = _clip_payload_input(inp, _FULL_PAYLOAD_CEILING)
                return {"which": "input", "tool_use_id": tool_use_id,
                        "input": clipped, "full_length": full_length,
                        "truncated": truncated}
        return None
    for b in content:
        if (isinstance(b, dict) and b.get("type") == "tool_result"
                and b.get("tool_use_id") == tool_use_id):
            raw = _stringify(b.get("content"))
            # The bound is PER-STREAM by design: `text` and the Bash `stderr`
            # below are each clipped to _FULL_PAYLOAD_CEILING independently, so a
            # result carrying both can serialize to ~2× the ceiling. That is
            # intentional — they are distinct streams the DiffCard renders side by
            # side, and each is individually bounded against the HTTP/browser DoS.
            resp = {"which": "result", "tool_use_id": tool_use_id,
                    "text": raw[:_FULL_PAYLOAD_CEILING], "full_length": len(raw),
                    "truncated": len(raw) > _FULL_PAYLOAD_CEILING,
                    "is_error": bool(b.get("is_error"))}
            tur = obj.get("toolUseResult")
            if (isinstance(tur, dict) and isinstance(tur.get("stderr"), str)
                    and tur.get("stderr")):
                resp["stderr"] = tur["stderr"][:_FULL_PAYLOAD_CEILING]   # per-stream bound (see above)
            return resp
    return None
