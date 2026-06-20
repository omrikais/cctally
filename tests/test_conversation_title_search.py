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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db   # noqa: E402
import _cctally_cache as cc  # noqa: E402
import _lib_conversation_query as cq  # noqa: E402

from conftest import load_script, redirect_paths  # noqa: E402

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
    if fa and not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
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
    if fa and not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
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


def test_kind_title_missing_vtable_degrades_to_like():
    """Resilience (code-review P3): ``fts_available`` is derived from the
    ``fts5_unavailable`` flag (NOT a per-table probe), so on an otherwise
    FTS5-capable build a missing/corrupt ``conversation_title_fts`` vtable would
    raise inside ``_search_title`` — the ``kind=title`` branch must catch that
    ``sqlite3.OperationalError`` and degrade to the title LIKE scan, mirroring the
    message-FTS path's resilience, instead of bubbling a 500.

    The LIKE degradation still returns the correct session-level title hit (the
    title content lives in ``conversation_ai_titles``, not the dropped vtable)."""
    c = _conn()
    if not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
    _seed_titles(c)
    # Simulate a missing/corrupt title vtable on an FTS5-capable build: drop the
    # sync triggers (so the DROP doesn't strand a live trigger) then the vtable.
    db._drop_conversation_title_fts_triggers(c)
    c.execute("DROP TABLE IF EXISTS conversation_title_fts")
    # fts_available=True mirrors the live derivation (fts5_unavailable is NOT set,
    # so the kernel believes FTS is usable) — the per-table miss is what we test.
    res = cq.search_conversations(c, "refactor", kind="title", fts_available=True)
    assert res["kind"] == "title"
    assert res["mode"] == "like"                   # degraded, not a 500
    assert {h["session_id"] for h in res["hits"]} == {"s1"}
    assert res["total"] == 1


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
    if fa and not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
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
    if fa and not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
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
    if fa and not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
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
    if fa and not db._fts5_available(c):
        pytest.skip("sqlite build lacks FTS5")
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
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _get(port, path):
    from http.client import HTTPConnection
    c = HTTPConnection("127.0.0.1", port, timeout=5)
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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()
