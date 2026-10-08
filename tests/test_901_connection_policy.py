"""#901 G8 (spec §4.7, §5.3a, Q11): the sync and ingest writers keep their
statement journals in memory.

Every opener §4.7 enumerates reports ``PRAGMA temp_store = 2`` (MEMORY) at
open, before any transaction: the cache, conversations and stats openers, the
cache-recovery replacements, the journal's two raw cache writers and the
reclaim connection. The read-only, state, backup, checkpoint and vacuum
connections keep SQLite's default, as does the stats publication connection
(an unlisted writer this amendment leaves unchanged).

The decisive case runs a real conversation-sync pass and a real Codex cache
ingest in a child process whose SQLite temp directory is injected. SQLite
unlinks a temp file the moment it creates it, so a directory listing never
shows one; the directory's modification time does, because both the create
and the unlink rewrite it. A control on an identical copy with the policy
disabled must spill, in the very statement the fixture targets (the
conversation-message insert, whose FTS5 trigger writes segments, and the quota
insert), and both runs must write the same number of WAL frames: the policy
moves statement journals and nothing else.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

import pytest

from conftest import load_script, redirect_paths

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
UTC = dt.timezone.utc
START = dt.datetime(2026, 7, 14, 12, tzinfo=UTC)
FIXED = dt.datetime(2026, 7, 15, 12, tzinfo=UTC)
MEMORY = 2
DEFAULT = 0
#: The injected temp directory's modification time before a child runs.
UNTOUCHED_NS = 10**18
SESSION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _temp_store(conn) -> int:
    return int(sqlite3.Connection.execute(
        conn, "PRAGMA temp_store").fetchone()[0])


@pytest.fixture
def ns(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


# ── every §4.7 opener reports MEMORY at open ────────────────────────────────

def test_the_cache_opener_sets_memory_on_first_and_steady_state_opens(ns):
    for _ in range(2):  # first open applies the schema; the second does not
        conn = ns["open_cache_db"]()
        try:
            assert _temp_store(conn) == MEMORY
            assert conn.execute("PRAGMA cache_size").fetchone()[0] == -2000, (
                "no ordinary writer's cache size changes")
        finally:
            conn.close()


def test_the_conversations_openers_set_memory(ns):
    cache = importlib.import_module("_cctally_cache")
    for attach in (False, True):
        conn = ns["open_conversations_db"](attach_cache=attach)
        try:
            assert _temp_store(conn) == MEMORY
        finally:
            conn.close()
    conn = cache._open_conversations_db_for_recovery(attach_cache=False)
    try:
        assert _temp_store(conn) == MEMORY
    finally:
        conn.close()


def test_the_stats_opener_and_its_scratch_target_set_memory(ns, tmp_path):
    conn = ns["open_db"]()
    try:
        assert _temp_store(conn) == MEMORY
    finally:
        conn.close()
    core = importlib.import_module("_cctally_core")
    conn = core.open_db(_target_path=str(tmp_path / "scratch-stats.db"))
    try:
        assert _temp_store(conn) == MEMORY
    finally:
        conn.close()


FILE = 1


def _record_temp_store(module, name: str, monkeypatch, seen: list) -> None:
    """Record the connection's temp store whenever ``module.name`` runs."""
    real = getattr(module, name)

    def spy(conn, *args, **kwargs):
        seen.append((name, _temp_store(conn)))
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(module, name, spy)


