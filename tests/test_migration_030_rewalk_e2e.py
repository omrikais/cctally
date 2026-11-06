"""#195 spec §4.2 / review gate P2b: cache migration 030 against a POPULATED
cache, driven through the real ``sync_cache``.

``tests/test_migration_030_cache_creation_split.py`` proves the chained UPSERT
SQL in isolation and the handler against a fixture; ``test_cache_write_ttl_
pricing.py``'s e2e drives a FRESH ingest, which only exercises the INSERT path.
Nothing wired the two together, so the re-walk itself — the one mechanism that
carries the split onto rows that already exist — was unproven:

  * ``rewalk_armed`` actually selecting ``SESSION_ENTRY_UPSERT_SQL_REWALK``;
  * an existing split-free row gaining its split IN PLACE (row count unchanged);
  * the per-file cursors advancing back to the real size/offset;
  * the flag being cleared after a clean full walk, but NOT after a targeted
    one and NOT after one with an on-disk defect;
  * #916: a walk whose only defect is orphan rows retiring it too, because the
    chained clause is chosen per file from that file's own ``-1`` cursor.
"""
from __future__ import annotations

import os

import pytest
from conftest import load_script, redirect_paths

MODEL = "claude-opus-4-7"
SID_A = "00000000-0000-0000-0000-0000000030a1"
SID_B = "00000000-0000-0000-0000-0000000030b2"
SID_C = "00000000-0000-0000-0000-0000000030c3"
FLAG = "cache_creation_split_rewalk_pending"


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects" / "-p30"
    projects.mkdir(parents=True, exist_ok=True)
    conn = ns["open_cache_db"]()
    yield ns, conn, projects
    conn.close()


def _emit(ns, path, sid, *, msg, req, h, append=False):
    import _fixture_builders as fb
    fb.emit_streaming_pair(
        path, model=MODEL, msg_id=msg, req_id=req,
        ts_intermediate="2026-04-30T10:00:00.100Z",
        ts_final="2026-04-30T10:00:00.500Z",
        intermediate_output_tokens=1, final_output_tokens=500,
        cache_read_tokens=1000, cache_create_tokens=100000,
        cache_1h_tokens=h, input_tokens=10,
        session_id=sid, cwd="/p30", append=append,
    )


def _emit_nullkey(path, sid, h):
    """One assistant line with NO ``requestId``.

    This is the whole reason ``SESSION_ENTRY_UPSERT_SQL_REWALK`` exists: the
    partial dedup index is `WHERE msg_id IS NOT NULL AND req_id IS NOT NULL`,
    so a null-key row's replay skips it and lands on the FULL
    `idx_entries_physical` instead. Without the chained conflict target the
    replay raises IntegrityError and rolls back its whole file — which is what
    makes these tests non-vacuous about `rewalk_armed`."""
    import json
    obj = {
        "type": "assistant", "timestamp": "2026-04-30T10:00:00.500Z",
        "sessionId": sid, "cwd": "/p30",
        "message": {
            "id": "m_nullkey", "model": MODEL,
            "usage": {"input_tokens": 10, "output_tokens": 500,
                      "cache_creation_input_tokens": 100000,
                      "cache_read_input_tokens": 1000,
                      "cache_creation": {"ephemeral_1h_input_tokens": h,
                                         "ephemeral_5m_input_tokens": 100000 - h}},
        },
    }
    path.write_text(json.dumps(obj) + "\n")


def _strip_split(conn):
    """Reduce the ingested rows to their pre-#195 shape: the columns exist
    (add_column_if_missing put them there) but carry no split."""
    conn.execute("UPDATE session_entries "
                 "SET cache_create_1h_tokens=NULL, cache_create_5m_tokens=NULL")
    conn.commit()


def _arm(ns, conn):
    handler = next(m.handler for m in ns["_CACHE_MIGRATIONS"]
                   if m.name == "030_session_entries_cache_creation_split")
    handler(conn)


def _splits(conn):
    return conn.execute(
        "SELECT cache_create_tokens, cache_create_1h_tokens, cache_create_5m_tokens "
        "FROM session_entries ORDER BY source_path").fetchall()


def _flag(conn):
    return conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (FLAG,)).fetchone()


def _cursors(conn):
    return conn.execute(
        "SELECT path, size_bytes, last_byte_offset FROM session_files ORDER BY path"
    ).fetchall()


def _populated(ns, conn, projects):
    """A cache in the exact shape 030 finds on a real upgrade: real rows,
    real cursors, no split, marker present."""
    a = projects / (SID_A + ".jsonl")
    b = projects / (SID_B + ".jsonl")
    c = projects / (SID_C + ".jsonl")
    _emit(ns, a, SID_A, msg="m_a", req="r_a", h=60000)
    _emit(ns, b, SID_B, msg="m_b", req="r_b", h=25000)
    _emit_nullkey(c, SID_C, 10000)
    ns["sync_cache"](conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM session_entries WHERE req_id IS NULL"
    ).fetchone()[0] == 1, "guard: the null-key row must actually be null-keyed"
    _strip_split(conn)
    assert _splits(conn) == [(100000, None, None)] * 3
    return a, b, c


