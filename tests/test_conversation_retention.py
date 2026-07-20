"""#313 P3: conversation-transcript retention prune kernel (Tasks 8-9).

Covers base-table eligibility (F6), threads-kept for Codex (F5), cost-row
preservation, NULL-identity fallback (F12), the maintenance-flock + throttle
orchestration (F7), and from-zero-replay force prune (F9).
"""
from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402

import _cctally_db as _db  # noqa: E402
import _lib_codex_conversation_query as _cxq  # noqa: E402

UTC = dt.timezone.utc

_CORPUS = Path(__file__).resolve().parent / "fixtures" / "codex-parity" / "v1" / "rollouts"


def _env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_cache_db"]()
    retention = importlib.import_module("_lib_conversation_retention")
    return ns, conn, retention


_OFFSET = [0]


def _seed_msg(conn, session_id, ts, *, source_path="seed.jsonl",
              entry_type="human", text="hello world"):
    _OFFSET[0] += 1
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id, uuid, source_path, byte_offset, timestamp_utc, entry_type, text) "
        "VALUES (?,?,?,?,?,?,?)",
        (session_id, f"u-{_OFFSET[0]}", source_path, _OFFSET[0], ts, entry_type, text),
    )
    return _OFFSET[0]


def _fts_count(conn):
    return conn.execute("SELECT count(*) FROM conversation_fts").fetchone()[0]


def _cutoff():
    # Retention boundary: messages strictly before this are prunable.
    return dt.datetime(2026, 1, 18, 0, 0, 0, tzinfo=UTC)


OLD = "2025-08-01T12:00:00.000Z"     # well before the cutoff
OLDER = "2025-07-01T09:00:00.000Z"
FRESH = "2026-07-01T08:00:00.000Z"   # well after the cutoff


def test_old_session_fully_pruned(tmp_path, monkeypatch):
    ns, conn, retention = _env(tmp_path, monkeypatch)
    mid = _seed_msg(conn, "old", OLD)
    _seed_msg(conn, "old", OLDER)
    _seed_msg(conn, "fresh", FRESH)
    conn.execute(
        "INSERT INTO conversation_ai_titles(session_id, ai_title, byte_offset) "
        "VALUES ('old', 'Old title', 1)")
    conn.execute(
        "INSERT INTO conversation_file_touches(message_id, session_id, file_path, tool) "
        "VALUES (?, 'old', '/x.py', 'Edit')", (mid,))
    conn.execute(
        "INSERT INTO conversation_sessions(session_id, msg_count, started_utc, last_activity_utc) "
        "VALUES ('old', 2, ?, ?)", (OLDER, OLD))
    conn.execute(
        "INSERT INTO conversation_sessions(session_id, msg_count, started_utc, last_activity_utc) "
        "VALUES ('fresh', 1, ?, ?)", (FRESH, FRESH))
    conn.commit()
    fts_before = _fts_count(conn)

    stats = retention.prune_conversation_transcripts(conn, cutoff_utc=_cutoff())
    conn.commit()

    assert stats.claude_sessions == 1
    assert stats.claude_messages == 2
    # old session gone everywhere; fresh untouched
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE session_id='old'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE session_id='fresh'").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_ai_titles WHERE session_id='old'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_file_touches WHERE session_id='old'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_sessions WHERE session_id='old'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_sessions WHERE session_id='fresh'").fetchone()[0] == 1
    # FTS index shrank by exactly the two pruned messages.
    assert _fts_count(conn) == fts_before - 2


def test_stale_rollup_does_not_prune_active_session(tmp_path, monkeypatch):
    """F6: eligibility is decided from the base table, never the rollup. A
    session with a fresh message is kept whole even if its rollup row is
    stale-old."""
    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_msg(conn, "active", OLD)     # an old message ...
    _seed_msg(conn, "active", FRESH)   # ... and a genuinely recent one
    # Stale rollup claims the session last active long ago (would wrongly mark
    # it prunable if the rollup decided eligibility).
    conn.execute(
        "INSERT INTO conversation_sessions(session_id, msg_count, started_utc, last_activity_utc) "
        "VALUES ('active', 2, ?, ?)", (OLD, OLD))
    conn.commit()

    stats = retention.prune_conversation_transcripts(conn, cutoff_utc=_cutoff())
    conn.commit()

    assert stats.claude_sessions == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE session_id='active'").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_sessions WHERE session_id='active'").fetchone()[0] == 1


