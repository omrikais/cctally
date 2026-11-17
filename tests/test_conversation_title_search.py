"""kind=title cross-session search + the kind-validation split + filtered-search
(#217 S2 / E7 + Filtered-search; subtasks I-2c / I-2d).

Kernel-level tests (in-memory cache.db seeded directly) plus HTTP-route tests
that boot a real ``DashboardHTTPHandler`` to prove the kind-validation split
(``/find?kind=title`` → 400, never 500) and that the search route threads the
browse filters.

Load-bearing findings exercised:
  * P1-1 — ``/find?kind=title`` returns 400 (search-kinds include ``title``;
    find-kinds do NOT), never a 500 KeyError.
  * P2-9 — ``kind=title`` ``total`` counts ONLY anchorable sessions (a title row
    whose session has no surviving message rows is excluded from both ``total``
    and the page).
  * P1-5 — filtered-search degraded mode uses the session ``MAX(timestamp_utc)``
    prefilter, not matched-row ts.
  * No-filter search output stays byte-stable (no ``filter_degraded`` key).
"""
from __future__ import annotations

import json
import pathlib
import socketserver
import sqlite3
import sys
import threading

import pytest
from _fts5_gate import require_fts5  # the ONE FTS5 capability gate (#630 S6)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db   # noqa: E402
import _cctally_cache as cc  # noqa: E402
import _lib_conversation_query as cq  # noqa: E402

from conftest import load_script, redirect_paths  # noqa: E402
from tests._support_http import PRESENCE_BACKSTOP_SECONDS, start, stop

_MODEL = "claude-opus-4-8"


def _conn():
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)
    return c


def _msg(c, **kw):
    cols = ("session_id", "uuid", "parent_uuid", "source_path", "byte_offset",
            "timestamp_utc", "entry_type", "text", "blocks_json", "model",
            "msg_id", "req_id", "cwd", "git_branch", "is_sidechain",
            "source_tool_use_id", "stop_reason", "attribution_skill",
            "attribution_plugin")
    row = {k: kw.get(k) for k in cols}
    row["blocks_json"] = kw.get("blocks_json", "[]")
    row["text"] = kw.get("text", "")
    row["is_sidechain"] = kw.get("is_sidechain", 0)
    row["search_tool"] = kw.get("search_tool", "")
    row["search_thinking"] = kw.get("search_thinking", "")
    c.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,is_sidechain,"
        " source_tool_use_id,stop_reason,attribution_skill,attribution_plugin,"
        " search_tool,search_thinking)"
        " VALUES(:session_id,:uuid,:parent_uuid,:source_path,:byte_offset,"
        ":timestamp_utc,:entry_type,:text,:blocks_json,:model,:msg_id,:req_id,"
        ":cwd,:git_branch,:is_sidechain,:source_tool_use_id,:stop_reason,"
        ":attribution_skill,:attribution_plugin,:search_tool,:search_thinking)", row)


def _title(c, sid, title):
    c.execute(cc._AI_TITLE_UPSERT_SQL, (sid, title, "a.jsonl", 0))


def _seed_titles(c):
    # s1: title matches 'refactor', anchorable (has a message row).
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="hi",
         cwd="/home/u/proj", git_branch="main")
    _msg(c, session_id="s1", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="ok", model=_MODEL, msg_id="m1", req_id="r1", cwd="/home/u/proj")
    _title(c, "s1", "refactor the cache module")
    # s2: title does NOT match 'refactor'.
    _msg(c, session_id="s2", uuid="h2", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-02T00:00:00Z", entry_type="human", text="yo",
         cwd="/home/u/other")
    _title(c, "s2", "budget weekly usage")


# --- I-2c: kind=title kernel search ---------------------------------------

@pytest.mark.parametrize("fa", [True, False])
def test_kind_title_returns_session_hit(fa):
    c = _conn()
    if fa:
        require_fts5()
    _seed_titles(c)
    res = cq.search_conversations(c, "refactor", kind="title", fts_available=fa)
    assert res["kind"] == "title"
    assert len(res["hits"]) == 1
    h = res["hits"][0]
    assert h["session_id"] == "s1"
    assert h["uuid"] == "h1"                       # anchored to the FIRST turn
    assert h["match_kinds"] == ["title"]
    assert "refactor" in h["snippet"].lower()      # snippet = the matched title
    assert h["title"] == "refactor the cache module"
    # one hit per session; total == #anchorable matching sessions.
    assert res["total"] == 1


@pytest.mark.parametrize("fa", [True, False])
def test_kind_title_total_counts_only_anchorable(fa):
    """P2-9: a title row whose session has NO message rows must not count toward
    total or appear in the page."""
    c = _conn()
    if fa:
        require_fts5()
    # s-orphan: title matches 'orphan' but the session has no conversation_messages.
    _title(c, "s-orphan", "orphan title here")
    # s-real: anchorable match for 'orphan'.
    _msg(c, session_id="s-real", uuid="hr", source_path="r.jsonl", byte_offset=0,
         timestamp_utc="2026-06-05T00:00:00Z", entry_type="human", text="x",
         cwd="/home/u/r")
    _title(c, "s-real", "orphan rescue plan")
    res = cq.search_conversations(c, "orphan", kind="title", fts_available=fa)
    assert {h["session_id"] for h in res["hits"]} == {"s-real"}
    assert res["total"] == len(res["hits"]) == 1


def test_kind_title_empty_query_is_empty():
    c = _conn()
    _seed_titles(c)
    res = cq.search_conversations(c, "   ", kind="title", fts_available=False)
    assert res["hits"] == [] and res["total"] == 0 and res["kind"] == "title"


@pytest.mark.parametrize("store", ["memory", "committed-file"])
def test_kind_title_missing_vtable_degrades_to_like(tmp_path, store):
    """Resilience (code-review P3): ``fts_available`` is derived from the
    ``fts5_unavailable`` flag (NOT a per-table probe), so on an otherwise
    FTS5-capable build a missing/corrupt ``conversation_title_fts`` vtable would
    raise inside ``_search_title`` — the ``kind=title`` branch must catch that
    ``sqlite3.OperationalError`` and degrade to the title LIKE scan, mirroring the
    message-FTS path's resilience, instead of bubbling a 500.

    The LIKE degradation still returns the correct session-level title hit (the
    title content lives in ``conversation_ai_titles``, not the dropped vtable).

    ``committed-file`` (#1093): with no caller transaction the FTS attempt
    opens its own read snapshot, so the failure it raises must roll that
    snapshot back before the LIKE path begins its own. Transaction failure
    detected: the failed FTS attempt's snapshot left open, so the LIKE count
    and page run inside it and the connection is returned holding a read
    transaction (a long-lived reader then pins the WAL)."""
    require_fts5()
    if store == "memory":
        c = _conn()
    else:
        c = sqlite3.connect(tmp_path / "conversations.db")
        db._apply_cache_schema(c)
    _seed_titles(c)
    # Simulate a missing/corrupt title vtable on an FTS5-capable build: drop the
    # sync triggers (so the DROP doesn't strand a live trigger) then the vtable.
    db._drop_conversation_title_fts_triggers(c)
    c.execute("DROP TABLE IF EXISTS conversation_title_fts")
    if store != "memory":
        c.commit()
        assert not c.in_transaction
    # fts_available=True mirrors the live derivation (fts5_unavailable is NOT set,
    # so the kernel believes FTS is usable) — the per-table miss is what we test.
    res = cq.search_conversations(c, "refactor", kind="title", fts_available=True)
    assert res["kind"] == "title"
    assert res["mode"] == "like"                   # degraded, not a 500
    assert {h["session_id"] for h in res["hits"]} == {"s1"}
    assert res["total"] == 1
    if store != "memory":
        assert not c.in_transaction
    c.close()