def test_pending_schema_work_runs_under_a_file_temp_store(ns, monkeypatch):
    """PR-3. Every writable opener selected MEMORY before its schema and
    migration work, so a one-time index build or migration over a
    history-sized table sorted entirely in RAM on upgrade (the base selected
    no temp store). Pending work now runs under FILE and the opener returns
    with MEMORY; a current-schema open never switches: it selects the writer
    temp store exactly once and runs none of that work."""
    db = importlib.import_module("_cctally_db")
    cache = importlib.import_module("_cctally_cache")
    core = importlib.import_module("_cctally_core")
    store = importlib.import_module("_cctally_store")
    seen: list = []
    _record_temp_store(db, "_apply_cache_schema", monkeypatch, seen)
    _record_temp_store(db, "_run_pending_cache_migrations_under_writer_lock",
                       monkeypatch, seen)
    _record_temp_store(db, "_apply_conversations_schema", monkeypatch, seen)
    _record_temp_store(cache, "_run_pending_migrations", monkeypatch, seen)
    _record_temp_store(core, "_apply_stats_read_indexes", monkeypatch, seen)
    _record_temp_store(store, "mark_stats_open_fixups_done", monkeypatch, seen)
    selections: list = []
    real_select = store.apply_writer_temp_store

    def select(conn):
        selections.append(1)
        return real_select(conn)

    monkeypatch.setattr(store, "apply_writer_temp_store", select)
    openers = (
        ("cache", lambda: ns["open_cache_db"]()),
        ("conversations",
         lambda: ns["open_conversations_db"](attach_cache=False)),
        ("stats", lambda: ns["open_db"]()),
    )
    for label, opener in openers:
        seen.clear()
        conn = opener()  # the first open: the schema is pending
        try:
            assert seen, f"non-vacuity: no {label} schema work was observed"
            assert all(value == FILE for _name, value in seen), (
                f"{label} schema work ran under a memory temp store", seen)
            assert _temp_store(conn) == MEMORY, label
        finally:
            conn.close()
    for label, opener in openers:
        seen.clear()
        selections.clear()
        conn = opener()  # current schema
        try:
            assert [name for name, _value in seen
                    if name != "_run_pending_migrations"] == [], (label, seen)
            assert len(selections) == 1, (
                f"a current-schema {label} open switched its temp store",
                len(selections))
            assert _temp_store(conn) == MEMORY, label
        finally:
            conn.close()


def _corrupt():
    return sqlite3.DatabaseError("database disk image is malformed")


def test_cache_recovery_replacements_set_memory(ns, monkeypatch):
    cache = importlib.import_module("_cctally_cache")
    ns["open_cache_db"]().close()
    monkeypatch.setattr(cache, "_recover_corrupt_cache",
                        lambda exc, **_kw: True)
    monkeypatch.setattr(cache, "_cache_storm_test_pause", lambda _point: None)

    # The opener's own one retry after a recovered open.
    real_guarded = cache._cache_open_guarded
    calls = []

    def guarded():
        calls.append(1)
        if len(calls) == 1:
            raise _corrupt()
        return real_guarded()

    monkeypatch.setattr(cache, "_cache_open_guarded", guarded)
    conn = cache.open_cache_db()
    try:
        assert len(calls) == 2, "non-vacuity: the open recovered once"
        assert _temp_store(conn) == MEMORY
    finally:
        conn.close()
    monkeypatch.setattr(cache, "_cache_open_guarded", real_guarded)

    # The replacement a provider plan continues on.
    ran = []

    def operation(conn):
        ran.append(conn)
        if len(ran) == 1:
            raise _corrupt()
        return "ok"

    first = sqlite3.connect(":memory:")
    results, replacement = cache._run_cache_plan_with_recovery(
        first, (operation,), origins=("g8",))
    try:
        assert results == ("ok",) and replacement is not first
        assert _temp_store(replacement) == MEMORY
    finally:
        replacement.close()


class _Recorded(sqlite3.Connection):
    """Records ``temp_store`` when a transaction begins and at close, with the
    product functions on the stack."""

    log: list = []

    def _record(self, event):
        frames, frame = set(), sys._getframe(2)
        while frame is not None:
            frames.add(frame.f_code.co_name)
            frame = frame.f_back
        try:
            value = _temp_store(self)
        except sqlite3.Error:
            return
        _Recorded.log.append((event, value, frames))

    def execute(self, sql, *args):
        if str(sql).lstrip().upper().startswith("BEGIN"):
            self._record("begin")
        return super().execute(sql, *args)

    def close(self):
        self._record("close")
        return super().close()


@pytest.fixture
def recorded(monkeypatch):
    real = sqlite3.connect

    def connect(*args, **kwargs):
        kwargs.setdefault("factory", _Recorded)
        return real(*args, **kwargs)

    _Recorded.log = []
    monkeypatch.setattr(sqlite3, "connect", connect)
    return _Recorded.log


def _values(log, event, function):
    return [value for kind, value, frames in log
            if kind == event and function in frames]


def _attribution(jl):
    return jl.make_codex_window_attribution(
        at="2026-08-14T00:00:00Z", account_key="a" * 32,
        source_root_key="root-a", logical_limit_key="limit-weekly",
        observed_slot="primary", window_minutes=10080,
        raw_resets_at_utc=["2026-07-20T09:45:35Z"],
        canonical_resets_at_utc="2026-07-20T09:40:00Z")


