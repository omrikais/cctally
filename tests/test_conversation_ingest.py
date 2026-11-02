import json
import sqlite3, sys, pathlib
import types

import pytest
from _fts5_gate import require_fts5  # the ONE FTS5 capability gate (#630 S6)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db

from conftest import load_script, redirect_paths

def _fresh():
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    return conn

def test_schema_creates_conversation_messages():
    conn = _fresh()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversation_messages)")}
    assert {"id", "session_id", "uuid", "parent_uuid", "source_path", "byte_offset",
            "timestamp_utc", "entry_type", "text", "blocks_json", "model", "msg_id",
            "req_id", "cwd", "git_branch", "is_sidechain"} <= cols

def test_storage_dedup_is_source_path_byte_offset_not_uuid():
    conn = _fresh()
    # same uuid replayed across two files must BOTH persist (resume replay)
    for path in ("a.jsonl", "b.jsonl"):
        conn.execute(
            "INSERT OR IGNORE INTO conversation_messages"
            "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
            " VALUES('s','dup',?,0,'t','human','x','[]',0)", (path,))
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 2
    # re-inserting the same (source_path,byte_offset) is idempotent
    conn.execute(
        "INSERT OR IGNORE INTO conversation_messages"
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
        " VALUES('s','dup','a.jsonl',0,'t','human','x','[]',0)")
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 2

def test_fts_present_when_available_and_indexes_text():
    conn = _fresh()
    require_fts5()
    conn.execute("INSERT INTO conversation_messages"
                 "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
                 " VALUES('s','u','f',0,'t','assistant','token limit window','[]',0)")
    hits = conn.execute("SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH 'token'").fetchall()
    assert len(hits) == 1
    flag = conn.execute("SELECT value FROM cache_meta WHERE key='fts5_unavailable'").fetchone()
    assert flag is None  # not set when FTS works

def test_delete_propagates_to_fts():
    conn = _fresh()
    require_fts5()
    conn.execute("INSERT INTO conversation_messages"
                 "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
                 " VALUES('s','u','f',0,'t','assistant','findme','[]',0)")
    conn.execute("DELETE FROM conversation_messages")
    assert conn.execute("SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'findme'").fetchone()[0] == 0

def test_fts_unavailable_sets_flag_and_skips_fts_table(monkeypatch):
    # The documented test seam: when the sqlite build lacks FTS5, _apply_cache_schema
    # must create NEITHER the virtual table NOR the triggers, and persist the flag so
    # search falls back to LIKE.
    monkeypatch.setattr(db, "_fts5_available", lambda conn: False)
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    flag = conn.execute("SELECT value FROM cache_meta WHERE key='fts5_unavailable'").fetchone()
    assert flag is not None and flag[0] == "1"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "conversation_fts" not in names
    trigs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert not any(t.startswith("conv_fts_") for t in trigs)

def test_update_text_reindexes_fts():
    conn = _fresh()
    require_fts5()
    conn.execute("INSERT INTO conversation_messages"
                 "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
                 " VALUES('s','u','f',0,'t','assistant','alpha','[]',0)")
    conn.execute("UPDATE conversation_messages SET text='beta' WHERE uuid='u'")
    assert conn.execute("SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'beta'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'").fetchone()[0] == 0


# ──────────────────────────────────────────────────────────────────────────
# #177 S6: the split multi-column conversation_fts(text, search_tool,
# search_thinking) replaces the old prose + aux pair — present / delete-
# propagates / per-column-update-reindexes — plus the FTS-unavailable drop and
# the create-failure all-or-nothing envelope under the single fts5_unavailable
# flag.
# ──────────────────────────────────────────────────────────────────────────

def _insert_split(conn, uuid, off, *, text="", search_tool="", search_thinking="",
                  entry_type="assistant"):
    conn.execute(
        "INSERT INTO conversation_messages"
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,"
        " blocks_json,is_sidechain,search_tool,search_thinking)"
        " VALUES('s',?,?,?, 't',?,?,'[]',0,?,?)",
        (uuid, f"f{off}", off, entry_type, text, search_tool, search_thinking),
    )


def test_split_fts_present_when_available_and_indexes_columns():
    conn = _fresh()
    require_fts5()
    _insert_split(conn, "u", 0, text="PROSETOKEN", search_tool="TOOLTOKEN command",
                  search_thinking="THINKTOKEN")
    # each column matchable via the column-filter syntax; prose-only and
    # tool-only stay disjoint.
    assert conn.execute(
        "SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH '{text}: PROSETOKEN'"
    ).fetchall() == [(1,)]
    assert conn.execute(
        "SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH '{search_tool}: TOOLTOKEN'"
    ).fetchall() == [(1,)]
    assert conn.execute(
        "SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH '{search_thinking}: THINKTOKEN'"
    ).fetchall() == [(1,)]
    # a tool-only token does NOT match the {text} column filter.
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH '{text}: TOOLTOKEN'"
    ).fetchone()[0] == 0


def test_split_delete_propagates_to_fts():
    conn = _fresh()
    require_fts5()
    _insert_split(conn, "u", 0, search_tool="findtool")
    conn.execute("DELETE FROM conversation_messages")
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'findtool'"
    ).fetchone()[0] == 0


def test_split_update_reindexes_each_column():
    conn = _fresh()
    require_fts5()
    _insert_split(conn, "u", 0, search_tool="alphatool", search_thinking="alphathink")
    conn.execute("UPDATE conversation_messages SET search_tool='betatool' WHERE uuid='u'")
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'betatool'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alphatool'"
    ).fetchone()[0] == 0
    # the AU trigger fires on any of text/search_tool/search_thinking — a
    # search_thinking update reindexes too.
    conn.execute("UPDATE conversation_messages SET search_thinking='betathink' WHERE uuid='u'")
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'betathink'"
    ).fetchone()[0] == 1
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")


def test_fts_unavailable_drops_table_under_one_flag(monkeypatch):
    """The single fts5_unavailable flag covers the consolidated index — when
    FTS5 is unavailable conversation_fts does not exist and no conv_fts_* trigger
    survives. (The old conversation_fts_aux is gone under the split shape.)"""
    monkeypatch.setattr(db, "_fts5_available", lambda conn: False)
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    flag = conn.execute("SELECT value FROM cache_meta WHERE key='fts5_unavailable'").fetchone()
    assert flag is not None and flag[0] == "1"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "conversation_fts" not in names
    assert "conversation_fts_aux" not in names
    trigs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert not any(t.startswith("conv_fts_") for t in trigs)


