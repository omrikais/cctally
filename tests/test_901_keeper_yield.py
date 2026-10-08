"""#901 G10 (m): keepers yield to exclusive maintenance (Q15).

Spec ``docs/superpowers/specs/2026-10-03-901-dashboard-disk-writes.md`` §4.9
("Exclusive maintenance and handle-drain checks (Q15)"), §5.3c W9 (Keeper,
Yield, Drain grace, Close control) and §6.2 G10 (m). Revision 13's idle keeper
keeps its shared lock on the database file, as every WAL-mode connection does
between transactions, so `db vacuum`'s exclusive acquisition and every
cross-process handle-drain check refused for as long as any process held one
(§1.9). Revision 14 makes a keeper yield to an explicit, owner-qualified
request (``<store>.keeper-yield``), a live repair marker or a pending
quarantine, with a silent close and no checkpoint, and admits no keeper where
the no-checkpoint-on-close control cannot be set.

Every store lives under ``tmp_path``; child processes are fresh interpreters
started with ``subprocess`` and are always reaped.

RED proof (spec G10 (m), on ``b31533236``): a live fence does not stop `arm`
from admitting a keeper, the timer round's PASSIVE or the finalizer's exit
attempt; a runtime whose close control cannot be set still admits a keeper and
reads the store; and, with a separate live process holding an idle keeper,
`db vacuum --db <store>` from a third process fails "in use" while the
cross-process recoveries, the rebuild probe and the pending-quarantine resumes
refuse "still open in process(es)". The rest are correctness guards.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest

from conftest import load_script, redirect_paths_without_conversation_retention
from test_901_sync_transactions import _wal_salt
from test_901_wal_keeper import (  # noqa: F401 — `w9` is a fixture
    PassiveSpy,
    Product,
    _caught_up_iterations,
    _count,
    _drained,
    _fill,
    _identity,
    _idle,
    _retained,
    _wait,
    _wal_store,
    _warm,
    w9,
)

BIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _cctally_db  # noqa: E402

STORES = ("cache.db", "conversations.db")
FENCES = ("request", "repair", "quarantine")
#: The stable marker every keeper yield writes to stderr (W9, P's yield line).
YIELD_LINE = "[w9] keeper-yield"


# ── fences ───────────────────────────────────────────────────────────────────


def _request_path(db: pathlib.Path) -> pathlib.Path:
    """``<store>.keeper-yield``, spelled here so the RED run on the unchanged
    module needs no helper that does not exist yet."""
    return db.with_name(f"{db.name}.keeper-yield")


def _owner_record(pid: int, process_start: str) -> str:
    return _cctally_db._encode_repair_owner(
        pid=pid, process_start=process_start, claim_id=uuid.uuid4().hex)


def _live_owner() -> str:
    start = _cctally_db._process_start_identity(os.getpid())
    assert start, "this platform reports no process start identity"
    return _owner_record(os.getpid(), start)


def _fence_path(kind: str, db: pathlib.Path) -> pathlib.Path:
    if kind == "request":
        return _request_path(db)
    if kind == "repair":
        return _cctally_db._repair_marker_path(db)
    return _cctally_db._quarantine_pending_path(db)


def _publish(kind: str, db: pathlib.Path, owner: "str | None" = None) -> None:
    """A live fence of ``kind`` owned by this (live) process."""
    path = _fence_path(kind, db)
    if kind == "quarantine":
        path.write_text(json.dumps({"schemaVersion": 1,
                                    "originalPath": str(db)}) + "\n")
    else:
        path.write_text(owner if owner is not None else _live_owner())


def _withdraw(kind: str, db: pathlib.Path) -> None:
    _fence_path(kind, db).unlink()


def _family(db) -> dict[str, "str | None"]:
    """The family's three files, hashed (None for an absent one)."""
    out = {}
    for suffix in ("", "-wal", "-shm"):
        member = pathlib.Path(f"{db}{suffix}")
        out[suffix or "main"] = (
            hashlib.sha256(member.read_bytes()).hexdigest()
            if member.exists() else None)
    return out


def _kept_store(w9, tmp_path, name: str = "store.db"):
    """A WAL store armed in this process (keeper open) with retained frames
    and no other connection open."""
    wal, _clock = w9
    db = tmp_path / name
    writer = _wal_store(db)
    assert wal.arm(writer)
    _fill(writer, 8)
    wal.after_commit(writer)
    writer.close()
    assert wal.kept_stores() == {os.path.realpath(db): _identity(db)}
    assert _retained(db), "the fixture must leave frames in the WAL"
    return db


def _yield_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if YIELD_LINE in line]


# ── RED: a live fence suppresses admission, the timer and the exit attempt ───


@pytest.mark.parametrize("fence", FENCES)
def test_m_a_live_fence_suppresses_keeper_admission_at_arm(
    w9, tmp_path, fence,
):
    wal, _clock = w9
    db = tmp_path / "store.db"
    writer = _wal_store(db)
    try:
        _publish(fence, db)
        wal.arm(writer)
        assert wal.kept_stores() == {}, (
            f"a keeper was admitted under a live {fence} fence")
    finally:
        writer.close()


@pytest.mark.parametrize("fence", FENCES)
def test_m_a_live_fence_suppresses_the_timer_rounds_checkpoint(
    w9, tmp_path, monkeypatch, capsys, fence,
):
    wal, clock = w9
    db = _kept_store(w9, tmp_path)
    passive = PassiveSpy(wal, monkeypatch)
    _publish(fence, db)
    before = _family(db)
    clock.advance(61)
    wal._timer_round()
    assert passive.for_path(db) == [], (
        f"the timer round checkpointed under a live {fence} fence")
    assert wal.kept_stores() == {}, "the keeper did not yield"
    assert _family(db) == before, "the yield path wrote the family"
    assert _retained(db)
    lines = _yield_lines(capsys.readouterr().err)
    assert len(lines) == 1 and "store.db" in lines[0], lines


