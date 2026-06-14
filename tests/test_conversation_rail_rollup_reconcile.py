"""Reconcile the conversation browse-rail rollup read path against the live
GROUP BY (spec §3 / §6, Codex gate BLOCKER 2 fallback).

``list_conversations`` reads ``conversation_sessions`` (the Task-A rollup) when
the durable ``conversation_sessions_backfill_pending`` flag is CLEAR, and falls
back to the retained live ``GROUP BY`` over ``conversation_messages`` when it is
SET. The load-bearing invariant is byte-identity: for any
``(sort, limit, offset)`` the rail output must be identical in BOTH states —
which it is, because the rollup is recomputed from the same COUNT/MIN/MAX over
the same rows (rollup branch) and the fallback IS the old aggregate (live
branch).

This module pins all three:
  * ROLLUP branch (flag clear, rollup populated) == live-aggregate reference.
  * FALLBACK branch (flag set, rollup empty) == the SAME reference.
  * ``EXPLAIN QUERY PLAN`` for the ``recent`` rollup read rides
    ``idx_conv_sessions_recent`` and does NOT spill to a temp B-tree.

Uses the ``load_script()`` + ``redirect_paths()`` idiom (NOT a bare
``setenv(HOME)`` — that would read the real prod cache.db once ``_cctally_core``
is cached) so the seeded ``conversation_messages`` land in a tmp-dir cache.db.
The recompute + flag helpers come from ``_cctally_cache`` (Task A); the read
path is ``_lib_conversation_query.list_conversations``.
"""
import json
import pathlib
import sys

from conftest import load_script, redirect_paths

# A real model id from CLAUDE_MODEL_PRICING so token-derived cost is non-zero
# (a bare "opus" prices to $0 and would make the cost column vacuous).
_MODEL = "claude-opus-4-8"

# Both sorts × the four (limit, offset) combos from the Task-B baseline.
_COMBOS = [(50, 0), (5, 0), (5, 5), (2, 3)]
_SORTS = ["recent", "oldest"]


def _bin_on_path(ns):
    """Make the bin/ siblings importable (``_cctally_cache`` /
    ``_lib_conversation_query``) the same way the endpoint tests do."""
    bin_dir = str(pathlib.Path(ns["__file__"]).resolve().parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)


def _msg(conn, **kw):
    """Insert one ``conversation_messages`` row (only the columns the rail
    aggregate reads need to be meaningful: session_id / timestamp_utc; the rest
    feed the unchanged downstream cost/meta/title maps)."""
    cols = ("session_id", "uuid", "parent_uuid", "source_path", "byte_offset",
            "timestamp_utc", "entry_type", "text", "blocks_json", "model",
            "msg_id", "req_id", "cwd", "git_branch", "is_sidechain")
    row = {k: kw.get(k) for k in cols}
    row["blocks_json"] = kw.get("blocks_json", "[]")
    row["text"] = kw.get("text", "")
    row["is_sidechain"] = kw.get("is_sidechain", 0)
    conn.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain) VALUES(:session_id,:uuid,:parent_uuid,:source_path,"
        ":byte_offset,:timestamp_utc,:entry_type,:text,:blocks_json,:model,"
        ":msg_id,:req_id,:cwd,:git_branch,:is_sidechain)",
        row,
    )


def _entry(conn, *, source_path, line_offset, model, msg_id, req_id,
           inp=0, out=0):
    conn.execute(
        "INSERT OR IGNORE INTO session_entries "
        "(source_path,line_offset,timestamp_utc,model,msg_id,req_id,"
        " input_tokens,output_tokens,cache_create_tokens,cache_read_tokens)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (source_path, line_offset, "t", model, msg_id, req_id, inp, out, 0, 0),
    )


def _seed(conn):
    """Several sessions with varied msg_count / started / last_activity so the
    recent (MAX) and oldest (MIN) orderings, pagination, and cost/title/meta
    columns are all exercised — not a single-row degenerate rail."""
    # s1: human prompt + one assistant turn with cost; spans two timestamps.
    _msg(conn, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="hi",
         cwd="/home/u/proj", git_branch="main")
    _msg(conn, session_id="s1", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="hello", blocks_json='[{"kind":"text","text":"hello"}]',
         model=_MODEL, msg_id="m1", req_id="r1")
    _entry(conn, source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=1000, out=500)
    # s2: human-only, latest activity (sorts first under recent).
    _msg(conn, session_id="s2", uuid="h2", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-04T00:00:00Z", entry_type="human",
         text="how do I set a budget", cwd="/home/u/other", git_branch="dev")
    # s3: earliest start (sorts first under oldest), three rows.
    for i, off in enumerate((0, 1, 2)):
        _msg(conn, session_id="s3", uuid=f"c{i}", source_path="c.jsonl",
             byte_offset=off,
             timestamp_utc=f"2026-05-3{0 + i}T00:00:00Z"
             if i == 0 else f"2026-06-0{i}T00:00:00Z",
             entry_type="human", text="audit", cwd="/home/u/proj",
             git_branch="main")
    # A NULL session_id row must NOT contribute a rail row (the recompute
    # GROUP BY filters NULLs by construction; the fallback's WHERE clause does
    # the same) — so it is excluded by BOTH branches, leaving 4 rail sessions.
    _msg(conn, session_id=None, uuid="nx", source_path="d.jsonl", byte_offset=0,
         timestamp_utc="2026-06-09T00:00:00Z", entry_type="human", text="orphan")
    # s5: a single-row session (msg_count == 1) at a mid timestamp.
    _msg(conn, session_id="s5", uuid="e1", source_path="e.jsonl", byte_offset=0,
         timestamp_utc="2026-06-03T12:00:00Z", entry_type="human",
         text="quick one", cwd="/home/u/proj")
    conn.commit()