def test_all_null_timestamp_session_never_pruned(tmp_path, monkeypatch):
    """F12: a session whose messages are entirely NULL-timestamped is treated
    conservatively and never pruned in isolation."""
    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_msg(conn, "nullts", None)
    _seed_msg(conn, "nullts", None)
    conn.commit()
    retention.prune_conversation_transcripts(conn, cutoff_utc=_cutoff())
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE session_id='nullts'").fetchone()[0] == 2


def test_cost_rows_preserved(tmp_path, monkeypatch):
    """Pruning transcripts must never touch cost/usage rows."""
    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_msg(conn, "old", OLD)
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, output_tokens) "
        "VALUES ('/x.jsonl', 1, ?, 'claude-test', 42)", (OLD,))
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0]
    retention.prune_conversation_transcripts(conn, cutoff_utc=_cutoff())
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == before == 1


def test_fresh_cache_uses_incremental_auto_vacuum(tmp_path, monkeypatch):
    """A freshly created cache.db must be in INCREMENTAL auto_vacuum mode (2) so
    the retention prune can return freed pages to the OS with an incremental
    vacuum instead of requiring a full ``db vacuum``."""
    _, conn, _ = _env(tmp_path, monkeypatch)
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2


def test_orchestrated_prune_reclaims_freed_pages(tmp_path, monkeypatch):
    """The throttled/orchestrated prune reclaims freed pages (incremental vacuum)
    so the file physically shrinks — deleting rows alone only grows the freelist.
    Regression for the bloat that pegged the dashboard (8.7 GB cache.db)."""
    ns, conn, retention = _env(tmp_path, monkeypatch)
    # Enough old, sizeable messages that deleting them frees many pages.
    for i in range(2000):
        _seed_msg(conn, f"old-{i}", OLD, text="x" * 500)
    _seed_msg(conn, "fresh", FRESH)
    conn.commit()
    pages_before = conn.execute("PRAGMA page_count").fetchone()[0]

    stats = retention._maybe_prune_conversation_retention(
        conn, now_utc=NOW, retention_days=180, force=True)

    assert stats is not None and stats.claude_messages == 2000
    # incremental_vacuum returned the freed pages to the OS: no freelist backlog
    # remains and page_count drops. Without the reclaim page_count stays flat.
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert conn.execute("PRAGMA page_count").fetchone()[0] < pages_before


class _PartialVacuumConnection:
    """Model Python 3.11 stopping after one zero-column vacuum row."""

    def __init__(self, conn):
        self._conn = conn
        self.script_calls = 0

    def execute(self, sql, *args, **kwargs):
        if sql.strip().rstrip(";").lower() == "pragma incremental_vacuum":
            return self._conn.execute("PRAGMA incremental_vacuum(1)")
        return self._conn.execute(sql, *args, **kwargs)

    def executescript(self, sql):
        self.script_calls += 1
        if self.script_calls == 1:
            return self._conn.executescript("PRAGMA incremental_vacuum(1);")
        return self._conn.executescript(sql)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_orchestrated_prune_retries_partial_incremental_vacuum(
    tmp_path, monkeypatch
):
    """A partial zero-column pragma step is retried until reclaim completes."""
    ns, conn, retention = _env(tmp_path, monkeypatch)
    for i in range(2000):
        _seed_msg(conn, f"old-{i}", OLD, text="x" * 500)
    _seed_msg(conn, "fresh", FRESH)
    conn.commit()
    partial = _PartialVacuumConnection(conn)

    stats = retention._maybe_prune_conversation_retention(
        partial, now_utc=NOW, retention_days=180, force=True)

    assert stats is not None and stats.claude_messages == 2000
    assert partial.script_calls == 2
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0


