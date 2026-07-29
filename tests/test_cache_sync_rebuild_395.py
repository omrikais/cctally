"""Issue #395: real-process cache-sync rebuild containment and convergence."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parents[1]
CCTALLY = ROOT / "bin" / "cctally"
CODEX_FIXTURE = (
    ROOT / "tests" / "fixtures" / "codex-parity" / "v1"
    / "rollouts" / "modern-full.jsonl"
)


def _stage_sources(tmp_path: pathlib.Path) -> tuple[dict[str, str], pathlib.Path]:
    home = tmp_path / "home"
    data = tmp_path / "data"
    claude_dir = home / ".claude" / "projects" / "-project"
    claude_dir.mkdir(parents=True)
    (claude_dir / "session.jsonl").write_text(
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-07-20T00:00:00Z",
            "requestId": "request-1",
            "sessionId": "session-1",
            "uuid": "message-1",
            "message": {
                "id": "message-1",
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "hello"}],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }) + "\n",
        encoding="utf-8",
    )
    codex_home = tmp_path / "codex"
    codex_rollout = (
        codex_home / "sessions" / "2026" / "07" / "20"
        / "rollout-modern-full.jsonl"
    )
    codex_rollout.parent.mkdir(parents=True)
    shutil.copyfile(CODEX_FIXTURE, codex_rollout)
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "CCTALLY_DATA_DIR": str(data),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "CCTALLY_TEST_CONVERSATION_PROBE_COPY": "1",
        "TZ": "Etc/UTC",
    })
    return env, data


def _start_statusline_actor(env: dict[str, str]) -> subprocess.Popen[str]:
    actor = (
        "import subprocess,sys,time\n"
        "exe,script=sys.argv[1:3]\n"
        "for _ in range(8):\n"
        " subprocess.run([exe,script,'statusline'],"
        " stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)\n"
        " time.sleep(0.05)\n"
    )
    actor_env = env.copy()
    actor_env.pop("CCTALLY_TEST_CACHE_SYNC_STALL_PHASE", None)
    actor_env.pop("CCTALLY_TEST_CACHE_SYNC_PHASE_TIMEOUT_SECONDS", None)
    return subprocess.Popen(
        [sys.executable, "-c", actor, sys.executable, str(CCTALLY)],
        env=actor_env,
        text=True,
    )


def _counts(data: pathlib.Path) -> tuple[int, int]:
    conn = sqlite3.connect(data / "cache.db")
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return (
            conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0],
            conn.execute(
                "SELECT COUNT(*) FROM codex_session_entries"
            ).fetchone()[0],
        )
    finally:
        conn.close()


def test_real_rebuild_stall_is_bounded_and_retry_converges(tmp_path):
    env, data = _stage_sources(tmp_path)
    fault_env = env.copy()
    fault_env.update({
        "CCTALLY_TEST_CACHE_SYNC_STALL_PHASE": "claude:ingest",
        "CCTALLY_TEST_CACHE_SYNC_PHASE_TIMEOUT_SECONDS": "0.2",
    })
    actor = _start_statusline_actor(fault_env)
    started = time.monotonic()
    stuck = subprocess.run(
        [
            sys.executable,
            str(CCTALLY),
            "cache-sync",
            "--source",
            "all",
            "--rebuild",
        ],
        env=fault_env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    elapsed = time.monotonic() - started
    actor.wait(timeout=10)

    assert stuck.returncode == 1
    assert elapsed < 5.0
    assert "[cache-sync] claude done:" in stuck.stderr
    assert "[cache-sync] codex done:" in stuck.stderr
    assert (
        "provider=claude store=conversations.db phase=ingest"
        in stuck.stderr
    )
    assert "core accounting/quota sync is complete" in stuck.stderr
    core_counts = _counts(data)
    assert core_counts[0] > 0
    assert core_counts[1] > 0

    conversations = sqlite3.connect(data / "conversations.db")
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

    actor = _start_statusline_actor(env)
    retry = subprocess.run(
        [
            sys.executable,
            str(CCTALLY),
            "cache-sync",
            "--source",
            "all",
            "--rebuild",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    actor.wait(timeout=10)

    assert retry.returncode == 0, retry.stderr
    assert "claude transcripts phase=prepare" in retry.stderr
    assert "codex transcripts phase=prepare" in retry.stderr
    assert "claude transcripts done: 1 processed" in retry.stderr
    assert "codex transcripts done: 1 processed" in retry.stderr
    assert _counts(data) == core_counts

    conversations = sqlite3.connect(data / "conversations.db")
    try:
        assert conversations.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0] == "ok"
        assert conversations.execute(
            "SELECT COUNT(*) FROM cache_meta WHERE key IN "
            "('conversation_rebuild_claude_pending',"
            "'conversation_rebuild_codex_pending')"
        ).fetchone()[0] == 0
        assert conversations.execute(
            "SELECT COUNT(*) FROM conversation_source_files"
        ).fetchone()[0] == 1
        assert conversations.execute(
            "SELECT COUNT(*) FROM codex_conversation_source_files"
        ).fetchone()[0] == 1
    finally:
        conversations.close()