def test_the_journal_raw_cache_writers_set_memory_before_their_transaction(
    ns, recorded,
):
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    ns["open_cache_db"]().close()
    ns["open_db"]().close()
    jr.append_record(_attribution(jl), now_utc=FIXED)
    assert jr.run_stats_ingest(mode="authoritative").ran
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    applier = _values(recorded, "begin", "_cache_applier")
    recovery = _values(recorded, "begin", "_run_bounded_recovery")
    assert applier and recovery, (
        "non-vacuity: both raw writers opened a transaction", recorded)
    assert set(applier) == {MEMORY}, applier
    assert set(recovery) == {MEMORY}, recovery
    publication = _values(recorded, "close", "_publish_stats_index_in_place")
    assert publication, "non-vacuity: the rebuild published in place"
    assert set(publication) == {DEFAULT}, (
        "the stats publication connection is not an enumerated writer",
        publication)


def test_the_reclaim_connection_keeps_journals_and_dirty_pages_in_memory(ns):
    retention = importlib.import_module("_lib_conversation_retention")
    core = importlib.import_module("_cctally_core")
    ns["open_conversations_db"](attach_cache=False).close()
    conn = retention.open_reclaim_connection(core.CONVERSATIONS_DB_PATH)
    try:
        assert _temp_store(conn) == MEMORY
        assert conn.execute("PRAGMA cache_spill").fetchone()[0] == 0
    finally:
        conn.close()


# ── every other connection keeps SQLite's default ───────────────────────────

def test_state_and_read_only_connections_keep_the_default(ns):
    retention = importlib.import_module("_lib_conversation_retention")
    core = importlib.import_module("_cctally_core")
    cache = importlib.import_module("_cctally_cache")
    db = importlib.import_module("_cctally_db")
    ns["open_conversations_db"](attach_cache=True).close()
    for conn in (
        retention._open_state_connection(core.CONVERSATIONS_DB_PATH),
        cache.open_conversations_db_readonly(attach_cache=True),
        db._open_cache_ro_with_gate_defer(),
    ):
        try:
            assert _temp_store(conn) == DEFAULT
        finally:
            conn.close()


def test_backup_checkpoint_and_vacuum_connections_keep_the_default(
    ns, recorded, tmp_path,
):
    db = importlib.import_module("_cctally_db")
    ns["open_cache_db"]().close()
    ns["open_conversations_db"](attach_cache=False).close()
    ns["open_db"]().close()
    del recorded[:]
    for which in ("cache", "stats"):
        assert db.cmd_db_backup(argparse.Namespace(
            db=which, backup_output=str(tmp_path / f"{which}.bak"),
            busy_timeout_ms=1000)) == 0
    for which in ("cache", "conversations"):
        db.cmd_db_checkpoint(argparse.Namespace(
            db=which, json=True, busy_timeout_ms=1000))
    assert db.cmd_db_vacuum(argparse.Namespace(db="all")) == 0
    for function in ("cmd_db_backup", "cmd_db_checkpoint",
                     "_run_vacuum_exclusive"):
        values = _values(recorded, "close", function)
        assert values, f"non-vacuity: {function} opened a connection"
        assert set(values) == {DEFAULT}, (function, values)


# ── no temp file on a real sync pass and a real ingest (non-vacuous) ────────

