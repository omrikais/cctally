"""cmd_cache_sync: --rebuild aggregates a non-zero exit on lock contention
(was silently exit 0); --prune-orphans runs the helper and reports."""
from __future__ import annotations
import argparse, fcntl, json, multiprocessing, os, pathlib, shutil, sqlite3, subprocess, sys, time
import pytest
from conftest import load_script, redirect_paths

from tests._support_http import PRESENCE_BACKSTOP_SECONDS

import _lib_test_isolation as _iso
from _retention_fixtures import (
    LAUNCH_TIMEOUT_S, SWITCH, assert_holds_expired, copy_layout, counts, fork,
    seed, stage, write_retention)

_REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", "1")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    return ns, tmp_path, monkeypatch


def _force_confirmed_forensics(ns, monkeypatch):
    """Keep orchestration tests explicit: their synthetic trigger is confirmed."""
    db_mod = ns["_cctally_db"]
    real_write = db_mod.write_corruption_forensics

    def confirmed(*args, **kwargs):
        assert kwargs.get("return_result") is True
        result = real_write(*args, **kwargs)
        assert isinstance(result, db_mod.CorruptionForensicsResult)
        assert result.path is not None
        payload = json.loads(result.path.read_text())
        payload["probeDisposition"] = "confirmed"
        payload["probeReason"] = "test_confirmed"
        result.path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return db_mod.CorruptionForensicsResult(
            path=result.path,
            disposition=db_mod.CorruptionProbeDisposition.CONFIRMED,
            reason="test_confirmed",
            integrity_check=result.integrity_check,
        )

    monkeypatch.setattr(db_mod, "write_corruption_forensics", confirmed)


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
    monkeypatch.setattr(
        ns["_cctally_cache"], "open_conversations_db", unavailable,
    )
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
    monkeypatch.setattr(
        ns["_cctally_cache"],
        "_open_conversations_db_for_recovery",
        unavailable,
    )
    args = argparse.Namespace(
        source="claude", rebuild=True, prune_orphans=False,
        prune_conversations=False,
    )
    assert ns["cmd_cache_sync"](args) == 1
    stderr = capsys.readouterr().err
    assert "store=conversations.db phase=recovery" in stderr
    assert "core accounting/quota sync is complete" in stderr
    assert "Re-run `cctally cache-sync --rebuild`." in stderr


def test_transcript_lock_contention_names_phase_and_retry(env, monkeypatch, capsys):
    ns, _tmp_path, _fixture_monkeypatch = env
    cache_mod = ns["_cctally_cache"]
    monkeypatch.setattr(
        cache_mod, "_REBUILD_LOCK_TIMEOUT_SECONDS", 0.1
    )
    bootstrap = ns["open_conversations_db"](attach_cache=False)
    bootstrap.close()
    lock_path = ns["_cctally_core"].CONVERSATIONS_LOCK_PATH
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        args = argparse.Namespace(
            source="claude",
            rebuild=True,
            prune_orphans=False,
            prune_conversations=False,
        )
        assert ns["cmd_cache_sync"](args) == 1
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    stderr = capsys.readouterr().err
    assert "store=conversations.db phase=recovery" in stderr
    assert "provider locks" in stderr
    assert "Re-run `cctally cache-sync --rebuild`." in stderr


def test_rebuild_on_a_filesystem_without_clones_skips_the_probe(env, capfd):
    """A store whose filesystem cannot clone must still rebuild its transcripts.

    ext4 rejects `cp --reflink=always`. The pre-rebuild quick-check needs a
    clone and never falls back to a byte copy, so it cannot run there; the
    rebuild must say so and continue instead of failing on every invocation.
    The fake `cp` refuses the clone the way GNU coreutils does on hosted
    Linux, without the pytest copy seam.
    """
    ns, tmp_path, monkeypatch = env
    monkeypatch.delenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY")
    fake_bin = tmp_path / "no-clone-bin"
    fake_bin.mkdir()
    invoked = tmp_path / "fake-cp-invoked"
    fake_cp = fake_bin / "cp"
    fake_cp.write_text(
        "#!/bin/sh\n"
        f"echo invoked >> '{invoked}'\n"
        "for last; do :; done\n"
        "echo \"cp: failed to clone '$last': Operation not supported\" >&2\n"
        "exit 1\n"
    )
    fake_cp.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    _write_claude_entry(tmp_path)
    bootstrap = ns["open_conversations_db"](attach_cache=False)
    bootstrap.close()
    args = argparse.Namespace(
        source="claude",
        rebuild=True,
        prune_orphans=False,
        prune_conversations=False,
    )

    assert ns["cmd_cache_sync"](args) == 0
    stderr = capfd.readouterr().err
    assert invoked.exists(), "the probe never reached the clone"
    assert "transcript rebuild failed" not in stderr
    assert "integrity check skipped" in stderr
    assert "Operation not supported" in stderr
    assert "claude transcripts done: 1 processed" in stderr
    conversations = ns["open_conversations_db"](attach_cache=False)
    try:
        assert conversations.execute(
            "SELECT COUNT(*) FROM cache_meta "
            "WHERE key='conversation_rebuild_claude_pending'"
        ).fetchone()[0] == 0
    finally:
        conversations.close()
    data_dir = pathlib.Path(ns["_cctally_core"].CONVERSATIONS_DB_PATH).parent
    assert not list(data_dir.glob(".conversations-probe-*"))
    assert not list((data_dir / "quarantine").glob("conversations.db-*"))


