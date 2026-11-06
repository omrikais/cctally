"""Issue #453: defer whole-journal stats epoch rebuilds off live callers."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import load_script, redirect_paths
from tests._support_http import PRESENCE_BACKSTOP_SECONDS


REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "cctally"


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return (
        ns,
        sys.modules["_cctally_core"],
        sys.modules["_cctally_db"],
        sys.modules["_cctally_store"],
    )


def _stamp(path, version: int) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def test_shared_pending_probe_classifies_only_readable_post_legacy_mismatch(
    runtime,
):
    """A missing/legacy/corrupt/current store must never enter async rebuild."""
    _ns, core, _db, store = runtime
    path = core.DB_PATH

    assert hasattr(store, "stats_epoch_rebuild_pending"), (
        "the wrong-epoch predicate still has no store-level home shared by "
        "ordinary opens and the Codex hook"
    )
    assert store.stats_epoch_rebuild_pending(path) is False

    _stamp(path, core.LEGACY_STATS_HEAD)
    assert store.stats_epoch_rebuild_pending(path) is False

    _stamp(path, core.STATS_INDEX_EPOCH)
    assert store.stats_epoch_rebuild_pending(path) is False

    _stamp(path, core.STATS_INDEX_EPOCH - 1)
    assert store.stats_epoch_rebuild_pending(path) is True

    path.write_bytes(b"not a sqlite database")
    assert store.stats_epoch_rebuild_pending(path) is False


def test_live_wrong_epoch_open_defers_without_inline_resolver(
    runtime, monkeypatch,
):
    """An ordinary opener must schedule once instead of paying replay inline."""
    ns, core, _db, store = runtime
    ns["open_db"]().close()
    _stamp(core.DB_PATH, core.STATS_INDEX_EPOCH - 1)
    calls: list[str] = []

    monkeypatch.setattr(
        store,
        "defer_stats_epoch_rebuild",
        lambda: calls.append("defer") or "spawned",
        raising=False,
    )
    monkeypatch.setattr(
        store,
        "resolve_stats_epoch_mismatch",
        lambda: pytest.fail("ordinary open invoked the synchronous resolver"),
    )
    monkeypatch.setattr(
        store,
        "stats_open_guarded",
        lambda _path: pytest.fail(
            "wrong-epoch preflight reached the maintenance-blocking opener"
        ),
    )

    with pytest.raises(_db.StatsEpochRebuildDeferred) as raised:
        core.open_db()

    assert type(raised.value).__name__ == "StatsEpochRebuildDeferred"
    assert calls == ["defer"]


def test_losing_fresh_open_rechecks_epoch_after_open_time_lock(
    runtime, monkeypatch,
):
    """A waiter must accept the current index completed by the lock winner."""
    _ns, core, _db, store = runtime
    real_guard = store.stats_open_time_guard
    winner_ran = False

    @contextlib.contextmanager
    def winner_before_loser_guard(*, live):
        nonlocal winner_ran
        if not winner_ran:
            winner_ran = True
            monkeypatch.setattr(store, "stats_open_time_guard", real_guard)
            try:
                winner = core.open_db()
                winner.close()
            finally:
                monkeypatch.setattr(
                    store,
                    "stats_open_time_guard",
                    winner_before_loser_guard,
                )
        with real_guard(live=live):
            yield

    monkeypatch.setattr(
        store,
        "stats_open_time_guard",
        winner_before_loser_guard,
    )

    loser = core.open_db()
    try:
        assert winner_ran is True
        assert loser.execute("PRAGMA user_version").fetchone()[0] == (
            core.STATS_INDEX_EPOCH
        )
        assert loser.execute(
            "SELECT 1 FROM weekly_usage_snapshots LIMIT 1"
        ).fetchone() is None
    finally:
        loser.close()


def test_scheduler_spawns_once_and_recent_marker_suppresses_duplicates(
    runtime, monkeypatch,
):
    """Repeated interactive opens must not create a detached-process storm."""
    _ns, _core, _db, store = runtime
    import _cctally_update

    spawned: list[str] = []
    monkeypatch.setattr(
        _cctally_update,
        "_spawn_detached_with_pid",
        lambda command, **_kw: spawned.append(command) or os.getpid(),
    )

    assert store.defer_stats_epoch_rebuild() == "spawned"
    assert store.defer_stats_epoch_rebuild() == "pending"
    assert spawned == ["_stats-epoch-rebuild"]


def test_scheduler_failed_spawn_is_immediately_retryable(runtime, monkeypatch):
    """A failed Popen must not leave a fresh marker that wedges all retries."""
    _ns, core, _db, store = runtime
    import _cctally_update

    outcomes = iter((None, os.getpid()))
    monkeypatch.setattr(
        _cctally_update,
        "_spawn_detached_with_pid",
        lambda _command, **_kw: next(outcomes),
    )

    assert store.defer_stats_epoch_rebuild() == "failed"
    assert not (core.APP_DIR / "stats-epoch-rebuild.pending").exists()
    assert store.defer_stats_epoch_rebuild() == "spawned"


def test_scheduler_suppresses_launch_while_long_worker_holds_flock(
    runtime, monkeypatch,
):
    """An active replay outliving the marker TTL must not spawn a duplicate.

    #1012: the running worker is identified by its records, not by probing
    its flock."""
    _ns, core, _db, store = runtime
    import _cctally_update
    from test_997_worker_lifecycle import _SyntheticOwner

    core.APP_DIR.mkdir(parents=True, exist_ok=True)
    marker = core.APP_DIR / "stats-epoch-rebuild.pending"
    marker.touch()
    stale = time.time() - store._STATS_EPOCH_REBUILD_RETRY_SECONDS - 1
    os.utime(marker, (stale, stale))
    spawned: list[str] = []
    monkeypatch.setattr(
        _cctally_update,
        "_spawn_detached_with_pid",
        lambda command, **_kw: spawned.append(command) or os.getpid(),
    )
    with _SyntheticOwner("epoch"):
        assert store.defer_stats_epoch_rebuild() == "pending"
    assert spawned == []
    assert time.time() - marker.stat().st_mtime < 5


def test_spawn_failure_message_does_not_claim_worker_is_running(runtime):
    """Failed admission must give explicit, truthful retry guidance."""
    _ns, _core, db, _store = runtime
    exc = db.StatsEpochRebuildDeferred("failed")
    assert "could not start" in str(exc)
    assert "running in the background" not in str(exc)


def test_both_deferral_signals_share_one_base(runtime):
    """#496 S3 §6: one parent, so every catch site is widened exactly once."""
    _ns, _core, db, _store = runtime

    assert issubclass(db.StatsEpochRebuildDeferred, db.StatsRebuildDeferred)
    assert issubclass(db.StatsHealDeferred, db.StatsRebuildDeferred)
    assert issubclass(db.StatsRebuildDeferred, BaseException)
    assert not issubclass(db.StatsRebuildDeferred, Exception), (
        "a broad `except Exception` reporting fallback must not swallow either "
        "control signal and publish a misleading partial report"
    )
    heal = db.StatsHealDeferred("spawned", heal_id="h1", forensics_path="/a/b")
    assert heal.outcome == "spawned"
    assert heal.heal_id == "h1" and heal.forensics_path == "/a/b"
    assert "corruption rebuild is running in the background" in str(heal)
    assert "could not start" in str(db.StatsHealDeferred("failed"))


