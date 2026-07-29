"""Issue #415 Task A: conversations.db diagnosis and crash-safe recovery."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from conftest import load_script, redirect_paths


ROOT = pathlib.Path(__file__).resolve().parents[1]
CCTALLY = ROOT / "bin" / "cctally"
CODEX_FIXTURE = (
    ROOT / "tests" / "fixtures" / "codex-parity" / "v1"
    / "rollouts" / "modern-full.jsonl"
)


def _family_snapshot(path: pathlib.Path) -> dict[str, bytes]:
    return {
        pathlib.Path(f"{path}{suffix}").name:
        pathlib.Path(f"{path}{suffix}").read_bytes()
        for suffix in ("", "-wal", "-shm")
        if pathlib.Path(f"{path}{suffix}").exists()
    }


def _stage_sources(
    tmp_path: pathlib.Path,
) -> tuple[dict[str, str], pathlib.Path]:
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
                "content": [{"type": "text", "text": "hello recovery"}],
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


def _run(env: dict[str, str], *args: str, timeout: int = 40):
    return subprocess.run(
        [sys.executable, str(CCTALLY), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _accounting_digest(data: pathlib.Path) -> str:
    conn = sqlite3.connect(data / "cache.db")
    try:
        columns = {
            "session_entries": (
                "source_path,line_offset,timestamp_utc,model,msg_id,req_id,"
                "input_tokens,output_tokens,cache_create_tokens,"
                "cache_read_tokens,usage_extra_json,cost_usd_raw,speed"
            ),
            "codex_session_entries": (
                "source_path,line_offset,timestamp_utc,session_id,model,"
                "input_tokens,cached_input_tokens,output_tokens,"
                "reasoning_output_tokens,total_tokens"
            ),
            "quota_window_snapshots": (
                "source,source_root_key,source_path,line_offset,"
                "captured_at_utc,observed_slot,logical_limit_key,limit_id,"
                "limit_name,window_minutes,used_percent,resets_at_utc,"
                "plan_type,individual_limit_json,reached_type,observed_model"
            ),
        }
        rows = {
            table: sorted(conn.execute(
                f"SELECT {selected} FROM {table}"
            ).fetchall())
            for table, selected in columns.items()
        }
        encoded = json.dumps(rows, default=str, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()
    finally:
        conn.close()


def _leave_live_wal_family(path: pathlib.Path) -> None:
    actor = (
        "import sqlite3,sys,time\n"
        "c=sqlite3.connect(sys.argv[1])\n"
        "c.execute('PRAGMA journal_mode=WAL')\n"
        "c.execute('PRAGMA wal_autocheckpoint=0')\n"
        "c.execute(\"INSERT OR REPLACE INTO cache_meta(key,value) "
        "VALUES('issue_415_wal_evidence','retained')\")\n"
        "c.commit()\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", actor, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    process.kill()
    process.wait(timeout=10)
    process.stdout.close()
    assert process.stderr is not None
    process.stderr.close()
    assert pathlib.Path(f"{path}-wal").exists()
    assert pathlib.Path(f"{path}-shm").exists()


def _integrity_check(payload: dict) -> dict:
    db_category = next(
        category for category in payload["categories"]
        if category["id"] == "db"
    )
    return next(
        check for check in db_category["checks"]
        if check["id"] == "db.integrity"
    )


def test_real_cli_recovers_corrupt_transcript_family_and_both_providers(
    tmp_path,
):
    env, data = _stage_sources(tmp_path)
    initial = _run(env, "cache-sync", "--source", "all", "--rebuild")
    assert initial.returncode == 0, initial.stderr
    accounting_before = _accounting_digest(data)

    conversations = data / "conversations.db"
    _leave_live_wal_family(conversations)
    raw = bytearray(conversations.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big")
    if page_size == 1:
        page_size = 65_536
    assert len(raw) >= page_size * 3
    raw[page_size:page_size * 2] = b"\xa5" * page_size
    conversations.write_bytes(raw)
    assert set(_family_snapshot(conversations)) == {
        "conversations.db",
        "conversations.db-wal",
        "conversations.db-shm",
    }

    before = _run(env, "doctor", "--json")
    before_payload = json.loads(before.stdout)
    before_integrity = _integrity_check(before_payload)
    assert before_integrity["severity"] == "warn"
    assert "conversations.db" in before_integrity["summary"]
    assert (
        before_integrity["remediation"]
        == "conversations.db is re-derivable — run "
        "`cctally cache-sync --rebuild`."
    )
    corrupt_family = _family_snapshot(conversations)
    assert set(corrupt_family) == {
        "conversations.db",
        "conversations.db-wal",
        "conversations.db-shm",
    }

    rebuilt = _run(env, "cache-sync", "--source", "all", "--rebuild")
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert "quarantined its file family" in rebuilt.stderr
    incidents = list((data / "quarantine").glob("conversations.db-*"))
    assert len(incidents) == 1
    manifest = json.loads((incidents[0] / "manifest.json").read_text())
    assert set(manifest["movedFiles"]) == {
        "conversations.db",
        "conversations.db-wal",
        "conversations.db-shm",
    }
    for name, content in corrupt_family.items():
        assert (incidents[0] / name).read_bytes() == content

    after = _run(env, "doctor", "--json")
    after_integrity = _integrity_check(json.loads(after.stdout))
    assert after_integrity["severity"] == "ok"
    assert after_integrity["details"] == {
        "stats_quick_check": "ok",
        "cache_quick_check": "ok",
        "conversations_quick_check": "ok",
    }

    conn = sqlite3.connect(conversations)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_source_files"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_source_files"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM cache_meta WHERE key IN "
            "('conversation_rebuild_claude_pending',"
            "'conversation_rebuild_codex_pending')"
        ).fetchone() == (0,)
        sys.path.insert(0, str(ROOT / "bin"))
        import _lib_conversation_query as conversation_query
        listed = conversation_query.list_conversations(conn)
        assert any(
            row["session_id"] == "session-1"
            for row in listed["conversations"]
        )
    finally:
        conn.close()

    search = _run(env, "transcript", "search", "recovery", "--json")
    assert search.returncode == 0, search.stderr
    assert json.loads(search.stdout)["hits"]
    detail = _run(env, "transcript", "export", "session-1", "--raw")
    assert detail.returncode == 0, detail.stderr
    assert "hello recovery" in detail.stdout
    assert _accounting_digest(data) == accounting_before
    assert not (data / "conversations.db.recovery.json").exists()
    assert not (data / "conversations.db.repairing").exists()


def test_integrity_clean_classified_trigger_preserves_family(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    core = ns["_cctally_core"]
    conn = ns["open_conversations_db"](attach_cache=False)
    conn.close()
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    before = _family_snapshot(path)
    trigger = sqlite3.DatabaseError("database disk image is malformed")

    recovered = cache_mod._recover_corrupt_conversations(
        trigger,
        origin="cache_sync.cli.conversations.open",
        providers=("claude", "codex"),
        lock_timeout=0.05,
    )

    assert recovered is False
    assert _family_snapshot(path) == before
    assert not list((path.parent / "quarantine").glob("conversations.db-*"))
    assert not path.with_name("conversations.db.recovery.json").exists()
    assert not path.with_name("conversations.db.repairing").exists()


@pytest.mark.parametrize("message", [
    "database is locked",
    "disk I/O error",
    "attempt to write a readonly database",
    "no such table: conversation_messages",
])
def test_noncorruption_sqlite_failures_never_enter_recovery(
    tmp_path, monkeypatch, message,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    core = ns["_cctally_core"]
    conn = ns["open_conversations_db"](attach_cache=False)
    conn.close()
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    before = _family_snapshot(path)

    assert cache_mod._recover_corrupt_conversations(
        sqlite3.OperationalError(message),
        origin="cache_sync.cli.conversations.open",
        providers=("claude", "codex"),
        lock_timeout=0.05,
    ) is False

    assert _family_snapshot(path) == before
    assert not list((path.parent / "quarantine").glob("conversations.db-*"))


def test_forensics_failure_preserves_corrupt_family(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    core = ns["_cctally_core"]
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite transcript database")
    before = _family_snapshot(path)

    def unavailable(*_args, **_kwargs):
        raise OSError("forensics disk full")

    monkeypatch.setattr(
        ns["_cctally_db"], "write_corruption_forensics", unavailable,
    )
    recovered = cache_mod._recover_corrupt_conversations(
        sqlite3.DatabaseError("database disk image is malformed"),
        origin="cache_sync.cli.conversations.open",
        providers=("claude", "codex"),
        lock_timeout=0.05,
    )

    assert recovered is False
    assert _family_snapshot(path) == before
    assert not list((path.parent / "quarantine").glob("conversations.db-*"))
    assert not path.with_name("conversations.db.recovery.json").exists()


def test_real_open_handle_declines_recovery_and_preserves_family(tmp_path):
    env, data = _stage_sources(tmp_path)
    initial = _run(env, "cache-sync", "--source", "all", "--rebuild")
    assert initial.returncode == 0, initial.stderr
    path = data / "conversations.db"
    holder_code = (
        "import sqlite3,sys,time\n"
        "c=sqlite3.connect(sys.argv[1])\n"
        "c.execute('PRAGMA schema_version').fetchone()\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "ready"
    path.write_bytes(b"not a sqlite transcript database")
    before = _family_snapshot(path)
    try:
        declined = _run(env, "cache-sync", "--source", "all", "--rebuild")
        assert declined.returncode == 1
        assert "still open in process(es)" in declined.stderr
        assert _family_snapshot(path) == before
        assert not list((data / "quarantine").glob("conversations.db-*"))
        assert not (data / "conversations.db.recovery.json").exists()
    finally:
        holder.kill()
        holder.wait(timeout=10)
        holder.stdout.close()
        assert holder.stderr is not None
        holder.stderr.close()


def test_killed_quarantined_recovery_converges_on_next_invocation(tmp_path):
    env, data = _stage_sources(tmp_path)
    initial = _run(env, "cache-sync", "--source", "all", "--rebuild")
    assert initial.returncode == 0, initial.stderr
    accounting_before = _accounting_digest(data)
    path = data / "conversations.db"
    path.write_bytes(b"not a sqlite transcript database")

    fault_env = env.copy()
    fault_env.update({
        "PYTEST_CURRENT_TEST": (
            "test_killed_quarantined_recovery_converges_on_next_invocation"
        ),
        "CCTALLY_TEST_CONVERSATION_RECOVERY_STALL": "quarantined",
    })
    process = subprocess.Popen(
        [
            sys.executable, str(CCTALLY), "cache-sync",
            "--source", "all", "--rebuild",
        ],
        env=fault_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    state_path = data / "conversations.db.recovery.json"
    deadline = time.monotonic() + 15
    state = None
    while time.monotonic() < deadline:
        try:
            state = json.loads(state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.05)
            continue
        if state.get("phase") == "quarantined":
            break
        time.sleep(0.05)
    assert state is not None and state.get("phase") == "quarantined"
    os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=10)
    assert process.stdout is not None
    process.stdout.close()
    assert process.stderr is not None
    process.stderr.close()
    assert state_path.exists()
    assert (data / "conversations.db.repairing").exists()
    assert not path.exists()

    retry = _run(env, "cache-sync", "--source", "claude", "--rebuild")
    assert retry.returncode == 0, retry.stderr
    assert "claude transcripts done:" in retry.stderr
    assert "codex transcripts done:" in retry.stderr
    assert not state_path.exists()
    assert not (data / "conversations.db.repairing").exists()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_source_files"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_source_files"
        ).fetchone() == (1,)
    finally:
        conn.close()
    assert _accounting_digest(data) == accounting_before


def test_provider_lock_contention_declines_without_mutation(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    core = ns["_cctally_core"]
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite transcript database")
    before = _family_snapshot(path)
    core.CONVERSATIONS_LOCK_PATH.touch()
    holder = open(core.CONVERSATIONS_LOCK_PATH, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(
            sqlite3.DatabaseError, match="provider lock",
        ):
            cache_mod._recover_corrupt_conversations(
                sqlite3.DatabaseError("database disk image is malformed"),
                origin="cache_sync.cli.conversations.open",
                providers=("claude", "codex"),
                lock_timeout=0.05,
            )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert _family_snapshot(path) == before
    assert not list((path.parent / "quarantine").glob("conversations.db-*"))
    assert not path.with_name("conversations.db.recovery.json").exists()
    assert not path.with_name("conversations.db.repairing").exists()


def test_maintenance_lock_contention_declines_probe_without_mutation(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    core = ns["_cctally_core"]
    conn = ns["open_conversations_db"](attach_cache=False)
    conn.close()
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    before = _family_snapshot(path)
    maintenance = pathlib.Path(core.CONVERSATIONS_LOCK_MAINTENANCE_PATH)
    holder = maintenance.open("a+")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(
            sqlite3.DatabaseError, match="maintenance lock",
        ):
            cache_mod._prepare_conversation_rebuild(
                ("claude", "codex"), lock_timeout=0.05,
            )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert _family_snapshot(path) == before
    assert not list((path.parent / "quarantine").glob("conversations.db-*"))
    assert not path.with_name("conversations.db.recovery.json").exists()
    assert not path.with_name("conversations.db.repairing").exists()


def test_recovery_state_blocks_public_readers_until_all_providers_complete(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    conn = ns["open_conversations_db"](attach_cache=False)
    conn.close()
    cache_mod._write_conversation_recovery_state(
        providers=("claude", "codex"),
        phase="quarantined",
    )

    with pytest.raises(
        sqlite3.DatabaseError, match="recovery is incomplete",
    ):
        ns["open_conversations_db"](attach_cache=False)
    assert cache_mod.read_session_titles_bounded(["session-1"]) == {}

    internal = cache_mod._open_conversations_db_for_recovery(
        attach_cache=False,
    )
    internal.close()
    cache_mod._clear_conversation_recovery_state()
    reopened = ns["open_conversations_db"](attach_cache=False)
    reopened.close()


def test_probe_snapshot_copy_failure_preserves_live_family(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    core = ns["_cctally_core"]
    conn = ns["open_conversations_db"](attach_cache=False)
    conn.close()
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    before = _family_snapshot(path)

    def partial_clone(_source, destination):
        pathlib.Path(destination).write_bytes(b"partial snapshot")
        raise OSError("simulated clone failure")

    monkeypatch.setattr(
        cache_mod, "_clone_conversation_probe_member", partial_clone,
    )
    with pytest.raises(OSError, match="clone failure"):
        cache_mod._prepare_conversation_rebuild(
            ("claude", "codex"), lock_timeout=0.05,
        )

    assert _family_snapshot(path) == before
    assert not list((path.parent / "quarantine").glob("conversations.db-*"))
    assert not path.with_name("conversations.db.recovery.json").exists()


def test_probe_snapshot_test_copy_seam_requires_pytest_guard(
    tmp_path, monkeypatch,
):
    """The portable integration seam is inert outside an active pytest case."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    source.write_bytes(b"probe source")
    monkeypatch.setenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(cache_mod.sys, "platform", "linux")
    monkeypatch.setattr(cache_mod.shutil, "which", lambda _name: "/bin/cp")

    def fail_clone(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 1, "", "clone support unavailable",
        )

    monkeypatch.setattr(cache_mod.subprocess, "run", fail_clone)
    with pytest.raises(OSError, match="clone support unavailable"):
        cache_mod._clone_conversation_probe_member(source, destination)
    assert not destination.exists()

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "portable probe integration")

    def unexpected_subprocess(*_args, **_kwargs):
        raise AssertionError("pytest copy seam must not invoke cp")

    monkeypatch.setattr(cache_mod.subprocess, "run", unexpected_subprocess)
    cache_mod._clone_conversation_probe_member(source, destination)
    assert destination.read_bytes() == b"probe source"