def test_rewalk_enriches_existing_rows_in_place(env):
    ns, conn, projects = env
    a, b, c = _populated(ns, conn, projects)
    before = conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0]
    _arm(ns, conn)
    ns["sync_cache"](conn)

    assert _splits(conn) == [(100000, 60000, 40000), (100000, 25000, 75000),
                             (100000, 10000, 90000)], \
        "the re-walk did not carry the split onto the existing rows"
    assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == before, \
        "the re-walk duplicated rows instead of UPSERTing in place"


def test_rewalk_readvances_the_cursors_and_retires_the_flag(env):
    ns, conn, projects = env
    a, b, c = _populated(ns, conn, projects)
    _arm(ns, conn)
    assert _flag(conn) == ("1",), "guard: the handler must have armed the walk"
    assert [sz for _p, sz, _o in _cursors(conn)] == [-1, -1, -1], \
        "guard: the handler must have invalidated the cursors"

    ns["sync_cache"](conn)

    assert _cursors(conn) == [(str(f), f.stat().st_size, f.stat().st_size)
                              for f in sorted([a, b, c], key=str)], \
        "the cursors did not re-advance to the real size/offset"
    assert _flag(conn) is None, "a clean full walk must retire the arming flag"
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'"
    ).fetchone() is not None


def test_a_targeted_sync_leaves_the_flag_armed(env):
    """The live-tail fast path must not retire the arming flag: it visits one
    file, so the rest of the cache is still split-free."""
    ns, conn, projects = env
    a, b, c = _populated(ns, conn, projects)
    _arm(ns, conn)
    ns["sync_cache"](conn, only_paths={str(a)})

    assert _flag(conn) == ("1",), "a targeted sync retired the flag early"
    got = dict((p, h) for p, h, _5 in conn.execute(
        "SELECT source_path, cache_create_1h_tokens, cache_create_5m_tokens "
        "FROM session_entries"))
    assert got[str(a)] == 60000, "the targeted file should still have re-walked"
    assert got[str(b)] is None, "guard: the untargeted file must still be bare"


TOKEN = "claude_ingest_walk_orphan_residual"
MARKER = "claude_ingest_walk_complete"


def _meta(conn, key):
    return conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()


def _size_of(conn, path):
    return dict((p, sz) for p, sz, _o in _cursors(conn))[str(path)]


def test_a_residual_only_walk_retires_the_flag_and_writes_the_token(env):
    """#916 A3(b); rewritten from ``test_an_unclean_walk_leaves_the_flag_armed``,
    whose coupling (orphans keep the flag armed) this design reverses.

    Detects: a store whose orphan predates migration 030 keeping the split
    flag armed forever. The flag is in the accounting frontier's pending set,
    so such a store could never seed and every dashboard tick re-read every
    transcript. Retiring it is safe because the orphan is the only file still
    at ``-1``, and that cursor re-selects the chained clause if it ever
    reappears (``test_a_reappearing_minus_one_orphan_replays_after_retirement``).
    """
    import _lib_ingest_frontier as frontier
    ns, conn, projects = env
    a, b, c = _populated(ns, conn, projects)
    b.unlink()
    _arm(ns, conn)
    state = frontier.DashboardIngestFrontier(ns["_cctally_core"].APP_DIR)
    cutoff = state.capture_cutoff()
    stats = ns["sync_cache"](conn)

    assert _flag(conn) is None, "a residual-only walk must retire the flag"
    assert _meta(conn, TOKEN) is not None, "a residual-only walk writes the token"
    assert _meta(conn, MARKER) is None, "the orphan still withholds the marker (D5a)"
    assert _size_of(conn, b) == -1, (
        "the orphan keeps its owed re-walk: nothing visited it")
    assert frontier.provider_sync_certifiable("full", stats), stats
    roots = (projects.parent,)
    assert state.seed_provider("claude", conn, roots=roots, cutoff=cutoff), (
        f"last_seed_failure={state.last_seed_failure}")
    plan = state.plan_provider("claude", conn, roots=roots)
    assert plan.mode == "caught_up", (plan.mode, plan.reason)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="chmod-0 unreadable injection has no effect when running as root",
)
def test_an_on_disk_defect_leaves_the_flag_armed(env):
    """The property the rewritten test used to carry, on the defect that still
    means it: a walk that skipped an on-disk file without ingesting it has not
    re-walked that file, so the flag must stay armed and retry next time."""
    import _lib_ingest_frontier as frontier
    ns, conn, projects = env
    a, b, c = _populated(ns, conn, projects)
    _arm(ns, conn)
    a.chmod(0)
    try:
        stats = ns["sync_cache"](conn)
    finally:
        a.chmod(0o644)

    assert stats.files_failed == 1, "guard: the unreadable file must fail its read"
    assert _flag(conn) == ("1",), "an unclean walk retired the flag"
    assert _meta(conn, MARKER) is None and _meta(conn, TOKEN) is None
    assert not frontier.provider_sync_certifiable("full", stats), stats


