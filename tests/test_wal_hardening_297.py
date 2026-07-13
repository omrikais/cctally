"""cache.db WAL hardening (#297) — pragmas, checkpoint core, end-of-sync drain.

Golden harnesses are wrong here (output would carry volatile WAL byte
counts), so this is pytest. The DB-open tests go through the repo's
``load_script() + redirect_paths()`` harness because both ``open_cache_db``
and ``open_db`` resolve the live ``cctally`` module via the ``_cctally()``
call-time accessor — a bare ``importlib.import_module`` leaves
``sys.modules['cctally']`` unset and raises ``KeyError 'cctally'`` (verified
against source). The lower-level helper tests (``_run_wal_truncate`` /
``_maybe_truncate_wal``) operate only on the connection passed in, so they
import ``_cctally_cache`` directly.
"""
import importlib
import json
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from conftest import load_script, redirect_paths  # noqa: E402


def _load(name):
    return importlib.import_module(name)


# --- Task 1: WAL-cap + busy_timeout pragmas at both DB-open chokepoints ----

def test_open_cache_db_sets_wal_cap_and_timeout(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 134217728
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
    finally:
        conn.close()


def test_open_db_sets_wal_cap_and_timeout(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    try:
        assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 16777216
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
    finally:
        conn.close()


# --- Task 2: _run_wal_truncate checkpoint core ----------------------------

def _grow_wal(db_path):
    """Grow the -wal sidecar and return the STILL-OPEN connection.

    The caller MUST keep the returned connection open: when the last
    connection on a WAL database closes, SQLite checkpoints and deletes
    the WAL file, so a closed writer would leave nothing to drain. An
    idle (committed, no active txn) connection does not pin a read
    snapshot, so a second connection can still TRUNCATE the WAL.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")  # let the WAL grow, no auto-truncate
    conn.execute("CREATE TABLE IF NOT EXISTS t(x)")
    conn.executemany("INSERT INTO t(x) VALUES (?)", [(i,) for i in range(20000)])
    conn.commit()
    return conn


def test_run_wal_truncate_drains(tmp_path):
    cache = _load("_cctally_cache")
    db = tmp_path / "cache.db"
    writer = _grow_wal(db)  # keep idle-open so the WAL file persists
    wal = str(db) + "-wal"
    try:
        assert os.path.getsize(wal) > 0
        conn = sqlite3.connect(str(db))
        try:
            res = cache._run_wal_truncate(conn, db, db_label="cache.db")
        finally:
            conn.close()
        assert res.truncated is True
        assert res.busy is False
        assert res.wal_bytes_before > 0
        assert (not os.path.exists(wal)) or os.path.getsize(wal) == 0
    finally:
        writer.close()


def test_run_wal_truncate_busy_when_reader_pins(tmp_path):
    cache = _load("_cctally_cache")
    db = tmp_path / "cache.db"
    writer = _grow_wal(db)  # keep idle-open so the WAL file persists
    wal = str(db) + "-wal"
    reader = sqlite3.connect(str(db))
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM t").fetchone()  # pins a read snapshot
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA busy_timeout=100")
    try:
        res = cache._run_wal_truncate(conn, db, db_label="cache.db")
        assert res.busy is True
        assert res.truncated is False
        assert os.path.getsize(wal) > 0  # WAL untouched while the reader pins it
    finally:
        conn.close()
        reader.close()
    # after the reader releases, a re-run truncates:
    conn2 = sqlite3.connect(str(db))
    try:
        res2 = cache._run_wal_truncate(conn2, db, db_label="cache.db")
        assert res2.truncated is True
    finally:
        conn2.close()
        writer.close()


# --- Task 3: end-of-sync forced checkpoint --------------------------------

def test_maybe_truncate_wal_gated_below_threshold(tmp_path, monkeypatch):
    cache = _load("_cctally_cache")
    db = tmp_path / "cache.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES(1)")
    conn.commit()
    # Force a huge threshold so a small WAL is 'below'; assert no exception + no-op.
    monkeypatch.setattr(cache, "CACHE_WAL_CHECKPOINT_TRIGGER_BYTES", 10**12)
    cache._maybe_truncate_wal(conn, db)  # below threshold -> returns without checkpoint
    conn.close()


def test_maybe_truncate_wal_drains_above_threshold(tmp_path, monkeypatch):
    cache = _load("_cctally_cache")
    db = tmp_path / "cache.db"
    writer = _grow_wal(db)
    # keep the SAME connection (committed, autocommit) to mimic end-of-sync
    monkeypatch.setattr(cache, "CACHE_WAL_CHECKPOINT_TRIGGER_BYTES", 1)  # any WAL is 'above'
    assert cache._wal_file_size(db) > 0
    cache._maybe_truncate_wal(writer, db)
    wal = str(db) + "-wal"
    assert (not os.path.exists(wal)) or os.path.getsize(wal) == 0
    # busy_timeout is restored to its pre-checkpoint value (default 5000).
    assert writer.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    writer.close()


# --- Task 3 (wiring): both ingest end-hooks INVOKE the forced WAL drain -----
#
# Review finding M1: ``_maybe_truncate_wal`` was unit-tested in isolation
# (above) but no test asserted that ``sync_cache`` / ``sync_codex_cache`` are
# actually WIRED to call it at end-of-sync (call sites in ``_cctally_cache.py``).
#
# A "run a real sync, then assert the ``-wal`` file is drained" test is
# VACUOUS: each sync CLOSES its DB connection before returning, and SQLite
# deletes the ``-wal`` sidecar on last-connection-close regardless of whether
# our checkpoint ran — so such a test passes even if the
# ``_maybe_truncate_wal(...)`` line is deleted. Instead these SPY the drain
# (monkeypatch it with a recorder) and assert it is invoked EXACTLY ONCE with
# ``(conn, CACHE_DB_PATH)``. The assertion is on the CALL itself, which only
# happens because the line is wired into the sync body — so removing that line
# leaves ``calls == []`` and the test goes RED. (Confirmed non-vacuous by
# deleting each call site and observing both tests fail; call sites restored.)
#
# The spy replaces the real drain, so no actual checkpoint runs — that is fine:
# these tests verify wiring, not the checkpoint behavior (covered above).
# ``sync_cache``/``sync_codex_cache`` reference ``_maybe_truncate_wal`` as a
# bare module global, so patching it on the ``_cctally_cache`` module is seen at
# call time; ``open_cache_db``/``sync_*`` come off ``ns`` per the harness note.


def test_sync_cache_wires_end_of_sync_wal_drain(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache = sys.modules["_cctally_cache"]
    core = sys.modules["_cctally_core"]

    calls = []
    monkeypatch.setattr(
        cache, "_maybe_truncate_wal",
        lambda conn, db_path: calls.append((conn, db_path)),
    )

    projects = tmp_path / ".claude" / "projects" / "-Users-u-project-A"
    projects.mkdir(parents=True)
    entry = json.dumps({
        "type": "assistant", "timestamp": "2026-07-01T10:00:00Z",
        "requestId": "req_1",
        "message": {"id": "msg_1", "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 5, "output_tokens": 7}},
    })
    (projects / "sess-a.jsonl").write_text(entry + "\n")

    conn = ns["open_cache_db"]()
    try:
        ns["sync_cache"](conn)
        assert len(calls) == 1, f"expected exactly one WAL-drain call, got {len(calls)}"
        assert calls[0][0] is conn                 # the SAME open sync connection
        assert calls[0][1] == core.CACHE_DB_PATH    # the cache DB path
    finally:
        conn.close()


def test_sync_codex_cache_wires_end_of_sync_wal_drain(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache = sys.modules["_cctally_cache"]
    core = sys.modules["_cctally_core"]

    calls = []
    monkeypatch.setattr(
        cache, "_maybe_truncate_wal",
        lambda conn, db_path: calls.append((conn, db_path)),
    )

    codex_home = tmp_path / ".codex"
    sessions = codex_home / "sessions" / "2026" / "07" / "01"
    sessions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    rollout = (
        sessions
        / "rollout-2026-07-01T10-00-00-aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa.jsonl"
    )
    rollout.write_text("\n".join([
        json.dumps({"timestamp": "2026-07-01T10:00:00Z", "type": "session_meta",
                    "payload": {"id": "sess-1"}}),
        json.dumps({"timestamp": "2026-07-01T10:00:00Z", "type": "turn_context",
                    "payload": {"model": "gpt-5"}}),
        json.dumps({"timestamp": "2026-07-01T10:00:01Z", "type": "event_msg",
                    "payload": {"type": "token_count", "info": {
                        "last_token_usage": {
                            "input_tokens": 100, "output_tokens": 0,
                            "cached_input_tokens": 0,
                            "reasoning_output_tokens": 0, "total_tokens": 100},
                        "total_token_usage": {"total_tokens": 100}}}}),
    ]) + "\n")

    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
        assert len(calls) == 1, f"expected exactly one WAL-drain call, got {len(calls)}"
        assert calls[0][0] is conn                 # the SAME open sync connection
        assert calls[0][1] == core.CACHE_DB_PATH    # the cache DB path
    finally:
        conn.close()
