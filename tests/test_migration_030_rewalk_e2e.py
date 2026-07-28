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
    one and NOT after an unclean one.
"""
from __future__ import annotations

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


def test_an_unclean_walk_leaves_the_flag_armed(env):
    """An orphaned tracked file makes the walk unclean (D5a). The flag rides
    the same condition as the walk-complete marker, so it must stay armed and
    retry on the next clean walk."""
    ns, conn, projects = env
    a, b, c = _populated(ns, conn, projects)
    b.unlink()
    _arm(ns, conn)
    ns["sync_cache"](conn)

    assert _flag(conn) == ("1",), "an unclean walk retired the flag"
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'"
    ).fetchone() is None, "guard: this walk must actually be unclean"
