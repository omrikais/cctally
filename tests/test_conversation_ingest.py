import json
import sqlite3, sys, pathlib

import pytest

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
    if not db._fts5_available(conn):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    conn.execute("INSERT INTO conversation_messages"
                 "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
                 " VALUES('s','u','f',0,'t','assistant','token limit window','[]',0)")
    hits = conn.execute("SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH 'token'").fetchall()
    assert len(hits) == 1
    flag = conn.execute("SELECT value FROM cache_meta WHERE key='fts5_unavailable'").fetchone()
    assert flag is None  # not set when FTS works

def test_delete_propagates_to_fts():
    conn = _fresh()
    if not db._fts5_available(conn):
        import pytest; pytest.skip("sqlite build lacks FTS5")
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
    if not db._fts5_available(conn):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    conn.execute("INSERT INTO conversation_messages"
                 "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
                 " VALUES('s','u','f',0,'t','assistant','alpha','[]',0)")
    conn.execute("UPDATE conversation_messages SET text='beta' WHERE uuid='u'")
    assert conn.execute("SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'beta'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'").fetchone()[0] == 0


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
    """Load bin/cctally under a redirected tmp data dir + Claude projects
    tree. Returns (ns, conn, projects_dir, sync)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    conn = ns["open_cache_db"]()
    sync_cache = ns["sync_cache"]

    def sync(rebuild=False):
        return sync_cache(conn, rebuild=rebuild)

    yield ns, conn, projects, sync
    try:
        conn.close()
    except Exception:
        pass


def _conv_rows(conn):
    return conn.execute(
        "SELECT uuid, source_path, byte_offset, entry_type, text, msg_id, model "
        "FROM conversation_messages ORDER BY source_path, byte_offset"
    ).fetchall()


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


def test_existing_install_defers_backfill_to_sync_then_consumes_flag(isolated, monkeypatch):
    """REGRESSION (Plan 1 Task 5; deferral is issue #139): on an EXISTING
    install the 002 handler must NOT walk the JSONL history inline — that
    blocked the triggering command, even a stats-only ``cctally report`` that
    fires the cache dispatcher but never reads cache.db. Instead the handler
    sets the ``conversation_backfill_pending`` cache_meta flag and returns (the
    dispatcher central-stamps the migration marker on the handler's clean
    return, #140); the next ``sync_cache`` — which already holds the flock + owns the
    walker — consumes the flag and runs the offset-0 backfill once.

    Models the upgrade state precisely: cost already ingested (session_entries
    non-empty, session_files cursors at EOF) but conversation_messages empty AND
    no 002 marker. Spies on the real walker to prove WHO runs it and WHEN:
      1. After the dispatcher run the marker is persisted and the flag is SET,
         but the backfill has NOT run (spy 0) and conversation_messages is still
         empty — the command returns without the walk.
      2. The first ``sync_cache`` consumes the flag: backfill runs exactly once
         (spy 1), conversation_messages is populated, and the flag is cleared.
      3. A second ``sync_cache`` does NOT re-run the backfill (spy still 1, flag
         already gone) — stable row counts alone are insufficient since INSERT
         OR IGNORE keeps them stable even on a redundant re-walk.
      4. A second dispatcher run does NOT re-invoke the handler (the marker
         persists), so the flag is never re-set.
    """
    import _cctally_db as db
    import _cctally_cache as cache  # the module whose global sync_cache calls
    ns, conn, projects, sync = isolated
    fa = projects / "a.jsonl"
    fb = projects / "b.jsonl"
    fa.write_text(_asst_line("a1", "m1", "r1", "hello") + _user_line("u1", "q1"))
    fb.write_text(_asst_line("b1", "mb", "rb", "world"))

    # Cost-ingest (populates session_entries + session_files cursors at EOF and,
    # on this fresh cache, stamps 002 via the fresh-install fast-path). Then
    # rewind to the pre-feature upgrade state: drop the 002 marker, empty
    # conversation_messages + any pending flag, and roll PRAGMA user_version
    # back to 1 (the value a cache.db carried when only cache 001 was registered
    # — before 002 shipped). That reproduces exactly what an upgrading install
    # looks like: schema_migrations exists with 001 applied, session_entries
    # non-empty (=> NOT fresh), 002 pending, user_version < len(registry) so the
    # dispatcher's user_version fast-path does NOT short-circuit the walk.
    sync()
    conn.execute(
        "DELETE FROM schema_migrations "
        "WHERE name='002_conversation_messages_backfill'"
    )
    conn.execute("DELETE FROM conversation_messages")
    conn.execute("DELETE FROM cache_meta WHERE key='conversation_backfill_pending'")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT 1 FROM schema_migrations "
        "WHERE name='002_conversation_messages_backfill'"
    ).fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] > 0

    # Spy on the real walker. sync_cache calls the bare module global
    # ``backfill_conversation_messages``; rebinding it on the module patches the
    # exact name sync_cache resolves at call time.
    calls = {"n": 0}
    real_backfill = cache.backfill_conversation_messages

    def _spy(c):
        calls["n"] += 1
        return real_backfill(c)

    monkeypatch.setattr(cache, "backfill_conversation_messages", _spy)

    # (1) Dispatcher run: 002 pending on an existing install -> handler sets the
    # flag and the dispatcher central-stamps the marker (#140), but does NOT walk.
    db._run_pending_migrations(
        conn, registry=db._CACHE_MIGRATIONS, db_label="cache.db",
    )
    assert calls["n"] == 0, "handler must NOT walk inline — it only sets a flag"
    assert conn.execute(
        "SELECT 1 FROM schema_migrations "
        "WHERE name='002_conversation_messages_backfill'"
    ).fetchone() is not None, (
        "the dispatcher must central-stamp 002's marker on the handler's clean "
        "return (#140) — even on the existing-install path"
    )
    assert conn.execute(
        "SELECT value FROM cache_meta WHERE key='conversation_backfill_pending'"
    ).fetchone() == ("1",), "handler must set the pending flag"
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 0, "no inline backfill — the command returns un-stalled"

    # (2) First sync consumes the flag: backfill runs ONCE, index populated,
    # flag cleared.
    sync()
    assert calls["n"] == 1, "the first sync after the flag must run the backfill"
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 3
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_backfill_pending'"
    ).fetchone() is None, "sync must clear the pending flag after backfilling"

    # (3) Second sync does NOT re-run the backfill (flag gone).
    sync()
    assert calls["n"] == 1, (
        "a later sync must NOT re-walk — the flag was cleared, so the one-time "
        "backfill never repeats"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 3

    # (4) Second dispatcher run: the persisted marker keeps the handler from
    # re-running, so the flag is never re-set.
    db._run_pending_migrations(
        conn, registry=db._CACHE_MIGRATIONS, db_label="cache.db",
    )
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_backfill_pending'"
    ).fetchone() is None, (
        "a re-dispatch must not re-set the flag — the marker persists the "
        "migration so the handler never re-runs"
    )


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

    # Stand up the deferred-upgrade state: a pending flag set, index empty.
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES('conversation_backfill_pending','1') "
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
