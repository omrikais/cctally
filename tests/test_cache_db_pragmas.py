"""#279 S1 F8 — cache.db opens with synchronous=NORMAL (re-derivable, WAL).

Under WAL, SQLite's default synchronous=FULL fsyncs more than needed on a
fully re-derivable DB. stats.db already sets NORMAL; cache.db did not. NORMAL
risks at most the tail transaction on power loss, and cache.db can always be
rebuilt (`cache-sync --rebuild`).
"""
from conftest import load_script, redirect_paths


def test_open_cache_db_sets_synchronous_normal(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_cache_db"]()
    try:
        # PRAGMA synchronous: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA.
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        conn.close()