def test_codex_events_pruned_threads_retained(tmp_path, monkeypatch):
    """F5: prune only codex_conversation_events; keep codex_conversation_threads
    (and codex_session_entries) so source_analytics's range still resolves."""
    ns, conn, retention = _env(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO codex_conversation_threads "
        "(conversation_key, source_root_key, native_thread_id, root_thread_id, "
        " source_path, cwd, git_json) "
        "VALUES ('conv-old', 'root-a', 'nt', 'rt', '/c.jsonl', '/proj', '{\"b\":\"main\"}')")
    conn.execute(
        "INSERT INTO codex_conversation_events "
        "(source_path, line_offset, source_root_key, conversation_key, timestamp_utc, payload_json) "
        "VALUES ('/c.jsonl', 1, 'root-a', 'conv-old', ?, '{}')", (OLD,))
    conn.execute(
        "INSERT INTO codex_conversation_events "
        "(source_path, line_offset, source_root_key, conversation_key, timestamp_utc, payload_json) "
        "VALUES ('/c.jsonl', 2, 'root-a', 'conv-fresh', ?, '{}')", (FRESH,))
    conn.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model, input_tokens, "
        " cached_input_tokens, output_tokens, reasoning_output_tokens, total_tokens, "
        " source_root_key, conversation_key) "
        "VALUES ('/c.jsonl', 3, ?, 's', 'gpt-5', 10, 0, 5, 0, 15, 'root-a', 'conv-old')", (OLD,))
    conn.commit()

    stats = retention.prune_conversation_transcripts(conn, cutoff_utc=_cutoff())
    conn.commit()

    assert stats.codex_conversations == 1
    assert stats.codex_events == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_events WHERE conversation_key='conv-old'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_events WHERE conversation_key='conv-fresh'"
    ).fetchone()[0] == 1
    # threads + cost entries retained (F5)
    assert conn.execute("SELECT COUNT(*) FROM codex_conversation_threads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 1
    # the source_analytics join still resolves cwd/git for the pruned range
    joined = conn.execute(
        "SELECT threads.cwd, threads.git_json FROM codex_session_entries AS entries "
        "LEFT JOIN codex_conversation_threads AS threads "
        "  ON threads.conversation_key = entries.conversation_key "
        " AND threads.source_root_key = entries.source_root_key"
    ).fetchone()
    assert joined == ("/proj", '{"b":"main"}')


def test_null_session_grouped_by_source_path(tmp_path, monkeypatch):
    """F12: NULL session_id rows group by source_path so they're bounded."""
    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_msg(conn, None, OLD, source_path="/malformed-old.jsonl")
    _seed_msg(conn, None, OLD, source_path="/malformed-old.jsonl")
    _seed_msg(conn, None, FRESH, source_path="/malformed-fresh.jsonl")
    conn.commit()
    retention.prune_conversation_transcripts(conn, cutoff_utc=_cutoff())
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE source_path='/malformed-old.jsonl'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE source_path='/malformed-fresh.jsonl'"
    ).fetchone()[0] == 1


NOW = dt.datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC)


def test_orchestrator_throttles_and_force_bypasses(tmp_path, monkeypatch):
    """F7 throttle + F9 force. First prune stamps; a second within 24h is
    throttled; force=True (from-zero replay) bypasses and re-trims."""
    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_msg(conn, "old1", OLD)
    conn.commit()

    r1 = retention._maybe_prune_conversation_retention(
        conn, now_utc=NOW, retention_days=180)
    assert r1 is not None and r1.claude_sessions == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE session_id='old1'").fetchone()[0] == 0

    # Simulate a from-zero replay restoring old rows.
    _seed_msg(conn, "old2", OLD)
    conn.commit()
    r2 = retention._maybe_prune_conversation_retention(
        conn, now_utc=NOW + dt.timedelta(hours=1), retention_days=180)
    assert r2 is None  # throttled within 24h
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE session_id='old2'").fetchone()[0] == 1

    r3 = retention._maybe_prune_conversation_retention(
        conn, now_utc=NOW + dt.timedelta(hours=1), retention_days=180, force=True)
    assert r3 is not None and r3.claude_sessions == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE session_id='old2'").fetchone()[0] == 0


def test_orchestrator_runs_again_after_24h(tmp_path, monkeypatch):
    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_msg(conn, "old1", OLD)
    conn.commit()
    retention._maybe_prune_conversation_retention(conn, now_utc=NOW, retention_days=180)
    _seed_msg(conn, "old2", OLD)
    conn.commit()
    r = retention._maybe_prune_conversation_retention(
        conn, now_utc=NOW + dt.timedelta(hours=25), retention_days=180)
    assert r is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE session_id='old2'").fetchone()[0] == 0