@pytest.mark.parametrize("fence", FENCES)
def test_m_a_live_fence_suppresses_the_exit_attempt(
    w9, tmp_path, monkeypatch, fence,
):
    wal, _clock = w9
    db = _kept_store(w9, tmp_path)
    passive = PassiveSpy(wal, monkeypatch)
    _publish(fence, db)
    before = _family(db)
    wal.finalize()
    assert passive.for_path(db) == [], (
        f"the exit attempt checkpointed under a live {fence} fence")
    assert wal.kept_stores() == {}
    assert _family(db) == before, "the exit path wrote the family"
    _withdraw(fence, db)
    assert _count(db) == 8, "the next opener recovers every row"


def _released_store(w9, tmp_path):
    """An armed store whose keeper was released (PR-7's unkept store), with
    retained frames and no connection open."""
    wal, _clock = w9
    db = _kept_store(w9, tmp_path)
    wal.release(db)
    assert wal.kept_stores() == {}
    assert _retained(db)
    return db


def _replace_store(db: pathlib.Path, aside: pathlib.Path) -> None:
    """Replace the main file's inode (a recovery or `db recover`), keeping
    the old one reachable through ``aside`` and the WAL family by name."""
    os.link(db, aside)
    fresh = db.with_name(f"{db.name}.fresh")
    conn = sqlite3.connect(fresh)
    conn.execute("CREATE TABLE other(x)")
    conn.commit()
    conn.close()
    os.replace(fresh, db)


def _old_family(db: pathlib.Path, aside: pathlib.Path) -> dict:
    family = _family(db)
    family["main"] = hashlib.sha256(aside.read_bytes()).hexdigest()
    return family


@pytest.mark.parametrize("change", FENCES + ("replacement",))
def test_m_the_unkept_exit_attempt_rechecks_after_it_connects(
    w9, tmp_path, monkeypatch, change,
):
    """901-RW-003. The finalizer's attempt for an armed store with no keeper
    checked the store's identity and the fences only before it connected:
    a fence or a replacement that landed while it connected still got a
    PASSIVE, and a fenced store a checkpointing close. It now rechecks both
    after connecting and refuses with a silent close."""
    wal, _clock = w9
    db = _released_store(w9, tmp_path)
    aside = tmp_path / "old-main.db"
    passive = PassiveSpy(wal, monkeypatch)
    real_connect = wal._connect_keeper
    at_change: list[dict] = []

    def connect_then_change(path):
        conn = real_connect(path)
        if change == "replacement":
            _replace_store(db, aside)
            at_change.append(_old_family(db, aside))
        else:
            _publish(change, db)
            at_change.append(_family(db))
        return conn

    monkeypatch.setattr(wal, "_connect_keeper", connect_then_change)
    wal.finalize()
    assert at_change, "the finalizer did not connect to the unkept store"
    assert passive.for_path(db) == [], (
        f"the exit attempt checkpointed after a {change} landed")
    after = (_old_family(db, aside) if change == "replacement"
             else _family(db))
    assert after == at_change[0], "the refusal path wrote the WAL family"
    if change == "replacement":
        # The old main file with the WAL it was written with, copied out.
        old = tmp_path / "old-family"
        old.mkdir()
        (old / "store.db").write_bytes(aside.read_bytes())
        (old / "store.db-wal").write_bytes(
            pathlib.Path(f"{db}-wal").read_bytes())
        assert _count(old / "store.db") == 8, (
            "the old family recovers every row")
    else:
        _withdraw(change, db)
        assert _count(db) == 8, "the next opener recovers every row"


def test_m_a_fence_during_the_unkept_exit_attempt_closes_without_a_checkpoint(
    w9, tmp_path, monkeypatch,
):
    """901-RW-003. A fence published while the unkept store's PASSIVE runs
    must not be followed by a checkpointing close: the WAL stays in place."""
    wal, _clock = w9
    db = _released_store(w9, tmp_path)
    real = wal.run_passive_checkpoint

    def passive_then_fence(conn):
        row = real(conn)
        _publish("request", db)
        return row

    monkeypatch.setattr(wal, "run_passive_checkpoint", passive_then_fence)
    wal.finalize()
    wal_file = pathlib.Path(f"{db}-wal")
    assert wal_file.exists() and wal_file.stat().st_size > 0, (
        "the close after a fence ran SQLite's close-time checkpoint")
    _withdraw("request", db)
    assert _count(db) == 8


def test_m_the_unkept_exit_attempt_refuses_without_the_close_control(
    w9, tmp_path, monkeypatch,
):
    """901-RW-003 / OV-4. The unkept store's exit attempt swallowed a failure
    to turn SQLite's close-time checkpoint off and still ran PASSIVE; a fence
    landing during it then got a close whose own retry of the unavailable
    control failed silently, so the last close checkpointed or removed the
    fenced WAL family. Without the control there is no attempt at all, as at
    keeper admission, and the family is left exactly as it was. Attempts are
    counted before the pragma runs, and the late fence of the regression above
    is published by any attempt."""
    wal, _clock = w9
    db = _released_store(w9, tmp_path)
    import _lib_sqlite_close

    real_set = _lib_sqlite_close.set_no_checkpoint_on_close

    def refuse(conn, disabled, *, purpose="connection"):
        if disabled:
            raise sqlite3.NotSupportedError("injected: no close control")
        return real_set(conn, disabled, purpose=purpose)

    monkeypatch.setattr(_lib_sqlite_close, "set_no_checkpoint_on_close",
                        refuse)
    attempts: list[str] = []
    real_passive = wal.run_passive_checkpoint

    def counted_then_fenced(conn):
        attempts.append("passive")
        _publish("request", db)
        return real_passive(conn)

    monkeypatch.setattr(wal, "run_passive_checkpoint", counted_then_fenced)
    before = _family(db)
    wal.finalize()
    assert attempts == [], (
        "the exit attempt ran PASSIVE without the close control", attempts)
    assert _family(db) == before, "the refusal wrote the WAL family"
    assert _count(db) == 8, "the next opener recovers every row"


