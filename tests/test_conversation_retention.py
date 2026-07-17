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

UTC = dt.timezone.utc


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