def test_transcript_file_failure_names_phase_and_retry(
    env, monkeypatch, capsys,
):
    ns, tmp_path, _fixture_monkeypatch = env
    cache_mod = ns["_cctally_cache"]
    _write_claude_entry(tmp_path)

    def fail_touches(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cache_mod, "_fill_file_touches", fail_touches)
    args = argparse.Namespace(
        source="claude",
        rebuild=True,
        prune_orphans=False,
        prune_conversations=False,
    )
    assert ns["cmd_cache_sync"](args) == 1
    stderr = capsys.readouterr().err
    assert "provider=claude store=conversations.db phase=ingest" in stderr
    assert "1 file(s) failed" in stderr
    assert (
        "Re-run `cctally cache-sync --source claude --rebuild`." in stderr
    )


def test_explicit_rebuild_bounds_a_stuck_claude_transcript_phase(env, capfd):
    """#395 RED: the real cmd orchestration must outlive a wedged transcript leg.

    The outer process is the test's fail-loud hard ceiling. Before the fix,
    cmd_cache_sync calls the patched leg in-process and this child survives the
    two-second join; the test kills it and fails instead of hanging pytest.
    """
    ns, tmp_path, monkeypatch = env
    cache_mod = ns["_cctally_cache"]
    # Fixed-date fixture: durable under the test default
    # (docs/fixtures-gotchas.md, "Conversation retention is off in test
    # environments (#965)").
    _write_claude_entry(tmp_path)
    monkeypatch.setattr(
        cache_mod,
        "_TRANSCRIPT_REBUILD_PHASE_TIMEOUT_SECONDS",
        0.2,
        raising=False,
    )
    real_fill_file_touches = cache_mod._fill_file_touches

    def stuck(*_args, **_kwargs):
        while True:
            time.sleep(0.01)

    # This seam is transcript-only and occurs after the durable pending marker
    # but before the file cursor/message transaction commits.
    monkeypatch.setattr(cache_mod, "_fill_file_touches", stuck)
    args = argparse.Namespace(
        source="claude",
        rebuild=True,
        prune_orphans=False,
        prune_conversations=False,
    )
    ctx = multiprocessing.get_context("fork")
    results = ctx.SimpleQueue()

    def invoke() -> None:
        results.put(ns["cmd_cache_sync"](args))

    proc = ctx.Process(target=invoke)
    started = time.monotonic()
    proc.start()
    # timing-budget: the cache-sync child has exited; `finished` below records whether it did, and the assertion reads that flag rather than the elapsed time
    proc.join(timeout=PRESENCE_BACKSTOP_SECONDS)
    elapsed = time.monotonic() - started
    finished = not proc.is_alive()
    if not finished:
        proc.kill()
        # timing-budget: the killed cache-sync child has been reaped
        proc.join(timeout=PRESENCE_BACKSTOP_SECONDS)

    assert finished, (
        "cmd_cache_sync did not contain the stuck Claude transcript phase "
        f"within the test ceiling ({elapsed:.3f}s)"
    )
    assert proc.exitcode == 0
    assert results.get() == 1
    assert elapsed < 2.0
    stderr = capfd.readouterr().err
    assert "provider=claude store=conversations.db phase=ingest" in stderr
    assert "core accounting/quota sync is complete" in stderr
    assert "partial transcript state remains retry-safe and incomplete" in stderr

    core = ns["open_cache_db"]()
    try:
        assert core.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        core_count = core.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0]
        assert core_count == 1
    finally:
        core.close()
    conversations = ns["open_conversations_db"](attach_cache=False)
    try:
        assert conversations.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0] == "ok"
        assert conversations.execute(
            "SELECT COUNT(*) FROM cache_meta "
            "WHERE key='conversation_rebuild_claude_pending'"
        ).fetchone()[0] == 1
    finally:
        conversations.close()

    # Retry with the injected stall removed. It must clear the retry marker and
    # converge without changing the already-committed core accounting count.
    monkeypatch.setattr(cache_mod, "_fill_file_touches", real_fill_file_touches)
    monkeypatch.setattr(
        cache_mod, "_TRANSCRIPT_REBUILD_PHASE_TIMEOUT_SECONDS", 5.0
    )
    assert ns["cmd_cache_sync"](args) == 0
    retry_stderr = capfd.readouterr().err
    assert "claude transcripts phase=prepare" in retry_stderr
    assert "claude transcripts: 1/1 files" in retry_stderr
    assert "claude transcripts done: 1 processed" in retry_stderr
    core = ns["open_cache_db"]()
    try:
        assert core.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0] == core_count
    finally:
        core.close()
    conversations = ns["open_conversations_db"](attach_cache=False)
    try:
        assert conversations.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0] == "ok"
        assert conversations.execute(
            "SELECT COUNT(*) FROM cache_meta "
            "WHERE key='conversation_rebuild_claude_pending'"
        ).fetchone()[0] == 0
        assert conversations.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone()[0] >= 1
    finally:
        conversations.close()