def test_m_no_keeper_without_the_close_control(w9, tmp_path, monkeypatch):
    """Where no-checkpoint-on-close cannot be set, no keeper is admitted, the
    store is not read before that refusal, the probe is not repeated by every
    arm, and the store keeps revision 12's close-time checkpoints."""
    wal, _clock = w9
    import _lib_sqlite_close

    keeper_statements: list[str] = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if kwargs.get("check_same_thread") is False:
            conn.set_trace_callback(keeper_statements.append)
        return conn

    probes: list[list[str]] = []

    def refuse(conn, disabled, *, purpose="connection"):
        probes.append(list(keeper_statements))
        raise sqlite3.NotSupportedError("injected: no close control")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(_lib_sqlite_close, "set_no_checkpoint_on_close",
                        refuse)
    db = tmp_path / "store.db"
    writer = _wal_store(db)
    try:
        wal.arm(writer)
        assert wal.kept_stores() == {}, (
            "a keeper was admitted without the close control")
        assert keeper_statements == [], (
            "the keeper read the store before the close control",
            keeper_statements)
        assert probes == [[]], probes
        wal.arm(writer)
        assert len(probes) == 1, "every arm probed the close control again"
        _fill(writer, 4)
    finally:
        writer.close()
    assert _drained(db), "the store did not keep its close-time checkpoint"
    assert _count(db) == 4


# ── admission, ownership and release ─────────────────────────────────────────


def test_m_a_request_published_during_admission_is_honoured(
    w9, tmp_path, monkeypatch, capsys,
):
    """The fence check runs again after the keeper opens, while the arming
    connection is still open: a request published between the two checks
    closes the new keeper silently."""
    wal, _clock = w9
    db = tmp_path / "store.db"
    writer = _wal_store(db)
    _fill(writer, 2)
    opened: list = []
    closed: list = []
    at_request: list = []
    real_open, real_close = wal._open_keeper, wal._close_quietly

    def open_then_publish(path, identity):
        keeper = real_open(path, identity)
        opened.append(keeper)
        _publish("request", db)
        at_request.append(_family(db))
        return keeper

    def close(conn):
        closed.append(conn)
        return real_close(conn)

    monkeypatch.setattr(wal, "_open_keeper", open_then_publish)
    monkeypatch.setattr(wal, "_close_quietly", close)
    try:
        wal.arm(writer)
        assert opened and opened[0] is not None, "no keeper was opened"
        assert wal.kept_stores() == {}, "the late request was missed"
        assert closed == [opened[0].conn], "the new keeper was not closed"
        assert _family(db) == at_request[0], "the yield path wrote the family"
        assert os.path.realpath(db) in wal.suspended_stores()
        lines = _yield_lines(capsys.readouterr().err)
        assert len(lines) == 1 and "keeper-yield request" in lines[0], lines
    finally:
        writer.close()


def _dead_pid() -> int:
    child = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                           capture_output=True, text=True, timeout=30)
    pid = int(child.stdout.strip())
    assert not _cctally_db._pid_is_alive(pid)
    return pid


@pytest.mark.parametrize("fence", ("request", "repair"))
@pytest.mark.parametrize("owner", ("dead", "reused"))
def test_m_a_dead_or_reused_owner_is_not_a_fence(
    w9, tmp_path, monkeypatch, fence, owner,
):
    wal, clock = w9
    db = tmp_path / "store.db"
    if owner == "dead":
        record = _owner_record(_dead_pid(), "ps:Thu Jan  1 00:00:00 1970")
    else:
        record = _owner_record(os.getpid(), "ps:Thu Jan  1 00:00:00 1970")
    writer = _wal_store(db)
    try:
        _publish(fence, db, owner=record)
        before = _fence_path(fence, db).read_bytes()
        assert wal.arm(writer)
        assert wal.kept_stores() == {os.path.realpath(db): _identity(db)}
        _fill(writer, 4)
        wal.after_commit(writer)
    finally:
        writer.close()
    passive = PassiveSpy(wal, monkeypatch)
    clock.advance(61)
    wal._timer_round()
    assert len(passive.for_path(db)) == 1, "a stale owner blocked the timer"
    assert wal.kept_stores()
    assert _fence_path(fence, db).read_bytes() == before, (
        "an observer touched the stale record")


def test_m_release_closes_this_processs_own_keeper_at_once(w9, tmp_path):
    wal, _clock = w9
    db = _kept_store(w9, tmp_path)
    before = _family(db)
    wal.release(db)
    assert wal.kept_stores() == {}
    assert _family(db) == before, "the release checkpointed"
    assert _cctally_db._db_family_open_pids(db) == set()


# ── ordinary maintenance keeps the keeper ────────────────────────────────────


@pytest.fixture
def product(tmp_path, monkeypatch):
    return Product(tmp_path, monkeypatch)


