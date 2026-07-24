"""cmd_cache_sync: --rebuild aggregates a non-zero exit on lock contention
(was silently exit 0); --prune-orphans runs the helper and reports."""
from __future__ import annotations
import argparse, fcntl, json, os, pathlib, shutil, sqlite3
import pytest
from conftest import load_script, redirect_paths


@pytest.fixture
def env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    return ns, tmp_path, monkeypatch


def test_rebuild_contended_returns_nonzero(env, capsys):
    ns, tmp_path, monkeypatch = env
    # Speed: lower the rebuild lock timeout so the test doesn't wait 30s.
    # cmd_cache_sync reads the constant from its OWN module (_cctally_cache),
    # so patch there (the `cctally` re-export copy is not what it reads).
    monkeypatch.setattr(
        ns["_cctally_cache"], "_REBUILD_LOCK_TIMEOUT_SECONDS", 0.3, raising=False
    )
    # Bootstrap the schema before simulating an in-flight writer. A real holder
    # cannot enter sync/rebuild until first-open schema work has completed; this
    # keeps the test on the rebuild-contention path rather than the distinct
    # first-open schema wait.
    bootstrap = ns["open_cache_db"]()
    bootstrap.close()
    lock_path = ns["_cctally_core"].CACHE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w"); fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        args = argparse.Namespace(source="claude", rebuild=True, prune_orphans=False)
        rc = ns["cmd_cache_sync"](args)
        assert rc == 1
        assert "rebuild skipped" in capsys.readouterr().err.lower()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN); holder.close()


def test_transcript_open_failure_preserves_successful_core_sync(env, capsys):
    ns, tmp_path, monkeypatch = env
    pdir = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-p"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "s.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-07-01T00:00:00Z",
        "requestId": "r1", "sessionId": "S1", "uuid": "u1",
        "message": {"id": "m1", "model": "claude-opus-4-7", "usage": {
            "input_tokens": 0, "output_tokens": 5,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }},
    }) + "\n")

    def unavailable():
        raise sqlite3.OperationalError("database is locked")

    import sqlite3
    monkeypatch.setattr(ns["_cctally_cache"], "open_conversations_db", unavailable)
    args = argparse.Namespace(
        source="claude", rebuild=False, prune_orphans=False,
        prune_conversations=False,
    )
    assert ns["cmd_cache_sync"](args) == 0
    assert "core accounting/quota sync is complete" in capsys.readouterr().err
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == 1
    finally:
        conn.close()


def test_transcript_open_failure_makes_explicit_rebuild_nonzero(env, capsys):
    ns, _tmp_path, monkeypatch = env

    def unavailable():
        raise sqlite3.OperationalError("database is locked")

    import sqlite3
    monkeypatch.setattr(ns["_cctally_cache"], "open_conversations_db", unavailable)
    args = argparse.Namespace(
        source="claude", rebuild=True, prune_orphans=False,
        prune_conversations=False,
    )
    assert ns["cmd_cache_sync"](args) == 1
    assert "core accounting/quota sync is complete" in capsys.readouterr().err


def test_rebuild_recovers_post_open_corruption_and_retries_once(
    env, capsys,
):
    ns, _tmp_path, monkeypatch = env
    cache_mod = ns["_cctally_cache"]
    real_sync = cache_mod.sync_cache
    attempts = 0
    first_conn = None

    def corrupt_once(conn, **kwargs):
        nonlocal attempts, first_conn
        attempts += 1
        if attempts == 1:
            first_conn = conn
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_sync(conn, **kwargs)

    monkeypatch.setattr(cache_mod, "sync_cache", corrupt_once)
    args = argparse.Namespace(
        source="claude", rebuild=True, prune_orphans=False,
        prune_conversations=False,
    )

    assert ns["cmd_cache_sync"](args) == 0
    assert attempts == 2
    assert "claude done" in capsys.readouterr().err
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        assert first_conn is not None
        first_conn.execute("SELECT 1")

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_post_open_noncorruption_is_non_destructive_and_closes_connection(
    env, monkeypatch,
):
    ns, _tmp_path, _fixture_monkeypatch = env
    cache_mod = ns["_cctally_cache"]
    path = pathlib.Path(ns["_cctally_core"].CACHE_DB_PATH)
    first_conn = None
    recovered = False

    def locked(conn, **_kwargs):
        nonlocal first_conn
        first_conn = conn
        raise sqlite3.OperationalError("database is locked")

    def unexpected_recovery(_exc):
        nonlocal recovered
        recovered = True
        return True

    monkeypatch.setattr(cache_mod, "sync_cache", locked)
    monkeypatch.setattr(cache_mod, "_recover_corrupt_cache", unexpected_recovery)
    args = argparse.Namespace(
        source="claude", rebuild=True, prune_orphans=False,
        prune_conversations=False,
    )

    assert ns["cmd_cache_sync"](args) == 1

    assert recovered is False
    assert path.exists()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        assert first_conn is not None
        first_conn.execute("SELECT 1")


