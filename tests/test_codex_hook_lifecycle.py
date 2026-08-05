"""Codex Stop/SubagentStop lifecycle orchestration contracts for #294 S2."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import multiprocessing as mp
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import load_script, redirect_paths
from _lib_codex_hooks import (
    acquire_due_lifecycle_locks,
    codex_hook_roots,
    release_lifecycle_locks,
)
from _lib_source_identity import source_root_key


def _hook_args(*, source: str = "codex") -> argparse.Namespace:
    return argparse.Namespace(
        explain=False,
        foreground=True,
        no_oauth=False,
        throttle_seconds=None,
        event=None,
        mock_oauth_response=None,
        source=source,
    )


def _root_key(path: Path) -> str:
    return source_root_key(str(path.resolve()))


def _hold_ingest_lock(lock_path: str, ready, release) -> None:
    """Model the quota verifier's locked projection apply in another process."""
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ready.set()
        release.wait(5.0)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    first = tmp_path / "codex-a"
    second = tmp_path / "codex-b"
    (first / "sessions").mkdir(parents=True)
    (second / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", f"{first},{second}")
    monkeypatch.setitem(
        ns,
        "_hook_tick_read_stdin_event",
        lambda: {
            "event": "Stop", "session_id": "codex-tick",
            "transcript_path": "", "cwd": "",
        },
    )
    return ns, first, second


def test_codex_foreground_tick_syncs_all_roots_but_alerts_only_due_root(
    runtime, monkeypatch, capsys,
):
    """One due root performs one all-root sync; a fresh root only reports."""
    ns, first, second = runtime
    first_key, second_key = _root_key(first), _root_key(second)
    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    marker_dir.mkdir(parents=True)
    (marker_dir / f"{second_key}.last-success").touch()
    calls: list[tuple] = []

    class Cache:
        def close(self):
            calls.append(("cache-close",))

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns,
        "sync_codex_cache",
        lambda conn, *, lock_timeout, **_budget: calls.append(("sync", lock_timeout))
        or SimpleNamespace(lock_contended=False),
    )
    monkeypatch.setitem(
        ns,
        "reconcile_codex_quota_projection",
        lambda *, source_root_keys, alert_eligible_root_keys, now=None,
        full_pass="inline": calls.append(
            ("reconcile", tuple(source_root_keys),
             tuple(alert_eligible_root_keys), full_pass)
        ) or SimpleNamespace(
            blocks_upserted=0, milestones_upserted=0,
            blocks_orphaned=0, milestones_orphaned=0, alerts_dispatched=0,
        ),
    )
    monkeypatch.setitem(
        ns,
        "maybe_record_codex_budget_milestone",
        lambda saved, **kwargs: calls.append(("budget", saved)) or 0,
    )

    assert ns["cmd_hook_tick"](_hook_args()) == 0

    assert calls[0] == ("sync", 0)
    reconcile = next(call for call in calls if call[0] == "reconcile")
    assert reconcile[1] == tuple(sorted((first_key, second_key)))
    assert reconcile[2] == (first_key,)
    assert reconcile[3] == "defer", (
        "the hook must hand EVERY whole-history quota pass to a detached "
        "worker rather than run one on the blocking path")
    assert [call[0] for call in calls].count("budget") == 1
    assert (marker_dir / f"{first_key}.last-success").is_file()
    assert (marker_dir / f"{second_key}.last-success").is_file()
    assert capsys.readouterr().out == capsys.readouterr().err == ""