@pytest.mark.parametrize(
    ("factory", "want_corruption", "want_kind"),
    (
        ("StatsEpochRebuildDeferred", False, "maintenance_active"),
        ("StatsHealDeferred", True, "stats_corruption"),
    ),
)
def test_widening_the_catches_does_not_flatten_the_two_cases(
    runtime, monkeypatch, factory, want_corruption, want_kind,
):
    """A wrong EPOCH is a readable index; a deferred heal is an unreadable one.

    Reporting a deferred corruption heal as `corruption=False` makes the
    dashboard name the wrong fault and lands back in the class F16 closes.
    """
    ns, _core, db, _store = runtime
    initial = ns["_empty_dashboard_snapshot"]()
    initial.hydrating = True
    ref = ns["_SnapshotRef"](initial)

    class Hub:
        def __init__(self):
            self.published = []

        def publish(self, snapshot):
            self.published.append(snapshot)

    hub = Hub()

    def deferred(**_kwargs):
        raise getattr(db, factory)("pending")

    monkeypatch.setitem(ns, "_tui_build_snapshot", deferred)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref,
        hub=hub,
        pinned_now=dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc),
        display_tz_pref_override=None,
    )
    locked(skip_sync=True)

    published = hub.published[-1]
    assert published.sync_failures[0].database == "stats"
    assert published.sync_failures[0].corruption is want_corruption
    envelope = ns["snapshot_to_envelope"](
        published,
        now_utc=dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert envelope["sync_failure"]["kind"] == want_kind


def test_dashboard_cold_bind_hydrates_when_the_heal_defers(
    runtime, monkeypatch,
):
    """The cold-bind heal call sits INSIDE an `except` handler.

    A `BaseException` raised there escapes past the sibling `except` and fails
    dashboard startup, so the shared base is caught at the heal-call boundary
    and the corruption-typed degraded frame is built directly.
    """
    ns, _core, db, _store = runtime
    import _cctally_dashboard as dashboard
    import _cctally_tui as tui

    monkeypatch.setattr(
        dashboard,
        "_dashboard_initial_snapshot_once",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            tui._StatsSnapshotCorruption(
                sqlite3.DatabaseError("database disk image is malformed")
            )
        ),
    )
    monkeypatch.setattr(
        tui,
        "_tui_heal_post_query_stats",
        lambda _cause: (_ for _ in ()).throw(db.StatsHealDeferred("spawned")),
    )
    monkeypatch.setattr(
        tui,
        "_tui_precompute_doctor_payload",
        lambda *_a, **_kw: pytest.fail("deferred bind reopened stats"),
    )

    snapshot = dashboard._dashboard_initial_snapshot(
        SimpleNamespace(no_sync=False, host="127.0.0.1"),
        pinned_now=dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc),
        display_tz_pref_override=None,
    )

    assert snapshot.hydrating is True
    assert snapshot.sync_failures[0].corruption is True
    assert "corruption rebuild is running" in snapshot.last_sync_error


@pytest.mark.parametrize(
    ("factory", "want_corruption"),
    (
        ("StatsEpochRebuildDeferred", False),
        ("StatsHealDeferred", True),
    ),
)
def test_tui_snapshot_degrades_typed_when_the_post_query_heal_defers(
    runtime, monkeypatch, factory, want_corruption,
):
    """The TUI calls the heal BEFORE entering its retry catch, and must not
    spin: after deferral the index is still corrupt."""
    ns, _core, db, _store = runtime
    import _cctally_tui as tui

    builds = 0

    def once(**_kwargs):
        nonlocal builds
        builds += 1
        raise tui._StatsSnapshotCorruption(
            sqlite3.DatabaseError("database disk image is malformed")
        )

    monkeypatch.setattr(tui, "_tui_build_snapshot_once", once)
    monkeypatch.setattr(
        tui,
        "_tui_heal_post_query_stats",
        lambda _cause: (_ for _ in ()).throw(getattr(db, factory)("pending")),
    )

    snapshot = tui._tui_build_snapshot(
        now_utc=dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc),
        skip_sync=True,
    )

    assert builds == 1, "the retry must not spin against a still-corrupt index"
    assert snapshot.sync_failures[0].database == "stats"
    assert snapshot.sync_failures[0].corruption is want_corruption


def test_tui_sync_thread_survives_a_stats_deferral(runtime, monkeypatch):
    """#932 R8 (twin). Failure it detects: `StatsRebuildDeferred` is a
    `BaseException`, so one deferral raised by a later rebuild ended the TUI's
    sync thread and left the TUI showing its last frame forever, even after
    the rebuild published the current index."""
    ns, _core, db, _store = runtime
    import dataclasses

    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    sentinel = dataclasses.replace(
        ns["_empty_dashboard_snapshot"](), last_sync_error="sentinel-932")
    calls = []

    def build(**_kwargs):
        calls.append("build")
        if len(calls) == 1:
            raise db.StatsEpochRebuildDeferred("pending")
        return sentinel

    monkeypatch.setitem(ns, "_tui_build_snapshot", build)
    thread = ns["_TuiSyncThread"](ref, 1.0, skip_sync=True)
    thread.start()
    try:
        deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
        while (ref.get().last_sync_error != "sentinel-932"
               and thread._thread.is_alive()
               and time.monotonic() < deadline):
            time.sleep(0.05)
        assert thread._thread.is_alive(), "one deferral ended the sync thread"
        assert ref.get().last_sync_error == "sentinel-932"
    finally:
        thread.stop()


def test_worker_closes_resolver_connection_and_clears_marker_on_success(
    runtime, monkeypatch,
):
    """A successful detached resolver must publish then clear pending state."""
    _ns, core, _db, store = runtime
    marker = core.APP_DIR / "stats-epoch-rebuild.pending"
    marker.touch()
    calls: list[str] = []

    class Connection:
        def close(self):
            calls.append("close")

    monkeypatch.setattr(store, "stats_epoch_rebuild_pending", lambda: True)
    monkeypatch.setattr(
        store,
        "resolve_stats_epoch_mismatch",
        lambda: calls.append("resolve") or Connection(),
    )

    assert store.cmd_stats_epoch_rebuild_internal(SimpleNamespace()) == 0
    assert calls == ["resolve", "close"]
    assert not marker.exists()


def test_worker_failure_stays_retryable_and_logs_sanitized_error(
    runtime, monkeypatch,
):
    """A killed/broken rebuild must retain admission state without leaking paths."""
    _ns, core, _db, store = runtime
    marker = core.APP_DIR / "stats-epoch-rebuild.pending"
    marker.touch()
    monkeypatch.setattr(store, "stats_epoch_rebuild_pending", lambda: True)

    def fail():
        raise OSError(5, "input/output failure", "/private/secret=stats.db")

    monkeypatch.setattr(store, "resolve_stats_epoch_mismatch", fail)

    assert store.cmd_stats_epoch_rebuild_internal(SimpleNamespace()) == 0
    assert marker.exists()
    log = (core.LOG_DIR / "stats-epoch-rebuild.log").read_text()
    assert "result=error" in log
    assert "/private/secret" not in log


def test_worker_flock_prevents_concurrent_rebuild_execution(
    runtime, monkeypatch,
):
    """The worker flock is the final defense against concurrent replay: a
    worker started while another runs records `busy` (#1012: after losing
    the claim to that running owner) and never enters the resolver."""
    _ns, core, _db, store = runtime
    from test_997_worker_lifecycle import _Run, _SyntheticOwner, _until

    core.APP_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        store,
        "resolve_stats_epoch_mismatch",
        lambda: pytest.fail("duplicate worker entered the resolver"),
    )
    with _SyntheticOwner("epoch"):
        duplicate = _Run(
            lambda: store.cmd_stats_epoch_rebuild_internal(SimpleNamespace()),
            name="duplicate worker")
        duplicate.start()
        _until(lambda: not duplicate.is_alive(),
               "the duplicate worker never returned")
        duplicate.finish()
        assert duplicate.result == 0