def test_split_fts_create_failure_drops_and_insert_still_commits(monkeypatch):
    """If the split FTS create fails, the schema apply must drop the index +
    trigger set, set the fts5_unavailable flag, and a later
    conversation_messages INSERT must still commit (the shared write txn — which
    also carries session_entries cost ingest — is NOT rolled back)."""
    conn = sqlite3.connect(":memory:")
    require_fts5()

    # Fail the split-table create by feeding a malformed DDL through the seam the
    # apply uses (the _CONV_FTS_SPLIT_DDL execute). Simulate by monkeypatching
    # _create_conversation_fts_triggers to raise AFTER the table create — that
    # exercises the same except-arm cleanup (drop table + trigger set + flag).
    def _boom(c):
        raise sqlite3.OperationalError("simulated split fts trigger create failure")

    monkeypatch.setattr(db, "_create_conversation_fts_triggers", _boom)
    db._apply_cache_schema(conn)

    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "conversation_fts" not in names, "split FTS must be dropped on create failure"
    assert "conversation_fts_aux" not in names
    trigs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert not any(t.startswith("conv_fts_") for t in trigs), "trigger set dropped"
    flag = conn.execute("SELECT value FROM cache_meta WHERE key='fts5_unavailable'").fetchone()
    assert flag is not None and flag[0] == "1", "the shared flag is set"

    # The load-bearing assertion: a conversation_messages INSERT still commits —
    # no orphan trigger over a missing table rolls back the shared write txn.
    conn.execute(
        "INSERT INTO conversation_messages"
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,"
        " blocks_json,is_sidechain,search_tool)"
        " VALUES('s','u','f',0,'t','assistant','x','[]',0,'a')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 1


# ──────────────────────────────────────────────────────────────────────────
# Task 4: sync_cache second seek-and-walk ingest + lifecycle (real path)
#
# Driven against the real ``sync_cache`` via the established
# ``load_script() + redirect_paths()`` harness (per the "HOME-only test
# loader reads prod DB" gotcha — bare ``setenv(HOME)`` would read the
# developer's real cache.db once ``_cctally_core`` is cached). The harness
# pins every path constant (including ``CACHE_DB_PATH`` and the Claude
# projects dir at ``<tmp>/.claude/projects``) to the per-test tmp tree.
# ──────────────────────────────────────────────────────────────────────────


def _asst_line(uuid, msg_id, req_id, text, *, ts="2026-06-01T00:00:00Z",
               model="claude-opus-4-7", out_tokens=5):
    """One assistant JSONL line carrying both a cost (msg_id/req_id/usage)
    AND a conversation surface (content text)."""
    return json.dumps({
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "s1",
        "requestId": req_id,
        "timestamp": ts,
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": out_tokens,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def _user_line(uuid, text, *, ts="2026-06-01T00:01:00Z"):
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": "s1", "timestamp": ts,
        "message": {"role": "user", "content": text},
    }) + "\n"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Load the split core/conversation stores under redirected tmp paths.

    Returns the conversation connection so the existing transcript assertions
    keep exercising the same SQL surface.  Its read-only ``cache_db`` attachment
    also preserves the handful of accounting assertions in this suite.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    cache_conn = ns["open_cache_db"]()
    conn = ns["open_conversations_db"]()
    sync_cache = ns["sync_cache"]
    sync_conversations = ns["sync_claude_conversations"]

    def sync(rebuild=False):
        sync_cache(cache_conn, rebuild=rebuild)
        return sync_conversations(conn, rebuild=rebuild)

    yield ns, conn, projects, sync
    try:
        conn.close()
    except Exception:
        pass
    try:
        cache_conn.close()
    except Exception:
        pass


def _conv_rows(conn):
    return conn.execute(
        "SELECT uuid, source_path, byte_offset, entry_type, text, msg_id, model "
        "FROM conversation_messages ORDER BY source_path, byte_offset"
    ).fetchall()


def test_iter_sync_entries_parses_each_line_once(monkeypatch):
    """#138 acceptance: the fused walker parses each JSONL line exactly once
    (one json.loads per line), yielding a (offset, cost, msgrow, aititle) tuple
    per productive line. The pre-#138 design walked the delta byte range TWICE
    (cost walk then conversation seek-and-walk), parsing every line twice. #193:
    parse_ai_title runs on the SAME already-parsed obj — still one parse/line."""
    import io
    import _cctally_cache as cache
    body = (_asst_line("a1", "m1", "r1", "one")
            + _user_line("u1", "two")
            + _asst_line("a2", "m2", "r2", "three"))
    fh = io.StringIO(body)

    calls = {"n": 0}
    real = json.loads

    def spy(s, *a, **k):
        calls["n"] += 1
        return real(s, *a, **k)

    # Rebind the name on the IMPORTING module, not on the shared stdlib object.
    # `cache.json is json`, so `monkeypatch.setattr(cache.json, "loads", spy)`
    # replaced json.loads process-wide for the duration of this test, where any
    # concurrent thread would have seen the spy. #628 fixed exactly this at
    # tests/test_speed_column_read_paths.py:151-176; this was the second
    # instance.
    cache_local_json = types.SimpleNamespace(**vars(json))
    cache_local_json.loads = spy
    monkeypatch.setattr(cache, "json", cache_local_json)
    out = list(cache._iter_sync_entries(fh, "/p/a.jsonl"))

    assert json.loads('{"unrelated": true}') == {"unrelated": True}, (
        "the cache-local spy must not replace stdlib json.loads process-wide"
    )

    assert calls["n"] == 3, f"one json.loads per line expected, got {calls['n']}"
    assert len(out) == 3
    o0, c0, m0, t0 = out[0]
    o1, c1, m1, t1 = out[1]
    o2, c2, m2, t2 = out[2]
    # assistant lines yield BOTH a cost tuple and a message row; user line is
    # a message row only. None of these are ai-title lines.
    assert c0 is not None and m0 is not None and m0.entry_type == "assistant"
    assert c1 is None and m1 is not None and m1.entry_type == "human"
    assert c2 is not None and m2 is not None
    assert t0 is None and t1 is None and t2 is None
    # cost tuple shape is (UsageEntry, msg_id, req_id)
    assert c0[1] == "m1" and c0[2] == "r1"
    # offsets are byte-accurate and strictly increasing; the tuple's offset
    # equals the message row's own byte_offset.
    assert o0 == 0 and o1 == m1.byte_offset and o2 == m2.byte_offset
    assert o0 < o1 < o2


def test_iter_sync_entries_partial_tail_rewinds():
    """A mid-write tail line (no trailing newline) rewinds the handle and stops,
    so fh.tell() after the loop is the cost cursor's final_offset and the next
    sync re-reads the line once complete — same discipline as the leaf walkers."""
    import io
    import _cctally_cache as cache
    complete = _asst_line("a1", "m1", "r1", "ok")
    partial = '{"type":"assistant","uuid":"a2"'  # no newline
    fh = io.StringIO(complete + partial)
    out = list(cache._iter_sync_entries(fh, "/p/a.jsonl"))
    assert len(out) == 1
    assert fh.tell() == len(complete)  # rewound to start of the partial line
    assert fh.readline().startswith('{"type":"assistant","uuid":"a2"')


def test_sync_ingests_messages_and_dedups_replay(isolated):
    """Two files: b.jsonl resumes a.jsonl, replaying a1's uuid. Both physical
    rows are stored (UNIQUE(source_path, byte_offset)); the uuid is only
    indexed, never a dedup key. Reader-level dedup is Plan 2."""
    ns, conn, projects, sync = isolated
    a1 = _asst_line("a1", "m1", "r1", "answer one")
    (projects / "a.jsonl").write_text(a1)
    (projects / "b.jsonl").write_text(a1 + _user_line("u2", "next"))

    sync(rebuild=True)

    # 3 physical message rows: a1 in a.jsonl, a1 replayed in b.jsonl, u2.
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 3
    assert conn.execute(
        "SELECT COUNT(DISTINCT uuid) FROM conversation_messages"
    ).fetchone()[0] == 2
    # Both a1 rows are assistants carrying msg_id 'm1' + model.
    a_rows = conn.execute(
        "SELECT entry_type, msg_id, model FROM conversation_messages "
        "WHERE uuid='a1'"
    ).fetchall()
    assert len(a_rows) == 2
    assert all(r == ("assistant", "m1", "claude-opus-4-7") for r in a_rows)
    # u2 is a human prompt with prose indexed.
    u_row = conn.execute(
        "SELECT entry_type, text FROM conversation_messages WHERE uuid='u2'"
    ).fetchone()
    assert u_row == ("human", "next")


def test_sync_delta_append_ingests_only_new_messages(isolated):
    """An append-only growth must ingest only the newly-appended message,
    not re-walk from offset 0 — the conversation walk shares the cost walk's
    [start_offset, final_offset] region."""
    ns, conn, projects, sync = isolated
    f = projects / "a.jsonl"
    f.write_text(_asst_line("a1", "m1", "r1", "first"))
    sync()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 1
    first_offset = conn.execute(
        "SELECT byte_offset FROM conversation_messages WHERE uuid='a1'"
    ).fetchone()[0]
    assert first_offset == 0

    with f.open("a") as fh:
        fh.write(_user_line("u2", "second"))
    sync()
    rows = _conv_rows(conn)
    assert [r[0] for r in rows] == ["a1", "u2"]
    # The appended row starts AFTER the first line (delta resume, not a
    # re-walk from 0).
    assert rows[1][2] > 0


def test_sync_cursor_consistency_byte_offset_unchanged(isolated):
    """The conversation walk re-seeks the SAME handle the cost walk used; it
    must NOT corrupt session_files.last_byte_offset (written from the cost
    walk's final_offset). The recorded offset must equal the file size."""
    ns, conn, projects, sync = isolated
    f = projects / "a.jsonl"
    body = _asst_line("a1", "m1", "r1", "x") + _user_line("u2", "y")
    f.write_text(body)
    sync()
    last_off = conn.execute(
        "SELECT last_byte_offset FROM session_files WHERE path=?", (str(f),)
    ).fetchone()[0]
    assert last_off == f.stat().st_size, (
        "last_byte_offset must reflect the cost walk's final_offset (== EOF), "
        "never be clobbered by the conversation walk's fh movement"
    )


def test_sync_rebuild_clears_conversation_messages(isolated):
    """A --rebuild full-clear empties conversation_messages then re-ingests
    only what is currently on disk."""
    ns, conn, projects, sync = isolated
    f = projects / "a.jsonl"
    f.write_text(_asst_line("a1", "m1", "r1", "keep") + _user_line("u2", "drop"))
    sync()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 2

    # Shrink the on-disk content, then rebuild: the table must reflect only
    # the new content, never stale rows.
    f.write_text(_asst_line("a9", "m9", "r9", "fresh"))
    sync(rebuild=True)
    rows = _conv_rows(conn)
    assert [r[0] for r in rows] == ["a9"]


def test_sync_truncation_full_resets_then_reingests(isolated):
    """Truncating a tracked file (size shrinks) escalates to a full reset of
    conversation_messages (parallel to session_entries) then re-ingests every
    file from offset 0. No stale rows from the pre-truncation content survive."""
    ns, conn, projects, sync = isolated
    fa = projects / "a.jsonl"
    fb = projects / "b.jsonl"
    fa.write_text(
        _asst_line("a1", "m1", "r1", "alpha")
        + _user_line("u1", "alpha2")
    )
    fb.write_text(_asst_line("b1", "mb", "rb", "bravo"))
    sync()
    assert sorted(r[0] for r in _conv_rows(conn)) == ["a1", "b1", "u1"]

    # Truncate A (shrinks below tracked size) -> escalation full reset.
    fa.write_text(_asst_line("a2", "m2", "r2", "rewritten"))
    stats = sync()
    assert stats.files_reset_truncated >= 1
    rows = sorted(r[0] for r in _conv_rows(conn))
    # B preserved (re-ingested), A's old rows gone, A's new row present. No
    # stale 'a1'/'u1' rows linger from the pre-truncation content.
    assert rows == ["a2", "b1"], rows


def test_sync_cost_cardinality_one_session_entry_per_turn(isolated):
    """Cost-cardinality scaffolding (Plan 2 verifies the SUM end-to-end). A
    single assistant turn produces exactly ONE session_entries row keyed on
    (msg_id, req_id), even though it also produces a conversation_messages
    row. The two indexes are independent: messages are keyed by physical line,
    cost by logical turn."""
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(_asst_line("a1", "m1", "r1", "one turn"))
    sync()
    assert conn.execute(
        "SELECT COUNT(*) FROM session_entries WHERE msg_id='m1' AND req_id='r1'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE msg_id='m1'"
    ).fetchone()[0] == 1


def test_sync_cost_path_unchanged_session_entries_present(isolated):
    """Belt-and-suspenders: adding the second walk must NOT disturb the cost
    rows — session_entries is still populated with the right token totals."""
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(
        _asst_line("a1", "m1", "r1", "x", out_tokens=42)
    )
    sync()
    row = conn.execute(
        "SELECT input_tokens, output_tokens FROM session_entries "
        "WHERE msg_id='m1' AND req_id='r1'"
    ).fetchone()
    assert row == (10, 42)


# ──────────────────────────────────────────────────────────────────────────
# Task 5: backfill_conversation_messages walker (existing-install path)
# ──────────────────────────────────────────────────────────────────────────


def test_backfill_populates_existing_install_and_is_idempotent(isolated):
    """Simulate the pre-feature state: cost already ingested (session_files
    cursors at EOF, session_entries populated) but conversation_messages
    EMPTY. The backfill walker populates it from all JSONL at offset 0, and a
    second run is a no-op (INSERT OR IGNORE on (source_path, byte_offset))."""
    ns, conn, projects, sync = isolated
    fa = projects / "a.jsonl"
    fb = projects / "b.jsonl"
    fa.write_text(_asst_line("a1", "m1", "r1", "hello") + _user_line("u1", "q1"))
    fb.write_text(_asst_line("b1", "mb", "rb", "world"))

    # Cost-ingest only (this is what a pre-feature cache already had), then
    # wipe conversation_messages to model the upgrade state.
    sync()
    conn.execute("DELETE FROM conversation_messages")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 0
    # session cost cursors are at EOF (cost was ingested).
    offs = dict(conn.execute(
        "SELECT path, last_byte_offset FROM session_files"
    ).fetchall())
    assert all(v > 0 for v in offs.values()), offs

    backfill = ns["backfill_conversation_messages"]
    inserted = backfill(conn)
    n1 = conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0]
    assert n1 == 3, n1
    assert inserted == 3
    # Cost cursors UNTOUCHED by the backfill (it must not move offsets).
    offs2 = dict(conn.execute(
        "SELECT path, last_byte_offset FROM session_files"
    ).fetchall())
    assert offs2 == offs

    # Idempotent re-run: no new rows.
    inserted2 = backfill(conn)
    assert inserted2 == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == n1