def _write_claude_entry(tmp_path: pathlib.Path) -> None:
    pdir = tmp_path / ".claude" / "projects" / "-p"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "s.jsonl").write_text(json.dumps({
        "type": "assistant",
        "timestamp": "2026-07-01T00:00:00Z",
        "requestId": "r1",
        "sessionId": "S1",
        "uuid": "u1",
        "message": {
            "id": "m1",
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 5,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }) + "\n")


def test_real_claude_file_loop_reraises_classified_corruption_for_recovery(
    env, monkeypatch,
):
    ns, tmp_path, _fixture_monkeypatch = env
    cache_mod = ns["_cctally_cache"]
    _write_claude_entry(tmp_path)
    real_ensure = cache_mod._ensure_session_files_row
    attempts = 0

    def corrupt_once(conn, source_path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_ensure(conn, source_path)

    monkeypatch.setattr(cache_mod, "_ensure_session_files_row", corrupt_once)
    args = argparse.Namespace(
        source="claude", rebuild=True, prune_orphans=False,
        prune_conversations=False,
    )

    assert ns["cmd_cache_sync"](args) == 0
    assert attempts == 2
    incidents = list(
        (pathlib.Path(ns["_cctally_core"].APP_DIR) / "quarantine").glob(
            "cache.db-*"
        )
    )
    assert len(incidents) == 1
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == 1
    finally:
        conn.close()


def test_rebuild_file_failure_is_not_reported_as_success(env, monkeypatch):
    ns, tmp_path, _fixture_monkeypatch = env
    cache_mod = ns["_cctally_cache"]
    _write_claude_entry(tmp_path)

    def fail_file(_conn, _source_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cache_mod, "_ensure_session_files_row", fail_file)
    args = argparse.Namespace(
        source="claude", rebuild=True, prune_orphans=False,
        prune_conversations=False,
    )

    assert ns["cmd_cache_sync"](args) == 1
    assert not (
        pathlib.Path(ns["_cctally_core"].APP_DIR) / "quarantine"
    ).exists()


def test_all_plan_restarts_claude_after_codex_leg_recovers(env, monkeypatch):
    ns, tmp_path, _fixture_monkeypatch = env
    cache_mod = ns["_cctally_cache"]
    _write_claude_entry(tmp_path)
    real_claude = cache_mod.sync_cache
    real_codex = cache_mod.sync_codex_cache
    claude_runs = 0
    codex_runs = 0

    def count_claude(conn, **kwargs):
        nonlocal claude_runs
        claude_runs += 1
        return real_claude(conn, **kwargs)

    def corrupt_codex_once(conn, **kwargs):
        nonlocal codex_runs
        codex_runs += 1
        if codex_runs == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_codex(conn, **kwargs)

    monkeypatch.setattr(cache_mod, "sync_cache", count_claude)
    monkeypatch.setattr(cache_mod, "sync_codex_cache", corrupt_codex_once)
    args = argparse.Namespace(
        source="all", rebuild=True, prune_orphans=False,
        prune_conversations=False,
    )

    assert ns["cmd_cache_sync"](args) == 0
    assert claude_runs == 2
    assert codex_runs == 2
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] == 1
    finally:
        conn.close()


def test_prune_orphans_cli(env, capsys):
    ns, tmp_path, monkeypatch = env
    pdir = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-p-gone"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "s.jsonl").write_text(json.dumps({"type": "assistant",
        "timestamp": "2026-07-01T00:00:00Z", "requestId": "r1", "sessionId": "S1",
        "uuid": "u1", "parentUuid": None,
        "message": {"id": "m1", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 0, "output_tokens": 5,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")
    conn = ns["open_cache_db"](); ns["sync_cache"](conn); conn.close()
    conversations = ns["open_conversations_db"]()
    ns["sync_claude_conversations"](conversations)
    conversations.close()
    shutil.rmtree(pdir)
    args = argparse.Namespace(source="claude", rebuild=False, prune_orphans=True)
    rc = ns["cmd_cache_sync"](args)
    assert rc == 0
    assert "pruned 1 orphaned file" in capsys.readouterr().err.lower()


def test_prune_orphans_source_codex_is_noop(env, capsys):
    # --prune-orphans applies to the Claude cache only; --source codex must
    # print a note and no-op (exit 0) rather than silently pruning Claude.
    ns, tmp_path, monkeypatch = env
    pdir = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-p-gone"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "s.jsonl").write_text(json.dumps({"type": "assistant",
        "timestamp": "2026-07-01T00:00:00Z", "requestId": "r1", "sessionId": "S1",
        "uuid": "u1", "parentUuid": None,
        "message": {"id": "m1", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 0, "output_tokens": 5,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")
    conn = ns["open_cache_db"](); ns["sync_cache"](conn); conn.close()
    conversations = ns["open_conversations_db"]()
    ns["sync_claude_conversations"](conversations)
    conversations.close()
    shutil.rmtree(pdir)
    args = argparse.Namespace(source="codex", rebuild=False, prune_orphans=True)
    rc = ns["cmd_cache_sync"](args)
    assert rc == 0
    assert "claude cache only" in capsys.readouterr().err.lower()
    # The Claude orphan was NOT pruned (source codex was respected).
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute(
            "SELECT count(*) FROM session_files WHERE size_bytes>0"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_prune_orphans_source_all(env, capsys):
    # --source all (the default) prunes the Claude surface, same as --source claude.
    ns, tmp_path, monkeypatch = env
    pdir = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-p-gone"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "s.jsonl").write_text(json.dumps({"type": "assistant",
        "timestamp": "2026-07-01T00:00:00Z", "requestId": "r1", "sessionId": "S1",
        "uuid": "u1", "parentUuid": None,
        "message": {"id": "m1", "model": "claude-opus-4-7",
            "usage": {"input_tokens": 0, "output_tokens": 5,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")
    conn = ns["open_cache_db"](); ns["sync_cache"](conn); conn.close()
    conversations = ns["open_conversations_db"]()
    ns["sync_claude_conversations"](conversations)
    conversations.close()
    shutil.rmtree(pdir)
    args = argparse.Namespace(source="all", rebuild=False, prune_orphans=True)
    rc = ns["cmd_cache_sync"](args)
    assert rc == 0
    assert "pruned 1 orphaned file" in capsys.readouterr().err.lower()


def test_prune_conversations_cli(env, capsys):
    # On-demand transcript retention prune (#313 P3, Task 10). Far-past /
    # far-future timestamps keep the assertion time-independent (the command
    # uses real `now`).
    ns, tmp_path, monkeypatch = env
    conn = ns["open_conversations_db"](attach_cache=False)
    for off, (sid, ts) in enumerate(
        [("old", "2020-01-01T00:00:00.000Z"),
         ("fresh", "2099-01-01T00:00:00.000Z")], start=1
    ):
        conn.execute(
            "INSERT INTO conversation_messages "
            "(session_id, uuid, source_path, byte_offset, timestamp_utc, entry_type, text) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, f"u{off}", "seed.jsonl", off, ts, "human", "hi"))
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        source="all", rebuild=False, prune_orphans=False, prune_conversations=True)
    rc = ns["cmd_cache_sync"](args)
    assert rc == 0
    assert "pruned transcripts" in capsys.readouterr().err.lower()

    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE session_id='old'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE session_id='fresh'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_prune_conversations_disabled_when_retention_zero(env, capsys):
    ns, tmp_path, monkeypatch = env
    ns["save_config"]({"conversation": {"retention_days": 0}})
    conn = ns["open_conversations_db"](attach_cache=False)
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id, uuid, source_path, byte_offset, timestamp_utc, entry_type, text) "
        "VALUES ('old','u1','seed.jsonl',1,'2020-01-01T00:00:00.000Z','human','hi')")
    conn.commit()
    conn.close()

    args = argparse.Namespace(
        source="all", rebuild=False, prune_orphans=False, prune_conversations=True)
    rc = ns["cmd_cache_sync"](args)
    assert rc == 0
    assert "disabled" in capsys.readouterr().err.lower()
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE session_id='old'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