def test_worker_real_rebuild_converges_then_next_open_is_steady(
    runtime, monkeypatch,
):
    """The real worker must publish the epoch before ordinary opens resume."""
    _ns, core, _db, store = runtime
    import _cctally_journal as journal
    import _lib_journal as wire

    resets_at = int(dt.datetime(2026, 8, 6, tzinfo=dt.timezone.utc).timestamp())
    journal.append_record(
        wire.make_obs(
            at="2026-08-02T12:00:00Z",
            src="statusline",
            provider="claude",
            payload={
                "weekly_percent": 12.0,
                "resets_at": resets_at,
                "source": "statusline",
                "captured_at": "2026-08-02T12:00:00Z",
            },
        )
    )
    journal.run_stats_ingest(mode="authoritative")
    _stamp(core.DB_PATH, core.STATS_INDEX_EPOCH - 1)
    (core.APP_DIR / "stats-epoch-rebuild.pending").touch()

    assert store.cmd_stats_epoch_rebuild_internal(SimpleNamespace()) == 0
    assert store.stats_epoch_rebuild_pending() is False

    monkeypatch.setattr(
        store,
        "defer_stats_epoch_rebuild",
        lambda: pytest.fail("steady open scheduled a second worker"),
        raising=False,
    )
    conn = core.open_db()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            core.STATS_INDEX_EPOCH
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0] >= 1
    finally:
        conn.close()


def test_pending_rebuild_publishes_through_a_probe_overlap(
    runtime, monkeypatch,
):
    """#1012 T5. Detects the activity probe suppressing a pending epoch
    rebuild: probing at the worker's start made it exit `busy`, so the index
    stayed at the old epoch. Through the overlap the worker must publish the
    current epoch from the journal, including an observation appended after
    the last ingest (a header-only stamp cannot pass), and the next open
    must schedule nothing."""
    _ns, core, _db, store = runtime
    import _cctally_journal as journal
    import _lib_journal as wire
    from test_997_worker_lifecycle import _run_overlap

    journal.append_record(_usage_obs(wire, 12.0, "2026-08-02T12:00:00Z"))
    journal.run_stats_ingest(mode="authoritative")
    _stamp(core.DB_PATH, core.STATS_INDEX_EPOCH - 1)
    marker = core.APP_DIR / "stats-epoch-rebuild.pending"
    marker.touch()
    journal.append_record(_usage_obs(wire, 37.5, "2026-08-02T12:05:00Z"))

    run = _run_overlap(SimpleNamespace(app=core.APP_DIR, st=store),
                       monkeypatch, "epoch",
                       store._stats_epoch_rebuild_worker_active)
    log = core.LOG_DIR / "stats-epoch-rebuild.log"
    assert run.outcome == "success", log.read_text() if log.exists() else ""
    assert store.stats_epoch_rebuild_pending() is False
    assert not marker.exists()
    monkeypatch.setattr(
        store,
        "defer_stats_epoch_rebuild",
        lambda: pytest.fail("steady open scheduled a second worker"),
        raising=False,
    )
    conn = core.open_db()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            core.STATS_INDEX_EPOCH
        )
        percents = sorted(r[0] for r in conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"))
    finally:
        conn.close()
    assert percents == [12.0, 37.5]


def _applied_cursor(core, journal):
    conn = core.open_db()
    try:
        return journal._read_cursor(conn)
    finally:
        conn.close()


def _usage_obs(wire, percent: float, at: str) -> dict:
    resets_at = int(dt.datetime(2026, 8, 6, tzinfo=dt.timezone.utc).timestamp())
    return wire.make_obs(
        at=at, src="statusline", provider="claude",
        payload={"weekly_percent": percent, "resets_at": resets_at,
                 "source": "statusline", "captured_at": at},
    )


def test_worker_derives_the_observation_whose_ingest_deferred(runtime):
    """#825/#832 (spec C3/F1): a recorder appends its observation and only then
    opens the index; finding another epoch, that open defers and ingests
    nothing. A rebuild is apply-only, so the worker's rebuild runs the
    derivation-completion step, which journals the decision the deferred
    ingest never made instead of consuming the observation unmaterialized
    (Codex fix-round-1 Q1)."""
    _ns, core, _db, store = runtime
    import _cctally_journal as journal
    import _lib_journal as wire

    journal.append_record(_usage_obs(wire, 12.0, "2026-08-02T12:00:00Z"))
    journal.run_stats_ingest(mode="authoritative")
    # Ingest appends its own evts past the prefix it pinned, so the applied
    # cursor sits behind the journal high-water even when nothing is pending.
    applied = _applied_cursor(core, journal)
    journal.append_record(_usage_obs(wire, 14.0, "2026-08-02T12:05:00Z"))
    appended = journal.journal_high_water()
    _stamp(core.DB_PATH, core.STATS_INDEX_EPOCH - 1)
    (core.APP_DIR / "stats-epoch-rebuild.pending").touch()

    assert store.cmd_stats_epoch_rebuild_internal(SimpleNamespace()) == 0
    assert store.stats_epoch_rebuild_pending() is False

    conn = core.open_db()
    try:
        percents = sorted(r[0] for r in conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots"))
        cursor = journal._read_cursor(conn)
    finally:
        conn.close()
    assert percents == [12.0, 14.0]
    # The suffix ingest consumed everything that was appended before it ran.
    assert journal._coordinate_covers(cursor, appended)


def test_derivation_frontier_is_trusted_only_from_a_sound_current_index(
    runtime,
):
    """Any doubt about the replaced index's derivation frontier answers None,
    so completion replays from the cutover rather than from somewhere
    unproven; the applied cursor is never consulted (Codex fix-round-1 Q1)."""
    _ns, core, _db, _store = runtime
    import _cctally_journal as journal
    import _lib_journal as wire

    journal.append_record(_usage_obs(wire, 12.0, "2026-08-02T12:00:00Z"))
    journal.run_stats_ingest(mode="authoritative")
    segment, offset = _applied_cursor(core, journal)
    assert journal.read_trusted_derivation_frontier(core.DB_PATH) == (
        segment, offset)

    def with_frontier(sql, params=()):
        conn = sqlite3.connect(core.DB_PATH)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
        return journal.read_trusted_derivation_frontier(core.DB_PATH)

    set_frontier = ("UPDATE journal_derivation_frontier SET segment = ?, "
                    "offset = ? WHERE id = 1")
    assert with_frontier(set_frontier, (segment, offset - 1)) is None
    assert with_frontier(set_frontier, (segment, offset + 1)) is None
    assert with_frontier(
        set_frontier, ("observations-2099-01.jsonl", offset)) is None
    assert with_frontier(set_frontier, (segment, 0)) == (segment, 0)
    # A previous-release epoch never certifies derivation, whatever it holds.
    _stamp(core.DB_PATH, journal.DERIVATION_FRONTIER_EPOCH - 1)
    assert journal.read_trusted_derivation_frontier(core.DB_PATH) is None
    _stamp(core.DB_PATH, core.STATS_INDEX_EPOCH)
    assert with_frontier("DELETE FROM journal_derivation_frontier") is None
    assert journal.read_trusted_derivation_frontier(
        core.APP_DIR / "absent.db") is None


def _subprocess_env(core) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "CCTALLY_DATA_DIR": str(core.APP_DIR),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
    })
    return env


def _epoch_run(store):
    """The epoch worker's recorded run, read without touching its flock.

    `_WorkerRun.begin()` writes the run only after the worker has won its
    flock, so a recorded run is a started worker. (The handshake once polled
    `_stats_epoch_rebuild_worker_active()`, which then try-locked the worker
    flock: a worker whose claim landed in that instant exited `busy`, the
    fresh pending marker suppressed a respawn, and the test waited out its
    backstop. #1012 made the probe lock-free; this reads the run directly.)"""
    return (store.read_worker_state("epoch")["state"] or {}).get("run")


