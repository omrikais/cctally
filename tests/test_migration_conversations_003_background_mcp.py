"""Conversations migration ``003_background_mcp_result_replay`` — arming,
deferral, and the five mandatory plumbing sites a real reingest flag must touch.

Spec: ``docs/superpowers/specs/2026-07-31-background-mcp-result-recovery-design.md`` §4.

This is a ``conversations.db`` migration, not a ``cache.db`` one: transcript rows
and their replay markers live in conversations.db and the Claude conversation
synchronizer never writes cache.db, so a cache migration would arm a flag the
conversation walker never reads.
"""
import fcntl
import json
import os
import sqlite3

import pytest

from conftest import load_script, redirect_paths  # type: ignore


FLAG = "conversation_background_mcp_reingest_pending"
MIGRATION = "003_background_mcp_result_replay"
LEGACY_REINGEST_FLAGS = (
    "conversation_reingest_pending",
    "conversation_source_tool_use_reingest_pending",
    "conversation_reingest_enrichment_pending",
    "conversation_media_reingest_pending",
    "conversation_queued_prompt_reingest_pending",
    "conversation_reingest_nested_agent_pending",
    FLAG,
)


def _handler(ns):
    for m in ns["_CONVERSATIONS_MIGRATIONS"]:
        if m.name == MIGRATION:
            return m.handler
    raise AssertionError(f"conversations migration {MIGRATION} not registered")


def _meta(conn, key):
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def test_the_flag_constant_is_distinct_from_every_existing_reingest_flag():
    """Reusing an existing flag would conflate two enrichments and make a
    partially-completed replay unrecoverable."""
    load_script()
    import _cctally_cache as cc

    assert cc.CONVERSATION_BACKGROUND_MCP_REINGEST_KEY == FLAG
    others = [k for k in cc._REINGEST_FLAG_KEYS if k != FLAG]
    assert FLAG not in others


def test_the_flag_is_wired_into_both_registries():
    """Migration 014 documents the mandatory sites; this covers the two lists.

    The remaining three (the reingest selection SELECT, the completion DELETE
    and the rebuild cleanup DELETE) are covered BEHAVIORALLY below —
    ``test_replay_lands_the_notification_row_and_clears_the_flag``,
    ``test_a_targeted_sync_declines_while_the_flag_is_pending`` and
    ``test_rebuild_clears_the_flag_instead_of_re_arming_it`` — not by scanning
    the module source for literal occurrences."""
    load_script()
    import _cctally_cache as cc

    assert FLAG in cc._REINGEST_FLAG_KEYS, (
        "without this the resumable reingest never runs for this flag")
    assert FLAG in cc._TARGETED_DECLINE_FLAGS, (
        "without this a targeted sync would run while a global replay is pending")


