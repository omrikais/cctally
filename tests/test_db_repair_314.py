"""Issue #314: guided stats.db repair and SQLite-native safe backups."""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import types

from conftest import load_script, redirect_paths


REPO = pathlib.Path(__file__).resolve().parents[1]


def _ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return types.SimpleNamespace(**ns)


def _repair_args(**overrides):
    values = {
        "db": "stats",
        "yes": True,
        "busy_timeout_ms": 100,
        "sqlite3_binary": shutil.which("sqlite3"),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _backup_args(output, **overrides):
    values = {
        "db": "stats",
        "output": str(output),
        "busy_timeout_ms": 1000,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _seed_corrupt_stats(
    path: pathlib.Path, *, corrupt_table: str = "damaged"
) -> bytes:
    """Corrupt only a re-derivable table root; usage rows stay readable."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA page_size=512")
    conn.execute("VACUUM")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute(
        "CREATE TABLE weekly_usage_snapshots("
        "id INTEGER PRIMARY KEY, captured_at_utc TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO weekly_usage_snapshots VALUES(?, ?)",
        [(1, "a"), (2, "b"), (3, "c")],
    )
    conn.execute("CREATE TABLE damaged(id INTEGER PRIMARY KEY, payload BLOB)")
    conn.executemany(
        "INSERT INTO damaged VALUES(?, zeroblob(400))",
        [(i,) for i in range(1, 401)],
    )
    conn.execute("PRAGMA user_version=13")
    root = conn.execute(
        "SELECT rootpage FROM sqlite_schema WHERE name=?", (corrupt_table,)
    ).fetchone()[0]
    conn.commit()
    conn.close()

    with path.open("r+b") as fh:
        fh.seek((root - 1) * 512)
        fh.write(b"\0" * 512)
    return path.read_bytes()


def _integrity(path: pathlib.Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


def _header_user_version(path: pathlib.Path) -> int:
    data = path.read_bytes()[:64]
    assert data[:16] == b"SQLite format 3\0"
    return int.from_bytes(data[60:64], "big")


def _copy_family(source: pathlib.Path, destination: pathlib.Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        item = pathlib.Path(str(source) + suffix)
        if item.exists():
            shutil.copyfile(item, pathlib.Path(str(destination) + suffix))


def _seed_corrupt_wal_family(path: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """Copy an idle-handle WAL family whose main header is stale at v13."""
    staging = tmp_path / "wal-staging.db"
    _seed_corrupt_stats(staging)
    keeper = sqlite3.connect(staging)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute("PRAGMA user_version=17")
    keeper.commit()
    assert _header_user_version(staging) == 13
    assert keeper.execute("PRAGMA user_version").fetchone()[0] == 17
    _copy_family(staging, path)
    keeper.close()


def test_repair_stats_recovers_and_preserves_irreplaceable_invariants(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    corrupt_bytes = _seed_corrupt_stats(source)
    assert _integrity(source) != "ok"

    rc = c.cmd_db_repair(_repair_args())

    assert rc == 0, capsys.readouterr().err
    assert _integrity(source) == "ok"
    conn = sqlite3.connect(source)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0] == 3
    finally:
        conn.close()
    assert stat.S_IMODE(source.stat().st_mode) == 0o600

    backups = list(source.parent.glob("stats.db.bak-corrupt-malformed-*") )
    assert len(backups) == 1
    assert backups[0].read_bytes() == corrupt_bytes
    assert _integrity(backups[0]) != "ok"
    out = capsys.readouterr().out
    assert "weekly_usage_snapshots: 3 -> 3" in out
    assert str(backups[0]) in out


def test_repair_cli_runs_the_verified_path_end_to_end(tmp_path):
    source = tmp_path / "stats.db"
    _seed_corrupt_stats(source)
    env = dict(
        os.environ,
        CCTALLY_DATA_DIR=str(tmp_path),
        CCTALLY_DISABLE_DEV_AUTODETECT="1",
        CCTALLY_DISABLE_UPDATE_CHECK="1",
        TZ="Etc/UTC",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "bin" / "cctally"),
            "db",
            "repair",
            "--db",
            "stats",
            "--yes",
        ],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "integrity_check ok" in result.stdout
    assert _integrity(source) == "ok"
    conn = sqlite3.connect(source)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0] == 3
    finally:
        conn.close()


def test_repair_preserves_effective_user_version_from_wal(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    _seed_corrupt_wal_family(source, tmp_path)
    assert _header_user_version(source) == 13
    probe = sqlite3.connect(source)
    assert probe.execute("PRAGMA user_version").fetchone()[0] == 17
    probe.close()

    rc = c.cmd_db_repair(_repair_args())

    assert rc == 0, capsys.readouterr().err
    repaired = sqlite3.connect(source)
    try:
        assert repaired.execute("PRAGMA user_version").fetchone()[0] == 17
    finally:
        repaired.close()


def test_repair_refuses_healthy_database_without_creating_backup(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE weekly_usage_snapshots(id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    rc = c.cmd_db_repair(_repair_args())

    assert rc == 2
    assert "quick_check is ok" in capsys.readouterr().err
    assert not list(source.parent.glob("stats.db.bak-corrupt-malformed-*"))


def test_repair_refuses_when_source_usage_count_cannot_be_proved(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    before = _seed_corrupt_stats(
        source, corrupt_table="weekly_usage_snapshots"
    )

    rc = c.cmd_db_repair(_repair_args())

    assert rc == 3
    assert "cannot prove preservation" in capsys.readouterr().err
    assert source.read_bytes() == before
    assert len(list(source.parent.glob("stats.db.bak-corrupt-malformed-*"))) == 1


def test_repair_stats_requires_yes_and_leaves_bytes_untouched(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    before = _seed_corrupt_stats(source)

    rc = c.cmd_db_repair(_repair_args(yes=False))

    assert rc == 2
    assert "--yes" in capsys.readouterr().err
    assert source.read_bytes() == before
    assert not list(tmp_path.glob("stats.db.bak-corrupt-malformed-*"))


def test_repair_rejects_sqlite_without_recover_capability_before_backup(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    before = _seed_corrupt_stats(source)
    incompatible = tmp_path / "sqlite3-no-dbpage"
    incompatible.write_text(
        "#!/bin/sh\n"
        "echo 'sql error: no such table: sqlite_dbpage (1)' >&2\n"
        "exit 1\n"
    )
    incompatible.chmod(0o755)

    rc = c.cmd_db_repair(
        _repair_args(sqlite3_binary=str(incompatible))
    )

    assert rc == 3
    err = capsys.readouterr().err
    assert "does not support SQLite .recover" in err
    assert "SQLITE_ENABLE_DBPAGE_VTAB" in err
    assert source.read_bytes() == before
    assert not list(source.parent.glob("stats.db.bak-corrupt-malformed-*"))


def test_repair_stats_honors_dev_to_prod_guard(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    before = _seed_corrupt_stats(source)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(c._cctally_core, "_repo_root", lambda: repo)
    monkeypatch.setattr(
        c._cctally_core, "_real_prod_data_dir", lambda: source.parent
    )
    monkeypatch.delenv("CCTALLY_ALLOW_PROD_MIGRATION", raising=False)

    rc = c.cmd_db_repair(_repair_args())

    assert rc == 2
    err = capsys.readouterr().err
    assert "refusing to repair stats.db" in err
    assert "CCTALLY_ALLOW_PROD_MIGRATION" in err
    assert source.read_bytes() == before
    assert not list(tmp_path.glob("stats.db.bak-corrupt-malformed-*"))


def test_repair_stats_refuses_while_another_writer_holds_database(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    _seed_corrupt_stats(source)
    writer = sqlite3.connect(source)
    writer.execute("BEGIN IMMEDIATE")
    try:
        rc = c.cmd_db_repair(_repair_args(busy_timeout_ms=10))
    finally:
        writer.rollback()
        writer.close()

    assert rc == 3
    assert "still open" in capsys.readouterr().err
    assert not list(tmp_path.glob("stats.db.bak-corrupt-malformed-*"))


def test_repair_refuses_even_an_idle_preexisting_handle(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    _seed_corrupt_stats(source)
    idle = sqlite3.connect(source)
    try:
        rc = c.cmd_db_repair(_repair_args())
    finally:
        idle.close()

    assert rc == 3
    assert "still open" in capsys.readouterr().err
    assert not list(source.parent.glob("stats.db.bak-corrupt-malformed-*"))


def test_repair_marker_blocks_new_cctally_stats_open(monkeypatch, tmp_path):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    source.parent.mkdir(parents=True, exist_ok=True)
    source.with_name("stats.db.repairing").write_text(f"{os.getpid()}\n")

    try:
        c.open_db()
    except c.StatsDbMaintenanceError as exc:
        assert "repair is in progress" in str(exc)
    else:
        raise AssertionError("open_db ignored the active repair marker")
    assert not source.exists()


def test_repair_guard_blocks_post_snapshot_raw_writer(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    import _cctally_db

    source = c._cctally_core.DB_PATH
    _seed_corrupt_stats(source)
    observed = {}

    def attempt_writer(_binary, _snapshot, _destination, _scratch):
        contender = sqlite3.connect(source, timeout=0.01)
        try:
            contender.execute(
                "INSERT INTO weekly_usage_snapshots VALUES(4, 'late')"
            )
            contender.commit()
            observed["committed"] = True
        except sqlite3.OperationalError as exc:
            observed["error"] = str(exc)
        finally:
            contender.close()
        return False, "intentional stop after concurrency probe"

    monkeypatch.setattr(_cctally_db, "_run_sqlite_recover", attempt_writer)

    rc = c.cmd_db_repair(_repair_args())

    assert rc == 3
    assert observed.get("committed") is not True
    assert "locked" in observed.get("error", "")
    assert "intentional stop" in capsys.readouterr().err
    check = sqlite3.connect(source)
    try:
        assert check.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0] == 3
    finally:
        check.close()


def test_repair_swap_failure_keeps_checkpointed_live_db_coherent(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    _seed_corrupt_wal_family(source, tmp_path)

    def fail_replace(_source, _destination):
        raise PermissionError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    rc = c.cmd_db_repair(_repair_args())

    assert rc == 3
    assert "injected replace failure" in capsys.readouterr().err
    live = sqlite3.connect(source)
    try:
        assert live.execute("PRAGMA user_version").fetchone()[0] == 17
        assert live.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0] == 3
    finally:
        live.close()
    wal = pathlib.Path(str(source) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    backup_mains = [
        item
        for item in source.parent.glob("stats.db.bak-corrupt-malformed-*")
        if not item.name.endswith(("-wal", "-shm"))
    ]
    assert len(backup_mains) == 1
    assert pathlib.Path(str(backup_mains[0]) + "-wal").exists()


def test_repair_setup_failure_is_staged_and_releases_marker(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    import _cctally_db

    source = c._cctally_core.DB_PATH
    _seed_corrupt_stats(source)

    def fail_tempdir(*_args, **_kwargs):
        raise PermissionError("injected tempdir failure")

    monkeypatch.setattr(_cctally_db.tempfile, "TemporaryDirectory", fail_tempdir)

    rc = c.cmd_db_repair(_repair_args())

    assert rc == 3
    assert "injected tempdir failure" in capsys.readouterr().err
    assert not source.with_name("stats.db.repairing").exists()


def test_backup_uses_online_snapshot_and_includes_committed_wal_rows(
    tmp_path,
):
    source = tmp_path / "stats.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t(x INTEGER)")
    writer.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(200)])
    writer.commit()
    assert os.path.exists(str(source) + "-wal")
    output = tmp_path / "safe-backup.sqlite"
    env = dict(
        os.environ,
        CCTALLY_DATA_DIR=str(tmp_path),
        CCTALLY_DISABLE_DEV_AUTODETECT="1",
        CCTALLY_DISABLE_UPDATE_CHECK="1",
        TZ="Etc/UTC",
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "bin" / "cctally"),
                "db",
                "backup",
                "--db",
                "stats",
                "--output",
                str(output),
            ],
            text=True,
            capture_output=True,
            env=env,
        )
    finally:
        writer.close()

    assert result.returncode == 0, result.stderr
    assert "integrity_check ok" in result.stdout
    assert _integrity(output) == "ok"
    snap = sqlite3.connect(output)
    try:
        assert snap.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 200
    finally:
        snap.close()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not pathlib.Path(str(output) + "-wal").exists()
    assert not pathlib.Path(str(output) + "-shm").exists()


def test_backup_refuses_to_overwrite_existing_destination(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()
    output = tmp_path / "existing.sqlite"
    output.write_bytes(b"owner data")

    rc = c.cmd_db_backup(_backup_args(output))

    assert rc == 2
    assert "already exists" in capsys.readouterr().err
    assert output.read_bytes() == b"owner data"


def test_backup_publish_race_never_overwrites_new_owner_file(
    monkeypatch, tmp_path, capsys
):
    c = _ns(monkeypatch, tmp_path)
    source = c._cctally_core.DB_PATH
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()
    output = tmp_path / "raced.sqlite"
    real_link = os.link

    def race_link(source_path, destination_path):
        pathlib.Path(destination_path).write_bytes(b"racing owner")
        return real_link(source_path, destination_path)

    monkeypatch.setattr(os, "link", race_link)

    rc = c.cmd_db_backup(_backup_args(output))

    assert rc == 2
    assert "already exists" in capsys.readouterr().err
    assert output.read_bytes() == b"racing owner"
