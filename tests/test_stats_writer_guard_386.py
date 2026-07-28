"""Sanctioned-write-context and authorizer guard regressions for issue #386.

Spec section 6.2 (runtime proof). The sanctioned-write context is what tells the
enforcement layer "this mutation is legal" — the ingester enters it while it
holds ``journal.ingest.lock``, maintenance paths enter it while they hold
``stats.db.maintenance.lock``.

It is a ``ContextVar``, NOT a module global. The dashboard is threaded, and a
process-global boolean would let one sanctioned thread authorize an unrelated
one; ``test_scope_does_not_leak_across_threads`` is the regression for exactly
that.
"""

from __future__ import annotations

import fcntl
import os
import pathlib
import sqlite3
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import _cctally_core  # noqa: E402
import _cctally_store as store  # noqa: E402

#: Opt out of `tests/conftest.py::_stats_write_sanction`. That autouse fixture
#: declares the pytest process a sanctioned stats writer so the corpus's
#: hand-built fixtures are legal; this module's entire subject is what the guard
#: does with NO scope held, so it must run with the guard live.
CCTALLY_STATS_GUARD_LIVE = True


def test_scope_is_false_by_default():
    assert store.in_stats_write_scope() is False
    assert store.holds_ingest_lock() is False


def test_scope_is_true_inside_and_false_after():
    with store.stats_write_scope("ingest"):
        assert store.in_stats_write_scope() is True
    assert store.in_stats_write_scope() is False


def test_scope_nests():
    with store.stats_write_scope("maintenance"):
        with store.stats_write_scope("ingest"):
            assert store.in_stats_write_scope() is True
        assert store.in_stats_write_scope() is True
    assert store.in_stats_write_scope() is False


def test_ingest_lock_flag_is_independent_of_the_scope():
    """Entering the scope does NOT imply holding the ingest lock. The heal path
    keys its self-deadlock avoidance on the narrower fact."""
    with store.stats_write_scope("maintenance"):
        assert store.in_stats_write_scope() is True
        assert store.holds_ingest_lock() is False
    with store.stats_write_scope("ingest", ingest_lock=True):
        assert store.holds_ingest_lock() is True
    assert store.holds_ingest_lock() is False


def test_ingest_lock_flag_unwinds_from_a_nested_non_holder():
    """A nested scope that does NOT hold the lock must not clear the outer
    scope's claim on exit."""
    with store.stats_write_scope("ingest", ingest_lock=True):
        with store.stats_write_scope("inner"):
            assert store.holds_ingest_lock() is True
        assert store.holds_ingest_lock() is True
    assert store.holds_ingest_lock() is False


def test_scope_is_restored_when_the_body_raises():
    try:
        with store.stats_write_scope("ingest", ingest_lock=True):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert store.in_stats_write_scope() is False
    assert store.holds_ingest_lock() is False


def test_scope_does_not_leak_across_threads():
    """A process-global boolean would fail this. The dashboard is threaded."""
    seen = {}

    def worker():
        seen["inner"] = store.in_stats_write_scope()
        seen["ingest"] = store.holds_ingest_lock()

    with store.stats_write_scope("ingest", ingest_lock=True):
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    assert seen["inner"] is False
    assert seen["ingest"] is False


# ---------------------------------------------------------------------------
# _heal_flock_bounded — the timeout contract (#386 Task 9)
# ---------------------------------------------------------------------------
#
# `flock` conflicts are per open-file-DESCRIPTION and apply WITHIN a process, so
# a second fd in THIS process is a faithful stand-in for a competing holder —
# no subprocess needed, and no chance of a stray child outliving the test.


def _hold_exclusive(path: pathlib.Path) -> int:
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_heal_flock_bounded_returns_none_when_another_holder_wins(
    tmp_path, monkeypatch
):
    """The pre-#386 helper returned the OPEN fd WITHOUT the lock on timeout and
    the caller proceeded "best-effort" — describing itself as serialized while
    running unserialized, with no way for a caller to tell the outcomes apart
    because both were an ``int``."""
    monkeypatch.setattr(_cctally_core, "APP_DIR", tmp_path)
    lock = tmp_path / "contended.lock"
    other = _hold_exclusive(lock)
    try:
        assert store._heal_flock_bounded(lock, 0.15) is None
    finally:
        _release(other)