def test_codex_tick_cache_contention_is_silent_and_touches_no_marker(
    runtime, monkeypatch, capsys,
):
    """A contended shared Codex cache lock is a successful no-op."""
    ns, first, second = runtime
    keys = {_root_key(first), _root_key(second)}
    calls: list[str] = []

    class Cache:
        def close(self):
            calls.append("cache-close")

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns,
        "sync_codex_cache",
        lambda conn, *, lock_timeout, **_budget: calls.append("sync")
        or SimpleNamespace(lock_contended=True),
    )
    monkeypatch.setitem(
        ns,
        "reconcile_codex_quota_projection",
        lambda **kwargs: calls.append("reconcile"),
    )
    monkeypatch.setitem(
        ns,
        "maybe_record_codex_budget_milestone",
        lambda saved, **kwargs: calls.append("budget") or 0,
    )

    assert ns["cmd_hook_tick"](_hook_args()) == 0

    assert calls == ["sync", "cache-close"]
    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    assert not any((marker_dir / f"{key}.last-success").exists() for key in keys)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_codex_tick_retries_budget_after_quota_worker_holds_ingest_lock(
    runtime, monkeypatch, capsys,
):
    """A verifier overlap must not acknowledge away the budget-alert retry.

    The detached quota verifier spends its observation load outside the ingest
    lock, then holds that lock while applying its projection.  A following hook
    tick can therefore reach the separate authoritative Codex-budget cycle
    while the verifier owns the lock.  The authoritative API reports that
    timeout as ``ran=False``; the hook must leave its lifecycle markers due so
    the first tick after the verifier commits evaluates the budget leg.
    """
    ns, _first, _second = runtime
    ns["open_db"]().close()
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-15T12:00:00Z")
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(json.dumps({
        "display": {"tz": "utc"},
        "budget": {"codex": {
            "amount_usd": 100.0,
            "period": "calendar-month",
            "alerts_enabled": True,
            "alert_thresholds": [90],
        }},
    }) + "\n")
    monkeypatch.setitem(
        ns, "_sum_codex_cost_for_range",
        lambda start, end, *, speed="auto": 100.0,
    )
    monkeypatch.setitem(
        ns, "_dispatch_alert_notification",
        lambda payload, *, mode="real", **kwargs: "queued",
    )

    class Cache:
        def close(self):
            pass

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns, "sync_codex_cache",
        lambda conn, *, lock_timeout, **_budget: SimpleNamespace(
            lock_contended=False),
    )
    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda **kwargs: SimpleNamespace(
            blocks_upserted=0, milestones_upserted=0,
            blocks_orphaned=0, milestones_orphaned=0,
            alerts_dispatched=0),
    )

    import _cctally_journal

    real_ingest = _cctally_journal.run_stats_ingest

    def short_authoritative_wait(*, mode="opportunistic", **kwargs):
        return real_ingest(mode=mode, timeout_s=0.05, **kwargs)

    monkeypatch.setattr(
        _cctally_journal, "run_stats_ingest", short_authoritative_wait)

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(
        target=_hold_ingest_lock,
        args=(str(_cctally_core.JOURNAL_INGEST_LOCK_PATH), ready, release),
    )
    holder.start()
    try:
        assert ready.wait(5.0), "quota-worker lock holder never became ready"
        assert ns["cmd_hook_tick"](_hook_args()) == 0
        marker_dir = ns["APP_DIR"] / "codex-hook-tick"
        first_markers = tuple(marker_dir.glob("*.last-success"))
        first_log = (ns["APP_DIR"] / "logs" / "hook-tick.log").read_text()
        conn = ns["open_db"]()
        try:
            first_budget_rows = conn.execute(
                "SELECT threshold FROM budget_milestones "
                "WHERE vendor='codex' ORDER BY threshold"
            ).fetchall()
        finally:
            conn.close()
    finally:
        release.set()
        holder.join(10.0)
        assert holder.exitcode == 0

    assert ns["cmd_hook_tick"](_hook_args()) == 0
    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    final_markers = tuple(marker_dir.glob("*.last-success"))
    log = (ns["APP_DIR"] / "logs" / "hook-tick.log").read_text()
    conn = ns["open_db"]()
    try:
        final_budget_rows = conn.execute(
            "SELECT threshold FROM budget_milestones "
            "WHERE vendor='codex' ORDER BY threshold"
        ).fetchall()
    finally:
        conn.close()

    assert first_markers == (), (
        "the contended tick acknowledged its lifecycle markers, so the next "
        "tick cannot retry the skipped Codex-budget evaluation")
    assert first_budget_rows == []
    assert "sync=contended" in first_log and "result=noop" in first_log
    assert len(final_markers) == 2, (
        "the first uncontended tick did not complete and acknowledge both roots")
    assert [row["threshold"] for row in final_budget_rows] == [90]
    assert "result=success" in log
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_codex_tick_hands_a_declined_byte_zero_replay_to_a_drain_worker(
    runtime, monkeypatch, capsys,
):
    """A budgeted tick cannot run the replay, and on a hook-only install
    nothing else ever will — no dashboard, no `codex quota`, no `cache-sync`.
    Before the hand-off, every tick from cache migration 035 onwards returned at
    the decline and Codex ingest froze permanently while the lifecycle line, the
    ingest-backlog leg and the dashboard envelope all reported a drained store.
    """
    ns, _first, _second = runtime
    calls: list[str] = []
    spawned: list[str] = []

    class Cache:
        def close(self):
            calls.append("cache-close")

    import _cctally_cache as cache_mod
    import _cctally_update
    monkeypatch.setattr(
        _cctally_update, "_spawn_detached",
        lambda command: spawned.append(command) or True)
    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns, "sync_codex_cache",
        lambda conn, *, lock_timeout, **_budget: calls.append("sync")
        or SimpleNamespace(
            lock_contended=False, deferred_reason="replay_pending",
            backlog_files=0),
    )
    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda **kwargs: calls.append("reconcile") or SimpleNamespace(
            blocks_upserted=0, milestones_upserted=0, alerts_dispatched=0),
    )
    monkeypatch.setitem(
        ns, "maybe_record_codex_budget_milestone",
        lambda saved, **kwargs: calls.append("budget") or 0,
    )

    assert ns["cmd_hook_tick"](_hook_args()) == 0

    assert spawned == [cache_mod.CODEX_REPLAY_DRAIN_COMMAND], (
        "the tick declined the replay and handed the drain to nobody — the "
        "install is frozen and every surface reports fine")
    line = (ns["APP_DIR"] / "logs" / "hook-tick.log").read_text()
    assert "sync=deferred" in line
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_codex_tick_records_what_the_blanket_except_swallowed(
    runtime, monkeypatch, capsys,
):
    """A `result=error` tick with nothing else recorded is undiagnosable.

    This already cost a debugging round: a changed keyword signature raised
    TypeError inside the hook's blanket except and presented as a silent error
    tick indistinguishable from a database failure. Class and message only — no
    traceback, and stdout/stderr stay contractually silent.
    """
    ns, _first, _second = runtime

    class Cache:
        def close(self):
            pass

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())

    def _boom(conn, **kwargs):
        raise TypeError("sync_codex_cache() got an unexpected keyword 'nope'")

    monkeypatch.setitem(ns, "sync_codex_cache", _boom)

    assert ns["cmd_hook_tick"](_hook_args()) == 0

    log = (ns["APP_DIR"] / "logs" / "hook-tick.log").read_text()
    assert "result=error" in log
    assert "error=TypeError: sync_codex_cache() got an unexpected keyword" in log
    assert "Traceback" not in log
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_the_lifecycle_line_keeps_its_fixed_columns_parseable(runtime):
    """`error` is appended LAST and collapsed to one line, so a multi-line
    message cannot split the record or shift a fixed column."""
    import _cctally_record

    line = _cctally_record._codex_lifecycle_log_line(
        source_root_key="rk", event="Stop", sync="error", result="error",
        blocks=0, milestones=0, alert_eligible_roots=1, quota_alerts=0,
        budget_alerts=0, dur_ms=7, error="ValueError: one\ntwo\n  three")
    assert line.count("\n") == 0
    assert " result=error error=ValueError: one two three" in line
    assert line.index("dur_ms=") < line.index("error=")

    # Absent by default: the ordinary success line is byte-unchanged.
    plain = _cctally_record._codex_lifecycle_log_line(
        source_root_key="rk", event="Stop", sync="ok", result="success",
        blocks=0, milestones=0, alert_eligible_roots=1, quota_alerts=0,
        budget_alerts=0, dur_ms=7)
    assert plain.endswith("result=success")


