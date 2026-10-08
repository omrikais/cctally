"""#901 G10 (i)-(k): writer-owned checkpoints made effective (Q14).

Spec ``docs/superpowers/specs/2026-10-03-901-dashboard-disk-writes.md`` §4.9
("Connection lifetimes and open-time writes"), §5.3c W9 and §6.2 G10 (i), (j)
and (k). Revision 12's deferral never spanned a pass: the dashboard opens and
closes its sync connections every pass and tick, each close was the store's
last, and SQLite's close-time checkpoint copied and reset the WAL (§1.8). The
policy module now holds one idle keeper connection per armed store for the
process's life, a daemon timer thread that makes the timed attempt through it,
a finalizer at orderly exit, and an at-fork rule.

Every store lives under ``tmp_path``. Child processes are fresh interpreters
started with ``subprocess``; only the hook-worker driver loads ``bin/cctally``.

RED proof (spec G10, on ``d2aa86843``): (i), (j)'s caught-up checkpoint and
(k)'s hook-worker exit attempt with another keeper open fail; (k)'s other
lifecycle cases and the seam control are correctness guards.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from conftest import load_script, redirect_paths_without_conversation_retention
from test_901_sync_transactions import (
    FakeClock,
    _claude_line,
    _lines,
    _main_file_digest,
    _session_meta,
    _shm_backfill,
    _turn,
    _wal_salt,
)

BIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

STORES = ("cache.db", "conversations.db")


# ── shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def w9(monkeypatch):
    """The W9 module with a fresh per-process state and an injected clock.

    The timer thread's interval is long unless a test shortens it before its
    first arm, so tests that drive boundaries are never raced by the thread.
    """
    monkeypatch.delenv("CCTALLY_TEST_W9_DISABLE", raising=False)
    import _lib_wal_checkpoint as wal

    clock = FakeClock()
    monkeypatch.setattr(wal, "CLOCK", clock)
    monkeypatch.setattr(wal, "TIMER_INTERVAL_SECONDS", 3600.0, raising=False)
    wal.reset_policies()
    yield wal, clock
    wal.reset_policies()


class PassiveSpy:
    """Every PASSIVE attempt this process makes: (main path, result row)."""

    def __init__(self, wal, monkeypatch) -> None:
        self.calls: list[tuple[str, "tuple | None"]] = []
        self.lock = threading.Lock()
        real = wal.run_passive_checkpoint

        def spy(conn):
            path = _main_path(conn)
            row = real(conn)
            with self.lock:
                self.calls.append((path, row))
            return row

        monkeypatch.setattr(wal, "run_passive_checkpoint", spy)

    def for_path(self, path) -> list["tuple | None"]:
        key = os.path.realpath(path)
        with self.lock:
            return [row for p, row in self.calls if p == key]


def _main_path(conn) -> str:
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "main":
            return os.path.realpath(path)
    return ""


def _incomplete(row) -> bool:
    return (row is not None and int(row[0]) == 0
            and 0 <= int(row[2]) < int(row[1]))


def _complete(row) -> bool:
    return (row is not None and int(row[0]) == 0
            and int(row[1]) == int(row[2]) >= 0)


def _retained(path) -> bool:
    max_frame, n_backfill = _shm_backfill(path)
    return max_frame > n_backfill


def _drained(path) -> bool:
    wal_file = pathlib.Path(f"{path}-wal")
    return not wal_file.exists() or wal_file.stat().st_size == 0


def _wait(predicate, *, seconds: float = 15.0) -> bool:
    """Poll an observable state; True as soon as it holds."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _idle(path) -> sqlite3.Connection:
    """A connection that has read the store once and holds no transaction,
    as the dashboard's other long-lived connections do."""
    conn = sqlite3.connect(path)
    conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchall()
    assert not conn.in_transaction
    return conn


def _pinned(path) -> sqlite3.Connection:
    """A reader holding a read snapshot, so no checkpoint can complete."""
    conn = sqlite3.connect(path)
    conn.execute("BEGIN")
    conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchall()
    return conn


def _child_env() -> dict:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("CCTALLY_TEST_W9_DISABLE", None)
    return env