def test_title_in_search_kinds_not_find_kinds():
    """P1-1 (kernel side): the cross-session search-kind set carries ``title``;
    the find-kind set does NOT (so the shared /find route 400s it, not 500s)."""
    assert "title" in cq._SEARCH_KINDS
    assert "title" not in cq._FIND_KINDS


def test_find_in_conversation_rejects_title_kind():
    """find_in_conversation must reject ``title`` with ValueError (route → 400),
    NOT reach _KIND_COLUMN/_FIND_KIND_COLUMNS and KeyError → 500."""
    c = _conn()
    _seed_titles(c)
    with pytest.raises(ValueError):
        cq.find_in_conversation(c, "s1", "refactor", kind="title")


# --- I-2d: filtered-search -------------------------------------------------

def _seed_filter_corpus(c):
    # proj-a session, cost via a session_entries-backed turn; matches 'needle'.
    _msg(c, session_id="sa", uuid="ua", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="the needle in the haystack", model=_MODEL, msg_id="m1",
         req_id="r1", cwd="/home/u/proj-a")
    # proj-b session; also matches 'needle'.
    _msg(c, session_id="sb", uuid="ub", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-02T00:00:00Z", entry_type="assistant",
         text="another needle entirely", model=_MODEL, msg_id="m2",
         req_id="r2", cwd="/home/u/proj-b")


@pytest.mark.parametrize("fa", [True, False])
def test_search_applies_project_filter(fa):
    c = _conn()
    if fa:
        require_fts5()
    _seed_filter_corpus(c)
    cc._recompute_conversation_sessions(c)         # make the rollup authoritative
    base = cq.search_conversations(c, "needle", kind="all", fts_available=fa)
    assert base["total"] == 2
    filt = cq.search_conversations(c, "needle", kind="all",
                                   projects=["proj-a"], fts_available=fa)
    assert {h["session_id"] for h in filt["hits"]} == {"sa"}
    assert filt["total"] == 1
    assert "filter_degraded" not in filt           # rollup authoritative


@pytest.mark.parametrize("fa", [True, False])
def test_search_applies_date_filter(fa):
    c = _conn()
    if fa:
        require_fts5()
    _seed_filter_corpus(c)
    cc._recompute_conversation_sessions(c)
    # date_to is the EXCLUSIVE next-day bound; only sa (2026-06-01) qualifies.
    res = cq.search_conversations(c, "needle", kind="all",
                                  date_to="2026-06-02T00:00:00Z", fts_available=fa)
    assert {h["session_id"] for h in res["hits"]} == {"sa"}
    assert res["total"] == 1


@pytest.mark.parametrize("fa", [True, False])
def test_title_kind_respects_filters(fa):
    c = _conn()
    if fa:
        require_fts5()
    # two anchorable sessions, both titles match 'plan'.
    _msg(c, session_id="ta", uuid="hta", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="x",
         cwd="/home/u/proj-a")
    _title(c, "ta", "the grand plan")
    _msg(c, session_id="tb", uuid="htb", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-02T00:00:00Z", entry_type="human", text="y",
         cwd="/home/u/proj-b")
    _title(c, "tb", "another plan")
    cc._recompute_conversation_sessions(c)
    res = cq.search_conversations(c, "plan", kind="title",
                                  projects=["proj-b"], fts_available=fa)
    assert {h["session_id"] for h in res["hits"]} == {"tb"}
    assert res["total"] == 1


@pytest.mark.parametrize("fa", [True, False])
def test_search_filter_degraded_uses_session_max_ts(fa):
    """P1-5: when the rollup is pending and a rollup-only filter is requested,
    drop project/cost/rebuild, set filter_degraded, and apply the date axis via
    the session MAX(timestamp_utc) prefilter — NOT by matched-row ts."""
    c = _conn()
    if fa:
        require_fts5()
    # session sx: an OLD matching turn (the 'needle') but a NEW later turn that
    # carries the session's MAX(timestamp_utc). The matched-row ts is old; the
    # session activity is new. A correct date_from over session activity must KEEP
    # this session; a (wrong) matched-row-ts filter would DROP it.
    _msg(c, session_id="sx", uuid="old", source_path="x.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
         text="the needle (old turn)", model=_MODEL, msg_id="m1", req_id="r1",
         cwd="/home/u/proj-x")
    _msg(c, session_id="sx", uuid="new", source_path="x.jsonl", byte_offset=1,
         timestamp_utc="2026-06-20T00:00:00Z", entry_type="human",
         text="later activity, nothing relevant", cwd="/home/u/proj-x")
    # Arm the rollup-pending flag so the kernel takes the DEGRADED branch.
    db._set_cache_meta(c, "conversation_sessions_backfill_pending", "1")
    res = cq.search_conversations(
        c, "needle", kind="all",
        date_from="2026-06-15T00:00:00Z",   # > matched-row ts, < session MAX ts
        cost_min=1.0,                        # rollup-only axis -> dropped + degraded
        fts_available=fa)
    assert res.get("filter_degraded") is True
    # The session is KEPT (its MAX(ts)=2026-06-20 >= date_from), proving the
    # prefilter uses session activity not the old matched-row ts.
    assert {h["session_id"] for h in res["hits"]} == {"sx"}
    assert res["total"] == 1


def test_search_no_filter_byte_stable():
    c = _conn()
    _seed_filter_corpus(c)
    a = cq.search_conversations(c, "needle", kind="all", fts_available=False)
    assert "filter_degraded" not in a
    # The pre-filter-arg signature still defaults to the same shape.
    b = cq.search_conversations(c, "needle", kind="all", fts_available=False,
                                date_from=None, date_to=None, projects=None,
                                cost_min=None, cost_max=None, rebuild_min=None)
    assert a == b


# --- HTTP routes: the kind-validation split (P1-1) ------------------------

def _seed_http_cache(ns):
    cache = ns["open_cache_db"]()
    cache.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain) VALUES('s1','h1',NULL,'a.jsonl',0,"
        "'2026-06-01T00:00:00Z','human','hi','[]',NULL,NULL,NULL,"
        "'/home/u/proj','main',0)")
    cache.execute(cc._AI_TITLE_UPSERT_SQL, ("s1", "refactor the cache", "a.jsonl", 0))
    import _cctally_cache as _cc
    _cc._recompute_conversation_sessions(cache)
    cache.commit()
    cache.close()
    # The dashboard's startup owner creates the conversation store (and
    # bridges these legacy cache rows into it) before bind; a request thread
    # never creates a store (#787 spec 5.8).
    ns["open_conversations_db"]().close()


