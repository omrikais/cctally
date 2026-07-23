"""Crash-safe cache.db corruption recovery.

Regression for the 2026-07-23 dashboard SIGBUS: the legacy opener unlinked
cache.db in place while another process still held a WAL reader.  The live
reader then faulted in sqlite3.walFindFrame after the mapped file was shortened.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths


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
