"""Cross-provider cache.db writer durability regressions for issue #344."""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _write_codex_rollout(codex_home: pathlib.Path) -> None:
    sessions = codex_home / "sessions" / "2026" / "07" / "24"
    sessions.mkdir(parents=True)
    rollout = (
        sessions
        / "rollout-2026-07-24T08-00-00-aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa.jsonl"
    )
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-07-24T08:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "sess-344"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-24T08:00:00Z",
                        "type": "turn_context",
                        "payload": {"model": "gpt-5"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-24T08:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 100,
                                    "output_tokens": 10,
                                    "cached_input_tokens": 20,
                                    "reasoning_output_tokens": 5,
                                    "total_tokens": 135,
                                },
                                "total_token_usage": {"total_tokens": 135},
                            },
                        },
                    }
                ),
            ]
        )
        + "\n"
    )


def _write_claude_session(claude_dir: pathlib.Path) -> pathlib.Path:
    projects = claude_dir / "projects" / "-Users-u-project-A"
    projects.mkdir(parents=True)
    session = projects / "sess-344.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-07-24T08:00:00Z",
                "requestId": "req-344",
                "message": {
                    "id": "msg-344",
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                },
            }
        )
        + "\n"
    )
    return session


def test_codex_real_process_respects_global_cache_writer_lock(tmp_path):
    """The Claude lock must exclude every writer/checkpointer on cache.db.

    The pre-#344 design used ``cache.db.lock`` for Claude and
    ``cache.db.codex.lock`` for Codex, so this real Codex CLI process could
    commit while a Claude writer owned its lock. That is the exact
    cross-connection write/checkpoint overlap required by SQLite's WAL-reset
    corruption class.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    empty_claude = tmp_path / "claude"
    (empty_claude / "projects").mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        {
            "CCTALLY_DATA_DIR": str(data_dir),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "CLAUDE_CONFIG_DIR": str(empty_claude),
            "CODEX_HOME": str(codex_home),
        }
    )
    bootstrap = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "cctally"), "cache-sync",
         "--source", "all"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert bootstrap.returncode == 0, bootstrap.stderr
    _write_codex_rollout(codex_home)

    global_lock = data_dir / "cache.db.lock"
    global_lock.touch()
    holder = global_lock.open("w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "cctally"), "cache-sync",
             "--source", "codex"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert proc.returncode == 0, proc.stderr
    cache_path = data_dir / "cache.db"
    conn = sqlite3.connect(cache_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM codex_session_entries"
        ).fetchone()[0] == 0
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_repeated_real_process_writer_storm_converges_exactly(tmp_path):
    """A multiprocess Claude/Codex storm must serialize and then converge."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    claude_dir = tmp_path / "claude"
    codex_home = tmp_path / "codex-home"
    (claude_dir / "projects").mkdir(parents=True)
    codex_home.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "CCTALLY_DATA_DIR": str(data_dir),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "CLAUDE_CONFIG_DIR": str(claude_dir),
            "CODEX_HOME": str(codex_home),
        }
    )

    session = _write_claude_session(claude_dir)
    _write_codex_rollout(codex_home)
    rollout = next(codex_home.rglob("*.jsonl"))
    commands = [
        [sys.executable, str(ROOT / "bin" / "cctally"), "cache-sync",
         "--source", source]
        for source in ("claude", "codex") * 6
    ]
    procs = [
        subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for command in commands
    ]
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 0, f"{stdout}\n{stderr}"

    survivor = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "cctally"), "cache-sync",
         "--source", "all"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert survivor.returncode == 0, survivor.stderr

    cache_path = data_dir / "cache.db"
    conn = sqlite3.connect(cache_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM session_entries"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT size_bytes,last_byte_offset FROM session_files"
        ).fetchone() == (session.stat().st_size, session.stat().st_size)
        assert conn.execute(
            "SELECT count(*) FROM codex_session_entries"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT size_bytes,last_byte_offset FROM codex_session_files"
        ).fetchone() == (rollout.stat().st_size, rollout.stat().st_size)
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    wal_path = pathlib.Path(f"{cache_path}-wal")
    assert (wal_path.stat().st_size if wal_path.exists() else 0) <= 128 * 1024 * 1024
    assert (data_dir / "cache.db.lock").stat().st_mode & 0o777 == 0o600
    assert (data_dir / "cache.db.codex.lock").stat().st_mode & 0o777 == 0o600


