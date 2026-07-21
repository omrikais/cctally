"""Browse-rail rollup (conversation_sessions) maintenance — Task A.

Drives the REAL sync_cache over synthetic Claude JSONL through the
load_script()+redirect_paths() loader (NOT a bare setenv("HOME", …) — that reads
the real prod cache.db once _cctally_core is cached). The load-bearing invariant
across every mutation path is that conversation_sessions is byte-identical to a
live GROUP BY over conversation_messages, pinned by assert_rollup_matches_live.
"""
import json
import sqlite3

import pytest

from conftest import load_script, redirect_paths  # type: ignore

FLAG = "conversation_sessions_backfill_pending"


# ---------------------------------------------------------------------------
# Synthetic Claude JSONL lines (same shape the #179 reingest test seeds with).
# ---------------------------------------------------------------------------
def _asst_line(uuid, msg_id, req_id, text, *, session_id, ts,
               model="claude-opus-4-7", cwd=None, git_branch=None):
    obj = {
        "type": "assistant", "uuid": uuid, "sessionId": session_id,
        "requestId": req_id, "timestamp": ts,
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }
    if cwd is not None:
        obj["cwd"] = cwd
    if git_branch is not None:
        obj["gitBranch"] = git_branch
    return json.dumps(obj) + "\n"


def _user_line(uuid, text, *, session_id, ts, cwd=None, git_branch=None):
    obj = {
        "type": "user", "uuid": uuid, "sessionId": session_id, "timestamp": ts,
        "message": {"role": "user", "content": text},
    }
    if cwd is not None:
        obj["cwd"] = cwd
    if git_branch is not None:
        obj["gitBranch"] = git_branch
    return json.dumps(obj) + "\n"


# ---------------------------------------------------------------------------
# Invariant helpers (from the task spec).
# ---------------------------------------------------------------------------
def _live_aggregate(conn):
    return conn.execute(
        "SELECT session_id, COUNT(*), MIN(timestamp_utc), MAX(timestamp_utc) "
        "FROM conversation_messages WHERE session_id IS NOT NULL "
        "GROUP BY session_id ORDER BY session_id").fetchall()


def _rollup(conn):
    return conn.execute(
        "SELECT session_id, msg_count, started_utc, last_activity_utc "
        "FROM conversation_sessions ORDER BY session_id").fetchall()


def assert_rollup_matches_live(conn):
    assert _rollup(conn) == _live_aggregate(conn)


