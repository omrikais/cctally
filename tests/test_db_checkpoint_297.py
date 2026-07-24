"""`cctally db checkpoint` — raw-connect WAL drain (#297, Task 4).

The handler opens a RAW existing-file-only connection
(``sqlite3.connect("file:<path>?mode=rw", uri=True)`` guarded by
``path.exists()``) — NOT ``open_cache_db()`` / ``open_db()``, which apply
schema, run the migration dispatcher, can DELETE Codex rows, and create a
missing DB. Tests import ``_cctally_db`` / ``_cctally_core`` directly and
monkeypatch ``CACHE_DB_PATH`` (the handler resolves it via ``_cctally_core``
at call time, no live ``cctally`` module needed).

``_grow`` returns the STILL-OPEN writer connection: when the last connection
on a WAL database closes, SQLite checkpoints and deletes the -wal file, so a
closed writer would leave nothing to drain (verified against SQLite 3.53.3).
An idle (committed, no txn) connection pins no read snapshot, so the handler's
separate raw connection can still TRUNCATE the WAL.
"""
import argparse
import fcntl
import importlib
import json
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))


def _load(n):
    return importlib.import_module(n)


def _args(**kw):
    ns = argparse.Namespace(db="cache", json=False, busy_timeout_ms=15000)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _grow(db):
    """Grow the -wal sidecar and return the STILL-OPEN writer connection."""
    c = sqlite3.connect(str(db))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE t(x)")
    c.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(20000)])
    c.commit()
    return c


def test_checkpoint_drains_exit0(tmp_path, monkeypatch, capsys):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    db = tmp_path / "cache.db"
    writer = _grow(db)
    monkeypatch.setattr(core, "CACHE_DB_PATH", db)
    try:
        assert os.path.getsize(str(db) + "-wal") > 0
        rc = dbmod.cmd_db_checkpoint(_args())
        assert rc == 0
        assert (not os.path.exists(str(db) + "-wal")) or os.path.getsize(str(db) + "-wal") == 0
    finally:
        writer.close()


def test_checkpoint_missing_db_exit0(tmp_path, monkeypatch, capsys):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    monkeypatch.setattr(core, "CACHE_DB_PATH", tmp_path / "nope.db")
    assert dbmod.cmd_db_checkpoint(_args()) == 0
    assert "nothing to drain" in capsys.readouterr().out


def test_checkpoint_busy_exit3(tmp_path, monkeypatch):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    db = tmp_path / "cache.db"
    writer = _grow(db)
    monkeypatch.setattr(core, "CACHE_DB_PATH", db)
    pin = sqlite3.connect(str(db))
    pin.execute("BEGIN")
    pin.execute("SELECT count(*) FROM t").fetchone()  # pin snapshot with WAL frames
    try:
        rc = dbmod.cmd_db_checkpoint(_args(busy_timeout_ms=100))
        assert rc == 3
        # WAL stayed put — nothing was truncated while the reader pins it.
        assert os.path.getsize(str(db) + "-wal") > 0
    finally:
        pin.close()
        writer.close()


def test_cache_checkpoint_defers_while_global_writer_flock_is_held(
    tmp_path, monkeypatch
):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    db = tmp_path / "cache.db"
    writer = _grow(db)
    lock_path = tmp_path / "cache.db.lock"
    monkeypatch.setattr(core, "CACHE_DB_PATH", db)
    monkeypatch.setattr(core, "CACHE_LOCK_PATH", lock_path)
    held = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        rc = dbmod.cmd_db_checkpoint(_args(busy_timeout_ms=100))
        assert rc == 3
        assert os.path.getsize(str(db) + "-wal") > 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
        writer.close()


def test_cache_checkpoint_defers_during_exclusive_maintenance(
    tmp_path, monkeypatch
):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    db = tmp_path / "cache.db"
    writer = _grow(db)
    maintenance_path = tmp_path / "cache.db.maintenance.lock"
    monkeypatch.setattr(core, "CACHE_DB_PATH", db)
    monkeypatch.setattr(core, "CACHE_LOCK_MAINTENANCE_PATH", maintenance_path)
    monkeypatch.setattr(core, "CACHE_LOCK_PATH", tmp_path / "cache.db.lock")
    held = os.open(str(maintenance_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        rc = dbmod.cmd_db_checkpoint(_args(busy_timeout_ms=100))
        assert rc == 3
        assert os.path.getsize(str(db) + "-wal") > 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
        writer.close()


def test_checkpoint_json_schemaversion_first(tmp_path, monkeypatch, capsys):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    db = tmp_path / "cache.db"
    writer = _grow(db)
    monkeypatch.setattr(core, "CACHE_DB_PATH", db)
    try:
        dbmod.cmd_db_checkpoint(_args(json=True))
        out = capsys.readouterr().out
        obj = json.loads(out)
        assert list(obj.keys())[0] == "schemaVersion"
        assert obj["schemaVersion"] == 1
        assert set(["db", "walBytesBefore", "walBytesAfter", "framesCheckpointed",
                    "busy", "truncated"]).issubset(obj)
    finally:
        writer.close()


def test_checkpoint_does_not_create_missing_db(tmp_path, monkeypatch):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    db = tmp_path / "nope.db"
    monkeypatch.setattr(core, "CACHE_DB_PATH", db)
    dbmod.cmd_db_checkpoint(_args())
    assert not db.exists()  # raw connect never created it


def test_checkpoint_missing_db_json_present_false(tmp_path, monkeypatch, capsys):
    core = _load("_cctally_core")
    dbmod = _load("_cctally_db")
    monkeypatch.setattr(core, "CACHE_DB_PATH", tmp_path / "nope.db")
    assert dbmod.cmd_db_checkpoint(_args(json=True)) == 0
    obj = json.loads(capsys.readouterr().out)
    assert list(obj.keys())[0] == "schemaVersion"
    assert obj["present"] is False
    assert obj["truncated"] is True  # absent re-derivable cache is not an error
