"""#302 Task 5 — the authoritative browse rail issues NO conversation_messages
enrichment re-read (the whole point of materializing enrichment onto the rollup).

Two guards (Codex P2-2 — stronger than name-monkeypatching):
  * FAST PATH (rollup authoritative + enrichment filled): wrap
    list_conversations in sqlite3.Connection.set_trace_callback and assert NO
    executed SQL references ``conversation_messages`` besides the one indexed
    ``conversation_ai_titles`` live overlay (which does NOT touch
    conversation_messages). Cost/models/title/branch all come from the rollup.
  * LIVE BRANCH (backfill pending): the retained four maps are still all reached
    (spies), so the fallback is fully exercised, not short-circuited.
"""
import pathlib
import sys

from conftest import load_script, redirect_paths  # type: ignore

_MODEL = "claude-opus-4-8"


def _bin_on_path(ns):
    bin_dir = str(pathlib.Path(ns["__file__"]).resolve().parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)


def _msg(conn, **kw):
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


def _seed(conn):
    _msg(conn, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="hi",
         cwd="/home/u/proj", git_branch="main")
    _msg(conn, session_id="s1", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="hello", model=_MODEL, msg_id="m1", req_id="r1")
    _msg(conn, session_id="s2", uuid="h2", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-04T00:00:00Z", entry_type="human",
         text="budget?", cwd="/home/u/other", git_branch="dev")
    conn.execute(
        "INSERT OR REPLACE INTO conversation_ai_titles "
        "(session_id, ai_title, source_path, byte_offset) VALUES ('s2','T','x',0)")
    conn.commit()


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    import _cctally_cache as cc
    import _lib_conversation_query as cq
    conn = ns["open_cache_db"]()
    _seed(conn)
    return cc, cq, conn


def test_authoritative_rail_issues_no_conversation_messages_sql(tmp_path,
                                                                monkeypatch):
    cc, cq, conn = _load(tmp_path, monkeypatch)
    try:
        # Populate the rollup enrichment (Task 3 fill) + clear the flag so the
        # rollup is authoritative — the fast path.
        cc._recompute_conversation_sessions(conn)
        conn.execute("DELETE FROM cache_meta "
                     "WHERE key='conversation_sessions_backfill_pending'")
        conn.commit()
        assert cq._rollup_authoritative(conn) is True

        seen = []
        conn.set_trace_callback(lambda s: seen.append(s))
        res = cq.list_conversations(conn, limit=50)
        conn.set_trace_callback(None)

        offenders = [s for s in seen if "conversation_messages" in s]
        assert not offenders, (
            "authoritative rail must not re-scan conversation_messages; got:\n"
            + "\n".join(offenders))
        # Non-vacuous: the rail actually ran and the AI overlay won.
        by_id = {c["session_id"]: c for c in res["conversations"]}
        assert by_id["s2"]["title"] == "T", "AI title must be overlaid live"
        assert by_id["s1"]["models"] == [_MODEL]
        assert by_id["s1"]["git_branch"] == "main"
    finally:
        conn.close()


def test_live_branch_reaches_all_retained_maps(tmp_path, monkeypatch):
    """Under the LIVE branch (flag set) every retained map is reached — the
    fallback is fully exercised, not stopped at the first call."""
    cc, cq, conn = _load(tmp_path, monkeypatch)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) "
            "VALUES('conversation_sessions_backfill_pending','1')")
        conn.commit()
        assert cq._rollup_authoritative(conn) is False

        reached = set()
        for name in ("_session_cost_map", "_session_models_map",
                     "_session_latest_meta_map", "_session_titles_map"):
            orig = getattr(cq, name)

            def spy(*a, _name=name, _orig=orig, **k):
                reached.add(_name)
                return _orig(*a, **k)

            monkeypatch.setattr(cq, name, spy)

        cq.list_conversations(conn, limit=50)
        assert reached == {"_session_cost_map", "_session_models_map",
                           "_session_latest_meta_map", "_session_titles_map"}, \
            f"live branch skipped a map: reached={reached}"
    finally:
        conn.close()
