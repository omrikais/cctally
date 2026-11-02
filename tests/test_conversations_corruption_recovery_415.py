"""Issue #415 Task A: conversations.db diagnosis and crash-safe recovery."""
from __future__ import annotations
import types

import argparse
import errno
import fcntl
import hashlib
import importlib
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


# --- #1000: the probe clones or checks in place, never byte-copies ----------

_WAL_CLAUSE = (
    "conversations.db-wal is not empty, so the live file cannot be checked "
    "on its own"
)
_JOURNAL_CLAUSE = (
    "conversations.db-journal is not empty, so the live file cannot be "
    "checked on its own"
)
_DRIFT_CLAUSE = (
    "conversations.db changed while it was checked in place, so the result "
    "was discarded"
)
_PROBE_DIR_PREFIX = ".conversations-probe-"


def _refusal(code: int, name: str = "conversations.db") -> str:
    """The Darwin clone refusal (#1000 spec 5.2)."""
    return (
        f"copy-on-write transcript probe unavailable for {name}: "
        f"clonefile failed: {os.strerror(code)}"
    )


def _skip_line(refusal: str, clause: "str | None" = None) -> str:
    reason = refusal if clause is None else f"{refusal}; {clause}"
    return (
        "[cache-sync] conversations.db integrity check skipped before the "
        f"transcript rebuild ({reason}); continuing without automatic "
        "corruption recovery"
    )


def _decline_line(origin: str, reason: str) -> str:
    return (
        "[conversations] destructive recovery declined for classified "
        f"trigger at {origin}: forensics was unavailable ({reason}); leaving "
        "the conversations.db family untouched"
    )


def _darwin_clone_seams(monkeypatch, cache_mod, *, refuse: "int | None"):
    """Select Darwin for the importer and the clone module (spec 8, Seams).

    ``refuse`` is the errno `_clone_fd` raises, or None to emulate a
    successful clone. Returns the list of destinations the clone was asked for.
    """
    clone_mod = importlib.import_module("_lib_clone_copy")
    iso_sys = types.SimpleNamespace(**vars(cache_mod.sys))
    iso_sys.platform = "darwin"
    monkeypatch.setattr(cache_mod, "sys", iso_sys)
    monkeypatch.setattr(clone_mod, "_PLATFORM", "darwin")
    monkeypatch.setattr(clone_mod, "_fclonefileat", lambda: (lambda *_a: 0))
    attempts: list[str] = []

    def clone_fd(fd, destination):
        attempts.append(str(destination))
        if refuse is not None:
            raise OSError(refuse, os.strerror(refuse), destination)
        size = os.fstat(fd).st_size
        with open(destination, "xb") as fh:
            fh.write(os.pread(fd, size, 0))

    monkeypatch.setattr(clone_mod, "_clone_fd", clone_fd)
    return attempts


def _forbid_subprocess(monkeypatch, cache_mod) -> None:
    """Fail any `subprocess.run` through `_cctally_cache` (never the shared
    module, which the census's `lsof` resolves through)."""
    def unexpected(*_args, **_kwargs):
        raise AssertionError("the Darwin probe must not run a subprocess")

    proxy = types.SimpleNamespace(**vars(cache_mod.subprocess))
    proxy.run = unexpected
    monkeypatch.setattr(cache_mod, "subprocess", proxy)


def _no_clone(monkeypatch) -> None:
    """Refuse every member clone through the child-side seam (ENOTSUP)."""
    monkeypatch.delenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", raising=False)
    monkeypatch.setenv("CCTALLY_TEST_CONVERSATION_PROBE_CLONE_ERRNO", "ENOTSUP")


def _full_family(path: pathlib.Path) -> dict[str, bytes]:
    """Every family member, the rollback journal included, by name."""
    return {
        pathlib.Path(f"{path}{suffix}").name:
        pathlib.Path(f"{path}{suffix}").read_bytes()
        for suffix in ("", "-wal", "-shm", "-journal")
        if os.path.lexists(f"{path}{suffix}")
    }


def _fresh_store(ns) -> pathlib.Path:
    """A clean WAL-less conversations store (closed, no -wal/-shm left)."""
    conn = ns["open_conversations_db"](attach_cache=False)
    conn.close()
    path = pathlib.Path(ns["_cctally_core"].CONVERSATIONS_DB_PATH)
    for suffix in ("-wal", "-shm"):
        pathlib.Path(f"{path}{suffix}").unlink(missing_ok=True)
    return path


def _stage_log(monkeypatch, cache_mod, act=None) -> list:
    """Install the in-process probe stage hook; ``act(stage)`` runs at each."""
    log: list[str] = []

    def hook(stage):
        log.append(stage)
        if act is not None:
            act(stage)

    monkeypatch.setattr(cache_mod, "_CONVERSATION_PROBE_STAGE_HOOK", hook)
    return log


def _append_byte(path: pathlib.Path) -> None:
    """A guaranteed identity change: the size and mtime move."""
    with open(path, "ab") as fh:
        fh.write(b"\0")


# T1's observer. Audit hooks cannot be removed, so it is installed once per
# module and records only while `_PROBE_AUDIT["active"]` is set.
_PROBE_AUDIT: dict = {"active": False, "events": [], "installed": False}
_WRITE_FLAGS = (
    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
)
_SPAWN_EVENTS = frozenset({
    "subprocess.Popen", "os.posix_spawn", "os.exec", "os.system",
})


def _probe_audit_hook(event, args):
    if not _PROBE_AUDIT["active"]:
        return
    try:
        if event in _SPAWN_EVENTS:
            _PROBE_AUDIT["events"].append(("spawn", event, repr(args)[:200]))
        elif event == "open":
            target, mode, flags = (tuple(args) + (None, None, None))[:3]
            if isinstance(target, (str, bytes, os.PathLike)):
                name = os.fsdecode(target)
                writing = bool(isinstance(flags, int) and flags & _WRITE_FLAGS)
                if isinstance(mode, str) and any(c in mode for c in "wax+"):
                    writing = True
                under_probe = any(
                    part.startswith(_PROBE_DIR_PREFIX)
                    for part in pathlib.PurePath(name).parts
                )
                if writing and under_probe:
                    _PROBE_AUDIT["events"].append(("write", name, mode))
    except Exception:  # noqa: BLE001 — an observer never breaks the call
        pass