# How long the released worker state must stay ended before `_replay_held`
# returns: longer than a queued launch takes to record its run.
_WORKER_SETTLE_SECONDS = 0.5


@contextlib.contextmanager
def _replay_held(core, store):
    """Hold the stats maintenance lock SHARED, as a live reader does, for the
    body; then release it and wait (bounded) for the deferred worker to end.

    Every replay of the index, the epoch worker's or an inline one, needs that
    lock EXCLUSIVE, so while it is held no replay can publish: a foreground
    that returns inside the body returned while the replay stayed blocked,
    whatever the host's speed (#453; an `elapsed < 3.0` reading of the
    foreground failed under full-suite load). The worker the foreground
    spawned records its run and then waits for the lock, which is the
    "started" half of the handshake (`_await_worker_started`); releasing the
    lock lets it finish, so the test leaves no process behind."""
    core.APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(core.STATS_LOCK_MAINTENANCE_PATH, os.O_RDWR | os.O_CREAT,
                 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

        def worker_ended():
            run = _epoch_run(store)
            return run is None or (
                run.get("status") == "finished"
                and store.process_identity(run.get("pid"))["status"] == "gone")

        # The foreground can queue a second launch behind the first worker's
        # flock. That worker records its own run as soon as the first releases
        # the flock, finds the index current and finishes. Reading the state
        # twice (once to leave the loop, once to assert) failed whenever that
        # run was recorded between the two reads, 1-2 s into a 30 s wait
        # (2 of 60 runs on main under load). Read it once per poll and wait
        # until it has stayed ended for a short settle window, so a queued
        # run is waited for instead of being read as a hung worker.
        deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
        ended_since = None
        while time.monotonic() < deadline:
            if not worker_ended():
                ended_since = None
            elif ended_since is None:
                ended_since = time.monotonic()
            elif time.monotonic() - ended_since >= _WORKER_SETTLE_SECONDS:
                break
            time.sleep(0.05)
        assert ended_since is not None, "the released epoch worker never finished"


def _await_worker_started(core, store):
    """The foreground's deferred worker is running and blocked before any
    replay: it has recorded its run (so it holds the worker flock) and the
    index is still at the old epoch."""
    deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
    while _epoch_run(store) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _epoch_run(store) is not None, (
        "the foreground never started the deferred epoch worker")
    assert store._raw_user_version(core.DB_PATH) == (
        core.STATS_INDEX_EPOCH - 1), "the replay ran while it was held"


def test_statusline_renders_promptly_while_stats_epoch_worker_runs(runtime):
    """A Claude prompt must retain provider truth and never wait for replay."""
    ns, core, _db, store = runtime
    ns["open_db"]().close()
    _stamp(core.DB_PATH, core.STATS_INDEX_EPOCH - 1)
    now = int(time.time())
    payload = json.dumps({
        "model": {"display_name": "Sonnet"},
        "rate_limits": {
            "five_hour": {"used_percentage": 17.0, "resets_at": now + 7200},
            "seven_day": {"used_percentage": 23.0, "resets_at": now + 432000},
        },
    })

    with _replay_held(core, store):
        result = subprocess.run(
            [str(BIN), "statusline"],
            input=payload,
            text=True,
            capture_output=True,
            env=_subprocess_env(core),
            timeout=PRESENCE_BACKSTOP_SECONDS,
        )
        _await_worker_started(core, store)

    assert result.returncode == 0, result.stderr
    assert "17%" in result.stdout and "23%" in result.stdout
    assert len(result.stdout.splitlines()) == 1


@pytest.mark.parametrize(
    "argv",
    (
        ("report",),
        ("blocks",),
        ("diff", "--a", "last-week", "--b", "this-week"),
    ),
)
def test_stats_commands_return_retry_guidance_instead_of_partial_output(
    runtime, argv,
):
    """Broad fallback kernels must not swallow the epoch control signal."""
    ns, core, _db, store = runtime
    ns["open_db"]().close()
    _stamp(core.DB_PATH, core.STATS_INDEX_EPOCH - 1)

    with _replay_held(core, store):
        result = subprocess.run(
            [str(BIN), *argv],
            text=True,
            capture_output=True,
            env=_subprocess_env(core),
            timeout=PRESENCE_BACKSTOP_SECONDS,
        )
        _await_worker_started(core, store)

    assert result.returncode == 3
    assert result.stdout == ""
    assert "rebuild is running in the background; retry shortly" in result.stderr


def test_cli_exits_3_while_a_corruption_heal_runs_in_the_background(runtime):
    """The widened CLI boundary must give retry guidance, not a raw traceback
    and not the pre-cutover "never auto-recreated — run db repair" wording,
    which is false once a worker is running.

    #496 S6 F21 reworded that message, so the negative assertion below tracks
    the CURRENT wording. Asserting the absence of a phrase the product no
    longer emits anywhere would pass by construction."""
    ns, core, _db, _store = runtime
    import _cctally_journal as journal
    import _lib_journal as wire

    ns["open_db"]().close()
    journal.append_record(
        wire.make_obs(
            at="2026-08-02T12:00:00Z",
            src="statusline",
            provider="claude",
            payload={
                "weekly_percent": 12.0,
                "resets_at": int(
                    dt.datetime(2026, 8, 6, tzinfo=dt.timezone.utc).timestamp()
                ),
                "source": "statusline",
                "captured_at": "2026-08-02T12:00:00Z",
            },
        )
    )
    journal.run_stats_ingest(mode="authoritative")
    with open(core.DB_PATH, "r+b") as handle:
        handle.write(b"not a database " * 200)

    env = _subprocess_env(core)
    started = time.monotonic()
    result = subprocess.run(
        [str(BIN), "report"],
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 3, result.stderr
    assert elapsed < 30.0
    assert result.stdout == ""
    assert "corruption rebuild is running in the background" in result.stderr
    assert "never auto-recreated" not in result.stderr
    assert "db repair --db stats --yes" not in result.stderr


@pytest.mark.parametrize("no_sync", (False, True))
def test_dashboard_initial_snapshot_degrades_without_second_stats_open(
    runtime, monkeypatch, no_sync,
):
    """Dashboard bind state must remain serializable while replay is pending.

    #932: the deferred first frame hydrates in BOTH modes. Failure it detects:
    a frozen (`--no-sync`) dashboard over an older-epoch index telling the user
    "Projects data unavailable — restart the dashboard" while the rebuild runs,
    instead of the loading state a syncing dashboard shows for the same
    deferral; and a deferral the frame does not state explicitly, which leaves
    nothing to tell a deferral from a terminal stats failure.
    """
    ns, core, db, store = runtime
    ns["open_db"]().close()
    _stamp(core.DB_PATH, core.STATS_INDEX_EPOCH - 1)
    calls: list[str] = []
    monkeypatch.setattr(
        store,
        "defer_stats_epoch_rebuild",
        lambda: calls.append("defer") or "pending",
    )
    import _cctally_dashboard as dashboard
    import _cctally_tui as tui

    monkeypatch.setattr(
        tui,
        "_tui_precompute_doctor_payload",
        lambda *_args, **_kwargs: pytest.fail(
            "deferred first paint reopened stats through Doctor"
        ),
    )

    snapshot = dashboard._dashboard_initial_snapshot(
        SimpleNamespace(no_sync=no_sync, host="127.0.0.1"),
        pinned_now=dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc),
        display_tz_pref_override=None,
    )

    assert snapshot.hydrating is True
    assert snapshot.last_sync_error is not None
    assert "stats.db index epoch rebuild" in snapshot.last_sync_error
    assert snapshot.doctor_payload is not None
    assert snapshot.doctor_payload["severity"] == "warn"
    assert snapshot.envelope_precompute is not None
    assert calls == ["defer"]
    assert snapshot.sync_failures[0].deferred is True
    assert snapshot.sync_failures[0].deferral_outcome == "pending"
    assert tui._tui_snapshot_is_stats_deferral(snapshot) is True
    envelope = ns["snapshot_to_envelope"](
        snapshot,
        now_utc=dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc),
    )
    # The shape the client's `isHydratingEntry` reads as a loading skeleton,
    # on both provider entries (`all` is their composition, the same on a
    # syncing dashboard's bootstrap frame).
    for name in ("claude", "codex"):
        entry = envelope["sources"][name]
        assert entry["data"] is None, name
        assert entry["last_success_at"] is None, name
        assert entry["warnings"] == [], name
        assert entry["capabilities"] == {}, name