def _log_entries(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# ── the product's own open–sync–close cycles ────────────────────────────────


class Product:
    """One tick's ``sync_codex_cache`` on a fresh ``cache.db`` connection and
    one conversation pass's ``sync_codex_conversations`` on a fresh
    ``conversations.db`` connection, each opened through the product's full
    writable opener and closed afterwards, as the dashboard does
    (``bin/_cctally_tui.py`` ~5042-5104, ``bin/_cctally_dashboard.py``
    ~2742-2819)."""

    def __init__(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        self.ns = load_script()
        self.home = tmp_path / "home"
        redirect_paths_without_conversation_retention(
            self.ns, monkeypatch, self.home)
        self.share = self.home / ".local" / "share" / "cctally"
        codex = tmp_path / "codex"
        self.sessions = codex / "sessions" / "2026" / "07" / "20"
        self.sessions.mkdir(parents=True)
        monkeypatch.setenv("CODEX_HOME", str(codex))
        import _cctally_cache as cache

        self.cache = cache
        self.rollout = self.sessions / "rollout-keeper.jsonl"
        self.turns = 0

    def stores(self) -> dict[str, pathlib.Path]:
        return {name: self.share / name for name in STORES}

    def write(self, turns: int = 1) -> None:
        records = [] if self.rollout.exists() else [
            _session_meta("keeper", 0)]
        for _ in range(turns):
            records.extend(_turn("keeper", self.turns, 1 + self.turns * 10))
            self.turns += 1
        with self.rollout.open("a", encoding="utf-8") as fh:
            fh.write(_lines(records))

    def tick(self) -> None:
        conn = self.ns["open_cache_db"]()
        try:
            self.cache.sync_codex_cache(conn)
        finally:
            conn.close()

    def conversation_pass(self) -> None:
        conn = self.ns["open_conversations_db"]()
        try:
            self.cache.sync_codex_conversations(conn)
        finally:
            conn.close()

    def cycle(self) -> None:
        self.tick()
        self.conversation_pass()

    def state(self) -> dict[str, tuple]:
        """Per store: the WAL generation (its salt) and the main file bytes."""
        return {
            name: (_wal_salt(path), _main_file_digest(path))
            for name, path in self.stores().items()
        }


@pytest.fixture
def product(tmp_path, monkeypatch):
    return Product(tmp_path, monkeypatch)


# ── (i) close cycles ─────────────────────────────────────────────────────────


def test_i_ingesting_passes_keep_one_wal_generation_until_the_timer(
    product, w9, monkeypatch,
):
    """Three consecutive open–sync–close passes that each ingest new data
    leave each store's WAL in one generation and its main file unwritten;
    once the clock passes 60 s, one PASSIVE per store copies the union."""
    wal, clock = w9
    passive = PassiveSpy(wal, monkeypatch)
    product.write(2)
    product.cycle()  # creates and arms both stores
    product.write()
    product.cycle()  # settles what a first pass legitimately completes
    baseline = product.state()
    assert all(salt is not None for salt, _main in baseline.values()), baseline
    for _ in range(3):
        clock.advance(10)
        product.write()
        product.cycle()
        assert product.state() == baseline, (
            "a pass's close checkpointed or reset a WAL")
    assert passive.calls == []
    for name, path in product.stores().items():
        assert _retained(path), name

    clock.advance(30)  # 60 s since the arm, and no attempt has run
    product.write()
    product.cycle()
    for name, path in product.stores().items():
        # One completed PASSIVE copied the union of the passes' frames; a
        # later write in the same cycle may start the next WAL generation.
        rows = passive.for_path(path)
        assert len(rows) == 1 and _complete(rows[0]), (name, rows)
        assert int(rows[0][1]) > 0, (name, rows)
        assert _main_file_digest(path) != baseline[name][1], name


def test_i_with_the_seam_every_close_checkpoints(product, monkeypatch):
    """The control: with W9 disabled (revision 12's close behaviour), every
    pass's close copies its frames into the main file."""
    monkeypatch.setenv("CCTALLY_TEST_W9_DISABLE", "1")
    product.write(2)
    product.cycle()
    product.write()
    product.cycle()
    for _ in range(3):
        before = product.state()
        product.write()
        product.cycle()
        after = product.state()
        for name in STORES:
            assert after[name][1] != before[name][1], (
                f"{name}: the pass's close did not checkpoint")


# ── (j) retained frames at caught-up iterations ──────────────────────────────


_FOREIGN_WRITER = r'''
import json, os, sqlite3, sys

bin_dir, db, log = sys.argv[1:4]
sys.path.insert(0, bin_dir)
import _lib_wal_checkpoint as wal

real = wal.run_passive_checkpoint


def spy(conn):
    row = real(conn)
    with open(log, "a") as fh:
        fh.write(json.dumps({"pid": os.getpid(),
                             "row": list(row) if row is not None else None}) + "\n")
    return row


wal.run_passive_checkpoint = spy
conn = sqlite3.connect(db)
assert wal.arm(conn)
conn.execute(
    "INSERT OR REPLACE INTO cache_meta(key, value) VALUES ('g10-j-foreign', ?)",
    ("x" * 200000,))
conn.commit()
wal.after_commit(conn)
conn.close()
'''


def _foreign_writer(path: pathlib.Path, log: pathlib.Path) -> list[dict]:
    """A separate writer process commits frames and exits in order."""
    child = subprocess.run(
        [sys.executable, "-c", _FOREIGN_WRITER, str(BIN_DIR), str(path),
         str(log)],
        env=_child_env(), capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr[-4000:]
    return _log_entries(log)


class _Hub:
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, snapshot) -> None:
        self.published.append(snapshot)


def _caught_up_iterations(product: Product, monkeypatch):
    """The product's own dashboard iterations with both providers caught up:
    the conversation-sync loop's pass and the tick's accounting operation."""
    import datetime as dt

    ns = product.ns
    dashboard = sys.modules["_cctally_dashboard"]
    frontier_mod = ns["_load_sibling"]("_lib_ingest_frontier")

    class _Plan:
        mode = "caught_up"
        paths = frozenset()

    class _ConversationFrontierModule:
        @staticmethod
        def conversation_sync_certifiable(mode, stats, **_kwargs):
            return mode == "caught_up" and stats is None

    class _ConversationFrontier:
        def commit_provider(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(
        dashboard, "_conversation_frontier_plans",
        lambda _conn: (
            _ConversationFrontierModule(), _ConversationFrontier(), object(),
            {"claude": ((), (), True), "codex": ((), (), True)},
            {"claude": _Plan(), "codex": _Plan()},
        ))
    monkeypatch.setattr(
        frontier_mod.DashboardIngestFrontier, "plan_provider",
        lambda self, provider, _conn, **_kwargs: frontier_mod.FrontierPlan(
            provider, "caught_up"))
    monkeypatch.setattr(
        frontier_mod.DashboardIngestFrontier, "commit_provider",
        lambda self, *_args, **_kwargs: None)
    monkeypatch.setitem(
        ns, "_tui_build_snapshot",
        lambda **_kwargs: ns["_empty_dashboard_snapshot"]())
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=_Hub(),
        pinned_now=dt.datetime(2026, 7, 20, 12, tzinfo=dt.timezone.utc),
        display_tz_pref_override="utc")

    def iterate() -> None:
        status = dashboard._conversation_sync_pass()
        assert status == "ok", status
        assert status.modes == {"claude": "caught_up", "codex": "caught_up"}
        locked(skip_sync=False)

    return iterate


def _record_calls(wal, monkeypatch, *names: str) -> list[str]:
    """Record every call the product makes into the named policy functions."""
    calls: list[str] = []
    for name in names:
        real = getattr(wal, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(wal, name, spy)
    return calls


def _warm(product: Product, wal, monkeypatch, store: str):
    """Arm both stores in this process (their keepers open) and hold one idle
    connection to the store under test, as the dashboard's readers do, so an
    iteration's close is never the store's last."""
    monkeypatch.setattr(wal, "TIMER_INTERVAL_SECONDS", 0.02, raising=False)
    product.write(2)
    product.cycle()
    product.write()
    product.cycle()
    return product.stores()[store], _idle(product.stores()[store])


@pytest.mark.parametrize("store", STORES)
def test_j_caught_up_iterations_copy_a_foreign_writers_retained_frames(
    product, w9, monkeypatch, tmp_path, store,
):
    wal, clock = w9
    path, observer = _warm(product, wal, monkeypatch, store)
    reader = _pinned(path)
    try:
        exits = _foreign_writer(path, tmp_path / "foreign.log")
        assert len(exits) == 1, (
            "the foreign writer's orderly exit attempts one PASSIVE", exits)
        assert _incomplete(exits[0]["row"]), (
            "the pinned reader blocks that attempt", exits)
    finally:
        reader.rollback()
        reader.close()
    assert _retained(path), "the foreign frames wait in the WAL"

    iterate = _caught_up_iterations(product, monkeypatch)
    policy_calls = _record_calls(wal, monkeypatch, "arm", "after_commit",
                                 "at_boundary")
    clock.advance(30)
    for _ in range(2):
        iterate()
    assert policy_calls == [], "a caught-up iteration skips every sync"
    assert _retained(path), "nothing is due 30 s after the arm"
    try:
        clock.advance(31)
        assert _wait(lambda: not _retained(path)), (
            "no completed PASSIVE copied the frames once 60 s had passed",
            _shm_backfill(path))
        for _ in range(2):
            iterate()
        assert policy_calls == []
    finally:
        observer.close()


@pytest.mark.parametrize("store", STORES)
def test_j_a_pinned_reader_leaves_the_timed_attempt_incomplete_until_a_retry(
    product, w9, monkeypatch, tmp_path, store,
):
    wal, clock = w9
    path, observer = _warm(product, wal, monkeypatch, store)
    passive = PassiveSpy(wal, monkeypatch)
    reader = _pinned(path)
    try:
        _foreign_writer(path, tmp_path / "foreign.log")
        iterate = _caught_up_iterations(product, monkeypatch)
        iterate()
        clock.advance(60)
        assert _wait(lambda: len(passive.for_path(path)) == 1), (
            "the timer made no attempt once 60 s had passed")
        assert _incomplete(passive.for_path(path)[0])
        clock.advance(30)
        iterate()
        time.sleep(0.3)  # many timer rounds, none of them due
        assert len(passive.for_path(path)) == 1, "no retry sooner than 60 s"
        clock.advance(30)
        assert _wait(lambda: len(passive.for_path(path)) == 2), (
            "the next due attempt did not retry")
        assert _incomplete(passive.for_path(path)[1])
        assert _retained(path)
    finally:
        reader.rollback()
        reader.close()
    try:
        clock.advance(60)
        assert _wait(lambda: not _retained(path))
        assert _complete(passive.for_path(path)[-1])
    finally:
        observer.close()


# ── (k) keeper lifecycle ─────────────────────────────────────────────────────


def _wal_store(path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS rows(id INTEGER PRIMARY KEY, b BLOB)")
    conn.commit()
    return conn


def _fill(conn: sqlite3.Connection, rows: int, size: int = 8192) -> None:
    conn.executemany("INSERT INTO rows(b) VALUES (randomblob(?))",
                     [(size,)] * rows)
    conn.commit()


def _count(path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0])
    finally:
        conn.close()


def _identity(path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_dev, st.st_ino


def test_k_the_keeper_holds_no_read_transaction(w9, tmp_path):
    wal, _clock = w9
    db = tmp_path / "keeper.db"
    writer = _wal_store(db)
    assert wal.arm(writer)
    _fill(writer, 8)
    writer.close()
    assert wal.kept_stores() == {os.path.realpath(db): _identity(db)}
    other = sqlite3.connect(db)
    try:
        busy, _log, _ckpt = other.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert busy == 0, "the keeper pinned a frame"
        assert pathlib.Path(f"{db}-wal").stat().st_size == 0
    finally:
        other.close()
    assert _count(db) == 8


def test_k_a_replaced_store_gets_a_new_keeper_and_the_old_family_is_untouched(
    w9, tmp_path,
):
    """Store recovery moves the whole family aside and creates a new one;
    the next arm opens a keeper on the new file, and closing the old keeper
    neither copies into the moved family nor removes the new store's WAL."""
    wal, _clock = w9
    db = tmp_path / "store.db"
    writer = _wal_store(db)
    assert wal.arm(writer)
    _fill(writer, 4)
    writer.close()
    old_identity = _identity(db)
    assert wal.kept_stores()[os.path.realpath(db)] == old_identity

    moved = tmp_path / "quarantine"
    moved.mkdir()
    for suffix in ("", "-wal", "-shm"):
        os.rename(f"{db}{suffix}", moved / f"store.db{suffix}")
    moved_bytes = {
        suffix: hashlib.sha256((moved / f"store.db{suffix}").read_bytes()).hexdigest()
        for suffix in ("", "-wal")
    }
    replacement = _wal_store(db)
    replacement.execute("PRAGMA wal_autocheckpoint = 0")
    _fill(replacement, 3)
    assert wal.arm(replacement)
    assert wal.kept_stores()[os.path.realpath(db)] == _identity(db) != old_identity
    replacement.close()

    import _cctally_db
    holders = _cctally_db._db_family_open_pids(moved / "store.db")
    assert holders is not None and os.getpid() not in holders, (
        "a keeper stayed on the replaced inode")
    for suffix, digest in moved_bytes.items():
        assert hashlib.sha256(
            (moved / f"store.db{suffix}").read_bytes()).hexdigest() == digest, (
                f"closing the old keeper wrote store.db{suffix}")
    assert pathlib.Path(f"{db}-wal").exists(), (
        "closing the old keeper removed the new store's WAL")
    assert _count(db) == 3


_EXITING_WRITER = r'''
import json, os, sqlite3, sys

bin_dir, log, mode, *dbs = sys.argv[1:]
sys.path.insert(0, bin_dir)
import _lib_wal_checkpoint as wal

real = wal.run_passive_checkpoint


def spy(conn):
    row = real(conn)
    path = [p for _s, n, p in conn.execute("PRAGMA database_list") if n == "main"][0]
    with open(log, "a") as fh:
        fh.write(json.dumps({"path": os.path.realpath(path),
                             "row": list(row) if row is not None else None}) + "\n")
    return row


wal.run_passive_checkpoint = spy
writers = []
for db in dbs:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS rows(id INTEGER PRIMARY KEY, b BLOB)")
    conn.commit()
    assert wal.arm(conn)
    conn.executemany("INSERT INTO rows(b) VALUES (randomblob(8192))", [()] * 16)
    conn.commit()
    wal.after_commit(conn)
    writers.append(conn)
if mode == "release":
    # A drain check releases the first store's keeper; its writer stays open
    # until the interpreter exits, after the finalizer.
    wal.release(dbs[0])
else:
    for conn in writers:
        conn.close()
if mode == "kill":
    print("ready", flush=True)
    sys.stdin.read()
'''


def test_k_an_orderly_exit_attempts_one_passive_per_store_and_drains(tmp_path):
    dbs = [tmp_path / "one.db", tmp_path / "two.db"]
    log = tmp_path / "exit.log"
    child = subprocess.run(
        [sys.executable, "-c", _EXITING_WRITER, str(BIN_DIR), str(log),
         "exit", *map(str, dbs)],
        env=_child_env(), capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr[-4000:]
    entries = _log_entries(log)
    assert sorted(e["path"] for e in entries) == sorted(
        os.path.realpath(db) for db in dbs), entries
    assert all(_complete(e["row"]) for e in entries), entries
    for db in dbs:
        assert _drained(db), f"{db.name}: the last keeper close did not drain"
        assert _count(db) == 16


def test_k_an_orderly_exit_attempts_a_store_whose_keeper_was_released(
        tmp_path):
    """#901 Amendment 19 PR-7. The finalizer attempted only the stores that
    still had a keeper, so a store armed earlier whose keeper a drain check
    released (or a fence made yield) got no PASSIVE at exit. It now attempts
    every store the process armed: the released one through a short-lived
    connection, the kept one through its keeper, one PASSIVE each, both
    complete."""
    dbs = [tmp_path / "released.db", tmp_path / "kept.db"]
    log = tmp_path / "exit.log"
    child = subprocess.run(
        [sys.executable, "-c", _EXITING_WRITER, str(BIN_DIR), str(log),
         "release", *map(str, dbs)],
        env=_child_env(), capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr[-4000:]
    entries = _log_entries(log)
    assert sorted(e["path"] for e in entries) == sorted(
        os.path.realpath(db) for db in dbs), entries
    assert all(_complete(e["row"]) for e in entries), entries
    for db in dbs:
        assert _count(db) == 16


def test_k_a_sigkilled_writer_leaves_its_frames_for_the_next_opener(tmp_path):
    db = tmp_path / "killed.db"
    log = tmp_path / "kill.log"
    child = subprocess.Popen(
        [sys.executable, "-c", _EXITING_WRITER, str(BIN_DIR), str(log),
         "kill", str(db)],
        env=_child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        assert pathlib.Path(f"{db}-wal").stat().st_size > 0
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()
    assert child.returncode == -signal.SIGKILL
    assert _log_entries(log) == [], "a killed process attempts nothing"
    assert pathlib.Path(f"{db}-wal").stat().st_size > 0, (
        "the committed frames stay in the WAL")
    assert _count(db) == 16, "the next opener recovers every row"


_FORKING_WRITER = r'''
import json, os, sqlite3, sys, threading

bin_dir, parent_db, child_db = sys.argv[1:4]
sys.path.insert(0, bin_dir)
import _lib_wal_checkpoint as wal


def store(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS rows(id INTEGER PRIMARY KEY, b BLOB)")
    conn.execute("INSERT INTO rows(b) VALUES (randomblob(8192))")
    conn.commit()
    return conn


parent = store(parent_db)
assert wal.arm(parent)
parent.close()
before = sorted(wal.kept_stores())
pid = os.fork()
if pid == 0:
    report = {"inherited": sorted(wal.kept_stores()),
              "timer": wal.timer_thread() is not None}
    conn = store(child_db)
    report["armed"] = wal.arm(conn)
    conn.close()
    report["own"] = sorted(wal.kept_stores())
    report["own_timer"] = (wal.timer_thread() is not None
                           and wal.timer_thread().is_alive())
    wal.finalize()
    report["after"] = sorted(wal.kept_stores())
    with open(child_db + ".json", "w") as fh:
        json.dump(report, fh)
    os._exit(0)
_pid, status = os.waitpid(pid, 0)
print(json.dumps({"status": status, "before": before,
                  "kept": sorted(wal.kept_stores()),
                  "suspended": sorted(wal.suspended_stores()),
                  "timer": wal.timer_thread() is not None
                  and wal.timer_thread().is_alive()}))
'''


def test_k_a_forked_child_starts_with_no_keeper_and_arms_its_own(tmp_path):
    parent_db, child_db = tmp_path / "parent.db", tmp_path / "child.db"
    run = subprocess.run(
        [sys.executable, "-c", _FORKING_WRITER, str(BIN_DIR), str(parent_db),
         str(child_db)],
        env=_child_env(), capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-4000:]
    parent = json.loads(run.stdout.strip().splitlines()[-1])
    child = json.loads(pathlib.Path(f"{child_db}.json").read_text())
    assert parent["status"] == 0
    assert parent["before"] == [os.path.realpath(parent_db)]
    # PR-6: the parent released its keeper before the fork (no keeper
    # connection crosses one); the store is suspended until its next arm.
    assert parent["kept"] == []
    assert parent["suspended"] == [os.path.realpath(parent_db)]
    assert parent["timer"], "the parent's timer thread survived the fork"
    assert child["inherited"] == [] and child["timer"] is False
    assert child["armed"] and child["own"] == [os.path.realpath(child_db)]
    assert child["own_timer"] and child["after"] == []
    assert _drained(parent_db), "the parent's own exit drained its store"
    assert _drained(child_db)


_HOOK_DRIVER = r'''
import json, os, sys

bin_dir, log = sys.argv[1:3]
sys.path.insert(0, bin_dir)
import _lib_wal_checkpoint as wal


def record(event, conn, **extra):
    path = None
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            path = os.path.realpath(file)
    entry = dict(event=event, pid=os.getpid(), path=path, **extra)
    with open(log, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


real_arm, real_run = wal.arm, wal.run_passive_checkpoint


def arm(conn):
    armed = real_arm(conn)
    record("arm", conn, armed=armed)
    return armed


def run(conn):
    row = real_run(conn)
    record("passive", conn, row=list(row) if row is not None else None)
    return row


wal.arm = arm
wal.run_passive_checkpoint = run

import runpy

sys.argv = ["cctally", "hook-tick", "--no-oauth"]
runpy.run_path(os.path.join(bin_dir, "cctally"), run_name="__main__")
'''


def _hook_home(tmp_path: pathlib.Path, monkeypatch):
    ns = load_script()
    redirect_paths_without_conversation_retention(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects" / "-synthetic-h1-hook"
    projects.mkdir(parents=True)
    (projects / "session-h1.jsonl").write_text(
        "".join(_claude_line("session-h1", n, f"hook line {n}")
                for n in range(6)),
        encoding="utf-8")
    return ns, tmp_path / ".local" / "share" / "cctally" / "cache.db"


def _run_hook_worker(tmp_path: pathlib.Path) -> tuple[int, list[dict]]:
    """Run the real background ``hook-tick``: the parent returns at the fork
    and the detached child syncs, logs and ends with ``os._exit(0)``."""
    log = tmp_path / "hook-driver.log"
    env = _child_env()
    env["CCTALLY_DISABLE_RETENTION_SWEEP"] = "1"
    env["CCTALLY_DISABLE_TELEMETRY"] = "1"
    parent = subprocess.run(
        [sys.executable, "-c", _HOOK_DRIVER, str(BIN_DIR), str(log)],
        input=json.dumps({"hook_event_name": "Stop", "session_id": "h1",
                          "transcript_path": ""}),
        env=env, capture_output=True, text=True, timeout=60)
    assert parent.returncode == 0, parent.stderr[-4000:]
    # Only the forked worker syncs: the parent returns at the fork.
    assert _wait(lambda: any(e["event"] == "arm" for e in _log_entries(log)),
                 seconds=60.0), ("the hook worker never synced",
                                 _log_entries(log))
    child = next(e["pid"] for e in _log_entries(log) if e["event"] == "arm")

    def exited() -> bool:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return True
        return False

    assert _wait(exited, seconds=60.0), "the hook worker did not exit"
    return child, _log_entries(log)


def test_k_the_hook_worker_drains_the_wal_when_it_is_alone(
    tmp_path, monkeypatch,
):
    _ns, cache_db = _hook_home(tmp_path, monkeypatch)
    child, _entries = _run_hook_worker(tmp_path)
    assert child != os.getpid()
    assert _drained(cache_db), "the hook worker left frames behind"
    conn = sqlite3.connect(cache_db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM session_entries").fetchone()[0] == 6
    finally:
        conn.close()


def test_k_the_hook_worker_attempts_one_passive_with_another_keeper_open(
    tmp_path, monkeypatch,
):
    """Another process's keeper is open and a reader is pinned: the worker's
    exit attempt is one PASSIVE, blocked, and its frames stay retained."""
    import _lib_wal_checkpoint as wal

    monkeypatch.delenv("CCTALLY_TEST_W9_DISABLE", raising=False)
    monkeypatch.setattr(wal, "TIMER_INTERVAL_SECONDS", 3600.0, raising=False)
    wal.reset_policies()
    ns, cache_db = _hook_home(tmp_path, monkeypatch)
    keeper_owner = ns["open_cache_db"]()
    assert wal.arm(keeper_owner)
    keeper_owner.close()
    reader = _pinned(cache_db)
    try:
        child, entries = _run_hook_worker(tmp_path)
        exits = [e for e in entries
                 if e["event"] == "passive" and e["pid"] == child
                 and e["path"] == os.path.realpath(cache_db)]
        assert len(exits) == 1, ("the worker's exit attempted no PASSIVE",
                                 entries)
        max_frame, n_backfill = _shm_backfill(cache_db)
        assert _incomplete(exits[0]["row"]), (exits, max_frame, n_backfill)
        assert max_frame > n_backfill, (
            f"retained frames: {max_frame - n_backfill}")
    finally:
        reader.rollback()
        reader.close()
        wal.reset_policies()
