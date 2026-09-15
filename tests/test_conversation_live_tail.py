import json
import os
import sqlite3
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db
import _cctally_cache as cc
import _lib_conversation_query as cq

_MODEL = "claude-opus-4-8"  # real id from CLAUDE_MODEL_PRICING so cost > 0


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
    # #217 S1 / U7a: search_aux was dropped from the live schema by migration 016.
    c.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,is_sidechain,"
        " source_tool_use_id,stop_reason,attribution_skill,attribution_plugin,"
        " search_tool,search_thinking)"
        " VALUES(:session_id,:uuid,:parent_uuid,:source_path,:byte_offset,"
        ":timestamp_utc,:entry_type,:text,:blocks_json,:model,:msg_id,:req_id,"
        ":cwd,:git_branch,:is_sidechain,:source_tool_use_id,:stop_reason,"
        ":attribution_skill,:attribution_plugin,"
        ":search_tool,:search_thinking)", row)


def test_session_source_paths_returns_distinct_files():
    c = _conn()
    _msg(c, session_id="s1", uuid="h1", source_path="/p/a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human", text="hi")
    _msg(c, session_id="s1", uuid="a1", source_path="/p/a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant", text="x",
         model=_MODEL, msg_id="m1", req_id="r1")
    _msg(c, session_id="s1", uuid="a2", source_path="/p/b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:01:00Z", entry_type="assistant", text="y",
         model=_MODEL, msg_id="m2", req_id="r2")
    _msg(c, session_id="s2", uuid="o1", source_path="/p/c.jsonl", byte_offset=0,
         timestamp_utc="2026-06-02T00:00:00Z", entry_type="human", text="z")
    got = sorted(cq.session_source_paths(c, "s1"))
    assert got == ["/p/a.jsonl", "/p/b.jsonl"]


def test_session_source_paths_unknown_session_is_empty():
    c = _conn()
    assert cq.session_source_paths(c, "nope") == []


from conftest import load_script, redirect_paths_without_conversation_retention


def _asst_line(uuid, msg_id, req_id, text, *, sid="s1",
               ts="2026-06-01T00:00:00Z", model="claude-opus-4-8", out_tokens=5):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": sid,
        "requestId": req_id, "timestamp": ts,
        "message": {"role": "assistant", "id": msg_id, "model": model,
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 10, "output_tokens": out_tokens,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
    }) + "\n"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths_without_conversation_retention(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    conn = ns["open_conversations_db"]()
    yield ns, conn, projects
    try:
        conn.close()
    except Exception:
        pass


def _count(conn, path):
    return conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE source_path=?",
        (str(path),)).fetchone()[0]


def test_only_paths_ingests_only_the_named_file(isolated):
    ns, conn, projects = isolated
    sync_cache = ns["sync_claude_conversations"]
    a = projects / "a.jsonl"
    b = projects / "b.jsonl"
    a.write_text(_asst_line("a1", "m1", "r1", "answer A"))
    b.write_text(_asst_line("b1", "m2", "r2", "answer B", sid="s2"))
    stats = sync_cache(conn, only_paths={str(a)})
    assert stats.targeted_clean is True
    assert _count(conn, a) == 1     # A ingested
    assert _count(conn, b) == 0     # B untouched (no global walk)


def test_only_paths_withholds_walk_complete_marker(isolated):
    ns, conn, projects = isolated
    sync_cache = ns["sync_claude_conversations"]
    a = projects / "a.jsonl"
    a.write_text(_asst_line("a1", "m1", "r1", "hi"))
    sync_cache(conn, only_paths={str(a)})
    marker = conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'"
    ).fetchone()
    assert marker is None           # partial walk cannot vouch for the tree


def test_only_paths_does_not_orphan_other_tracked_files(isolated):
    ns, conn, projects = isolated
    sync_cache = ns["sync_claude_conversations"]
    a = projects / "a.jsonl"
    b = projects / "b.jsonl"
    a.write_text(_asst_line("a1", "m1", "r1", "A"))
    b.write_text(_asst_line("b1", "m2", "r2", "B", sid="s2"))
    sync_cache(conn, rebuild=True)             # full: both tracked
    assert _count(conn, b) == 1
    b.unlink()                                  # B gone from disk
    sync_cache(conn, only_paths={str(a)})       # targeted on A only
    assert _count(conn, b) == 1                 # B NOT pruned (no orphan scan)


def test_only_paths_declines_on_shrink(isolated):
    ns, conn, projects = isolated
    sync_cache = ns["sync_claude_conversations"]
    a = projects / "a.jsonl"
    a.write_text(_asst_line("a1", "m1", "r1", "A") + _asst_line("a2", "m2", "r2", "AA"))
    sync_cache(conn, rebuild=True)
    a.write_text(_asst_line("a1", "m1", "r1", "A"))   # shrank
    stats = sync_cache(conn, only_paths={str(a)})
    assert stats.targeted_clean is False
    assert stats.deferred_reason == "truncation"