def test_dashboard_background_tick_preserves_typed_hydrating_frame(
    runtime, monkeypatch,
):
    """The immediate pre-bind sync tick must not publish a generic crash.

    #932 G1 also detects: under a pinned clock (``CCTALLY_AS_OF``) the
    deferral frame stamped with the wall clock, so the client's time-order
    guard discards every later pinned frame and an open page never fills."""
    ns, _core, db, _store = runtime
    initial = ns["_empty_dashboard_snapshot"]()
    initial.hydrating = True
    initial.last_sync_error = "stats-open: replay pending"
    ref = ns["_SnapshotRef"](initial)

    class Hub:
        def __init__(self):
            self.published = []

        def publish(self, snapshot):
            self.published.append(snapshot)

    hub = Hub()

    def still_pending(**_kwargs):
        raise db.StatsEpochRebuildDeferred("pending")

    monkeypatch.setitem(ns, "_tui_build_snapshot", still_pending)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref,
        hub=hub,
        pinned_now=dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc),
        display_tz_pref_override=None,
    )
    locked(skip_sync=True)

    published = hub.published[-1]
    assert published.generated_at == dt.datetime(
        2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc)
    assert published.hydrating is True
    assert "stats-open:" in published.last_sync_error
    assert "sync crashed:" not in published.last_sync_error
    assert published.sync_failures[0].database == "stats"
    assert published.sync_failures[0].corruption is False
    envelope = ns["snapshot_to_envelope"](
        published,
        now_utc=dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert envelope["sync_failure"]["kind"] == "maintenance_active"
    assert envelope["sync_failure"]["label"] == "stats index rebuilding"


# ── #932: the explicit deferral fact and its consumers ─────────────────────

_PINNED_932 = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc)


class _RecordingHub932:
    def __init__(self):
        self.published = []

    def publish(self, snapshot):
        self.published.append(snapshot)


def _deferred_frame_932(tui, *, exc):
    """The frame the publisher holds after a thrown deferral (startup shape).

    The hydration fields are stated explicitly so each consumer test below
    depends only on the consumer it names, not on the startup builder.
    """
    import dataclasses
    import _cctally_dashboard as dashboard

    frame = dashboard._dashboard_stats_deferred_snapshot(
        SimpleNamespace(no_sync=True, host="127.0.0.1"),
        pinned_now=_PINNED_932, exc=exc,
    )
    return dataclasses.replace(
        frame, hydrating=True,
        source_bundle=tui._tui_hydrating_source_bundle())


def test_failed_epoch_admission_classifies_as_server_sync_until_admission_succeeds(
    runtime, monkeypatch,
):
    """#932 R4. Failure it detects: an epoch rebuild whose admission FAILED
    (no marker, no worker: nothing is rebuilding) still labelled "stats index
    rebuilding", so the user waits on a rebuild that will never run. The next
    successful admission returns the label to "stats index rebuilding", and a
    heal deferral keeps reading as corruption whatever its outcome."""
    ns, _core, db, _store = runtime
    initial = ns["_empty_dashboard_snapshot"]()
    initial.hydrating = True
    ref = ns["_SnapshotRef"](initial)
    hub = _RecordingHub932()
    raised = []

    def deferred(**_kwargs):
        raise raised[-1]

    monkeypatch.setitem(ns, "_tui_build_snapshot", deferred)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=_PINNED_932,
        display_tz_pref_override=None,
    )

    def kind_after(exc):
        raised.append(exc)
        locked(skip_sync=True)
        published = hub.published[-1]
        assert published.sync_failures[0].deferred is True
        assert published.sync_failures[0].deferral_outcome == exc.outcome
        return ns["snapshot_to_envelope"](
            published, now_utc=_PINNED_932)["sync_failure"]

    failed = kind_after(db.StatsEpochRebuildDeferred("failed"))
    assert failed == {
        "kind": "server_sync",
        "label": "⚠ server sync error",
        "detail": "The server could not complete its background sync.",
        "action": None,
    }
    spawned = kind_after(db.StatsEpochRebuildDeferred("spawned"))
    assert spawned["kind"] == "maintenance_active"
    assert spawned["label"] == "stats index rebuilding"
    heal = kind_after(db.StatsHealDeferred("failed"))
    assert heal["kind"] == "stats_corruption"


def test_returned_deferred_heal_frame_hydrates_without_the_full_doctor(
    runtime, monkeypatch,
):
    """#932 R4/R5. Failure it detects: a heal that defers is RETURNED as a
    frame, not thrown, and today that frame clears the hydration latch and
    runs the full doctor gather, which opens the very index the detached heal
    owns. The frame must be the explicit deferral frame (placeholder doctor,
    hydrating), and the publisher's no-ingest branch must keep its latch."""
    ns, _core, db, _store = runtime
    import _cctally_tui as tui

    monkeypatch.setattr(
        tui, "_tui_build_snapshot_once",
        lambda **_kw: (_ for _ in ()).throw(tui._StatsSnapshotCorruption(
            sqlite3.DatabaseError("database disk image is malformed"))),
    )
    monkeypatch.setattr(
        tui, "_tui_heal_post_query_stats",
        lambda _cause: (_ for _ in ()).throw(db.StatsHealDeferred("spawned")),
    )
    monkeypatch.setattr(
        tui, "_tui_precompute_doctor_payload",
        lambda *_a, **_kw: pytest.fail("deferred heal frame ran the doctor"),
    )

    frame = tui._tui_build_snapshot_impl(
        now_utc=_PINNED_932, skip_sync=True, precompute_envelope=True,
        runtime_bind="127.0.0.1",
    )

    assert frame.hydrating is True
    assert tui._tui_snapshot_is_stats_deferral(frame) is True
    assert frame.sync_failures[0].corruption is True
    assert frame.doctor_payload is not None
    assert tui._tui_is_hydrating_source_bundle(frame.source_bundle) is True

    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = _RecordingHub932()
    monkeypatch.setitem(ns, "_tui_build_snapshot", lambda **_kw: frame)
    ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=_PINNED_932,
        display_tz_pref_override=None,
    )(skip_sync=True)
    assert hub.published[-1].hydrating is True
    assert hub.published[-1].last_sync_at is None