def test_m_own_retention_prune_and_reclaim_keep_the_keeper(
    product, w9, monkeypatch, capsys,
):
    """The process's own retention visit and reclaim attempt hold the
    maintenance lock exclusively; neither publishes a request, so the keeper
    stays, nothing checkpoints and the WAL keeps its generation."""
    wal, _clock = w9
    import datetime as dt

    import _cctally_core
    import _lib_conversation_retention as retention

    product.write(3)
    product.cycle()
    path = product.stores()["conversations.db"]
    kept = wal.kept_stores()
    assert os.path.realpath(path) in kept
    keeper = wal._KEEPERS[os.path.realpath(path)].conn
    conn = product.ns["open_conversations_db"]()
    try:
        conn.execute(
            "INSERT INTO conversation_messages (session_id, uuid, "
            "source_path, byte_offset, timestamp_utc, entry_type, text) "
            "VALUES ('g10-m-old', 'g10-m-u1', 'old.jsonl', 1, "
            "'2025-08-01T12:00:00.000Z', 'human', 'expired')")
        conn.commit()
        passive = PassiveSpy(wal, monkeypatch)
        salt = _wal_salt(path)
        now = dt.datetime(2026, 7, 17, 12, tzinfo=dt.timezone.utc)
        pruned = retention._maybe_prune_conversation_retention(
            conn, now_utc=now, retention_days=180, force=True)
        assert pruned is not None and pruned.claude_sessions == 1, pruned
        maint = open(_cctally_core.CONVERSATIONS_LOCK_MAINTENANCE_PATH, "a+")
        try:
            fcntl.flock(maint, fcntl.LOCK_EX | fcntl.LOCK_NB)
            retention._run_reclaim_attempt(
                str(path), now=now, record_phase=None, clock=time.monotonic)
            wal._timer_round()
        finally:
            fcntl.flock(maint, fcntl.LOCK_UN)
            maint.close()
    finally:
        conn.close()
    assert wal.kept_stores() == kept
    assert wal._KEEPERS[os.path.realpath(path)].conn is keeper
    assert passive.for_path(path) == [], "ordinary maintenance checkpointed"
    assert _wal_salt(path) == salt, "ordinary maintenance began a new WAL"
    assert _yield_lines(capsys.readouterr().err) == []


# ── resumption: `arm` takes no lock, the timer takes maintenance shared ─────


_PROVIDER_LOCKS = {
    "cache.db": ("CACHE_LOCK_PATH", "CACHE_LOCK_CODEX_PATH"),
    "conversations.db": ("CONVERSATIONS_LOCK_PATH",
                         "CONVERSATIONS_LOCK_CODEX_PATH"),
}


def _open_store(product: Product, store: str) -> sqlite3.Connection:
    opener = ("open_cache_db" if store == "cache.db"
              else "open_conversations_db")
    return product.ns[opener]()


def _suspend(wal, path) -> None:
    """Yield the store's keeper to a request, then withdraw the request."""
    _publish("request", pathlib.Path(path))
    wal._timer_round()
    assert wal.kept_stores().get(os.path.realpath(path)) is None
    _withdraw("request", pathlib.Path(path))
    assert os.path.realpath(path) in wal.suspended_stores()


@pytest.mark.parametrize("store", STORES)
def test_m_arm_resumes_a_suspended_store_without_taking_a_lock(
    product, w9, monkeypatch, store,
):
    """`arm` runs under the production callers' writer and provider locks; it
    resumes a suspended keeper under the module's lock alone."""
    wal, _clock = w9
    import _cctally_core
    import _lib_cache_writer_lock as writer_lock

    product.write(2)
    product.cycle()
    path = product.stores()[store]
    _suspend(wal, path)
    conn = _open_store(product, store)
    held = writer_lock.acquire_cache_writer_flocks(
        *(getattr(_cctally_core, name) for name in _PROVIDER_LOCKS[store]),
        timeout=None)
    assert held is not None
    acquisitions: list = []
    real_flock = fcntl.flock

    def instrumented(fd, operation):
        if operation != fcntl.LOCK_UN:
            acquisitions.append((getattr(fd, "name", fd), operation))
            raise AssertionError(
                f"arm acquired a flock: {getattr(fd, 'name', fd)}")
        return real_flock(fd, operation)

    try:
        monkeypatch.setattr(fcntl, "flock", instrumented)
        try:
            assert wal.arm(conn)
        finally:
            monkeypatch.setattr(fcntl, "flock", real_flock)
        assert acquisitions == []
        assert os.path.realpath(path) in wal.kept_stores()
        assert os.path.realpath(path) not in wal.suspended_stores()
    finally:
        writer_lock.release_cache_writer_flocks(held)
        conn.close()


@pytest.mark.parametrize("store", STORES)
def test_m_the_timer_resumes_under_a_short_nonblocking_shared_maintenance_lock(
    product, w9, monkeypatch, store,
):
    wal, _clock = w9
    import _cctally_core

    product.write(2)
    product.cycle()
    path = product.stores()[store]
    maintenance = getattr(
        _cctally_core,
        "CACHE_LOCK_MAINTENANCE_PATH" if store == "cache.db"
        else "CONVERSATIONS_LOCK_MAINTENANCE_PATH")
    assert os.path.realpath(wal.maintenance_lock_path(path)) == (
        os.path.realpath(maintenance))
    _suspend(wal, path)

    holder = open(maintenance, "a+")
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        wal._timer_round()
        assert os.path.realpath(path) not in wal.kept_stores(), (
            "the timer resumed while the maintenance lock was held")
        assert os.path.realpath(path) in wal.suspended_stores()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    calls: list = []
    real_flock = fcntl.flock

    def recorded(fd, operation):
        calls.append((os.path.realpath(getattr(fd, "name", "")), operation))
        return real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", recorded)
    try:
        wal._timer_round()
    finally:
        monkeypatch.setattr(fcntl, "flock", real_flock)
    assert os.path.realpath(path) in wal.kept_stores()
    assert calls == [
        (os.path.realpath(maintenance), fcntl.LOCK_SH | fcntl.LOCK_NB),
        (os.path.realpath(maintenance), fcntl.LOCK_UN),
    ], calls
    probe = open(maintenance, "a+")
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe, fcntl.LOCK_UN)
    finally:
        probe.close()