def test_a_free_text_error_cannot_override_a_fixed_column(runtime):
    """Last position does not protect the fixed columns — it endangers them.

    The line's only real reader is a LAST-WINS dict comprehension over
    whitespace tokens (`_parse_codex_lifecycle_log`), so a `k=v` substring
    inside an exception message beats the real field, and being last on the
    line is exactly what makes it win.
    """
    import _cctally_core
    import _cctally_doctor
    import _cctally_record

    ns, _first, _second = runtime
    line = _cctally_record._codex_lifecycle_log_line(
        source_root_key="rk", event="Stop", sync="error", result="error",
        blocks=0, milestones=0, alert_eligible_roots=1, quota_alerts=0,
        budget_alerts=0, dur_ms=7,
        error="ValueError: expected result=success provider=claude dur_ms=0")
    assert "expected" in line, "the message was discarded rather than defused"

    log = _cctally_core.HOOK_TICK_LOG_PATH
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(line + "\n", encoding="utf-8")
    records = _cctally_doctor._codex_lifecycle_activity_24h(
        root_keys={"rk"}, now_utc=dt.datetime.now(dt.timezone.utc))

    assert "rk" in records, (
        "an exception message overrode the record's own `provider` field, so "
        "the errored tick vanished from doctor's Codex lifecycle view entirely")
    assert records["rk"]["error_count_24h"] == 1, (
        "an exception message overrode the record's own `result` field, so an "
        "errored tick is counted as a success")
    assert records["rk"]["success_count_24h"] == 0


