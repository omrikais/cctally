"""Issue #388 Task B: next-open interrupted-rebuild recovery."""

from __future__ import annotations

import json
import os
import pathlib
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone


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


def _cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CCTALLY), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _cli_with_prod_guard(
    env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    script = """
import importlib.util, pathlib, sys
from importlib.machinery import SourceFileLoader
root, *argv = sys.argv[1:]
bin_dir = pathlib.Path(root) / "bin"
sys.path.insert(0, str(bin_dir))
loader = SourceFileLoader("cctally", str(bin_dir / "cctally"))
spec = importlib.util.spec_from_loader("cctally", loader)
module = importlib.util.module_from_spec(spec)
sys.modules["cctally"] = module
loader.exec_module(module)
import _cctally_db
_cctally_db._would_block_prod_stats = lambda _path: True
raise SystemExit(module.main(argv))
"""
    return subprocess.run(
        [sys.executable, "-c", script, str(ROOT), *args],
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
    marker: pathlib.Path, process: subprocess.Popen[str], budget_s: float = 30.0
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


def _create_legacy_interrupted_state(
    env: dict[str, str], db: pathlib.Path, tmp_path: pathlib.Path
) -> int:
    """Model the pre-Task-A quarantine-before-build orchestration, then kill."""
    quarantine = db.parent / "quarantine"
    quarantine.mkdir()
    incident_at = datetime.now(timezone.utc)
    legacy_incident = quarantine / f"stats.db-{incident_at:%Y%m%dT%H%M%SZ}"
    legacy_incident.mkdir()
    moved: list[str] = []
    for suffix in ("", "-wal", "-shm"):
        member = pathlib.Path(f"{db}{suffix}")
        if member.exists():
            member.replace(legacy_incident / member.name)
            moved.append(member.name)
    (legacy_incident / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "quarantinedAtUtc": incident_at.isoformat().replace("+00:00", "Z"),
                "originalPath": str(db),
                "movedFiles": moved,
                "complete": True,
            }
        )
    )
    assert not db.exists()

    marker = tmp_path / "legacy-rebuild.pid"
    child_env = dict(env)
    child_env.update(
        {
            "CCTALLY_TEST_STATS_REBUILD_PAUSE_AT": "rebuild_fold_started",
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
        killed_pid = rebuild.pid
        os.kill(killed_pid, signal.SIGKILL)
    finally:
        if rebuild.poll() is None:
            os.kill(rebuild.pid, signal.SIGKILL)
        rebuild.communicate(timeout=30)
    assert rebuild.returncode == -signal.SIGKILL
    assert not db.exists()
    assert list(db.parent.glob("stats.db.rebuilding-*"))
    return killed_pid


def _read_usage_rows(db: pathlib.Path) -> list[tuple[float, str]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [
            (float(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT weekly_percent, journal_id "
                "FROM weekly_usage_snapshots ORDER BY id"
            )
        ]
    finally:
        conn.close()


def _build_independent_rebuild(
    env: dict[str, str], destination: pathlib.Path
) -> None:
    script = """
import importlib.util, pathlib, sys
from importlib.machinery import SourceFileLoader
root, target = sys.argv[1:]
bin_dir = pathlib.Path(root) / "bin"
sys.path.insert(0, str(bin_dir))
loader = SourceFileLoader("cctally", str(bin_dir / "cctally"))
spec = importlib.util.spec_from_loader("cctally", loader)
module = importlib.util.module_from_spec(spec)
sys.modules["cctally"] = module
loader.exec_module(module)
import _cctally_journal
import _cctally_store
with _cctally_store.stats_write_scope("test-independent-rebuild"):
    _cctally_journal.rebuild_stats_index(
        context=_cctally_journal.RebuildContext(trigger="test-fixture"),
        target_path=target,
    )
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT), str(destination)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def _install_empty_current_epoch_destination(
    env: dict[str, str], db: pathlib.Path
) -> None:
    """Model the valid-looking empty index a pre-fix next open could create."""
    empty = db.with_name("empty-current-epoch.db")
    script = """
import importlib.util, os, pathlib, sys
from importlib.machinery import SourceFileLoader
root, target = sys.argv[1:]
bin_dir = pathlib.Path(root) / "bin"
sys.path.insert(0, str(bin_dir))
loader = SourceFileLoader("cctally", str(bin_dir / "cctally"))
spec = importlib.util.spec_from_loader("cctally", loader)
module = importlib.util.module_from_spec(spec)
sys.modules["cctally"] = module
loader.exec_module(module)
import _cctally_core
conn = _cctally_core.open_db(_target_path=target)
conn.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT), str(empty)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    segments = sorted(
        (pathlib.Path(env["CCTALLY_DATA_DIR"]) / "journal").glob("*.jsonl")
    )
    assert segments
    conn = sqlite3.connect(empty)
    try:
        conn.execute(
            "INSERT INTO journal_cursor (id, segment, offset) VALUES (1, ?, ?)",
            (segments[-1].name, segments[-1].stat().st_size),
        )
        conn.commit()
    finally:
        conn.close()
    empty.replace(db)


def test_next_real_cli_open_recovers_legacy_interrupted_rebuild(
    tmp_path: pathlib.Path,
) -> None:
    """Removing next-open recovery must make the CLI serve an empty index."""
    env = _isolated_env(tmp_path)
    db = _seed(env)
    before = _read_usage_rows(db)
    assert len(before) == 1 and before[0][0] == 7.0
    maintenance_lock = db.with_name("stats.db.maintenance.lock")
    assert maintenance_lock.exists()
    unrelated = db.parent / "stats.db.rebuilding-not-ours.txt"
    unrelated.write_text("preserve me")

    killed_pid = _create_legacy_interrupted_state(env, db, tmp_path)
    artifacts_before = sorted(
        path.name for path in db.parent.glob("stats.db.rebuilding-*")
    )
    assert killed_pid > 0

    opened = _cli(env, "report", "--json")
    assert opened.returncode == 0, opened.stderr
    payload = json.loads(opened.stdout)
    assert payload["current"]["weeklyPercent"] == 7.0
    assert _read_usage_rows(db) == before
    assert not list(db.parent.glob("stats.db.rebuilding-????????T??????_??????*"))
    assert unrelated.read_text() == "preserve me"
    assert maintenance_lock.exists()
    assert artifacts_before


def test_next_open_recovers_valid_looking_empty_current_epoch_destination(
    tmp_path: pathlib.Path,
) -> None:
    """Skipping journal/index consistency must leave the prior 7% invisible."""
    env = _isolated_env(tmp_path)
    db = _seed(env)
    before = _read_usage_rows(db)
    _create_legacy_interrupted_state(env, db, tmp_path)
    _install_empty_current_epoch_destination(env, db)
    assert _read_usage_rows(db) == []

    opened = _cli(env, "report", "--json")
    assert opened.returncode == 0, opened.stderr
    assert json.loads(opened.stdout)["current"]["weeklyPercent"] == 7.0
    assert _read_usage_rows(db) == before
    assert not list(db.parent.glob("stats.db.rebuilding-????????T??????_??????*"))


def test_next_open_recovers_partial_destination_with_complete_metadata(
    tmp_path: pathlib.Path,
) -> None:
    """Effective metadata and cursor cannot hide a missing derived row."""
    env = _isolated_env(tmp_path)
    db = _seed(env)
    complete = tmp_path / "complete.db"
    _build_independent_rebuild(env, complete)
    _create_legacy_interrupted_state(env, db, tmp_path)
    shutil.copy2(complete, db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM weekly_usage_snapshots")
        conn.commit()
    finally:
        conn.close()
    inode_before = db.stat().st_ino
    assert _read_usage_rows(db) == []

    opened = _cli(env, "report", "--json")
    assert opened.returncode == 0, opened.stderr
    assert json.loads(opened.stdout)["current"]["weeklyPercent"] == 7.0
    assert _read_usage_rows(db)[0][0] == 7.0
    assert db.stat().st_ino != inode_before
    assert not list(db.parent.glob("stats.db.rebuilding-????????T??????_??????*"))


def _cutover_manifests(app_dir: pathlib.Path) -> list[dict]:
    """Every `preserve-then-atomic-replace-v1` manifest, oldest first.

    Selected by protocol so the synthesized legacy incident that sets this
    scenario up is never mistaken for one the recovery cutover wrote.
    """
    root = pathlib.Path(app_dir) / "quarantine"
    if not root.is_dir():
        return []
    out = []
    for incident in sorted(root.iterdir()):
        path = incident / "manifest.json"
        if not path.is_file():
            continue
        payload = json.loads(path.read_text())
        if payload.get("cutoverProtocol") == "preserve-then-atomic-replace-v1":
            out.append(payload)
    return out


def test_recovery_rebuild_incident_records_its_trigger_identity(
    tmp_path: pathlib.Path,
) -> None:
    """#496 S1 F3, driven through a real kill and a real next open.

    The partial-destination shape is used deliberately: it is the branch that
    actually calls `rebuild_stats_index`, so it is the only interrupted-rebuild
    path that preserves a family and therefore writes a manifest at all.
    """
    env = _isolated_env(tmp_path)
    db = _seed(env)
    complete = tmp_path / "complete.db"
    _build_independent_rebuild(env, complete)
    _create_legacy_interrupted_state(env, db, tmp_path)
    shutil.copy2(complete, db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM weekly_usage_snapshots")
        conn.commit()
    finally:
        conn.close()
    data = pathlib.Path(env["CCTALLY_DATA_DIR"])
    assert _cutover_manifests(data) == []

    opened = _cli(env, "report", "--json")

    assert opened.returncode == 0, opened.stderr
    assert json.loads(opened.stdout)["current"]["weeklyPercent"] == 7.0
    manifests = _cutover_manifests(data)
    assert [m["trigger"] for m in manifests] == ["interrupted-rebuild-recovery"]
    assert manifests[0]["schemaVersion"] == 2
    assert manifests[0]["triggerError"] is None


def test_legitimate_empty_index_is_not_rebuilt_merely_because_scratch_exists(
    tmp_path: pathlib.Path,
) -> None:
    """Treating zero rows as damage must replace this consistent empty index."""
    env = _isolated_env(tmp_path)
    data = pathlib.Path(env["CCTALLY_DATA_DIR"])
    journal = data / "journal"
    journal.mkdir()
    segment = journal / "observations-2026-07.jsonl"
    segment.write_text(
        json.dumps(
            {
                "t": "obs",
                "at": "2026-07-26T12:00:00Z",
                "src": "test-noop",
                "payload": {"kind": "noop"},
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    db = data / "stats.db"
    _install_empty_current_epoch_destination(env, db)
    assert _read_usage_rows(db) == []

    quarantine = data / "quarantine"
    incident = quarantine / "stats.db-20260720T120000Z"
    incident.mkdir(parents=True)
    (incident / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "quarantinedAtUtc": "2026-07-20T12:00:00Z",
                "originalPath": str(db),
                "movedFiles": ["stats.db"],
                "complete": True,
            }
        )
    )
    scratch = data / "stats.db.rebuilding-20260726T120000_000000"
    shutil.copy2(db, scratch)
    inode_before = db.stat().st_ino
    incidents_before = sorted(path.name for path in quarantine.iterdir())

    opened = _cli(env, "report", "--json")
    assert opened.returncode == 0, opened.stderr
    assert json.loads(opened.stdout)["current"] is None
    assert db.stat().st_ino == inode_before
    assert sorted(path.name for path in quarantine.iterdir()) == incidents_before
    assert not scratch.exists()
    assert db.with_name("stats.db.maintenance.lock").exists()


def test_historical_quarantine_is_not_positive_evidence_for_new_scratch(
    tmp_path: pathlib.Path,
) -> None:
    """An unrelated old incident must not authorize recovery of a new artifact."""
    env = _isolated_env(tmp_path)
    db = _seed(env)
    _install_empty_current_epoch_destination(env, db)
    inode_before = db.stat().st_ino

    quarantine = db.parent / "quarantine"
    incident = quarantine / "stats.db-20200101T000000Z"
    incident.mkdir(parents=True)
    (incident / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "quarantinedAtUtc": "2020-01-01T00:00:00Z",
                "originalPath": str(db),
                "movedFiles": ["stats.db"],
                "complete": True,
            }
        )
    )
    scratch = db.parent / "stats.db.rebuilding-20260726T120000_000000"
    shutil.copy2(db, scratch)

    opened = _cli(env, "report", "--json")
    assert opened.returncode == 0, opened.stderr
    assert json.loads(opened.stdout)["current"] is None
    assert db.stat().st_ino == inode_before
    assert not scratch.exists()


def test_cleanup_only_reclamation_honors_dev_on_prod_guard(
    tmp_path: pathlib.Path,
) -> None:
    env = _isolated_env(tmp_path)
    db = _seed(env)
    scratch = db.parent / "stats.db.rebuilding-20260726T120000_000000"
    shutil.copy2(db, scratch)
    inode_before = db.stat().st_ino

    opened = _cli_with_prod_guard(env, "report", "--json")
    assert opened.returncode == 2
    assert "prod data dir" in opened.stderr
    assert db.stat().st_ino == inode_before
    assert scratch.exists()


def test_live_rebuild_owner_blocks_open_without_reclaiming_scratch_or_lock(
    tmp_path: pathlib.Path,
) -> None:
    env = _isolated_env(tmp_path)
    db = _seed(env)
    lock = db.with_name("stats.db.maintenance.lock")
    lock_inode = lock.stat().st_ino
    marker = tmp_path / "live-rebuild.pid"
    child_env = dict(env)
    child_env.update(
        {
            "CCTALLY_TEST_STATS_REBUILD_PAUSE_AT": "rebuild_fold_started",
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
        artifacts_before = sorted(
            path.name for path in db.parent.glob("stats.db.rebuilding-*")
        )
        opened = _cli(env, "report", "--json")
        assert opened.returncode == 3
        assert _read_usage_rows(db)[0][0] == 7.0
        assert sorted(
            path.name for path in db.parent.glob("stats.db.rebuilding-*")
        ) == artifacts_before
        assert lock.stat().st_ino == lock_inode
    finally:
        if rebuild.poll() is None:
            os.kill(rebuild.pid, signal.SIGKILL)
        rebuild.communicate(timeout=30)

    retry = _cli(env, "report", "--json")
    assert retry.returncode == 0, retry.stderr
    assert json.loads(retry.stdout)["current"]["weeklyPercent"] == 7.0
    assert not list(db.parent.glob("stats.db.rebuilding-????????T??????_??????*"))
    assert lock.stat().st_ino == lock_inode


def test_recovery_taints_structural_batch_and_restores_best_index(
    tmp_path: pathlib.Path,
) -> None:
    """A tainted correction batch must not block interrupted-index recovery."""
    env = _isolated_env(tmp_path)
    db = _seed(env)
    _create_legacy_interrupted_state(env, db, tmp_path)
    _install_empty_current_epoch_destination(env, db)
    inode_before = db.stat().st_ino
    segment = sorted(
        (pathlib.Path(env["CCTALLY_DATA_DIR"]) / "journal").glob("*.jsonl")
    )[-1]
    with segment.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "v": 1,
                    "t": "correction_batch",
                    "id": "batch:missing-begin",
                    "at": "2026-07-26T12:00:00Z",
                    "src": "rederive",
                    "family": "claude-usage",
                    "action_count": 0,
                    "actions_hash": "sha256:" + "0" * 64,
                    "phase": "commit",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
    artifacts_before = sorted(
        path.name for path in db.parent.glob("stats.db.rebuilding-*")
    )

    opened = _cli(env, "report", "--json")
    assert opened.returncode == 0, opened.stderr
    assert json.loads(opened.stdout)["current"]["weeklyPercent"] == 7.0
    assert db.stat().st_ino != inode_before
    assert _read_usage_rows(db)[0][0] == 7.0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert conn.execute(
            "SELECT batch_id, kind FROM journal_protocol_violations"
        ).fetchall() == [
            ("batch:missing-begin", "commit_without_begin")
        ]
    finally:
        conn.close()
    assert artifacts_before
    assert not list(
        db.parent.glob("stats.db.rebuilding-????????T??????_??????*")
    )
    assert db.with_name("stats.db.maintenance.lock").exists()


def test_three_racing_openers_share_one_recovery_and_all_see_restored_data(
    tmp_path: pathlib.Path,
) -> None:
    """Dropping exclusive ownership must let racers double-recover the family."""
    env = _isolated_env(tmp_path)
    db = _seed(env)
    before = _read_usage_rows(db)
    _create_legacy_interrupted_state(env, db, tmp_path)
    alerts = db.parent / "alerts.log"
    alert_bytes = alerts.read_bytes() if alerts.exists() else b""

    openers = [
        subprocess.Popen(
            [sys.executable, str(CCTALLY), "report", "--json"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    results = [process.communicate(timeout=120) for process in openers]

    assert all(process.returncode == 0 for process in openers), results
    payloads = [json.loads(stdout) for stdout, _stderr in results]
    assert [payload["current"]["weeklyPercent"] for payload in payloads] == [
        7.0,
        7.0,
        7.0,
    ]
    assert _read_usage_rows(db) == before
    incidents = sorted((db.parent / "quarantine").glob("stats.db-*"))
    assert len(incidents) == 1
    assert not list(db.parent.glob("stats.db.rebuilding-????????T??????_??????*"))
    assert (alerts.read_bytes() if alerts.exists() else b"") == alert_bytes
    assert db.with_name("stats.db.maintenance.lock").exists()


def test_real_doctor_open_recovers_interrupted_rebuild(
    tmp_path: pathlib.Path,
) -> None:
    """Bypassing the guarded stats boundary in Doctor must leave rows absent."""
    env = _isolated_env(tmp_path)
    db = _seed(env)
    before = _read_usage_rows(db)
    _create_legacy_interrupted_state(env, db, tmp_path)

    doctor = _cli(env, "doctor", "--json")
    assert doctor.returncode == 2, doctor.stderr
    payload = json.loads(doctor.stdout)
    checks = {
        check["id"]: check
        for category in payload["categories"]
        for check in category["checks"]
    }
    stats_file = checks["db.stats.file"]
    assert stats_file["severity"] == "warn"
    assert "interrupted rebuild" in stats_file["summary"]
    assert "cctally report" in stats_file["remediation"]
    assert not db.exists(), "Doctor remains read-only"
    assert list(db.parent.glob("stats.db.rebuilding-*"))

    report = _cli(env, "report", "--json")
    assert report.returncode == 0, report.stderr
    assert _read_usage_rows(db) == before
    assert not list(db.parent.glob("stats.db.rebuilding-????????T??????_??????*"))


def test_doctor_does_not_recover_existing_empty_interrupted_destination(
    tmp_path: pathlib.Path,
) -> None:
    env = _isolated_env(tmp_path)
    db = _seed(env)
    _create_legacy_interrupted_state(env, db, tmp_path)
    _install_empty_current_epoch_destination(env, db)
    inode_before = db.stat().st_ino
    artifacts_before = sorted(
        path.name for path in db.parent.glob("stats.db.rebuilding-*")
    )

    doctor = _cli(env, "doctor", "--json")
    assert doctor.returncode == 2, doctor.stderr
    payload = json.loads(doctor.stdout)
    checks = {
        check["id"]: check
        for category in payload["categories"]
        for check in category["checks"]
    }
    assert "interrupted rebuild" in checks["db.stats.file"]["summary"]
    assert db.stat().st_ino == inode_before
    assert _read_usage_rows(db) == []
    assert sorted(
        path.name for path in db.parent.glob("stats.db.rebuilding-*")
    ) == artifacts_before