def _observe_probe(call):
    """Run ``call`` with the audit observer recording; return its events."""
    if not _PROBE_AUDIT["installed"]:
        sys.addaudithook(_probe_audit_hook)
        _PROBE_AUDIT["installed"] = True
    _PROBE_AUDIT["events"] = []
    _PROBE_AUDIT["active"] = True
    try:
        result = call()
    finally:
        _PROBE_AUDIT["active"] = False
    return result, list(_PROBE_AUDIT["events"])


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
    # #630 S2: patch the IMPORTER's reference, never the shared
    # stdlib module object, which every other importer and every
    # concurrent thread resolves through.
    _iso_sys = types.SimpleNamespace(**vars(cache_mod.sys))
    _iso_sys.platform = "linux"
    monkeypatch.setattr(cache_mod, "sys", _iso_sys)
    # #630 S2: patch the IMPORTER's reference, never the shared
    # stdlib module object, which every other importer and every
    # concurrent thread resolves through.
    _iso_shutil = types.SimpleNamespace(**vars(cache_mod.shutil))
    _iso_shutil.which = lambda _name: "/bin/cp"
    monkeypatch.setattr(cache_mod, "shutil", _iso_shutil)

    def fail_clone(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 1, "", "clone support unavailable",
        )

    # #630 S2: patch the IMPORTER's reference, never the shared stdlib
    # module object, which every other importer and every concurrent
    # thread resolves through. The later line flips behaviour on the
    # copy already installed, so the restore does not need a second
    # setattr.
    _iso_subprocess = types.SimpleNamespace(**vars(cache_mod.subprocess))
    _iso_subprocess.run = fail_clone
    monkeypatch.setattr(cache_mod, "subprocess", _iso_subprocess)
    with pytest.raises(OSError, match="clone support unavailable"):
        cache_mod._clone_conversation_probe_member(source, destination)
    assert not destination.exists()

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "portable probe integration")

    def unexpected_subprocess(*_args, **_kwargs):
        raise AssertionError("pytest copy seam must not invoke cp")

    _iso_subprocess.run = unexpected_subprocess
    cache_mod._clone_conversation_probe_member(source, destination)
    assert destination.read_bytes() == b"probe source"


def test_the_darwin_probe_clones_in_process_and_cleans_stale_dirs(
    tmp_path, monkeypatch,
):
    """Machine safety (#1000): the Darwin probe clones through clonefile(2)
    in-process and never runs `cp -c`, whose copyfile(2) fallback wrote a full
    copy of the store beside it on HFS+/exFAT; stale probe dirs are removed."""
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
    monkeypatch.delenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", raising=False)
    clones = _darwin_clone_seams(monkeypatch, cache_mod, refuse=None)
    _forbid_subprocess(monkeypatch, cache_mod)
    with cache_mod._conversation_probe_source(path) as source:
        assert source.immutable is False
        assert source.probe_path != path
        assert source.probe_path.read_bytes() == b"probe source"
        assert not stale.exists()

    assert clones == [str(source.probe_path)]
    assert not source.probe_path.parent.exists()


def _fake_cp(
    tmp_path: pathlib.Path,
    monkeypatch,
    stderr: str,
    *,
    status: int = 1,
) -> pathlib.Path:
    """Put a `cp` on PATH that writes ``stderr`` and exits ``status``.

    The pytest copy seam is dropped so the real clone helper runs it. Returns
    the marker the fake touches, so a test can prove the clone was attempted.
    """
    fake_bin = tmp_path / "fake-cp-bin"
    fake_bin.mkdir()
    message = fake_bin / "stderr.txt"
    message.write_text(stderr + "\n", encoding="utf-8")
    invoked = fake_bin / "invoked"
    fake = fake_bin / "cp"
    fake.write_text(
        "#!/bin/sh\n"
        f": > '{invoked}'\n"
        f"cat '{message}' >&2\n"
        f"exit {status}\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", raising=False)
    return invoked


def _select_linux(monkeypatch, cache_mod) -> None:
    """Run the GNU `cp --reflink=always` branch on any host (#1000 spec 7).

    Darwin no longer runs `cp` at all, and the runners are macOS, so a fake-cp
    test must select Linux on the importer's own `sys`, never the shared one.
    """
    iso_sys = types.SimpleNamespace(**vars(cache_mod.sys))
    iso_sys.platform = "linux"
    monkeypatch.setattr(cache_mod, "sys", iso_sys)


def _gnu_clone_failure(error: int) -> str:
    """GNU coreutils' `cp --reflink=always` message for a failed FICLONE."""
    return (
        "cp: failed to clone '/d/.conversations-probe-x/conversations.db' "
        f"from '/d/conversations.db': {os.strerror(error)}"
    )


_CLONE_REFUSALS = {
    **{
        f"gnu-{name}": _gnu_clone_failure(getattr(errno, name))
        for name in ("ENOTSUP", "EOPNOTSUPP", "EXDEV", "ENOTTY", "EINVAL")
    },
    "macos-ENOTSUP": (
        "cp: /d/.conversations-probe-x/conversations.db: clonefile failed: "
        f"{os.strerror(errno.ENOTSUP)}"
    ),
}