def test_the_error_field_never_carries_a_filesystem_path(runtime):
    """The docstring promises "only the bounded event label, opaque source root
    key, aggregate counts and duration" — and the whole `OSError` family breaks
    that promise through `str(exc)`, which embeds `filename`. A single
    `PermissionError` on a rollout puts a username AND a conversation UUID into
    a durable log. A traceback was already rejected for carrying paths; this
    carries the same thing without one.
    """
    import _cctally_record

    exc = PermissionError(
        13, "Permission denied",
        "/Users/someone/.codex/sessions/2026/07/31/"
        "rollout-2026-07-31T09-00-00-0199aa11-bb22-cc33-dd44-ee55ff667788.jsonl")
    line = _cctally_record._codex_lifecycle_log_line(
        source_root_key="rk", event="Stop", sync="error", result="error",
        blocks=0, milestones=0, alert_eligible_roots=1, quota_alerts=0,
        budget_alerts=0, dur_ms=7,
        error=_cctally_record._hook_log_error_detail(exc))

    assert "someone" not in line
    assert "0199aa11" not in line
    assert "/Users/" not in line
    assert ".jsonl" not in line
    assert "PermissionError" in line, "the class must survive; it is the whole "\
        "reason the field exists"
    assert "Permission denied" in line


def test_a_conversation_id_is_redacted_outside_absolute_path_form(runtime):
    """The path rule alone does not make the promise true.

    It matches only an ABSOLUTE or `~`-relative root, because the negative
    lookbehind that keeps `Input/output error` intact also refuses every
    separator in a RELATIVE path — each one has a word character in front of it.
    The `OSError` narrowing hides that for the one family whose `filename` it
    drops, but these callers are blanket `except` blocks: any OTHER type quoting
    a rollout relatively, and a bare conversation key in one of our own
    `ValueError(f"… {key}")` messages, walked straight into the durable log.
    Widening the path rule to reach them is the same edit that starts eating
    prose, so the identifier gets its own rule: a canonical UUID cannot occur in
    prose.
    """
    import _cctally_record

    relative = _cctally_record._hook_log_error_detail(
        RuntimeError(
            "torn rollout .codex/sessions/2026/07/31/"
            "rollout-2026-07-31T09-00-00-0199aa11-bb22-cc33-dd44-ee55ff667788"
            ".jsonl"))
    assert "0199aa11" not in relative, (
        "a relative rollout path on a non-OSError type carried the "
        "conversation id into the log")

    bare = _cctally_record._hook_log_error_detail(
        ValueError("unknown conversation 0199aa11-bb22-cc33-dd44-ee55ff667788"))
    assert "0199aa11" not in bare, (
        "a bare conversation key carried the conversation id into the log")
    assert "unknown conversation" in bare, (
        "the diagnostic half was scrubbed along with the identifier")


def test_a_pathless_error_is_left_readable(runtime):
    """Defusing must not gut the diagnostic — the field exists because a bare
    `result=error` already cost a debugging round."""
    import _cctally_record

    detail = _cctally_record._hook_log_error_detail(
        TypeError("sync_codex_cache() got an unexpected keyword 'nope'"))
    assert detail == (
        "TypeError: sync_codex_cache() got an unexpected keyword 'nope'")
    # `I/O` is not a path, and scrubbing must not treat it as one.
    assert _cctally_record._hook_log_error_detail(
        OSError(5, "Input/output error")) == "OSError: [Errno 5] Input/output error"