def test_transcript_timeout_tracks_no_progress_not_total_wall_time(
    env, monkeypatch,
):
    """A healthy large phase may outlive the bound while still advancing."""
    ns, _tmp_path, _fixture_monkeypatch = env
    cache_mod = ns["_cctally_cache"]
    step_seconds = 0.04
    phase_timeout_seconds = step_seconds * 25
    startup_delay_seconds = step_seconds * 5
    progress_steps = 30
    monkeypatch.setattr(
        cache_mod,
        "_TRANSCRIPT_REBUILD_PHASE_TIMEOUT_SECONDS",
        phase_timeout_seconds,
    )

    class FakeConnection:
        def close(self) -> None:
            pass

    def delayed_open():
        # Exercise worker-startup scheduling latency without letting it consume
        # the deliberately proportional no-progress budget.
        time.sleep(startup_delay_seconds)
        return FakeConnection()

    def advancing(_conn, *, progress, **_kwargs):
        stats = cache_mod.IngestStats(files_total=progress_steps)
        for completed in range(1, progress_steps + 1):
            time.sleep(step_seconds)
            stats.files_processed = completed
            progress("ingest", stats)
        return stats

    monkeypatch.setattr(
        cache_mod,
        "_open_conversations_db_for_recovery",
        delayed_open,
    )
    monkeypatch.setattr(cache_mod, "sync_claude_conversations", advancing)

    started = time.monotonic()
    outcome = cache_mod._run_transcript_rebuild_worker(
        "claude", lock_timeout=0.1
    )
    elapsed = time.monotonic() - started

    assert elapsed > phase_timeout_seconds
    assert outcome.timed_out is False
    assert outcome.error is None
    assert outcome.stats is not None
    assert outcome.stats.files_processed == progress_steps


def test_rebuild_recovers_post_open_corruption_and_retries_once(
    env, capsys,
):
    ns, _tmp_path, monkeypatch = env
    cache_mod = ns["_cctally_cache"]
    _force_confirmed_forensics(ns, monkeypatch)
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
    _force_confirmed_forensics(ns, monkeypatch)
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
    _force_confirmed_forensics(ns, monkeypatch)
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
    # #965: deletion is this test's subject, so it opts into retention explicitly.
    ns["save_config"]({"conversation": {"retention_days": 90}})
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