_CLONE_FAILURES = {
    **{
        f"gnu-clone-{name}": _gnu_clone_failure(getattr(errno, name))
        for name in ("ENOSPC", "EDQUOT", "EIO", "EPERM", "EACCES")
    },
    "gnu-open-EACCES": (
        "cp: cannot open '/d/conversations.db' for reading: "
        f"{os.strerror(errno.EACCES)}"
    ),
    "gnu-stat-ENOENT": (
        f"cp: cannot stat '/d/conversations.db': {os.strerror(errno.ENOENT)}"
    ),
    "gnu-create-ENOSPC": (
        "cp: cannot create regular file "
        "'/d/.conversations-probe-x/conversations.db': "
        f"{os.strerror(errno.ENOSPC)}"
    ),
    "macos-copyfile-EIO": (
        f"cp: /d/conversations.db: {os.strerror(errno.EIO)}"
    ),
    "macos-clonefile-EIO": (
        "cp: /d/.conversations-probe-x/conversations.db: clonefile failed: "
        f"{os.strerror(errno.EIO)}"
    ),
    "no-stderr": "",
}


@pytest.mark.parametrize(
    "stderr", list(_CLONE_REFUSALS.values()), ids=list(_CLONE_REFUSALS),
)
def test_a_filesystem_that_cannot_clone_is_reported_as_unavailable(
    tmp_path, monkeypatch, stderr,
):
    """Only a refusal meaning "this filesystem cannot clone" is unavailability.

    The pre-rebuild quick-check skips on that error and the rebuild continues,
    so it must be reserved for a permanent property of the filesystem.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    invoked = _fake_cp(tmp_path, monkeypatch, stderr)
    _select_linux(monkeypatch, cache_mod)
    source = tmp_path / "source.db"
    source.write_bytes(b"probe source")

    with pytest.raises(cache_mod.ConversationProbeCloneUnavailable):
        cache_mod._clone_conversation_probe_member(
            source, tmp_path / "snapshot.db",
        )
    assert invoked.exists()


@pytest.mark.parametrize(
    "stderr", list(_CLONE_FAILURES.values()), ids=list(_CLONE_FAILURES),
)
def test_a_clone_that_fails_for_another_reason_is_not_unavailability(
    tmp_path, monkeypatch, stderr,
):
    """Out of space, permission, a vanished source or an I/O error must fail.

    An I/O error reading a damaged store is the very signal the probe exists to
    catch, so none of these may be mistaken for a filesystem without clones.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    invoked = _fake_cp(tmp_path, monkeypatch, stderr)
    _select_linux(monkeypatch, cache_mod)
    source = tmp_path / "source.db"
    source.write_bytes(b"probe source")

    with pytest.raises(OSError) as raised:
        cache_mod._clone_conversation_probe_member(
            source, tmp_path / "snapshot.db",
        )
    assert invoked.exists()
    assert not isinstance(
        raised.value, cache_mod.ConversationProbeCloneUnavailable,
    )
    if stderr:
        assert stderr in str(raised.value)


