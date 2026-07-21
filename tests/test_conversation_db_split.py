"""Issue #320: transcript storage is physically independent from cache.db."""

from __future__ import annotations

import datetime as dt
import json
import fcntl
import pathlib
import shutil
import sqlite3

import pytest

from conftest import load_script, redirect_paths


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _assistant_line(*, uuid: str, msg_id: str, request_id: str, text: str) -> str:
    return json.dumps({
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "session-1",
        "requestId": request_id,
        "timestamp": "2026-07-20T00:00:00Z",
        "message": {
            "role": "assistant",
            "id": msg_id,
            "model": "claude-opus-4-7",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }) + "\n"


def test_conversation_paths_are_derived_from_app_dir(tmp_path, monkeypatch):
    ns = load_script()
    core = ns["_cctally_core"]
    data_dir = tmp_path / "split-data"
    monkeypatch.setenv("CCTALLY_DATA_DIR", str(data_dir))

    core._init_paths_from_env()

    assert core.CONVERSATIONS_DB_PATH == data_dir / "conversations.db"
    assert core.CONVERSATIONS_LOCK_PATH == data_dir / "conversations.db.lock"
    assert core.CONVERSATIONS_LOCK_CODEX_PATH == data_dir / "conversations.db.codex.lock"
    assert core.CONVERSATIONS_LOCK_MAINTENANCE_PATH == data_dir / "conversations.db.maintenance.lock"


def test_open_cache_db_never_opens_conversation_store(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    real_connect = sqlite3.connect
    conversation_path = ns["_cctally_core"].CONVERSATIONS_DB_PATH

    def guarded_connect(database, *args, **kwargs):
        if str(database) == str(conversation_path):
            raise AssertionError("core cache opener touched conversations.db")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_conversation_connection_owns_transcripts_and_reads_attached_core(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "INSERT INTO session_entries "
            "(source_path,line_offset,timestamp_utc,model,msg_id,req_id) "
            "VALUES ('source.jsonl',0,'2026-07-20T00:00:00+00:00','model','m','r')"
        )
        cache.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) "
            "VALUES ('session_entries_mutation_seq','7')"
        )
        cache.commit()
    finally:
        cache.close()

    conversations = ns["open_conversations_db"]()
    try:
        conversations.execute(
            "INSERT INTO conversation_messages "
            "(session_id,source_path,byte_offset,entry_type,msg_id,req_id) "
            "VALUES ('s','source.jsonl',0,'assistant','m','r')"
        )
        conversations.execute(
            "INSERT INTO conversation_sessions "
            "(session_id,msg_count,last_activity_utc) "
            "VALUES ('s',1,'2026-07-20T00:00:00+00:00')"
        )
        conversations.commit()

        assert conversations.execute(
            "SELECT COUNT(*) FROM main.conversation_messages"
        ).fetchone() == (1,)
        assert conversations.execute(
            "SELECT COUNT(*) FROM cache_db.session_entries"
        ).fetchone() == (1,)
        assert conversations.execute(
            "SELECT COUNT(*) FROM main.conversation_messages AS m "
            "JOIN cache_db.session_entries AS e "
            "ON e.msg_id=m.msg_id AND e.req_id=m.req_id"
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conversations.execute(
                "INSERT INTO cache_db.session_entries "
                "(source_path,line_offset,timestamp_utc,model) "
                "VALUES ('forbidden.jsonl',1,'2026-07-20T00:00:00Z','model')"
            )
        conversations.rollback()

        query = ns["_load_sibling"]("_lib_conversation_query")
        assert query._assemble_memo_key(conversations, "s")[-1] == 7
        cache = ns["open_cache_db"]()
        try:
            cache.execute(
                "UPDATE cache_meta SET value='8' "
                "WHERE key='session_entries_mutation_seq'"
            )
            cache.commit()
        finally:
            cache.close()
        assert query._assemble_memo_key(conversations, "s")[-1] == 8
    finally:
        conversations.close()

    cache = ns["open_cache_db"]()
    try:
        assert cache.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone() == (0,)
    finally:
        cache.close()


def test_conversation_connection_enables_uri_for_readonly_cache_attach(
    tmp_path, monkeypatch,
):
    """The main connection must enable SQLite URI filenames.

    Python/SQLite builds that do not enable URI parsing on the main connection
    otherwise treat the read-only ``file:...cache.db?mode=ro`` ATTACH value as a
    literal filename and every conversation route fails with a 500.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    real_connect = sqlite3.connect
    conversation_path = ns["_cctally_core"].CONVERSATIONS_DB_PATH
    calls = []

    def recording_connect(database, *args, **kwargs):
        if str(database) == str(conversation_path):
            calls.append(kwargs.copy())
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    conn = ns["open_conversations_db"]()
    conn.close()

    assert calls and calls[0].get("uri") is True


def test_core_sessions_panel_never_opens_conversation_store(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "INSERT INTO session_files "
            "(path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at,"
            "session_id,project_path) VALUES "
            "('source.jsonl',100,1,100,'2026-07-20T00:00:00+00:00',"
            "'session-core','/project')"
        )
        cache.execute(
            "INSERT INTO session_entries "
            "(source_path,line_offset,timestamp_utc,model,input_tokens,output_tokens) "
            "VALUES ('source.jsonl',0,'2026-07-20T00:00:00+00:00',"
            "'claude-opus-4-7',10,5)"
        )
        cache.commit()
    finally:
        cache.close()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("core Sessions panel opened conversations.db")

    monkeypatch.setitem(ns, "open_conversations_db", forbidden)
    rows = ns["_tui_build_sessions"](
        dt.datetime(2026, 7, 21, tzinfo=dt.timezone.utc),
        skip_sync=True,
        use_session_cache=False,
    )
    assert [row.session_id for row in rows] == ["session-core"]
    assert rows[0].title is None


def test_claude_core_and_transcript_cursors_advance_independently(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects" / "-tmp-project"
    projects.mkdir(parents=True)
    source = projects / "session.jsonl"
    source.write_text(
        _assistant_line(uuid="u1", msg_id="m1", request_id="r1", text="hello")
    )

    cache = ns["open_cache_db"]()
    try:
        core_stats = ns["sync_cache"](cache)
        assert core_stats.files_processed == 1
        assert cache.execute("SELECT COUNT(*) FROM session_entries").fetchone() == (1,)
        assert cache.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone() == (0,)
        core_before = cache.execute(
            "SELECT path,size_bytes,last_byte_offset FROM session_files"
        ).fetchall()
    finally:
        cache.close()

    conversations = ns["open_conversations_db"]()
    try:
        transcript_stats = ns["sync_claude_conversations"](conversations)
        assert transcript_stats.files_processed == 1
        assert conversations.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone() == (1,)
        assert conversations.execute(
            "SELECT path,size_bytes,last_byte_offset FROM conversation_source_files"
        ).fetchall() == core_before
    finally:
        conversations.close()

    cache = ns["open_cache_db"]()
    try:
        assert cache.execute("SELECT COUNT(*) FROM session_entries").fetchone() == (1,)
        assert cache.execute(
            "SELECT path,size_bytes,last_byte_offset FROM session_files"
        ).fetchall() == core_before
    finally:
        cache.close()


def test_failed_transcript_commit_does_not_advance_its_cursor(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects" / "-tmp-project"
    projects.mkdir(parents=True)
    source = projects / "session.jsonl"
    source.write_text(
        _assistant_line(uuid="u1", msg_id="m1", request_id="r1", text="hello")
    )

    cache = ns["open_cache_db"]()
    try:
        assert ns["sync_cache"](cache).files_processed == 1
        assert cache.execute("SELECT COUNT(*) FROM session_entries").fetchone() == (1,)
    finally:
        cache.close()

    conversations = ns["open_conversations_db"]()
    try:
        conversations.execute(
            "CREATE TRIGGER fail_transcript_insert "
            "BEFORE INSERT ON conversation_messages BEGIN "
            "SELECT RAISE(ABORT, 'forced transcript failure'); END"
        )
        conversations.commit()
        stats = ns["sync_claude_conversations"](conversations)
        assert stats.files_failed == 1
        assert conversations.execute(
            "SELECT COUNT(*) FROM conversation_source_files"
        ).fetchone() == (0,)
        assert conversations.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone() == (0,)

        conversations.execute("DROP TRIGGER fail_transcript_insert")
        conversations.commit()
        assert ns["sync_claude_conversations"](conversations).files_processed == 1
        assert conversations.execute(
            "SELECT last_byte_offset FROM conversation_source_files WHERE path=?",
            (str(source),),
        ).fetchone() == (source.stat().st_size,)
    finally:
        conversations.close()


def test_codex_core_and_transcript_cursors_advance_independently(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "codex-provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "20" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(
        REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts" / "modern-full.jsonl",
        rollout,
    )
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    cache = ns["open_cache_db"]()
    try:
        core_stats = ns["sync_codex_cache"](cache)
        assert core_stats.files_processed == 1
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone() == (1,)
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_conversation_threads"
        ).fetchone() == (1,)
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_conversation_events"
        ).fetchone() == (0,)
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages"
        ).fetchone() == (0,)
        assert cache.execute(
            "SELECT DISTINCT observed_model FROM quota_window_snapshots"
        ).fetchall() == [("gpt-synthetic-codex",)]
        core_before = cache.execute(
            "SELECT path,size_bytes,last_byte_offset FROM codex_session_files"
        ).fetchall()
    finally:
        cache.close()

    conversations = ns["open_conversations_db"]()
    try:
        codex_query = ns["_load_sibling"]("_lib_codex_conversation_query")
        conversations.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) "
            "VALUES ('conversation_rebuild_codex_pending','1')"
        )
        conversations.commit()
        assert codex_query.codex_normalization_authoritative(conversations) is False
        transcript_stats = ns["sync_codex_conversations"](conversations)
        assert transcript_stats.files_processed == 1
        assert codex_query.codex_normalization_authoritative(conversations) is True
        assert conversations.execute(
            "SELECT COUNT(*) FROM codex_conversation_events"
        ).fetchone()[0] > 0
        assert conversations.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages"
        ).fetchone()[0] > 0
        assert conversations.execute(
            "SELECT path,size_bytes,last_byte_offset "
            "FROM codex_conversation_source_files"
        ).fetchall() == core_before
    finally:
        conversations.close()

    cache = ns["open_cache_db"]()
    try:
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone() == (1,)
        assert cache.execute(
            "SELECT path,size_bytes,last_byte_offset FROM codex_session_files"
        ).fetchall() == core_before
    finally:
        cache.close()


def test_codex_core_sync_never_runs_transcript_normalization(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "codex-provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "20" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(
        REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts" / "modern-full.jsonl",
        rollout,
    )
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("core sync invoked transcript normalization")

    monkeypatch.setattr(
        ns["_cctally_cache"]._lib_codex_conversation,
        "normalize_codex_events",
        forbidden,
    )
    cache = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](cache)
        assert stats.files_processed == 1
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone()[0] > 0
    finally:
        cache.close()


def test_migration_028_preserves_core_and_arms_independent_rebuild(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = ns["_cctally_core"]
    db = ns["_cctally_db"]
    conn = sqlite3.connect(core.CACHE_DB_PATH)
    db._apply_cache_schema(conn)
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path,line_offset,timestamp_utc,model) "
        "VALUES ('core.jsonl',0,'2026-07-20T00:00:00+00:00','model')"
    )
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id,source_path,byte_offset,entry_type,text) "
        "VALUES ('s','legacy.jsonl',0,'human','legacy prose')"
    )
    conn.execute(
        "INSERT INTO codex_conversation_events "
        "(source_path,line_offset,source_root_key,payload_json) "
        "VALUES ('legacy-codex.jsonl',0,'root','{}')"
    )
    conn.commit()

    db._028_split_conversation_store(conn)

    assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone() == (1,)
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    assert "conversation_messages" not in names
    assert "codex_conversation_events" not in names
    conn.close()

    conversations = sqlite3.connect(core.CONVERSATIONS_DB_PATH)
    try:
        names = {
            row[0]
            for row in conversations.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"conversation_messages", "codex_conversation_events"} <= names
        assert conversations.execute(
            "SELECT key FROM cache_meta WHERE key LIKE 'conversation_rebuild_%_pending' "
            "ORDER BY key"
        ).fetchall() == [
            ("conversation_rebuild_claude_pending",),
            ("conversation_rebuild_codex_pending",),
        ]
        assert conversations.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone() == (0,)
        assert conversations.execute(
            "SELECT COUNT(*) FROM codex_conversation_events"
        ).fetchone() == (0,)
    finally:
        conversations.close()


def test_core_refresh_survives_locked_then_missing_conversation_store(
    tmp_path, monkeypatch,
):
    """The decisive #320 gate: core refresh has no transcript dependency."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    core = ns["_cctally_core"]

    projects = tmp_path / "data" / ".claude" / "projects" / "-tmp-project"
    projects.mkdir(parents=True)
    claude_source = projects / "session.jsonl"
    claude_source.write_text(
        _assistant_line(uuid="u1", msg_id="m1", request_id="r1", text="one")
    )

    codex_root = tmp_path / "codex-provider"
    rollout = codex_root / "sessions" / "2026" / "07" / "20" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(
        REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts" / "modern-full.jsonl",
        rollout,
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_root))

    # Complete the one-time split migration before simulating an unavailable
    # transcript store. Runtime core opens after migration must stay independent.
    cache = ns["open_cache_db"]()
    cache.close()
    conversations = ns["open_conversations_db"](attach_cache=False)
    conversations.close()

    lock_db = sqlite3.connect(core.CONVERSATIONS_DB_PATH)
    lock_db.execute("PRAGMA locking_mode=EXCLUSIVE")
    lock_db.execute("BEGIN EXCLUSIVE")
    lock_db.execute(
        "INSERT INTO cache_meta(key,value) VALUES('acceptance_lock','held')"
    )
    lock_fhs = []
    try:
        for path in (
            core.CONVERSATIONS_LOCK_PATH,
            core.CONVERSATIONS_LOCK_CODEX_PATH,
        ):
            fh = open(path, "w")
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_fhs.append(fh)

        cache = ns["open_cache_db"]()
        try:
            assert ns["sync_cache"](cache).files_processed == 1
            assert ns["sync_codex_cache"](cache).files_processed == 1
            assert cache.execute("SELECT COUNT(*) FROM session_entries").fetchone() == (1,)
            assert cache.execute(
                "SELECT COUNT(*) FROM codex_session_entries"
            ).fetchone() == (1,)
            assert cache.execute(
                "SELECT COUNT(*) FROM quota_window_snapshots"
            ).fetchone()[0] > 0
        finally:
            cache.close()
    finally:
        for fh in reversed(lock_fhs):
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        lock_db.rollback()
        lock_db.close()

    for suffix in ("", "-wal", "-shm"):
        pathlib.Path(str(core.CONVERSATIONS_DB_PATH) + suffix).unlink(missing_ok=True)
    claude_source.write_text(
        claude_source.read_text()
        + _assistant_line(uuid="u2", msg_id="m2", request_id="r2", text="two")
    )
    cache = ns["open_cache_db"]()
    try:
        assert ns["sync_cache"](cache).files_processed == 1
        assert cache.execute("SELECT COUNT(*) FROM session_entries").fetchone() == (2,)
        core_before = {
            "claude": cache.execute(
                "SELECT source_path,line_offset,timestamp_utc,model,input_tokens,"
                "output_tokens FROM session_entries ORDER BY source_path,line_offset"
            ).fetchall(),
            "codex": cache.execute(
                "SELECT source_path,line_offset,timestamp_utc,model,total_tokens "
                "FROM codex_session_entries ORDER BY source_path,line_offset"
            ).fetchall(),
            "quota": cache.execute(
                "SELECT source,source_root_key,source_path,line_offset,"
                "logical_limit_key,used_percent,resets_at_utc "
                "FROM quota_window_snapshots ORDER BY id"
            ).fetchall(),
        }
    finally:
        cache.close()
    assert not core.CONVERSATIONS_DB_PATH.exists()

    conversations = ns["open_conversations_db"]()
    try:
        assert ns["sync_claude_conversations"](conversations).files_processed == 1
        assert ns["sync_codex_conversations"](conversations).files_processed == 1
        assert conversations.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone()[0] > 0
        assert conversations.execute(
            "SELECT COUNT(*) FROM codex_conversation_events"
        ).fetchone()[0] > 0
    finally:
        conversations.close()

    cache = ns["open_cache_db"]()
    try:
        assert cache.execute(
            "SELECT source_path,line_offset,timestamp_utc,model,input_tokens,"
            "output_tokens FROM session_entries ORDER BY source_path,line_offset"
        ).fetchall() == core_before["claude"]
        assert cache.execute(
            "SELECT source_path,line_offset,timestamp_utc,model,total_tokens "
            "FROM codex_session_entries ORDER BY source_path,line_offset"
        ).fetchall() == core_before["codex"]
        assert cache.execute(
            "SELECT source,source_root_key,source_path,line_offset,"
            "logical_limit_key,used_percent,resets_at_utc "
            "FROM quota_window_snapshots ORDER BY id"
        ).fetchall() == core_before["quota"]
    finally:
        cache.close()
