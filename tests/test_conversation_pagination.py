"""U4 — tail-page / reverse pagination kernel + handler tests (#217 S2).

Covers the additive cursor contract on ``get_conversation``:

  - ``tail=1`` returns the last page (open-at-bottom), ``has_more=False``.
  - ``before=X`` returns the ``limit`` items immediately before X.
  - a short ``before`` page truncated at the head still reports ``has_more``
    (P2-8 — newer items remain to page DOWN to).
  - page-up-then-down reconstructs the full ordered item list (structural —
    no gaps / dupes, not hardcoded page contents).
  - a stale ``before`` cursor → empty page (mirrors the ``after`` M1 contract,
    never a tail re-serve).
  - no-cursor output stays byte-stable except the two additive ``page`` keys.
  - the handler rejects mutually-exclusive ``after``/``before``/``tail`` → 400.

Kernel tests use the in-memory direct-seed pattern from
``tests/test_conversation_query.py`` (``sqlite3`` + ``_apply_cache_schema`` +
direct ``conversation_messages`` inserts). The handler test boots a real
``DashboardHTTPHandler`` via ``load_script`` + ``redirect_paths`` (the loader
that pins the cache.db under a fake HOME — NOT ``setenv(HOME)``-only, which
would read the developer's prod DB).
"""
import json as _json
import pathlib
import socketserver
import sqlite3
import sys
import threading
from http.client import HTTPConnection

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db
import _lib_conversation_query as cq

from conftest import load_script, redirect_paths

_SID = "s1"


def _conn():
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)
    return c


def _seed_turns(c, n=12):
    """Seed one session of ``n`` distinct ordered human turns. Each row carries
    a unique uuid + monotonically increasing timestamp, so the assembler emits
    exactly ``n`` items in (timestamp_utc, id) order — a clean ordered list to
    paginate. Mirrors ``test_get_conversation_cursor_pagination``."""
    for i in range(n):
        c.execute(
            "INSERT INTO conversation_messages "
            "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
            " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
            " is_sidechain,source_tool_use_id,stop_reason,attribution_skill,"
            " attribution_plugin,search_tool,search_thinking) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_SID, f"u{i:02d}", None, "a.jsonl", i,
             f"2026-06-01T00:{i:02d}:00Z", "human", f"message number {i}",
             "[]", None, None, None, "/home/u/proj", "main",
             0, None, None, None, None, "", ""),
        )
    return c


@pytest.fixture()
def conn():
    return _seed_turns(_conn())


@pytest.fixture()
def sid():
    return _SID


@pytest.fixture()
def qmod():
    return cq


# ---------------------------------------------------------------------------
# Kernel cursor logic
# ---------------------------------------------------------------------------

def test_tail_returns_last_page(qmod, conn, sid):
    full = qmod.get_conversation(conn, sid, limit=1000)["items"]
    page = qmod.get_conversation(conn, sid, tail=True, limit=5)
    assert [it["anchor"]["id"] for it in page["items"]] == \
        [it["anchor"]["id"] for it in full[-5:]]
    assert page["page"]["has_more"] is False
    assert page["page"]["next_after"] is None
    assert page["page"]["has_prev"] is True
    assert page["page"]["prev_before"] == full[-5]["anchor"]["id"]


def test_before_returns_preceding_page(qmod, conn, sid):
    full = qmod.get_conversation(conn, sid, limit=1000)["items"]
    x = full[8]["anchor"]["id"]
    page = qmod.get_conversation(conn, sid, before=x, limit=3)
    assert [it["anchor"]["id"] for it in page["items"]] == \
        [it["anchor"]["id"] for it in full[5:8]]
    assert page["page"]["has_more"] is True          # items at/after x remain
    assert page["page"]["next_after"] == full[7]["anchor"]["id"]
    assert page["page"]["has_prev"] is True
    assert page["page"]["prev_before"] == full[5]["anchor"]["id"]


def test_before_short_head_page_still_has_more(qmod, conn, sid):
    # P2-8: a before-page truncated at the head must still report has_more.
    full = qmod.get_conversation(conn, sid, limit=1000)["items"]
    x = full[2]["anchor"]["id"]
    page = qmod.get_conversation(conn, sid, before=x, limit=10)
    assert [it["anchor"]["id"] for it in page["items"]] == \
        [it["anchor"]["id"] for it in full[0:2]]
    assert page["page"]["has_prev"] is False
    assert page["page"]["prev_before"] is None
    assert page["page"]["has_more"] is True
    assert page["page"]["next_after"] == full[1]["anchor"]["id"]