def test_only_paths_declines_on_pending_global_flag(isolated):
    ns, conn, projects = isolated
    sync_cache = ns["sync_claude_conversations"]
    a = projects / "a.jsonl"
    a.write_text(_asst_line("a1", "m1", "r1", "A"))
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) "
                 "VALUES('conversation_reingest_pending','1')")
    conn.commit()
    stats = sync_cache(conn, only_paths={str(a)})
    assert stats.targeted_clean is False
    assert stats.deferred_reason == "pending_global_flags"
    # flag NOT consumed (left for the backstop full sync)
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_reingest_pending'"
    ).fetchone() is not None


@pytest.mark.skipif(os.getuid() == 0, reason="root bypasses chmod-000 read perms")
def test_only_paths_per_file_failure_is_not_targeted_clean(isolated):
    # A path that survives the is_file() filter but fails to OPEN (here:
    # chmod-000 so the read raises PermissionError) increments files_failed,
    # so the targeted ingest is NOT targeted_clean — the watch loop must NOT
    # advance `seen` for it.
    ns, conn, projects = isolated
    sync_cache = ns["sync_claude_conversations"]
    a = projects / "a.jsonl"
    a.write_text(_asst_line("a1", "m1", "r1", "A"))
    os.chmod(a, 0o000)
    try:
        stats = sync_cache(conn, only_paths={str(a)})
    finally:
        os.chmod(a, 0o644)  # restore so the fixture teardown can clean up
    assert stats.files_failed >= 1
    assert stats.targeted_clean is False


def test_only_paths_with_rebuild_raises_value_error(isolated):
    ns, conn, projects = isolated
    sync_cache = ns["sync_claude_conversations"]
    a = projects / "a.jsonl"
    a.write_text(_asst_line("a1", "m1", "r1", "A"))
    with pytest.raises(ValueError):
        sync_cache(conn, only_paths={str(a)}, rebuild=True)


_FINGERPRINT_TABLES = (
    "conversation_messages",
    "conversation_ai_titles",
    "conversation_sessions",
    "conversation_source_files",
    "conversation_file_touches",
)


def _store_fingerprint(conn):
    """Every row of every table the rejected call could reach, plus cache_meta.

    Asserting one surviving message row would pass while the rebuild branch
    still committed its pending marker and emptied the titles, the rollup and
    the source-file cursors, so the fingerprint covers all five derived tables
    and the whole marker set. `key=repr` because a column mixing None with a
    string is not orderable.
    """
    fingerprint = {
        table: sorted(
            (tuple(row) for row in conn.execute(f"SELECT * FROM {table}")),
            key=repr,
        )
        for table in _FINGERPRINT_TABLES
    }
    fingerprint["cache_meta"] = sorted(
        (tuple(row) for row in conn.execute("SELECT key, value FROM cache_meta")),
        key=repr,
    )
    return fingerprint


def _conversations_lock_mtime(core):
    """The lock file's mtime, or None when the file does not exist."""
    try:
        return core.CONVERSATIONS_LOCK_PATH.stat().st_mtime_ns
    except OSError:
        return None


def test_rebuild_with_only_paths_mutates_nothing(isolated, monkeypatch):
    """#779: the incompatible-argument rejection runs before every side effect.

    The check used to sit after the pending-marker commit, the maintenance
    preparation and the four destructive DELETEs, so the call that raised had
    already emptied the store it was refusing to touch.
    """
    ns, conn, projects = isolated
    import _cctally_core
    import _cctally_cache as cache
    a = projects / "a.jsonl"
    a.write_text(_asst_line("a1", "m1", "r1", "A"))
    ns["sync_claude_conversations"](conn)
    before = _store_fingerprint(conn)
    assert before["conversation_messages"], "the guard needs real rows to protect"
    _cctally_core.CONVERSATIONS_LOCK_PATH.unlink(missing_ok=True)
    lock_mtime = _conversations_lock_mtime(_cctally_core)
    calls = []
    monkeypatch.setattr(
        cache, "_report_conversation_progress",
        lambda *a, **k: calls.append(a),
    )
    with pytest.raises(
        ValueError, match="only_paths is incompatible with rebuild"
    ):
        ns["sync_claude_conversations"](
            conn, rebuild=True, only_paths={str(a)})
    assert _store_fingerprint(conn) == before
    assert _conversations_lock_mtime(_cctally_core) == lock_mtime
    assert calls == []