_CHILD = r'''
import json, os, sqlite3, sys

bin_dir, workload, mode = sys.argv[1:4]
sys.path.insert(0, bin_dir)
import importlib.util as ilu
from importlib.machinery import SourceFileLoader

loader = SourceFileLoader("cctally", os.path.join(bin_dir, "cctally"))
spec = ilu.spec_from_loader("cctally", loader)
mod = ilu.module_from_spec(spec)
sys.modules["cctally"] = mod
loader.exec_module(mod)
import _cctally_core
import _cctally_store

if mode == "control":
    # The policy disabled: SQLite's default (file) temp store.
    _cctally_store.WRITER_TEMP_STORE = "DEFAULT"

watched = os.environ["SQLITE_TMPDIR"]
untouched = int(os.environ["G8_UNTOUCHED_NS"])
first_spill = []


def _check(sql):
    if not first_spill and os.stat(watched).st_mtime_ns != untouched:
        first_spill.append(" ".join(str(sql).split())[:120])


class Cursor(sqlite3.Cursor):
    def execute(self, sql, *args):
        try:
            return super().execute(sql, *args)
        finally:
            _check(sql)

    def executemany(self, sql, *args):
        try:
            return super().executemany(sql, *args)
        finally:
            _check(sql)


class Connection(sqlite3.Connection):
    def cursor(self, factory=Cursor):
        return super().cursor(factory)

    def execute(self, sql, *args):
        try:
            return super().execute(sql, *args)
        finally:
            _check(sql)

    def executemany(self, sql, *args):
        try:
            return super().executemany(sql, *args)
        finally:
            _check(sql)

    def commit(self):
        try:
            return super().commit()
        finally:
            _check("COMMIT")


real_connect = sqlite3.connect


def connect(*args, **kwargs):
    kwargs.setdefault("factory", Connection)
    conn = real_connect(*args, **kwargs)
    # No checkpoint during the run, so the WAL holds every frame it wrote.
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    return conn


sqlite3.connect = connect
target = str(_cctally_core.CONVERSATIONS_DB_PATH
             if workload == "conversation-sync"
             else _cctally_core.CACHE_DB_PATH)
pin = real_connect(target)  # keeps the WAL from a checkpoint-on-close
pin.execute("SELECT 1").fetchone()
if workload == "conversation-sync":
    import _cctally_dashboard
    status = str(_cctally_dashboard._conversation_sync_pass())
else:
    cache = mod.open_cache_db()
    try:
        stats = mod.sync_codex_cache(cache)
    finally:
        cache.close()
    status = f"rows_changed={stats.rows_changed}"
page = pin.execute("PRAGMA page_size").fetchone()[0]
wal = os.path.getsize(target + "-wal")
print(json.dumps({"status": status, "first_spill": first_spill,
                  "frames": (wal - 32) // (page + 24)}))
pin.close()
'''


def _words(i, count):
    return " ".join(f"w{(i * 37 + j * 101) % 50000}" for j in range(count))


