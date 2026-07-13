"""Regression: stats.db must open with ``PRAGMA busy_timeout=5000`` so
multi-process contention smooths over instead of crashing on the first
``SQLITE_BUSY`` (cctally-dev#87).

cctally is a multi-process system by design — ``record-usage`` (from
claude-statusline), ``hook-tick`` (CC PostToolUse/Stop hooks), and one or
more ``dashboard`` servers all write to ``~/.local/share/cctally/stats.db``
concurrently, and worktrees magnify the writer count without changing the
data store. Before #87, ``open_db()`` set ``journal_mode=WAL`` +
``synchronous=NORMAL`` but NOT ``busy_timeout`` (default 0), so the first
time SQLite returned BUSY the call raised ``OperationalError: database is
locked`` immediately. Both open paths now set ``busy_timeout=15000``
(``bin/_cctally_cache.py`` + ``open_db``; raised from 5000 by #297 so a
writer waits out a slow-but-normal sync rather than erroring instantly).

The user-visible cost of the missing timeout was not just a UX papercut:
``record-usage`` ticks that died on BUSY silently dropped write-once,
forward-only ``percent_milestones`` / ``five_hour_milestones`` rows and the
alerts that would have fired at those crossings — permanent data loss (see
the #87 thread). ``test_concurrent_milestone_crossing_keeps_exactly_one_row``
guards that integrity property directly.

These are file-backed (not ``:memory:``) so SQLite's OS-level file locking
actually serializes the connections — the same approach as
``tests/test_migration_gate_concurrency.py``.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

import _cctally_core

from conftest import load_script, redirect_paths

# Matches the cache.db precedent at bin/_cctally_cache.py and the value
# the fix installs in open_db(). The longest stats.db writer transaction is
# _run_pending_migrations, which is bounded well under this timeout.
# #297 raised both opens from 5000 -> 15000 so a writer waits out a
# slow-but-normal sync (>5 s) instead of instantly erroring.
EXPECTED_BUSY_TIMEOUT_MS = 15000


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _busy_timeout(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA busy_timeout").fetchone()[0]


def test_open_db_sets_busy_timeout(ns):
    """The connection returned by open_db() carries busy_timeout=15000.

    This is the direct check on issue #87 acceptance criterion 1 (value
    raised 5000 -> 15000 by #297) — the PRAGMA is connection-scoped, so
    once set it governs every statement on that connection.
    """
    conn = ns["open_db"]()
    try:
        assert _busy_timeout(conn) == EXPECTED_BUSY_TIMEOUT_MS
    finally:
        conn.close()


def test_open_db_write_waits_for_concurrent_holder(ns):
    """A write through an open_db() connection BLOCKS (up to busy_timeout)
    behind a concurrent write-lock holder and then succeeds — it does NOT
    raise ``database is locked`` on the first BUSY.

    Deterministic: a separate connection grabs the write lock via
    ``BEGIN IMMEDIATE`` and holds it ~0.5s. A worker thread then drives an
    open_db() INSERT, which must wait for the release rather than crash.
    Without the fix (busy_timeout=0) the worker's first write raises
    instantly while the lock is held.
    """
    # Build the schema once, uncontended.
    ns["open_db"]().close()
    db_path = str(_cctally_core.DB_PATH)

    # Holder takes the write lock and keeps it. Autocommit mode (None) so
    # BEGIN/COMMIT are fully manual; it acquires uncontended so it needs no
    # busy_timeout of its own.
    holder = sqlite3.connect(db_path, timeout=30)
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")

    result: dict[str, object] = {}

    def worker() -> None:
        started = time.monotonic()
        try:
            conn = ns["open_db"]()
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?)",
                ("2026-05-25T00:00:00Z", "2026-05-19", "2026-05-26", 55.0, "{}"),
            )
            conn.commit()
            conn.close()
            result["ok"] = True
        except Exception as exc:  # pragma: no cover - failure path asserted below
            result["error"] = exc
        finally:
            result["elapsed"] = time.monotonic() - started

    th = threading.Thread(target=worker, name="busy-timeout-waiter")
    th.start()
    # Hold the lock long enough that the worker is provably blocked on it.
    time.sleep(0.5)
    holder.execute("COMMIT")
    holder.close()
    th.join(timeout=15)

    assert not th.is_alive(), "worker never completed — busy_timeout may be too small or absent"
    assert "error" not in result, (
        "open_db() write raised under a held write lock instead of waiting: "
        f"{result.get('error')!r}"
    )
    assert result.get("ok") is True
    # It really waited for the holder (released at ~0.5s) rather than racing
    # through before the lock was taken.
    assert float(result["elapsed"]) >= 0.4, (  # type: ignore[arg-type]
        f"write completed in {result['elapsed']}s — it did not actually "
        "contend for the held lock"
    )

    # The row landed exactly once.
    verify = ns["open_db"]()
    try:
        n = verify.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 55.0"
        ).fetchone()[0]
    finally:
        verify.close()
    assert n == 1


def test_concurrent_writers_no_lock_error(ns):
    """N threads each open their own open_db() connection and hammer writes;
    none raise ``database is locked`` and every row lands.

    Each write transaction holds the write lock for a short, bounded slice
    (a ~20ms sleep inside the transaction) so collisions are near-certain;
    busy_timeout=5000 lets the losers wait their turn. Total serialized
    lock time stays far under 5s, so this passes deterministically with the
    fix and fails without it.
    """
    ns["open_db"]().close()  # build schema once

    n_workers = 12
    iters = 3
    errors: list[Exception] = []
    barrier = threading.Barrier(n_workers)

    def worker(worker_id: int) -> None:
        try:
            conn = ns["open_db"]()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
            return
        try:
            barrier.wait(timeout=10)  # release all writers at once
            for i in range(iters):
                conn.execute(
                    "INSERT INTO weekly_usage_snapshots "
                    "(captured_at_utc, week_start_date, week_end_date, "
                    " weekly_percent, payload_json) VALUES (?, ?, ?, ?, ?)",
                    (f"2026-05-25T00:00:{worker_id:02d}Z", "2026-05-19",
                     "2026-05-26", float(worker_id * 10 + i), "{}"),
                )
                time.sleep(0.02)  # hold the write lock briefly to force contention
                conn.commit()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=(wid,), name=f"writer-{wid}")
        for wid in range(n_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, (
        f"concurrent open_db() writers hit {len(errors)} error(s); first: "
        f"{errors[0]!r}"
    )
    verify = ns["open_db"]()
    try:
        total = verify.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0]
    finally:
        verify.close()
    assert total == n_workers * iters, (
        f"expected {n_workers * iters} rows, got {total} — a write was lost "
        "under contention"
    )


def test_concurrent_milestone_crossing_keeps_exactly_one_row(ns):
    """N workers race to record the SAME percent-milestone crossing under
    heavy write contention; afterwards exactly ONE row exists — no losses,
    no dupes (issue #87 comment's added acceptance criterion).

    This is the integrity property the missing busy_timeout broke: a
    ``record-usage`` tick that crashed on BUSY before its milestone INSERT
    permanently dropped a write-once row. With busy_timeout the contenders
    queue; the ``UNIQUE(week_start_date, percent_threshold, reset_event_id)``
    constraint + ``INSERT OR IGNORE`` collapses them to one.
    """
    ns["open_db"]().close()  # build schema once

    n_workers = 12
    week_start = "2026-05-19"
    threshold = 55
    errors: list[Exception] = []
    barrier = threading.Barrier(n_workers)

    def worker() -> None:
        try:
            conn = ns["open_db"]()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
            return
        try:
            barrier.wait(timeout=10)
            conn.execute(
                "INSERT OR IGNORE INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " percent_threshold, cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-25T00:00:00Z", week_start, "2026-05-26",
                 threshold, 12.34, 0.5, 1, 1),
            )
            time.sleep(0.02)  # hold the write lock briefly to force contention
            conn.commit()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, name=f"milestone-{i}")
        for i in range(n_workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, (
        f"concurrent milestone writers hit {len(errors)} error(s); first: "
        f"{errors[0]!r}"
    )
    verify = ns["open_db"]()
    try:
        rows = verify.execute(
            "SELECT COUNT(*) FROM percent_milestones "
            "WHERE week_start_date = ? AND percent_threshold = ?",
            (week_start, threshold),
        ).fetchone()[0]
    finally:
        verify.close()
    assert rows == 1, (
        f"expected exactly one milestone row, got {rows} — the write-once "
        "crossing was lost or duplicated under contention"
    )


def test_busy_timeout_does_not_absorb_busy_snapshot(ns):
    """Characterizes WHY the live write paths take the write lock up front
    (BEGIN IMMEDIATE) rather than relying on busy_timeout alone (#87).

    busy_timeout=15000 IS in effect (see test_open_db_sets_busy_timeout), but
    it does NOT cover ``SQLITE_BUSY_SNAPSHOT``: a deferred transaction that
    READS (taking a WAL read snapshot), then sees a competing connection
    COMMIT, then tries to WRITE, fails the snapshot-to-write upgrade and
    raises ``database is locked`` *instantly* — the busy handler is never
    consulted, because waiting could never resolve a stale snapshot.

    This is the exact shape ``_backfill_five_hour_blocks`` had with a plain
    ``BEGIN`` (read min/max rows, then INSERT). The fix is to grab the write
    lock at BEGIN time so there is no read-then-upgrade.
    """
    ns["open_db"]().close()  # build schema
    seed = ns["open_db"]()
    seed.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, weekly_percent, "
        " payload_json) VALUES (?, ?, ?, ?, ?)",
        ("2026-05-25T00:00:00Z", "2026-05-19", "2026-05-26", 10.0, "{}"),
    )
    seed.commit()
    seed.close()

    reader = ns["open_db"]()
    writer = ns["open_db"]()
    reader.isolation_level = None
    writer.isolation_level = None
    try:
        reader.execute("BEGIN")  # deferred — no lock yet
        reader.execute("SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()

        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE weekly_usage_snapshots SET weekly_percent = weekly_percent + 1"
        )
        writer.execute("COMMIT")  # advance the WAL past reader's snapshot

        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            reader.execute(
                "UPDATE weekly_usage_snapshots SET weekly_percent = 99"
            )
        # Instant raise: busy_timeout (15s) did NOT delay it.
        assert time.monotonic() - start < 1.0
    finally:
        try:
            reader.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        reader.close()
        writer.close()


def _function_source(path: "object", func_name: str) -> str:
    import pathlib

    text = pathlib.Path(str(path)).read_text()
    marker = f"def {func_name}("
    start = text.index(marker)
    # Slice to the next top-level `def ` so we only see this function's body.
    nxt = text.find("\ndef ", start + 1)
    return text[start: nxt if nxt != -1 else len(text)]


def test_live_write_paths_use_begin_immediate():
    """Guard the #87 hardening: the live stats.db write transactions take the
    write lock up front via ``BEGIN IMMEDIATE``, never a bare deferred
    ``BEGIN``.

    A deferred ``BEGIN`` followed by a read leaves the transaction exposed to
    the unabsorbable ``SQLITE_BUSY_SNAPSHOT`` raise characterized in
    test_busy_timeout_does_not_absorb_busy_snapshot. This source-level guard
    fails loudly if either site regresses to a plain ``BEGIN``.
    """
    import pathlib

    bin_dir = pathlib.Path(__file__).resolve().parents[1] / "bin"

    backfill = _function_source(bin_dir / "_cctally_five_hour.py", "_backfill_five_hour_blocks")
    assert 'conn.execute("BEGIN IMMEDIATE")' in backfill, (
        "_backfill_five_hour_blocks must use BEGIN IMMEDIATE (#87): it reads "
        "min/max rows before its first INSERT, so a deferred BEGIN is exposed "
        "to SQLITE_BUSY_SNAPSHOT under concurrent openers."
    )
    assert 'conn.execute("BEGIN")' not in backfill

    upsert = _function_source(
        bin_dir / "_cctally_record.py", "maybe_update_five_hour_block"
    )
    assert 'conn.execute("BEGIN IMMEDIATE")' in upsert, (
        "maybe_update_five_hour_block must use BEGIN IMMEDIATE (#87) so the "
        "write-lock-up-front contract is explicit, not an accident of the "
        "first DML happening to be a write."
    )
    assert 'conn.execute("BEGIN")' not in upsert