def test_codex_tick_defers_a_pending_stats_epoch_rebuild(
    runtime, monkeypatch, capsys,
):
    """The one unbounded operation `defer` and the ingest budget cannot reach.

    An index-epoch bump makes the first `open_db()` after an upgrade replay the
    whole journal, and that happens before any of the hook's own bounding gets
    a say. Measured on a real 211K-observation / 1,859-rollout store the first
    post-bump tick cost 82.05s wall, 76.45s of it the rebuild — against Codex's
    30-second kill, after which nothing is committed and the next tick repeats
    it. The tick must hand the rebuild off and do nothing else.
    """
    ns, _first, _second = runtime
    calls: list[str] = []
    spawned: list[str] = []

    import _cctally_store as store
    import _cctally_update
    monkeypatch.setattr(
        _cctally_update, "_spawn_detached",
        lambda command: spawned.append(command) or True)
    monkeypatch.setitem(
        ns, "open_cache_db", lambda: calls.append("open-cache") or None)
    monkeypatch.setitem(
        ns, "sync_codex_cache",
        lambda conn, **kwargs: calls.append("sync") or SimpleNamespace(
            lock_contended=False),
    )
    import _cctally_record
    monkeypatch.setattr(
        _cctally_record, "_stats_epoch_rebuild_pending", lambda: True)

    assert ns["cmd_hook_tick"](_hook_args()) == 0

    assert calls == [], (
        "the tick opened the cache and ran the sync behind a pending epoch "
        "rebuild, so `open_db()` was about to replay the whole journal inline")
    assert spawned == [store.STATS_EPOCH_REBUILD_COMMAND]
    log = (ns["APP_DIR"] / "logs" / "hook-tick.log").read_text()
    assert "sync=deferred" in log and "result=noop" in log
    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    assert not any(marker_dir.glob("*.last-success")), (
        "a deferred tick must not consume the lifecycle throttle — the next "
        "Codex turn has to re-check whether the rebuild landed")
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_the_epoch_probe_only_fires_on_a_readable_wrong_epoch_index(
    runtime, monkeypatch, tmp_path,
):
    """A missing index is a fresh install (cheap to build, and skipping it
    would leave the hook with nothing to do forever); an unreadable one belongs
    to the corruption auto-heal; a legacy one takes the migration route."""
    import sqlite3
    import _cctally_core
    import _cctally_record

    path = ns_path = tmp_path / "epoch-probe.db"
    monkeypatch.setattr(_cctally_core, "DB_PATH", ns_path)
    assert _cctally_record._stats_epoch_rebuild_pending() is False

    def _stamp(version: int) -> None:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        finally:
            conn.close()

    _stamp(_cctally_core.STATS_INDEX_EPOCH)
    assert _cctally_record._stats_epoch_rebuild_pending() is False
    _stamp(_cctally_core.LEGACY_STATS_HEAD)
    assert _cctally_record._stats_epoch_rebuild_pending() is False
    _stamp(_cctally_core.STATS_INDEX_EPOCH - 1)
    assert _cctally_record._stats_epoch_rebuild_pending() is True
    path.write_bytes(b"not a database at all")
    assert _cctally_record._stats_epoch_rebuild_pending() is False


def test_codex_tick_excludes_a_contended_root_from_alerts_but_not_reporting(
    runtime, monkeypatch,
):
    """A per-root lifecycle lock narrows claims without narrowing the S1 sync."""
    ns, first, second = runtime
    first_key, second_key = _root_key(first), _root_key(second)
    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    marker_dir.mkdir(parents=True)
    held_fd = os.open(marker_dir / f"{second_key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    calls: list[tuple] = []

    class Cache:
        def close(self):
            calls.append(("cache-close",))

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns, "sync_codex_cache",
        lambda conn, *, lock_timeout, **_budget: calls.append(("sync", lock_timeout))
        or SimpleNamespace(lock_contended=False),
    )
    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda *, source_root_keys, alert_eligible_root_keys, now=None,
        full_pass="inline": calls.append(
            ("reconcile", tuple(source_root_keys),
             tuple(alert_eligible_root_keys), full_pass)
        ) or SimpleNamespace(
            blocks_upserted=0, milestones_upserted=0,
            blocks_orphaned=0, milestones_orphaned=0, alerts_dispatched=0,
        ),
    )
    monkeypatch.setitem(
        ns, "maybe_record_codex_budget_milestone", lambda saved, **kwargs: 0,
    )
    try:
        assert ns["cmd_hook_tick"](_hook_args()) == 0
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)

    reconcile = next(call for call in calls if call[0] == "reconcile")
    assert reconcile[1] == tuple(sorted((first_key, second_key)))
    assert reconcile[2] == (first_key,)
    assert (marker_dir / f"{first_key}.last-success").is_file()
    assert not (marker_dir / f"{second_key}.last-success").exists()


