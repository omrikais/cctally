"""Crash-safe cache.db corruption recovery.

Regression for the 2026-07-23 dashboard SIGBUS: the legacy opener unlinked
cache.db in place while another process still held a WAL reader.  The live
reader then faulted in sqlite3.walFindFrame after the mapped file was shortened.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import sqlite3
import struct
import subprocess
import sys
import time

import pytest

from conftest import load_script, redirect_paths


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return (
        ns,
        sys.modules["_cctally_core"],
        sys.modules["_cctally_store"],
    )


def test_corrupt_open_preserves_family_while_live_reader_exists(
    tmp_path, monkeypatch,
):
    ns, core, store = _load(tmp_path, monkeypatch)
    live = ns["open_cache_db"]()
    path = pathlib.Path(core.CACHE_DB_PATH)
    inode = path.stat().st_ino

    def corrupt_open(_store):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(store, "open_index", corrupt_open)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="still open"):
            ns["open_cache_db"]()

        assert path.exists()
        assert path.stat().st_ino == inode
        assert live.execute("PRAGMA schema_version").fetchone() is not None
        assert not path.with_name("cache.db.repairing").exists()
    finally:
        live.close()


def test_idle_corrupt_cache_quarantines_whole_family_then_recreates(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database")
    pathlib.Path(str(path) + "-wal").write_bytes(b"forensic wal")
    pathlib.Path(str(path) + "-shm").write_bytes(b"forensic shm")

    # Inject the already-observed corruption result before SQLite gets a chance
    # to discard deliberately synthetic sidecars as invalid. The retry uses the
    # real guarded opener against the freshly quarantined path.
    real_open = cache_mod._cache_open_guarded
    attempts = 0

    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_open()

    monkeypatch.setattr(cache_mod, "_cache_open_guarded", fail_once)
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    incidents = sorted((path.parent / "quarantine").glob("cache.db-*"))
    assert len(incidents) == 1
    assert {
        item.name for item in incidents[0].iterdir()
    } >= {"cache.db", "cache.db-wal", "cache.db-shm", "manifest.json"}
    assert not path.with_name("cache.db.repairing").exists()


def test_partial_family_quarantine_fails_closed_then_resumes(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    db_mod = sys.modules["_cctally_db"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database")
    pathlib.Path(f"{path}-wal").write_bytes(b"forensic wal")
    pathlib.Path(f"{path}-shm").write_bytes(b"forensic shm")
    real_replace = db_mod.os.replace
    failed = False

    def fail_shm_once(src, dst):
        nonlocal failed
        if not failed and str(src).endswith("cache.db-shm"):
            failed = True
            raise OSError("injected sidecar move failure")
        return real_replace(src, dst)

    monkeypatch.setattr(db_mod.os, "replace", fail_shm_once)
    with pytest.raises(
        sqlite3.DatabaseError, match="could not complete whole-family quarantine"
    ):
        cache_mod._recover_corrupt_cache(
            sqlite3.DatabaseError("database disk image is malformed")
        )

    pending = db_mod._quarantine_pending_path(path)
    assert pending.exists()
    assert path.exists(), "main DB must not be recreated after a partial move"

    monkeypatch.setattr(db_mod.os, "replace", real_replace)
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert not pending.exists()
    incidents = list((path.parent / "quarantine").glob("cache.db-*"))
    assert len(incidents) == 1
    assert {
        item.name for item in incidents[0].iterdir()
    } >= {"cache.db", "cache.db-wal", "cache.db-shm", "manifest.json"}


def test_cache_repair_marker_blocks_new_open(tmp_path, monkeypatch):
    ns, core, _store = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    path = pathlib.Path(core.CACHE_DB_PATH)
    inode = path.stat().st_ino
    marker = path.with_name("cache.db.repairing")
    marker.write_text(f"{os.getpid()}\n")

    with pytest.raises(sqlite3.DatabaseError, match="maintenance"):
        ns["open_cache_db"]()

    assert path.stat().st_ino == inode


def test_dead_cache_repair_marker_is_reclaimed_before_open(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    path = pathlib.Path(core.CACHE_DB_PATH)
    inode = path.stat().st_ino
    marker = path.with_name("cache.db.repairing")
    marker.write_text("999999999\n")

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    assert path.stat().st_ino == inode
    assert not marker.exists()


def test_malformed_cache_repair_marker_is_reclaimed_before_open(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    ns["open_cache_db"]().close()
    path = pathlib.Path(core.CACHE_DB_PATH)
    marker = path.with_name("cache.db.repairing")
    marker.write_text("{truncated-owner-record")

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    assert not marker.exists()


def test_cache_repair_marker_rejects_reused_pid_identity(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    ns["open_cache_db"]().close()
    path = pathlib.Path(core.CACHE_DB_PATH)
    marker = path.with_name("cache.db.repairing")
    current_identity = db_mod._process_start_identity(os.getpid())
    assert current_identity
    marker.write_text(
        db_mod._encode_repair_owner(
            pid=os.getpid(),
            process_start=current_identity + "-reused",
            claim_id="old-claim",
        )
    )

    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    assert not marker.exists()


def _write_recovery_sources(
    claude_dir: pathlib.Path, codex_home: pathlib.Path,
) -> None:
    claude_project = claude_dir / "projects" / "-tmp-recovery"
    claude_project.mkdir(parents=True)
    (claude_project / "recovery-session.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-07-24T08:00:00Z",
                "requestId": "req-recovery",
                "message": {
                    "id": "msg-recovery",
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                },
            }
        )
        + "\n"
    )
    codex_sessions = codex_home / "sessions" / "2026" / "07" / "24"
    codex_sessions.mkdir(parents=True)
    (codex_sessions / "rollout-recovery.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-07-24T08:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "recovery-codex-session"},
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


@pytest.mark.parametrize(
    ("source", "claude_rows", "codex_rows"),
    [
        ("claude", 1, 0),
        ("codex", 0, 1),
        ("all", 1, 1),
    ],
)
def test_corrupt_cache_rebuild_preserves_requested_source_scope(
    tmp_path, source, claude_rows, codex_rows,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    claude_dir = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    _write_recovery_sources(claude_dir, codex_home)
    cache_path = data_dir / "cache.db"
    cache_path.write_bytes(b"not a sqlite database")
    pathlib.Path(f"{cache_path}-wal").write_bytes(b"forensic wal")
    pathlib.Path(f"{cache_path}-shm").write_bytes(b"forensic shm")
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

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin" / "cctally"),
            "cache-sync",
            "--source",
            source,
            "--rebuild",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(cache_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM session_entries"
        ).fetchone()[0] == claude_rows
        assert conn.execute(
            "SELECT count(*) FROM codex_session_entries"
        ).fetchone()[0] == codex_rows
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert len(list((data_dir / "quarantine").glob("cache.db-*"))) == 1


def test_corrupt_cache_with_dead_owner_marker_rebuilds_without_surgery(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    claude_dir = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    _write_recovery_sources(claude_dir, codex_home)
    cache_path = data_dir / "cache.db"
    cache_path.write_bytes(b"not a sqlite database")
    cache_path.with_name("cache.db.repairing").write_text("999999999\n")
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

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin" / "cctally"),
            "cache-sync",
            "--source",
            "all",
            "--rebuild",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not cache_path.with_name("cache.db.repairing").exists()
    conn = sqlite3.connect(cache_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM session_entries"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM codex_session_entries"
        ).fetchone()[0] == 1
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("pause_at", "source", "claude_rows", "codex_rows"),
    [
        ("cache_repair_claimed", "all", 1, 1),
        ("cache_repair_forensics", "all", 1, 1),
        ("cache_repair_quarantined", "all", 1, 1),
        ("cache_repair_recreated", "all", 1, 1),
        # The final three cases kill the recovery owner inside the real
        # provider transaction after recreation.  They prove provider-scoped
        # and all-provider restart semantics rather than only pre-ingest phase
        # boundaries.
        ("claude_precommit", "claude", 1, 0),
        ("codex_precommit", "codex", 0, 1),
        ("codex_precommit", "all", 1, 1),
    ],
)
def test_killed_cache_repair_converges_on_next_rebuild(
    tmp_path, pause_at, source, claude_rows, codex_rows,
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    claude_dir = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    _write_recovery_sources(claude_dir, codex_home)
    cache_path = data_dir / "cache.db"
    cache_path.write_bytes(b"not a sqlite database")
    pathlib.Path(f"{cache_path}-wal").write_bytes(b"forensic wal")
    pathlib.Path(f"{cache_path}-shm").write_bytes(b"forensic shm")
    pause_marker = tmp_path / f"{pause_at}.marker"

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
        "CCTALLY_TEST_CACHE_STORM_PAUSE_AT": pause_at,
        "CCTALLY_TEST_CACHE_STORM_MARKER": str(pause_marker),
    }
    victim = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "bin" / "cctally"),
            "cache-sync",
            "--source",
            source,
        ],
        env=victim_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not pause_marker.exists():
            if victim.poll() is not None:
                break
            time.sleep(0.02)
        if not pause_marker.exists():
            stdout, stderr = victim.communicate(timeout=5)
            pytest.fail(
                f"victim never reached {pause_at}\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        os.kill(victim.pid, signal.SIGKILL)
        victim.communicate(timeout=5)
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.communicate(timeout=5)

    survivor = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin" / "cctally"),
            "cache-sync",
            "--source",
            source,
            "--rebuild",
        ],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert survivor.returncode == 0, survivor.stderr

    conn = sqlite3.connect(cache_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM session_entries"
        ).fetchone()[0] == claude_rows
        assert conn.execute(
            "SELECT count(*) FROM codex_session_entries"
        ).fetchone()[0] == codex_rows
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert not cache_path.with_name("cache.db.repairing").exists()
    assert len(list((data_dir / "quarantine").glob("cache.db-*"))) == 1


def test_guarded_open_detects_schema_readable_session_tree_corruption(
    tmp_path, monkeypatch,
):
    _ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA page_size=512")
        conn.execute(
            "CREATE TABLE session_entries "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO session_entries(payload) VALUES (?)",
            [("x" * 200,) for _ in range(300)],
        )
        conn.commit()
        root_page = conn.execute(
            "SELECT rootpage FROM sqlite_schema "
            "WHERE type='table' AND name='session_entries'"
        ).fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    finally:
        conn.close()

    # Interior table B-tree pages store their right-most child at header +8.
    # Point it past EOF: schema_version and the left edge remain readable, while
    # the exact descending-rowid probe needed for the production failure raises
    # SQLITE_CORRUPT ("invalid page number").
    with path.open("r+b") as fh:
        header = (root_page - 1) * page_size
        fh.seek(header)
        assert fh.read(1) == b"\x05", "fixture root must be an interior table page"
        fh.seek(header + 8)
        fh.write(struct.pack(">I", page_count + 100))

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA schema_version").fetchone() is not None
        assert raw.execute(
            "SELECT rowid FROM session_entries ORDER BY rowid LIMIT 1"
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.DatabaseError, match="malformed"):
            raw.execute(
                "SELECT rowid FROM session_entries ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
    finally:
        raw.close()

    with pytest.raises(sqlite3.DatabaseError, match="malformed"):
        cache_mod._cache_open_guarded()