def test_probe_snapshot_uses_bounded_cow_clone_and_cleans_stale_dirs(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    core = ns["_cctally_core"]
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"probe source")
    stale = path.parent / ".conversations-probe-stale"
    stale.mkdir()
    (stale / "leftover").write_bytes(b"stale")
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        pathlib.Path(command[-1]).write_bytes(
            pathlib.Path(command[-2]).read_bytes()
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cache_mod.sys, "platform", "darwin")
    monkeypatch.setattr(cache_mod.shutil, "which", lambda _name: "/bin/cp")
    monkeypatch.setattr(cache_mod.subprocess, "run", fake_run)
    with cache_mod._conversation_probe_snapshot(path) as snapshot:
        assert snapshot.read_bytes() == b"probe source"
        assert not stale.exists()

    assert not snapshot.parent.exists()
    assert commands[0][0][1] == "-c"
    assert commands[0][1]["timeout"] == (
        cache_mod._CONVERSATION_PROBE_CLONE_TIMEOUT_SECONDS
    )


def test_confirmed_forensics_uses_durable_json_publication(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    db_mod = ns["_cctally_db"]
    core = ns["_cctally_core"]
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite transcript database")
    published = []
    real_publish = db_mod._atomic_write_private_json

    def recording_publish(output, payload):
        published.append(pathlib.Path(output))
        real_publish(output, payload)

    monkeypatch.setattr(
        db_mod, "_atomic_write_private_json", recording_publish,
    )
    result = db_mod.write_corruption_forensics(
        path,
        db_label="conversations",
        trigger_origin="test.durable_forensics",
        trigger_exception=sqlite3.DatabaseError(
            "database disk image is malformed"
        ),
        return_result=True,
    )

    assert result.path is not None
    assert published == [result.path]
    assert json.loads(result.path.read_text())["probeDisposition"] == "confirmed"