def _boot(ns, tmp_path, monkeypatch):
    import datetime as dt
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    _seed_http_cache(ns)
    HandlerCls = ns["DashboardHTTPHandler"]
    SnapshotRef = ns["_SnapshotRef"]
    SSEHub = ns["SSEHub"]
    DataSnapshot = ns["DataSnapshot"]
    snap = DataSnapshot(
        current_week=None, forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=dt.datetime(2026, 6, 3, 12, 0, tzinfo=dt.timezone.utc),
        percent_milestones=[], weekly_history=[],
        weekly_periods=[], monthly_periods=[],
        blocks_panel=[], daily_panel=[])
    HandlerCls.snapshot_ref = SnapshotRef(snap)
    HandlerCls.hub = SSEHub()
    HandlerCls.sync_lock = threading.Lock()
    HandlerCls.run_sync_now = staticmethod(lambda: None)
    HandlerCls.cctally_host = "127.0.0.1"
    HandlerCls.cctally_expose_transcripts = False
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), HandlerCls)
    srv._test_thread = start(srv)
    return srv


def _get(port, path):
    from http.client import HTTPConnection
    c = HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    status = r.status
    c.close()
    return status, body


def test_find_rejects_title_and_files_400_not_500(tmp_path, monkeypatch):
    """P1-1: the shared /find route 400s ``title``/``files`` (find-kind set
    excludes them); it MUST NOT reach the kernel and 500 with a KeyError."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        for k in ("title", "files"):
            status, body = _get(port, f"/api/conversation/s1/find?q=refactor&kind={k}")
            assert status == 400, (k, status, body)
            assert "error" in json.loads(body)
        # sanity: a real find kind still 200s.
        status, _ = _get(port, "/api/conversation/s1/find?q=hi&kind=prompts")
        assert status == 200
    finally:
        stop(srv, srv._test_thread)


def test_search_accepts_title_kind_200(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, body = _get(port, "/api/conversation/search?q=refactor&kind=title")
        assert status == 200, (status, body)
        out = json.loads(body)
        assert out["kind"] == "title"
        assert out["hits"] and out["hits"][0]["session_id"] == "s1"
        assert out["hits"][0]["match_kinds"] == ["title"]
    finally:
        stop(srv, srv._test_thread)


def test_search_threads_browse_filters(tmp_path, monkeypatch):
    """The search route must call _parse_conversation_filters and thread the
    filter dict into the kernel (filtered-search). A project filter for a
    non-existent project drops the only hit."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, body = _get(
            port, "/api/conversation/search?q=refactor&kind=title&projects=nope")
        assert status == 200, (status, body)
        out = json.loads(body)
        assert out["hits"] == [] and out["total"] == 0
        # a bad numeric filter is a 400 (parsed by _parse_conversation_filters).
        status, body = _get(
            port, "/api/conversation/search?q=refactor&kind=title&cost_min=abc")
        assert status == 400, (status, body)
    finally:
        stop(srv, srv._test_thread)


# --- #1036: title snippets as structured parts ------------------------------

_BRACKET_TITLE = "Review [P1] findings for the tail poll"


def _title_original(c, sid, left="[", right="]"):
    """SQLite's own title snippet() on the same connection (the pre-#1036 bytes,
    or with empty decorations the plain excerpt)."""
    return c.execute(
        f"SELECT snippet(conversation_title_fts, 0, '{left}', '{right}', ' … ', 12) "
        "FROM conversation_title_fts f "
        "JOIN conversation_ai_titles t ON t.rowid = f.rowid "
        "WHERE f.conversation_title_fts MATCH ? AND t.session_id = ?",
        (cq._fts_query("findings", prefix_last=True), sid)).fetchone()[0]


def _seed_bracket_title(c, title=_BRACKET_TITLE):
    _msg(c, session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="hi",
         cwd="/home/u/proj")
    _title(c, "s1", title)


def test_1036_title_parts_keep_literal_brackets():
    """Detects the title path marking `[P1]` (a literal bracket pair in the
    title) instead of only the FTS match, or drifting the legacy bytes."""
    require_fts5()
    c = _conn()
    _seed_bracket_title(c)
    res = cq.search_conversations(c, "findings", kind="title")
    hit, = res["hits"]
    assert res["total"] == 1
    assert hit["snippet_parts"] == [
        {"text": "Review [P1] ", "match": False},
        {"text": "findings", "match": True},
        {"text": " for the tail poll", "match": False}]
    assert hit["snippet"] == _title_original(c, "s1") == \
        "Review [P1] [findings] for the tail poll"


def test_1036_title_marker_collision_is_retried():
    """Detects a title containing a whole marker being parsed as a highlight."""
    require_fts5()
    c = _conn()
    _seed_bracket_title(c, "cctally:0:open findings [x]")
    hit, = cq.search_conversations(c, "findings", kind="title")["hits"]
    assert "".join(p["text"] for p in hit["snippet_parts"]) == \
        _title_original(c, "s1", left="", right="")
    assert [p["text"] for p in hit["snippet_parts"] if p["match"]] == ["findings"]
    assert hit["snippet"] == _title_original(c, "s1")


def test_1036_title_extraction_reads_the_page_snapshot(tmp_path, monkeypatch):
    """Detects a title rewritten by the production upsert between page
    selection and extraction dropping the selected hit, changing `total`, or
    publishing the new title's snippet."""
    require_fts5()
    path = tmp_path / "conversations.db"
    w = sqlite3.connect(path)
    db._apply_cache_schema(w)
    w.execute("PRAGMA journal_mode=WAL")
    _seed_bracket_title(w)
    w.commit()
    real = cq._extract_fts_snippets
    fired = []

    def racing(conn, **kw):
        if not fired:
            fired.append(1)
            w.execute(cc._AI_TITLE_UPSERT_SQL,
                      ("s1", "completely different words", "a.jsonl", 0))
            w.commit()
        return real(conn, **kw)

    monkeypatch.setattr(cq, "_extract_fts_snippets", racing)
    reader = sqlite3.connect(path)
    res = cq.search_conversations(reader, "findings", kind="title")
    assert fired
    assert res["total"] == 1
    hit, = res["hits"]
    assert hit["snippet"] == "Review [P1] [findings] for the tail poll"
    assert [p["text"] for p in hit["snippet_parts"] if p["match"]] == ["findings"]


def test_1036_title_like_hit_is_one_unmatched_part():
    """Detects the title LIKE fallback inventing a highlight or changing bytes."""
    c = _conn()
    _seed_bracket_title(c)
    hit, = cq.search_conversations(
        c, "findings", kind="title", fts_available=False)["hits"]
    assert hit["snippet"] == _BRACKET_TITLE
    assert hit["snippet_parts"] == [{"text": _BRACKET_TITLE, "match": False}]


# --- #1062: account-scoped title search over HTTP ---------------------------

_1062_A = "a" * 32
_1062_B = "b" * 32
# (session, account per message, title). Each file: messages at 0 and 200,
# the title at its own offset 100, the cursor past the last line.
_1062_SESSIONS = (
    ("sa1", (_1062_A, _1062_A), _BRACKET_TITLE),
    ("sb1", (_1062_B, _1062_B), "Bravo findings first"),
    ("sa2", (_1062_A, _1062_A), "Alpha findings second"),
    ("smx", (_1062_A, _1062_B), "Mixed findings session"),
)