class _CommitCountingConnection:
    """Proxy that records transaction boundaries and can fail before commit."""

    def __init__(self, conn, *, fail_on_commit=None):
        self._conn = conn
        self.commit_calls = 0
        self.fail_on_commit = fail_on_commit

    def commit(self):
        self.commit_calls += 1
        if self.commit_calls == self.fail_on_commit:
            raise RuntimeError("synthetic intermediate commit failure")
        return self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_orchestrator_commits_each_pruned_conversation_separately(
    tmp_path, monkeypatch
):
    """#315: bound WAL growth with one commit per whole conversation.

    The final extra commit carries only the 24-hour throttle stamp. Provider and
    maintenance locks stay held by the orchestrator across every boundary.
    """
    ns, conn, retention = _env(tmp_path, monkeypatch)
    for session_id in ("claude-a", "claude-b", "claude-c"):
        _seed_msg(conn, session_id, OLD)
    for index, conversation_key in enumerate(("codex-a", "codex-b"), start=1):
        _seed_codex_event(
            conn,
            conversation_key,
            OLD,
            source_path=f"/{conversation_key}.jsonl",
            line_offset=index,
        )
    conn.commit()
    counted = _CommitCountingConnection(conn)

    stats = retention._maybe_prune_conversation_retention(
        counted, now_utc=NOW, retention_days=180, force=True
    )

    assert stats is not None
    assert stats.claude_sessions == 3
    assert stats.codex_conversations == 2
    assert counted.commit_calls == 6  # five whole groups + final stamp
    assert conn.execute(
        "SELECT COUNT(*) FROM cache_meta "
        "WHERE key='conversation_retention_last_prune_at'"
    ).fetchone()[0] == 1
    conn.close()


def test_intermediate_commit_failure_keeps_completed_groups_and_retries_rest(
    tmp_path, monkeypatch
):
    """#315 partial-progress contract.

    A completed conversation remains durably pruned, the active conversation
    rolls back whole, and no throttle stamp suppresses the next retry.
    """
    import pytest

    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_msg(conn, "a-first", OLD, text="FirstUnique")
    _seed_msg(conn, "b-second", OLD, text="SecondUnique")
    conn.commit()
    failing = _CommitCountingConnection(conn, fail_on_commit=2)

    with pytest.raises(RuntimeError, match="synthetic intermediate commit failure"):
        retention._maybe_prune_conversation_retention(
            failing, now_utc=NOW, retention_days=180, force=True
        )

    remaining_sessions = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT session_id FROM conversation_messages"
        )
    }
    assert len(remaining_sessions) == 1
    assert remaining_sessions <= {"a-first", "b-second"}
    assert conn.execute(
        "SELECT COUNT(*) FROM cache_meta "
        "WHERE key='conversation_retention_last_prune_at'"
    ).fetchone()[0] == 0
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")
    conn.commit()

    retry = retention._maybe_prune_conversation_retention(
        conn, now_utc=NOW, retention_days=180, force=True
    )

    assert retry is not None and retry.claude_sessions == 1
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM cache_meta "
        "WHERE key='conversation_retention_last_prune_at'"
    ).fetchone()[0] == 1
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")
    conn.close()


def test_orchestrator_disabled_when_retention_zero(tmp_path, monkeypatch):
    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_msg(conn, "old1", OLD)
    conn.commit()
    r = retention._maybe_prune_conversation_retention(
        conn, now_utc=NOW, retention_days=0)
    assert r is None
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE session_id='old1'").fetchone()[0] == 1