@pytest.mark.parametrize("store", STORES)
def test_m_a_caught_up_process_resumes_from_the_timer_and_copies_retained_frames(
    product, w9, monkeypatch, store,
):
    """After a yield and the fence's removal, a process whose iterations are
    all caught up reopens its keeper from the timer thread, and a completed
    PASSIVE copies the frames retained while it was suspended."""
    wal, clock = w9
    path, observer = _warm(product, wal, monkeypatch, store)
    try:
        _publish("request", path)
        assert _wait(lambda: os.path.realpath(path) not in wal.kept_stores()), (
            "the timer thread did not yield the keeper")
        writer = sqlite3.connect(path)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) "
            "VALUES ('g10-m-retained', ?)", ("y" * 100000,))
        writer.commit()
        writer.close()
        assert _retained(path)
        iterate = _caught_up_iterations(product, monkeypatch)
        iterate()
        clock.advance(120)
        time.sleep(0.3)  # many timer rounds while the request is live
        assert _retained(path), "a suspended store was checkpointed"
        assert os.path.realpath(path) not in wal.kept_stores()
        _withdraw("request", path)
        iterate()
        assert _wait(lambda: not _retained(path)), (
            "no completed PASSIVE copied the retained frames after resuming",
            wal.kept_stores(), wal.suspended_stores())
        assert os.path.realpath(path) in wal.kept_stores()
    finally:
        observer.close()


@pytest.mark.parametrize("store", STORES)
def test_m_db_checkpoint_and_the_backup_api_work_with_a_keeper_open(
    product, w9, tmp_path, store,
):
    wal, _clock = w9
    import argparse

    product.write(3)
    product.cycle()
    path = product.stores()[store]
    kept = wal.kept_stores()
    assert os.path.realpath(path) in kept
    label = store.split(".")[0]
    rc = product.ns["cmd_db_checkpoint"](
        argparse.Namespace(db=label, json=False, timeout=5.0))
    assert rc == 0, "db checkpoint did not drain the WAL with a keeper open"
    assert _drained(path)
    source = sqlite3.connect(path)
    target = sqlite3.connect(tmp_path / f"{store}.backup")
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    if store == "cache.db":
        rc = product.ns["cmd_db_backup"](argparse.Namespace(
            db="cache", backup_output=str(tmp_path / "cache.db.bak")))
        assert rc == 0
    assert wal.kept_stores() == kept


# ── the requesters, across processes (G10 (m), cross-process half) ───────────


_KEEPER_DRIVER = r'''
import json, os, sqlite3, sys

bin_dir, db, log, interval, timer_seconds = sys.argv[1:6]
sys.path.insert(0, bin_dir)
import _lib_wal_checkpoint as wal

wal.TIMER_INTERVAL_SECONDS = float(interval)
wal.TIMER_SECONDS = float(timer_seconds)


def record(event, **extra):
    with open(log, "a") as fh:
        fh.write(json.dumps(dict(event=event, pid=os.getpid(), **extra)) + "\n")


real_run, real_open, real_close = (
    wal.run_passive_checkpoint, wal._open_keeper, wal._close_quietly)


def run(conn):
    row = real_run(conn)
    record("passive", row=list(row) if row is not None else None)
    return row


def open_keeper(path, identity):
    keeper = real_open(path, identity)
    record("open", admitted=keeper is not None)
    return keeper


def close_quietly(conn):
    record("close")
    return real_close(conn)


wal.run_passive_checkpoint = run
wal._open_keeper = open_keeper
wal._close_quietly = close_quietly
conn = sqlite3.connect(db)
assert wal.arm(conn)
conn.execute(
    "INSERT OR REPLACE INTO cache_meta(key, value) VALUES ('g10-m-keeper', ?)",
    ("k" * 60000,))
conn.commit()
wal.after_commit(conn)
conn.close()
print("ready", flush=True)
sys.stdin.read()
'''


def _log_events(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


class KeeperProcess:
    """A separate live process holding an idle keeper on one store: an armed
    sync connection that committed, closed, and stays alive (its timer thread
    running every ``interval`` seconds)."""

    def __init__(self, db: pathlib.Path, log: pathlib.Path, env: dict, *,
                 interval: float, timer_seconds: float) -> None:
        self.db, self.log = db, log
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _KEEPER_DRIVER, str(BIN_DIR), str(db),
             str(log), str(interval), str(timer_seconds)],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.stderr = ""
        ready = self.proc.stdout.readline().strip()
        if ready != "ready":
            self.stop()
            raise AssertionError(("the keeper driver did not start",
                                  self.stderr[-4000:]))

    @property
    def pid(self) -> int:
        return self.proc.pid

    def events(self) -> list[dict]:
        return _log_events(self.log)

    def reopened(self) -> bool:
        """A keeper admitted again after the keeper yielded."""
        seen_close = False
        for event in self.events():
            if event["event"] == "close":
                seen_close = True
            elif seen_close and event["event"] == "open" and event["admitted"]:
                return True
        return False

    def stop(self) -> int:
        """End the driver in order (its finalizer runs); kill it if it does
        not exit. Always reaps it and closes its pipes."""
        try:
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
                try:
                    self.proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=30)
        finally:
            for stream in (self.proc.stdin, self.proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            try:
                self.stderr = self.proc.stderr.read()
            except (OSError, ValueError):
                pass
            try:
                self.proc.stderr.close()
            except OSError:
                pass
        return self.proc.returncode


class Stores:
    """Both product stores under one ``CCTALLY_DATA_DIR``, the keeper drivers
    on them, and the CLI run against the same data directory."""

    def __init__(self, tmp_path: pathlib.Path, monkeypatch) -> None:
        import _lib_wal_checkpoint as wal

        monkeypatch.delenv("CCTALLY_TEST_W9_DISABLE", raising=False)
        monkeypatch.setenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", "1")
        wal.reset_policies()
        self.wal = wal
        self.tmp = tmp_path
        self.ns = load_script()
        redirect_paths_without_conversation_retention(
            self.ns, monkeypatch, tmp_path)
        self.share = tmp_path / ".local" / "share" / "cctally"
        self.cache = sys.modules["_cctally_cache"]
        self.db = sys.modules["_cctally_db"]
        self.ns["open_cache_db"]().close()
        self.ns["open_conversations_db"]().close()
        self.env = {**os.environ, "HOME": str(tmp_path),
                    "CCTALLY_DATA_DIR": str(self.share),
                    "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
                    "CCTALLY_DISABLE_TELEMETRY": "1",
                    "PYTHONDONTWRITEBYTECODE": "1", "TZ": "Etc/UTC"}
        for name in ("CCTALLY_TEST_W9_DISABLE", "CLAUDE_CONFIG_DIR"):
            self.env.pop(name, None)
        self.keepers: list[KeeperProcess] = []

    def path(self, store: str) -> pathlib.Path:
        return self.share / store

    def keeper(self, store: str, *, interval: float = 0.05,
               timer_seconds: float = 3600.0) -> KeeperProcess:
        keeper = KeeperProcess(
            self.path(store), self.tmp / f"keeper-{store}.log", self.env,
            interval=interval, timer_seconds=timer_seconds)
        self.keepers.append(keeper)
        return keeper

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(BIN_DIR / "cctally"), *args],
            env=self.env, capture_output=True, text=True, timeout=60)

    def close(self) -> None:
        for keeper in self.keepers:
            keeper.stop()
        self.wal.reset_policies()