@pytest.mark.parametrize("prior", ("bootstrap", "genuine"))
def test_crash_after_deferral_drops_only_the_bootstrap_bundle(
    runtime, monkeypatch, prior,
):
    """#932 R4. Failure it detects: a generic crash after a deferral keeps the
    hydrating bootstrap bundle, so the client shows loading skeletons forever
    where it should show the crash; while a crash over a genuine prior bundle
    must keep that bundle (its data is still the best the dashboard has).

    #932 G1 also detects: under a pinned clock the crash frame stamped with
    the wall clock, so the client's time-order guard discards every later
    pinned frame."""
    ns, _core, db, _store = runtime
    import dataclasses
    import _cctally_tui as tui

    held = _deferred_frame_932(tui, exc=db.StatsEpochRebuildDeferred("spawned"))
    if prior == "genuine":
        bundle = held.source_bundle
        genuine = dataclasses.replace(bundle, sources={
            name: dataclasses.replace(state, data_version=f"gen:{name}")
            for name, state in bundle.sources.items()
        })
        held = dataclasses.replace(held, source_bundle=genuine)
    ref = ns["_SnapshotRef"](held)
    hub = _RecordingHub932()
    monkeypatch.setitem(
        ns, "_tui_build_snapshot",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("boom")))
    ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=_PINNED_932,
        display_tz_pref_override=None,
    )(skip_sync=True)

    published = hub.published[-1]
    assert published.generated_at == _PINNED_932
    assert published.hydrating is False
    assert "sync crashed:" in published.last_sync_error
    if prior == "genuine":
        assert published.source_bundle is genuine
    else:
        assert published.source_bundle is None
        envelope = ns["snapshot_to_envelope"](published, now_utc=_PINNED_932)
        for name in ("claude", "codex"):
            entry = envelope["sources"][name]
            assert entry["warnings"], (
                f"{name} still serializes as a loading skeleton after a crash")


@pytest.mark.parametrize(("held", "want_hydrating"), (
    ("deferral", True),
    ("populated", False),
))
def test_update_republish_keeps_a_held_deferrals_hydration(
    runtime, monkeypatch, held, want_hydrating,
):
    """#932 R3. Failure it detects: the update-check republish (a version
    self-heal or a due check) clears the hydration latch of a held deferral
    frame, turning the frozen dashboard's loading state back into "Projects
    data unavailable — restart the dashboard" mid-rebuild. Over any other
    frame it still clears the latch, as before."""
    ns, _core, db, _store = runtime
    import threading
    import _cctally_tui as tui

    if held == "deferral":
        frame = _deferred_frame_932(
            tui, exc=db.StatsEpochRebuildDeferred("spawned"))
    else:
        frame = ns["_empty_dashboard_snapshot"]()
        frame.hydrating = True
    ref = ns["_SnapshotRef"](frame)
    hub = _RecordingHub932()
    monkeypatch.setattr(
        tui, "_tui_precompute_doctor_payload",
        lambda *_a, **_kw: {"severity": "ok",
                            "counts": {"ok": 1, "warn": 0, "fail": 0}})
    thread = ns["_DashboardUpdateCheckThread"](
        threading.Event(), hub=hub, snapshot_ref=ref)

    thread._republish_with_fresh_envelope()

    assert hub.published[-1].hydrating is want_hydrating
    assert ref.get().hydrating is want_hydrating


# ── #932: the frozen settle loop ───────────────────────────────────────────


class _VirtualClock932:
    """A virtual clock whose ``wait`` advances time and runs scripted hooks.

    ``wait`` returns True (stop) once ``max_waits`` waits have been taken, so a
    loop that would keep going is observable without running forever.
    """

    def __init__(self, stop, *, max_waits, hooks=None):
        self.t = 0.0
        self.waits = []
        self._stop = stop
        self._max_waits = max_waits
        self._hooks = hooks or {}

    def monotonic(self):
        return self.t

    def wait(self, seconds):
        self.waits.append(seconds)
        self.t += seconds
        hook = self._hooks.get(len(self.waits))
        if hook is not None:
            hook()
        if len(self.waits) > self._max_waits:
            self._stop.set()
        return self._stop.is_set()


_SETTLE_CASES_932 = {
    # name: (continues, builder factory)
    "thrown-epoch-deferral": (True, "epoch"),
    "thrown-heal-deferral": (True, "heal"),
    "returned-heal-deferral": (True, "returned-heal"),
    "populated-success": (False, "populated"),
    "empty-success": (False, "empty"),
    "generic-crash": (False, "crash"),
    "exhausted-corruption-retry": (False, "exhausted"),
}


@pytest.mark.parametrize("case", sorted(_SETTLE_CASES_932))
def test_frozen_settle_loop_outcomes_through_the_real_publisher(
    runtime, monkeypatch, case,
):
    """#932 R4/R7. Failure it detects: a settle loop that spins on a terminal
    failure (a crash, an exhausted corruption retry, an empty success), stops
    on a deferral (thrown or returned) before recovery publishes the index, or
    never refreshes the rebuild-worker state a deferred attempt carries."""
    ns, _core, db, _store = runtime
    import dataclasses
    import threading
    import _cctally_dashboard as dashboard
    import _cctally_tui as tui

    continues, kind = _SETTLE_CASES_932[case]
    initial = _deferred_frame_932(
        tui, exc=db.StatsEpochRebuildDeferred("spawned"))
    ref = ns["_SnapshotRef"](initial)
    hub = _RecordingHub932()
    worker_values = []

    def worker_value(_now, fallback=None):
        worker_values.append({"severity": "warn",
                              "summary": f"worker-{len(worker_values)}"})
        return worker_values[-1]

    monkeypatch.setattr(tui, "_tui_rebuild_worker_value", worker_value)
    monkeypatch.setattr(
        tui, "_tui_precompute_doctor_payload",
        lambda *_a, **_kw: pytest.fail("a deferred attempt ran the doctor"))
    attempts = []

    def build(**kwargs):
        attempts.append(kind)
        empty = ns["_empty_dashboard_snapshot"]()
        if kind == "epoch":
            raise db.StatsEpochRebuildDeferred("spawned")
        if kind == "heal":
            raise db.StatsHealDeferred("spawned")
        if kind == "returned-heal":
            return tui._tui_build_snapshot_impl(**kwargs)
        if kind == "populated":
            return dataclasses.replace(empty, data_version="populated-932")
        if kind == "empty":
            return empty
        if kind == "crash":
            raise RuntimeError("boom")
        return tui._tui_stats_retry_degraded_snapshot(
            now_utc=_PINNED_932,
            exc=sqlite3.DatabaseError("database disk image is malformed"),
            precompute_envelope=False, runtime_bind=None,
        )

    if kind == "returned-heal":
        monkeypatch.setattr(
            tui, "_tui_build_snapshot_once",
            lambda **_kw: (_ for _ in ()).throw(tui._StatsSnapshotCorruption(
                sqlite3.DatabaseError("database disk image is malformed"))))
        monkeypatch.setattr(
            tui, "_tui_heal_post_query_stats",
            lambda _cause: (_ for _ in ()).throw(
                db.StatsHealDeferred("spawned")))
    monkeypatch.setitem(ns, "_tui_build_snapshot", build)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=_PINNED_932,
        display_tz_pref_override=None,
    )
    stop = threading.Event()
    clock = _VirtualClock932(stop, max_waits=3)
    dashboard._frozen_deferral_settle_loop(
        stop=stop, interval=5.0, ref=ref, sync_lock=threading.Lock(),
        run_locked=locked,
        mark_rebuilding=dashboard._make_sync_loop_collaborators(
            ref=ref, hub=hub)["mark_rebuilding"],
        is_deferred=tui._tui_snapshot_is_stats_deferral,
        monotonic=clock.monotonic, wait=clock.wait,
    )

    if continues:
        # Three cadences were allowed; the fourth wait stopped the loop.
        assert len(attempts) == 3, attempts
        assert tui._tui_snapshot_is_stats_deferral(ref.get())
        assert ref.get().hydrating is True
        assert ref.get().doctor_payload["rebuild_worker"] == worker_values[-1]
        assert len({v["summary"] for v in worker_values}) == len(worker_values)
        assert hub.published[-1].doctor_payload["rebuild_worker"] == (
            worker_values[-1])
    else:
        assert len(attempts) == 1, attempts
        assert not stop.is_set(), "a terminal outcome must end the loop itself"
        assert not tui._tui_snapshot_is_stats_deferral(ref.get())


