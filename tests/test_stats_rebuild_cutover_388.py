"""Issue #388 Task A: real-process interrupted stats rebuild acceptance."""

from __future__ import annotations

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
CCTALLY = ROOT / "bin" / "cctally"


def _isolated_env(tmp_path: pathlib.Path) -> dict[str, str]:
    data = tmp_path / "data"
    home = tmp_path / "home"
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    for path in (data, home, claude / "projects", codex):
        path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "CCTALLY_DATA_DIR": str(data),
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_DISABLE_TELEMETRY": "1",
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(claude),
            "CODEX_HOME": str(codex),
            "TZ": "Etc/UTC",
        }
    )
    return env


def _cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CCTALLY), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _seed(env: dict[str, str]) -> pathlib.Path:
    now = int(time.time())
    result = _cli(
        env,
        "record-usage",
        "--percent",
        "7",
        "--resets-at",
        str(now + 3 * 86400),
        "--five-hour-percent",
        "11",
        "--five-hour-resets-at",
        str(now + 3600),
    )
    assert result.returncode == 0, result.stderr
    db = pathlib.Path(env["CCTALLY_DATA_DIR"]) / "stats.db"
    assert db.exists()
    return db


def _await_marker(
    marker: pathlib.Path, process: subprocess.Popen, budget_s: float = 30.0
) -> None:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text().strip():
            return
        assert process.poll() is None, (
            f"rebuild exited at rc={process.returncode} before {marker.name}"
        )
        time.sleep(0.01)
    raise AssertionError(f"rebuild never reached pause marker {marker}")


def _read_destination_in_separate_process(db: pathlib.Path) -> dict:
    script = """
import json, sqlite3, sys
db = sys.argv[1]
try:
    conn = sqlite3.connect(db, timeout=5)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    rows = conn.execute(
        "SELECT weekly_percent, journal_id "
        "FROM weekly_usage_snapshots ORDER BY id"
    ).fetchall()
    cursor = conn.execute(
        "SELECT segment, offset FROM journal_cursor WHERE id = 1"
    ).fetchone()
    conn.close()
    print(json.dumps({"ok": True, "integrity": integrity,
                      "rows": rows, "cursor": cursor}))
except Exception as exc:
    print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(db)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def _strand_committed_wal_family(
    db: pathlib.Path, tmp_path: pathlib.Path
) -> tuple[bytes, bytes]:
    """Leave a committed, non-empty WAL+SHM family as if its writer crashed."""
    marker = tmp_path / "wal-writer.pid"
    script = """