def _seed_1062_scoped_titles(ns):
    conn = ns["open_conversations_db"]()
    try:
        for index, (sid, accounts, title) in enumerate(_1062_SESSIONS):
            path = f"{sid}.jsonl"
            for line, account in zip((0, 200), accounts):
                conn.execute(
                    "INSERT INTO conversation_messages "
                    "(session_id,uuid,source_path,byte_offset,timestamp_utc,"
                    " entry_type,text,blocks_json,cwd,is_sidechain,"
                    " account_key) VALUES(?,?,?,?,?,'human',?,'[]',"
                    "'/home/u/proj',0,?)",
                    (sid, f"{sid}-{line}", path, line,
                     f"2026-06-0{index + 2}T00:0{line // 100}:00Z",
                     f"prompt {sid} {line}", account))
            conn.execute(cc._AI_TITLE_UPSERT_SQL, (sid, title, path, 100))
            conn.execute(
                "INSERT INTO conversation_source_files"
                "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at) "
                "VALUES(?,300,1,300,'2026-06-09T00:00:00Z')", (path,))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("qualified", [False, True], ids=["legacy", "source"])
def test_1062_account_title_search_route_is_fts_and_account_local(
        tmp_path, monkeypatch, qualified):
    """Detects the dashboard's account-focused title search returning nothing
    (or another account's or a mixed session's title), or degrading to LIKE
    with no highlight, on the legacy and the qualified `source=claude` route."""
    require_fts5()
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    _seed_1062_scoped_titles(ns)
    disp = ns["_load_sibling"]("_lib_conversation_dispatch")
    try:
        port = srv.server_address[1]
        prefix = "source=claude&" if qualified else ""
        status, body = _get(
            port, f"/api/conversation/search?{prefix}q=findings&kind=title")
        assert status == 200, (status, body)
        assert json.loads(body)["total"] == 4, "unscoped reference"
        status, body = _get(
            port, f"/api/conversation/search?{prefix}q=findings&kind=title"
            f"&account={_1062_A}")
        assert status == 200, (status, body)
        out = json.loads(body)
        assert out["mode"] == "fts"
        assert out["total"] == 2
        if qualified:
            keys = {h["conversation_key"] for h in out["hits"]}
            assert keys == {disp._mint_claude_conversation_key(s)
                            for s in ("sa1", "sa2")}
        else:
            assert {h["session_id"] for h in out["hits"]} == {"sa1", "sa2"}
        for hit in out["hits"]:
            assert [p["text"] for p in hit["snippet_parts"] if p["match"]] == [
                "findings"]
    finally:
        stop(srv, srv._test_thread)


# --- #1093: the first-turn anchor is resolved per matched session ----------

import re  # noqa: E402 — section-local, kept beside the cases that use it

_1093_SONNET = "claude-sonnet-4-5"
_1093_A = "a" * 32
_1093_B = "b" * 32


def _window_title_search(conn, q, limit, offset, fts_available, filt_sql="",
                         filt_params=()):
    """The reference oracle: ``_search_title`` as it stood before #1093
    (``main`` ``7da78fea9``), verbatim, with its helpers reached through ``cq``.
    It ranks every message row with ``ROW_NUMBER()`` to find each session's
    first turn, so its results are the semantics the rewrite must keep."""
    fpred = cq._session_filter_pred("t.session_id", filt_sql)
    fargs = tuple(filt_params)
    anchor_cte = (
        "anchor AS ("
        "  SELECT session_id, aid, auuid, ats, acwd, amid, arqd FROM ("
        "    SELECT session_id, id AS aid, uuid AS auuid, timestamp_utc AS ats, "
        "           cwd AS acwd, msg_id AS amid, req_id AS arqd, "
        "           ROW_NUMBER() OVER (PARTITION BY session_id "
        "                              ORDER BY timestamp_utc, id) AS rn "
        "    FROM conversation_messages WHERE session_id IS NOT NULL"
        "  ) WHERE rn = 1)")

    if fts_available:
        fts_q = cq._fts_query(q, prefix_last=True)
        with cq._read_snapshot(conn):
            total = conn.execute(
                "WITH " + anchor_cte + " "
                "SELECT COUNT(*) FROM conversation_title_fts f "
                "JOIN conversation_ai_titles t ON t.rowid = f.rowid "
                "JOIN anchor a ON a.session_id = t.session_id "
                f"WHERE f.conversation_title_fts MATCH ?{fpred}",
                (fts_q, *fargs)).fetchone()[0]
            page = conn.execute(
                "WITH " + anchor_cte + " "
                "SELECT t.session_id, a.auuid, a.ats, a.acwd, a.amid, a.arqd, "
                "       t.rowid AS trid "
                "FROM conversation_title_fts f "
                "JOIN conversation_ai_titles t ON t.rowid = f.rowid "
                "JOIN anchor a ON a.session_id = t.session_id "
                f"WHERE f.conversation_title_fts MATCH ?{fpred} "
                "ORDER BY a.ats DESC, t.session_id DESC LIMIT ? OFFSET ?",
                (fts_q, *fargs, limit, offset)).fetchall()
            snips = (cq._extract_fts_snippets(
                conn, fts_table="conversation_title_fts",
                content_table="conversation_ai_titles", content_rowid="rowid",
                source_col="ai_title", col=0, match_expr=fts_q,
                rowids=[r[6] for r in page]) if page else {})
        hits = []
        for (sid, auuid, ats, acwd, amid, arqd, trid) in page:
            snip, parts = snips.get(trid, (None, []))
            hits.append(cq._row_to_hit(auuid, sid, ats, acwd, snip, amid, arqd,
                                       match_kinds=["title"],
                                       snippet_parts=parts))
        return {"query": q, "mode": "fts",
                "hits": cq._attach_search_meta(conn, hits),
                "total": total}
    like = cq._like_pattern(q)
    total = conn.execute(
        "WITH " + anchor_cte + " "
        "SELECT COUNT(*) FROM conversation_ai_titles t "
        "JOIN anchor a ON a.session_id = t.session_id "
        f"WHERE t.ai_title LIKE ? ESCAPE '\\'{fpred}",
        (like, *fargs)).fetchone()[0]
    rows = conn.execute(
        "WITH " + anchor_cte + " "
        "SELECT t.session_id, a.auuid, a.ats, a.acwd, a.amid, a.arqd, "
        "       t.ai_title AS snip "
        "FROM conversation_ai_titles t "
        "JOIN anchor a ON a.session_id = t.session_id "
        f"WHERE t.ai_title LIKE ? ESCAPE '\\'{fpred} "
        "ORDER BY a.ats DESC, t.session_id DESC LIMIT ? OFFSET ?",
        (like, *fargs, limit, offset)).fetchall()

    hits = [cq._row_to_hit(auuid, sid, ats, acwd, snip, amid, arqd,
                           match_kinds=["title"])
            for (sid, auuid, ats, acwd, amid, arqd, snip) in rows]
    return {"query": q, "mode": "like",
            "hits": cq._attach_search_meta(conn, hits),
            "total": total}