@pytest.fixture
def stores(tmp_path, monkeypatch):
    fixture = Stores(tmp_path, monkeypatch)
    try:
        yield fixture
    finally:
        fixture.close()


def _retain_frames(path: pathlib.Path) -> None:
    """Commit frames from an unarmed connection whose close is not the last
    (another process's keeper is open), so they stay in the WAL."""
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) "
            "VALUES ('g10-m-after', ?)", ("a" * 80000,))
        writer.commit()
    finally:
        writer.close()


@pytest.mark.parametrize("store", STORES)
def test_m_db_vacuum_from_a_third_process_succeeds_while_a_keeper_lives(
    stores, store,
):
    path = stores.path(store)
    keeper = stores.keeper(store, timer_seconds=1.0)
    # Success is the proof it acquired within the grace: past the deadline
    # the same call refuses (test_m_a_handle_held_past_the_deadline_...).
    vacuum = stores.cli("db", "vacuum", "--db", store.split(".")[0])
    assert vacuum.returncode == 0, (
        "db vacuum refused while another process held an idle keeper",
        vacuum.stderr)
    assert f"cctally: {store} reclaimed" in vacuum.stdout, vacuum.stdout
    assert not _request_path(path).exists(), "the request outlived the vacuum"

    assert _wait(keeper.reopened, seconds=20.0), (
        "the keeper process did not reopen its keeper", keeper.events())
    _retain_frames(path)
    assert _retained(path)
    assert _wait(lambda: not _retained(path), seconds=20.0), (
        "the resumed keeper's timed checkpoint did not copy the frames",
        keeper.events())
    keeper.stop()
    lines = _yield_lines(keeper.stderr)
    assert len(lines) == 1, keeper.stderr[-4000:]
    assert store in lines[0] and "keeper-yield request" in lines[0], lines


def _recover(stores: Stores, store: str):
    exc = sqlite3.DatabaseError("database disk image is malformed")
    if store == "cache.db":
        return stores.cache._recover_corrupt_cache(exc, origin="test.g10_m")
    return stores.cache._recover_corrupt_conversations(
        exc, origin="test.g10_m", providers=("claude", "codex"),
        lock_timeout=5.0)


def _hash_at(monkeypatch, module, name: str, path, seen: list) -> None:
    """Hash the family when the requester reaches ``module.name``: its gate."""
    real = getattr(module, name)

    def at_gate(*args, **kwargs):
        seen.append(_family(path))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, at_gate)


@pytest.mark.parametrize("store", STORES)
def test_m_cross_process_recovery_proceeds_once_the_keeper_yields(
    stores, monkeypatch, store,
):
    """Recovery finds the keeper closed and reaches its forensics gate with
    the family byte-identical: the yield path ran no checkpoint. The store is
    healthy, so the gate declines and the family stays."""
    path = stores.path(store)
    keeper = stores.keeper(store)
    assert _retained(path), "the keeper's frames must still be in the WAL"
    before = _family(path)
    gate: list = []
    if store == "cache.db":
        _hash_at(monkeypatch, stores.cache._cctally_db_sib,
                 "write_corruption_forensics", path, gate)
    else:
        _hash_at(monkeypatch, stores.cache, "_conversation_probe_snapshot",
                 path, gate)
    try:
        recovered = _recover(stores, store)
    except sqlite3.DatabaseError as exc:
        pytest.fail(f"recovery refused while a keeper lived: {exc}")
    assert recovered is False, "a healthy family was quarantined"
    assert gate == [before], "the family changed between request and gate"
    assert path.exists() and not _request_path(path).exists()
    assert _wait(keeper.reopened, seconds=20.0), keeper.events()


def test_m_the_conversation_rebuild_probe_proceeds_once_the_keeper_yields(
    stores, monkeypatch,
):
    path = stores.path("conversations.db")
    keeper = stores.keeper("conversations.db")
    before = _family(path)
    gate: list = []
    _hash_at(monkeypatch, stores.cache, "_conversation_probe_snapshot", path,
             gate)
    try:
        trigger = stores.cache._probe_conversation_rebuild(
            path, lock_timeout=5.0)
    except sqlite3.DatabaseError as exc:
        pytest.fail(f"the rebuild probe refused while a keeper lived: {exc}")
    assert trigger is None, trigger
    assert gate == [before], "the family changed between request and gate"
    assert not _request_path(path).exists()
    assert _wait(keeper.reopened, seconds=20.0), keeper.events()