def test_a_refused_transcript_rebuild_is_not_reported_as_success(env, capsys):
    """#705: `cache-sync --rebuild` is the operator's recovery path, so a
    rebuild refused because this cctally's pricing table is older than the one
    recorded in the store must say so rather than print `done: 0 processed`
    and exit 0.

    sync_claude_conversations already sets deferred_reason on that path; the
    outcome ladder simply never read it, so the refusal fell through to the
    final success branch.
    """
    ns, tmp_path, monkeypatch = env
    cache_mod = ns["_cctally_cache"]
    _write_claude_entry(tmp_path)
    conn = ns["open_conversations_db"]()
    try:
        cache_mod.sync_claude_conversations(conn)
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
            (cache_mod.CONVERSATION_ROLLUP_PRICING_FP_KEY, "2026-09-30"),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(cache_mod, "PRICING_SNAPSHOT_DATE", "2026-08-25")

    args = argparse.Namespace(
        source="claude", rebuild=True, prune_orphans=False,
        prune_conversations=False,
    )

    assert ns["cmd_cache_sync"](args) == 1
    stderr = capsys.readouterr().err
    assert "provider=claude store=conversations.db phase=pricing" in stderr
    assert "Upgrade or restart cctally" in stderr
    assert (
        "Re-run `cctally cache-sync --source claude --rebuild`." in stderr
    )
    assert "claude transcripts done" not in stderr, \
        "a refused rebuild must not be reported as success"


def test_a_refused_rebuild_names_the_unorderable_cause_too(env, capsys):
    """#705: "older than the one recorded in the store" is not the only cause.

    `_pricing_write_authorized` also refuses a recorded value that is not an
    ISO date, which is not "older" — no version can order it, `--rebuild` is
    refused too, and only clearing that one `cache_meta` row gets out of it.
    `docs/commands/doctor.md` documents that state; the `cache-sync` stderr and
    its own doc contradicted it, so an operator hitting the unorderable state
    read a cause that does not apply and a remedy that does nothing.

    Mutation: restoring the single-cause wording on either surface.
    """
    ns, tmp_path, monkeypatch = env
    cache_mod = ns["_cctally_cache"]
    _write_claude_entry(tmp_path)
    conn = ns["open_conversations_db"]()
    try:
        cache_mod.sync_claude_conversations(conn)
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
            (cache_mod.CONVERSATION_ROLLUP_PRICING_FP_KEY, "v2/2026-09-02"),
        )
        conn.commit()
    finally:
        conn.close()

    args = argparse.Namespace(
        source="claude", rebuild=True, prune_orphans=False,
        prune_conversations=False,
    )

    assert ns["cmd_cache_sync"](args) == 1
    stderr = capsys.readouterr().err
    assert "provider=claude store=conversations.db phase=pricing" in stderr
    assert "older than the one recorded in the store" not in stderr, \
        "the store's fingerprint is not older here; it cannot be ordered at all"
    assert "ISO date" in stderr
    assert "cctally doctor" in stderr, \
        "the unorderable state's only remedy is the one doctor names"

    doc = (pathlib.Path(__file__).resolve().parent.parent
           / "docs" / "commands" / "cache-sync.md").read_text()
    assert "ISO date" in doc, \
        "the documented cause list must carry the second cause too"


# --- #729: `--prune-orphans` exit contract when the rollup re-derive is refused


def _orphan_then_remove(ns):
    """One fully ingested transcript whose directory is then deleted."""
    pdir = pathlib.Path(os.environ["HOME"]) / ".claude" / "projects" / "-gone"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "s.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-07-01T00:00:00Z",
        "requestId": "r1", "sessionId": "S1", "uuid": "u1",
        "parentUuid": None,
        "message": {"id": "m1", "model": "claude-opus-4-7", "usage": {
            "input_tokens": 0, "output_tokens": 5,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }},
    }) + "\n")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_cache"](conn)
    finally:
        conn.close()
    conn = ns["open_conversations_db"]()
    try:
        ns["sync_claude_conversations"](conn)
    finally:
        conn.close()
    shutil.rmtree(pdir)


def _prune_args():
    return argparse.Namespace(
        source="claude", rebuild=False, prune_orphans=True,
        prune_conversations=False,
    )


def test_prune_orphans_exits_3_when_the_rollup_rederive_is_refused(env, capsys):
    """Staged failure under docs/cli-contract.md, not a usage error: the
    deletions committed and the re-derive that should have followed them did
    not, so the store is left partially derived."""
    ns, tmp_path, monkeypatch = env
    _orphan_then_remove(ns)
    monkeypatch.setattr(
        ns["_cctally_cache"], "PRICING_SNAPSHOT_DATE", "2020-01-01")
    assert ns["cmd_cache_sync"](_prune_args()) == 3
    err = capsys.readouterr().err
    assert "refused" in err
    assert "1 file" in err