def _cc(ns):
    import _cctally_cache as cc
    return cc


def _cq(ns):
    import _lib_conversation_query as cq
    return cq


def _dump(cq, conn):
    """All (sort, limit, offset) outputs as a JSON-canonical dict, so equality
    is the byte-identity the spec requires."""
    out = {}
    for s in _SORTS:
        for (limit, offset) in _COMBOS:
            res = cq.list_conversations(conn, sort=s, limit=limit, offset=offset)
            out[f"{s}-{limit}-{offset}"] = json.dumps(res, sort_keys=True)
    return out


def test_rollup_read_matches_live_aggregate(tmp_path, monkeypatch):
    """Flag CLEAR + populated rollup: the fast rollup read is byte-identical to
    the retained live GROUP BY for every (sort, limit, offset)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    cc, cq = _cc(ns), _cq(ns)

    conn = ns["open_cache_db"]()
    try:
        _seed(conn)

        # Reference = the live GROUP BY: force the fallback by SETTING the flag.
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) "
            "VALUES('conversation_sessions_backfill_pending','1')"
        )
        conn.commit()
        assert cc._conversation_sessions_backfill_pending(conn) is True
        reference = _dump(cq, conn)

        # Now populate the rollup and CLEAR the flag -> the fast path is active.
        cc._recompute_conversation_sessions(conn)
        conn.execute(
            "DELETE FROM cache_meta "
            "WHERE key='conversation_sessions_backfill_pending'"
        )
        conn.commit()
        assert cc._conversation_sessions_backfill_pending(conn) is False
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_sessions"
        ).fetchone()[0] == 4  # 4 non-null sessions (the NULL row is excluded)

        rollup = _dump(cq, conn)
        assert rollup == reference, "rollup read diverged from live GROUP BY"
    finally:
        conn.close()


def test_fallback_branch_matches_live_aggregate(tmp_path, monkeypatch):
    """Flag SET + EMPTY rollup (the BLOCKER-2 pre-sync / --no-sync state):
    list_conversations must serve the live GROUP BY, NOT an empty rail — and it
    must equal the populated-rollup reference."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    cc, cq = _cc(ns), _cq(ns)

    conn = ns["open_cache_db"]()
    try:
        _seed(conn)

        # Reference from the authoritative (populated, flag-clear) rollup.
        cc._recompute_conversation_sessions(conn)
        conn.commit()
        assert cc._conversation_sessions_backfill_pending(conn) is False
        reference = _dump(cq, conn)

        # Now emulate the not-yet-synced state: empty rollup + flag SET. A naive
        # rollup read here would return an empty rail; the fallback must not.
        conn.execute("DELETE FROM conversation_sessions")
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) "
            "VALUES('conversation_sessions_backfill_pending','1')"
        )
        conn.commit()
        assert cc._conversation_sessions_backfill_pending(conn) is True
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_sessions"
        ).fetchone()[0] == 0

        fallback = _dump(cq, conn)
        # The fallback rail is non-empty (proves it did NOT read the empty
        # rollup) and byte-identical to the populated-rollup reference.
        first = json.loads(fallback["recent-50-0"])
        assert len(first["conversations"]) == 4
        assert fallback == reference, "fallback read diverged from rollup read"
    finally:
        conn.close()


def test_recent_rollup_read_uses_index_no_temp_btree(tmp_path, monkeypatch):
    """The whole point of the rollup: the ``recent`` read early-terminates on
    idx_conv_sessions_recent with no temp B-tree (vs the old aggregate's
    USE TEMP B-TREE FOR ORDER BY over every session)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    cc, cq = _cc(ns), _cq(ns)

    conn = ns["open_cache_db"]()
    try:
        _seed(conn)
        cc._recompute_conversation_sessions(conn)
        conn.commit()

        order = cq._SORTS["recent"]
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT session_id, msg_count, started_utc, last_activity_utc "
            "FROM conversation_sessions ORDER BY " + order + " LIMIT ? OFFSET ?",
            (51, 0),
        ).fetchall()
        text = " ".join(str(r[-1]) for r in plan)
        assert "idx_conv_sessions_recent" in text, text
        assert "USE TEMP B-TREE" not in text.upper(), text
    finally:
        conn.close()