def test_backfill_migration_stamped_not_run_on_fresh_install(tmp_path, monkeypatch):
    """A fresh install (no session_entries) must STAMP the backfill migration
    without invoking its handler — there is no history to populate. Opening a
    brand-new cache.db must leave 002 marked applied with conversation_messages
    empty and no error."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    # Fresh open: no JSONL on disk, brand-new cache.db.
    conn = ns["open_cache_db"]()
    try:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name='002_conversation_messages_backfill'"
        ).fetchone()
        assert applied is not None, (
            "fresh install must STAMP the backfill migration (framework "
            "fresh-install fast-path)"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_existing_install_backfill_is_consumed_by_conversation_sync_only(
    isolated, monkeypatch,
):
    """Deferred transcript rebuilds never rewind the core accounting cursor."""
    import _cctally_cache as cache
    ns, conn, projects, sync = isolated
    fa = projects / "a.jsonl"
    fb = projects / "b.jsonl"
    fa.write_text(_asst_line("a1", "m1", "r1", "hello") + _user_line("u1", "q1"))
    fb.write_text(_asst_line("b1", "mb", "rb", "world"))

    sync()
    core_cursor = conn.execute(
        "SELECT path,size_bytes,last_byte_offset FROM session_files ORDER BY path"
    ).fetchall()
    conn.execute("DELETE FROM conversation_messages")
    conn.execute(
        "INSERT INTO cache_meta(key,value) "
        "VALUES('conversation_backfill_pending','1')"
    )
    conn.commit()

    calls = {"n": 0}
    real_backfill = cache.backfill_conversation_messages

    def _spy(c):
        calls["n"] += 1
        return real_backfill(c)

    monkeypatch.setattr(cache, "backfill_conversation_messages", _spy)

    sync()
    assert calls["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 3
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_backfill_pending'"
    ).fetchone() is None, "sync must clear the pending flag after backfilling"

    sync()
    assert calls["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 3
    assert conn.execute(
        "SELECT path,size_bytes,last_byte_offset FROM session_files ORDER BY path"
    ).fetchall() == core_cursor


def test_rebuild_clears_pending_flag_without_separate_backfill(isolated, monkeypatch):
    """A ``cache-sync --rebuild`` re-walks every file from offset 0, so its
    normal per-file second seek-and-walk repopulates conversation_messages
    fully. The deferred-backfill flag (issue #139) must therefore be cleared on
    the rebuild path WITHOUT a redundant offset-0 backfill_conversation_messages
    pass — the rebuild already covers it."""
    import _cctally_cache as cache
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(
        _asst_line("a1", "m1", "r1", "hello") + _user_line("u1", "q1")
    )

    # Stand up the deferred-upgrade state: a pending flag set, index empty. Also
    # arm the #177 enrichment reingest flag — the rebuild's normal offset-0 walk
    # re-derives the enriched fields, so it must be cleared on the rebuild path
    # too (else cache-sync --rebuild re-arms it every run).
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES('conversation_backfill_pending','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES('conversation_reingest_enrichment_pending','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    conn.commit()

    calls = {"n": 0}
    real_backfill = cache.backfill_conversation_messages

    def _spy(c):
        calls["n"] += 1
        return real_backfill(c)

    monkeypatch.setattr(cache, "backfill_conversation_messages", _spy)

    sync(rebuild=True)

    # The normal rebuild walk populated the index; the dedicated backfill walker
    # was never invoked (the flag-consume branch is skipped under rebuild).
    assert calls["n"] == 0, (
        "rebuild must NOT run the separate backfill — its normal offset-0 walk "
        "already repopulates conversation_messages"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_backfill_pending'"
    ).fetchone() is None, "rebuild must clear the pending flag directly"
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_reingest_enrichment_pending'"
    ).fetchone() is None, "rebuild must also clear the #177 enrichment reingest flag"


# ──────────────────────────────────────────────────────────────────────────
# #138 item 2: integrity-safe, storm-free FTS full-clear
#   (clear_conversation_messages: drop triggers → truncate base → 'delete-all'
#    on the FTS index → recreate triggers; NOT the per-row delete-trigger storm)
# ──────────────────────────────────────────────────────────────────────────


def _insert_msg(conn, uuid, off, text, *, entry_type="assistant"):
    conn.execute(
        "INSERT INTO conversation_messages"
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
        " VALUES('s',?,?,?, '2026-01-01T00:00:00Z',?,?,'[]',0)",
        (uuid, f"f{off}", off, entry_type, text),
    )


def test_clear_conversation_messages_empties_both_and_keeps_integrity():
    """The full-clear empties BOTH conversation_messages and conversation_fts,
    leaves the triggers in place (a later insert is indexed again), and the FTS
    index passes integrity-check — the load-bearing acceptance criterion. The
    naive 'clear the index then let the per-row delete trigger fire' ordering
    corrupts the external-content index ('database disk image is malformed')."""
    conn = _fresh()
    require_fts5()
    for i in range(6):
        _insert_msg(conn, f"u{i}", i, "alpha findme prose")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] == 6

    db.clear_conversation_messages(conn)
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] == 0
    # Triggers restored: a fresh insert is indexed; old prose stays gone.
    _insert_msg(conn, "fresh", 99, "beta brandnew")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'beta'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] == 0
    # Integrity-check must not raise (would raise on a corrupted index).
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")


def test_clear_conversation_messages_fts_unavailable_plain_delete(monkeypatch):
    """On an FTS5-unavailable build _apply_cache_schema sets the flag + creates
    no triggers / no vtable. The full-clear must still empty the base table
    without issuing a 'delete-all' against the absent conversation_fts."""
    monkeypatch.setattr(db, "_fts5_available", lambda conn: False)
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    for i in range(3):
        _insert_msg(conn, f"u{i}", i, "x")
    conn.commit()
    db.clear_conversation_messages(conn)  # must not raise
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0


def test_sync_rebuild_keeps_fts_integrity(isolated):
    """A `cache-sync --rebuild` full-clears via the storm-free path and the FTS
    index remains integrity-valid + correctly repopulated."""
    ns, conn, projects, sync = isolated
    require_fts5()
    f = projects / "a.jsonl"
    f.write_text(_asst_line("a1", "m1", "r1", "alpha old prose") + _user_line("u1", "alpha more"))
    sync()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] >= 1

    f.write_text(_asst_line("a9", "m9", "r9", "beta fresh prose"))
    sync(rebuild=True)

    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'beta'"
    ).fetchone()[0] == 1
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")


def test_sync_truncation_keeps_fts_integrity(isolated):
    """The truncation-escalation full-clear uses the same storm-free path; the
    FTS index stays integrity-valid after a real reset + re-ingest."""
    ns, conn, projects, sync = isolated
    require_fts5()
    fa = projects / "a.jsonl"
    fb = projects / "b.jsonl"
    fa.write_text(_asst_line("a1", "m1", "r1", "alpha gone") + _user_line("u1", "alpha gone2"))
    fb.write_text(_asst_line("b1", "mb", "rb", "bravo kept"))
    sync()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] >= 1

    fa.write_text(_asst_line("a2", "m2", "r2", "gamma rewritten"))  # shrink → escalation
    stats = sync()
    assert stats.files_reset_truncated >= 1

    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'gamma'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'bravo'"
    ).fetchone()[0] == 1
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")


# ──────────────────────────────────────────────────────────────────────────
# Migration 005: isMeta reingest reclassifies stale 'human' rows to 'meta'
# ──────────────────────────────────────────────────────────────────────────

def _meta_skill_line(uuid, *, path="/x/skills/brainstorming", ts="2026-06-01T00:02:00Z"):
    body = f"Base directory for this skill: {path}\n\n# Skill body"
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": "s1", "timestamp": ts,
        "isMeta": True, "sourceToolUseID": "toolu_x",
        "message": {"role": "user", "content": [{"type": "text", "text": body}]},
    }) + "\n"


def test_migration_005_reingest_reclassifies_ismeta_rows_to_meta(isolated):
    """005 is flag-only (sets conversation_reingest_pending); the offset-0
    re-ingest then re-parses every JSONL through the meta-aware parser, so a
    stale 'human' skill-body row (a pre-upgrade ingest) is reclassified to
    entry_type='meta' with text='' and the flag is cleared (Codex P2.1)."""
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(
        _asst_line("a1", "m1", "r1", "hello") + _meta_skill_line("u1")
    )
    sync()
    # Simulate a PRE-upgrade ingest: the skill body stored as a 'human' prose row
    # (entry_type='human', body in the indexed text column).
    body = "Base directory for this skill: /x/skills/brainstorming\n\n# Skill body"
    conn.execute(
        "UPDATE conversation_messages SET entry_type='human', text=? WHERE uuid='u1'",
        (body,))
    conn.commit()
    assert conn.execute(
        "SELECT entry_type FROM conversation_messages WHERE uuid='u1'"
    ).fetchone()[0] == "human"

    # Run the real 005 handler (flag-only) then sync to consume it.
    db._005_conversation_reingest_meta(conn)
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_reingest_pending'"
    ).fetchone() is not None
    sync()

    row = conn.execute(
        "SELECT entry_type, text, blocks_json FROM conversation_messages WHERE uuid='u1'"
    ).fetchone()
    assert row is not None, "the skill row survives the reingest"
    assert row[0] == "meta"             # reclassified by the meta-aware parser
    assert row[1] == ""                 # not FTS-indexed / not a title candidate
    assert "Skill body" in row[2]       # blocks still carry the body for rendering
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_reingest_pending'"
    ).fetchone() is None, "the reingest flag is dropped after consumption"


# ──────────────────────────────────────────────────────────────────────────
# #177 Session 1: the enriched data contract lands through the real sync_cache
# write path — the new columns are populated and the aux FTS indexes the tool
# content. Plus migration 007's flag-only reingest cycle.
# ──────────────────────────────────────────────────────────────────────────

def _asst_tooluse_line(uuid, msg_id, req_id, *, cmd="enrichcmd",
                       ts="2026-06-01T00:00:00Z"):
    """An assistant JSONL line carrying a Bash tool_use (so search_tool is
    non-empty) and a message-level stop_reason."""
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": "s1",
        "requestId": req_id, "timestamp": ts,
        "message": {
            "role": "assistant", "id": msg_id, "model": "claude-opus-4-7",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "name": "Bash", "id": "tu1",
                         "input": {"command": cmd}}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def test_sync_lands_enriched_columns_and_split_fts(isolated):
    """A real sync_cache ingests the enriched fields: stop_reason on the row,
    search_tool populated from the tool input, blocks_json carrying structured
    input/input_truncated, and the split FTS indexing the tool content via the
    search_tool column. (#217 S1 / U7a: the documented-dead search_aux column was
    dropped from the live schema, so it is no longer selected here.)"""
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(_asst_tooluse_line("a1", "m1", "r1"))
    sync(rebuild=True)

    row = conn.execute(
        "SELECT stop_reason, search_tool, blocks_json "
        "FROM conversation_messages WHERE uuid='a1'"
    ).fetchone()
    assert row is not None
    assert row[0] == "tool_use"                 # stop_reason column populated
    assert "enrichcmd" in row[1]                # search_tool carries the tool input
    blocks = json.loads(row[2])
    tu = [b for b in blocks if b["kind"] == "tool_use"][0]
    assert tu["input"] == {"command": "enrichcmd"}
    assert tu["input_truncated"] is False
    if db._fts5_available(conn):
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_fts "
            "WHERE conversation_fts MATCH '{search_tool}: enrichcmd'"
        ).fetchone()[0] == 1


def test_migration_007_reingest_lands_enrichment_on_stale_row(isolated):
    """007 is flag-only (sets conversation_reingest_enrichment_pending); the
    offset-0 reingest then re-parses every JSONL through the enriched parser, so
    a stale pre-upgrade row (enriched columns NULL/'') gets them backfilled, and
    the flag is cleared after consumption."""
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(_asst_tooluse_line("a1", "m1", "r1"))
    sync()
    # Simulate a PRE-upgrade ingest: blank out the enriched columns.
    conn.execute(
        "UPDATE conversation_messages SET stop_reason=NULL, search_tool='' WHERE uuid='a1'")
    conn.commit()
    assert conn.execute(
        "SELECT stop_reason, search_tool FROM conversation_messages WHERE uuid='a1'"
    ).fetchone() == (None, "")

    # Run the real 007 handler (flag-only) then sync to consume it.
    db._007_conversation_reingest_enrichment(conn)
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_reingest_enrichment_pending'"
    ).fetchone() is not None
    sync()

    row = conn.execute(
        "SELECT stop_reason, search_tool FROM conversation_messages WHERE uuid='a1'"
    ).fetchone()
    assert row[0] == "tool_use", "stop_reason re-derived by the enriched parser"
    assert "enrichcmd" in row[1], "search_tool re-derived from the tool input"
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_reingest_enrichment_pending'"
    ).fetchone() is None, "the enrichment reingest flag is dropped after consumption"


# ──────────────────────────────────────────────────────────────────────────
# #177 Session 4: migration 009 flag-only media reingest. The flag must be
# wired into EVERY reingest flag site (Codex F2 — a missed site either never
# triggers the reingest or re-arms it forever), and the resumable reingest must
# land the media placeholders + web captures on a stale pre-upgrade row.
# ──────────────────────────────────────────────────────────────────────────

def test_009_flag_in_reingest_flag_keys_and_sql_sites():
    import inspect, _cctally_cache as cc
    assert "conversation_media_reingest_pending" in cc._REINGEST_FLAG_KEYS
    # The two SELECT/DELETE literal lists + the tuple + completion cleanup: the
    # flag string must appear at least 4 times in the module source (Codex F2 —
    # a missed site either never triggers the reingest or re-arms it forever).
    src = inspect.getsource(cc)
    assert src.count("conversation_media_reingest_pending") >= 4


def _user_tool_result_image_line(uuid, tool_use_id, *, ts="2026-06-01T00:02:00Z"):
    """A user JSONL line carrying a tool_result whose content array holds an
    image item (the MCP-screenshot shape) plus a WebSearch toolUseResult so the
    reingest lands both media[] and web_search."""
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": "s1", "timestamp": ts,
        "toolUseResult": {"query": "q1", "results": [
            {"content": [{"title": "T1", "url": "https://e.example/x"}]}]},
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": "image/png",
                                             "data": "AAAA"}},
                {"type": "text", "text": "screenshot"}]}]},
    }) + "\n"


def test_resumable_reingest_consumes_media_flag(isolated):
    """009 is flag-only (sets conversation_media_reingest_pending); the resumable
    reingest re-parses every JSONL through the current parser, so a stale
    pre-upgrade row (no media[] / web_search) gains them, and the flag + cursor +
    gen keys are cleared after consumption."""
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(_user_tool_result_image_line("u1", "tw1"))
    sync()
    # Simulate a PRE-S4 ingest: strip the media + web_search keys off blocks_json.
    blocks = json.loads(conn.execute(
        "SELECT blocks_json FROM conversation_messages WHERE uuid='u1'").fetchone()[0])
    for b in blocks:
        b.pop("media", None)
        b.pop("web_search", None)
    conn.execute("UPDATE conversation_messages SET blocks_json=? WHERE uuid='u1'",
                 (json.dumps(blocks, separators=(",", ":")),))
    conn.commit()
    stale = json.loads(conn.execute(
        "SELECT blocks_json FROM conversation_messages WHERE uuid='u1'").fetchone()[0])
    assert all("media" not in b for b in stale)

    # Run the real 009 handler (flag-only) then sync to consume it.
    db._009_conversation_media_reingest(conn)
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_media_reingest_pending'"
    ).fetchone() is not None
    sync()

    re_blocks = json.loads(conn.execute(
        "SELECT blocks_json FROM conversation_messages WHERE uuid='u1'").fetchone()[0])
    tr = [b for b in re_blocks if b["kind"] == "tool_result"][0]
    assert tr["media"] == [{"kind": "image", "media_type": "image/png",
                            "bytes": 4, "index": 0}], "media[] re-derived by the parser"
    assert tr["web_search"]["query"] == "q1", "web_search capture re-derived"
    for key in ("conversation_media_reingest_pending",
                "conversation_reingest_cursor", "conversation_reingest_cursor_gen"):
        assert conn.execute(
            "SELECT 1 FROM cache_meta WHERE key=?", (key,)
        ).fetchone() is None, f"{key} dropped after consumption"


# ──────────────────────────────────────────────────────────────────────────
# #177 S6: migration 010 search-column split consumed by the REAL sync_cache
# (flag-only handler -> flock-held backfill + legacy->split FTS swap).
# ──────────────────────────────────────────────────────────────────────────

def _to_legacy_shape(conn):
    """Tear the fresh split shape down to the legacy prose+aux two-table shape
    so a real sync can exercise the swap."""
    db._drop_conversation_fts_triggers(conn)
    conn.execute("DROP TABLE IF EXISTS conversation_fts")
    conn.execute("DROP TABLE IF EXISTS conversation_fts_aux")
    conn.execute("CREATE VIRTUAL TABLE conversation_fts "
                 "USING fts5(text, content='conversation_messages', content_rowid='id')")
    db._create_conversation_fts_aux_table(conn)
    db._create_conversation_fts_legacy_triggers(conn)
    conn.commit()


def test_sync_consumes_search_split_flag_backfills_and_swaps(isolated):
    """End-to-end: a legacy-shape cache with the migration-010 flag set is
    swapped to the split shape by the next real sync_cache, the tool content is
    backfilled into search_tool, and the flag + cursor clear."""
    ns, conn, projects, sync = isolated
    require_fts5()
    (projects / "a.jsonl").write_text(_asst_tooluse_line("a1", "m1", "r1"))
    sync()   # split-shape ingest of the tool row
    # Model a pre-S6 install: revert to legacy FTS shape, blank the split
    # columns, arm the migration-010 flag.
    _to_legacy_shape(conn)
    conn.execute("UPDATE conversation_messages SET search_tool='', search_thinking=''")
    db._set_cache_meta(conn, "conversation_search_split_pending", "1")
    conn.commit()
    assert db._conversation_fts_is_split(conn) is False

    sync()   # consumes the flag under the flock

    assert db._conversation_fts_is_split(conn), "sync swapped to the split shape"
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='conversation_fts_aux'"
    ).fetchone() is None
    assert "enrichcmd" in conn.execute(
        "SELECT search_tool FROM conversation_messages WHERE uuid='a1'"
    ).fetchone()[0]
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_search_split_pending'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT rowid FROM conversation_fts "
        "WHERE conversation_fts MATCH '{search_tool}: enrichcmd'"
    ).fetchall() != []
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")


def test_rebuild_swaps_legacy_shape_and_clears_split_flag(isolated):
    """A `cache-sync --rebuild` on a legacy-shape DB with the migration-010 flag
    swaps to the split shape (the walk repopulates the columns through the new
    triggers) and clears the flag without a redundant backfill pass."""
    ns, conn, projects, sync = isolated
    require_fts5()
    (projects / "a.jsonl").write_text(_asst_tooluse_line("a1", "m1", "r1"))
    sync()
    _to_legacy_shape(conn)
    db._set_cache_meta(conn, "conversation_search_split_pending", "1")
    conn.commit()
    assert db._conversation_fts_is_split(conn) is False

    sync(rebuild=True)

    assert db._conversation_fts_is_split(conn), "rebuild swapped to the split shape"
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_search_split_pending'"
    ).fetchone() is None
    assert "enrichcmd" in conn.execute(
        "SELECT search_tool FROM conversation_messages WHERE uuid='a1'"
    ).fetchone()[0]
    assert conn.execute(
        "SELECT rowid FROM conversation_fts "
        "WHERE conversation_fts MATCH '{search_tool}: enrichcmd'"
    ).fetchall() != []
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")


# ──────────────────────────────────────────────────────────────────────────
# #186 Task 1B: parser-level command-marker classification at ingest. A
# slash-command echo carried as a plain (non-isMeta) user line is classified
# entry_type='meta', text='' at ingest — mirroring the isMeta convention so the
# body stays out of FTS, title derivation, and the entry_type='human' prompts
# facet. The all-text guard mirrors the tool_result block-shape gate so an
# attachment-bearing row is never folded.
# ──────────────────────────────────────────────────────────────────────────


def test_command_marker_user_row_classified_meta_at_ingest():
    from _lib_conversation import _normalize
    obj = {"type": "user", "uuid": "u1",
           "message": {"content": "<local-command-stdout>done</local-command-stdout>"}}
    row = _normalize(obj, "user", 0)
    assert row.entry_type == "meta"
    assert row.text == ""               # kept out of FTS / title / prompts-facet
    # the body survives in blocks_json for rendering (isMeta convention)
    assert "<local-command-stdout>done</local-command-stdout>" in row.blocks_json


def test_command_marker_row_with_media_block_stays_human():
    # all-text guard: a marker text PLUS a non-text block must NOT fold
    from _lib_conversation import _normalize
    obj = {"type": "user", "uuid": "u2", "message": {"content": [
        {"type": "text", "text": "<local-command-stdout>x</local-command-stdout>"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "AA"}},
    ]}}
    row = _normalize(obj, "user", 0)
    assert row.entry_type == "human"


def test_command_marker_as_text_block_list_classified_meta():
    # the all-text list shape (a single text block whose body is the marker) also
    # folds to META — the common shape for a multi-block user content array.
    from _lib_conversation import _normalize
    obj = {"type": "user", "uuid": "u3", "message": {"content": [
        {"type": "text",
         "text": "<local-command-stderr>boom</local-command-stderr>"},
    ]}}
    row = _normalize(obj, "user", 0)
    assert row.entry_type == "meta"
    assert row.text == ""


def test_plain_user_prose_stays_human_at_ingest():
    # a real prompt that merely quotes a tag mid-sentence is NOT a marker
    from _lib_conversation import _normalize
    obj = {"type": "user", "uuid": "u4", "message": {
        "content": "see <local-command-stdout>x</local-command-stdout> above"}}
    row = _normalize(obj, "user", 0)
    assert row.entry_type == "human"
    assert "see <local-command-stdout>" in row.text


# ──────────────────────────────────────────────────────────────────────────
# #188 Task A2: a slash-command invocation carrying a real prompt in
# <command-args> is a USER turn — promote it at ingest (entry_type='human',
# text=args) BEFORE the empty-args/stdout system-marker fold. /clear and friends
# (empty args) and stdout-only markers STAY meta (text='').
# ──────────────────────────────────────────────────────────────────────────


def test_command_args_promoted_to_human_at_ingest():
    from _lib_conversation import _normalize
    raw = ("<command-message>frontend-design:frontend-design</command-message>"
           "<command-name>/frontend-design</command-name>"
           "<command-args>Audit the reader UI and file issues.</command-args>")
    obj = {"type": "user", "uuid": "u1",
           "message": {"content": [{"type": "text", "text": raw}]}}
    row = _normalize(obj, "user", 0)
    assert row.entry_type == "human"
    assert row.text == "Audit the reader UI and file issues."
    # the raw wrapper survives in blocks_json (badge derives command_name from it)
    assert "<command-name>/frontend-design</command-name>" in row.blocks_json


def test_command_args_str_content_promoted_to_human_at_ingest():
    # the str-content carrier shape (content is a bare string) also promotes
    from _lib_conversation import _normalize
    raw = ("<command-name>/effort</command-name><command-args>max</command-args>")
    obj = {"type": "user", "uuid": "u1b", "message": {"content": raw}}
    row = _normalize(obj, "user", 0)
    assert row.entry_type == "human" and row.text == "max"


def test_empty_command_args_stays_meta_at_ingest():
    from _lib_conversation import _normalize
    raw = ("<command-name>/clear</command-name>"
           "<command-message>clear</command-message><command-args></command-args>")
    obj = {"type": "user", "uuid": "u2",
           "message": {"content": [{"type": "text", "text": raw}]}}
    row = _normalize(obj, "user", 0)
    assert row.entry_type == "meta"
    assert row.text == ""


def test_command_args_with_attachment_stays_human_unpromoted_full_text():
    # all-text guard: a marker text block PLUS an image is NOT a clean command
    # invocation → it stays a plain human turn (not a promoted args turn).
    from _lib_conversation import _normalize
    raw = "<command-name>/x</command-name><command-args>hi</command-args>"
    obj = {"type": "user", "uuid": "u3", "message": {"content": [
        {"type": "text", "text": raw},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "AA"}},
    ]}}
    row = _normalize(obj, "user", 0)
    assert row.entry_type == "human"
    # NOT collapsed to args — the full raw text rides through (the marker+image
    # case is the documented non-promotion shape)
    assert row.text == raw


def test_command_args_promoted_text_is_fts_searchable():
    # the promoted args become the searchable `text` column (title + FTS facet)
    from _lib_conversation import _normalize
    raw = ("<command-name>/review</command-name>"
           "<command-args>Review the conversation reader code.</command-args>")
    obj = {"type": "user", "uuid": "u4",
           "message": {"content": [{"type": "text", "text": raw}]}}
    row = _normalize(obj, "user", 0)
    assert row.entry_type == "human"
    assert "conversation reader" in row.text


# ──────────────────────────────────────────────────────────────────────────
# #186 Task 1E: _strip_ansi helper + ingest-layer ANSI stripping. ANSI control
# sequences (terminal SGR/CSI/OSC) are stripped from the str-content path, the
# text-block txt, and thinking text in _blocks_and_text — so FTS indexes clean
# tokens for new rows. _stringify is NOT stripped (Bash colors preserved).
# ──────────────────────────────────────────────────────────────────────────


def test_blocks_and_text_strips_ansi_str_path():
    from _lib_conversation import _blocks_and_text
    blocks, text = _blocks_and_text("Set model to \x1b[1mFable 5\x1b[22m now")
    assert "\x1b" not in text and "Fable 5" in text
    assert all("\x1b" not in b.get("text", "") for b in blocks)


def test_blocks_and_text_strips_ansi_text_block():
    from _lib_conversation import _blocks_and_text
    blocks, text = _blocks_and_text(
        [{"type": "text", "text": "hello \x1b[31mred\x1b[0m world"}])
    assert "\x1b" not in text and "red" in text
    assert all("\x1b" not in b.get("text", "") for b in blocks)


def test_blocks_and_text_strips_ansi_thinking():
    from _lib_conversation import _blocks_and_text
    blocks, _ = _blocks_and_text(
        [{"type": "thinking", "thinking": "weigh \x1b[1moptions\x1b[22m"}])
    think = [b for b in blocks if b["kind"] == "thinking"][0]
    assert "\x1b" not in think["text"] and "options" in think["text"]


def test_stringify_preserves_ansi_for_bash():
    from _lib_conversation import _stringify
    # Bash tool_result stringifier MUST preserve SGR for AnsiText colorization.
    assert "\x1b[31m" in _stringify("\x1b[31mred\x1b[0m")
    assert "\x1b[32m" in _stringify(
        [{"type": "text", "text": "\x1b[32mgreen\x1b[0m"}])


def test_strip_ansi_handles_osc_and_lone_esc():
    from _lib_conversation import _strip_ansi
    # OSC (BEL-terminated)
    assert _strip_ansi("a\x1b]0;title\x07b") == "ab"
    # OSC (ST-terminated)
    assert _strip_ansi("a\x1b]0;title\x1b\\b") == "ab"
    # lone / truncated ESC
    assert _strip_ansi("a\x1bb") == "ab"
    # SGR / CSI
    assert _strip_ansi("a\x1b[1mb") == "ab"
    assert _strip_ansi("\x1b[38;5;201mx\x1b[0m") == "x"
    # ordinary '[', digits, 'm' in prose are untouched
    assert _strip_ansi("array[0] has m items") == "array[0] has m items"
    # empty / None passthrough
    assert _strip_ansi("") == ""
    assert _strip_ansi(None) is None


# ──────────────────────────────────────────────────────────────────────────
# #193 Task 3: incremental fused-walk ai-title population (real sync_cache)
# ──────────────────────────────────────────────────────────────────────────


def _ai_title_line(title, *, session_id="s1"):
    return json.dumps({"type": "ai-title", "aiTitle": title,
                       "sessionId": session_id}) + "\n"


def test_sync_cache_populates_ai_titles_incrementally(isolated):
    ns, conn, projects, sync = isolated
    (projects / "sess.jsonl").write_text(
        _user_line("u1", "hello")
        + _ai_title_line("Generated Title", session_id="s1")
    )
    sync()
    assert conn.execute(
        "SELECT ai_title FROM conversation_ai_titles WHERE session_id='s1'"
    ).fetchone()[0] == "Generated Title"


def test_sync_cache_ai_title_last_non_null_wins_in_file(isolated):
    ns, conn, projects, sync = isolated
    (projects / "sess.jsonl").write_text(
        _ai_title_line("First", session_id="s1")
        + _user_line("u1", "hi")
        + _ai_title_line(None, session_id="s1")   # null rewrite — dropped
        + _ai_title_line("Second", session_id="s1")
    )
    sync()
    assert conn.execute(
        "SELECT ai_title FROM conversation_ai_titles WHERE session_id='s1'"
    ).fetchone()[0] == "Second"


def test_backfill_ai_titles_cross_file_mtime_ordering_wins(isolated):
    """A session whose ai-title spans two JSONL files (a ``--resume``) must
    resolve to the title from the MTIME-LATER file. ``backfill_ai_titles`` walks
    files mtime-ascending and UPSERTs on ``session_id`` (last-write-wins), so the
    newer file's title overwrites the older's.

    Non-vacuity guard: the two filenames are chosen so STRING order
    (``a.jsonl`` < ``z.jsonl``) is the OPPOSITE of mtime order — the LATER file
    sorts FIRST alphabetically. An unsorted (or string-sorted) walk would write
    the newer title first and the OLDER title last, picking the wrong winner;
    only the mtime sort yields ``Newer``."""
    ns, conn, projects, sync = isolated
    backfill = ns["backfill_ai_titles"]
    # a.jsonl sorts FIRST by name but carries the NEWER mtime + winning title;
    # z.jsonl sorts LAST by name but is OLDER. mtime-ascending => z then a.
    f_new = projects / "a.jsonl"
    f_old = projects / "z.jsonl"
    f_new.write_text(_ai_title_line("Newer", session_id="s1"))
    f_old.write_text(_ai_title_line("Older", session_id="s1"))
    import os
    os.utime(f_old, (1_700_000_000, 1_700_000_000))   # older
    os.utime(f_new, (1_700_000_100, 1_700_000_100))   # newer (wins)

    n = backfill(conn)
    assert n == 2, n   # both files upserted
    assert conn.execute(
        "SELECT ai_title FROM conversation_ai_titles WHERE session_id='s1'"
    ).fetchone()[0] == "Newer"


def test_backfill_ai_titles_rerun_does_not_regress(isolated):
    """A second ``backfill_ai_titles`` over the same files reproduces the same
    mtime-ascending walk and re-UPSERTs the identical winning title — the
    resolved title is unchanged (idempotent / no regression)."""
    ns, conn, projects, sync = isolated
    backfill = ns["backfill_ai_titles"]
    f_new = projects / "a.jsonl"
    f_old = projects / "z.jsonl"
    f_new.write_text(_ai_title_line("Newer", session_id="s1"))
    f_old.write_text(_ai_title_line("Older", session_id="s1"))
    import os
    os.utime(f_old, (1_700_000_000, 1_700_000_000))
    os.utime(f_new, (1_700_000_100, 1_700_000_100))

    backfill(conn)
    first = conn.execute(
        "SELECT ai_title FROM conversation_ai_titles WHERE session_id='s1'"
    ).fetchone()[0]
    assert first == "Newer"

    backfill(conn)   # re-run
    second = conn.execute(
        "SELECT ai_title FROM conversation_ai_titles WHERE session_id='s1'"
    ).fetchone()[0]
    assert second == first == "Newer", (first, second)


# ──────────────────────────────────────────────────────────────────────────
# #939 D1/D2: Claude prepare-then-apply batches and the pass-scoped cache
# ──────────────────────────────────────────────────────────────────────────
#
# The batch bounds are module constants a test monkeypatches. Faults are real
# SQLite failures wherever SQLite can produce them (TEMP triggers, a deferred
# foreign key, a competing connection); a Python-level hook is used only for
# the exceptions SQLite cannot raise.


def _d1_line(session, uuid, text, *, ts="2026-06-01T00:00:00Z"):
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": session, "timestamp": ts,
        "message": {"role": "user", "content": text},
    }) + "\n"


def _d1_edit_line(session, uuid, target, *, ts="2026-06-01T00:00:30Z"):
    """An assistant turn whose Edit tool use derives a file-touch row."""
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": session,
        "requestId": f"r-{uuid}", "timestamp": ts,
        "message": {
            "role": "assistant", "id": f"m-{uuid}", "model": "claude-opus-4-7",
            "content": [{"type": "tool_use", "name": "Edit", "id": f"t-{uuid}",
                         "input": {"file_path": target, "old_string": "x",
                                   "new_string": "y"}}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def _d1_files(projects, names, *, lines=1):
    """One transcript per name; equal-length names give equal-size files."""
    out = []
    for name in names:
        path = projects / f"{name}.jsonl"
        path.write_text("".join(
            _d1_line(f"s-{name}", f"{name}-{i}", f"{name} text {i}")
            for i in range(lines)))
        out.append(path)
    return out


def _d1_unit():
    return len(_d1_line("s-a", "a-0", "a text 0"))


def _d1_other(ns, *, timeout=5.0):
    return sqlite3.connect(
        str(ns["_cctally_core"].CONVERSATIONS_DB_PATH), timeout=timeout)


def _d1_committed_cursors(ns):
    """Committed cursors by file stem, read through a second connection."""
    other = _d1_other(ns)
    try:
        return {
            pathlib.Path(path).stem: offset for path, offset in other.execute(
                "SELECT path,last_byte_offset FROM conversation_source_files")
        }
    finally:
        other.close()


def _d1_on_parse(monkeypatch, cache, on_start):
    """Call ``on_start(stem)`` as each file's parse begins."""
    real = cache._iter_sync_entries

    def walking(fh, path_str, *args, **kwargs):
        on_start(pathlib.Path(path_str).stem)
        yield from real(fh, path_str, *args, **kwargs)
    monkeypatch.setattr(cache, "_iter_sync_entries", walking)
    return real


def _d1_traced(ns, conn, **kwargs):
    statements: list = []
    conn.set_trace_callback(statements.append)
    try:
        stats = ns["sync_claude_conversations"](conn, **kwargs)
    finally:
        conn.set_trace_callback(None)
    return stats, statements


def _d1_batches(statements, stems):
    """The stems whose cursors each committed transaction wrote, in order."""
    batches, current, previous = [], [], None
    for statement in statements:
        if statement.lstrip().startswith("--") or statement == previous:
            continue
        previous = statement
        head = statement.lstrip().upper()
        if head.startswith(("COMMIT", "END")):
            if current:
                batches.append(current)
            current = []
        elif head.startswith("ROLLBACK") and not head.startswith("ROLLBACK TO"):
            current = []
        elif head.startswith("INSERT INTO CONVERSATION_SOURCE_FILES"):
            current.append(next(
                stem for stem in stems if f"/{stem}.jsonl'" in statement))
    return batches


def _d1_sql_text(value):
    return "'" + str(value).replace("'", "''") + "'"


def test_claude_no_write_transaction_is_open_while_a_file_is_prepared(
        isolated, monkeypatch):
    """Detects: a file parsed and attributed inside an open write transaction,
    which holds the write lock through the parse and pins the attribution
    reads to a snapshot older than the record."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    _d1_files(projects, ["a", "b", "c"], lines=2)
    seen = []
    real = cache._iter_sync_entries

    def walking(fh, path_str, *args, **kwargs):
        for item in real(fh, path_str, *args, **kwargs):
            seen.append((pathlib.Path(path_str).stem, conn.in_transaction))
            yield item
    monkeypatch.setattr(cache, "_iter_sync_entries", walking)
    stats = ns["sync_claude_conversations"](conn)
    assert stats.files_processed == 3
    assert {stem for stem, _open in seen} == {"a", "b", "c"}
    assert not any(open_ for _stem, open_ in seen)


def test_claude_changed_files_share_bounded_batches(isolated, monkeypatch):
    """Detects: a pass that commits once per file (the repeated-frame cost
    #939 measured) or a batch that grows past its file bound."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    names = ["a", "b", "c", "d", "e"]
    _d1_files(projects, names)
    monkeypatch.setattr(cache, "CLAUDE_CONVERSATION_BATCH_FILES", 2)
    stats, statements = _d1_traced(ns, conn)
    assert stats.files_processed == 5 and stats.files_failed == 0
    assert _d1_batches(statements, names) == [["a", "b"], ["c", "d"], ["e"]]


def test_claude_batch_byte_bound_admits_by_new_input(isolated, monkeypatch):
    """Detects: a batch admitted past its byte bound, an exact fit split, or
    an appended file measured by its whole size instead of its new input."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    unit = _d1_unit()
    z = projects / "z.jsonl"
    z.write_text("".join(
        _d1_line("s-z", f"z-{i}", f"z text {i}") for i in range(8)))
    ns["sync_claude_conversations"](conn)
    with z.open("a") as fh:
        fh.write(_d1_line("s-z", "z-8", "z text 8"))
    _d1_files(projects, ["a", "b", "c", "e"])
    _d1_files(projects, ["d"], lines=3)
    monkeypatch.setattr(cache, "CLAUDE_CONVERSATION_BATCH_BYTES", 2 * unit)
    stats, statements = _d1_traced(ns, conn)
    assert stats.files_processed == 6
    # a+b fit exactly; c crosses and starts a batch; d (3 units) is oversized
    # and runs alone; z's append (1 unit) joins e although z is 9 units long.
    assert _d1_batches(statements, ["a", "b", "c", "d", "e", "z"]) == [
        ["a", "b"], ["c"], ["d"], ["e", "z"]]


def test_claude_oversized_file_parses_after_the_open_batch_commits(
        isolated, monkeypatch):
    """Detects: a file that crosses the bound parsed while the open batch
    still holds its rows (two batches of input in memory) or with the batch's
    write transaction open."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    _d1_files(projects, ["a", "b"])
    _d1_files(projects, ["c"], lines=2)
    monkeypatch.setattr(cache, "CLAUDE_CONVERSATION_BATCH_BYTES", 3 * _d1_unit())
    observed = {}

    def on_start(stem):
        if stem == "c":
            observed["cursors"] = set(_d1_committed_cursors(ns))
            observed["open"] = conn.in_transaction
    _d1_on_parse(monkeypatch, cache, on_start)
    stats = ns["sync_claude_conversations"](conn)
    assert stats.files_processed == 3
    assert observed == {"cursors": {"a", "b"}, "open": False}


def test_claude_growth_during_parse_stops_at_the_read_limit(
        isolated, monkeypatch):
    """Detects: a pass that consumes records appended after the file was
    admitted, so a batch holds more input than it admitted."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    (a,) = _d1_files(projects, ["a"], lines=2)
    admitted = a.stat().st_size
    fired = []

    def on_start(_stem):
        if not fired:
            fired.append(True)
            with a.open("a") as fh:
                fh.write(_d1_line("s-a", "a-9", "a text 9"))
    _d1_on_parse(monkeypatch, cache, on_start)
    ns["sync_claude_conversations"](conn)
    assert fired
    assert _d1_committed_cursors(ns) == {"a": admitted}
    assert [r[0] for r in _conv_rows(conn)] == ["a-0", "a-1"]
    ns["sync_claude_conversations"](conn)
    assert _d1_committed_cursors(ns) == {"a": a.stat().st_size}
    assert [r[0] for r in _conv_rows(conn)] == ["a-0", "a-1", "a-9"]


def _d1_store(conn, root):
    """The stored result, with random incarnation ids named by their path and
    ingest times and file identity (inode, mtime, device) removed."""
    current = {
        inc: path for path, inc in conn.execute(
            "SELECT path,source_incarnation_id FROM conversation_source_files")
    }

    def strip(value):
        return value.replace(root, "<root>") if isinstance(value, str) else value

    def inc_name(inc, path):
        return "current:" + strip(path) if inc in current else "old:" + strip(path)

    cols = [r[1] for r in conn.execute("PRAGMA table_info(conversation_messages)")
            if r[1] != "id"]
    messages = [tuple(strip(v) for v in row) for row in conn.execute(
        f"SELECT {','.join(cols)} FROM conversation_messages "
        "ORDER BY source_path,byte_offset")]
    touches = [tuple(strip(v) for v in row) for row in conn.execute(
        "SELECT m.source_path,m.byte_offset,t.session_id,t.uuid,t.file_path,t.tool "
        "FROM conversation_file_touches t JOIN conversation_messages m "
        "ON m.id=t.message_id ORDER BY 1,2,5,6")]
    titles = [tuple(strip(v) for v in row) for row in conn.execute(
        "SELECT session_id,ai_title,source_path,byte_offset "
        "FROM conversation_ai_titles ORDER BY session_id")]
    stamps = sorted(
        (strip(path), inc_name(inc, path), offset, digest, account)
        for path, inc, offset, digest, account in conn.execute(
            "SELECT canonical_source_path,source_incarnation_id,byte_offset,"
            "record_sha256,account_key FROM claude_conversation_account_stamps"))
    sources = [
        (strip(path), size, offset, inc_name(inc, path), prefix)
        for path, size, offset, inc, prefix in conn.execute(
            "SELECT path,size_bytes,last_byte_offset,source_incarnation_id,"
            "committed_prefix_sha256 FROM conversation_source_files ORDER BY path")
    ]
    return {"messages": messages, "touches": touches, "titles": titles,
            "stamps": stamps, "sources": sources}


def _d1_timeline(ns, projects):
    """Two passes over a changing corpus: new files, an append with a
    file-touching tool use, a smaller rewrite (truncation) and a title."""
    for old in projects.glob("*.jsonl"):
        old.unlink()
    conn = ns["open_conversations_db"]()
    try:
        a, b, c = (projects / f"{n}.jsonl" for n in ("a", "b", "c"))
        a.write_text(_d1_line("s-a", "a-0", "alpha") + _d1_edit_line(
            "s-a", "a-1", "/w/one.py"))
        b.write_text("".join(_d1_line("s-b", f"b-{i}", f"bravo {i}")
                             for i in range(3)))
        c.write_text(_d1_line("s-c", "c-0", "charlie"))
        ns["sync_claude_conversations"](conn)
        with a.open("a") as fh:
            fh.write(_d1_line("s-a", "a-2", "alpha again")
                     + _d1_edit_line("s-a", "a-3", "/w/two.py"))
        b.write_text(_d1_line("s-b", "b-9", "rewritten"))
        with c.open("a") as fh:
            fh.write(json.dumps({"type": "ai-title", "aiTitle": "Charlie",
                                 "sessionId": "s-c"}) + "\n")
        (projects / "d.jsonl").write_text(
            _d1_line("s-d", "d-0", "delta") + _d1_line("s-d", "d-1", "delta 2"))
        stats = ns["sync_claude_conversations"](conn)
        assert stats.files_failed == 0 and stats.files_reset_truncated == 1
        return _d1_store(conn, str(projects))
    finally:
        conn.close()


def test_claude_one_file_control_produces_the_same_store(isolated, monkeypatch):
    """Detects: batching that changes what a pass stores (a lost or
    duplicated message, touch, title, stamp or cursor) relative to the
    one-file-per-transaction control."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    conn.close()
    files_bound = cache.CLAUDE_CONVERSATION_BATCH_FILES
    bytes_bound = cache.CLAUDE_CONVERSATION_BATCH_BYTES
    monkeypatch.setattr(cache, "CLAUDE_CONVERSATION_BATCH_FILES", 1)
    monkeypatch.setattr(cache, "CLAUDE_CONVERSATION_BATCH_BYTES", 1)
    control = _d1_timeline(ns, projects)
    db_path = ns["_cctally_core"].CONVERSATIONS_DB_PATH
    for suffix in ("", "-wal", "-shm"):
        pathlib.Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    monkeypatch.setattr(cache, "CLAUDE_CONVERSATION_BATCH_FILES", files_bound)
    monkeypatch.setattr(cache, "CLAUDE_CONVERSATION_BATCH_BYTES", bytes_bound)
    batched = _d1_timeline(ns, projects)
    assert control["touches"] and control["titles"] and control["stamps"]
    assert batched == control


def _d1_wrap_begin(monkeypatch, cache, *, before=None, after=None):
    real = cache._begin_claude_conversation_batch

    def begin(conn):
        if before is not None:
            before(conn)
        real(conn)
        if after is not None:
            after(conn)
    monkeypatch.setattr(cache, "_begin_claude_conversation_batch", begin)
    return real


def test_claude_batch_survives_a_competing_commit_before_apply(
        isolated, monkeypatch):
    """Detects: a batch failed by SQLITE_BUSY_SNAPSHOT because the other
    provider's writer committed between preparation and apply, and a
    truncation read that does not see that commit."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    b = projects / "b.jsonl"
    b.write_text(_d1_line("s-b", "b-0", "bravo") + _d1_line("s-b", "b-1", "two"))
    ns["sync_claude_conversations"](conn)
    b.write_text(_d1_line("s-b", "b-9", "short"))
    _d1_files(projects, ["a"])

    def compete(_conn):
        other = _d1_other(ns)
        try:
            other.execute(
                "INSERT INTO conversation_messages(session_id,uuid,source_path,"
                "byte_offset,timestamp_utc,entry_type,text,blocks_json,"
                "is_sidechain) VALUES('s-rival','rival',?,999999,'t','human',"
                "'rival','[]',0)", (str(b),))
            other.commit()
        finally:
            other.close()
    _d1_wrap_begin(monkeypatch, cache, before=compete)
    stats = ns["sync_claude_conversations"](conn)
    assert stats.files_failed == 0 and stats.files_processed == 2
    assert stats.files_reset_truncated == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE uuid='rival'"
    ).fetchone()[0] == 0


def test_claude_batch_holds_the_write_lock_during_apply(isolated, monkeypatch):
    """Detects: a deferred BEGIN, whose first read pins a snapshot that the
    first write must upgrade (SQLITE_BUSY_SNAPSHOT, which no busy timeout
    retries); the batch must hold the write lock from its BEGIN."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    _d1_files(projects, ["a", "b"])
    outcomes = []

    def probe(_conn):
        other = _d1_other(ns, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            outcomes.append("acquired")
            other.rollback()
        except sqlite3.OperationalError:
            outcomes.append("busy")
        finally:
            other.close()
    _d1_wrap_begin(monkeypatch, cache, after=probe)
    stats = ns["sync_claude_conversations"](conn)
    assert stats.files_processed == 2
    assert outcomes == ["busy"]


@pytest.mark.parametrize("exc_type,committed", [
    (RuntimeError, {"a", "b"}),
    (KeyboardInterrupt, set()),
], ids=["exception", "base-exception"])
def test_claude_prepare_exception_resolves_the_open_batch(
        isolated, monkeypatch, exc_type, committed):
    """Detects: files prepared before an escaping exception being lost (an
    Exception applies them first) or committed under an interrupt (a
    BaseException drops them, for the next pass to re-read)."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    names = ["a", "b", "c", "d", "e"]
    _d1_files(projects, names)

    def on_start(stem):
        if stem == "c":
            raise exc_type("injected")
    real = _d1_on_parse(monkeypatch, cache, on_start)
    with pytest.raises(exc_type):
        ns["sync_claude_conversations"](conn)
    assert set(_d1_committed_cursors(ns)) == committed
    assert not conn.in_transaction
    monkeypatch.setattr(cache, "_iter_sync_entries", real)
    ns["sync_claude_conversations"](conn)
    assert set(_d1_committed_cursors(ns)) == set(names)


def test_claude_unreadable_file_fails_alone_inside_its_batch(
        isolated, monkeypatch):
    """Detects: a read error that fails or splits the rest of its batch, or a
    failed file the #939 D5 residual does not name."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    _a, b, _c = _d1_files(projects, ["a", "b", "c"])

    def on_start(stem):
        if stem == "b":
            raise OSError("injected")
    _d1_on_parse(monkeypatch, cache, on_start)
    stats, statements = _d1_traced(ns, conn)
    assert stats.files_failed == 1 and stats.files_processed == 2
    assert stats.failed_paths == {str(b)}
    assert _d1_batches(statements, ["a", "b", "c"]) == [["a", "c"]]


def test_claude_busy_begin_fails_every_file_of_the_batch(isolated, monkeypatch):
    """Detects: a batch whose BEGIN IMMEDIATE timed out yet advanced a cursor
    or counted a file processed, so the next pass would skip its records."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    paths = _d1_files(projects, ["a", "b", "c"])
    real = cache._begin_claude_conversation_batch

    def begin(c):
        other = _d1_other(ns)
        other.execute("BEGIN IMMEDIATE")
        (previous,) = c.execute("PRAGMA busy_timeout").fetchone()
        c.execute("PRAGMA busy_timeout=0")
        try:
            real(c)
        finally:
            c.execute(f"PRAGMA busy_timeout={int(previous)}")
            other.rollback()
            other.close()
    monkeypatch.setattr(cache, "_begin_claude_conversation_batch", begin)
    stats = ns["sync_claude_conversations"](conn)
    assert stats.files_failed == 3 and stats.files_processed == 0
    assert stats.failed_paths == {str(path) for path in paths}
    assert _d1_committed_cursors(ns) == {}
    assert not conn.in_transaction
    monkeypatch.setattr(cache, "_begin_claude_conversation_batch", real)
    assert ns["sync_claude_conversations"](conn).files_processed == 3


@pytest.mark.parametrize("raise_kind,failed,committed", [
    ("ABORT", 1, {"a", "c"}),
    ("ROLLBACK", 3, set()),
], ids=["savepoint-rollback", "transaction-lost"])
def test_claude_savepoint_database_error(isolated, raise_kind, failed,
                                         committed):
    """Detects: a database error in one file undoing its batch's other files
    (savepoint rollback), or, when the error ended the transaction, files
    counted processed although nothing of the batch was committed."""
    ns, conn, projects, _sync = isolated
    _, b, _ = _d1_files(projects, ["a", "b", "c"])
    conn.execute(
        "CREATE TEMP TRIGGER d1_fail BEFORE INSERT ON main.conversation_source_files "
        f"WHEN NEW.path = {_d1_sql_text(b)} "
        f"BEGIN SELECT RAISE({raise_kind}, 'd1 injected'); END")
    stats = ns["sync_claude_conversations"](conn)
    assert stats.files_failed == failed
    assert stats.files_processed == 3 - failed
    assert set(_d1_committed_cursors(ns)) == committed
    assert not conn.in_transaction
    conn.execute("DROP TRIGGER temp.d1_fail")
    ns["sync_claude_conversations"](conn)
    assert set(_d1_committed_cursors(ns)) == {"a", "b", "c"}


@pytest.mark.parametrize("exc_type,committed", [
    (RuntimeError, {"a"}),
    (KeyboardInterrupt, set()),
], ids=["exception", "base-exception"])
def test_claude_apply_exception_keeps_or_discards_released_files(
        isolated, monkeypatch, exc_type, committed):
    """Detects: an exception during apply that leaves the batch's transaction
    open on the caller's connection, loses the files released before it (an
    Exception commits them) or commits under an interrupt."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    _d1_files(projects, ["a", "b", "c"])
    real = cache._fill_file_touches

    def touching(c, scope=None):
        if scope is not None and any(
                pathlib.Path(path).stem == "b" for path, _offset in scope):
            raise exc_type("injected")
        return real(c, scope=scope)
    monkeypatch.setattr(cache, "_fill_file_touches", touching)
    with pytest.raises(exc_type):
        ns["sync_claude_conversations"](conn)
    assert set(_d1_committed_cursors(ns)) == committed
    assert not conn.in_transaction


def test_claude_commit_failure_fails_every_released_file_once(isolated):
    """Detects: a failed COMMIT that leaves its transaction open or counts a
    file processed whose cursor never committed."""
    ns, conn, projects, _sync = isolated
    paths = _d1_files(projects, ["a", "b", "c"])
    c = paths[2]
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TEMP TABLE d1_parent(id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TEMP TABLE d1_child(pid INTEGER REFERENCES d1_parent(id) "
        "DEFERRABLE INITIALLY DEFERRED)")
    conn.execute(
        "CREATE TEMP TRIGGER d1_defer AFTER INSERT ON main.conversation_source_files "
        f"WHEN NEW.path = {_d1_sql_text(c)} "
        "BEGIN INSERT INTO d1_child VALUES (7); END")
    stats = ns["sync_claude_conversations"](conn)
    assert stats.files_failed == 3 and stats.files_processed == 0
    assert stats.failed_paths == {str(path) for path in paths}
    assert _d1_committed_cursors(ns) == {}
    assert not conn.in_transaction


def test_claude_targeted_truncation_applies_the_open_batch_first(
        isolated, monkeypatch):
    """Detects: the targeted truncation early return dropping the files the
    open batch already prepared, or leaving its transaction open."""
    ns, conn, projects, _sync = isolated
    cache = ns["_cctally_cache"]
    a, b = _d1_files(projects, ["a", "b"], lines=2)
    ns["sync_claude_conversations"](conn)
    with a.open("a") as fh:
        fh.write(_d1_line("s-a", "a-7", "a text 7"))

    def on_start(stem):
        if stem == "a":
            b.write_text(_d1_line("s-b", "b-9", "x"))
    _d1_on_parse(monkeypatch, cache, on_start)
    stats = ns["sync_claude_conversations"](conn, only_paths={str(a), str(b)})
    assert stats.deferred_reason == "truncation"
    assert stats.files_processed == 1
    assert _d1_committed_cursors(ns)["a"] == a.stat().st_size
    assert not conn.in_transaction


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("fails", [False, True], ids=["success", "exception"])
def test_conversation_sync_cache_scope(isolated, provider, fails):
    """Detects: the pass's 64 MiB writer cache leaking onto a connection the
    caller keeps (cache-sync, the viewer's targeted syncs) or onto the
    attached accounting store."""
    ns, conn, projects, _sync = isolated
    if provider == "claude":
        _d1_files(projects, ["a"])
    conn.execute("PRAGMA main.cache_size=-1234")
    (cache_db_before,) = conn.execute("PRAGMA cache_db.cache_size").fetchone()
    during = []

    def progress(phase, _stats):
        if phase == "ingest" and not during:
            during.append((
                conn.execute("PRAGMA main.cache_size").fetchone()[0],
                conn.execute("PRAGMA cache_db.cache_size").fetchone()[0]))
            if fails:
                raise RuntimeError("injected")
    sync = ns[f"sync_{provider}_conversations"]
    if fails:
        with pytest.raises(RuntimeError):
            sync(conn, progress=progress)
    else:
        sync(conn, progress=progress)
    assert during == [(-65536, cache_db_before)]
    assert conn.execute("PRAGMA main.cache_size").fetchone()[0] == -1234
    assert conn.execute(
        "PRAGMA cache_db.cache_size").fetchone()[0] == cache_db_before