def _claude_lines(first, count):
    lines = []
    for i in range(first, first + count):
        ts = (START + dt.timedelta(seconds=10 * i)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
        message = {"content": [{"text": _words(i, 300), "type": "text"}],
                   "role": "user" if i % 2 == 0 else "assistant"}
        record = {"cwd": "/synthetic/g8", "message": message,
                  "sessionId": SESSION, "timestamp": ts,
                  "type": message["role"], "uuid": f"g8-u{i}"}
        if i % 2:
            message.update(id=f"g8-m{i}", model="claude-opus-4-8", usage={
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "input_tokens": 10, "output_tokens": 20})
            record.update(parentUuid=f"g8-u{i - 1}", requestId=f"g8-r{i}")
        lines.append(json.dumps(record) + "\n")
    return "".join(lines)


def _rollout_lines(first, count, *, meta):
    lines = []
    if meta:
        lines.append(json.dumps({"payload": {
            "cwd": "/synthetic/g8", "id": "g8-thread", "model": "gpt-5",
            "model_provider": "openai", "session_id": "g8-thread",
            "source": "cli", "thread_source": "user"},
            "timestamp": START.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "type": "session_meta"}) + "\n")
    for i in range(first, first + count):
        ts = (START + dt.timedelta(seconds=30 * i)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        limits = {
            "limit_id": "codex", "plan_type": "pro",
            "primary": {"resets_at": 1784048400 + 18000 * (i // 400),
                        "used_percent": (i % 100) * 0.5,
                        "window_minutes": 300},
            "secondary": {"resets_at": 1784635200,
                          "used_percent": (i % 100) * 0.25,
                          "window_minutes": 10080}}
        lines.append(json.dumps({"payload": {"info": {
            "last_token_usage": {
                "cached_input_tokens": 3, "input_tokens": 12,
                "output_tokens": 4, "reasoning_output_tokens": 1,
                "total_tokens": 16},
            "model_context_window": 272000, "rate_limits": limits,
            "total_token_usage": {"total_tokens": 16 * (i + 1)}},
            "type": "token_count"}, "timestamp": ts,
            "type": "event_msg"}) + "\n")
        lines.append(json.dumps({"payload": {
            "content": [{"text": _words(i, 20), "type": "input_text"}],
            "role": "user", "type": "message"}, "timestamp": ts,
            "type": "response_item"}) + "\n")
    return "".join(lines)


#: workload -> (the statement the control must first spill in, base, delta).
#: #901 W11 applies an unbudgeted cache ingest's files inside one transaction,
#: each in its own savepoint, so the accounting insert (the file's first large
#: statement) is now the first that needs a statement journal; it was the
#: quota insert while every file committed alone.
WORKLOADS = {
    "conversation-sync": (
        "INSERT OR IGNORE INTO conversation_messages", 200, 200),
    "cache-ingest": ("INSERT OR IGNORE INTO codex_session_entries", 600, 60),
}


def _build(ns, tmp_path, monkeypatch, workload):
    """Ingest a base, then append the delta the child will sync."""
    _statement, base, delta = WORKLOADS[workload]
    ns["CONFIG_PATH"].write_text('{"conversation":{"retention_days":0}}\n')
    codex = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex))
    rollout = codex / "sessions" / "2026" / "07" / "14" / "rollout-g8.jsonl"
    rollout.parent.mkdir(parents=True)
    transcript = (tmp_path / ".claude" / "projects" / "-synthetic-g8"
                  / f"{SESSION}.jsonl")
    transcript.parent.mkdir(parents=True)
    if workload == "conversation-sync":
        transcript.write_text(_claude_lines(0, base))
        rollout.write_text(_rollout_lines(0, 2, meta=True))
        conn = ns["open_conversations_db"]()
        try:
            ns["sync_claude_conversations"](conn)
            ns["sync_codex_conversations"](conn)
        finally:
            conn.close()
        with transcript.open("a") as fh:
            fh.write(_claude_lines(base, delta))
    else:
        rollout.write_text(_rollout_lines(0, base, meta=True))
        cache = ns["open_cache_db"]()
        try:
            ns["sync_codex_cache"](cache)
        finally:
            cache.close()
        with rollout.open("a") as fh:
            fh.write(_rollout_lines(base, delta, meta=False))
    return codex


def _run_child(tmp_path, codex, workload, mode):
    """Run one pass on a fresh copy of the base store (the source files are
    shared, so their identities, offsets and paths are identical)."""
    data = tmp_path / ".local" / "share" / "cctally"
    snapshot = tmp_path / "base-store"
    if not snapshot.exists():
        shutil.copytree(data, snapshot)
    else:
        shutil.rmtree(data)
        shutil.copytree(snapshot, data)
    watched = tmp_path / f"sqlite-tmp-{mode}"
    watched.mkdir()
    os.utime(watched, ns=(UNTOUCHED_NS, UNTOUCHED_NS))
    env = {**os.environ, "HOME": str(tmp_path), "CCTALLY_DATA_DIR": str(data),
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1", "CODEX_HOME": str(codex),
           "SQLITE_TMPDIR": str(watched), "TMPDIR": str(watched),
           "G8_UNTOUCHED_NS": str(UNTOUCHED_NS),
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("CLAUDE_CONFIG_DIR", None)
    child = subprocess.run(
        [sys.executable, "-c", _CHILD, str(BIN), workload, mode],
        env=env, capture_output=True, text=True, timeout=50)
    assert child.returncode == 0, child.stderr[-4000:]
    result = json.loads(child.stdout.strip().splitlines()[-1])
    result["temp_dir_touched"] = (
        os.stat(watched).st_mtime_ns != UNTOUCHED_NS)
    result["temp_dir_entries"] = sorted(os.listdir(watched))
    return result


@pytest.mark.parametrize("workload", sorted(WORKLOADS))
def test_a_sync_pass_and_an_ingest_write_no_temp_file(
    ns, tmp_path, monkeypatch, workload,
):
    codex = _build(ns, tmp_path, monkeypatch, workload)
    product = _run_child(tmp_path, codex, workload, "product")
    control = _run_child(tmp_path, codex, workload, "control")
    statement = WORKLOADS[workload][0]
    assert control["temp_dir_touched"] and control["first_spill"], (
        "non-vacuity: with the policy disabled the fixture must spill", control)
    assert control["first_spill"][0].startswith(statement), (
        "the control must spill in the statement the fixture targets",
        control["first_spill"])
    assert product["first_spill"] == [] and not product["temp_dir_touched"], (
        "the product pass created a SQLite temp file", product)
    assert product["temp_dir_entries"] == [], product
    assert product["status"] == control["status"], (product, control)
    assert product["frames"] == control["frames"] > 0, (
        "the policy must change where statement journals live and nothing "
        "else", product, control)