def test_a_reappearing_minus_one_orphan_replays_after_retirement(env, capsys):
    """#916 A3(c). Detects: an orphan whose ``-1`` cursor outlived the flag
    reappearing with a NULL-key row, replaying from offset 0 through the
    single-target SQL, and losing its whole file to an IntegrityError rollback
    (skipped on every later walk too)."""
    ns, conn, projects = env
    a, b, c = _populated(ns, conn, projects)
    c.unlink()
    _arm(ns, conn)
    ns["sync_cache"](conn)
    assert _flag(conn) is None, "guard: the residual-only walk must retire the flag"
    assert _size_of(conn, c) == -1, "guard: the orphan must still owe its re-walk"

    _emit_nullkey(c, SID_C, 10000)
    capsys.readouterr()
    stats = ns["sync_cache"](conn)

    assert stats.files_failed == 0, capsys.readouterr().err
    rows = conn.execute(
        "SELECT cache_create_tokens, cache_create_1h_tokens, cache_create_5m_tokens "
        "FROM session_entries WHERE source_path=?", (str(c),)).fetchall()
    assert rows == [(100000, 10000, 90000)], (
        "the replay must update the retained row in place, split included")
    size = c.stat().st_size
    assert [(sz, off) for p, sz, off in _cursors(conn) if p == str(c)] == [(size, size)]
    assert _meta(conn, MARKER) is not None and _meta(conn, TOKEN) is None, (
        "the residual cleared, so the walk is complete again")


def _upsert_sql_by_file(statements, files):
    """Which ``session_entries`` UPSERT each file executed: chained or single."""
    used = {}
    for statement in statements:
        if not statement.lstrip().startswith("INSERT INTO session_entries"):
            continue
        kind = ("chained" if "ON CONFLICT(source_path, line_offset)" in statement
                else "single")
        for f in files:
            if f"'{f}'" in statement:
                used.setdefault(str(f), set()).add(kind)
    return used


def _traced_sync(ns, conn):
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        stats = ns["sync_cache"](conn)
    finally:
        conn.set_trace_callback(None)
    return stats, statements


def test_new_files_use_single_target_sql_with_the_flag_absent(env):
    """#916 R2-P2. Detects: the per-file re-walk predicate reading a stale
    ``prev_size`` local, so a new file first in the walk raises
    UnboundLocalError and a new file after a ``-1`` replay inherits its
    chained clause, silently converting migration 020's loud physical-key
    backstop into an update. Observes the SQL each file executed."""
    ns, conn, projects = env
    a, b, c = _populated(ns, conn, projects)
    c.unlink()
    _arm(ns, conn)
    ns["sync_cache"](conn)
    # The state under test, whichever walk retired the flag.
    conn.execute("DELETE FROM cache_meta WHERE key=?", (FLAG,))
    conn.commit()
    assert _size_of(conn, c) == -1, "guard: the orphan must still owe its re-walk"

    _emit_nullkey(c, SID_C, 10000)
    first = projects / "0-new-first.jsonl"   # sorts before every SID_* file
    last = projects / "zz-new-last.jsonl"    # sorts after the -1 replay
    _emit(ns, first, "00000000-0000-0000-0000-0000000030f1",
          msg="m_first", req="r_first", h=1000)
    _emit(ns, last, "00000000-0000-0000-0000-0000000030f2",
          msg="m_last", req="r_last", h=2000)
    stats, statements = _traced_sync(ns, conn)

    assert stats.files_failed == 0, stats
    assert _upsert_sql_by_file(statements, (first, c, last)) == {
        str(first): {"single"}, str(c): {"chained"}, str(last): {"single"},
    }, "only the file still owed its re-walk may use the chained clause"


def test_a_store_without_minus_one_cursors_keeps_the_loud_physical_backstop(
        env, capsys):
    """#916 A7. Detects: the per-file predicate widening beyond ``-1`` cursors,
    so a steady-state physical duplicate (an ingest bug, per migration 020)
    is silently merged instead of failing loudly."""
    ns, conn, projects = env
    a, b, c = _populated(ns, conn, projects)
    assert _flag(conn) is None and all(sz > 0 for _p, sz, _o in _cursors(conn))
    # Rewind the null-key file's cursor so its line replays from offset 0.
    conn.execute("UPDATE session_files SET size_bytes=1, last_byte_offset=0 "
                 "WHERE path=?", (str(c),))
    conn.commit()
    capsys.readouterr()
    stats, statements = _traced_sync(ns, conn)

    assert _upsert_sql_by_file(statements, (c,)) == {str(c): {"single"}}
    assert stats.files_failed == 1, "the physical duplicate must stay loud"
    assert "db error" in capsys.readouterr().err
    assert conn.execute("SELECT COUNT(*) FROM session_entries WHERE source_path=?",
                        (str(c),)).fetchone()[0] == 1