def test_codex_tick_failure_leaves_every_due_marker_unmodified(runtime, monkeypatch):
    """Sync/projection/budget failure can never partially acknowledge a root."""
    ns, first, second = runtime

    class Cache:
        def close(self):
            pass

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns,
        "sync_codex_cache",
        lambda conn, *, lock_timeout, **_budget: SimpleNamespace(lock_contended=False),
    )

    def fail_projection(**_kwargs):
        raise RuntimeError("projection boom")

    monkeypatch.setitem(ns, "reconcile_codex_quota_projection", fail_projection)
    assert ns["cmd_hook_tick"](_hook_args()) == 0

    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    assert not (marker_dir / f"{_root_key(first)}.last-success").exists()
    assert not (marker_dir / f"{_root_key(second)}.last-success").exists()


def test_codex_tick_budget_evaluation_failure_leaves_markers_unmodified(
    runtime, monkeypatch,
):
    """The strict lifecycle observes budget-core failures before acknowledging."""
    ns, first, second = runtime

    class Cache:
        def close(self):
            pass

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns, "sync_codex_cache",
        lambda conn, *, lock_timeout, **_budget: SimpleNamespace(lock_contended=False),
    )
    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda **kwargs: SimpleNamespace(
            blocks_upserted=0, milestones_upserted=0,
            blocks_orphaned=0, milestones_orphaned=0, alerts_dispatched=0,
        ),
    )
    ns["open_db"]().close()
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(json.dumps({
        "display": {"tz": "utc"},
        "budget": {"codex": {
            "amount_usd": 100.0,
            "period": "calendar-month",
            "alerts_enabled": True,
            "alert_thresholds": [90],
        }},
    }) + "\n")

    def fail_budget_sum(*_args, **_kwargs):
        raise RuntimeError("budget boom")

    monkeypatch.setitem(ns, "_sum_codex_cost_for_range", fail_budget_sum)
    assert ns["cmd_hook_tick"](_hook_args()) == 0

    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    assert not (marker_dir / f"{_root_key(first)}.last-success").exists()
    assert not (marker_dir / f"{_root_key(second)}.last-success").exists()
    log = (ns["APP_DIR"] / "logs" / "hook-tick.log").read_text()
    assert "result=error" in log
    assert "result=success" not in log


@pytest.mark.parametrize(
    ("quota_alerts", "budget_alerts"),
    [(0, 0), (2, 1)],
)
def test_codex_lifecycle_logs_alert_counts_separately_from_eligibility(
    runtime, monkeypatch, quota_alerts, budget_alerts,
):
    ns, first, second = runtime

    class Cache:
        def close(self):
            pass

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns, "sync_codex_cache",
        lambda conn, *, lock_timeout, **_budget: SimpleNamespace(lock_contended=False),
    )
    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda **kwargs: SimpleNamespace(
            blocks_upserted=1, milestones_upserted=1,
            blocks_orphaned=0, milestones_orphaned=0,
            alerts_dispatched=quota_alerts,
        ),
    )
    monkeypatch.setitem(
        ns, "maybe_record_codex_budget_milestone",
        lambda _saved, **kwargs: budget_alerts,
    )

    assert ns["cmd_hook_tick"](_hook_args()) == 0
    log = (ns["APP_DIR"] / "logs" / "hook-tick.log").read_text()
    assert "alert_eligible_roots=2" in log
    assert f"quota_alerts={quota_alerts}" in log
    assert f"budget_alerts={budget_alerts}" in log


def test_codex_lifecycle_throttle_boundary_is_due_at_exactly_fifteen_seconds(runtime):
    """A 14.999s marker is fresh; the 15.0s boundary is eligible."""
    ns, first, _second = runtime
    root = codex_hook_roots([first])[0]
    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    marker_dir.mkdir(parents=True)
    marker_path = marker_dir / f"{root.source_root_key}.last-success"
    marker_path.touch()
    now = 1_000_000.0

    os.utime(marker_path, (now - 14.999, now - 14.999))
    assert acquire_due_lifecycle_locks(ns["APP_DIR"], [root], now=now) == []

    os.utime(marker_path, (now - 15.0, now - 15.0))
    locks = acquire_due_lifecycle_locks(ns["APP_DIR"], [root], now=now)
    try:
        assert [lock.root.source_root_key for lock in locks] == [root.source_root_key]
    finally:
        release_lifecycle_locks(locks)