def _1093_assert_oracle(conn, q, fa, limit=50, offset=0, **filters):
    """One ``search_conversations(kind="title")`` call compared with the
    oracle run on the same connection, through the same filter resolution and
    the same limit/offset clamping. Returns the kernel's result."""
    res = cq.search_conversations(conn, q, kind="title", fts_available=fa,
                                  limit=limit, offset=offset, **filters)
    full = {k: filters.get(k) for k in cq._FILTER_KEYS}
    for k in ("projects", "models"):
        full[k] = list(full[k]) if full[k] else None
    filt_sql, filt_params, degraded = cq._resolve_search_session_filter(
        conn, full)
    want = _window_title_search(conn, q, max(1, min(int(limit), 200)),
                                max(0, int(offset)), fa, filt_sql, filt_params)
    got = {k: v for k, v in res.items()
           if k not in ("kind", "search_depth", "filter_degraded")}
    case = (q, fa, limit, offset, filters)
    assert list(got.items()) == list(want.items()), case
    assert res.get("filter_degraded", False) is degraded, case
    assert res["mode"] == ("fts" if fa else "like"), case
    return res


def _seed_1093_oracle(c):
    """Nine anchorable sessions whose titles match ``plan`` (three pages at
    limit 3), across two projects, two model families and three months; a
    session whose two earliest rows share a timestamp (the lower id is the
    anchor), one whose earliest row has a NULL timestamp, one with only NULL
    timestamps, an orphan title, and a non-matching titled session."""
    offsets = {}

    def row(sid, uuid, ts, etype="human", model=None, proj="proj-a"):
        path = f"{sid}.jsonl"
        offsets[path] = offsets.get(path, -1) + 1
        _msg(c, session_id=sid, uuid=uuid, source_path=path,
             byte_offset=offsets[path], timestamp_utc=ts, entry_type=etype,
             text=f"turn {uuid}", model=model,
             msg_id=f"m-{uuid}" if etype == "assistant" else None,
             req_id=f"r-{uuid}" if etype == "assistant" else None,
             cwd=f"/home/u/{proj}")

    # The tie: the first-inserted (lower id) row wins although its uuid sorts
    # after its twin's.
    row("t-tie", "z-first", "2026-06-10T00:00:00Z")
    row("t-tie", "a-second", "2026-06-10T00:00:00Z")
    row("t-tie", "tie-late", "2026-06-10T00:05:00Z", "assistant", _1093_SONNET)
    _title(c, "t-tie", "plan alpha tie")
    # The NULL-first anchor: inserted after a dated row, still the anchor.
    row("t-null", "n-ts", "2026-06-12T00:00:00Z", "assistant", _MODEL, "proj-b")
    row("t-null", "n-null", None, proj="proj-b")
    _title(c, "t-null", "plan alpha null")
    row("t-null2", "nn-1", None)
    row("t-null2", "nn-2", None, "assistant", _MODEL)
    _title(c, "t-null2", "plan all null")
    for i, (day, proj, model) in enumerate((
            ("2026-05-03", "proj-a", _MODEL),
            ("2026-05-20", "proj-b", _1093_SONNET),
            ("2026-06-05", "proj-a", _1093_SONNET),
            ("2026-06-20", "proj-b", _MODEL),
            ("2026-07-02", "proj-a", _MODEL),
            ("2026-07-15", "proj-b", _1093_SONNET))):
        sid = f"s0{i}"
        row(sid, f"{sid}-h", f"{day}T01:00:00Z", proj=proj)
        row(sid, f"{sid}-a", f"{day}T01:00:05Z", "assistant", model, proj)
        _title(c, sid, f"plan {'alpha ' if i % 2 else ''}number {i}")
    _title(c, "t-orphan", "plan alpha orphan")          # no message rows
    row("t-other", "o-h", "2026-06-15T00:00:00Z")
    _title(c, "t-other", "budget weekly usage")


@pytest.mark.parametrize("fa", [True, False], ids=["fts", "like"])
def test_1093_title_search_matches_window_oracle(fa):
    """Accuracy failure detected: the per-session anchor rewrite returning a
    different Titles hit, anchor row, order, page or total than the old
    whole-table window (a tie broken by uuid instead of id, a NULL-timestamp
    first turn skipped, an orphan title counted, a filter or a pending rollup
    applied differently, a clamped limit read raw)."""
    if fa:
        require_fts5()
    c = _conn()
    _seed_1093_oracle(c)
    cc._recompute_conversation_sessions(c)
    filter_cases = (
        {},
        {"date_from": "2026-06-01T00:00:00Z", "date_to": "2026-07-01T00:00:00Z"},
        {"projects": ["proj-b"]},
        {"models": ["sonnet"]},
        {"date_from": "2026-05-15T00:00:00Z", "models": ["opus"],
         "projects": ["proj-a"]},
    )
    for pending in (False, True):
        if pending:
            db._set_cache_meta(c, "conversation_sessions_backfill_pending", "1")
        for limit, offset in ((3, 0), (3, 3), (3, 6), (3, 30), (0, 0),
                              (500, 0)):
            res = _1093_assert_oracle(c, "plan", fa, limit, offset)
            if (limit, offset) == (3, 30):
                assert res["hits"] == [] and res["total"] == 9
        for filters in filter_cases:
            for limit, offset in ((3, 0), (500, 0)):
                _1093_assert_oracle(c, "plan", fa, limit, offset, **filters)
        _1093_assert_oracle(c, "alpha", fa, 2, 1)
        if pending:
            assert _1093_assert_oracle(
                c, "plan", fa, projects=["proj-b"])["filter_degraded"] is True

    # Independent of the oracle: the anchors and the P2-9 exclusion.
    res = cq.search_conversations(c, "plan", kind="title", fts_available=fa,
                                  limit=500)
    by_sid = {h["session_id"]: h for h in res["hits"]}
    assert res["total"] == len(res["hits"]) == 9
    assert by_sid["t-tie"]["uuid"] == "z-first"
    assert by_sid["t-null"]["uuid"] == "n-null"
    assert by_sid["t-null"]["ts"] is None
    assert "t-orphan" not in by_sid and "t-other" not in by_sid


def _1093_store(tmp_path, sessions):
    """A file-backed conversations store with an attached cache.db, as the
    account-scoped tests build it. ``sessions`` holds ``(sid, title, rows)``
    with rows ``(uuid, ts, account, entry_type, model)``; each titled session
    has valid title provenance (its file cursor past the title's offset)."""
    cache_path = tmp_path / "cache.db"
    cache_conn = sqlite3.connect(cache_path)
    db._apply_cache_schema(cache_conn)
    cache_conn.commit()
    cache_conn.close()
    conn = sqlite3.connect(tmp_path / "conversations.db")
    db._apply_conversations_schema(conn)
    for sid, title, rows in sessions:
        path = f"{sid}.jsonl"
        for n, (uuid, ts, account, etype, model) in enumerate(rows):
            conn.execute(
                "INSERT INTO conversation_messages "
                "(session_id,uuid,source_path,byte_offset,timestamp_utc,"
                " entry_type,text,blocks_json,model,msg_id,req_id,cwd,"
                " is_sidechain,account_key) "
                "VALUES(?,?,?,?,?,?,?,'[]',?,?,?,'/home/u/proj',0,?)",
                (sid, uuid, path, 200 * n, ts, etype, f"prompt {sid} {n}",
                 model, f"m-{uuid}" if model else None,
                 f"r-{uuid}" if model else None, account))
        if title is not None:
            end = 200 * len(rows) + 300
            conn.execute(cc._AI_TITLE_UPSERT_SQL, (sid, title, path, 100))
            conn.execute(
                "INSERT INTO conversation_source_files"
                "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at) "
                "VALUES(?,?,1,?,'2026-06-09T00:00:00Z')", (path, end, end))
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS cache_db", (str(cache_path),))
    cc._recompute_conversation_sessions(conn)
    conn.commit()
    return conn