# --- #779: the ordering the rejection depends on ---------------------------
# The incompatible-argument check is the function's first statement and is
# NEVER re-checked. That is only correct because a targeted call meeting a
# PENDING global rebuild returns `deferred_reason="rebuild_pending"` BEFORE
# `rebuild = rebuild or pending_rebuild` runs. Reorder those two and the merge
# turns a legitimate targeted call into `only_paths` + `rebuild` — inherited
# global work mistaken for an explicitly incompatible pair — and the store is
# rebuilt from a targeted request, or (with the check restored after the merge)
# an ordinary live-tail tick raises. Spec §7 required this test and it did not
# exist; `conversation_rebuild_claude_pending` is not in _TARGETED_DECLINE_FLAGS
# and has its own branch, so the pending-global-flag test above does not cover
# it.


def test_targeted_under_a_pending_global_rebuild_defers(isolated):
    ns, conn, projects = isolated
    a = projects / "a.jsonl"
    a.write_text(_asst_line("a1", "m1", "r1", "A"))
    ns["sync_claude_conversations"](conn)
    before = _store_fingerprint(conn)
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) "
                 "VALUES('conversation_rebuild_claude_pending','1')")
    conn.commit()
    a.write_text(_asst_line("a1", "m1", "r1", "A")
                 + _asst_line("a2", "m2", "r2", "AA"))
    stats = ns["sync_claude_conversations"](conn, only_paths={str(a)})
    assert stats.deferred_reason == "rebuild_pending"
    assert stats.targeted_clean is False
    # It deferred rather than ingesting: the appended line is NOT in the store.
    assert _count(conn, a) == 1
    # The marker survives for the backstop full sync that owns the rebuild.
    assert conn.execute(
        "SELECT 1 FROM cache_meta "
        "WHERE key='conversation_rebuild_claude_pending'"
    ).fetchone() is not None
    after = _store_fingerprint(conn)
    del before["cache_meta"], after["cache_meta"]   # the marker we just added
    assert after == before


def test_the_pending_rebuild_deferral_precedes_the_rebuild_merge(isolated):
    """Order-sensitive by construction: it fails if the deferral return is
    moved below `rebuild = rebuild or pending_rebuild`.

    After the merge a targeted call carries `rebuild=True`, so the function
    either raises the #779 ValueError (if the check were re-applied there) or
    performs a global rebuild from a targeted request. Both are visible here:
    a raised ValueError is not the contract, and a rebuild would re-ingest the
    file whose appended line the deferral leaves unread.
    """
    ns, conn, projects = isolated
    a = projects / "a.jsonl"
    b = projects / "b.jsonl"
    a.write_text(_asst_line("a1", "m1", "r1", "A"))
    b.write_text(_asst_line("b1", "m2", "r2", "B", sid="s2"))
    ns["sync_claude_conversations"](conn)
    assert _count(conn, b) == 1
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) "
                 "VALUES('conversation_rebuild_claude_pending','1')")
    conn.commit()
    stats = ns["sync_claude_conversations"](conn, only_paths={str(a)})
    assert stats.deferred_reason == "rebuild_pending"
    assert _count(conn, b) == 1, (
        "a merged rebuild clears the whole store and then re-ingests only the "
        "targeted file, so B's rows are the evidence the deferral ran first"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 2


def test_only_paths_parity_with_full_sync_for_that_file(isolated, tmp_path, monkeypatch):
    ns, conn, projects = isolated
    sync_cache = ns["sync_claude_conversations"]
    a = projects / "a.jsonl"
    a.write_text(_asst_line("a1", "m1", "r1", "A") + _asst_line("a2", "m2", "r2", "AA"))
    sync_cache(conn, only_paths={str(a)})
    targeted = conn.execute(
        "SELECT uuid, entry_type, text, msg_id, model FROM conversation_messages "
        "WHERE source_path=? ORDER BY byte_offset", (str(a),)).fetchall()
    # Fresh cache, full rebuild, same file → identical rows for A.
    conn2 = ns["open_conversations_db"]()
    try:
        sync_cache(conn2, rebuild=True)
        full = conn2.execute(
            "SELECT uuid, entry_type, text, msg_id, model FROM conversation_messages "
            "WHERE source_path=? ORDER BY byte_offset", (str(a),)).fetchall()
    finally:
        conn2.close()
    assert targeted == full


def test_live_tail_default_on(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths_without_conversation_retention(ns, monkeypatch, tmp_path)
    assert ns["_config_known_value"]({}, "dashboard.live_tail") is True


def test_live_tail_set_false_then_read(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths_without_conversation_retention(ns, monkeypatch, tmp_path)
    cfg = {"dashboard": {"live_tail": False}}
    assert ns["_config_known_value"](cfg, "dashboard.live_tail") is False
