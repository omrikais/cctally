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

from conftest import (  # type: ignore
    load_script,
    redirect_paths,
    rollup_fresh_mismatches,
)

FLAG = "conversation_sessions_backfill_pending"


# ---------------------------------------------------------------------------
# Synthetic Claude JSONL lines (same shape the #179 reingest test seeds with).
# ---------------------------------------------------------------------------
def _asst_line(uuid, msg_id, req_id, text, *, session_id, ts,
               model="claude-opus-4-7", cwd=None, git_branch=None, out=5):
    obj = {
        "type": "assistant", "uuid": uuid, "sessionId": session_id,
        "requestId": req_id, "timestamp": ts,
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": out,
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


# ---------------------------------------------------------------------------
# #1072: the persisted rollup equals a fresh derivation after accounting-only
# changes, accounting deletes and an interrupted transcript pass. Each failure
# is a stale unscoped Viewer value: the cost column, the cost filter and sort,
# the facets, the detail summary and the cache-rebuild count all read the row.
# ---------------------------------------------------------------------------
def _accounting_generation(core):
    row = core.execute("SELECT value FROM cache_meta "
                       "WHERE key='claude_accounting_generation'").fetchone()
    return row[0] if row else None


def _sync_accounting(cache_mod, *, rebuild=False):
    core = cache_mod.open_cache_db()
    try:
        return cache_mod.sync_cache(core, rebuild=rebuild)
    finally:
        core.close()


@pytest.mark.parametrize("shape", ["late_accounting", "shared_key_elsewhere"])
def test_accounting_only_update_rederives_the_session(env, shape):
    """Input path 1: an accounting change after the session's last transcript
    touch, either priced after the transcript pass read the line or arriving
    through another session's file that holds the same turn key."""
    cache_mod, conn, projects = env
    a = projects / "a.jsonl"
    a.write_text(
        _user_line("a0", "hi", session_id="s1", ts="2026-06-01T00:00:00Z")
        + _asst_line("a1", "m1", "r1", "partial", session_id="s1",
                     ts="2026-06-01T00:00:01Z", out=5))
    _sync(cache_mod, conn)
    assert rollup_fresh_mismatches(conn) == []
    before = _enrichment_for(conn, "s1")[4]
    if shape == "late_accounting":
        with a.open("a") as fh:
            fh.write(_asst_line("a2", "m1", "r1", "final", session_id="s1",
                                ts="2026-06-01T00:00:02Z", out=5000))
        cache_mod.sync_claude_conversations(conn)
        _sync_accounting(cache_mod)
    else:
        (projects / "b.jsonl").write_text(
            _asst_line("b1", "m1", "r1", "final", session_id="s2",
                       ts="2026-06-01T00:00:02Z", out=5000))
        _sync(cache_mod, conn)
    cache_mod.sync_claude_conversations(conn)
    assert rollup_fresh_mismatches(conn) == []
    assert _enrichment_for(conn, "s1")[4] > before


@pytest.mark.parametrize(
    "delete", ["per_file_delete", "rebuild", "truncation_escalation"])
def test_accounting_delete_rederives_sessions_that_lost_rows(env, delete):
    """Input paths 2 and 3: accounting rows deleted under a session whose
    transcript the conversation store retains (its file left the disk), by the
    per-file re-ingest DELETE statement or by a full wipe and re-ingest."""
    cache_mod, conn, projects = env
    gone = projects / "gone.jsonl"
    gone.write_text(_asst_line("g1", "mg", "rg", "x", session_id="sg",
                               ts="2026-06-01T00:00:00Z", out=50))
    keep = projects / "keep.jsonl"
    keep.write_text(
        _asst_line("k1", "mk1", "rk1", "y", session_id="sk",
                   ts="2026-06-01T00:01:00Z")
        + _asst_line("k2", "mk2", "rk2", "z", session_id="sk",
                     ts="2026-06-01T00:02:00Z"))
    _sync(cache_mod, conn)
    assert rollup_fresh_mismatches(conn) == []
    assert _enrichment_for(conn, "sg")[4] > 0
    generation_before = _store_generation(cache_mod)
    gone.unlink()
    if delete == "per_file_delete":
        core = cache_mod.open_cache_db()
        try:
            before_gen = _accounting_generation(core)
            assert cache_mod._delete_claude_session_entries(
                core, "source_path = ?", (str(gone),)) == 1
            core.commit()
            assert _accounting_generation(core) != before_gen
        finally:
            core.close()
    elif delete == "rebuild":
        _sync_accounting(cache_mod, rebuild=True)
    else:
        keep.write_text(_asst_line("k1", "mk1", "rk1", "y", session_id="sk",
                                   ts="2026-06-01T00:01:00Z"))
        _sync_accounting(cache_mod)
    assert _store_generation(cache_mod) != generation_before
    cache_mod.sync_claude_conversations(conn)
    assert rollup_fresh_mismatches(conn) == []
    assert _enrichment_for(conn, "sg")[4] == 0


@pytest.mark.parametrize(
    "shape", ["append", "replaced_by_other_session", "deleted_without_insert"])
def test_interrupted_transcript_pass_is_recovered(env, monkeypatch, shape):
    """Input path 5: the writer dies after committing its transcript batch and
    before the rollup step, so the next pass, which skips the unchanged files,
    must still re-derive the sessions that batch touched. In the two
    replacement shapes the batch DELETED a session's messages (the file now
    holds another session, or nothing), which no message-id mark can see:
    without the durable pending set the deleted session's row survives in the
    unscoped Viewer with its old cost and message count."""
    cache_mod, conn, projects = env
    a = projects / "a.jsonl"
    a.write_text(_user_line("a0", "hi", session_id="s1",
                            ts="2026-06-01T00:00:00Z"))
    _sync(cache_mod, conn)
    if shape == "append":
        with a.open("a") as fh:
            fh.write(_asst_line("a1", "m1", "r1", "more", session_id="s1",
                                ts="2026-06-01T00:00:05Z"))
        (projects / "b.jsonl").write_text(
            _asst_line("b1", "m2", "r2", "new", session_id="s2",
                       ts="2026-06-01T00:01:00Z"))
        _sync_accounting(cache_mod)
    elif shape == "replaced_by_other_session":
        a.write_text(
            _user_line("c0", "other session replaces the file", session_id="s3",
                       ts="2026-06-02T00:00:00Z")
            + _asst_line("c1", "m3", "r3", "reply", session_id="s3",
                         ts="2026-06-02T00:00:05Z"))
    else:
        a.write_text("")
    real = cache_mod._arm_rollup_backfill_on_pricing_change

    def die(_conn):
        raise RuntimeError("writer died before the rollup step")

    monkeypatch.setattr(cache_mod, "_arm_rollup_backfill_on_pricing_change", die)
    with pytest.raises(RuntimeError):
        cache_mod.sync_claude_conversations(conn)
    conn.rollback()  # a dead process's open transaction never commits
    monkeypatch.setattr(cache_mod, "_arm_rollup_backfill_on_pricing_change", real)
    assert rollup_fresh_mismatches(conn) != []
    if shape != "append":
        assert "s1" in json.loads(_get_meta(conn, PENDING))["sessions"]
    cache_mod.sync_claude_conversations(conn)
    assert rollup_fresh_mismatches(conn) == []
    assert _get_meta(conn, PENDING) is None


# ---------------------------------------------------------------------------
# #1072 shared helpers: the five conversations.db keys and the cache.db
# accounting version.
# ---------------------------------------------------------------------------
WATERMARK = "conversation_sessions_input_watermark"
STAGED_WATERMARK = "conversation_sessions_staging_input_watermark"
CURSOR = "conversation_sessions_rederive_cursor"
PENDING = "conversation_sessions_rederive_pending"
GENERATION = "claude_accounting_generation"


def _watermark(conn):
    raw = _get_meta(conn, WATERMARK)
    return json.loads(raw) if raw is not None else None


def _cursor(conn):
    raw = _get_meta(conn, CURSOR)
    return json.loads(raw) if raw is not None else None


def _cache_exec(cache_mod, sql, params=()):
    """One committed statement on cache.db through a raw connection (no opener
    side effects, no version stamp: what an older binary or a hand edit
    leaves)."""
    import _cctally_core as core_mod
    raw = sqlite3.connect(str(core_mod.CACHE_DB_PATH), timeout=5.0)
    try:
        raw.execute(sql, params)
        raw.commit()
    finally:
        raw.close()


def _store_generation(cache_mod):
    import _cctally_core as core_mod
    raw = sqlite3.connect(str(core_mod.CACHE_DB_PATH), timeout=5.0)
    try:
        return _accounting_generation(raw)
    finally:
        raw.close()


def _rotate_generation(cache_mod):
    import uuid
    _cache_exec(cache_mod,
                "INSERT INTO cache_meta(key,value) VALUES(?,?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                (GENERATION, uuid.uuid4().hex))


def _silently_reprice(cache_mod, msg_id, out):
    """An accounting change no version records, so only a full traversal can
    repair the session that prices it."""
    _cache_exec(cache_mod,
                "UPDATE session_entries SET output_tokens=? WHERE msg_id=?",
                (out, msg_id))


def _spy_traversals(cache_mod, monkeypatch):
    calls = []
    real = cache_mod._run_rollup_traversal

    def spy(conn, target, **kwargs):
        calls.append(target)
        return real(conn, target, **kwargs)

    monkeypatch.setattr(cache_mod, "_run_rollup_traversal", spy)
    return calls


def _two_session_store(cache_mod, conn, projects):
    a, b = projects / "a.jsonl", projects / "b.jsonl"
    a.write_text(_user_line("a0", "hi", session_id="s1", ts="2026-06-01T00:00:00Z")
                 + _asst_line("a1", "m1", "r1", "x", session_id="s1",
                              ts="2026-06-01T00:00:01Z", out=50))
    b.write_text(_user_line("b0", "yo", session_id="s2", ts="2026-06-01T01:00:00Z")
                 + _asst_line("b1", "m2", "r2", "y", session_id="s2",
                              ts="2026-06-01T01:00:01Z", out=5))
    _sync(cache_mod, conn)
    assert rollup_fresh_mismatches(conn) == []
    return a, b


def _late_accounting_store(cache_mod, conn, projects):
    """T1's stale shape: s1's finalized line was read by the transcript pass
    before accounting priced it."""
    a, b = _two_session_store(cache_mod, conn, projects)
    with a.open("a") as fh:
        fh.write(_asst_line("a2", "m1", "r1", "final", session_id="s1",
                            ts="2026-06-01T00:00:02Z", out=5000))
    cache_mod.sync_claude_conversations(conn)
    _sync_accounting(cache_mod)
    return a, b


# ---------------------------------------------------------------------------
# T-A: every runtime delete of session_entries rotates the generation.
# ---------------------------------------------------------------------------
def test_every_session_entries_delete_rotates_generation(env):
    """A runtime delete that does not rotate the accounting generation is
    invisible to the rollup consumer, so the sessions that priced the deleted
    rows keep their old cost forever. Drives the per-file helper, the rebuild
    wipe, the truncation-escalation wipe and the orphan prune."""
    cache_mod, conn, projects = env
    gone = projects / "gone.jsonl"
    gone.write_text(_asst_line("g1", "mg", "rg", "x", session_id="sg",
                               ts="2026-06-01T00:00:00Z", out=50))
    keep = projects / "keep.jsonl"
    keep.write_text(
        _asst_line("k1", "mk1", "rk1", "y", session_id="sk",
                   ts="2026-06-01T00:01:00Z")
        + _asst_line("k2", "mk2", "rk2", "z", session_id="sk",
                     ts="2026-06-01T00:02:00Z"))
    _sync(cache_mod, conn)
    seen = [_store_generation(cache_mod)]
    assert seen[0] is not None
    core = cache_mod.open_cache_db()
    try:
        assert cache_mod._delete_claude_session_entries(
            core, "source_path = ?", (str(keep),)) == 2
        core.commit()
    finally:
        core.close()
    seen.append(_store_generation(cache_mod))
    _sync_accounting(cache_mod, rebuild=True)
    seen.append(_store_generation(cache_mod))
    keep.write_text(_asst_line("k1", "mk1", "rk1", "y", session_id="sk",
                               ts="2026-06-01T00:01:00Z"))
    _sync_accounting(cache_mod)
    seen.append(_store_generation(cache_mod))
    gone.unlink()
    core = cache_mod.open_cache_db()
    try:
        res = cache_mod._prune_orphaned_cache_entries(core, lock_timeout=None)
    finally:
        core.close()
    assert res.pruned_files == 1 and res.pruned_entries == 1
    seen.append(_store_generation(cache_mod))
    assert len(set(seen)) == len(seen), seen


# ---------------------------------------------------------------------------
# T5: the unscoped user-facing results equal a fresh derivation.
# ---------------------------------------------------------------------------
def test_unscoped_results_equal_fresh_derivation(env):
    """The cost column, cost sort, cost filter, facets and detail summary of
    the unscoped Viewer read the persisted rollup; after an accounting-only
    change they must equal what a forced full re-derive of the same store
    shows, or the browse row and the detail header disagree."""
    import _lib_conversation_query as q
    cache_mod, conn, projects = env
    _late_accounting_store(cache_mod, conn, projects)
    cache_mod.sync_claude_conversations(conn)
    costs = {sid: _enrichment_for(conn, sid)[4] for sid in ("s1", "s2")}
    assert costs["s1"] > costs["s2"]
    threshold = (costs["s1"] + costs["s2"]) / 2

    def observed():
        return (
            q.list_conversations(conn, sort="cost"),
            q.list_conversations(conn, cost_min=threshold),
            q.list_conversation_facets(conn),
            q.get_conversation(conn, "s1")["cost_usd"],
        )

    fixed = observed()
    assert [row["session_id"] for row in fixed[1]["conversations"]] == ["s1"]
    _set_meta(conn, FLAG, "1")
    cache_mod.sync_claude_conversations(conn)
    assert _get_meta(conn, FLAG) is None
    assert observed() == fixed


# ---------------------------------------------------------------------------
# T6: the watermark lifecycle.
# ---------------------------------------------------------------------------
def test_upgrade_without_generation_converges_in_one_conversation_pass(env):
    """An upgraded store has neither the accounting generation nor a
    watermark; the first conversation pass, with no sync_cache before it,
    must mint the generation itself and converge the stale rows rather than
    wait for an accounting sync that may never come."""
    cache_mod, conn, projects = env
    _late_accounting_store(cache_mod, conn, projects)
    _cache_exec(cache_mod, "DELETE FROM cache_meta WHERE key=?", (GENERATION,))
    conn.execute("DELETE FROM cache_meta WHERE key=?", (WATERMARK,))
    conn.commit()
    assert rollup_fresh_mismatches(conn) != []
    cache_mod.sync_claude_conversations(conn)
    assert rollup_fresh_mismatches(conn) == []
    generation = _store_generation(cache_mod)
    assert generation is not None
    assert _watermark(conn)["generation"] == generation


def test_mint_failure_defers_explicitly(env, monkeypatch):
    """A pass that cannot mint the generation must say so: a silent fallback
    to the touched-only recompute would be certified as caught up while the
    rows stay stale."""
    import _lib_ingest_frontier as frontier
    cache_mod, conn, projects = env
    _late_accounting_store(cache_mod, conn, projects)
    _cache_exec(cache_mod, "DELETE FROM cache_meta WHERE key=?", (GENERATION,))
    conn.execute("DELETE FROM cache_meta WHERE key=?", (WATERMARK,))
    conn.commit()
    real = cache_mod._mint_claude_accounting_generation
    monkeypatch.setattr(cache_mod, "_mint_claude_accounting_generation",
                        lambda: False)
    stats = cache_mod.sync_claude_conversations(conn)
    assert stats.deferred_reason == "accounting_generation_unavailable"
    assert frontier.conversation_sync_certifiable("full", stats) is False
    assert _watermark(conn) is None
    monkeypatch.setattr(cache_mod, "_mint_claude_accounting_generation", real)
    stats = cache_mod.sync_claude_conversations(conn)
    assert stats.deferred_reason is None
    assert rollup_fresh_mismatches(conn) == []


@pytest.mark.parametrize("discontinuity", [
    "generation_rotated", "malformed_watermark", "pending_full",
    "seq_regressed", "digest_changed"])
def test_discontinuity_forces_a_traversal(env, monkeypatch, discontinuity):
    """Each state the consumer cannot attribute to sessions must run a full
    traversal; an incremental step would leave the silently repriced session
    stale."""
    cache_mod, conn, projects = env
    _two_session_store(cache_mod, conn, projects)
    _silently_reprice(cache_mod, "m1", 7000)
    wm = _watermark(conn)
    if discontinuity == "generation_rotated":
        _rotate_generation(cache_mod)
    elif discontinuity == "malformed_watermark":
        _set_meta(conn, WATERMARK, "{not json")
    elif discontinuity == "pending_full":
        _set_meta(conn, PENDING, json.dumps({"v": 1, "full": True}))
    elif discontinuity == "seq_regressed":
        _set_meta(conn, WATERMARK, json.dumps({**wm, "seq": wm["seq"] + 10**6}))
    else:
        _set_meta(conn, WATERMARK, json.dumps({**wm, "migrations": []}))
    assert rollup_fresh_mismatches(conn) != []
    traversals = _spy_traversals(cache_mod, monkeypatch)
    stats = cache_mod.sync_claude_conversations(conn)
    assert stats.deferred_reason is None
    assert len(traversals) == 1
    assert rollup_fresh_mismatches(conn) == []
    assert _cursor(conn) is None and _get_meta(conn, PENDING) is None


def test_unskipped_008_converges(env, monkeypatch):
    """A cost-relevant cache migration that runs after the rollup consumed the
    store (db skip, then db unskip) changes session_entries with no stamp or
    rotation; the migration digest must force a traversal both when it
    disappears and when it returns."""
    cache_mod, conn, projects = env
    _two_session_store(cache_mod, conn, projects)
    name = "008_session_entries_speed_backfill"
    core = cache_mod.open_cache_db()
    try:
        applied = core.execute(
            "SELECT applied_at_utc FROM schema_migrations WHERE name=?",
            (name,)).fetchone()
        assert applied is not None
    finally:
        core.close()
    traversals = _spy_traversals(cache_mod, monkeypatch)
    _cache_exec(cache_mod, "DELETE FROM schema_migrations WHERE name=?", (name,))
    _silently_reprice(cache_mod, "m1", 7000)
    cache_mod.sync_claude_conversations(conn)
    assert len(traversals) == 1 and rollup_fresh_mismatches(conn) == []
    assert name not in _watermark(conn)["migrations"]
    _cache_exec(cache_mod,
                "INSERT INTO schema_migrations(name, applied_at_utc) VALUES(?,?)",
                (name, applied[0]))
    _silently_reprice(cache_mod, "m2", 9000)
    cache_mod.sync_claude_conversations(conn)
    assert len(traversals) == 2 and rollup_fresh_mismatches(conn) == []
    assert name in _watermark(conn)["migrations"]


def test_refused_recompute_advances_nothing(env, monkeypatch):
    """A refused re-derive (#705) must not acknowledge the inputs it did not
    derive: a watermark, cursor or pending set that moved past a refused chunk
    would make the next authorized process skip those sessions."""
    cache_mod, conn, projects = env
    a, _b = _two_session_store(cache_mod, conn, projects)
    with a.open("a") as fh:
        fh.write(_asst_line("a2", "m1", "r1", "final", session_id="s1",
                            ts="2026-06-01T00:00:02Z", out=5000))
    _sync_accounting(cache_mod)
    _set_meta(conn, PENDING, json.dumps({"v": 1, "sessions": ["s2"]}))
    before = (_get_meta(conn, WATERMARK), _get_meta(conn, PENDING))
    monkeypatch.setattr(cache_mod, "_recompute_conversation_sessions",
                        lambda *args, **kwargs: False)
    stats = cache_mod.sync_claude_conversations(conn)
    assert stats.deferred_reason == "pricing_write_refused"
    assert (_get_meta(conn, WATERMARK), _get_meta(conn, PENDING)) == before
    assert _cursor(conn) is None
    _rotate_generation(cache_mod)
    stats = cache_mod.sync_claude_conversations(conn)
    assert stats.deferred_reason == "pricing_write_refused"
    assert _get_meta(conn, WATERMARK) == before[0]
    assert _cursor(conn)["after"] == ""


def test_rebuild_publishes_staged_watermark(env):
    """A transcript rebuild re-derives everything into staging; its publish
    must carry the input version that generation consumed and retire the
    previous generation's cursor and pending set, or the next pass re-runs a
    traversal (or skips one it needs)."""
    cache_mod, conn, projects = env
    _two_session_store(cache_mod, conn, projects)
    _set_meta(conn, PENDING, json.dumps({"v": 1, "sessions": ["s1"]}))
    _set_meta(conn, CURSOR, json.dumps({"v": 1, "after": "s1"}))
    cache_mod.sync_claude_conversations(conn, rebuild=True)
    state = cache_mod._rollup_input_state(conn)
    assert _watermark(conn) == state.as_json_obj()
    assert _get_meta(conn, STAGED_WATERMARK) is None
    assert _cursor(conn) is None and _get_meta(conn, PENDING) is None
    assert rollup_fresh_mismatches(conn) == []


def test_targeted_pass_consumes_accounting_changes(env):
    """The dirty set is global: a live-tail (targeted) pass over an unrelated
    file must still re-derive a session whose accounting finalized, or the
    Viewer stays stale for as long as only targeted passes run."""
    cache_mod, conn, projects = env
    _a, b = _late_accounting_store(cache_mod, conn, projects)
    assert rollup_fresh_mismatches(conn) != []
    stats = cache_mod.sync_claude_conversations(conn, only_paths={str(b)})
    assert stats.deferred_reason is None
    assert rollup_fresh_mismatches(conn) == []
    assert _watermark(conn)["seq"] == cache_mod._rollup_input_state(conn).seq


def test_targeted_pass_defers_a_traversal(env):
    """A targeted pass may not run a traversal, so after a rotation it must
    leave the traversal Start and report itself incomplete; certified as
    complete it would leave the rows stale under a caught-up frontier."""
    import _lib_ingest_frontier as frontier
    cache_mod, conn, projects = env
    _a, b = _two_session_store(cache_mod, conn, projects)
    before = _get_meta(conn, WATERMARK)
    _rotate_generation(cache_mod)
    stats = cache_mod.sync_claude_conversations(conn, only_paths={str(b)})
    assert stats.deferred_reason == "rollup_traversal_pending"
    assert frontier.conversation_sync_certifiable(
        "targeted", stats, expected_paths=1) is False
    assert _cursor(conn)["after"] == ""
    assert _get_meta(conn, WATERMARK) == before


def test_change_between_state_and_changeset_reads_is_not_lost(env, monkeypatch):
    """An accounting change committed after the consumer read the input state
    but before it read the change set must be re-derived now or by the next
    pass; acknowledging a state read after the change set would lose it."""
    cache_mod, conn, projects = env
    _a, b = _two_session_store(cache_mod, conn, projects)
    with b.open("a") as fh:
        fh.write(_user_line("b2", "more", session_id="s2",
                            ts="2026-06-01T01:00:05Z"))
    real = cache_mod._rollup_dirty_sessions
    bumped = {}

    def interleave(conn_, wm, cur, touched):
        import _cctally_core as core_mod
        raw = sqlite3.connect(str(core_mod.CACHE_DB_PATH), timeout=5.0)
        try:
            bumped["seq"] = cache_mod._bump_mutation_seq(raw)
            raw.execute("UPDATE session_entries SET output_tokens=6000, "
                        "mutation_seq=? WHERE msg_id='m1'", (bumped["seq"],))
            raw.commit()
        finally:
            raw.close()
        return real(conn_, wm, cur, touched)

    monkeypatch.setattr(cache_mod, "_rollup_dirty_sessions", interleave)
    cache_mod.sync_claude_conversations(conn)
    monkeypatch.setattr(cache_mod, "_rollup_dirty_sessions", real)
    assert _watermark(conn)["seq"] < bumped["seq"]
    cache_mod.sync_claude_conversations(conn)
    assert rollup_fresh_mismatches(conn) == []


# ---------------------------------------------------------------------------
# T7: the chunked, resumable traversal (chunk size 2, a death after the first
# chunk commits).
# ---------------------------------------------------------------------------
def _five_session_store(cache_mod, conn, projects, monkeypatch):
    monkeypatch.setattr(cache_mod, "_ROLLUP_CHUNK_SESSIONS", 2)
    for n in range(1, 6):
        (projects / f"f{n}.jsonl").write_text(
            _user_line(f"u{n}", "hi", session_id=f"s{n}",
                       ts=f"2026-06-0{n}T00:00:00Z")
            + _asst_line(f"a{n}", f"m{n}", f"r{n}", "x", session_id=f"s{n}",
                         ts=f"2026-06-0{n}T00:00:01Z", out=10 * n))
    _sync(cache_mod, conn)
    assert rollup_fresh_mismatches(conn) == []


def _chunk_afters(cache_mod, monkeypatch, *, die_on=None, before_call=None):
    """Record each traversal chunk's ``after``; optionally die on, or run a
    side effect before, the N-th chunk read (1-based)."""
    real = cache_mod._rollup_next_chunk
    afters = []

    def stand_in(conn, after):
        afters.append(after)
        if before_call is not None and len(afters) == before_call[0]:
            before_call[1]()
        if die_on is not None and len(afters) == die_on:
            raise RuntimeError("died after the first chunk committed")
        return real(conn, after)

    monkeypatch.setattr(cache_mod, "_rollup_next_chunk", stand_in)
    return afters, real


def _interrupted_traversal(cache_mod, conn, projects, monkeypatch):
    _five_session_store(cache_mod, conn, projects, monkeypatch)
    old_generation = _watermark(conn)["generation"]
    _rotate_generation(cache_mod)
    _silently_reprice(cache_mod, "m4", 4444)
    _afters, real = _chunk_afters(cache_mod, monkeypatch, die_on=2)
    with pytest.raises(RuntimeError):
        cache_mod.sync_claude_conversations(conn)
    conn.rollback()
    monkeypatch.setattr(cache_mod, "_rollup_next_chunk", real)
    assert _watermark(conn)["generation"] == old_generation
    assert _cursor(conn)["after"] == "s2"
    return old_generation


def test_traversal_resumes_after_interruption(env, monkeypatch):
    """A traversal that dies mid-way must resume from its cursor rather than
    restart (a restart per death never finishes on a large store) and must not
    acknowledge anything until it completes."""
    cache_mod, conn, projects = env
    _interrupted_traversal(cache_mod, conn, projects, monkeypatch)
    assert rollup_fresh_mismatches(conn) != []
    afters, _real = _chunk_afters(cache_mod, monkeypatch)
    cache_mod.sync_claude_conversations(conn)
    assert afters[0] == "s2"
    assert rollup_fresh_mismatches(conn) == []
    assert _cursor(conn) is None
    assert _watermark(conn)["generation"] == _store_generation(cache_mod)


def test_pending_written_between_traversal_passes_is_rederived(env, monkeypatch):
    """N1: a deletion recorded between the passes of an interrupted traversal,
    for a session the traversal already passed, must survive the traversal's
    completion and be re-derived by the incremental step."""
    cache_mod, conn, projects = env
    _interrupted_traversal(cache_mod, conn, projects, monkeypatch)
    conn.execute("DELETE FROM conversation_file_touches WHERE message_id IN "
                 "(SELECT id FROM conversation_messages WHERE session_id='s1')")
    conn.execute("DELETE FROM conversation_messages WHERE session_id='s1'")
    cache_mod._merge_rollup_pending(conn, {"s1"})
    conn.commit()
    cache_mod.sync_claude_conversations(conn)
    assert rollup_fresh_mismatches(conn) == []
    assert _row_for(conn, "s1") is None
    assert _get_meta(conn, PENDING) is None


def test_cursor_with_other_pricing_restarts(env, monkeypatch):
    """N2: a cursor left by a process with another pricing table must restart,
    or one traversal's rows mix two pricing tables and the backfill flag is
    cleared over the mixture."""
    cache_mod, conn, projects = env
    _interrupted_traversal(cache_mod, conn, projects, monkeypatch)
    _set_meta(conn, CURSOR, json.dumps({**_cursor(conn), "pricing": "1999-01-01"}))
    afters, _real = _chunk_afters(cache_mod, monkeypatch)
    cache_mod.sync_claude_conversations(conn)
    assert afters[0] == ""
    assert rollup_fresh_mismatches(conn) == []


def test_generation_rotated_mid_traversal_forces_second_traversal(env, monkeypatch):
    """N3: a generation rotated after the first chunk committed means that
    chunk's rows may price deleted accounting; the pass must run a second
    traversal before any watermark carries the new generation."""
    cache_mod, conn, projects = env
    _five_session_store(cache_mod, conn, projects, monkeypatch)
    _rotate_generation(cache_mod)

    def delete_under_the_first_chunk():
        _cache_exec(cache_mod, "DELETE FROM session_entries WHERE msg_id='m1'")
        _rotate_generation(cache_mod)

    traversals = _spy_traversals(cache_mod, monkeypatch)
    _chunk_afters(cache_mod, monkeypatch,
                  before_call=(2, delete_under_the_first_chunk))
    stats = cache_mod.sync_claude_conversations(conn)
    assert stats.deferred_reason is None
    assert len(traversals) == 2
    assert rollup_fresh_mismatches(conn) == []
    assert _watermark(conn)["generation"] == _store_generation(cache_mod)


def test_rederive_chunks_are_bounded_by_message_volume(env, monkeypatch):
    """Every re-derive transaction holds the conversations write lock, which
    the Codex conversation writer waits on with a 15 s busy timeout: past its
    first session a chunk must stop at the message bound, or one traversal
    chunk over large sessions holds the lock for many seconds (8.3 s measured
    on the production copy at the original 100,000 bound)."""
    cache_mod, conn, projects = env
    _five_session_store(cache_mod, conn, projects, monkeypatch)
    monkeypatch.setattr(cache_mod, "_ROLLUP_CHUNK_SESSIONS", 50)
    ids = ["s1", "s2", "s3", "s4", "s5"]
    per = cache_mod._session_message_count(conn, "s1")
    assert per > 0
    assert all(cache_mod._session_message_count(conn, s) == per for s in ids)
    monkeypatch.setattr(cache_mod, "_ROLLUP_CHUNK_MESSAGES", 2 * per)
    assert cache_mod._split_by_volume(conn, ids) == [
        ["s1", "s2"], ["s3", "s4"], ["s5"]]
    # A session larger than the bound is still re-derived, alone.
    monkeypatch.setattr(cache_mod, "_ROLLUP_CHUNK_MESSAGES", 1)
    assert cache_mod._split_by_volume(conn, ids) == [[s] for s in ids]
    monkeypatch.setattr(cache_mod, "_ROLLUP_CHUNK_MESSAGES", 2 * per)
    _rotate_generation(cache_mod)
    _silently_reprice(cache_mod, "m5", 5555)
    afters, _real = _chunk_afters(cache_mod, monkeypatch)
    cache_mod.sync_claude_conversations(conn)
    assert afters == ["", "s2", "s4"]
    assert rollup_fresh_mismatches(conn) == []


def test_traversal_derives_each_chunk_outside_the_write_lock(env, monkeypatch):
    """A traversal reads every session cold, and a cold derivation under the
    conversations write lock is long (one 38,146-message session held it
    5.2 s on the production copy). Each chunk must first be derived where no
    store lock is held, so another writer can take the write lock, with no
    busy wait at all, while that derivation runs."""
    import _cctally_core as core_mod
    cache_mod, conn, projects = env
    _five_session_store(cache_mod, conn, projects, monkeypatch)
    real = cache_mod._recompute_conversation_sessions
    prewarmed = []

    def probe(conn_, session_ids=None, **kw):
        out = real(conn_, session_ids, **kw)
        if kw.get("target") == getattr(cache_mod, "_ROLLUP_PREWARM_TABLE", None):
            other = sqlite3.connect(str(core_mod.CONVERSATIONS_DB_PATH),
                                    timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
            finally:
                other.close()
            prewarmed.append(list(session_ids))
        return out

    monkeypatch.setattr(cache_mod, "_recompute_conversation_sessions", probe)
    _rotate_generation(cache_mod)
    _silently_reprice(cache_mod, "m3", 3333)
    cache_mod.sync_claude_conversations(conn)
    assert prewarmed == [["s1", "s2"], ["s3", "s4"], ["s5"]]
    assert rollup_fresh_mismatches(conn) == []


def test_backfill_flag_stays_until_traversal_completes(env, monkeypatch):
    """The pricing backfill now runs as the chunked traversal; its flag must
    stay armed until the completing chunk, or readers leave their live
    fallback over a half-derived rollup."""
    cache_mod, conn, projects = env
    _five_session_store(cache_mod, conn, projects, monkeypatch)
    _set_meta(conn, FLAG, "1")
    _afters, real = _chunk_afters(cache_mod, monkeypatch, die_on=2)
    with pytest.raises(RuntimeError):
        cache_mod.sync_claude_conversations(conn)
    conn.rollback()
    monkeypatch.setattr(cache_mod, "_rollup_next_chunk", real)
    assert _get_meta(conn, FLAG) == "1"
    assert _cursor(conn)["after"] == "s2"
    cache_mod.sync_claude_conversations(conn)
    assert _get_meta(conn, FLAG) is None
    assert _cursor(conn) is None
    assert _watermark(conn) is not None
    assert rollup_fresh_mismatches(conn) == []


# ---------------------------------------------------------------------------
# #1072 fix round: arming, allowances, bounds, yields, deferrals, prewarm,
# the marker Start, the chunked rebuild staging and the promote consumer.
# ---------------------------------------------------------------------------
def _stamp_accounting_change(cache_mod, msg_ids, out):
    """An accounting-only change stamped the way the guarded UPSERT stamps it
    (one bumped ``mutation_seq`` over the changed rows), with no transcript
    change."""
    import _cctally_core as core_mod
    raw = sqlite3.connect(str(core_mod.CACHE_DB_PATH), timeout=5.0)
    try:
        seq = cache_mod._bump_mutation_seq(raw)
        placeholders = ",".join("?" for _ in msg_ids)
        raw.execute(
            f"UPDATE session_entries SET output_tokens=?, mutation_seq=? "
            f"WHERE msg_id IN ({placeholders})", (out, seq, *msg_ids))
        raw.commit()
    finally:
        raw.close()


def _die_before_rollup_step(cache_mod, conn, monkeypatch):
    """Run one transcript pass that commits its batches and dies before the
    rollup step, as a killed writer does."""
    real = cache_mod._arm_rollup_backfill_on_pricing_change

    def die(_conn):
        raise RuntimeError("writer died before the rollup step")

    monkeypatch.setattr(cache_mod, "_arm_rollup_backfill_on_pricing_change", die)
    with pytest.raises(RuntimeError):
        cache_mod.sync_claude_conversations(conn)
    conn.rollback()
    monkeypatch.setattr(cache_mod, "_arm_rollup_backfill_on_pricing_change", real)


def _take_write_lock_now():
    """Take and release the conversations write lock with no busy wait at all;
    raises ``database is locked`` while another connection holds it."""
    import _cctally_core as core_mod
    other = sqlite3.connect(str(core_mod.CONVERSATIONS_DB_PATH), timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
    finally:
        other.close()


def test_backfill_arm_restarts_an_interrupted_traversal(env, monkeypatch):
    """A message reingest deletes and re-inserts message rows without the
    pending set, and arms the backfill. Resuming a traversal cursor written
    before it would skip every session sorting before the cursor, so a session
    that lost all its messages there keeps a ghost row (its old cost and count
    in the unscoped Viewer)."""
    cache_mod, conn, projects = env
    _interrupted_traversal(cache_mod, conn, projects, monkeypatch)
    assert _row_for(conn, "s1") is not None
    (projects / "f1.jsonl").write_text("")
    _set_meta(conn, "conversation_reingest_pending", "1")
    cache_mod.sync_claude_conversations(conn)
    assert _row_for(conn, "s1") is None
    assert rollup_fresh_mismatches(conn) == []
    assert _cursor(conn) is None and _get_meta(conn, FLAG) is None


def test_backfill_traversal_counts_against_the_pass_allowance(env, monkeypatch):
    """At most two full traversals per pass, the backfill's included: each one
    is minutes cold on a production-sized store, and an accounting writer that
    keeps rotating the generation would otherwise hold one pass for three."""
    cache_mod, conn, projects = env
    _two_session_store(cache_mod, conn, projects)
    _set_meta(conn, FLAG, "1")
    real = cache_mod._run_rollup_traversal
    calls = []

    def rotating(conn_, target, **kwargs):
        done = real(conn_, target, **kwargs)
        calls.append(target)
        _rotate_generation(cache_mod)
        return done

    monkeypatch.setattr(cache_mod, "_run_rollup_traversal", rotating)
    stats = cache_mod.sync_claude_conversations(conn)
    assert len(calls) == 2
    assert stats.deferred_reason == "rollup_traversal_incomplete"
    assert _cursor(conn) is not None


@pytest.mark.parametrize("change,past_bound", [
    ("accounting", False), ("accounting", True),
    ("messages", False), ("messages", True)])
def test_change_set_bounds(env, monkeypatch, change, past_bound):
    """A change set exactly at its bound is mapped to sessions (no traversal,
    the changed sessions re-derived); one row past it runs a traversal. An
    off-by-one either way skips the sessions the bound was meant to cover or
    pays a full traversal for a mappable change."""
    import _cctally_core as core_mod
    cache_mod, conn, projects = env
    a, b = _two_session_store(cache_mod, conn, projects)
    wm = _watermark(conn)
    if change == "accounting":
        _stamp_accounting_change(cache_mod, ["m1", "m2"], 7000)
        raw = sqlite3.connect(str(core_mod.CACHE_DB_PATH), timeout=5.0)
        try:
            n = raw.execute(
                "SELECT COUNT(*) FROM session_entries WHERE mutation_seq > ? "
                "AND msg_id IS NOT NULL AND req_id IS NOT NULL",
                (wm["seq"],)).fetchone()[0]
        finally:
            raw.close()
        bound = "_ROLLUP_ACCOUNTING_CHANGE_BOUND"
    else:
        with a.open("a") as fh:
            fh.write(_user_line("a8", "more", session_id="s1",
                                ts="2026-06-01T00:00:08Z")
                     + _user_line("a9", "again", session_id="s1",
                                  ts="2026-06-01T00:00:09Z"))
        with b.open("a") as fh:
            fh.write(_user_line("b9", "more", session_id="s2",
                                ts="2026-06-01T01:00:09Z"))
        _die_before_rollup_step(cache_mod, conn, monkeypatch)
        n = conn.execute("SELECT COUNT(*) FROM conversation_messages "
                         "WHERE id > ?", (wm["message_id"],)).fetchone()[0]
        bound = "_ROLLUP_MESSAGE_CHANGE_BOUND"
    assert n >= 2
    assert rollup_fresh_mismatches(conn) != []
    monkeypatch.setattr(cache_mod, bound, n - 1 if past_bound else n)
    traversals = _spy_traversals(cache_mod, monkeypatch)
    stats = cache_mod.sync_claude_conversations(conn)
    assert stats.deferred_reason is None
    assert len(traversals) == (1 if past_bound else 0)
    assert rollup_fresh_mismatches(conn) == []


def test_rederive_yields_only_between_chunks(env, monkeypatch):
    """The pause between two re-derive transactions is what lets a waiting
    writer in; one after the last chunk, or one inside a single-chunk change
    set, only delays the pass (and a missing one starves the writer)."""
    cache_mod, conn, projects = env
    _five_session_store(cache_mod, conn, projects, monkeypatch)
    yields = []
    monkeypatch.setattr(cache_mod, "_yield_between_rollup_chunks",
                        lambda: yields.append(1))
    _rotate_generation(cache_mod)
    _silently_reprice(cache_mod, "m3", 3333)
    cache_mod.sync_claude_conversations(conn)
    assert len(yields) == 2
    assert rollup_fresh_mismatches(conn) == []
    yields.clear()
    _stamp_accounting_change(cache_mod, ["m1"], 1111)
    cache_mod.sync_claude_conversations(conn)
    assert yields == []
    assert rollup_fresh_mismatches(conn) == []


def test_input_state_read_failure_with_cache_attached_defers(env, monkeypatch):
    """With cache.db attached, a failed input-state read means the pass
    consumed no accounting version; re-deriving only its touched sessions and
    reporting success would let the frontier certify stale rows."""
    import _lib_ingest_frontier as frontier
    cache_mod, conn, projects = env
    _late_accounting_store(cache_mod, conn, projects)
    before = _get_meta(conn, WATERMARK)
    monkeypatch.setattr(cache_mod, "_rollup_input_state", lambda _conn: None)
    stats = cache_mod.sync_claude_conversations(conn)
    assert stats.deferred_reason == "accounting_generation_unavailable"
    assert frontier.conversation_sync_certifiable("full", stats) is False
    assert _get_meta(conn, WATERMARK) == before


def test_change_set_prewarms_cold_dirty_sessions(env, monkeypatch):
    """A session dirty only through an accounting change (or the pending set)
    is read cold; deriving it cold under the write lock held the lock 5.2 s
    for one 38,146-message session on the production copy. It must be derived
    first where no store lock is held, while a steady pass whose dirty set is
    only its own freshly ingested sessions does no extra derivation."""
    cache_mod, conn, projects = env
    _a, b = _two_session_store(cache_mod, conn, projects)
    real = cache_mod._recompute_conversation_sessions
    prewarmed = []

    def probe(conn_, session_ids=None, **kw):
        out = real(conn_, session_ids, **kw)
        if kw.get("target") == cache_mod._ROLLUP_PREWARM_TABLE:
            _take_write_lock_now()
            prewarmed.append(list(session_ids))
        return out

    monkeypatch.setattr(cache_mod, "_recompute_conversation_sessions", probe)
    _stamp_accounting_change(cache_mod, ["m1"], 7000)
    cache_mod.sync_claude_conversations(conn)
    assert prewarmed == [["s1"]]
    assert rollup_fresh_mismatches(conn) == []
    prewarmed.clear()
    with b.open("a") as fh:
        fh.write(_user_line("b9", "more", session_id="s2",
                            ts="2026-06-01T01:00:09Z"))
    cache_mod.sync_claude_conversations(conn)
    assert prewarmed == []
    assert rollup_fresh_mismatches(conn) == []


def test_marker_start_adopts_the_full_pass_state(env, monkeypatch):
    """A traversal Start left by a pass that may not traverse has processed
    nothing; resuming it under its old target after a second rotation costs a
    second full traversal (minutes on a production store) for no gain."""
    cache_mod, conn, projects = env
    _two_session_store(cache_mod, conn, projects)
    _rotate_generation(cache_mod)
    stats = cache_mod.sync_claude_rollup(conn)
    assert stats.deferred_reason == "rollup_traversal_pending"
    assert _cursor(conn)["after"] == ""
    _rotate_generation(cache_mod)
    _silently_reprice(cache_mod, "m1", 7000)
    traversals = _spy_traversals(cache_mod, monkeypatch)
    stats = cache_mod.sync_claude_conversations(conn)
    assert stats.deferred_reason is None
    assert len(traversals) == 1
    assert rollup_fresh_mismatches(conn) == []
    assert _watermark(conn)["generation"] == _store_generation(cache_mod)


def test_rebuild_stages_in_short_transactions(env, monkeypatch):
    """A rebuild's staging derivation held the conversations write lock for
    the whole derivation (about 200 s cold on the production copy) while the
    Codex conversation writer waits 15 s; it must derive staging in bounded
    chunks, each prewarmed where no lock is held, with the write lock free
    between chunks, and still publish a generation equal to a fresh
    derivation with its staged watermark."""
    cache_mod, conn, projects = env
    _five_session_store(cache_mod, conn, projects, monkeypatch)
    real = cache_mod._recompute_conversation_sessions
    prewarmed, staged, yields = [], [], []

    def probe(conn_, session_ids=None, **kw):
        out = real(conn_, session_ids, **kw)
        target = kw.get("target")
        if target == cache_mod._ROLLUP_PREWARM_TABLE:
            _take_write_lock_now()
            prewarmed.append(list(session_ids))
        elif target == "conversation_sessions_staging":
            staged.append(None if session_ids is None else list(session_ids))
        return out

    def pause():
        _take_write_lock_now()
        yields.append(1)

    monkeypatch.setattr(cache_mod, "_recompute_conversation_sessions", probe)
    monkeypatch.setattr(cache_mod, "_yield_between_rollup_chunks", pause)
    cache_mod.sync_claude_conversations(conn, rebuild=True)
    assert staged == [["s1", "s2"], ["s3", "s4"], ["s5"]]
    assert prewarmed == staged
    assert len(yields) == 2
    assert rollup_fresh_mismatches(conn) == []
    assert _watermark(conn) == cache_mod._rollup_input_state(conn).as_json_obj()
    assert _get_meta(conn, STAGED_WATERMARK) is None
    assert _get_meta(conn, FLAG) is None


def test_rebuild_death_mid_staging_publishes_nothing(env, monkeypatch):
    """A rebuild that dies after its first staging chunk committed must leave
    the published rollup and its watermark exactly as they were (readers keep
    the previous complete generation), and the next rebuild must publish a
    complete fresh one."""
    cache_mod, conn, projects = env
    _five_session_store(cache_mod, conn, projects, monkeypatch)
    live_before = conn.execute(
        "SELECT * FROM conversation_sessions ORDER BY session_id").fetchall()
    watermark_before = _get_meta(conn, WATERMARK)
    real = cache_mod._recompute_conversation_sessions
    staged = []

    def die_on_second(conn_, session_ids=None, **kw):
        if kw.get("target") == "conversation_sessions_staging":
            staged.append(session_ids)
            if len(staged) == 2:
                raise RuntimeError("died after the first staging chunk")
        return real(conn_, session_ids, **kw)

    monkeypatch.setattr(cache_mod, "_recompute_conversation_sessions",
                        die_on_second)
    with pytest.raises(RuntimeError):
        cache_mod.sync_claude_conversations(conn, rebuild=True)
    conn.rollback()
    monkeypatch.setattr(cache_mod, "_recompute_conversation_sessions", real)
    assert conn.execute(
        "SELECT * FROM conversation_sessions ORDER BY session_id"
    ).fetchall() == live_before
    assert _get_meta(conn, WATERMARK) == watermark_before
    cache_mod.sync_claude_conversations(conn, rebuild=True)
    assert rollup_fresh_mismatches(conn) == []
    assert _watermark(conn) == cache_mod._rollup_input_state(conn).as_json_obj()



def test_rebuild_refused_mid_staging_publishes_nothing(env, monkeypatch):
    """A #705 refusal in a middle staging chunk, after an earlier chunk
    committed, must publish nothing and acknowledge nothing: readers keep the
    previous generation and its watermark, no staged watermark is written,
    the armed backfill flag survives and the pass reports the refusal, so the
    next authorized pass still repairs the store."""
    cache_mod, conn, projects = env
    _five_session_store(cache_mod, conn, projects, monkeypatch)
    live_before = conn.execute(
        "SELECT * FROM conversation_sessions ORDER BY session_id").fetchall()
    watermark_before = _get_meta(conn, WATERMARK)
    _set_meta(conn, FLAG, "1")
    real = cache_mod._recompute_conversation_sessions
    staged = []

    def refuse_second(conn_, session_ids=None, **kw):
        if kw.get("target") == "conversation_sessions_staging":
            staged.append(session_ids)
            if len(staged) == 2:
                return False
        return real(conn_, session_ids, **kw)

    monkeypatch.setattr(cache_mod, "_recompute_conversation_sessions",
                        refuse_second)
    monkeypatch.setattr(cache_mod, "_yield_between_rollup_chunks", lambda: None)
    stats = cache_mod.sync_claude_conversations(conn, rebuild=True)
    assert stats.deferred_reason == "pricing_write_refused"
    assert len(staged) == 2
    monkeypatch.setattr(cache_mod, "_recompute_conversation_sessions", real)
    assert conn.execute(
        "SELECT * FROM conversation_sessions ORDER BY session_id"
    ).fetchall() == live_before
    assert _get_meta(conn, WATERMARK) == watermark_before
    assert _get_meta(conn, STAGED_WATERMARK) is None
    assert _get_meta(conn, FLAG) == "1"
    stats = cache_mod.sync_claude_conversations(conn)
    assert stats.deferred_reason is None
    assert rollup_fresh_mismatches(conn) == []
    assert _get_meta(conn, FLAG) is None


def test_traversal_and_staging_visit_an_empty_session_id(env, monkeypatch):
    """A transcript line may carry ``"sessionId": ""`` and is stored under
    that id. Every chunk seek is strictly above an ``after`` that starts at
    the empty string, so a traversal left that session's row stale and a
    rebuild's staging dropped it from the published generation."""
    cache_mod, conn, projects = env
    _five_session_store(cache_mod, conn, projects, monkeypatch)
    (projects / "e.jsonl").write_text(
        _user_line("e0", "hi", session_id="", ts="2026-06-07T00:00:00Z")
        + _asst_line("e1", "me", "re", "x", session_id="",
                     ts="2026-06-07T00:00:01Z", out=70))
    _sync(cache_mod, conn)
    assert conn.execute("SELECT count(*) FROM conversation_messages "
                        "WHERE session_id = ''").fetchone()[0] == 2
    assert rollup_fresh_mismatches(conn) == []
    _rotate_generation(cache_mod)
    _silently_reprice(cache_mod, "me", 7777)
    cache_mod.sync_claude_conversations(conn)
    assert rollup_fresh_mismatches(conn) == []
    monkeypatch.setattr(cache_mod, "_yield_between_rollup_chunks", lambda: None)
    cache_mod.sync_claude_conversations(conn, rebuild=True)
    assert rollup_fresh_mismatches(conn) == []
    assert conn.execute("SELECT 1 FROM conversation_sessions "
                        "WHERE session_id = ''").fetchone() is not None


def test_reingest_death_before_the_arm_leaves_no_ghost_row(env, monkeypatch):
    """The resumable reingest deletes and re-inserts every file's message rows
    without recording the sessions it removed, so only the backfill traversal
    drops the row of a session that lost every message. A pass that died after
    the reingest cleared its flags and before the backfill was armed (two
    commits) left that ghost session in the unscoped Viewer for good."""
    cache_mod, conn, projects = env
    a, b = _two_session_store(cache_mod, conn, projects)
    b.write_text(_user_line("c0", "new", session_id="s3", ts="2026-06-01T02:00:00Z")
                 + _asst_line("c1", "m3", "r3", "z", session_id="s3",
                              ts="2026-06-01T02:00:01Z", out=7))
    _set_meta(conn, "conversation_reingest_pending", "1")
    real = cache_mod._resumable_reingest_conversation_messages

    def reingest_then_die(conn_, **kwargs):
        real(conn_, **kwargs)
        raise RuntimeError("died after the reingest committed")

    monkeypatch.setattr(cache_mod, "_resumable_reingest_conversation_messages",
                        reingest_then_die)
    with pytest.raises(RuntimeError):
        cache_mod.sync_claude_conversations(conn)
    conn.rollback()
    monkeypatch.setattr(cache_mod, "_resumable_reingest_conversation_messages",
                        real)
    assert conn.execute("SELECT 1 FROM conversation_messages "
                        "WHERE session_id = 's2'").fetchone() is None
    _sync(cache_mod, conn)
    assert rollup_fresh_mismatches(conn) == []
    assert conn.execute("SELECT 1 FROM conversation_sessions "
                        "WHERE session_id = 's2'").fetchone() is None


def test_failed_state_reread_after_a_traversal_defers(env, monkeypatch):
    """After a traversal completes (its cursor gone) the consumer re-reads the
    input state to find what changed while it ran. A failed re-read returned
    success, so an accounting change to a session an earlier chunk already
    derived stayed unapplied while the pass was certifiable as caught up."""
    cache_mod, conn, projects = env
    a, _b = _two_session_store(cache_mod, conn, projects)
    _rotate_generation(cache_mod)
    real_traversal = cache_mod._run_rollup_traversal
    real_state = cache_mod._rollup_input_state
    done = []

    def traversal_then_accounting_change(conn_, target, **kwargs):
        ok = real_traversal(conn_, target, **kwargs)
        _stamp_accounting_change(cache_mod, ["m1"], 6000)
        done.append(target)
        return ok

    monkeypatch.setattr(cache_mod, "_run_rollup_traversal",
                        traversal_then_accounting_change)
    monkeypatch.setattr(cache_mod, "_rollup_input_state",
                        lambda conn_: None if done else real_state(conn_))
    stats = cache_mod.sync_claude_conversations(conn)
    assert len(done) == 1
    assert stats.deferred_reason == "accounting_generation_unavailable"
    monkeypatch.setattr(cache_mod, "_run_rollup_traversal", real_traversal)
    monkeypatch.setattr(cache_mod, "_rollup_input_state", real_state)
    stats = cache_mod.sync_claude_conversations(conn)
    assert stats.deferred_reason is None
    assert rollup_fresh_mismatches(conn) == []

def test_promoted_command_prompt_rederives_the_title(env):
    """The promote consumer rewrites a legacy command marker into the human
    prompt it carried, in place and without new message ids, which changes
    the session's title; without recording the session the unscoped Viewer
    keeps the old title."""
    cache_mod, conn, projects = env
    marker = ("<command-name>/review</command-name>"
              "<command-args>Review the rollup carefully.</command-args>")
    (projects / "a.jsonl").write_text(
        _user_line("p0", marker, session_id="s1", ts="2026-06-01T00:00:00Z")
        + _user_line("p1", "a later prompt", session_id="s1",
                     ts="2026-06-01T00:00:05Z")
        + _asst_line("p2", "m1", "r1", "ok", session_id="s1",
                     ts="2026-06-01T00:00:06Z"))
    _sync(cache_mod, conn)
    promoted_title = _enrichment_for(conn, "s1")[2]
    conn.execute("UPDATE conversation_messages SET entry_type='meta', text='' "
                 "WHERE uuid='p0'")
    cache_mod._recompute_conversation_sessions(conn, ["s1"])
    conn.commit()
    assert rollup_fresh_mismatches(conn) == []
    assert _enrichment_for(conn, "s1")[2] != promoted_title
    _set_meta(conn, "conversation_promote_command_args_pending", "1")
    cache_mod.sync_claude_conversations(conn)
    assert conn.execute("SELECT entry_type FROM conversation_messages "
                        "WHERE uuid='p0'").fetchone()[0] == "human"
    assert rollup_fresh_mismatches(conn) == []
    assert _enrichment_for(conn, "s1")[2] == promoted_title
