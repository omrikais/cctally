"""File-path search axis: _derive_file_touches, _fill_file_touches lifecycle,
_consume_file_touches backfill, and kind=files cross-session search
(#217 S2 / I-3; subtasks I-3a..I-3d).

Kernel-level tests (in-memory cache.db seeded directly) plus an HTTP-route test
that boots a real ``DashboardHTTPHandler`` to prove the kind-validation split
(``/find?kind=files`` -> 400, never 500).

Load-bearing findings exercised:
  * I-3a — ``_derive_file_touches`` extracts WRITE-class tools only (Edit /
    MultiEdit / Write / NotebookEdit); Read / Bash excluded.
  * P1-3 — ``_fill_file_touches`` scopes by the PHYSICAL key
    ``(source_path, byte_offset)``, not ``uuid``: two replay rows sharing
    ``(session_id, uuid)`` but different physical keys do NOT cross-contaminate.
  * decoupled-from-rowcount — a no-op message reinsert (rowcount 0) still yields
    touches (we derive from conversation_messages, never the insert rowcount).
  * P1-4 — lifecycle cleanup: a clear/rebuild drops touches; a per-``source_path``
    reingest deletes-then-refills with no stale/duplicate anchors after the rowid
    bump.
  * ``_consume_file_touches`` backfills from blocks_json (resumable, scope=None).
  * I-3d / P3-10 — ``kind=files`` returns DISTINCT (session, file_path) anchored
    to the most-recent touch with ``match_kinds:["file"]``; a prefix-looking query
    is index-assisted and a substring query is a documented scan, both correct.
  * ``kind=files`` respects the browse filters (filtered-search composes).
  * P1-1 — ``/find?kind=files`` -> 400 (find-kind set excludes ``files``).
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
import _lib_conversation as conv  # noqa: E402
import _lib_conversation_query as cq  # noqa: E402

from conftest import load_script, redirect_paths  # noqa: E402

_MODEL = "claude-opus-4-8"


def _conn():
    c = sqlite3.connect(":memory:")
    db._apply_cache_schema(c)
    return c


def _tu(name, file_path):
    """A normalized tool_use block as it lives in blocks_json (``kind`` key,
    bounded ``input``)."""
    return {"kind": "tool_use", "name": name, "input": {"file_path": file_path}}


def _insert_msg(c, *, session_id, uuid, source_path, byte_offset,
                blocks, timestamp_utc="2026-06-01T00:00:00Z",
                entry_type="assistant", cwd=None, msg_id=None, req_id=None):
    """Insert one conversation_messages row with a JSON blocks payload; return
    its rowid (conversation_messages.id)."""
    cur = c.execute(
        "INSERT INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain,source_tool_use_id,stop_reason,attribution_skill,"
        " attribution_plugin,search_tool,search_thinking) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, uuid, None, source_path, byte_offset, timestamp_utc,
         entry_type, "", json.dumps(blocks), _MODEL, msg_id, req_id, cwd,
         "main", 0, None, None, None, None, "", ""))
    return cur.lastrowid


def _touches(c):
    return c.execute(
        "SELECT message_id, session_id, uuid, file_path, tool "
        "FROM conversation_file_touches ORDER BY message_id, file_path, tool"
    ).fetchall()


# --- I-3a: _derive_file_touches pure helper -------------------------------

def test_derive_file_touches_write_class_only():
    """Plan's literal shape: raw API blocks use ``type``; the helper must extract
    write-class tools only (Read / Bash excluded)."""
    blocks = [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "bin/cctally"}},
        {"type": "tool_use", "name": "Write", "input": {"file_path": "docs/x.md"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "bin/cctally"}},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls bin/cctally"}},
    ]
    assert sorted(conv._derive_file_touches(blocks)) == [
        ("bin/cctally", "Edit"), ("docs/x.md", "Write")]


def test_derive_file_touches_stored_kind_shape():
    """The STORED blocks_json form uses ``kind`` (not ``type``) — the helper must
    also derive touches from the normalized shape, since _fill_file_touches reads
    blocks_json."""
    blocks = [
        _tu("Edit", "bin/cctally"),
        _tu("MultiEdit", "bin/_cctally_db.py"),
        _tu("NotebookEdit", "nb.ipynb"),
        {"kind": "tool_use", "name": "Read", "input": {"file_path": "bin/cctally"}},
        {"kind": "thinking", "text": "ponder bin/cctally"},
        {"kind": "tool_result", "text": "edited bin/cctally"},
    ]
    assert sorted(conv._derive_file_touches(blocks)) == [
        ("bin/_cctally_db.py", "MultiEdit"),
        ("bin/cctally", "Edit"),
        ("nb.ipynb", "NotebookEdit"),
    ]


def test_derive_file_touches_skips_missing_or_nonstring_path():
    blocks = [
        {"kind": "tool_use", "name": "Edit", "input": {}},          # no file_path
        {"kind": "tool_use", "name": "Edit", "input": {"file_path": ""}},   # empty
        {"kind": "tool_use", "name": "Write", "input": {"file_path": 5}},   # non-str
        {"kind": "tool_use", "name": "Write"},                      # no input
        "not a dict",
        None,
    ]
    assert conv._derive_file_touches(blocks) == []
    assert conv._derive_file_touches(None) == []
    assert conv._derive_file_touches([]) == []


# --- I-3c: _fill_file_touches scoping (P1-3) + decoupled-from-rowcount -----

def test_fill_scoped_by_physical_key_no_uuid_crosstalk():
    """P1-3: two replay rows sharing (session_id, uuid) but different
    (source_path, byte_offset) must NOT cross-contaminate touches when the fill is
    scoped to one physical row only."""
    c = _conn()
    # Row A and Row B share (session_id='s1', uuid='u1') — a legitimate resume
    # replay across two JSONL files — but touch DIFFERENT files.
    id_a = _insert_msg(c, session_id="s1", uuid="u1", source_path="a.jsonl",
                       byte_offset=0, blocks=[_tu("Edit", "bin/a")])
    id_b = _insert_msg(c, session_id="s1", uuid="u1", source_path="b.jsonl",
                       byte_offset=0, blocks=[_tu("Edit", "bin/b")])
    assert id_a != id_b
    cc._fill_file_touches(c, scope=[("a.jsonl", 0)])
    rows = _touches(c)
    # Only row A's touch was filled — NOT row B's, despite the shared uuid.
    assert rows == [(id_a, "s1", "u1", "bin/a", "Edit")]
    assert all(r[0] == id_a for r in rows)


def test_fill_decoupled_from_message_rowcount():
    """A row already present in conversation_messages (a no-op INSERT OR IGNORE,
    rowcount 0, in the live path) still yields touches: _fill_file_touches derives
    from the message table by physical key, never from the insert's
    rowcount/lastrowid.

    Non-vacuity guard: after inserting the message we insert an UNRELATED row on
    the same connection, so ``last_insert_rowid()`` and the current statement's
    rowcount no longer point at the message — a fill coupled to either would MISS
    the message; a physical-key table read still finds it."""
    c = _conn()
    mid = _insert_msg(c, session_id="s1", uuid="u1", source_path="a.jsonl",
                      byte_offset=0, blocks=[_tu("Write", "docs/x.md")])
    # An unrelated INSERT so last_insert_rowid()/rowcount no longer reference the
    # message row (a non-touching message in a different session).
    _insert_msg(c, session_id="s2", uuid="u2", source_path="b.jsonl",
                byte_offset=0, blocks=[{"kind": "text", "text": "hi"}])
    # No touch rows yet (messages inserted directly, bypassing fill).
    assert _touches(c) == []
    cc._fill_file_touches(c, scope=[("a.jsonl", 0)])
    rows = _touches(c)
    assert rows == [(mid, "s1", "u1", "docs/x.md", "Write")]


def test_fill_idempotent_on_rerun():
    c = _conn()
    mid = _insert_msg(c, session_id="s1", uuid="u1", source_path="a.jsonl",
                      byte_offset=0, blocks=[_tu("Edit", "bin/a"),
                                             _tu("Write", "bin/b")])
    cc._fill_file_touches(c, scope=[("a.jsonl", 0)])
    cc._fill_file_touches(c, scope=[("a.jsonl", 0)])   # re-run -> INSERT OR IGNORE
    rows = _touches(c)
    assert rows == [(mid, "s1", "u1", "bin/a", "Edit"),
                    (mid, "s1", "u1", "bin/b", "Write")]


# --- I-3c: lifecycle cleanup (P1-4) ---------------------------------------

def test_rebuild_clears_file_touches():
    """P1-4: clear_conversation_messages (full rebuild/truncation) drops the
    derived touches too."""
    c = _conn()
    _insert_msg(c, session_id="s1", uuid="u1", source_path="a.jsonl",
                byte_offset=0, blocks=[_tu("Edit", "bin/a")])
    cc._fill_file_touches(c, scope=[("a.jsonl", 0)])
    assert _touches(c)
    db.clear_conversation_messages(c)
    assert c.execute(
        "SELECT count(*) FROM conversation_file_touches").fetchone()[0] == 0


def test_per_source_reingest_refills_without_dupes(tmp_path, monkeypatch):
    """P1-4: a per-source reingest DELETEs + re-inserts a file's
    conversation_messages rows (bumping autoincrement ids), so the file's touches
    must be deleted before the reingest and refilled after — leaving exactly the
    new anchors (the bumped ids), no stale old-id rows and no duplicates."""
    c = _conn()
    # Two files, both touching files; seed messages + their touches.
    id_a = _insert_msg(c, session_id="s1", uuid="ua", source_path="a.jsonl",
                       byte_offset=0, blocks=[_tu("Edit", "bin/a")])
    id_b = _insert_msg(c, session_id="s2", uuid="ub", source_path="b.jsonl",
                       byte_offset=0, blocks=[_tu("Edit", "bin/b")])
    cc._fill_file_touches(c, scope=[("a.jsonl", 0), ("b.jsonl", 0)])
    assert _touches(c) == [(id_a, "s1", "ua", "bin/a", "Edit"),
                           (id_b, "s2", "ub", "bin/b", "Edit")]

    # Make the reingest re-parse a.jsonl into one message row touching bin/a
    # (same logical content; the rowid will bump on reinsert).
    def _fake_parse(jp, path_str):
        from _lib_conversation import MessageRow
        return [cc._conv_row_tuple(
            MessageRow(
                byte_offset=0, session_id="s1", uuid="ua", parent_uuid=None,
                timestamp_utc="2026-06-01T00:00:00Z", entry_type="assistant",
                text="", blocks_json=json.dumps([_tu("Edit", "bin/a")]),
                model=_MODEL, msg_id=None, req_id=None, cwd=None,
                git_branch="main", is_sidechain=0, source_tool_use_id=None,
                stop_reason=None, attribution_skill=None,
                attribution_plugin=None, search_tool="", search_thinking=""),
            path_str)]

    monkeypatch.setattr(cc, "_iter_claude_jsonl_files",
                        lambda: [pathlib.Path("a.jsonl")])
    monkeypatch.setattr(cc, "_reingest_parse_file", _fake_parse)
    # Arm a reingest flag so the gen-guard runs the pass.
    db._set_cache_meta(c, "conversation_reingest_pending", "1")
    cc._resumable_reingest_conversation_messages(c)

    rows = _touches(c)
    # b.jsonl's touch is UNTOUCHED (not in the reingest walk). a.jsonl's touch was
    # deleted (old id_a gone) then refilled at the bumped id. No duplicates.
    paths = sorted(r[3] for r in rows)
    assert paths == ["bin/a", "bin/b"]
    a_rows = [r for r in rows if r[3] == "bin/a"]
    assert len(a_rows) == 1
    new_id_a = a_rows[0][0]
    assert new_id_a != id_a, "the reingest bumped the message id; the anchor must follow"
    # The new anchor id matches the surviving conversation_messages row.
    live = c.execute(
        "SELECT id FROM conversation_messages WHERE source_path='a.jsonl'"
    ).fetchone()[0]
    assert new_id_a == live


# --- I-3c: _consume_file_touches backfill (scope=None, resumable) ----------

def test_consume_file_touches_backfills_all_rows():
    c = _conn()
    id1 = _insert_msg(c, session_id="s1", uuid="u1", source_path="a.jsonl",
                      byte_offset=0, blocks=[_tu("Edit", "bin/a")])
    id2 = _insert_msg(c, session_id="s2", uuid="u2", source_path="b.jsonl",
                      byte_offset=0, blocks=[_tu("Write", "bin/b"),
                                             _tu("Read", "bin/c")])
    db._set_cache_meta(c, "conversation_reingest_file_touches_pending", "1")
    cc._consume_file_touches(c)
    rows = _touches(c)
    assert rows == [(id1, "s1", "u1", "bin/a", "Edit"),
                    (id2, "s2", "u2", "bin/b", "Write")]   # Read excluded
    # Flag cleared after a complete backfill.
    assert c.execute(
        "SELECT value FROM cache_meta "
        "WHERE key='conversation_reingest_file_touches_pending'").fetchone() is None


def test_consume_file_touches_noop_without_flag():
    c = _conn()
    _insert_msg(c, session_id="s1", uuid="u1", source_path="a.jsonl",
                byte_offset=0, blocks=[_tu("Edit", "bin/a")])
    cc._consume_file_touches(c)   # flag not armed -> no-op
    assert _touches(c) == []


# --- I-3d: kind=files cross-session search --------------------------------

def _seed_search_corpus(c):
    # sa: touches bin/cctally (Edit), in proj-a.
    ida = _insert_msg(c, session_id="sa", uuid="ua1", source_path="a.jsonl",
                      byte_offset=0, timestamp_utc="2026-06-01T00:00:00Z",
                      cwd="/home/u/proj-a", msg_id="m1", req_id="r1",
                      blocks=[_tu("Edit", "bin/cctally")])
    # sa later touches bin/cctally AGAIN (Write) — most-recent anchor for that path.
    ida2 = _insert_msg(c, session_id="sa", uuid="ua2", source_path="a.jsonl",
                       byte_offset=1, timestamp_utc="2026-06-01T01:00:00Z",
                       cwd="/home/u/proj-a", blocks=[_tu("Write", "bin/cctally")])
    # sb: touches bin/cctally-dashboard.py (Edit), in proj-b.
    idb = _insert_msg(c, session_id="sb", uuid="ub1", source_path="b.jsonl",
                      byte_offset=0, timestamp_utc="2026-06-02T00:00:00Z",
                      cwd="/home/u/proj-b", blocks=[_tu("Edit", "bin/cctally-dashboard.py")])
    # sc: touches docs/commands/dashboard.md (Write) — substring 'dashboard' but
    # NOT a 'bin/' prefix.
    idc = _insert_msg(c, session_id="sc", uuid="uc1", source_path="c.jsonl",
                      byte_offset=0, timestamp_utc="2026-06-03T00:00:00Z",
                      cwd="/home/u/proj-c", blocks=[_tu("Write", "docs/commands/dashboard.md")])
    db._set_cache_meta(c, "conversation_reingest_file_touches_pending", "1")
    cc._consume_file_touches(c)
    return {"ida": ida, "ida2": ida2, "idb": idb, "idc": idc}


def test_kind_files_returns_touching_sessions():
    c = _conn()
    ids = _seed_search_corpus(c)
    res = cq.search_conversations(c, "bin/cctally", kind="files")
    assert res["kind"] == "files"
    # DISTINCT (session, file_path): sa/bin/cctally and sb/bin/cctally-dashboard.py
    # both match the prefix 'bin/cctally'.
    got = {(h["session_id"], h["snippet"]) for h in res["hits"]}
    assert got == {("sa", "bin/cctally"), ("sb", "bin/cctally-dashboard.py")}
    for h in res["hits"]:
        assert h["match_kinds"] == ["file"]
        assert h["session_id"] and h["uuid"]
    # The sa/bin/cctally hit is anchored to the MOST-RECENT touch (the Write at
    # ua2), not the first (Edit at ua1).
    sa_hit = [h for h in res["hits"] if h["session_id"] == "sa"][0]
    assert sa_hit["uuid"] == "ua2"
    assert res["total"] == 2


def test_kind_files_total_excludes_orphan_anchor():
    """#219 S2.1: ``total`` must INNER-JOIN the same MAX(message_id) anchor the
    page joins, so a touch whose anchor message row is absent is excluded from
    BOTH the count and the page — never a 'lying count' (page-reachable < total).
    This is the file-axis analogue of the P2-9 guard in ``_search_title``.

    The lifecycle never leaves such an orphan in a quiescent cache (touches are
    deleted atomically with their message), so we inject one directly.

    Non-vacuity: with the pre-fix unjoined COUNT, ``total`` counts the orphan
    group (1) while the page INNER-JOINs it away (0 hits) — total != len(hits)."""
    c = _conn()
    _seed_search_corpus(c)
    # An orphan touch: MAX(message_id)=999999 has NO conversation_messages row.
    c.execute(
        "INSERT INTO conversation_file_touches"
        "(message_id, session_id, uuid, file_path, tool) VALUES(?,?,?,?,?)",
        (999999, "sorphan", "uorphan", "orphanonly/lying.txt", "Edit"))
    res = cq.search_conversations(c, "orphanonly/lying.txt", kind="files")
    assert res["hits"] == []
    assert res["total"] == len(res["hits"]) == 0, res["total"]


def _query_plan(conn, sql, params):
    """The flattened EXPLAIN QUERY PLAN detail lines for ``sql``."""
    return " | ".join(
        r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())


def test_kind_files_prefix_branch_uses_index_substring_scans():
    """Fix-1 (Important #1) / P3-10 — the prefix LIKE branch must be GENUINELY
    index-assisted via ``idx_file_touches_path`` (a SEARCH, not a SCAN), and the
    substring branch must remain a full SCAN (its leading wildcard can't use the
    index — expected and correct).

    The DEFAULT (case-insensitive) ``LIKE`` only rides a btree index when the
    index column is ``COLLATE NOCASE``; a BINARY-collated index leaves the prefix
    branch a full SCAN, so this asserts the NOCASE-index collation (the whole point
    of the fix). Critically the assertion holds WITH the ``ESCAPE '\\'`` clause the
    production query carries (a literal-prefix bound param keeps the LIKE
    optimization armed even after wildcard escaping) — so it proves the real query
    shape ``_search_files`` runs is index-assisted, not a hand-simplified one.

    Non-vacuity: under the pre-fix BINARY index this test FAILS (the prefix plan
    reads ``SCAN`` not ``SEARCH ... USING ... INDEX idx_file_touches_path``)."""
    c = _conn()
    _seed_search_corpus(c)
    # The exact WHERE shape _search_files runs: a single pre-built pattern bound
    # into ``file_path LIKE ? ESCAPE '\'``. Prefix pattern has a literal prefix
    # ('bin/cctally') before its trailing '%'; substring pattern leads with '%'.
    base = ("SELECT ft.session_id, ft.file_path FROM conversation_file_touches ft "
            "WHERE ft.file_path LIKE ? ESCAPE '\\' "
            "GROUP BY ft.session_id, ft.file_path")
    prefix_plan = _query_plan(c, base, ("bin/cctally%",))
    substring_plan = _query_plan(c, base, ("%dashboard%",))
    # Prefix branch: a SEARCH that rides idx_file_touches_path (NOT a SCAN). The
    # NOCASE index collation is what arms the default-LIKE optimization.
    assert "idx_file_touches_path" in prefix_plan, prefix_plan
    assert "SEARCH" in prefix_plan, prefix_plan
    assert "SCAN" not in prefix_plan, prefix_plan
    # Substring branch: a full SCAN (leading wildcard can't use the index). This is
    # the intentional, documented divergence — assert it stays a scan so a future
    # change can't silently claim index assistance for the substring case.
    assert "SEARCH" not in substring_plan, substring_plan


def test_kind_files_prefix_vs_substring():
    """P3-10: a prefix-looking query (no leading separator) is index-assisted via
    the ``COLLATE NOCASE`` path index (``file_path LIKE ? ESCAPE '\\'`` over a
    literal-prefix pattern); a leading-separator query falls to the substring scan.
    Both must return the correct rows."""
    c = _conn()
    _seed_search_corpus(c)
    # Prefix 'bin/cctally' (no leading separator) -> matches bin/cctally and
    # bin/cctally-dashboard.py via LIKE ?||'%', but NOT docs/commands/dashboard.md.
    pref = cq.search_conversations(c, "bin/cctally", kind="files")
    assert {h["snippet"] for h in pref["hits"]} == {
        "bin/cctally", "bin/cctally-dashboard.py"}
    # A bare 'dashboard' query takes the PREFIX branch (no leading separator), so it
    # only matches paths STARTING with 'dashboard' — there are none. This proves the
    # prefix branch is genuinely a prefix probe, not a substring scan.
    pref_dash = cq.search_conversations(c, "dashboard", kind="files")
    assert pref_dash["hits"] == [] and pref_dash["total"] == 0
    # A leading-separator query '/dashboard' forces the SUBSTRING scan
    # (LIKE '%'||?||'%'), matching the mid-path '/dashboard' in
    # docs/commands/dashboard.md (and NOT bin/cctally-dashboard.py, whose
    # 'dashboard' is preceded by '-', not '/').
    sub = cq.search_conversations(c, "/dashboard", kind="files")
    assert {h["snippet"] for h in sub["hits"]} == {"docs/commands/dashboard.md"}
    # A leading-separator substring shared by two paths returns both, proving the
    # substring branch is not a prefix probe ('/cctally' is inside both bin paths).
    sub2 = cq.search_conversations(c, "/cctally", kind="files")
    assert {h["snippet"] for h in sub2["hits"]} == {
        "bin/cctally", "bin/cctally-dashboard.py"}


def test_kind_files_case_insensitive():
    c = _conn()
    _seed_search_corpus(c)
    res = cq.search_conversations(c, "BIN/CCTALLY", kind="files")
    assert {h["snippet"] for h in res["hits"]} == {
        "bin/cctally", "bin/cctally-dashboard.py"}


def test_kind_files_empty_query_is_empty():
    c = _conn()
    _seed_search_corpus(c)
    res = cq.search_conversations(c, "   ", kind="files")
    assert res["hits"] == [] and res["total"] == 0 and res["kind"] == "files"


def test_kind_files_respects_filters():
    c = _conn()
    _seed_search_corpus(c)
    cc._recompute_conversation_sessions(c)   # make the rollup authoritative
    base = cq.search_conversations(c, "bin", kind="files")["total"]
    filt = cq.search_conversations(c, "bin", kind="files", projects=["proj-a"])
    assert {h["session_id"] for h in filt["hits"]} == {"sa"}
    assert filt["total"] <= base
    assert filt["total"] == 1
    assert "filter_degraded" not in filt


def test_files_in_search_kinds_not_find_kinds():
    """P1-1 (kernel side): the cross-session search-kind set carries ``files``;
    the find-kind set does NOT."""
    assert "files" in cq._SEARCH_KINDS
    assert "files" not in cq._FIND_KINDS


def test_find_in_conversation_rejects_files_kind():
    c = _conn()
    _seed_search_corpus(c)
    with pytest.raises(ValueError):
        cq.find_in_conversation(c, "sa", "bin/cctally", kind="files")


# --- HTTP route: the kind-validation split (P1-1) -------------------------

def _seed_http_cache(ns):
    cache = ns["open_cache_db"]()
    cache.execute(
        "INSERT INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain) VALUES('s1','h1',NULL,'a.jsonl',0,"
        "'2026-06-01T00:00:00Z','assistant','',?,NULL,NULL,NULL,"
        "'/home/u/proj','main',0)",
        (json.dumps([_tu("Edit", "bin/cctally")]),))
    import _cctally_cache as _cc
    _cc._fill_file_touches(cache, scope=[("a.jsonl", 0)])
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


def test_find_rejects_files_kind_400(tmp_path, monkeypatch):
    """P1-1: the shared /find route 400s ``files`` (find-kind set excludes it); it
    MUST NOT reach the kernel and 500."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, body = _get(port, "/api/conversation/s1/find?q=bin&kind=files")
        assert status == 400, (status, body)
        assert "error" in json.loads(body)
    finally:
        srv.shutdown()


def test_search_accepts_files_kind_200(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, body = _get(port, "/api/conversation/search?q=bin/cctally&kind=files")
        assert status == 200, (status, body)
        out = json.loads(body)
        assert out["kind"] == "files"
        assert out["hits"] and out["hits"][0]["session_id"] == "s1"
        assert out["hits"][0]["match_kinds"] == ["file"]
    finally:
        srv.shutdown()
