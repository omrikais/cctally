"""#997 T-C2 — the rederive's read-only snapshots clone instead of copying (D13, T13).

`db rederive` copied the whole `cache.db` (2.54 GB on the maintainer's copy)
into `TMPDIR` for every preview and apply: past the per-file and physical
write ceilings on its own. On Darwin the snapshot is now an APFS clone that
shares the source's blocks. The snapshot directory goes under `TMPDIR` when
`TMPDIR` is on the source's device, otherwise beside the source (a data
folder on an external volume with an internal `TMPDIR`); a refused clone
removes that directory and takes the real copy under `TMPDIR`, so a volume
that cannot clone never receives a copy beside the data.
"""
from __future__ import annotations

import errno
import importlib
import json
import os
import pathlib
import shutil
import sqlite3
import tempfile

import pytest

import journal_fixture_496_s5 as S5
from conftest import load_isolated_cctally_module


def _clone():
    return importlib.import_module("_lib_clone_copy")


def _rederive():
    return importlib.import_module("_cctally_rederive")


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A source database in `data/` and a separate `TMPDIR`."""
    data = tmp_path / "data"
    tmp = tmp_path / "tmpdir"
    data.mkdir()
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    source = data / "cache.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)",
                     [(f"row-{i}",) for i in range(200)])
    conn.commit()
    conn.close()
    rederive = _rederive()
    rederive.SNAPSHOT_COUNTS.clear()
    return {"data": data, "tmp": tmp, "source": source, "rederive": rederive}


def _snapshot_file(conn) -> pathlib.Path:
    return pathlib.Path(conn.execute("PRAGMA database_list").fetchone()[2])


def _devices_differ(monkeypatch, layout):
    """Report `TMPDIR` on another device than the source's directory."""
    rederive = layout["rederive"]
    real = rederive._device_of
    tmp = str(layout["tmp"])

    def device(path):
        if os.path.realpath(path) == os.path.realpath(tmp):
            return -1
        return real(path)

    monkeypatch.setattr(rederive, "_device_of", device)


def _force_darwin(monkeypatch):
    monkeypatch.setattr(_clone(), "_PLATFORM", "darwin")


def _clone_supported(directory: pathlib.Path) -> bool:
    probe = directory / "clone-probe.src"
    probe.write_bytes(b"probe")
    target = directory / "clone-probe.dst"
    try:
        return _clone().try_clone(str(probe), str(target))
    finally:
        probe.unlink()
        target.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# try_clone
# --------------------------------------------------------------------------


def test_try_clone_off_darwin_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(_clone(), "_PLATFORM", "linux")
    src = tmp_path / "a"
    src.write_bytes(b"x")
    assert _clone().try_clone(str(src), str(tmp_path / "b")) is False
    assert not (tmp_path / "b").exists()


def test_try_clone_refusal_is_false_and_other_errors_raise(
        tmp_path, monkeypatch):
    clone = _clone()
    _force_darwin(monkeypatch)
    src = tmp_path / "a"
    src.write_bytes(b"x")

    def refused(fd, destination):
        raise OSError(errno.ENOTSUP, "no", destination)

    monkeypatch.setattr(clone, "_clone_fd", refused)
    assert clone.try_clone(str(src), str(tmp_path / "b")) is False
    assert not (tmp_path / "b").exists()

    def broken(fd, destination):
        raise OSError(errno.EIO, "io", destination)

    monkeypatch.setattr(clone, "_clone_fd", broken)
    with pytest.raises(OSError) as raised:
        clone.try_clone(str(src), str(tmp_path / "b"))
    assert raised.value.errno == errno.EIO


# --------------------------------------------------------------------------
# _open_sqlite_snapshot
# --------------------------------------------------------------------------


def test_snapshot_is_clone_on_apfs(layout):
    if (_clone()._PLATFORM != "darwin"
            or not _clone_supported(layout["tmp"])):
        pytest.skip("the runner's volume reports no clone support "
                    "(not Darwin, or fclonefileat refused on this volume)")
    rederive = layout["rederive"]
    with rederive._open_sqlite_snapshot(
            layout["source"], prefix="cctally-rederive-cache-") as conn:
        snapshot = _snapshot_file(conn)
        assert snapshot.read_bytes() == layout["source"].read_bytes()
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 200
        assert snapshot.parent.parent == layout["tmp"].resolve() or (
            snapshot.parent.parent.resolve() == layout["tmp"].resolve())
    assert rederive.SNAPSHOT_COUNTS == {"clone": 1}