def test_killed_claude_transaction_retries_cursor_without_corruption(tmp_path):
    """SIGKILL before a Claude commit must not advance its file cursor."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    claude_dir = tmp_path / "claude"
    session = _write_claude_session(claude_dir)
    marker = tmp_path / "claude-precommit.marker"

    env = os.environ.copy()
    env.update(
        {
            "CCTALLY_DATA_DIR": str(data_dir),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "CLAUDE_CONFIG_DIR": str(claude_dir),
            "CCTALLY_TEST_CACHE_STORM_PAUSE_AT": "claude_precommit",
            "CCTALLY_TEST_CACHE_STORM_MARKER": str(marker),
        }
    )
    victim = subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / "cctally"), "cache-sync",
         "--source", "claude"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            if victim.poll() is not None:
                break
            time.sleep(0.02)
        if not marker.exists():
            stdout, stderr = victim.communicate(timeout=5)
            pytest.fail(
                "victim never reached the controlled precommit point\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        os.kill(victim.pid, signal.SIGKILL)
        victim.communicate(timeout=5)
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.communicate(timeout=5)

    survivor_env = {
        key: value
        for key, value in env.items()
        if not key.startswith("CCTALLY_TEST_CACHE_STORM_")
    }
    survivor = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "cctally"), "cache-sync",
         "--source", "claude"],
        env=survivor_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert survivor.returncode == 0, survivor.stderr

    conn = sqlite3.connect(data_dir / "cache.db")
    try:
        assert conn.execute(
            "SELECT count(*) FROM session_entries"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT size_bytes, last_byte_offset FROM session_files"
        ).fetchone() == (session.stat().st_size, session.stat().st_size)
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_killed_codex_transaction_retries_cursor_without_corruption(tmp_path):
    """SIGKILL before commit must leave the source bytes retryable."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    _write_codex_rollout(codex_home)
    rollout = next(codex_home.rglob("*.jsonl"))
    marker = tmp_path / "codex-precommit.marker"

    env = os.environ.copy()
    env.update(
        {
            "CCTALLY_DATA_DIR": str(data_dir),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "CODEX_HOME": str(codex_home),
            "CCTALLY_TEST_CACHE_STORM_PAUSE_AT": "codex_precommit",
            "CCTALLY_TEST_CACHE_STORM_MARKER": str(marker),
        }
    )
    victim = subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / "cctally"), "cache-sync",
         "--source", "codex"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            if victim.poll() is not None:
                break
            time.sleep(0.02)
        if not marker.exists():
            stdout, stderr = victim.communicate(timeout=5)
            pytest.fail(
                "victim never reached the controlled precommit point\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        os.kill(victim.pid, signal.SIGKILL)
        victim.communicate(timeout=5)
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.communicate(timeout=5)

    survivor_env = {
        key: value
        for key, value in env.items()
        if not key.startswith("CCTALLY_TEST_CACHE_STORM_")
    }
    survivor = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "cctally"), "cache-sync",
         "--source", "codex"],
        env=survivor_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert survivor.returncode == 0, survivor.stderr

    conn = sqlite3.connect(data_dir / "cache.db")
    try:
        assert conn.execute(
            "SELECT count(*) FROM codex_session_entries"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT size_bytes, last_byte_offset FROM codex_session_files"
        ).fetchone() == (rollout.stat().st_size, rollout.stat().st_size)
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_killed_checkpointer_releases_global_lock_for_other_provider(tmp_path):
    """A killed Codex checkpointer cannot overlap a Claude writer."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    claude_dir = tmp_path / "claude"
    session = _write_claude_session(claude_dir)
    codex_home = tmp_path / "codex-home"
    _write_codex_rollout(codex_home)
    rollout = next(codex_home.rglob("*.jsonl"))
    marker = tmp_path / "codex-precheckpoint.marker"

    base_env = os.environ.copy()
    base_env.update(
        {
            "CCTALLY_DATA_DIR": str(data_dir),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "CLAUDE_CONFIG_DIR": str(claude_dir),
            "CODEX_HOME": str(codex_home),
        }
    )
    victim_env = base_env | {
        "CCTALLY_TEST_CACHE_STORM_PAUSE_AT": "cache_precheckpoint",
        "CCTALLY_TEST_CACHE_STORM_MARKER": str(marker),
        "CCTALLY_TEST_CACHE_WAL_TRIGGER_BYTES": "0",
    }
    victim = subprocess.Popen(
        [sys.executable, str(ROOT / "bin" / "cctally"), "cache-sync",
         "--source", "codex"],
        env=victim_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists():
            if victim.poll() is not None:
                break
            time.sleep(0.02)
        if not marker.exists():
            stdout, stderr = victim.communicate(timeout=5)
            pytest.fail(
                "victim never reached the controlled precheckpoint point\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        wal_path = data_dir / "cache.db-wal"
        assert wal_path.exists() and wal_path.stat().st_size > 0

        blocked_claude = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "cctally"), "cache-sync",
             "--source", "claude"],
            env=base_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert blocked_claude.returncode == 0, blocked_claude.stderr
        conn = sqlite3.connect(data_dir / "cache.db")
        try:
            assert conn.execute(
                "SELECT count(*) FROM session_entries"
            ).fetchone()[0] == 0
        finally:
            conn.close()

        os.kill(victim.pid, signal.SIGKILL)
        victim.communicate(timeout=5)
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.communicate(timeout=5)

    resumed_claude = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "cctally"), "cache-sync",
         "--source", "claude"],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert resumed_claude.returncode == 0, resumed_claude.stderr

    conn = sqlite3.connect(data_dir / "cache.db")
    try:
        assert conn.execute(
            "SELECT count(*) FROM session_entries"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM codex_session_entries"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT size_bytes,last_byte_offset FROM session_files"
        ).fetchone() == (session.stat().st_size, session.stat().st_size)
        assert conn.execute(
            "SELECT size_bytes,last_byte_offset FROM codex_session_files"
        ).fetchone() == (rollout.stat().st_size, rollout.stat().st_size)
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    wal_path = data_dir / "cache.db-wal"
    assert (wal_path.stat().st_size if wal_path.exists() else 0) <= 128 * 1024 * 1024