def test_page_up_then_down_reconstructs_full_list(qmod, conn, sid):
    # Structural: page upward from tail, then downward from head; both
    # reconstruct the full ordered list (no gaps, no dupes).
    full = [it["anchor"]["id"]
            for it in qmod.get_conversation(conn, sid, limit=1000)["items"]]

    # upward: open at bottom, then walk `before` until the head.
    up, cur, tail = [], None, True
    while True:
        pg = (qmod.get_conversation(conn, sid, tail=True, limit=4) if tail
              else qmod.get_conversation(conn, sid, before=cur, limit=4))
        tail = False
        ids = [it["anchor"]["id"] for it in pg["items"]]
        up = ids + up
        if not pg["page"]["has_prev"]:
            break
        cur = pg["page"]["prev_before"]
    assert up == full

    # downward: open at top (no cursor), then walk `after` until the tail.
    down, cur, first = [], None, True
    while True:
        pg = (qmod.get_conversation(conn, sid, limit=4) if first
              else qmod.get_conversation(conn, sid, after=cur, limit=4))
        first = False
        ids = [it["anchor"]["id"] for it in pg["items"]]
        down = down + ids
        if not pg["page"]["has_more"]:
            break
        cur = pg["page"]["next_after"]
    assert down == full


def test_stale_before_cursor_empty_page(qmod, conn, sid):
    page = qmod.get_conversation(conn, sid, before=-999999, limit=5)
    assert page["items"] == []
    assert page["page"]["has_prev"] is False
    assert page["page"]["has_more"] is False
    assert page["page"]["prev_before"] is None
    assert page["page"]["next_after"] is None
    # session metadata still populated (not None) — only the page is empty.
    assert page["session_id"] == sid


def test_stale_after_cursor_keeps_new_keys(qmod, conn, sid):
    # The existing `after` M1 stale-cursor contract is preserved AND its
    # empty-page envelope gains the two additive keys.
    page = qmod.get_conversation(conn, sid, after="9999999", limit=5)
    assert page["items"] == []
    assert page["page"]["has_more"] is False
    assert page["page"]["next_after"] is None
    assert page["page"]["has_prev"] is False
    assert page["page"]["prev_before"] is None


def test_no_cursor_byte_stable(qmod, conn, sid):
    page = qmod.get_conversation(conn, sid, limit=5)
    # additive keys present and inert for the head page.
    assert "prev_before" in page["page"] and "has_prev" in page["page"]
    assert page["page"]["has_prev"] is False
    assert page["page"]["prev_before"] is None
    # next_after/has_more identical to prior behavior (start+limit<N == end<N).
    full = qmod.get_conversation(conn, sid, limit=1000)["items"]
    assert page["page"]["has_more"] is True
    assert page["page"]["next_after"] == full[4]["anchor"]["id"]
    assert [it["anchor"]["id"] for it in page["items"]] == \
        [it["anchor"]["id"] for it in full[0:5]]


def test_mutually_exclusive_kernel_raises(qmod, conn, sid):
    # The kernel raises ValueError when more than one cursor mode is supplied
    # (the handler maps this to 400).
    with pytest.raises(ValueError):
        qmod.get_conversation(conn, sid, after="1", before="2")
    with pytest.raises(ValueError):
        qmod.get_conversation(conn, sid, after="1", tail=True)
    with pytest.raises(ValueError):
        qmod.get_conversation(conn, sid, before="1", tail=True)


# ---------------------------------------------------------------------------
# Handler-level mutual-exclusion → HTTP 400
# ---------------------------------------------------------------------------

def _seed_handler_cache(ns):
    cache = ns["open_cache_db"]()
    _seed_turns(cache)
    import _cctally_cache as _cc
    _cc._recompute_conversation_sessions(cache)
    cache.commit()
    cache.close()


def _boot(ns, tmp_path, monkeypatch):
    import datetime as _dt
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    _seed_handler_cache(ns)

    HandlerCls = ns["DashboardHTTPHandler"]
    SnapshotRef = ns["_SnapshotRef"]
    SSEHub = ns["SSEHub"]
    DataSnapshot = ns["DataSnapshot"]
    snap = DataSnapshot(
        current_week=None, forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=_dt.datetime(2026, 6, 3, 12, 0, tzinfo=_dt.timezone.utc),
        percent_milestones=[], weekly_history=[],
        weekly_periods=[], monthly_periods=[],
        blocks_panel=[], daily_panel=[],
    )
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
    c = HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    status = r.status
    c.close()
    return status, body


def test_handler_rejects_mutually_exclusive_cursors(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        # single-mode requests succeed.
        status, _ = _get(port, "/api/conversation/s1?tail=1&limit=2")
        assert status == 200
        # any combination of two cursor modes → 400.
        for path in (
            "/api/conversation/s1?after=1&before=2",
            "/api/conversation/s1?after=1&tail=1",
            "/api/conversation/s1?before=1&tail=1",
        ):
            status, _ = _get(port, path)
            assert status == 400, (path, status)
    finally:
        srv.shutdown()
