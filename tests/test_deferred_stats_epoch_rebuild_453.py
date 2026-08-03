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
        "_spawn_detached",
        lambda command: spawned.append(command) or True,
    )

    assert store.defer_stats_epoch_rebuild() == "spawned"
    assert store.defer_stats_epoch_rebuild() == "pending"
    assert spawned == ["_stats-epoch-rebuild"]


def test_scheduler_failed_spawn_is_immediately_retryable(runtime, monkeypatch):
    """A failed Popen must not leave a fresh marker that wedges all retries."""
    _ns, core, _db, store = runtime
    import _cctally_update

    outcomes = iter((False, True))
    monkeypatch.setattr(
        _cctally_update,
        "_spawn_detached",
        lambda _command: next(outcomes),
    )

    assert store.defer_stats_epoch_rebuild() == "failed"
    assert not (core.APP_DIR / "stats-epoch-rebuild.pending").exists()
    assert store.defer_stats_epoch_rebuild() == "spawned"


def test_scheduler_suppresses_launch_while_long_worker_holds_flock(
    runtime, monkeypatch,
):
    """An active replay outliving the marker TTL must not spawn a duplicate."""
    _ns, core, _db, store = runtime
    import _cctally_update

    core.APP_DIR.mkdir(parents=True, exist_ok=True)
    marker = core.APP_DIR / "stats-epoch-rebuild.pending"
    marker.touch()
    stale = time.time() - store._STATS_EPOCH_REBUILD_RETRY_SECONDS - 1
    os.utime(marker, (stale, stale))
    lock_path = core.APP_DIR / "stats-epoch-rebuild.worker.lock"
    held = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    spawned: list[str] = []
    monkeypatch.setattr(
        _cctally_update,
        "_spawn_detached",
        lambda command: spawned.append(command) or True,
    )
    try:
        assert store.defer_stats_epoch_rebuild() == "pending"
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    assert spawned == []
    assert time.time() - marker.stat().st_mtime < 5


def test_spawn_failure_message_does_not_claim_worker_is_running(runtime):
    """Failed admission must give explicit, truthful retry guidance."""
    _ns, _core, db, _store = runtime
    exc = db.StatsEpochRebuildDeferred("failed")
    assert "could not start" in str(exc)
    assert "running in the background" not in str(exc)


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
    """The worker flock is the final defense against concurrent replay."""
    _ns, core, _db, store = runtime
    core.APP_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = core.APP_DIR / "stats-epoch-rebuild.worker.lock"
    held = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(
        store,
        "resolve_stats_epoch_mismatch",
        lambda: pytest.fail("duplicate worker entered the resolver"),
    )
    try:
        assert store.cmd_stats_epoch_rebuild_internal(SimpleNamespace()) == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)


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


def _subprocess_env(core) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "CCTALLY_DATA_DIR": str(core.APP_DIR),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
    })
    return env


def test_statusline_renders_promptly_while_stats_epoch_worker_runs(runtime):
    """A Claude prompt must retain provider truth and never wait for replay."""
    ns, core, _db, _store = runtime
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

    started = time.monotonic()
    result = subprocess.run(
        [str(BIN), "statusline"],
        input=payload,
        text=True,
        capture_output=True,
        env=_subprocess_env(core),
        timeout=5,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert elapsed < 3.0
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
    ns, core, _db, _store = runtime
    ns["open_db"]().close()
    _stamp(core.DB_PATH, core.STATS_INDEX_EPOCH - 1)

    started = time.monotonic()
    result = subprocess.run(
        [str(BIN), *argv],
        text=True,
        capture_output=True,
        env=_subprocess_env(core),
        timeout=5,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 3
    assert elapsed < 3.0
    assert result.stdout == ""
    assert "rebuild is running in the background; retry shortly" in result.stderr


@pytest.mark.parametrize(
    ("no_sync", "want_hydrating"),
    ((False, True), (True, False)),
)
def test_dashboard_initial_snapshot_degrades_without_second_stats_open(
    runtime, monkeypatch, no_sync, want_hydrating,
):
    """Dashboard bind state must remain serializable while replay is pending."""
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

    assert snapshot.hydrating is want_hydrating
    assert snapshot.last_sync_error is not None
    assert "stats.db index epoch rebuild" in snapshot.last_sync_error
    assert snapshot.doctor_payload is not None
    assert snapshot.doctor_payload["severity"] == "warn"
    assert snapshot.envelope_precompute is not None
    assert calls == ["defer"]


def test_dashboard_background_tick_preserves_typed_hydrating_frame(
    runtime, monkeypatch,
):
    """The immediate pre-bind sync tick must not publish a generic crash."""
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