def test_heal_flock_bounded_returns_a_genuinely_held_fd(tmp_path, monkeypatch):
    """And on success the returned fd really carries the lock — proven by a
    second descriptor being refused, not by trusting the return type."""
    monkeypatch.setattr(_cctally_core, "APP_DIR", tmp_path)
    lock = tmp_path / "free.lock"
    fd = store._heal_flock_bounded(lock, 1.0)
    assert fd is not None
    try:
        probe = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            with pytest.raises((BlockingIOError, OSError)):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
    finally:
        store._heal_release_flock(fd)


# ---------------------------------------------------------------------------
# The maintenance re-entrancy tracker (#386 Task 8/9)
# ---------------------------------------------------------------------------


def test_maintenance_hold_tracker_is_context_scoped():
    assert _cctally_core.holds_stats_maintenance() is False
    _cctally_core.note_stats_maintenance_acquired()
    try:
        assert _cctally_core.holds_stats_maintenance() is True

        seen = {}

        def worker():
            seen["inner"] = _cctally_core.holds_stats_maintenance()

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        # A thread that does NOT own the flock must be made to WAIT for it, so
        # the suppressor must not leak into it.
        assert seen["inner"] is False
    finally:
        _cctally_core.note_stats_maintenance_released()
    assert _cctally_core.holds_stats_maintenance() is False


def test_maintenance_hold_tracker_clamps_at_zero():
    """An unbalanced release is a bug, but raising from inside a `finally` would
    mask the original failure that got us there."""
    _cctally_core.note_stats_maintenance_released()
    assert _cctally_core.holds_stats_maintenance() is False


# ---------------------------------------------------------------------------
# The authorizer guard (#386 Task 13, spec §6.1/§6.2)
# ---------------------------------------------------------------------------
#
# `set_authorizer`, NOT `set_trace_callback`:
# `test_trace_callback_cannot_enforce_which_is_why_this_is_an_authorizer` is the
# executable form of that finding, so the mechanism choice is a live regression
# rather than a claim in a docstring.


#: SQLite's own wording for an authorizer DENY. Every denial assertion matches
#: on it: `sqlite3.ProgrammingError` (e.g. cross-thread use) and
#: `sqlite3.OperationalError` are both `DatabaseError` subclasses, so a bare
#: `pytest.raises(sqlite3.DatabaseError)` can pass with the guard entirely
#: disarmed. Measured: with the authorizer neutered,
#: `test_guard_does_not_leak_sanction_across_threads` still passed — on the
#: cross-thread ProgrammingError, not on a denial.
_DENIED = "not authorized"


def _armed_conn(tmp_path, *, name="s.db", cross_thread=False):
    conn = sqlite3.connect(
        str(tmp_path / name), check_same_thread=not cross_thread
    )
    conn.execute("CREATE TABLE weekly_usage_snapshots (x)")
    store.arm_stats_authorizer(conn)
    return conn


def test_trace_callback_cannot_enforce_which_is_why_this_is_an_authorizer(
    tmp_path,
):
    """Python SUPPRESSES exceptions raised inside a trace callback.

    Spec §6.1 rejected `set_trace_callback` on this ground. Pinning it here
    means a future reader who "simplifies" the guard back to a trace hook gets a
    failing test instead of a silently non-enforcing guard.
    """
    conn = sqlite3.connect(str(tmp_path / "trace.db"))
    try:
        conn.execute("CREATE TABLE t (x)")

        def _raiser(statement):
            if "INSERT" in statement.upper():
                raise RuntimeError("blocked")

        conn.set_trace_callback(_raiser)
        conn.execute("INSERT INTO t VALUES (1)")
        conn.set_trace_callback(None)
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1, (
            "a raising trace callback DID prevent the write — if this ever "
            "becomes true, revisit the mechanism choice in spec §6.1"
        )
    finally:
        conn.close()


def test_write_outside_scope_is_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: True)
    conn = _armed_conn(tmp_path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match=_DENIED):
            conn.execute("INSERT INTO weekly_usage_snapshots VALUES (1)")
        # Denied, AND it did not land. A guard that raises after the write is
        # theatre.
        assert conn.execute(
            "SELECT count(*) FROM weekly_usage_snapshots").fetchone()[0] == 0
    finally:
        conn.close()


def test_write_inside_scope_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: True)
    conn = _armed_conn(tmp_path)
    try:
        with store.stats_write_scope("ingest", ingest_lock=True):
            conn.execute("INSERT INTO weekly_usage_snapshots VALUES (1)")
        assert conn.execute(
            "SELECT count(*) FROM weekly_usage_snapshots").fetchone()[0] == 1
    finally:
        conn.close()