def _1093_rows(account, *specs):
    return [(uuid, ts, account, etype, model)
            for (uuid, ts, etype, model) in specs]


_1093_SCOPED_SESSIONS = (
    ("pa-tie", "plan alpha tie", _1093_rows(
        _1093_A, ("z-first", "2026-06-10T00:00:00Z", "human", None),
        ("a-second", "2026-06-10T00:00:00Z", "human", None),
        ("pa-tie-a", "2026-06-10T00:01:00Z", "assistant", _MODEL))),
    ("pa-null", "plan alpha null", _1093_rows(
        _1093_A, ("pn-ts", "2026-06-11T00:00:00Z", "assistant", _MODEL),
        ("pn-null", None, "human", None))),
    ("pa-3", "plan three", _1093_rows(
        _1093_A, ("p3-h", "2026-06-12T00:00:00Z", "human", None))),
    ("pb", "plan foreign", _1093_rows(
        _1093_B, ("pb-h", "2026-06-13T00:00:00Z", "human", None))),
    ("pmx-ab", "plan mixed", [
        ("mx-a", "2026-06-14T00:00:00Z", _1093_A, "human", None),
        ("mx-b", "2026-06-14T00:01:00Z", _1093_B, "human", None)]),
    ("pu-null", "plan unattributed null", _1093_rows(
        None, ("un-h", "2026-06-15T00:00:00Z", "human", None),
        ("un-a", "2026-06-15T00:01:00Z", "assistant", _MODEL))),
    ("pu-lit", "plan unattributed literal", _1093_rows(
        "unattributed", ("ul-h", "2026-06-16T00:00:00Z", "human", None))),
    ("pmx-un", "plan mixed unattributed", [
        ("mu-n", "2026-06-17T00:00:00Z", None, "human", None),
        ("mu-a", "2026-06-17T00:01:00Z", _1093_A, "human", None)]),
    ("porph", "plan orphan", []),
)


@pytest.mark.parametrize("scope,expected", [
    (_1093_A, {"pa-tie", "pa-null", "pa-3"}),
    ("unattributed", {"pu-null", "pu-lit"}),
], ids=["real", "unattributed"])
@pytest.mark.parametrize("fa", [True, False], ids=["fts", "like"])
def test_1093_scoped_title_search_matches_window_oracle(tmp_path, fa, scope,
                                                        expected):
    """Privacy/accuracy failure detected: on an account-scoped connection the
    rewritten anchor reading the unscoped message table (an anchor from
    another account's row, a foreign or mixed session admitted) or dropping a
    scoped session the old window returned, for a real account and for
    ``unattributed`` with both stored spellings (NULL and the literal)."""
    if fa:
        require_fts5()
    conn = _1093_store(tmp_path, _1093_SCOPED_SESSIONS)
    cc.scope_conversations_db_to_account(conn, scope)
    try:
        res = _1093_assert_oracle(conn, "plan", fa)
        _1093_assert_oracle(conn, "plan", fa, 1, 1)
        assert len(res["hits"]) >= 2
        assert {h["session_id"] for h in res["hits"]} == expected
        assert res["total"] == len(expected)
        by_sid = {h["session_id"]: h for h in res["hits"]}
        if scope == _1093_A:
            assert by_sid["pa-tie"]["uuid"] == "z-first"
            assert by_sid["pa-null"]["uuid"] == "pn-null"
    finally:
        conn.close()


def _1093_build_t2(untitled_rows, nonmatching_titled, rows_per_nonmatching):
    """Two matched sessions of three rows (title ``refactor the cache``) among
    untitled sessions' rows and non-matching titled sessions."""
    c = _conn()

    def msg(sid, i, ts):
        c.execute(
            "INSERT INTO conversation_messages(session_id,uuid,source_path,"
            "byte_offset,timestamp_utc,entry_type,text) VALUES(?,?,?,?,?,?,?)",
            (sid, f"{sid}-{i}", f"{sid}.jsonl", i, ts, "human", "x" * 50))

    for s in range(2):
        for i in range(3):
            msg(f"m{s}", i, f"2026-06-0{s + 1}T00:00:0{i}Z")
        c.execute(cc._AI_TITLE_UPSERT_SQL,
                  (f"m{s}", "refactor the cache", f"m{s}.jsonl", 0))
    for i in range(untitled_rows):
        msg(f"u{i % 20}", i, f"2026-05-01T00:{i // 60 % 60:02d}:{i % 60:02d}Z")
    for s in range(nonmatching_titled):
        for i in range(rows_per_nonmatching):
            msg(f"n{s}", i, f"2026-04-01T00:00:{i % 60:02d}Z")
        c.execute(cc._AI_TITLE_UPSERT_SQL,
                  (f"n{s}", "budget weekly usage", f"n{s}.jsonl", 0))
    return c


def _1093_ticks(conn, fn):
    """Progress-handler ticks (one per 10 VM operations) of ``fn()``."""
    ticks = [0]

    def tick():
        ticks[0] += 1
        return 0

    conn.set_progress_handler(tick, 10)
    try:
        out = fn()
    finally:
        conn.set_progress_handler(None, 0)
    return ticks[0], out


def _1093_t2_sql(fa):
    """The bare matched-title relation and the all-titles counterexample
    (anchors materialized for every title, the match applied afterwards; with
    the existence count) for the in-test calibration controls."""
    sub = ("(SELECT cm.{col} FROM conversation_messages cm "
           "WHERE cm.session_id = t.session_id "
           "ORDER BY cm.timestamp_utc, cm.id LIMIT 1)")
    anchors = (sub.format(col="id") + " AS aid, "
               + sub.format(col="timestamp_utc") + " AS ats")
    exists = ("EXISTS (SELECT 1 FROM conversation_messages cm "
              "WHERE cm.session_id = t.session_id)")
    if fa:
        src = ("conversation_title_fts f "
               "JOIN conversation_ai_titles t ON t.rowid = f.rowid")
        pred = "f.conversation_title_fts MATCH ?"
        wrong = ("WITH m AS MATERIALIZED (SELECT t.rowid AS trid, "
                 "t.session_id AS sid, " + anchors
                 + " FROM conversation_ai_titles t) "
                 "SELECT m.sid FROM m JOIN conversation_title_fts f "
                 "ON f.rowid = m.trid WHERE f.conversation_title_fts MATCH ? "
                 "AND m.aid IS NOT NULL ORDER BY m.ats DESC, m.sid DESC "
                 "LIMIT 50")
    else:
        src = "conversation_ai_titles t"
        pred = "t.ai_title LIKE ? ESCAPE '\\'"
        wrong = ("WITH m AS MATERIALIZED (SELECT t.ai_title AS tt, "
                 "t.session_id AS sid, " + anchors
                 + " FROM conversation_ai_titles t) "
                 "SELECT m.sid FROM m WHERE m.tt LIKE ? ESCAPE '\\' "
                 "AND m.aid IS NOT NULL ORDER BY m.ats DESC, m.sid DESC "
                 "LIMIT 50")
    bare = f"SELECT t.session_id FROM {src} WHERE {pred}"
    count = f"SELECT COUNT(*) FROM {src} WHERE {pred} AND {exists}"
    return bare, wrong, count