def test_orchestrator_skips_when_maintenance_flock_contended(tmp_path, monkeypatch):
    """F7: a dedicated non-blocking maintenance flock serializes prune attempts
    across processes; a contended flock skips cleanly without stamping."""
    import fcntl
    import _cctally_core
    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_msg(conn, "old1", OLD)
    conn.commit()
    held = open(_cctally_core.CACHE_LOCK_MAINTENANCE_PATH, "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        r = retention._maybe_prune_conversation_retention(
            conn, now_utc=NOW, retention_days=180)
        assert r is None
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE session_id='old1'").fetchone()[0] == 1
        # No throttle stamp written, so the next uncontended cycle retries.
        assert conn.execute(
            "SELECT COUNT(*) FROM cache_meta WHERE key='conversation_retention_last_prune_at'"
        ).fetchone()[0] == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_rebuild_replay_triggers_force_prune_but_noop_sync_does_not(tmp_path, monkeypatch):
    """F9 wiring: a from-zero replay (rebuild) invokes the UNTHROTTLED prune
    (force=True); a plain no-op sync does not prune at all."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = importlib.import_module("_cctally_cache")
    retention = importlib.import_module("_lib_conversation_retention")
    (tmp_path / ".claude" / "projects" / "-Users-u-proj").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex"))

    calls = []

    def spy(conn, *, now_utc, retention_days, force=False):
        calls.append(force)
        return None

    monkeypatch.setattr(retention, "_maybe_prune_conversation_retention", spy)

    conn = ns["open_cache_db"]()
    try:
        cache_mod.sync_cache(conn, rebuild=True)
        assert calls == [True], "Claude rebuild must force-prune"
        calls.clear()
        cache_mod.sync_cache(conn)
        assert calls == [], "a Claude no-op sync must not prune"
        calls.clear()
        cache_mod.sync_codex_cache(conn, rebuild=True)
        assert calls == [True], "Codex rebuild must force-prune"
        calls.clear()
        cache_mod.sync_codex_cache(conn)
        assert calls == [], "a Codex no-op sync must not prune"
    finally:
        conn.close()


def test_truncation_replay_force_prunes_aged_transcripts_unthrottled(tmp_path, monkeypatch):
    """#313 P3 (F9), truncation branch: the force-prune gate is
    ``did_from_zero_replay = rebuild or files_reset_truncated > 0``. A
    truncation/requalification re-ingest replays from offset 0 and RESTORES
    >retention-day conversation_messages the throttled prune already trimmed, so
    it must ALSO fire the UNTHROTTLED prune — not only ``--rebuild``. The
    existing F9 test exercises only the ``rebuild`` branch; this drives a REAL
    Claude truncation reingest and asserts the restored aged rows are gone AFTER
    the sync even with a FRESH throttle stamp (which a plain throttled prune
    would honor by skipping)."""
    import json
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = importlib.import_module("_cctally_cache")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex"))

    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    jsonl = projects / "aged.jsonl"

    def _user_line(uuid, ts):
        return json.dumps({
            "type": "user", "uuid": uuid, "sessionId": "aged",
            "timestamp": ts, "message": {"role": "user", "content": "hello world"},
        }) + "\n"

    # v1: two aged (well-before-cutoff) user messages — headroom to shrink.
    jsonl.write_text(_user_line("m-old-1", OLD) + _user_line("m-old-2", OLDER))

    conn = ns["open_cache_db"]()
    try:
        cache_mod.sync_cache(conn)
        # Precondition: the aged session ingested into conversation_messages and
        # the ordinary (non-replay) sync did NOT prune it.
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE session_id='aged'"
        ).fetchone()[0] == 2, "aged transcript rows must ingest on the first sync"

        # A FRESH throttle stamp: a *throttled* prune would skip this cycle. Only
        # the UNTHROTTLED force-prune after a from-zero replay may still trim.
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('conversation_retention_last_prune_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (dt.datetime.now(UTC).isoformat(),),
        )
        conn.commit()

        # v2: rewrite the file SMALLER (one aged line) → next sync detects
        # truncation → escalation wipes + re-ingests conversation_messages from
        # offset 0 (RESTORING the aged row) → files_reset_truncated increments.
        jsonl.write_text(_user_line("m-old-1", OLD))
        stats = cache_mod.sync_cache(conn)
        assert stats.files_reset_truncated >= 1, (
            "rewriting the file smaller must trip the truncation escalation"
        )

        # The from-zero replay restored the aged row; the UNTHROTTLED prune must
        # have removed it despite the fresh throttle stamp.
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE session_id='aged'"
        ).fetchone()[0] == 0, (
            "a truncation replay must trigger the UNTHROTTLED retention prune "
            "(F9) — the restored >retention-day rows must be gone even with a "
            "fresh throttle stamp"
        )
    finally:
        conn.close()


def test_cutoff_far_in_past_prunes_nothing(tmp_path, monkeypatch):
    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_msg(conn, "s1", OLD)
    _seed_msg(conn, "s1", FRESH)
    conn.commit()
    stats = retention.prune_conversation_transcripts(
        conn, cutoff_utc=dt.datetime(2020, 1, 1, tzinfo=UTC))
    conn.commit()
    assert stats.claude_sessions == 0
    assert stats.claude_messages == 0
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 2


# ── #294 S6: pruning also removes the Codex normalized derived rows ────────────
#
# S6 added three derived tables (codex_conversation_messages / _rollups /
# _file_touches) + an external-content FTS index over the messages, all derived
# from codex_conversation_events. #313's retention prune predates S6 and only
# deleted the physical events — leaving the derived rows orphaned (stale browse
# rows, stale search hits). The Codex prune leg must now delete the derived rows
# in the SAME transaction as the events, at whole-conversation granularity
# (§3.2 "or-delete"; §3.4 partial-delete rides the per-row FTS triggers, never
# the full-clear 'delete-all').


def _seed_codex_event(conn, ck, ts, *, source_path, line_offset, root_key="root-a"):
    conn.execute(
        "INSERT INTO codex_conversation_events "
        "(source_path, line_offset, source_root_key, conversation_key, timestamp_utc, "
        " payload_json) VALUES (?,?,?,?,?,'{}')",
        (source_path, line_offset, root_key, ck, ts),
    )


def _seed_codex_msg(conn, ck, ts, *, source_path, line_offset, text,
                    kind="assistant", record_family="response_item", root_key="root-a"):
    conn.execute(
        "INSERT INTO codex_conversation_messages "
        "(conversation_key, source_root_key, source_path, line_offset, timestamp_utc, "
        " turn_id, call_id, kind, event_type, record_family, model, text, "
        " content_digest, content_len, detail_json, search_tool, search_thinking) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ck, root_key, source_path, line_offset, ts, "turn-a", None, kind, None,
         record_family, "gpt-x", text, "d" * 32, len(text.encode("utf-8")), None, "", ""),
    )
    return conn.execute(
        "SELECT id FROM codex_conversation_messages "
        "WHERE source_path=? AND line_offset=?", (source_path, line_offset)).fetchone()[0]


def _seed_codex_rollup(conn, ck, ts, *, title, item_count=1, root_key="root-a"):
    conn.execute(
        "INSERT INTO codex_conversation_rollups "
        "(conversation_key, source_root_key, parent_thread_id, item_count, started_utc, "
        " last_activity_utc, project_key, project_label, models_json, title) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ck, root_key, None, item_count, ts, ts, "project:x", "Proj", '["gpt-x"]', title),
    )


def _seed_codex_touch(conn, ck, message_id, *, source_path, file_path="/f.py",
                      tool="apply_patch"):
    conn.execute(
        "INSERT INTO codex_conversation_file_touches "
        "(message_id, conversation_key, source_path, file_path, tool) VALUES (?,?,?,?,?)",
        (message_id, ck, source_path, file_path, tool),
    )


def test_codex_prune_removes_all_normalized_derived_rows(tmp_path, monkeypatch):
    """A pruned Codex conversation leaves ZERO rows in all three derived tables
    (messages, file_touches, rollups) plus the FTS index — not just the events."""
    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_codex_event(conn, "conv-a", OLD, source_path="/a.jsonl", line_offset=1)
    mid = _seed_codex_msg(conn, "conv-a", OLD, source_path="/a.jsonl", line_offset=1,
                          text="MirrorAlpha aged content")
    _seed_codex_rollup(conn, "conv-a", OLD, title="Aged A")
    _seed_codex_touch(conn, "conv-a", mid, source_path="/a.jsonl")
    conn.commit()

    stats = retention.prune_conversation_transcripts(conn, cutoff_utc=_cutoff())
    conn.commit()

    # Reported counts stay physical-conversation/physical-event — no double-count.
    assert stats.codex_conversations == 1
    assert stats.codex_events == 1
    for table in ("codex_conversation_events", "codex_conversation_messages",
                  "codex_conversation_file_touches", "codex_conversation_rollups"):
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE conversation_key='conv-a'"
        ).fetchone()[0] == 0, table
    # FTS postings for the pruned conversation are gone (per-row AD trigger).
    assert conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_fts "
        "WHERE codex_conversation_fts MATCH 'MirrorAlpha'").fetchone()[0] == 0


def test_codex_prune_leaves_survivor_searchable_fts_and_like(tmp_path, monkeypatch):
    """Partial-delete policy (§3.4): pruning neighbor A must NOT disturb survivor
    B's postings — B stays searchable in both FTS and LIKE modes."""
    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_codex_event(conn, "conv-a", OLD, source_path="/a.jsonl", line_offset=1)
    _seed_codex_msg(conn, "conv-a", OLD, source_path="/a.jsonl", line_offset=1,
                    text="MirrorAlpha aged content", kind="assistant")
    _seed_codex_rollup(conn, "conv-a", OLD, title="Aged A")
    _seed_codex_event(conn, "conv-b", FRESH, source_path="/b.jsonl", line_offset=1)
    _seed_codex_msg(conn, "conv-b", FRESH, source_path="/b.jsonl", line_offset=1,
                    text="SurvivorBravo fresh content", kind="user")
    _seed_codex_rollup(conn, "conv-b", FRESH, title="Fresh B")
    conn.commit()

    retention.prune_conversation_transcripts(conn, cutoff_utc=_cutoff())
    conn.commit()

    assert conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_messages WHERE conversation_key='conv-b'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_messages WHERE conversation_key='conv-a'"
    ).fetchone()[0] == 0
    # Raw FTS index: survivor matches, pruned leaves no residue.
    assert conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_fts "
        "WHERE codex_conversation_fts MATCH 'SurvivorBravo'").fetchone()[0] > 0
    assert conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_fts "
        "WHERE codex_conversation_fts MATCH 'MirrorAlpha'").fetchone()[0] == 0
    # Raw LIKE over the base table: survivor matches, pruned absent.
    assert conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_messages WHERE text LIKE '%SurvivorBravo%'"
    ).fetchone()[0] > 0
    assert conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_messages WHERE text LIKE '%MirrorAlpha%'"
    ).fetchone()[0] == 0
    # Search kernel, FTS mode: only the survivor.
    res_fts = _cxq.search_codex_conversations(conn, "SurvivorBravo", effective_speed="standard")
    assert res_fts["mode"] == "fts"
    assert {h["conversation_key"] for h in res_fts["hits"]} == {"conv-b"}
    assert _cxq.search_codex_conversations(
        conn, "MirrorAlpha", effective_speed="standard")["hits"] == []
    # Search kernel, LIKE mode (marker forces the LIKE path): only the survivor.
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES('codex_fts_unavailable','1') "
        "ON CONFLICT(key) DO UPDATE SET value='1'")
    res_like = _cxq.search_codex_conversations(conn, "SurvivorBravo", effective_speed="standard")
    assert res_like["mode"] == "like"
    assert {h["conversation_key"] for h in res_like["hits"]} == {"conv-b"}
    assert _cxq.search_codex_conversations(
        conn, "MirrorAlpha", effective_speed="standard")["hits"] == []