def _pending_quarantine(stores: Stores, store: str) -> pathlib.Path:
    path = stores.path(store)
    incident = stores.share / "quarantine" / f"{store}-20260101T000000Z"
    incident.mkdir(parents=True)
    stores.db._atomic_write_private_json(
        stores.db._quarantine_pending_path(path),
        {
            "schemaVersion": 1,
            "originalPath": str(path),
            "incidentPath": str(incident),
            "members": [f"{store}-wal", f"{store}-shm", store],
            "createdAtUtc": "2026-01-01T00:00:00Z",
        },
    )
    return incident


@pytest.mark.parametrize("store", STORES)
def test_m_a_pending_quarantine_resume_proceeds_once_the_keeper_yields(
    stores, monkeypatch, store,
):
    path = stores.path(store)
    keeper = stores.keeper(store)
    before = _family(path)
    assert all(before.values()), before
    incident = _pending_quarantine(stores, store)
    gate: list = []
    _hash_at(monkeypatch, stores.db, "quarantine_db_family", path, gate)
    opener = ("open_cache_db" if store == "cache.db"
              else "open_conversations_db")
    try:
        stores.ns[opener]().close()
    except sqlite3.DatabaseError as exc:
        pytest.fail(f"the pending quarantine did not resume: {exc}")
    assert gate and gate[0] == before, (
        "the family changed between request and gate")
    moved = {suffix or "main": hashlib.sha256(
        (incident / f"{store}{suffix}").read_bytes()).hexdigest()
        for suffix in ("", "-wal", "-shm")}
    assert moved == before, "the quarantined family is not the family as kept"
    assert not stores.db._quarantine_pending_path(path).exists()
    assert not _request_path(path).exists()
    assert _wait(keeper.reopened, seconds=20.0), keeper.events()


# ── the drain grace: refusal at the deadline, the requester's own keeper ─────


_REQUESTERS = (
    "vacuum-cache", "vacuum-conversations", "recover-cache",
    "recover-conversations", "rebuild-probe", "resume-cache",
    "resume-conversations",
)
_REQUESTER_STORE = {
    "vacuum-cache": "cache.db", "vacuum-conversations": "conversations.db",
    "recover-cache": "cache.db", "recover-conversations": "conversations.db",
    "rebuild-probe": "conversations.db", "resume-cache": "cache.db",
    "resume-conversations": "conversations.db",
}


def _today_refusal(requester: str, pid: int) -> str:
    """Each requester's existing refusal, verbatim."""
    store = _REQUESTER_STORE[requester]
    if requester.startswith("vacuum"):
        return (f"cctally: {store} is in use — VACUUM needs exclusive access. "
                f"Stop the dashboard and any other cctally process holding "
                f"{store}, then retry.")
    if requester in ("recover-cache", "recover-conversations",
                     "rebuild-probe"):
        return (f"{store} is still open in process(es) {pid}; leaving the "
                f"live family untouched")
    if requester == "resume-cache":
        return ("cache.db pending quarantine could not resume: database "
                f"family is still open in process(es) {pid}")
    return ("conversations.db pending recovery found open handles in "
            f"process(es) {pid}")


def _run_requester(stores: Stores, requester: str):
    """Run one requester; return its exit status or raised error text."""
    import argparse

    store = _REQUESTER_STORE[requester]
    if requester.startswith("vacuum"):
        return stores.ns["cmd_db_vacuum"](
            argparse.Namespace(db=store.split(".")[0]))
    try:
        if requester.startswith("recover"):
            _recover(stores, store)
        elif requester == "rebuild-probe":
            stores.cache._probe_conversation_rebuild(
                stores.path(store), lock_timeout=5.0)
        elif requester == "resume-cache":
            stores.ns["open_cache_db"]().close()
        else:
            stores.ns["open_conversations_db"]().close()
    except sqlite3.DatabaseError as exc:
        return str(exc)
    return None


@pytest.mark.parametrize("holder", ("reader", "sync"))
@pytest.mark.parametrize("requester", _REQUESTERS)
def test_m_a_handle_held_past_the_deadline_gets_todays_refusal(
    stores, monkeypatch, capsys, requester, holder,
):
    """A reader, or a sync in progress, that keeps the store open past the
    drain grace gets the existing refusal, exit status and message, no later
    than the deadline plus one retry interval; nothing is killed or exempted
    and the family is left as it was."""
    deadline = 1.0
    monkeypatch.setattr(stores.db, "_KEEPER_YIELD_DEADLINE_SECONDS", deadline)
    store = _REQUESTER_STORE[requester]
    path = stores.path(store)
    if requester.startswith("resume"):
        _pending_quarantine(stores, store)
    held = sqlite3.connect(path)
    if holder == "sync":
        held.execute("BEGIN IMMEDIATE")
    held.execute("SELECT count(*) FROM sqlite_master").fetchall()
    before = _family(path)
    try:
        started = time.monotonic()
        outcome = _run_requester(stores, requester)
        elapsed = time.monotonic() - started
        after = _family(path)
    finally:
        if held.in_transaction:
            held.rollback()
        held.close()
    expected = _today_refusal(requester, os.getpid())
    if requester.startswith("vacuum"):
        assert outcome == 3
        assert expected in capsys.readouterr().err
    else:
        assert outcome is not None and expected in outcome, outcome
    retry = stores.db._KEEPER_YIELD_RETRY_SECONDS + (
        stores.db._VACUUM_BUSY_TIMEOUT_MS / 1000.0
        if requester.startswith("vacuum") else 0.0)
    assert elapsed >= deadline - 0.05, f"refused before the grace: {elapsed}"
    assert elapsed < deadline + retry + 2.0, f"refused late: {elapsed}"
    assert after == before, "a refused requester changed the family"
    assert not _request_path(path).exists(), "the request outlived the refusal"