def test_the_refusal_message_names_the_cause_that_actually_occurred(
        env, capsys, monkeypatch):
    """#769 S3 A2. The message stated one cause — "this process's pricing table
    is older than the one the store recorded" — which #728 made non-exclusive:
    a MALFORMED stored value and a read that FAILED refuse too, and neither is
    about which table is older.
    """
    ns, tmp_path, monkeypatch2 = env
    _orphan_then_remove(ns)
    cache = ns["_cctally_cache"]
    import _lib_pricing
    obs = _lib_pricing.classify_pricing_fingerprint(
        found=False, raw=None, error_kind="operational_error")
    monkeypatch.setattr(
        cache, "_read_pricing_fingerprint_observation",
        lambda conn, key=None: obs)
    assert ns["cmd_cache_sync"](_prune_args()) == 3
    err = capsys.readouterr().err
    assert "refused" in err
    assert "older" not in err, (
        "a read that failed says nothing about which pricing table is older"
    )
    assert cache.pricing_refusal_cause_phrase("degraded") in err


def test_the_older_pricing_message_survives_for_the_state_it_describes(
        env, capsys, monkeypatch):
    ns, tmp_path, monkeypatch2 = env
    _orphan_then_remove(ns)
    monkeypatch.setattr(
        ns["_cctally_cache"], "PRICING_SNAPSHOT_DATE", "2020-01-01")
    assert ns["cmd_cache_sync"](_prune_args()) == 3
    err = capsys.readouterr().err
    assert ns["_cctally_cache"].pricing_refusal_cause_phrase("present") in err


def test_prune_orphans_exits_0_when_the_rederive_is_authorized(env, capsys):
    ns, tmp_path, monkeypatch = env
    _orphan_then_remove(ns)
    assert ns["cmd_cache_sync"](_prune_args()) == 0
    assert "refused" not in capsys.readouterr().err


def test_prune_orphans_reports_contention_before_a_refusal(env, capsys):
    """Precedence: contention is reported first because NOTHING was attempted,
    so there is no refusal to outrank — the pricing table is irrelevant on a
    path that never reached the recompute."""
    ns, tmp_path, monkeypatch = env
    monkeypatch.setattr(
        ns["_cctally_cache"], "_REBUILD_LOCK_TIMEOUT_SECONDS", 0.3,
        raising=False)
    monkeypatch.setattr(
        ns["_cctally_cache"], "PRICING_SNAPSHOT_DATE", "2020-01-01")
    _orphan_then_remove(ns)
    lock_path = ns["_cctally_core"].CACHE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        assert ns["cmd_cache_sync"](_prune_args()) == 1
        err = capsys.readouterr().err
        assert "another process holds the lock" in err
        assert "refused" not in err
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


# ── #965: one seeded store per module, forked per row (runtime cost) ─────────

def _checkout_cache_sync(staged, *args):
    # timing-budget: #965 hang bound for one cctally launch, LAUNCH_TIMEOUT_S (45 s) from tests/_retention_fixtures.py; a node makes at most two in sequence, under the 120 s cap
    proc = subprocess.run(
        [sys.executable, str(_REPO / "bin" / "cctally"), "cache-sync", *args],
        env=staged.env, capture_output=True, text=True, timeout=LAUNCH_TIMEOUT_S)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


@pytest.fixture(scope="module")
def retention_template(tmp_path_factory):
    """A store seeded ONCE per module and xdist worker through the checkout CLI
    with retention explicitly off. Every T2 and T4 row forks it and reads the
    same source tree through the same explicit CODEX_HOME, so the seed proves
    that each row's launch reads a Codex root holding expired transcripts."""
    staged = stage(tmp_path_factory.mktemp("retention-965-template"))
    assert staged.env.get(SWITCH) == "1", "the copied environment lost the switch"
    seed(staged.data, lambda *args: _checkout_cache_sync(staged, *args))
    return staged


def _forked(template, tmp_path, explicit):
    """The row's own store: a fork proven to hold expired transcripts, then the
    row's retention setting (None removes the template's explicit 0)."""
    staged = fork(template, tmp_path)
    assert_holds_expired(staged.data, when="after forking the template")
    write_retention(staged.data, explicit)
    return staged


# ── #965 T2: eligibility follows the executed code (real subprocess pruning) ──