def test_every_guarded_action_is_denied_outside_scope(tmp_path, monkeypatch):
    """All eight action codes, not just INSERT — an authorizer set that silently
    lost DDL coverage would still pass an INSERT-only test."""
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: True)
    conn = _armed_conn(tmp_path)
    try:
        with store.stats_write_scope("setup"):
            conn.execute("CREATE TABLE victim (a, b)")
            conn.execute("CREATE INDEX idx_victim ON victim(a)")
            conn.execute("INSERT INTO victim VALUES (1, 2)")
        for sql in (
            "INSERT INTO victim VALUES (3, 4)",
            "UPDATE victim SET b = 9",
            "DELETE FROM victim",
            "CREATE TABLE another (x)",
            "CREATE INDEX idx_victim_b ON victim(b)",
            "DROP INDEX idx_victim",
            "ALTER TABLE victim ADD COLUMN c",
            "DROP TABLE victim",
        ):
            with pytest.raises(sqlite3.DatabaseError, match=_DENIED):
                conn.execute(sql)
        # Nothing landed: the table, its one row and its index all survive.
        assert conn.execute("SELECT count(*) FROM victim").fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name = 'idx_victim'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_reads_are_always_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: True)
    conn = _armed_conn(tmp_path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM weekly_usage_snapshots").fetchone()[0] == 0
    finally:
        conn.close()


def test_temp_views_are_allowed_outside_scope(tmp_path, monkeypatch):
    """Stats connections legitimately create TEMP VIEWs outside ingest
    (`bin/_cctally_tui.py`). Scoping to the `main` schema keeps these legal —
    without it the guard false-positives on the TUI and the dashboard."""
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: True)
    conn = _armed_conn(tmp_path)
    try:
        conn.execute("CREATE TEMP VIEW v AS SELECT 1")
        conn.execute("CREATE TEMP TABLE tt (x)")
        conn.execute("INSERT INTO tt VALUES (1)")
        assert conn.execute("SELECT count(*) FROM tt").fetchone()[0] == 1
    finally:
        conn.close()


def test_installed_build_logs_instead_of_raising(tmp_path, monkeypatch):
    """On an installed build the guard must never break a user's command: it
    logs one throttled line and lets the write through."""
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    log = tmp_path / "stats-writer-guard.log"
    monkeypatch.setattr(store, "_guard_log_path", lambda: log)
    monkeypatch.setattr(store, "_guard_last_logged", 0.0)
    conn = _armed_conn(tmp_path)
    try:
        conn.execute("INSERT INTO weekly_usage_snapshots VALUES (1)")
        assert conn.execute(
            "SELECT count(*) FROM weekly_usage_snapshots").fetchone()[0] == 1
    finally:
        conn.close()
    assert "unsanctioned" in log.read_text()


def test_guard_log_is_throttled(tmp_path, monkeypatch):
    """A looping unsanctioned writer must not fill the disk."""
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    log = tmp_path / "stats-writer-guard.log"
    monkeypatch.setattr(store, "_guard_log_path", lambda: log)
    monkeypatch.setattr(store, "_guard_last_logged", 0.0)
    conn = _armed_conn(tmp_path)
    try:
        for i in range(25):
            conn.execute("INSERT INTO weekly_usage_snapshots VALUES (?)", (i,))
    finally:
        conn.close()
    assert len(log.read_text().splitlines()) == 1


def test_guard_log_throttle_survives_process_local_state_reset(
        tmp_path, monkeypatch):
    """The throttle marker is filesystem-backed, so a fresh process cannot
    append its own first line during the same throttle window."""
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    log = tmp_path / "stats-writer-guard.log"
    monkeypatch.setattr(store, "_guard_log_path", lambda: log)
    monkeypatch.setattr(store, "_guard_last_logged", 0.0)

    store._log_unsanctioned_write(sqlite3.SQLITE_INSERT, "first")
    # A new interpreter starts with this module global reset.
    monkeypatch.setattr(store, "_guard_last_logged", 0.0)
    store._log_unsanctioned_write(sqlite3.SQLITE_INSERT, "second")

    assert len(log.read_text().splitlines()) == 1


