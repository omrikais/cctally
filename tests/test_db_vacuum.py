"""#313 P3 (F13): `cctally db vacuum` reclaim command.

Shrinks the file after a prune left free pages, fails PROMPTLY (not hang) under
an active reader via a real SQLite EXCLUSIVE lock, and refuses when free disk is
below the ~2x-file margin.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _seed_and_free_pages(ns, n=3000):
    conn = ns["open_cache_db"]()
    conn.executemany(
        "INSERT INTO conversation_messages "
        "(session_id, uuid, source_path, byte_offset, timestamp_utc, entry_type, text) "
        "VALUES (?,?,?,?,?,?,?)",
        [("s", f"u{i}", "seed.jsonl", i, "2026-07-01T00:00:00Z", "human", "x" * 500)
         for i in range(n)],
    )
    conn.commit()
    conn.execute("DELETE FROM conversation_messages")
    conn.commit()
    before = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.close()
    return before


def _page_count(ns):
    conn = ns["open_cache_db"]()
    try:
        return conn.execute("PRAGMA page_count").fetchone()[0]
    finally:
        conn.close()


def test_vacuum_cache_shrinks_page_count(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    before = _seed_and_free_pages(ns)
    rc = ns["cmd_db_vacuum"](argparse.Namespace(db="cache"))
    assert rc == 0
    assert _page_count(ns) < before


def test_vacuum_fails_promptly_under_active_reader(tmp_path, monkeypatch, capsys):
    ns = _load(tmp_path, monkeypatch)
    _seed_and_free_pages(ns, n=200)
    import _cctally_core
    reader = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH))
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()
    try:
        t0 = time.monotonic()
        rc = ns["cmd_db_vacuum"](argparse.Namespace(db="cache"))
        elapsed = time.monotonic() - t0
        assert rc == 3, "an in-use DB must fail, not silently succeed"
        assert elapsed < 5.0, "must fail promptly, not hang"
        assert "in use" in capsys.readouterr().err.lower()
    finally:
        reader.close()


def test_vacuum_refuses_on_insufficient_disk(tmp_path, monkeypatch, capsys):
    ns = _load(tmp_path, monkeypatch)
    _seed_and_free_pages(ns, n=200)
    import _cctally_db
    monkeypatch.setattr(_cctally_db, "_free_disk_bytes", lambda d: 1)
    rc = ns["cmd_db_vacuum"](argparse.Namespace(db="cache"))
    assert rc == 3
    assert "not enough free disk" in capsys.readouterr().err.lower()


def test_vacuum_absent_db_is_noop(tmp_path, monkeypatch, capsys):
    ns = _load(tmp_path, monkeypatch)
    # stats.db not created in this fixture → nothing to reclaim, exit 0.
    rc = ns["cmd_db_vacuum"](argparse.Namespace(db="stats"))
    assert rc == 0
    assert "nothing to reclaim" in capsys.readouterr().out.lower()