@pytest.mark.parametrize("fa", [True, False], ids=["fts", "like"])
def test_1093_title_search_cost_ignores_corpus_and_unmatched_titles(fa):
    """Machine-safety failure detected: a Titles search whose work grows with
    the stored transcript corpus (on a real store, minutes of disk reads per
    search) or with the number of non-matching titled sessions (anchors
    resolved for every title, not only the matched ones). In-test controls
    keep the bounds honest on the runner's SQLite: the old window must break
    the corpus bound and the all-titles counterexample the provenance bound."""
    if fa:
        require_fts5()
    arg = (cq._fts_query("refactor", prefix_last=True) if fa
           else cq._like_pattern("refactor"))
    bare, wrong, count = _1093_t2_sql(fa)

    def measure(conn):
        counted = {
            "cand": lambda: cq.search_conversations(
                conn, "refactor", kind="title", fts_available=fa),
            "old": lambda: _window_title_search(conn, "refactor", 50, 0, fa),
            "wrong": lambda: (conn.execute(count, (arg,)).fetchone(),
                              conn.execute(wrong, (arg,)).fetchall()),
            "bare": lambda: conn.execute(bare, (arg,)).fetchall(),
        }
        # Warm every callable (uncounted) first, so no count carries one-time
        # schema/statement preparation and none depends on measurement order.
        for fn in counted.values():
            fn()
        out = {}
        for name, fn in counted.items():
            out[name], result = _1093_ticks(conn, fn)
            if name == "cand":
                out["res"] = result
        assert len(out["res"]["hits"]) == out["res"]["total"] == 2
        return out

    small = measure(_1093_build_t2(400, 20, 20))
    corpus = measure(_1093_build_t2(4000, 20, 200))
    titled = measure(_1093_build_t2(400, 200, 20))
    mode = "fts" if fa else "like"
    summary = {name: {k: v for k, v in m.items() if k != "res"}
               for name, m in (("small", small), ("corpus", corpus),
                               ("titled", titled))}
    print(f"#1093 T2 {mode} ticks: {summary}")
    assert small["res"] == corpus["res"] == titled["res"]

    # Corpus independence (bound 1.5x), calibrated by the old window.
    assert corpus["old"] > 1.5 * small["old"], (
        f"calibration error: the old window's corpus growth "
        f"{small['old']} -> {corpus['old']} stays within the 1.5x bound")
    assert corpus["cand"] <= 1.5 * small["cand"], (
        f"Titles search work grows with the corpus: "
        f"{small['cand']} -> {corpus['cand']} ticks")

    # Matched-set provenance, calibrated by the all-titles counterexample.
    # The whole call evaluates the match predicate over every title once per
    # statement that carries it: the count and the page, plus (FTS only) the
    # snippet extraction, whose MATCH + snippet() costs about two bare
    # relations. So a call that grows only with the predicate grows by about
    # that many bare increments; anchors resolved for unmatched titles add far
    # more.
    match_evaluations = 4 if fa else 2
    bound = match_evaluations * (titled["bare"] - small["bare"]) + 20
    assert titled["wrong"] - small["wrong"] > bound, (
        f"calibration error: the all-titles counterexample's increment "
        f"{titled['wrong'] - small['wrong']} stays within the bound {bound}")
    assert titled["cand"] - small["cand"] <= bound, (
        f"Titles search work grows with non-matching titles: increment "
        f"{titled['cand'] - small['cand']} over the bound {bound}")


_1093_PLAN_KEYWORDS = {
    "AS", "SELECT", "FROM", "WHERE", "ON", "JOIN", "CROSS", "INNER", "LEFT",
    "GROUP", "ORDER", "LIMIT", "USING", "AND", "OR", "NOT", "INDEXED", "UNION",
    "WINDOW", "HAVING", "SET"}


def _1093_k1_rule(plan, names, indexes, scoped):
    """The K1 rule over one statement's ``EXPLAIN QUERY PLAN`` rows: every
    node that reads ``conversation_messages`` (by alias, schema-qualified or
    not, or by one of its indexes) outside a ``LIST SUBQUERY`` subtree (the
    session-scope filter) is a SEARCH whose constraint holds no range and
    either seeks ``session_id=?`` or is a primary-key equality; never a SCAN.
    On a scoped connection one more equality is accepted (see below)."""
    parent = {r[0]: r[1] for r in plan}
    detail = {r[0]: r[3] for r in plan}
    ctes = {d.split(" ", 1)[1] for d in detail.values()
            if d.startswith(("MATERIALIZE ", "CO-ROUTINE "))}
    bad = []
    for node, text in detail.items():
        up, in_list = parent.get(node), False
        while up in detail:
            if detail[up].startswith("LIST SUBQUERY"):
                in_list = True
                break
            up = parent.get(up)
        if in_list:
            continue
        m = re.match(r"(SCAN|SEARCH) (\S+)", text)
        table = m.group(2).rsplit(".", 1)[-1] if m else None
        by_index = any(re.search(rf"\b{re.escape(ix)}\b", text)
                       for ix in indexes)
        by_name = table in names
        if by_name and table in ctes and not by_index:
            bad.append(("ambiguous name: a CTE shares a message alias", text))
            continue
        if not (by_index or by_name):
            continue
        c = re.search(r"\(([^()]*)\)\s*$", text)
        constraint = c.group(1) if c else ""
        if (text.startswith("SCAN") or ">" in constraint or "<" in constraint):
            bad.append(text)
            continue
        # ``source_path=?`` alone is the #1062 scoped title view's own
        # eligibility test (a retained in-scope message of the session in the
        # title's file), which #1093 leaves as it is (spec D5): an exact
        # equality seek bounded by that one file, only on a scoped connection.
        if not ("session_id=?" in constraint
                or ("INTEGER PRIMARY KEY" in text and constraint == "rowid=?")
                or (scoped and constraint == "source_path=?")):
            bad.append(text)
    return bad


def _1093_k1_violations(conn, sql, scoped):
    """``_1093_k1_rule`` over one statement's plan on ``conn``: the message
    aliases come from the statement and the TEMP views (the scoped view
    reads the table under its own alias)."""
    texts = [sql] + [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_temp_master WHERE type='view' "
        "AND sql IS NOT NULL")]
    names = {"conversation_messages"}
    for text in texts:
        for m in re.finditer(
                r"\bconversation_messages\s+(?:AS\s+)?([A-Za-z_]\w*)", text,
                re.IGNORECASE):
            if m.group(1).upper() not in _1093_PLAN_KEYWORDS:
                names.add(m.group(1))
    indexes = {r[1] for r in conn.execute(
        "PRAGMA main.index_list('conversation_messages')")}
    plan = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    return _1093_k1_rule(plan, names, indexes, scoped), plan


_1093_RULE_NAMES = {"conversation_messages", "cm"}
_1093_RULE_INDEXES = {"idx_conv_source", "idx_conv_session_ts",
                      "idx_conv_messages_account_session"}