def test_guard_log_rotates_and_stays_bounded_during_violation_storm(
        tmp_path, monkeypatch):
    """More violations than the byte cap retain one bounded active generation
    plus one bounded rotated generation."""
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    log = tmp_path / "stats-writer-guard.log"
    rotated = tmp_path / "stats-writer-guard.log.1"
    monkeypatch.setattr(store, "_guard_log_path", lambda: log)
    monkeypatch.setattr(store, "_guard_last_logged", 0.0)
    monkeypatch.setattr(store, "_GUARD_THROTTLE_S", 0.0)
    monkeypatch.setattr(store, "_GUARD_LOG_ROTATE_BYTES", 256, raising=False)

    conn = _armed_conn(tmp_path)
    try:
        for i in range(100):
            # Distinct comments force distinct prepares. Re-executing one
            # cached statement does not call SQLite's authorizer again.
            conn.execute(
                f"INSERT INTO weekly_usage_snapshots VALUES (?) /* {i} */",
                (i,),
            )
    finally:
        conn.close()

    assert log.exists()
    assert rotated.exists()
    lines = log.read_bytes().splitlines() + rotated.read_bytes().splitlines()
    max_line_bytes = max(len(line) + 1 for line in lines)
    generation_bound = store._GUARD_LOG_ROTATE_BYTES + max_line_bytes
    assert log.stat().st_size <= generation_bound
    assert rotated.stat().st_size <= generation_bound
    assert log.stat().st_size + rotated.stat().st_size <= 2 * generation_bound


def test_guard_does_not_leak_sanction_across_threads(tmp_path, monkeypatch):
    """The reason the scope is a ContextVar, proven end to end through the
    authorizer rather than only through the flag."""
    monkeypatch.setattr(_cctally_core, "_is_dev_checkout", lambda: True)
    # `check_same_thread=False` is load-bearing: with the default, the worker
    # raises `sqlite3.ProgrammingError` (itself a `DatabaseError`) before the
    # authorizer is ever consulted, and this test passes with the guard
    # completely disarmed. Verified by neutering `_auth` — the assertion below
    # must be about a DENIAL, not about any DatabaseError.
    conn = _armed_conn(tmp_path, cross_thread=True)
    seen = {}

    def worker():
        try:
            conn.execute("INSERT INTO weekly_usage_snapshots VALUES (99)")
            seen["denied"] = False
        except sqlite3.DatabaseError as exc:
            seen["denied"] = _DENIED in str(exc)
            seen["error"] = str(exc)

    try:
        with store.stats_write_scope("ingest", ingest_lock=True):
            t = threading.Thread(target=worker)
            t.start()
            t.join()
        assert seen["denied"] is True, seen
        assert conn.execute(
            "SELECT count(*) FROM weekly_usage_snapshots").fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# R1-RESIDUAL — the three `own_conn` reconcile branches (#386 Task 13)
# ---------------------------------------------------------------------------


def test_own_conn_reconcile_branches_are_routed_not_self_sanctioned(
    tmp_path, monkeypatch
):
    """`bin/_cctally_milestones.py`'s three `conn is None` branches wrote
    stats.db outside the ingest cycle and outside every lock (the mutation
    inventory's R1-RESIDUAL). They are ROUTED, not exempted: the guard they
    enter must supply BOTH the sanctioned scope AND a real maintenance hold.

    Asserting only the scope would pass for a self-sanctioning writer that is
    still unserialized — which is the option that was rejected.
    """
    import _cctally_milestones as milestones

    monkeypatch.setattr(_cctally_core, "APP_DIR", tmp_path)
    monkeypatch.setattr(
        _cctally_core, "STATS_LOCK_MAINTENANCE_PATH",
        tmp_path / "stats.db.maintenance.lock",
    )
    assert store.in_stats_write_scope() is False
    assert _cctally_core.holds_stats_maintenance() is False
    with milestones._own_conn_stats_guard():
        assert store.in_stats_write_scope() is True
        assert _cctally_core.holds_stats_maintenance() is True
        # And the flock is genuinely held, not merely recorded: a second
        # descriptor must be refused.
        probe = os.open(
            str(_cctally_core.STATS_LOCK_MAINTENANCE_PATH),
            os.O_RDWR | os.O_CREAT, 0o600,
        )
        try:
            with pytest.raises((BlockingIOError, OSError)):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
    assert store.in_stats_write_scope() is False
    assert _cctally_core.holds_stats_maintenance() is False


def test_own_conn_guard_is_reentrant_under_a_held_maintenance_lock():
    """A caller that already owns the lock must not wait on itself — the same
    self-deadlock class Task 12.5 closed in the heal path."""
    import _cctally_milestones as milestones

    _cctally_core.note_stats_maintenance_acquired()
    try:
        with milestones._own_conn_stats_guard():
            assert store.in_stats_write_scope() is True
    finally:
        _cctally_core.note_stats_maintenance_released()
    assert store.in_stats_write_scope() is False