# id, layout, switch (None = unset), explicit window, declared control, survives.
# Spec T2's "an explicit positive window still prunes in an eligible checkout" is
# T4's test_965_rebuild_under_the_test_default[explicit-window], which runs the
# real checkout, so no copied-checkout row repeats it.
_LAYOUT_ROWS = [
    ("packaged-switch", "packaged", "1", None, True, False),
    ("npm-switch", "npm", "1", None, True, False),
    ("brew-switch", "brew", "1", None, True, False),
    ("checkout-switch", "checkout", "1", None, False, True),
    ("worktree-switch", "worktree", "1", None, False, True),
    ("checkout-unset", "checkout", None, None, True, False),
    ("worktree-unset", "worktree", None, None, True, False),
    ("packaged-explicit-zero", "packaged", "1", 0, True, True),
]


@pytest.fixture(scope="module")
def retention_layouts(tmp_path_factory):
    root = tmp_path_factory.mktemp("retention-965-layouts")
    return {kind: copy_layout(kind, root / kind)
            for kind in ("packaged", "npm", "brew", "checkout", "worktree")}


def _layout_cache_sync(launcher, staged, *args, switch, declared):
    env = dict(staged.env)
    if switch is None:
        env.pop(SWITCH, None)
    else:
        env[SWITCH] = switch
    argv = [sys.executable, str(launcher), "cache-sync", *args]

    def run():
        # Always from a checkout working directory: eligibility follows the code.
        # timing-budget: #965 hang bound for one cctally launch, LAUNCH_TIMEOUT_S (45 s) from tests/_retention_fixtures.py; a node makes at most two in sequence, under the 120 s cap
        return subprocess.run(argv, env=env, cwd=_REPO, capture_output=True,
                              text=True, timeout=LAUNCH_TIMEOUT_S)

    if declared:
        with _iso.retention_launch_control(
                "production-behavior",
                reason="#965 T2: production semantics are this row's subject"):
            proc = run()
    else:
        proc = run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


@pytest.mark.parametrize(
    "layout,switch,explicit,declared,survives",
    [row[1:] for row in _LAYOUT_ROWS], ids=[row[0] for row in _LAYOUT_ROWS])
def test_965_eligibility_follows_the_executed_code(
        retention_template, retention_layouts, tmp_path,
        layout, switch, explicit, declared, survives):
    staged = _forked(retention_template, tmp_path, explicit)
    _layout_cache_sync(retention_layouts[layout], staged, "--source", "all",
                       "--rebuild", switch=switch, declared=declared)
    got = counts(staged.data)
    assert got["recent_claude"] > 0, f"the rebuild lost the recent survivor: {got}"
    if survives:
        assert got["expired_claude"] > 0 and got["expired_codex"] > 0, (
            f"{layout}: the expired transcripts were deleted: {got}")
    else:
        assert got["expired_claude"] == 0 and got["expired_codex"] == 0, (
            f"{layout}: expired transcripts survived production retention: {got}")


# ── #965 T4: the real CLI under the test default, with deletion controls ──

@pytest.mark.parametrize("window", [None, 30], ids=["test-default", "explicit-window"])
def test_965_rebuild_under_the_test_default(retention_template, tmp_path, window):
    staged = _forked(retention_template, tmp_path, window)
    assert staged.env.get(SWITCH) == "1", "the copied environment lost the switch"
    _checkout_cache_sync(staged, "--source", "all", "--rebuild")
    got = counts(staged.data)
    assert got["recent_claude"] > 0, got
    if window is None:
        assert got["expired_claude"] > 0 and got["expired_codex"] > 0, (
            f"the test default deleted expired transcripts on --rebuild: {got}")
    else:
        assert got["expired_claude"] == 0 and got["expired_codex"] == 0, got


@pytest.mark.parametrize("window", [None, 30], ids=["test-default", "explicit-window"])
def test_965_explicit_prune_under_the_test_default(retention_template, tmp_path, window):
    # The template ingested with retention explicitly off, so the prune below is
    # the only deleter.
    staged = _forked(retention_template, tmp_path, window)
    _checkout_cache_sync(staged, "--prune-conversations")
    got = counts(staged.data)
    assert got["recent_claude"] > 0, got
    if window is None:
        assert got["expired_claude"] > 0 and got["expired_codex"] > 0, (
            f"the test default deleted expired transcripts on --prune-conversations: {got}")
    else:
        assert got["expired_claude"] == 0 and got["expired_codex"] == 0, got