@pytest.mark.parametrize("plan,scoped,rejected", [
    ([(2, 0, 0, "SCAN main.conversation_messages")], True, True),
    ([(2, 0, 0, "SEARCH cm USING INTEGER PRIMARY KEY (rowid>?)")], False,
     True),
    ([(2, 0, 0, "SEARCH main.conversation_messages USING INDEX "
                "idx_conv_source (source_path=? AND byte_offset>?)")], True,
     True),
    ([(2, 0, 0, "SEARCH main.conversation_messages USING INDEX "
                "idx_conv_source (source_path=?)")], False, True),
    ([(2, 0, 0, "SEARCH conversation_messages USING INDEX "
                "idx_conv_session_ts (session_id>?)")], False, True),
    ([(2, 0, 0, "SEARCH main.conversation_messages USING INDEX "
                "idx_conv_source (source_path=?)")], True, False),
    ([(2, 0, 0, "SEARCH cm USING INTEGER PRIMARY KEY (rowid=?)")], False,
     False),
    ([(2, 0, 0, "SEARCH main.conversation_messages USING INDEX "
                "idx_conv_messages_account_session "
                "(account_key=? AND session_id=?)")], True, False),
    ([(2, 0, 0, "LIST SUBQUERY 1"),
      (5, 2, 0, "SCAN main.conversation_messages")], True, False),
], ids=["scoped-scan", "pk-range", "source-offset-range",
        "source-seek-unscoped", "session-range", "source-seek-scoped",
        "pk-equality", "account-session-seek", "list-subquery-scan"])
def test_1093_k1_plan_rule_rejects_corpus_walks(plan, scoped, rejected):
    """Machine-safety failure detected: a plan guard that cannot fail, which
    lets a whole-corpus message walk through the Titles plan check (a plain
    scan through the flattened scoped view, a primary-key or byte-offset range
    walk, a ``source_path`` seek on an unscoped connection, a ``session_id``
    range), or one that rejects the per-session seeks the search relies on."""
    bad = _1093_k1_rule(plan, _1093_RULE_NAMES, _1093_RULE_INDEXES, scoped)
    assert bool(bad) is rejected, bad


def _1093_plan_sessions():
    accounts = (_1093_A, None, "unattributed", _1093_B)
    out = []
    for i in range(60):
        sid = f"ps{i:02d}"
        model = _MODEL if i % 2 == 0 else _1093_SONNET
        out.append((sid, f"plan session {i}", _1093_rows(
            accounts[i % 4],
            (f"{sid}-h", f"2026-06-{1 + i % 28:02d}T00:00:00Z", "human", None),
            (f"{sid}-a", f"2026-06-{1 + i % 28:02d}T00:00:05Z", "assistant",
             model),
            (f"{sid}-h2", f"2026-06-{1 + i % 28:02d}T00:01:00Z", "human",
             None))))
    return out


@pytest.mark.parametrize("models", [None, ["opus"]], ids=["unfiltered", "model"])
@pytest.mark.parametrize("scope", [None, _1093_A, "unattributed"],
                         ids=["unscoped", "real", "unattributed"])
@pytest.mark.parametrize("fa", [True, False], ids=["fts", "like"])
def test_1093_title_statements_seek_per_session(tmp_path, fa, scope, models):
    """Machine-safety failure detected: a Titles count or page statement whose
    plan walks the message table (a SCAN, or the old window's
    ``session_id>?`` range over the whole index) instead of seeking each
    matched session's rows, on unscoped, real-account and unattributed
    connections, with and without a model-family filter, with no
    ``sqlite_stat1`` to steer the planner."""
    if fa:
        require_fts5()
    conn = _1093_store(tmp_path, _1093_plan_sessions())
    if scope is not None:
        cc.scope_conversations_db_to_account(conn, scope)
    try:
        for schema in ("main", "temp"):
            assert conn.execute(
                f"SELECT COUNT(*) FROM {schema}.sqlite_master "
                "WHERE name='sqlite_stat1'").fetchone()[0] == 0, schema
        statements = []
        conn.set_trace_callback(statements.append)
        res = cq.search_conversations(conn, "plan", kind="title",
                                      fts_available=fa, models=models)
        conn.set_trace_callback(None)
        assert res["mode"] == ("fts" if fa else "like")
        assert res["hits"], "the plan check needs a non-empty search"
        # FTS5 traces its own internal statements as "-- ..." comments.
        title = [s for s in statements
                 if not s.lstrip().startswith("--")
                 and ("conversation_title_fts" in s
                      or ("conversation_ai_titles" in s
                          and "conversation_messages" in s))]
        counts = [s for s in title if "COUNT(*)" in s]
        pages = [s for s in title if "LIMIT" in s and "snippet(" not in s
                 and "COUNT(*)" not in s]
        assert len(counts) == 1 and len(pages) == 1, title
        for sql in counts + pages:
            bad, plan = _1093_k1_violations(conn, sql, scope is not None)
            assert not bad, (bad, sql, plan)
    finally:
        conn.close()


def _seed_1093_like_race(c):
    for i in range(3):
        sid = f"r{i}"
        _msg(c, session_id=sid, uuid=f"{sid}-h", source_path=f"{sid}.jsonl",
             byte_offset=0, timestamp_utc=f"2026-06-0{i + 1}T00:00:00Z",
             entry_type="human", text="hi", cwd="/home/u/proj")
        _title(c, sid, f"plan race {i}")


def test_1093_title_like_count_and_page_share_a_snapshot(tmp_path):
    """Accuracy failure detected: a LIKE Titles search whose total and page
    describe two states of the store (a session deleted by a concurrent
    writer between the count and the page is counted but missing from the
    page, so the total lies)."""
    path = tmp_path / "conversations.db"
    w = sqlite3.connect(path)
    db._apply_cache_schema(w)
    w.execute("PRAGMA journal_mode=WAL")
    _seed_1093_like_race(w)
    w.commit()
    state = {"count": False, "fired": False}

    def race(sql):
        if state["fired"]:
            return
        if state["count"]:
            state["fired"] = True
            w.execute("DELETE FROM conversation_messages WHERE session_id='r1'")
            w.commit()
        elif "COUNT(*)" in sql and "conversation_ai_titles" in sql:
            state["count"] = True

    reader = sqlite3.connect(path)
    reader.set_trace_callback(race)
    try:
        res = cq.search_conversations(reader, "plan", kind="title",
                                      fts_available=False)
    finally:
        reader.set_trace_callback(None)
    assert state["fired"], "the race hook never ran between count and page"
    assert w.execute("SELECT COUNT(*) FROM conversation_messages "
                     "WHERE session_id='r1'").fetchone()[0] == 0
    assert res["total"] == len(res["hits"]) == 3
    assert {h["session_id"] for h in res["hits"]} == {"r0", "r1", "r2"}
    reader.close()
    w.close()


def test_1093_title_like_leaves_caller_transaction_open(tmp_path):
    """Transaction failure detected: the LIKE Titles search committing or
    rolling back a read transaction its caller holds (the caller's snapshot
    ends early, or its final COMMIT fails)."""
    path = tmp_path / "conversations.db"
    w = sqlite3.connect(path)
    db._apply_cache_schema(w)
    w.execute("PRAGMA journal_mode=WAL")
    _seed_1093_like_race(w)
    w.commit()
    w.close()
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    res = cq.search_conversations(reader, "plan", kind="title",
                                  fts_available=False)
    assert res["total"] == len(res["hits"]) == 3
    assert reader.in_transaction
    reader.execute("COMMIT")
    assert not reader.in_transaction
    reader.close()