def _record_waits(monkeypatch, db_mod) -> list:
    """Every pause `_cctally_db` takes between two attempts while it waits
    for keepers (its only `time.sleep`s on these paths)."""
    import types

    waits: list = []
    real_sleep = db_mod.time.sleep

    def sleep(seconds):
        waits.append(seconds)
        return real_sleep(seconds)

    # Patch the importer's reference, never the shared stdlib module.
    isolated = types.SimpleNamespace(**vars(db_mod.time))
    isolated.sleep = sleep
    monkeypatch.setattr(db_mod, "time", isolated)
    return waits


def test_m_the_requester_closes_its_own_keeper_before_it_waits(
    stores, monkeypatch,
):
    """`db vacuum` in a process that holds its own keeper on the store closes
    it at once — its first exclusive attempt succeeds, with no pause to wait
    for the keeper — and the frames the keeper kept in the WAL reach the
    vacuumed file."""
    import argparse

    wal = stores.wal
    path = stores.path("cache.db")
    conn = sqlite3.connect(path)
    try:
        assert wal.arm(conn)
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) "
            "VALUES ('g10-m-own', ?)", ("o" * 50000,))
        conn.commit()
        wal.after_commit(conn)
    finally:
        conn.close()
    assert wal.kept_stores() and _retained(path)
    waits = _record_waits(monkeypatch, stores.db)
    rc = stores.ns["cmd_db_vacuum"](argparse.Namespace(db="cache"))
    assert rc == 0, "the requester's own keeper blocked its vacuum"
    assert waits == [], f"the requester waited for its own keeper: {waits}"
    assert wal.kept_stores() == {}
    reader = sqlite3.connect(path)
    try:
        assert reader.execute(
            "SELECT length(value) FROM cache_meta WHERE key='g10-m-own'"
        ).fetchone() == (50000,)
    finally:
        reader.close()


# ── stats.db has no keeper: its paths are unchanged ──────────────────────────


def test_m_db_repair_of_stats_db_is_unchanged(tmp_path, monkeypatch, capsys):
    from test_db_repair_314 import _ns, _repair_args, _seed_corrupt_stats

    c = _ns(monkeypatch, tmp_path)
    db_mod = sys.modules["_cctally_db"]
    claims: list = []
    monkeypatch.setattr(
        db_mod, "_claim_keeper_yield_request",
        lambda path: claims.append(path) or (None, ""), raising=False)
    drains: list = []
    monkeypatch.setattr(
        db_mod, "_await_family_drained",
        lambda *args: drains.append(args) or set(), raising=False)
    source = c._cctally_core.DB_PATH
    _seed_corrupt_stats(source)
    idle = sqlite3.connect(source)
    try:
        rc = c.cmd_db_repair(_repair_args())
    finally:
        idle.close()
    assert rc == 3
    assert "still open" in capsys.readouterr().err
    assert claims == [], "db repair published a keeper-yield request"
    assert drains == [], "db repair waited for keepers"


def test_m_db_vacuum_of_stats_db_is_unchanged(tmp_path, monkeypatch, capsys):
    import argparse

    ns = load_script()
    redirect_paths_without_conversation_retention(ns, monkeypatch, tmp_path)
    db_mod = sys.modules["_cctally_db"]
    claims: list = []
    monkeypatch.setattr(
        db_mod, "_claim_keeper_yield_request",
        lambda path: claims.append(path) or (None, ""), raising=False)
    import _cctally_core

    acquisitions: list = []
    monkeypatch.setattr(
        db_mod, "_acquire_vacuum_exclusion",
        lambda *args: acquisitions.append(args), raising=False)
    stats = sqlite3.connect(_cctally_core.DB_PATH)
    stats.execute("CREATE TABLE t(x)")
    stats.executemany("INSERT INTO t VALUES (randomblob(2000))", [()] * 20)
    stats.commit()
    stats.close()
    reader = sqlite3.connect(_cctally_core.DB_PATH)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM t").fetchone()
    try:
        rc = ns["cmd_db_vacuum"](argparse.Namespace(db="stats"))
    finally:
        reader.rollback()
        reader.close()
    assert rc == 3
    assert "cctally: stats.db is in use" in capsys.readouterr().err
    assert claims == [], "a stats.db vacuum published a keeper-yield request"
    assert acquisitions == [], "a stats.db vacuum retried its acquisition"


def test_m_a_fork_never_hands_the_child_an_open_keeper(w9, tmp_path):
    """PR-6 / OV-7. After `fork()` without `exec` the child inherited the
    parent's open keeper connections, and with them SQLite's per-process
    inode lock bookkeeping, while the fcntl locks behind it are not
    inherited: a child that opened the same store (the transcript rebuild
    worker does) could believe it held locks the kernel never granted it.
    The keepers are now released before every fork, so the child inherits
    none; the parent's store is suspended and reopened by its next arm."""
    wal, _clock = w9
    db = _kept_store(w9, tmp_path)
    key = os.path.realpath(db)
    before = len(wal._INHERITED)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            report = {
                "inherited": len(wal._INHERITED) - before,
                "kept": len(wal.kept_stores()),
                "rows": _count(db),
            }
        except BaseException as exc:  # noqa: BLE001
            report = {"error": repr(exc)}
        with os.fdopen(write_fd, "w") as stream:
            json.dump(report, stream)
        os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd) as stream:
        report = json.load(stream)
    os.waitpid(pid, 0)
    assert report == {"inherited": 0, "kept": 0, "rows": 8}, report
    assert wal.kept_stores() == {}, "the parent kept a keeper across the fork"
    assert key in wal.suspended_stores()
    writer = _wal_store(db)
    try:
        assert wal.arm(writer)
        assert key in wal.kept_stores(), "the next arm reopens the keeper"
    finally:
        writer.close()