def test_codex_prune_removes_from_browse_list_fast_path_and_live_fallback(tmp_path, monkeypatch):
    """The pruned conversation's rollup is gone and list_codex_conversations no
    longer returns it via the stored-rollup fast path OR the live recompute
    fallback (the fallback must not resurrect it from surviving-but-empty state)."""
    ns, conn, retention = _env(tmp_path, monkeypatch)
    _seed_codex_event(conn, "conv-a", OLD, source_path="/a.jsonl", line_offset=1)
    _seed_codex_msg(conn, "conv-a", OLD, source_path="/a.jsonl", line_offset=1,
                    text="AgedAlpha", kind="user")
    _seed_codex_rollup(conn, "conv-a", OLD, title="Aged A")
    _seed_codex_event(conn, "conv-b", FRESH, source_path="/b.jsonl", line_offset=1)
    _seed_codex_msg(conn, "conv-b", FRESH, source_path="/b.jsonl", line_offset=1,
                    text="FreshBravo", kind="user")
    _seed_codex_rollup(conn, "conv-b", FRESH, title="Fresh B")
    conn.commit()
    pre = _cxq.list_codex_conversations(conn, effective_speed="standard")
    assert {r["conversation_key"] for r in pre["rows"]} == {"conv-a", "conv-b"}

    retention.prune_conversation_transcripts(conn, cutoff_utc=_cutoff())
    conn.commit()

    assert conn.execute(
        "SELECT COUNT(*) FROM codex_conversation_rollups WHERE conversation_key='conv-a'"
    ).fetchone()[0] == 0
    # Fast path (B keeps its stored rollup): only the survivor lists.
    post = _cxq.list_codex_conversations(conn, effective_speed="standard")
    assert {r["conversation_key"] for r in post["rows"]} == {"conv-b"}
    # Live fallback: drop B's rollup so it must live-recompute; the pruned A must
    # NOT resurrect (its messages are gone, so the browse driver never sees it).
    conn.execute("DELETE FROM codex_conversation_rollups WHERE conversation_key='conv-b'")
    conn.commit()
    live = _cxq.list_codex_conversations(conn, effective_speed="standard")
    assert {r["conversation_key"] for r in live["rows"]} == {"conv-b"}