def test_snapshot_refused_clone_copies_under_tmpdir(layout, monkeypatch):
    rederive = layout["rederive"]
    clone = _clone()
    _force_darwin(monkeypatch)
    _devices_differ(monkeypatch, layout)
    attempts = []

    def refused(fd, destination):
        attempts.append(destination)
        raise OSError(errno.ENOTSUP, "no", destination)

    monkeypatch.setattr(clone, "_clone_fd", refused)
    before = sorted(os.listdir(layout["data"]))
    with rederive._open_sqlite_snapshot(
            layout["source"], prefix="cctally-rederive-cache-") as conn:
        snapshot = _snapshot_file(conn)
        assert snapshot.parent.parent.resolve() == layout["tmp"].resolve()
        assert sorted(os.listdir(layout["data"])) == before, (
            "the refused clone's directory beside the source was removed")
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 200
    assert attempts, "a clone beside the source was attempted first"
    assert pathlib.Path(attempts[0]).parent.parent.resolve() == (
        layout["data"].resolve())
    assert rederive.SNAPSHOT_COUNTS == {"copy": 1}
    assert sorted(os.listdir(layout["data"])) == before
    assert os.listdir(layout["tmp"]) == []


def test_snapshot_dir_beside_source_when_devices_differ(layout, monkeypatch):
    rederive = layout["rederive"]
    _force_darwin(monkeypatch)
    _devices_differ(monkeypatch, layout)

    def cloned(src, dst):
        shutil.copyfile(src, dst)
        return True

    monkeypatch.setattr(_clone(), "try_clone", cloned)
    before = sorted(os.listdir(layout["data"]))
    with rederive._open_sqlite_snapshot(
            layout["source"], prefix="cctally-rederive-cache-") as conn:
        snapshot = _snapshot_file(conn)
        assert snapshot.parent.parent.resolve() == layout["data"].resolve()
        assert snapshot.parent.name.startswith("cctally-rederive-cache-")
        assert os.listdir(layout["tmp"]) == []
    assert sorted(os.listdir(layout["data"])) == before
    assert rederive.SNAPSHOT_COUNTS == {"clone": 1}


def test_snapshot_same_device_stays_under_tmpdir(layout, monkeypatch):
    rederive = layout["rederive"]
    _force_darwin(monkeypatch)
    monkeypatch.setattr(rederive, "_device_of", lambda path: 7)

    def cloned(src, dst):
        shutil.copyfile(src, dst)
        return True

    monkeypatch.setattr(_clone(), "try_clone", cloned)
    with rederive._open_sqlite_snapshot(
            layout["source"], prefix="cctally-rederive-cache-") as conn:
        assert _snapshot_file(conn).parent.parent.resolve() == (
            layout["tmp"].resolve())


def test_snapshot_off_darwin_is_a_copy_under_tmpdir(layout, monkeypatch):
    rederive = layout["rederive"]
    monkeypatch.setattr(_clone(), "_PLATFORM", "linux")
    _devices_differ(monkeypatch, layout)
    with rederive._open_sqlite_snapshot(
            layout["source"], prefix="cctally-rederive-cache-") as conn:
        assert _snapshot_file(conn).parent.parent.resolve() == (
            layout["tmp"].resolve())
    assert rederive.SNAPSHOT_COUNTS == {"copy": 1}


def test_snapshot_retry_reclones_after_identity_change(layout, monkeypatch):
    rederive = layout["rederive"]
    _force_darwin(monkeypatch)
    calls = []

    def cloned(src, dst):
        # A clone needs a NEW path: the retry must have unlinked the first
        # attempt's snapshot before cloning again.
        assert not os.path.lexists(dst), "the previous snapshot was not unlinked"
        shutil.copyfile(src, dst)
        calls.append(dst)
        if len(calls) == 1:
            stat = os.stat(src)
            os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        return True

    monkeypatch.setattr(_clone(), "try_clone", cloned)
    with rederive._open_sqlite_snapshot(
            layout["source"], prefix="cctally-rederive-cache-") as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 200
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert rederive.SNAPSHOT_COUNTS == {"clone": 1}


def _tree(root: pathlib.Path):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


@pytest.mark.parametrize("devices", ["same", "differ"])
def test_preview_leaves_no_new_entries(tmp_path, monkeypatch, capsys, devices):
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    import _cctally_core

    S5.build_clean(_cctally_core.APP_DIR, seed_cache=True)
    tmp = tmp_path / "preview-tmpdir"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    rederive = _rederive()
    rederive.SNAPSHOT_COUNTS.clear()
    if devices == "differ":
        _force_darwin(monkeypatch)
        real = rederive._device_of
        monkeypatch.setattr(
            rederive, "_device_of",
            lambda path: -1 if os.path.realpath(path) == os.path.realpath(
                str(tmp)) else real(path))
    app_dir = pathlib.Path(_cctally_core.APP_DIR)
    before_tmp, before_app = _tree(tmp), _tree(app_dir)
    parser = mod.build_parser()
    args = parser.parse_args(
        ["db", "rederive", "--family", "claude-usage", "--json"])
    assert mod.cmd_db_rederive(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "preview"
    assert sum(rederive.SNAPSHOT_COUNTS.values()) >= 1
    assert _tree(tmp) == before_tmp
    assert _tree(app_dir) == before_app