def test_hook_tick_source_parser_is_explicit_and_default_stays_claude(runtime):
    ns, _first, _second = runtime
    parser = ns["build_parser"]()
    assert parser.parse_args(["hook-tick", "--foreground", "--source", "codex"]).source == "codex"
    assert parser.parse_args(["hook-tick", "--foreground"]).source == "claude"
    with pytest.raises(SystemExit):
        parser.parse_args(["hook-tick", "--source", "not-a-provider"])


def test_codex_tick_drains_stdin_before_discovering_roots(runtime, monkeypatch):
    """The native handler's payload is consumed before any lifecycle work."""
    ns, _first, _second = runtime
    order: list[str] = []
    record = sys.modules["_cctally_record"]
    monkeypatch.setattr(record, "_hook_tick_read_stdin_event", lambda: order.append("stdin"))
    monkeypatch.setattr(record, "_codex_lifecycle_roots", lambda: order.append("roots") or [])

    assert ns["cmd_hook_tick"](_hook_args()) == 0
    assert order == ["stdin", "roots"]


# --------------------------------------------------------------------------
# #341 Task 2 Step 7: throttle marker keys by (source_root_key, account_key).
# --------------------------------------------------------------------------

import base64  # noqa: E402
from _lib_codex_hooks import mark_lifecycle_success  # noqa: E402
import _lib_accounts as _accts  # noqa: E402


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")


def _codex_auth(account_id: str, email: str) -> str:
    id_token = (
        f"{_b64({'alg': 'RS256'})}."
        f"{_b64({'email': email, 'https://api.openai.com/auth': {'chatgpt_account_id': account_id}})}."
        "sig"
    )
    return json.dumps({"tokens": {"id_token": id_token}})


def test_codex_throttle_marker_keys_by_account_switch_bypasses(tmp_path, monkeypatch):
    """A mid-interval account switch bypasses the prior account's throttle: the
    marker keys by (source_root_key, account_key), so the new account's marker is
    absent and the first post-switch tick is due (spec §1)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    home = tmp_path / "codex-shared"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    [root] = codex_hook_roots([home])
    app_dir = ns["_cctally_core"].APP_DIR
    now = 1000.0

    # Account A active -> due, marker carries A's key.
    (home / "auth.json").write_text(_codex_auth("acct-a", "a@x.com"))
    key_a = _accts.account_key("codex", "acct-a\0a@x.com")
    locks = acquire_due_lifecycle_locks(app_dir, [root], now=now)
    assert [lk.root.source_root_key for lk in locks] == [root.source_root_key]
    assert locks[0].marker_path.name == f"{root.source_root_key}.{key_a}.last-success"
    mark_lifecycle_success(locks)
    release_lifecycle_locks(locks)

    # Same account A within the throttle window -> suppressed.
    assert acquire_due_lifecycle_locks(app_dir, [root], now=now + 1.0) == []

    # Switch to account B within the same window -> NOT throttled (B's marker
    # is absent), so the first post-switch tick observes the new account.
    (home / "auth.json").write_text(_codex_auth("acct-b", "b@x.com"))
    key_b = _accts.account_key("codex", "acct-b\0b@x.com")
    locks_b = acquire_due_lifecycle_locks(app_dir, [root], now=now + 2.0)
    try:
        assert [lk.root.source_root_key for lk in locks_b] == [root.source_root_key]
        assert locks_b[0].marker_path.name == (
            f"{root.source_root_key}.{key_b}.last-success")
    finally:
        release_lifecycle_locks(locks_b)


def test_codex_throttle_marker_unattributed_keeps_legacy_name(tmp_path, monkeypatch):
    """No auth.json (api-key / no identity) keeps the byte-stable legacy marker
    name (no account suffix) — single-account / no-auth installs are unchanged."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    home = tmp_path / "codex-noauth"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    [root] = codex_hook_roots([home])
    app_dir = ns["_cctally_core"].APP_DIR
    locks = acquire_due_lifecycle_locks(app_dir, [root], now=1000.0)
    try:
        assert locks[0].marker_path.name == f"{root.source_root_key}.last-success"
    finally:
        release_lifecycle_locks(locks)