def test_migration_arms_the_claude_replay_flag(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_conversations_db"]()
    try:
        conn.execute("DELETE FROM cache_meta WHERE key=?", (FLAG,))
        conn.commit()
        _handler(ns)(conn)
        assert _meta(conn, FLAG) == "1"
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_conversations_db"]()
    try:
        handler = _handler(ns)
        handler(conn)
        handler(conn)
        assert _meta(conn, FLAG) == "1"
    finally:
        conn.close()


def test_migration_defers_when_the_claude_provider_lock_is_held(
        tmp_path, monkeypatch):
    """Without the flock a reingest already in progress can complete and clear
    every reingest flag — including the marker just armed — consuming the
    backfill silently. Conversations 002 solves the equivalent Codex race the
    same way."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_db as db

    conn = ns["open_conversations_db"]()
    lock_path = db._cache_db_lock_path_for_conn(conn)
    assert lock_path is not None
    # The SAME sibling `sync_claude_conversations` serializes on.
    import _cctally_core
    assert lock_path == _cctally_core.CONVERSATIONS_LOCK_PATH
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        conn.execute("DELETE FROM cache_meta WHERE key=?", (FLAG,))
        conn.commit()
        with pytest.raises(db.MigrationGateNotMet):
            _handler(ns)(conn)
        assert _meta(conn, FLAG) is None, "a deferred migration arms nothing"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        conn.close()


# --- the replay itself -------------------------------------------------------

def _bg_lines():
    notif = ("<task-notification>\n<task-id>kravg1b9s</task-id>\n"
             "<status>completed</status>\n<summary>MCP task kravg1b9 done.</summary>\n"
             '<result>{"threadId":"t1","content":"recovered"}</result>\n'
             "</task-notification>")
    return (
        json.dumps({"type": "user", "uuid": "h1", "sessionId": "s1",
                    "timestamp": "2026-07-30T20:00:00Z",
                    "message": {"role": "user", "content": "go"}}) + "\n"
        + json.dumps({"type": "attachment", "uuid": "n1", "sessionId": "s1",
                      "timestamp": "2026-07-30T20:51:16.312Z",
                      "attachment": {"type": "queued_command",
                                     "commandMode": "task-notification",
                                     "prompt": notif}}) + "\n"
    )


@pytest.fixture
def replay_env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_cache as cc

    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "a.jsonl").write_text(_bg_lines())
    conn = ns["open_conversations_db"]()
    cc.sync_claude_conversations(conn)
    yield ns, cc, conn
    conn.close()


def test_replay_lands_the_notification_row_and_clears_the_flag(replay_env):
    ns, cc, conn = replay_env
    # Simulate a pre-fix store: drop the notification row and re-arm the flag,
    # the way the migration does on a real upgrade.
    conn.execute("DELETE FROM conversation_messages WHERE entry_type='meta'")
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,'1')",
                 (FLAG,))
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages "
        "WHERE entry_type='meta'").fetchone()[0] == 0

    cc.sync_claude_conversations(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages "
        "WHERE entry_type='meta'").fetchone()[0] == 1, (
        "the from-zero replay must re-ingest the notification row")
    assert _meta(conn, FLAG) is None, "a completed replay clears its flag"


def test_from_zero_replay_forces_the_retention_prune(replay_env, monkeypatch):
    """The resumable reingest deletes and reconstructs each source file from
    offset zero, which restores rows the throttled prune already trimmed. Without
    the report, `did_from_zero_replay` stays False and retention-pruned history
    silently reappears until the next ordinary prune."""
    ns, cc, conn = replay_env
    calls = []
    monkeypatch.setattr(cc, "_force_retention_prune_after_replay",
                        lambda *a, **k: calls.append(1))
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,'1')",
                 (FLAG,))
    conn.commit()
    cc.sync_claude_conversations(conn)
    assert calls, ("a from-zero replay must force the retention prune or "
                   "pruned history silently reappears")


def test_no_replay_does_not_force_the_retention_prune(replay_env, monkeypatch):
    """Non-vacuity: the ordinary steady-state sync must NOT force a prune."""
    ns, cc, conn = replay_env
    calls = []
    monkeypatch.setattr(cc, "_force_retention_prune_after_replay",
                        lambda *a, **k: calls.append(1))
    cc.sync_claude_conversations(conn)
    assert not calls


def test_a_targeted_sync_declines_while_the_flag_is_pending(replay_env):
    ns, cc, conn = replay_env
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,'1')",
                 (FLAG,))
    conn.commit()
    stats = cc.sync_claude_conversations(conn, only_paths={"/nope.jsonl"})
    assert stats.deferred_reason == "pending_global_flags"
    assert _meta(conn, FLAG) == "1", "a declined targeted sync consumes nothing"


def test_rebuild_clears_the_flag_instead_of_re_arming_it(replay_env):
    ns, cc, conn = replay_env
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,'1')",
                 (FLAG,))
    conn.commit()
    cc.sync_claude_conversations(conn, rebuild=True)
    assert _meta(conn, FLAG) is None, (
        "missing the rebuild cleanup site re-arms the flag on every rebuild")


# --- legacy cache.db plumbing ------------------------------------------------

@pytest.mark.parametrize("flag", LEGACY_REINGEST_FLAGS)
def test_legacy_sync_cache_selects_every_sibling_reingest_flag(
        tmp_path, monkeypatch, flag):
    """The pre-#320 core-cache consumer remains a compatibility path for an
    upgraded cache.db carrying one of the historical transcript flags. Every
    real message-reingest sibling must select the resumable consumer; a missing
    literal silently strands that marker forever."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_cache as cc

    conn = ns["open_cache_db"]()
    calls = []

    def consume(active):
        calls.append(flag)
        active.execute(
            "DELETE FROM cache_meta WHERE key IN ("
            + ",".join("?" for _ in LEGACY_REINGEST_FLAGS)
            + ")",
            LEGACY_REINGEST_FLAGS,
        )
        active.commit()

    monkeypatch.setattr(cc, "_resumable_reingest_conversation_messages", consume)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,'1')",
            (flag,),
        )
        conn.commit()
        cc.sync_cache(conn)
        assert calls == [flag], f"legacy sync_cache did not select {flag}"
        assert _meta(conn, flag) is None
    finally:
        conn.close()


def test_legacy_sync_cache_without_reingest_flag_skips_the_consumer(
        tmp_path, monkeypatch):
    """Non-vacuity: the compatibility selector is flag-driven, not an
    unconditional replay that would make every positive case pass."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_cache as cc

    conn = ns["open_cache_db"]()

    def unexpected(_active):
        raise AssertionError("reingest ran without a pending sibling flag")

    monkeypatch.setattr(cc, "_resumable_reingest_conversation_messages", unexpected)
    try:
        for flag in LEGACY_REINGEST_FLAGS:
            conn.execute("DELETE FROM cache_meta WHERE key=?", (flag,))
        conn.commit()
        cc.sync_cache(conn)
    finally:
        conn.close()


def test_legacy_sync_cache_rebuild_clears_every_sibling_reingest_flag(
        tmp_path, monkeypatch):
    """A compatibility rebuild is itself the byte-zero replay, so it must
    consume all sibling flags plus their resumable cursor state rather than
    scheduling a redundant second pass."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_cache as cc

    conn = ns["open_cache_db"]()
    try:
        for flag in (*LEGACY_REINGEST_FLAGS,
                     "conversation_reingest_cursor",
                     "conversation_reingest_cursor_gen"):
            conn.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,'1')",
                (flag,),
            )
        conn.commit()
        cc.sync_cache(conn, rebuild=True)
        remaining = conn.execute(
            "SELECT key FROM cache_meta WHERE key IN ("
            + ",".join("?" for _ in (*LEGACY_REINGEST_FLAGS,
                                      "conversation_reingest_cursor",
                                      "conversation_reingest_cursor_gen"))
            + ")",
            (*LEGACY_REINGEST_FLAGS,
             "conversation_reingest_cursor",
             "conversation_reingest_cursor_gen"),
        ).fetchall()
        assert remaining == []
    finally:
        conn.close()