class _HookedLock932:
    """A `threading.Lock` stand-in whose acquire runs a hook first."""

    def __init__(self, before_acquire=None):
        import threading
        self._lock = threading.Lock()
        self.before_acquire = before_acquire
        self.failed_acquires = 0

    def acquire(self, blocking=True, timeout=-1):
        if self.before_acquire is not None:
            self.before_acquire()
        got = self._lock.acquire(blocking, timeout)
        if not got:
            self.failed_acquires += 1
        return got

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()


@pytest.mark.parametrize("boundary", (
    "manual-settle-before-attempt",
    "manual-settle-between-precheck-and-lock",
    "lock-contention",
    "stop-after-precheck",
    "slow-attempt-cooldown",
))
def test_frozen_settle_loop_boundaries(runtime, boundary):
    """#932 R7. Failure it detects: an extra full build after a manual sync
    already settled the frame (before an attempt, or between its precheck and
    its lock), a loop that gives up on lock contention instead of retrying the
    next cadence, a build started after shutdown began, and a busy loop that
    ignores the #313 cooldown after a slow attempt."""
    ns, _core, db, _store = runtime
    import dataclasses
    import threading
    import _cctally_dashboard as dashboard
    import _cctally_tui as tui

    interval = 5.0
    deferred = _deferred_frame_932(
        tui, exc=db.StatsEpochRebuildDeferred("spawned"))
    settled = dataclasses.replace(
        ns["_empty_dashboard_snapshot"](), data_version="settled-932")
    ref = ns["_SnapshotRef"](deferred)
    stop = threading.Event()
    lock = _HookedLock932()
    builds = []
    slow_work = 3 * interval
    hooks = {}

    def run_locked(*, skip_sync):
        assert skip_sync is True
        assert lock.locked(), "the attempt must run under sync_lock"
        builds.append(clock.t)
        if boundary == "slow-attempt-cooldown" and len(builds) == 1:
            clock.t += slow_work      # still deferred after a slow attempt
            return
        ref.set(settled)

    if boundary == "manual-settle-before-attempt":
        hooks[1] = lambda: ref.set(settled)
    elif boundary == "manual-settle-between-precheck-and-lock":
        def settle_once():
            lock.before_acquire = None
            ref.set(settled)
        lock.before_acquire = settle_once
    elif boundary == "lock-contention":
        lock._lock.acquire()           # another publisher holds it
        hooks[2] = lock._lock.release  # ... until the second cadence
    elif boundary == "stop-after-precheck":
        lock.before_acquire = stop.set

    clock = _VirtualClock932(stop, max_waits=4, hooks=hooks)
    dashboard._frozen_deferral_settle_loop(
        stop=stop, interval=interval, ref=ref, sync_lock=lock,
        run_locked=run_locked, mark_rebuilding=None,
        is_deferred=tui._tui_snapshot_is_stats_deferral,
        monotonic=clock.monotonic, wait=clock.wait,
    )

    assert clock.waits[0] == interval, "the first attempt waits one interval"
    if boundary in ("manual-settle-before-attempt",
                    "manual-settle-between-precheck-and-lock",
                    "stop-after-precheck"):
        assert builds == []
    elif boundary == "lock-contention":
        assert lock.failed_acquires == 1
        assert builds == [2 * interval], (
            "contention skips one cadence; the next uncontended one builds")
        assert ref.get().data_version == "settled-932"
    else:
        assert len(builds) == 2
        # _next_deadline: cool down max(interval, work) from the work's end.
        assert clock.waits[1] == max(interval, slow_work)
        assert builds[1] == interval + slow_work + max(interval, slow_work)
    assert not lock.locked()


def test_frozen_settle_retirement_waits_for_a_held_build(runtime):
    """#932 §4.3 step 5. Failure it detects: shutdown retiring the memory
    verifiers and the hub while a settle build is still in flight (a timed
    join returns with the build alive), so its late verifier submission or
    publish lands in retired resources."""
    ns, _core, db, _store = runtime
    import dataclasses
    import threading
    import _cctally_dashboard as dashboard
    import _cctally_tui as tui

    deferred = _deferred_frame_932(
        tui, exc=db.StatsEpochRebuildDeferred("spawned"))
    settled = dataclasses.replace(
        ns["_empty_dashboard_snapshot"](), data_version="settled-932")
    ref = ns["_SnapshotRef"](deferred)
    hub = _RecordingHub932()
    sync_lock = threading.Lock()
    stop = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    order = []

    def run_locked(*, skip_sync):
        entered.set()
        release.wait(PRESENCE_BACKSTOP_SECONDS)
        hub.publish(ref.set(settled))
        order.append("publish")

    thread = dashboard._make_frozen_deferral_settle_thread(
        stop=stop, interval=0.05, ref=ref, hub=hub, sync_lock=sync_lock,
        run_sync_now_locked=run_locked,
    )
    assert thread.daemon and thread.name == "dashboard-frozen-deferral-settle"
    thread.start()
    retirer = None
    try:
        assert entered.wait(PRESENCE_BACKSTOP_SECONDS), "no settle attempt ran"

        def retire():
            dashboard._retire_frozen_deferral_settle(
                stop=stop, sync_lock=sync_lock, thread=thread)
            order.append("retired")

        retirer = threading.Thread(target=retire, daemon=True,
                                   name="932-retirer")
        retirer.start()
        # timing-budget: longer than any join timeout the old design used
        retirer.join(timeout=3.0)
        assert retirer.is_alive(), "retirement returned with the build held"
        assert order == []
    finally:
        release.set()
        if retirer is not None:
            # timing-budget: cleanup join for a thread just released
            retirer.join(timeout=PRESENCE_BACKSTOP_SECONDS)
    assert not retirer.is_alive()
    assert order == ["publish", "retired"]
    assert not thread.is_alive()
    assert not sync_lock.locked()


def test_update_republish_cannot_resurrect_a_superseded_deferral(
    runtime, monkeypatch,
):
    """#932 §4.2 (review P1). Failure it detects: the update-check republish
    patches the held DEFERRED frame, a settle attempt then publishes the
    filled frame, and the republish's publish of the older object lands last:
    the hub's latest delivery (what `/api/data` and every new subscriber read)
    reverts to the deferral for the dashboard's life."""
    ns, _core, db, _store = runtime
    import dataclasses
    import threading
    import _cctally_dashboard as dashboard
    import _cctally_tui as tui

    ref = ns["_SnapshotRef"](_deferred_frame_932(
        tui, exc=db.StatsEpochRebuildDeferred("spawned")))
    hub = dashboard.SSEHub()
    sync_lock = threading.Lock()
    monkeypatch.setattr(
        tui, "_tui_precompute_doctor_payload",
        lambda *_a, **_kw: {"severity": "ok",
                            "counts": {"ok": 1, "warn": 0, "fail": 0}})
    patched = threading.Event()
    release = threading.Event()
    real_replace = ref.replace_fields

    def paused_replace(**changes):
        result = real_replace(**changes)
        patched.set()
        release.wait(PRESENCE_BACKSTOP_SECONDS)
        return result

    monkeypatch.setattr(ref, "replace_fields", paused_replace)
    update = ns["_DashboardUpdateCheckThread"](
        threading.Event(), hub=hub, snapshot_ref=ref, sync_lock=sync_lock)
    republisher = threading.Thread(
        target=update._republish_with_fresh_envelope, daemon=True,
        name="932-republisher")
    builds = []

    def build(**_kwargs):
        builds.append("build")
        return dataclasses.replace(
            ns["_empty_dashboard_snapshot"](), data_version="settled-932")

    monkeypatch.setitem(ns, "_tui_build_snapshot", build)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=_PINNED_932,
        display_tz_pref_override=None,
    )
    observed = {}

    def after_contention():
        observed["builds_while_republish_held"] = len(builds)
        release.set()
        # timing-budget: cleanup join for a thread just released
        republisher.join(timeout=PRESENCE_BACKSTOP_SECONDS)
        observed["republisher_alive"] = republisher.is_alive()

    republisher.start()
    try:
        assert patched.wait(PRESENCE_BACKSTOP_SECONDS), "republish never ran"
        stop = threading.Event()
        clock = _VirtualClock932(stop, max_waits=4,
                                 hooks={2: after_contention})
        dashboard._frozen_deferral_settle_loop(
            stop=stop, interval=5.0, ref=ref, sync_lock=sync_lock,
            run_locked=locked, mark_rebuilding=None,
            is_deferred=tui._tui_snapshot_is_stats_deferral,
            monotonic=clock.monotonic, wait=clock.wait,
        )
    finally:
        release.set()
        # timing-budget: cleanup join for a thread just released
        republisher.join(timeout=PRESENCE_BACKSTOP_SECONDS)

    assert observed == {"builds_while_republish_held": 0,
                        "republisher_alive": False}
    assert builds == ["build"]
    held = ref.get()
    assert held.data_version == "settled-932"
    assert held.hydrating is False
    assert not tui._tui_snapshot_is_stats_deferral(held)
    latest = hub.latest().snapshot
    assert latest.data_version == "settled-932"
    assert not tui._tui_snapshot_is_stats_deferral(latest)
    first = hub.subscribe().get_nowait().snapshot
    assert first.data_version == "settled-932"
    hub.close()