def _set_meta(conn, key, value):
    conn.execute("INSERT INTO cache_meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def _get_meta(conn, key):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _row_for(conn, session_id):
    return conn.execute(
        "SELECT session_id, msg_count, started_utc, last_activity_utc "
        "FROM conversation_sessions WHERE session_id=?", (session_id,)).fetchone()


def _enrichment_for(conn, session_id):
    """(git_branch, models_json, title, project_label, cost_usd) for a rollup row
    — the #302 materialized enrichment columns (+ the reused 015 ones)."""
    return conn.execute(
        "SELECT git_branch, models_json, title, project_label, cost_usd "
        "FROM conversation_sessions WHERE session_id=?", (session_id,)).fetchone()


def _sync(cache_mod, conversations, *, rebuild=False):
    """Advance compact accounting and transcript projections independently."""
    core = cache_mod.open_cache_db()
    try:
        cache_mod.sync_cache(core, rebuild=rebuild)
    finally:
        core.close()
    return cache_mod.sync_claude_conversations(
        conversations, rebuild=rebuild
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated split stores + an empty Claude projects dir. Returns
    (cache_mod, conn, projects). Each test writes its own JSONL into ``projects``
    then advances both independent syncs through ``_sync``."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_cache as cache_mod   # the module object load_script just loaded
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    conn = ns["open_conversations_db"]()
    yield cache_mod, conn, projects
    try:
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1. Fresh ingest — two sessions across two files.
# ---------------------------------------------------------------------------
def test_fresh_ingest_populates_rollup(env):
    cache_mod, conn, projects = env
    (projects / "a.jsonl").write_text(
        _asst_line("a1", "ma1", "ra1", "hi a", session_id="s1",
                   ts="2026-06-01T00:00:00Z")
        + _user_line("a2", "ping a", session_id="s1", ts="2026-06-01T00:01:00Z"))
    (projects / "b.jsonl").write_text(
        _asst_line("b1", "mb1", "rb1", "hi b", session_id="s2",
                   ts="2026-06-02T00:00:00Z"))
    _sync(cache_mod, conn)
    rollup = _rollup(conn)
    assert [r[0] for r in rollup] == ["s1", "s2"]
    assert _row_for(conn, "s1")[1] == 2          # two messages
    assert _row_for(conn, "s2")[1] == 1
    assert_rollup_matches_live(conn)


# ---------------------------------------------------------------------------
# 2. Incremental append touches only the appended session.
# ---------------------------------------------------------------------------
def test_incremental_append_updates_only_touched(env):
    cache_mod, conn, projects = env
    a = projects / "a.jsonl"
    b = projects / "b.jsonl"
    a.write_text(
        _asst_line("a1", "ma1", "ra1", "hi a", session_id="s1",
                   ts="2026-06-01T00:00:00Z"))
    b.write_text(
        _asst_line("b1", "mb1", "rb1", "hi b", session_id="s2",
                   ts="2026-06-02T00:00:00Z"))
    _sync(cache_mod, conn)
    s1_before = _row_for(conn, "s1")
    s2_before = _row_for(conn, "s2")
    assert s1_before[1] == 1

    # Append a NEW line to s1's file only.
    with open(a, "a") as fh:
        fh.write(_user_line("a2", "more", session_id="s1",
                            ts="2026-06-01T05:00:00Z"))
    _sync(cache_mod, conn)

    s1_after = _row_for(conn, "s1")
    s2_after = _row_for(conn, "s2")
    assert s1_after[1] == 2                              # advanced
    assert s1_after[3] == "2026-06-01T05:00:00Z"         # last_activity advanced
    assert s2_after == s2_before                         # untouched, byte-for-byte
    assert_rollup_matches_live(conn)


# ---------------------------------------------------------------------------
# 3. Rebuild repopulates with no stale rows.
# ---------------------------------------------------------------------------
def test_rebuild_repopulates_rollup(env):
    cache_mod, conn, projects = env
    a = projects / "a.jsonl"
    a.write_text(
        _asst_line("a1", "ma1", "ra1", "hi a", session_id="s1",
                   ts="2026-06-01T00:00:00Z"))
    (projects / "b.jsonl").write_text(
        _asst_line("b1", "mb1", "rb1", "hi b", session_id="s2",
                   ts="2026-06-02T00:00:00Z"))
    _sync(cache_mod, conn)
    # Seed a STALE rollup row for a session that no longer has any messages —
    # a rebuild must drop it.
    conn.execute(
        "INSERT INTO conversation_sessions "
        "(session_id, msg_count, started_utc, last_activity_utc) "
        "VALUES ('ghost', 99, '1999-01-01T00:00:00Z', '1999-01-01T00:00:00Z')")
    conn.commit()

    _sync(cache_mod, conn, rebuild=True)
    assert _row_for(conn, "ghost") is None              # stale row dropped
    assert {r[0] for r in _rollup(conn)} == {"s1", "s2"}
    assert _get_meta(conn, FLAG) is None                # flag cleared
    assert_rollup_matches_live(conn)


# ---------------------------------------------------------------------------
# 4. Truncation-escalation repopulates.
# ---------------------------------------------------------------------------
def test_truncation_escalation_repopulates(env):
    cache_mod, conn, projects = env
    a = projects / "a.jsonl"
    a.write_text(
        _asst_line("a1", "ma1", "ra1", "hi a", session_id="s1",
                   ts="2026-06-01T00:00:00Z")
        + _user_line("a2", "ping a", session_id="s1", ts="2026-06-01T00:01:00Z"))
    (projects / "b.jsonl").write_text(
        _asst_line("b1", "mb1", "rb1", "hi b", session_id="s2",
                   ts="2026-06-02T00:00:00Z"))
    _sync(cache_mod, conn)
    assert _row_for(conn, "s1")[1] == 2

    # Shrink a.jsonl on disk to a single (different) line -> truncation
    # escalation re-ingests EVERY file from offset 0.
    a.write_text(
        _asst_line("a1b", "ma1b", "ra1b", "shrunk", session_id="s1",
                   ts="2026-06-03T00:00:00Z"))
    _sync(cache_mod, conn)
    assert _row_for(conn, "s1")[1] == 1                 # only the shrunk line
    assert _get_meta(conn, FLAG) is None                # flag cleared
    assert_rollup_matches_live(conn)


# ---------------------------------------------------------------------------
# 5. Migration-013 flag triggers a full recompute over direct-seeded messages.
# ---------------------------------------------------------------------------
def test_migration013_flag_triggers_full_recompute(env):
    cache_mod, conn, projects = env
    # Seed conversation_messages DIRECTLY (simulating an existing install whose
    # history predates the rollup table) — no JSONL, no rollup yet. byte_offset
    # is globally unique under UNIQUE(source_path, byte_offset).
    for off, (sid, ts) in enumerate([("s1", "2026-06-01T00:00:00Z"),
                                     ("s1", "2026-06-01T00:05:00Z"),
                                     ("s2", "2026-06-02T00:00:00Z")]):
        conn.execute(
            "INSERT INTO conversation_messages "
            "(session_id, uuid, source_path, byte_offset, timestamp_utc, entry_type) "
            "VALUES (?,?,?,?,?,?)",
            (sid, f"{sid}-{off}", "seed.jsonl", off, ts, "human"))
    conn.commit()
    assert _rollup(conn) == []                           # rollup empty pre-flag
    _set_meta(conn, FLAG, "1")                           # migration 013 arms it

    _sync(cache_mod, conn)                           # consumes the flag
    assert {r[0] for r in _rollup(conn)} == {"s1", "s2"}
    assert _row_for(conn, "s1")[1] == 2
    assert _get_meta(conn, FLAG) is None                 # flag cleared
    assert_rollup_matches_live(conn)


# ---------------------------------------------------------------------------
# 6. Crash durability — flag survives, rollup is fully recomputed.
# ---------------------------------------------------------------------------
def test_crash_durability(env):
    cache_mod, conn, projects = env
    for off, (sid, ts) in enumerate([("s1", "2026-06-01T00:00:00Z"),
                                     ("s2", "2026-06-02T00:00:00Z"),
                                     ("s2", "2026-06-02T00:09:00Z")]):
        conn.execute(
            "INSERT INTO conversation_messages "
            "(session_id, uuid, source_path, byte_offset, timestamp_utc, entry_type) "
            "VALUES (?,?,?,?,?,?)",
            (sid, f"{sid}-{off}", "seed.jsonl", off, ts, "human"))
    conn.commit()
    # Simulate a crash-stranded state: the rollup was partially populated (here:
    # a wrong/stale row) but the durable flag is still set because the post-walk
    # recompute never ran. The next sync MUST full-recompute, not trust the
    # stranded rollup.
    conn.execute(
        "INSERT INTO conversation_sessions "
        "(session_id, msg_count, started_utc, last_activity_utc) "
        "VALUES ('s1', 1, 'WRONG', 'WRONG')")          # stale/partial row
    conn.commit()
    _set_meta(conn, FLAG, "1")

    _sync(cache_mod, conn)
    assert _row_for(conn, "s1")[1] == 1                  # recomputed, not stale
    assert _row_for(conn, "s1")[2] == "2026-06-01T00:00:00Z"
    assert _row_for(conn, "s2")[1] == 2
    assert _get_meta(conn, FLAG) is None
    assert_rollup_matches_live(conn)


# ---------------------------------------------------------------------------
# 7. Reingest recompute — drive _resumable_reingest_conversation_messages.
# ---------------------------------------------------------------------------
def test_reingest_recompute(env):
    cache_mod, conn, projects = env
    a = projects / "a.jsonl"
    a.write_text(
        _asst_line("a1", "ma1", "ra1", "hi a", session_id="s1",
                   ts="2026-06-01T00:00:00Z")
        + _user_line("a2", "ping a", session_id="s1", ts="2026-06-01T00:01:00Z"))
    (projects / "b.jsonl").write_text(
        _asst_line("b1", "mb1", "rb1", "hi b", session_id="s2",
                   ts="2026-06-02T00:00:00Z"))
    _sync(cache_mod, conn)
    assert_rollup_matches_live(conn)

    # Arm a reingest flag (the #179 path), then sync — sync_cache consumes the
    # reingest (DELETE + re-insert every file's messages) and arms the rollup
    # backfill flag, which the post-walk recompute then consumes.
    _set_meta(conn, "conversation_reingest_enrichment_pending", "1")
    _sync(cache_mod, conn)
    assert {r[0] for r in _rollup(conn)} == {"s1", "s2"}
    assert _get_meta(conn, FLAG) is None
    assert _get_meta(conn, "conversation_reingest_enrichment_pending") is None
    assert_rollup_matches_live(conn)


# ---------------------------------------------------------------------------
# 8. #302 — the fill materializes git_branch / models_json / title.
# ---------------------------------------------------------------------------
def test_fill_populates_enrichment_columns(env):
    cache_mod, conn, projects = env
    # s1: a human first-prompt then a multi-model main-session pair, on branch
    # "main" in /home/u/proj. s2: a human-only session (NO model -> models_json
    # must be NULL) with no git_branch.
    (projects / "a.jsonl").write_text(
        _user_line("h1", "first prompt hi", session_id="s1",
                   ts="2026-06-01T00:00:00Z", cwd="/home/u/proj", git_branch="main")
        + _asst_line("a1", "ma1", "ra1", "reply", session_id="s1",
                     ts="2026-06-01T00:01:00Z", model="claude-opus-4-8",
                     cwd="/home/u/proj", git_branch="main")
        + _asst_line("a2", "ma2", "ra2", "reply2", session_id="s1",
                     ts="2026-06-01T00:02:00Z", model="claude-haiku-4-5",
                     cwd="/home/u/proj", git_branch="main"))
    (projects / "b.jsonl").write_text(
        _user_line("h2", "just a question", session_id="s2",
                   ts="2026-06-02T00:00:00Z"))
    _sync(cache_mod, conn)

    branch, models_json, title, proj, cost = _enrichment_for(conn, "s1")
    assert branch == "main"
    # main-first ordering, alphabetical within the main group (both on the main
    # session): haiku < opus -> ["claude-haiku-4-5", "claude-opus-4-8"].
    assert json.loads(models_json) == ["claude-haiku-4-5", "claude-opus-4-8"]
    assert title == "first prompt hi"
    assert proj == "proj"
    assert cost > 0  # priced models -> non-zero materialized cost

    b_branch, b_models, b_title, b_proj, _ = _enrichment_for(conn, "s2")
    assert b_branch is None
    assert b_models is None, "empty-model session -> models_json NULL, not '[]'"
    assert b_title == "just a question"
    assert b_proj == ""  # no cwd -> _project_label(None) == '' sentinel
    assert_rollup_matches_live(conn)


# ---------------------------------------------------------------------------
# 9. #302 — a scoped append that changes git_branch / model REPLACES the stored
#    enrichment (not stale).
# ---------------------------------------------------------------------------
def test_scoped_append_replaces_stored_enrichment(env):
    cache_mod, conn, projects = env
    a = projects / "a.jsonl"
    a.write_text(
        _user_line("h1", "first prompt hi", session_id="s1",
                   ts="2026-06-01T00:00:00Z", cwd="/home/u/proj", git_branch="main")
        + _asst_line("a1", "ma1", "ra1", "reply", session_id="s1",
                     ts="2026-06-01T00:01:00Z", model="claude-opus-4-8",
                     cwd="/home/u/proj", git_branch="main"))
    _sync(cache_mod, conn)
    assert _enrichment_for(conn, "s1")[0] == "main"
    assert json.loads(_enrichment_for(conn, "s1")[1]) == ["claude-opus-4-8"]

    # Append a later row on a DIFFERENT branch + a new model -> scoped recompute
    # of s1 must REPLACE the stored branch (latest non-null) and grow the model
    # set. The title (stable first-prompt) is unchanged.
    with open(a, "a") as fh:
        fh.write(_asst_line("a2", "ma2", "ra2", "later", session_id="s1",
                            ts="2026-06-01T05:00:00Z", model="claude-haiku-4-5",
                            cwd="/home/u/proj", git_branch="feature-x"))
    _sync(cache_mod, conn)

    branch, models_json, title, _, _ = _enrichment_for(conn, "s1")
    assert branch == "feature-x", "stored branch must be the latest non-null"
    assert json.loads(models_json) == ["claude-haiku-4-5", "claude-opus-4-8"]
    assert title == "first prompt hi", "stable first-prompt title unchanged"
    assert_rollup_matches_live(conn)


# ---------------------------------------------------------------------------
# 10. #302 — a flag-driven FULL backfill replaces stale enrichment on an
#     existing row (the migration-023 path: columns present but unfilled/stale).
# ---------------------------------------------------------------------------
def test_full_backfill_replaces_stale_enrichment(env):
    cache_mod, conn, projects = env
    (projects / "a.jsonl").write_text(
        _user_line("h1", "real title", session_id="s1",
                   ts="2026-06-01T00:00:00Z", cwd="/home/u/proj", git_branch="main")
        + _asst_line("a1", "ma1", "ra1", "reply", session_id="s1",
                     ts="2026-06-01T00:01:00Z", model="claude-opus-4-8",
                     cwd="/home/u/proj", git_branch="main"))
    _sync(cache_mod, conn)
    # Corrupt the stored enrichment in place, then arm the backfill flag (023's
    # arm): the next sync full-recomputes and must OVERWRITE the stale values.
    conn.execute(
        "UPDATE conversation_sessions SET git_branch='STALE', "
        "models_json='[\"STALE\"]', title='STALE' WHERE session_id='s1'")
    conn.commit()
    _set_meta(conn, FLAG, "1")

    _sync(cache_mod, conn)
    branch, models_json, title, _, _ = _enrichment_for(conn, "s1")
    assert branch == "main"
    assert json.loads(models_json) == ["claude-opus-4-8"]
    assert title == "real title"
    assert _get_meta(conn, FLAG) is None
    assert_rollup_matches_live(conn)