def test_codex_forced_replay_prune_removes_rederived_derived_rows(tmp_path, monkeypatch):
    """F9 end-to-end for the S6 derived tables: a rebuild re-ingests aged events
    AND re-derives their normalized rows; the forced UNTHROTTLED prune must then
    remove the events AND every re-derived derived row, even with a fresh throttle
    stamp (a throttled prune would skip)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache_mod = importlib.import_module("_cctally_cache")
    provider_root = tmp_path / "provider"
    rollout = provider_root / "sessions" / "2024" / "01" / "10" / "rollout-aged.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    # Age every timestamp in the corpus scenario to well before the 180-day cutoff.
    aged = (_CORPUS / "modern-full.jsonl").read_text().replace("2026-07-14", "2024-01-10")
    rollout.write_text(aged)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    conn = ns["open_cache_db"]()
    try:
        cache_mod.sync_codex_cache(conn)
        # Precondition: aged rows ingested + normalized; an ordinary sync did not prune.
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0] > 0
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_events").fetchone()[0] > 0
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_rollups").fetchone()[0] == 1

        # A FRESH throttle stamp: only the UNTHROTTLED force-prune may still trim.
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('conversation_retention_last_prune_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (dt.datetime.now(UTC).isoformat(),))
        conn.commit()

        # Rebuild = from-zero replay: clears + re-ingests + re-derives, then the
        # forced prune (F9) fires after the flock releases.
        cache_mod.sync_codex_cache(conn, rebuild=True)

        for table in ("codex_conversation_events", "codex_conversation_messages",
                      "codex_conversation_file_touches", "codex_conversation_rollups"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    finally:
        conn.close()


def test_migration_025_replay_after_prune_does_not_resurrect(tmp_path, monkeypatch):
    """Migration 025 replay (full-clear + re-derive from events) after a prune
    cannot resurrect a pruned conversation: its physical events are gone, so
    replay has nothing to re-derive for it. Non-vacuity is proven first — a
    pre-prune replay DOES reconstruct the conversation from its events."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache_mod = importlib.import_module("_cctally_cache")
    provider_root = tmp_path / "provider"
    rollout = provider_root / "sessions" / "2024" / "01" / "10" / "rollout-aged.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    aged = (_CORPUS / "modern-full.jsonl").read_text().replace("2026-07-14", "2024-01-10")
    rollout.write_text(aged)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    retention = importlib.import_module("_lib_conversation_retention")

    conn = ns["open_cache_db"]()
    try:
        cache_mod.sync_codex_cache(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0] > 0

        # Non-vacuity: 025's clear+replay reconstructs the conversation from its
        # still-present events (proves replay actually re-derives from events).
        _db._codex_conversation_fts_full_clear(conn)
        cache_mod._replay_codex_normalization(conn)
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0] > 0
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_rollups").fetchone()[0] == 1

        # Prune the aged conversation (events + derived rows gone).
        retention.prune_conversation_transcripts(conn, cutoff_utc=_cutoff())
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_events").fetchone()[0] == 0

        # A subsequent 025 replay must NOT resurrect it — no events to re-derive.
        _db._codex_conversation_fts_full_clear(conn)
        cache_mod._replay_codex_normalization(conn)
        conn.commit()
        for table in ("codex_conversation_messages", "codex_conversation_rollups",
                      "codex_conversation_file_touches"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    finally:
        conn.close()