def test_a_clone_failure_that_is_not_a_refusal_still_fails_the_rebuild(
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
    invoked = _fake_cp(
        tmp_path, monkeypatch, _gnu_clone_failure(errno.ENOSPC),
    )
    _select_linux(monkeypatch, cache_mod)

    with pytest.raises(OSError, match="No space left on device") as raised:
        cache_mod._prepare_conversation_rebuild(
            ("claude", "codex"), lock_timeout=0.05,
        )

    assert invoked.exists()
    assert not isinstance(
        raised.value, cache_mod.ConversationProbeCloneUnavailable,
    )
    assert _family_snapshot(path) == before
    assert not list(path.parent.glob(".conversations-probe-*"))
    assert not list((path.parent / "quarantine").glob("conversations.db-*"))


def _corrupt_header(path: pathlib.Path) -> None:
    path.write_bytes(b"not a sqlite transcript database")


def _corrupt_second_page(path: pathlib.Path) -> None:
    """Damage a page that opening never reads; only quick_check finds it."""
    raw = bytearray(path.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big")
    if page_size == 1:
        page_size = 65_536
    assert len(raw) >= page_size * 3
    raw[page_size:page_size * 2] = b"\xa5" * page_size
    path.write_bytes(raw)


@pytest.mark.parametrize("wal", ["no-wal", "wal-pending"])
@pytest.mark.parametrize(
    "corrupt", [_corrupt_header, _corrupt_second_page],
    ids=["header", "interior-page"],
)
def test_a_corrupt_store_without_clones_is_checked_in_place(
    tmp_path, corrupt, wal,
):
    """Stored-data accuracy (#1000 T5): on a volume that cannot clone, a
    corrupt store with no pending WAL is checked in place and recovered
    (quarantined, transcripts rebuilt); with a pending WAL the check cannot be
    exact, so it is skipped with the exact D2 line and nothing is quarantined.

    The child cannot be steered by a parent-side platform patch, so it refuses
    the clone through the child-side errno seam, as an HFS+ volume does.
    """
    env, data = _stage_sources(tmp_path)
    initial = _run(env, "cache-sync", "--source", "all", "--rebuild")
    assert initial.returncode == 0, initial.stderr
    path = data / "conversations.db"
    for suffix in ("-wal", "-shm"):
        pathlib.Path(f"{path}{suffix}").unlink(missing_ok=True)
    if wal == "wal-pending":
        _leave_live_wal_family(path)
        assert pathlib.Path(f"{path}-wal").stat().st_size > 0
    corrupt(path)
    corrupt_main = path.read_bytes()
    inode = path.stat().st_ino
    no_clone_env = dict(env)
    no_clone_env.pop("CCTALLY_TEST_CONVERSATION_PROBE_COPY")
    no_clone_env["CCTALLY_TEST_CONVERSATION_PROBE_CLONE_ERRNO"] = "ENOTSUP"

    result = _run(no_clone_env, "cache-sync", "--source", "all", "--rebuild")

    assert not list(data.glob(".conversations-probe-*"))
    assert not (data / "conversations.db.recovery.json").exists()
    assert not (data / "conversations.db.repairing").exists()
    if wal == "wal-pending":
        assert _skip_line(_refusal(errno.ENOTSUP), _WAL_CLAUSE) in (
            result.stderr.splitlines()
        ), result.stderr
        if corrupt is _corrupt_header:
            assert result.returncode == 1, result.stderr
            assert "file is not a database" in result.stderr
        assert path.stat().st_ino == inode
        assert not list((data / "quarantine").glob("conversations.db-*"))
        return
    assert result.returncode == 0, result.stderr
    assert "integrity check skipped" not in result.stderr, result.stderr
    assert "quarantined its file family" in result.stderr
    incidents = list((data / "quarantine").glob("conversations.db-*"))
    assert len(incidents) == 1
    assert (incidents[0] / "conversations.db").read_bytes() == corrupt_main
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_source_files"
        ).fetchone() == (1,)
    finally:
        conn.close()


def test_a_corrupt_store_recovers_on_a_volume_without_hard_links(tmp_path):
    """Stored-data accuracy (#1020 T6): on a volume that has neither clones
    nor hard links (exFAT), a corrupt store is still quarantined and rebuilt,
    because the repair marker and the keeper-yield request publish in their
    directory form. The trace proves the fallback published (a run that
    ignored the link seam would publish files and fail here)."""
    env, data = _stage_sources(tmp_path)
    initial = _run(env, "cache-sync", "--source", "all", "--rebuild")
    assert initial.returncode == 0, initial.stderr
    path = data / "conversations.db"
    for suffix in ("-wal", "-shm"):
        pathlib.Path(f"{path}{suffix}").unlink(missing_ok=True)
    _corrupt_second_page(path)
    corrupt_main = path.read_bytes()
    trace = tmp_path / "owner-record-trace.txt"
    child_env = dict(env)
    child_env.pop("CCTALLY_TEST_CONVERSATION_PROBE_COPY")
    child_env["CCTALLY_TEST_CONVERSATION_PROBE_CLONE_ERRNO"] = "ENOTSUP"
    child_env["CCTALLY_TEST_OWNER_RECORD_LINK_ERRNO"] = "ENOTSUP"
    child_env["CCTALLY_TEST_OWNER_RECORD_TRACE"] = str(trace)

    result = _run(child_env, "cache-sync", "--source", "all", "--rebuild")

    assert result.returncode == 0, result.stderr
    published = trace.read_text().splitlines() if trace.exists() else []
    assert "conversations.db.repairing directory" in published, published
    assert "conversations.db.keeper-yield directory" in published, published
    assert "quarantined its file family" in result.stderr
    incidents = list((data / "quarantine").glob("conversations.db-*"))
    assert len(incidents) == 1
    assert (incidents[0] / "conversations.db").read_bytes() == corrupt_main
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        claude_sources = [
            row[0] for row in conn.execute(
                "SELECT path FROM conversation_source_files"
            )
        ]
        assert [pathlib.Path(item).name for item in claude_sources] == [
            "session.jsonl"
        ], claude_sources
        codex_sources = [
            row[0] for row in conn.execute(
                "SELECT path FROM codex_conversation_source_files"
            )
        ]
        assert [pathlib.Path(item).name for item in codex_sources] == [
            "rollout-modern-full.jsonl"
        ], codex_sources
        claude_text = " ".join(
            str(row) for row in conn.execute("SELECT * FROM conversation_messages")
        )
        assert "hello recovery" in claude_text
        codex_text = " ".join(
            str(row)
            for row in conn.execute("SELECT * FROM codex_conversation_messages")
        )
        assert "Synthetic assistant response" in codex_text
    finally:
        conn.close()
    leftovers = sorted(
        entry.name for entry in data.iterdir()
        if (
            entry.name.startswith(".conversations.db.")
            or entry.name.startswith("conversations.db.repairing")
            or entry.name.startswith("conversations.db.keeper-yield")
        )
        and not entry.name.endswith(".lock")
    )
    assert leftovers == [], leftovers
    assert not (data / "conversations.db.recovery.json").exists()


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


# --- #1000 T1: no byte copy on a volume that cannot clone --------------------

@pytest.mark.parametrize("wal", ["wal-less", "wal-pending"])
@pytest.mark.parametrize(
    "code", [errno.ENOTSUP, errno.EXDEV], ids=["ENOTSUP", "EXDEV"],
)
def test_a_probe_without_clones_never_writes_a_byte_copy(
    tmp_path, monkeypatch, capsys, code, wal,
):
    """Machine safety (#1000 T1): on a volume that cannot clone, the probe
    must write no byte copy of conversations.db. macOS `cp -c` falls back to
    copyfile(2), so the baseline wrote the whole store (269 MB measured on
    HFS+, and ENOSPC on a small volume) beside it on every rebuild.

    The recorder on `_cctally_cache`'s own `subprocess` reference behaves as
    that `cp -c` does; the audit hook catches any write-intent open under a
    probe directory, through cleanup, and any process spawn. The census and
    the keeper-yield owner identity are stubbed, because the exclusion
    legitimately runs `lsof` and `ps`; T6, T8 and T9 cover their real behavior.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    db_mod = ns["_cctally_db"]
    path = _fresh_store(ns)
    if wal == "wal-pending":
        _leave_live_wal_family(path)
        assert pathlib.Path(f"{path}-wal").stat().st_size > 0
    monkeypatch.delenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", raising=False)
    monkeypatch.delenv(
        "CCTALLY_TEST_CONVERSATION_PROBE_CLONE_ERRNO", raising=False,
    )
    monkeypatch.setattr(db_mod, "_db_family_open_pids", lambda _path: set())
    monkeypatch.setattr(
        db_mod, "_process_start_identity", lambda _pid: "proc:test",
    )
    attempts = _darwin_clone_seams(monkeypatch, cache_mod, refuse=code)
    recorded: list[tuple[list[str], int]] = []

    def copying_cp(command, **_kwargs):
        # What macOS `cp -c` does on a volume without clones: a full copy.
        data = pathlib.Path(command[-2]).read_bytes()
        pathlib.Path(command[-1]).write_bytes(data)
        recorded.append((list(command), len(data)))
        return subprocess.CompletedProcess(command, 0, "", "")

    proxy = types.SimpleNamespace(**vars(cache_mod.subprocess))
    proxy.run = copying_cp
    monkeypatch.setattr(cache_mod, "subprocess", proxy)

    trigger, events = _observe_probe(
        lambda: cache_mod._probe_conversation_rebuild(path, lock_timeout=0.05)
    )

    assert not recorded, (
        "the probe ran "
        + "; ".join(" ".join(argv) for argv, _ in recorded)
        + f" and wrote {sum(size for _, size in recorded)} bytes of the "
        "store into the probe directory"
    )
    writes = [event for event in events if event[0] == "write"]
    spawns = [event for event in events if event[0] == "spawn"]
    assert not writes, f"write-intent opens under a probe dir: {writes}"
    assert not spawns, f"the probe spawned processes: {spawns}"
    assert attempts, "the in-process clone was never attempted"
    assert trigger is None
    stderr = capsys.readouterr().err
    if wal == "wal-less":
        assert "integrity check skipped" not in stderr, stderr
    else:
        assert _skip_line(_refusal(code), _WAL_CLAUSE) in stderr.splitlines()
    assert not list(path.parent.glob(f"{_PROBE_DIR_PREFIX}*"))


# --- #1000 T2 (probe side): errno classification ----------------------------

_DARWIN_REFUSALS = ("ENOTSUP", "EOPNOTSUPP", "EXDEV")
_DARWIN_FAILURES = ("EINVAL", "EPERM", "EEXIST", "EIO", "ENOSPC")


@pytest.mark.parametrize("name", [
    *(n for n in _DARWIN_REFUSALS if hasattr(errno, n)),
    *_DARWIN_FAILURES,
])
def test_the_darwin_member_clone_classifies_each_errno(
    tmp_path, monkeypatch, name,
):
    """Machine safety and accuracy (#1000 T2): only "this volume cannot
    clone" (ENOTSUP, EOPNOTSUPP, EXDEV) may skip or relocate the check; an I/O
    error, ENOSPC or a permission failure is real and must fail the rebuild,
    because an I/O error reading a damaged store is what the probe exists to
    catch. The cause is chained either way."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    monkeypatch.delenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", raising=False)
    code = getattr(errno, name)
    attempts = _darwin_clone_seams(monkeypatch, cache_mod, refuse=code)
    _forbid_subprocess(monkeypatch, cache_mod)
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    source.write_bytes(b"probe source")

    with pytest.raises(OSError) as raised:
        cache_mod._clone_conversation_probe_member(source, destination)

    assert attempts
    assert not destination.exists()
    cause = raised.value.__cause__
    assert isinstance(cause, OSError) and cause.errno == code
    if name in _DARWIN_REFUSALS:
        assert isinstance(
            raised.value, cache_mod.ConversationProbeCloneUnavailable,
        )
        assert str(raised.value) == _refusal(code, "source.db")
        assert raised.value.refusal == _refusal(code, "source.db")
        assert raised.value.clause is None
    else:
        assert not isinstance(
            raised.value, cache_mod.ConversationProbeCloneUnavailable,
        )
        assert str(raised.value) == (
            f"copy-on-write transcript probe failed for source.db: {cause}"
        )


@pytest.mark.parametrize("pytest_active", [True, False],
                         ids=["under-pytest", "production"])
def test_the_clone_errno_seam_classifies_like_a_real_refusal(
    tmp_path, monkeypatch, pytest_active,
):
    """Test-seam safety (#1000 spec 8): the child-side errno seam refuses the
    clone through the real classification, and is inert outside pytest."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    monkeypatch.delenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", raising=False)
    monkeypatch.setenv("CCTALLY_TEST_CONVERSATION_PROBE_CLONE_ERRNO", "EXDEV")
    attempts = _darwin_clone_seams(monkeypatch, cache_mod, refuse=None)
    _forbid_subprocess(monkeypatch, cache_mod)
    if not pytest_active:
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    source.write_bytes(b"probe source")
    if not pytest_active:
        cache_mod._clone_conversation_probe_member(source, destination)
        assert attempts and destination.read_bytes() == b"probe source"
        return
    with pytest.raises(cache_mod.ConversationProbeCloneUnavailable) as raised:
        cache_mod._clone_conversation_probe_member(source, destination)
    assert str(raised.value) == _refusal(errno.EXDEV, "source.db")
    assert not attempts and not destination.exists()


# --- #1000 T3: a real APFS clone is unchanged -------------------------------

def test_a_real_clone_reads_the_wal_and_leaves_the_family_untouched(
    tmp_path, monkeypatch,
):
    """Stored-data accuracy (#1000 T3, A4): where the volume clones, the probe
    clones main and WAL, so rows committed only in the WAL are checked, and
    the live family stays byte-identical."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    path = _fresh_store(ns)
    _leave_live_wal_family(path)
    monkeypatch.delenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", raising=False)
    monkeypatch.delenv(
        "CCTALLY_TEST_CONVERSATION_PROBE_CLONE_ERRNO", raising=False,
    )
    before = _full_family(path)
    try:
        with cache_mod._conversation_probe_source(path) as source:
            assert source.immutable is False
            assert source.probe_path != path
            conn = sqlite3.connect(
                cache_mod._conversation_probe_uri(source), uri=True,
            )
            try:
                assert conn.execute(
                    "SELECT value FROM cache_meta "
                    "WHERE key='issue_415_wal_evidence'"
                ).fetchone() == ("retained",)
            finally:
                conn.close()
    except cache_mod.ConversationProbeCloneUnavailable as exc:
        # Only a host whose scratch cannot clone (not the macOS runners).
        assert sys.platform != "darwin", exc
        assert exc.clause == _WAL_CLAUSE
        return
    assert _full_family(path) == before
    assert cache_mod._probe_conversation_rebuild(
        path, lock_timeout=5.0,
    ) is None
    assert _full_family(path) == before
    assert not list(path.parent.glob(f"{_PROBE_DIR_PREFIX}*"))


# --- #1000 T4: no-follow ----------------------------------------------------

def _link_state(paths) -> dict:
    out = {}
    for item in paths:
        item = pathlib.Path(item)
        if item.is_symlink():
            out[str(item)] = ("link", os.readlink(item))
        elif item.exists():
            out[str(item)] = ("file", item.read_bytes())
        else:
            out[str(item)] = None
    return out


@pytest.mark.parametrize(
    "shape", ["symlinked-main", "dangling-main", "symlinked-wal"],
)
def test_a_symlinked_store_member_fails_the_rebuild_untouched(
    tmp_path, monkeypatch, shape,
):
    """Stored-data safety (#1000 T4, P3): a symlinked store member must fail
    the rebuild rather than be cloned, checked or recreated through the link;
    a dangling main symlink must not bypass the probe and have the opener
    create its target. The family and every link target stay byte-identical."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    core = ns["_cctally_core"]
    # The member validation precedes any clone, so the portable copy seam
    # keeps this host-independent.
    monkeypatch.setenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", "1")
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    wal = pathlib.Path(f"{path}-wal")
    member = "conversations.db"
    if shape == "symlinked-main":
        target = elsewhere / "real.db"
        conn = sqlite3.connect(target)
        conn.execute("CREATE TABLE t (v)")
        conn.commit()
        conn.close()
        os.symlink(target, path)
    elif shape == "dangling-main":
        target = elsewhere / "missing.db"
        os.symlink(target, path)
    else:
        path = _fresh_store(ns)
        target = elsewhere / "wal-target"
        target.write_bytes(b"")
        os.symlink(target, wal)
        member = "conversations.db-wal"
    watched = [path, wal, pathlib.Path(f"{path}-shm"), target]
    before = _link_state(watched)

    with pytest.raises(OSError) as raised:
        cache_mod._prepare_conversation_rebuild(
            ("claude", "codex"), lock_timeout=0.05,
        )

    assert not isinstance(raised.value, sqlite3.DatabaseError)
    assert str(raised.value) == (
        f"copy-on-write transcript probe failed for {member}: "
        f"{os.strerror(errno.ELOOP)}"
    )
    assert _link_state(watched) == before
    assert not list(path.parent.glob(f"{_PROBE_DIR_PREFIX}*"))
    assert not list((path.parent / "quarantine").glob("conversations.db-*"))
    assert not path.with_name("conversations.db.recovery.json").exists()


# --- #1000 T5 (in-process): eligibility, forensics, escaping ----------------

@pytest.mark.parametrize(
    "member", ["empty-wal", "empty-journal", "stale-shm", "pending-journal"],
)
def test_live_check_eligibility_boundaries(
    tmp_path, monkeypatch, capsys, member,
):
    """Stored-data accuracy (#1000 T5, D1): without a clone, the live main
    file is checked in place only when it alone is the committed state. An
    empty -wal or -journal and a stale -shm do not change that and are never
    touched; a non-empty -journal does, so the check is skipped with its
    exact D2 line."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    path = _fresh_store(ns)
    _no_clone(monkeypatch)
    suffix, content = {
        "empty-wal": ("-wal", b""),
        "empty-journal": ("-journal", b""),
        "stale-shm": ("-shm", b"\0" * 32768),
        "pending-journal": ("-journal", b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"),
    }[member]
    pathlib.Path(f"{path}{suffix}").write_bytes(content)
    before = _full_family(path)
    log = _stage_log(monkeypatch, cache_mod)

    trigger = cache_mod._probe_conversation_rebuild(path, lock_timeout=5.0)

    stderr = capsys.readouterr().err
    assert trigger is None
    assert _full_family(path) == before
    if member == "pending-journal":
        assert _skip_line(_refusal(errno.ENOTSUP), _JOURNAL_CLAUSE) in (
            stderr.splitlines()
        ), stderr
        assert "probe-read-buffered" not in log
    else:
        assert "integrity check skipped" not in stderr, stderr
        assert "probe-read-buffered" in log, "the live check never ran"


def test_the_immutable_forensic_read_leaves_the_live_family_byte_identical(
    tmp_path, monkeypatch,
):
    """Stored-data safety (#1000 T5, spec 5.7): the live forensic read opens
    immutable, because a plain `mode=ro` open of a WAL-mode store creates
    `-wal` and `-shm` beside it (measured, spec 4.2) and would mutate the
    family recovery is about to preserve."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    db_mod = ns["_cctally_db"]
    path = _fresh_store(ns)
    _corrupt_second_page(path)
    before = _full_family(path)
    assert set(before) == {"conversations.db"}

    result = db_mod.write_corruption_forensics(
        path,
        probe_db_path=path,
        probe_immutable=True,
        db_label="conversations",
        trigger_origin="test.immutable_forensics",
        trigger_exception=sqlite3.DatabaseError(
            "database disk image is malformed"
        ),
        return_result=True,
    )

    assert result.disposition is db_mod.CorruptionProbeDisposition.CONFIRMED
    assert _full_family(path) == before


def test_the_live_check_opens_a_path_that_needs_uri_escaping(
    tmp_path, monkeypatch, capsys,
):
    """Stored-data accuracy (#1000 T5): a data folder whose path holds `?`,
    `#` or a space must open the live file itself, not a truncated path that
    reports a healthy (or missing) store, and recovery's immutable forensic
    read must confirm the corruption there too, or recovery would decline as
    "not confirmed" on such a data folder."""
    root = tmp_path / "a?b#c d"
    root.mkdir()
    ns = load_script()
    redirect_paths(ns, monkeypatch, root)
    cache_mod = ns["_cctally_cache"]
    path = _fresh_store(ns)
    _no_clone(monkeypatch)
    log = _stage_log(monkeypatch, cache_mod)

    assert cache_mod._probe_conversation_rebuild(
        path, lock_timeout=5.0,
    ) is None
    assert log.count("probe-read-buffered") == 1
    _corrupt_second_page(path)
    trigger = cache_mod._probe_conversation_rebuild(path, lock_timeout=5.0)
    assert isinstance(trigger, sqlite3.DatabaseError)
    assert "quick_check" in str(trigger)
    assert "integrity check skipped" not in capsys.readouterr().err
    db_mod = ns["_cctally_db"]
    result = db_mod.write_corruption_forensics(
        path,
        probe_db_path=path,
        probe_immutable=True,
        db_label="conversations",
        trigger_origin="test.escaped_forensics",
        trigger_exception=trigger,
        return_result=True,
    )
    assert result.disposition is db_mod.CorruptionProbeDisposition.CONFIRMED


# --- #1000 T6: drift and holders never yield a trusted result ---------------

@pytest.mark.parametrize("drift", [True, False], ids=["drift", "control"])
@pytest.mark.parametrize("state", ["ok", "corrupt"])
def test_drift_after_the_live_read_discards_the_probe_result(
    tmp_path, monkeypatch, capsys, drift, state,
):
    """Stored-data accuracy (#1000 T6, D2): a store that changed during the
    in-place check has an untrustworthy result either way, so an ok and a
    corrupt outcome are both discarded with the exact drift line; the
    unchanged control returns the buffered outcome."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    path = _fresh_store(ns)
    if state == "corrupt":
        _corrupt_second_page(path)
    _no_clone(monkeypatch)

    def act(stage):
        if drift and stage == "probe-read-buffered":
            _append_byte(path)

    log = _stage_log(monkeypatch, cache_mod, act)

    trigger = cache_mod._probe_conversation_rebuild(path, lock_timeout=5.0)

    stderr = capsys.readouterr().err
    assert "probe-read-buffered" in log
    if drift:
        assert trigger is None
        assert _skip_line(_refusal(errno.ENOTSUP), _DRIFT_CLAUSE) in (
            stderr.splitlines()
        ), stderr
    elif state == "ok":
        assert trigger is None
        assert "integrity check skipped" not in stderr
    else:
        assert isinstance(trigger, sqlite3.DatabaseError)
        assert "quick_check" in str(trigger)
        assert "integrity check skipped" not in stderr


@pytest.mark.parametrize(
    "stage",
    ["recovery-before-publish", "recovery-source-exited", None],
    ids=["inside-before-publish", "before-the-confirmed-record", "control"],
)
def test_drift_during_recovery_declines_before_any_confirmation(
    tmp_path, monkeypatch, capsys, stage,
):
    """Stored-data safety (#1000 T6, D3): recovery must never quarantine on
    a live read the store changed under. A change inside `before_publish`
    publishes no forensics bundle; a change after the source context
    validated is caught by the validation immediately before the `confirmed`
    record, so no `recovery.json` and no quarantine exist. The control
    quarantines."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    core = ns["_cctally_core"]
    path = _fresh_store(ns)
    _corrupt_second_page(path)
    _no_clone(monkeypatch)
    mutated = []

    def act(name):
        if name == stage and not mutated:
            _append_byte(path)
            mutated.append(name)

    log = _stage_log(monkeypatch, cache_mod, act)
    origin = "test.recovery_drift"

    recovered = cache_mod._recover_corrupt_conversations(
        sqlite3.DatabaseError("database disk image is malformed"),
        origin=origin,
        providers=("claude", "codex"),
        lock_timeout=5.0,
    )

    stderr = capsys.readouterr().err
    incidents = list((path.parent / "quarantine").glob("conversations.db-*"))
    bundles = list(pathlib.Path(core.LOG_DIR).glob(
        "conversations.db-corruption-forensics-*.json"))
    if stage is None:
        assert recovered is True
        assert len(incidents) == 1
        assert log[-1] == "recovery-before-confirmed"
        return
    assert mutated == [stage]
    assert recovered is False
    assert _decline_line(origin, _DRIFT_CLAUSE) in stderr.splitlines(), stderr
    assert not incidents and path.exists()
    assert not path.with_name("conversations.db.recovery.json").exists()
    if stage == "recovery-before-publish":
        assert not bundles, "a drifted read was published as forensics"
    else:
        assert "recovery-before-confirmed" in log, (
            "the validation before the confirmed record never ran"
        )


def test_a_holder_seen_by_the_post_read_census_fails_the_rebuild(
    tmp_path, monkeypatch, capsys,
):
    """Stored-data safety (#1000 T6): a process still holding the family when
    the live read ends makes the result untrustworthy. It must fail the
    rebuild with the existing census text in the existing failure line —
    never be taken as a corruption trigger, a skip or a traceback."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache_mod = ns["_cctally_cache"]
    db_mod = ns["_cctally_db"]
    path = _fresh_store(ns)
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    _no_clone(monkeypatch)
    read_done = []
    log = _stage_log(
        monkeypatch, cache_mod,
        lambda stage: read_done.append(stage)
        if stage == "probe-read-buffered" else None,
    )
    real_pids = db_mod._db_family_open_pids

    def pids(target):
        if read_done and pathlib.Path(target) == path:
            return {4242}
        return real_pids(target)

    monkeypatch.setattr(db_mod, "_db_family_open_pids", pids)
    before = _full_family(path)
    args = argparse.Namespace(
        source="claude", rebuild=True,
        prune_orphans=False, prune_conversations=False,
    )

    assert ns["cmd_cache_sync"](args) == 1

    stderr = capsys.readouterr().err
    assert "probe-read-buffered" in log
    assert (
        "[cache-sync] transcript rebuild failed: store=conversations.db "
        "phase=recovery (conversations.db is still open in process(es) 4242; "
        "leaving the live family untouched); core accounting/quota sync is "
        "complete. Re-run `cctally cache-sync --rebuild`."
    ) in stderr.splitlines(), stderr
    assert "integrity check skipped" not in stderr
    assert "Traceback" not in stderr
    assert _full_family(path) == before
    assert not list((path.parent / "quarantine").glob("conversations.db-*"))
    assert not path.with_name("conversations.db.recovery.json").exists()


# --- #1000 T9: no keeper-yield fence, the production finalizer --------------

import _conversation_hold_harness as _hold  # noqa: E402


@pytest.mark.parametrize("state", ["clean", "interior-corrupt"])
def test_a_finalizer_cycle_without_a_fence_cannot_change_the_live_read(
    tmp_path, state,
):
    """Stored-data accuracy (#1000 T9): on exFAT the keeper-yield fence cannot
    be published (#1020), so the unkept-store finalizer, which takes no
    maintenance lock, can open the store during the in-place read. With no
    frames in a WAL its cycle cannot change the main file, so the probe's
    outcome is the control's (ok, or the corruption trigger) or the exact
    drift discard, and never any other trusted result."""
    harness = _hold.Harness(tmp_path, _hold.child_env(tmp_path))
    try:
        store = pathlib.Path(harness.run("create", "create")["path"])
        if state == "interior-corrupt":
            _hold.corrupt_second_page(store)
        before = _hold.main_identity(store)
        hold = harness.workdir / "probe-hold"
        probe = harness.start(
            "probe", "probe",
            env=dict(harness.env,
                     CCTALLY_TEST_CONVERSATION_PROBE_CLONE_ERRNO="ENOTSUP",
                     CCTALLY_TEST_CONVERSATION_PROBE_HOLD=str(hold)),
        )
        harness.wait(
            lambda: pathlib.Path(f"{hold}.reached").exists()
            or probe.poll() is not None,
            _hold.REACH_SECONDS, "the held probe read")
        assert pathlib.Path(f"{hold}.reached").exists(), harness.log("probe")
        harness.start("finalizer", "finalize")
        harness.wait_ack("finalizer")
        harness.wait(lambda: harness.done("finalizer") is not None,
                     _hold.DONE_AFTER_RELEASE_SECONDS,
                     f"the finalizer cycle\n{harness.log('finalizer')}")
        assert (harness.workdir / "finalizer-cycle.attempted").exists(), (
            "non-vacuity: the finalizer returned before its PASSIVE\n"
            + harness.log("finalizer"))
        pathlib.Path(f"{hold}.release").write_text("1")
        probe.wait(timeout=_hold.HOLDER_EXIT_SECONDS)
        outcome = harness.done("probe")
        assert outcome is not None, harness.log("probe")
        assert _hold.main_identity(store) == before
    finally:
        harness.close()
    assert outcome["raised"] is None, outcome
    drift = _skip_line(_refusal(errno.ENOTSUP), _DRIFT_CLAUSE)
    discarded = drift in harness.log("probe").splitlines()
    if discarded:
        assert outcome["trigger"] is None
    elif state == "clean":
        assert outcome["trigger"] is None
        assert "integrity check skipped" not in harness.log("probe")
    else:
        assert outcome["trigger"] is not None
        assert "quick_check" in outcome["trigger"], outcome


def test_an_open_finalizer_without_a_fence_discards_the_live_read(tmp_path):
    """Stored-data safety (#1000 T9): a finalizer still holding the store
    when the in-place read ends is seen by the post-read census, so the
    result is discarded and the rebuild fails with the existing census text:
    no trigger, no skip line, no quarantine, and the main file unchanged."""
    harness = _hold.Harness(tmp_path, _hold.child_env(tmp_path))
    try:
        store = pathlib.Path(harness.run("create", "create")["path"])
        before = _hold.main_identity(store)
        hold = harness.workdir / "probe-hold"
        held_finalizer = harness.workdir / "finalizer-hold"
        probe = harness.start(
            "probe", "probe",
            env=dict(harness.env,
                     CCTALLY_TEST_CONVERSATION_PROBE_CLONE_ERRNO="ENOTSUP",
                     CCTALLY_TEST_CONVERSATION_PROBE_HOLD=str(hold)),
        )
        harness.wait(
            lambda: pathlib.Path(f"{hold}.reached").exists()
            or probe.poll() is not None,
            _hold.REACH_SECONDS, "the held probe read")
        assert pathlib.Path(f"{hold}.reached").exists(), harness.log("probe")
        finalizer = harness.start("finalizer", "finalize", str(held_finalizer))
        harness.wait_ack("finalizer")
        harness.wait(
            lambda: pathlib.Path(f"{held_finalizer}.attempted").exists(),
            _hold.ACK_SECONDS,
            f"the finalizer's PASSIVE\n{harness.log('finalizer')}")
        pathlib.Path(f"{hold}.release").write_text("1")
        probe.wait(timeout=_hold.HOLDER_EXIT_SECONDS)
        outcome = harness.done("probe")
        assert outcome is not None, harness.log("probe")
        assert _hold.main_identity(store) == before
        pathlib.Path(f"{held_finalizer}.release").write_text("1")
        harness.wait(lambda: harness.done("finalizer") is not None,
                     _hold.DONE_AFTER_RELEASE_SECONDS,
                     f"the finalizer's close\n{harness.log('finalizer')}")
        finalizer_pid = finalizer.pid
    finally:
        harness.close()
    assert outcome == {
        "raised": "ConversationProbeLiveFamilyHeld",
        "error": (
            f"conversations.db is still open in process(es) {finalizer_pid}; "
            "leaving the live family untouched"
        ),
    }, outcome
    assert "integrity check skipped" not in harness.log("probe")
    assert not list((store.parent / "quarantine").glob("conversations.db-*"))
    assert _hold.main_identity(store) == before