# ── #932: a recovery that holds maintenance through a settle attempt ───────


def _patch_recovery_932(monkeypatch, store, recovery):
    """Stand in for the detached worker's records (never the code under test):
    a confirmed heal request plus a live heal worker, or a live epoch worker,
    or neither."""
    monkeypatch.setattr(
        store, "_read_stats_heal_request",
        lambda: ({"forensicsDisposition": "confirmed"}
                 if recovery == "heal" else None))
    monkeypatch.setattr(
        store, "_stats_heal_worker_active", lambda: recovery == "heal")
    monkeypatch.setattr(
        store, "_stats_epoch_rebuild_worker_active",
        lambda: recovery == "epoch")


@pytest.mark.parametrize("recovery", ("heal", "epoch", "none"))
def test_frozen_settle_loop_waits_out_a_recovery_holding_maintenance(
    runtime, monkeypatch, recovery,
):
    """#932 F1. Failure it detects: a frozen dashboard started over a corrupt
    (or older-epoch) index whose detached recovery holds the stats maintenance
    lock EXCLUSIVE for its whole rebuild. A settle attempt's open times out
    with a plain maintenance-busy error, the publisher files it as a crash
    ("restart the dashboard"), and the loop stops for good — so the dashboard
    never fills after the recovery publishes. With no recovery running, the
    same timeout is a real crash and still ends the loop."""
    ns, core, db, store = runtime
    import threading
    import _cctally_dashboard as dashboard
    import _cctally_tui as tui

    ns["open_db"]().close()   # the readable index the recovery publishes
    monkeypatch.setattr(store, "_STATS_OPEN_MAINTENANCE_WAIT_S", 0.2)
    _patch_recovery_932(monkeypatch, store, recovery)
    monkeypatch.setattr(
        tui, "_tui_rebuild_worker_value",
        lambda _now, fallback=None: {"severity": "warn", "summary": "worker"})
    startup_exc = (db.StatsEpochRebuildDeferred("spawned")
                   if recovery == "epoch" else db.StatsHealDeferred("spawned"))
    ref = ns["_SnapshotRef"](_deferred_frame_932(tui, exc=startup_exc))
    hub = _RecordingHub932()
    attempts = []
    real_build = ns["_tui_build_snapshot"]
    monkeypatch.setitem(
        ns, "_tui_build_snapshot",
        lambda **kw: attempts.append(1) or real_build(**kw))
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=_PINNED_932,
        display_tz_pref_override=None, runtime_bind="127.0.0.1",
    )

    lock_path = pathlib.Path(core.STATS_LOCK_MAINTENANCE_PATH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "a+")
    fcntl.flock(holder, fcntl.LOCK_EX)   # the recovery worker's hold
    observed = {}

    def recovery_publishes():
        observed["during_hold"] = ref.get()
        fcntl.flock(holder, fcntl.LOCK_UN)

    stop = threading.Event()
    clock = _VirtualClock932(stop, max_waits=4,
                             hooks={2: recovery_publishes})
    try:
        dashboard._frozen_deferral_settle_loop(
            stop=stop, interval=5.0, ref=ref, sync_lock=threading.Lock(),
            run_locked=locked, mark_rebuilding=None,
            is_deferred=tui._tui_snapshot_is_stats_deferral,
            monotonic=clock.monotonic, wait=clock.wait,
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    held = ref.get()
    assert not stop.is_set(), "the loop must end by itself"
    if recovery == "none":
        assert len(attempts) == 1, attempts
        assert "during_hold" not in observed
        assert held.hydrating is False
        assert "sync crashed:" in held.last_sync_error
        assert not tui._tui_snapshot_is_stats_deferral(held)
        return
    assert "during_hold" in observed, (
        f"the loop ended during the hold on {held.last_sync_error!r}")
    during = observed["during_hold"]
    assert tui._tui_snapshot_is_stats_deferral(during), (
        "a maintenance-busy attempt under a live recovery left the deferral")
    assert during.hydrating is True
    assert "sync crashed:" not in during.last_sync_error
    attribution = during.sync_failures[0]
    assert attribution.deferral_outcome == "pending"
    assert attribution.corruption is (recovery == "heal")
    assert tui._tui_is_hydrating_source_bundle(during.source_bundle)
    assert during.doctor_payload["rebuild_worker"]["summary"] == "worker"
    assert during.generated_at == _PINNED_932
    assert len(attempts) == 2, attempts
    assert not tui._tui_snapshot_is_stats_deferral(held)
    assert held.hydrating is False
    assert "sync crashed:" not in (held.last_sync_error or "")


def test_maintenance_busy_over_a_populated_frame_keeps_the_crash_carry(
    runtime, monkeypatch,
):
    """#932 F1 boundary. Failure it detects: the recovery classification
    widened past a held deferral, so a populated dashboard whose index starts
    healing mid-life swaps its data for a loading frame instead of keeping
    its prior data with the crash."""
    ns, _core, db, store = runtime
    import dataclasses
    import _cctally_tui as tui

    _patch_recovery_932(monkeypatch, store, "heal")
    held = _deferred_frame_932(tui, exc=db.StatsHealDeferred("spawned"))
    bundle = held.source_bundle
    genuine = dataclasses.replace(bundle, sources={
        name: dataclasses.replace(state, data_version=f"gen:{name}")
        for name, state in bundle.sources.items()
    })
    held = dataclasses.replace(
        ns["_empty_dashboard_snapshot"](), data_version="populated-932",
        source_bundle=genuine)
    ref = ns["_SnapshotRef"](held)
    hub = _RecordingHub932()
    monkeypatch.setitem(
        ns, "_tui_build_snapshot",
        lambda **_kw: (_ for _ in ()).throw(
            db.StatsDbMaintenanceError(store._STATS_OPEN_MAINTENANCE_TIMEOUT_MSG)))
    ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=_PINNED_932,
        display_tz_pref_override=None,
    )(skip_sync=True)

    published = hub.published[-1]
    assert published.hydrating is False
    assert "sync crashed:" in published.last_sync_error
    assert published.source_bundle is genuine
    assert published.data_version == "populated-932"
    assert not tui._tui_snapshot_is_stats_deferral(published)