import os, pathlib, signal, sqlite3, sys
db, marker = sys.argv[1:]
conn = sqlite3.connect(db, timeout=5)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("CREATE TABLE cutover_wal_probe (value TEXT NOT NULL)")
conn.execute("INSERT INTO cutover_wal_probe VALUES ('committed')")
conn.commit()
pathlib.Path(marker).write_text(f"{os.getpid()}\\n")
os.kill(os.getpid(), signal.SIGSTOP)
"""
    writer = subprocess.Popen(
        [sys.executable, "-c", script, str(db), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _await_marker(marker, writer)
        os.kill(writer.pid, signal.SIGKILL)
        writer.communicate(timeout=30)
    finally:
        if writer.poll() is None:
            os.kill(writer.pid, signal.SIGKILL)
            writer.wait(timeout=30)
    assert writer.returncode == -signal.SIGKILL
    wal = pathlib.Path(f"{db}-wal")
    shm = pathlib.Path(f"{db}-shm")
    wal_bytes = wal.read_bytes()
    shm_bytes = shm.read_bytes()
    assert wal_bytes and shm_bytes
    return wal_bytes, shm_bytes


@pytest.mark.parametrize(
    "pause_point",
    [
        "rebuild_fold_started",
        "rebuild_scratch_complete",
        "rebuild_before_cutover",
    ],
)
def test_kill_during_real_operator_rebuild_preserves_old_then_retries(
    tmp_path: pathlib.Path, pause_point: str
) -> None:
    env = _isolated_env(tmp_path)
    db = _seed(env)
    before = _read_destination_in_separate_process(db)
    assert before["ok"] is True and before["integrity"] == "ok"
    assert before["rows"] == [[7.0, before["rows"][0][1]]]
    wal_bytes, shm_bytes = _strand_committed_wal_family(db, tmp_path)

    marker = tmp_path / f"{pause_point}.pid"
    child_env = dict(env)
    child_env.update(
        {
            "CCTALLY_TEST_STATS_REBUILD_PAUSE_AT": pause_point,
            "CCTALLY_TEST_STATS_REBUILD_MARKER": str(marker),
        }
    )
    rebuild = subprocess.Popen(
        [sys.executable, str(CCTALLY), "db", "rebuild", "--db", "stats"],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _await_marker(marker, rebuild)
        os.kill(rebuild.pid, signal.SIGKILL)
        rebuild.communicate(timeout=30)
    finally:
        if rebuild.poll() is None:
            os.kill(rebuild.pid, signal.SIGKILL)
            rebuild.wait(timeout=30)
    assert rebuild.returncode == -signal.SIGKILL

    scratches = sorted(db.parent.glob("stats.db.rebuilding-*"))
    assert scratches, f"{pause_point} did not leave the expected scratch artifact"
    incidents = sorted((db.parent / "quarantine").glob("stats.db-*"))
    if pause_point == "rebuild_before_cutover":
        assert incidents
        incident = incidents[-1]
        manifest = json.loads((incident / "manifest.json").read_text())
        assert manifest["cutoverProtocol"] == "preserve-then-atomic-replace-v1"
        assert manifest["complete"] is True
        assert manifest["movedFiles"] == [
            "stats.db",
            "stats.db-wal",
            "stats.db-shm",
        ]
        assert (incident / "stats.db-wal").read_bytes() == wal_bytes
        # SQLite may update SHM reader marks merely by opening the old family;
        # preservation must retain a non-empty SHM, while the committed WAL is
        # the byte-exact durable-content assertion.
        assert len((incident / "stats.db-shm").read_bytes()) == len(shm_bytes)
        assert not pathlib.Path(f"{db}-wal").exists()
        assert not pathlib.Path(f"{db}-shm").exists()
    else:
        assert not incidents, (
            f"{pause_point} preserved/quarantined the old family before the "
            "scratch was eligible for cutover"
        )
        assert pathlib.Path(f"{db}-wal").read_bytes() == wal_bytes
        assert len(pathlib.Path(f"{db}-shm").read_bytes()) == len(shm_bytes)

    after_kill = _read_destination_in_separate_process(db)
    assert after_kill == before, (
        f"{pause_point} exposed an absent, empty, partial, corrupt, or locked "
        f"destination: before={before!r} after={after_kill!r}"
    )

    alerts = pathlib.Path(env["CCTALLY_DATA_DIR"]) / "alerts.log"
    alert_bytes = alerts.read_bytes() if alerts.exists() else b""
    journal = pathlib.Path(env["CCTALLY_DATA_DIR"]) / "journal"
    segments = sorted(journal.glob("*.jsonl"))
    assert segments
    pinned_high_water = [segments[-1].name, segments[-1].stat().st_size]
    retry = _cli(env, "db", "rebuild", "--db", "stats", "--json")
    assert retry.returncode == 0, retry.stderr
    payload = json.loads(retry.stdout)
    assert payload["rowsByTable"]["weekly_usage_snapshots"] == 1
    after_retry = _read_destination_in_separate_process(db)
    assert after_retry["ok"] is True
    assert after_retry["integrity"] == "ok"
    assert after_retry["rows"] == before["rows"]
    assert after_retry["cursor"] == pinned_high_water
    assert (alerts.read_bytes() if alerts.exists() else b"") == alert_bytes
