"""Issue #407: dashboard stats corruption recovery and attribution."""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys
import types

import pytest

from conftest import load_script, redirect_paths


_NOW = dt.datetime(2026, 1, 4, 10, 0, tzinfo=dt.timezone.utc)
_RESET = int(dt.datetime(2026, 1, 8, tzinfo=dt.timezone.utc).timestamp())
_CORRUPT_INDEX = "issue_407_current_week_index"


@pytest.fixture
def env(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return (
        ns,
        sys.modules["_cctally_core"],
        sys.modules["_cctally_store"],
        sys.modules["_cctally_tui"],
        sys.modules["_cctally_dashboard"],
    )


def _seed_journal_backed_current_week():
    import _cctally_journal as journal
    import _lib_journal as journal_wire

    journal.append_record(
        journal_wire.make_obs(
            at="2026-01-04T09:00:00Z",
            src="record-usage",
            provider="claude",
            payload={
                "weekly_percent": 7.0,
                "resets_at": _RESET,
                "source": "statusline",
                "captured_at": "2026-01-04T09:00:00Z",
            },
        )
    )
    journal.run_stats_ingest(mode="authoritative")


def _corrupt_current_week_index(core):
    """Damage only one index B-tree page; leave the DB header/schema readable."""
    conn = core.open_db()
    try:
        conn.execute(
            f"CREATE INDEX {_CORRUPT_INDEX} "
            "ON weekly_usage_snapshots("
            "week_start_at, week_end_at, week_start_date, captured_at_utc)"
        )
        conn.commit()
        plan = " ".join(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT week_start_at, week_end_at, week_start_date, "
                "MAX(captured_at_utc) "
                "FROM weekly_usage_snapshots "
                "WHERE week_start_at IS NOT NULL AND week_end_at IS NOT NULL "
                "GROUP BY week_start_at, week_end_at, week_start_date"
            )
        )
        assert _CORRUPT_INDEX in plan
        root_page = int(
            conn.execute(
                "SELECT rootpage FROM sqlite_schema WHERE name = ?",
                (_CORRUPT_INDEX,),
            ).fetchone()[0]
        )
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        conn.close()

    with pathlib.Path(core.DB_PATH).open("r+b", buffering=0) as handle:
        handle.seek((root_page - 1) * page_size)
        handle.write(b"\x00")

    # The exact production gap: the lightweight opener boundary succeeds, but
    # the real dashboard stats read that selects this index is corrupt.
    probe = core.open_db()
    try:
        assert probe.execute("PRAGMA schema_version").fetchone() is not None
        with pytest.raises(
            sqlite3.DatabaseError,
            match="database disk image is malformed|malformed database schema",
        ):
            probe.execute(
                "SELECT week_start_at, week_end_at, week_start_date, "
                "MAX(captured_at_utc) "
                "FROM weekly_usage_snapshots "
                "WHERE week_start_at IS NOT NULL AND week_end_at IS NOT NULL "
                "GROUP BY week_start_at, week_end_at, week_start_date"
            ).fetchall()
    finally:
        probe.close()


@pytest.mark.parametrize("snapshot_path", ["initial", "background"])
def test_index_only_stats_corruption_heals_once_after_handle_drain(
    env, monkeypatch, snapshot_path,
):
    ns, core, store, tui, dashboard = env
    _seed_journal_backed_current_week()
    _corrupt_current_week_index(core)

    heal_calls = []
    real_heal = store.HEAL_HOOK

    def tracked_heal(*args, **kwargs):
        # The dashboard's faulting stats connection must be closed before the
        # replacement-capable hook is invoked.
        assert store._stats_family_drained(core.DB_PATH) is None
        heal_calls.append((args, kwargs))
        return real_heal(*args, **kwargs)

    monkeypatch.setattr(store, "HEAL_HOOK", tracked_heal)
    opened = []
    target_module = dashboard if snapshot_path == "initial" else tui
    real_open = target_module.open_db

    def tracked_open(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(target_module, "open_db", tracked_open)
    if snapshot_path == "initial":
        snapshot = dashboard._dashboard_initial_snapshot(
            types.SimpleNamespace(no_sync=False, host="127.0.0.1"),
            pinned_now=_NOW,
            display_tz_pref_override="utc",
        )
    else:
        snapshot = ns["_tui_build_snapshot"](
            now_utc=_NOW,
            skip_sync=True,
            display_tz_pref_override="utc",
            precompute_envelope=True,
            runtime_bind="127.0.0.1",
        )

    assert len(heal_calls) == 1
    assert heal_calls[0][1] == {"post_query": True}
    assert len(opened) == 2
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")
    assert snapshot.current_week is not None
    assert snapshot.current_week.used_pct == 7.0
    assert snapshot.last_sync_error is None
    assert snapshot.sync_failures == ()

    live = core.open_db()
    try:
        assert live.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert live.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0] == 1
        assert live.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = ?",
            (_CORRUPT_INDEX,),
        ).fetchone() is None
    finally:
        live.close()
    incidents = sorted((core.APP_DIR / "quarantine").glob("stats.db-*"))
    forensics = sorted(core.LOG_DIR.glob("stats.db-corruption-forensics-*.json"))
    assert len(incidents) == 1
    assert len(forensics) == 1
    assert (incidents[0] / "manifest.json").exists()
    assert forensics[0].stat().st_mtime_ns <= incidents[0].stat().st_mtime_ns


def test_stats_attribution_wins_mixed_failure_without_leaking_raw_text(env):
    ns, _core, _store, tui, _dashboard = env
    raw = (
        "sync-cache: database disk image is malformed at /private/cache.db; "
        "week-index: database disk image is malformed at /private/stats.db"
    )
    snapshot = ns["_empty_dashboard_snapshot"]()
    snapshot = snapshot.__class__(
        **{
            **snapshot.__dict__,
            "last_sync_error": raw,
            "sync_failures": (
                tui.SyncFailureAttribution(
                    leg="sync-cache",
                    database="cache",
                    corruption=True,
                ),
                tui.SyncFailureAttribution(
                    leg="week-index",
                    database="stats",
                    corruption=True,
                ),
            ),
        }
    )

    envelope = ns["snapshot_to_envelope"](snapshot, now_utc=_NOW)

    assert envelope["sync_failure"] == {
        "kind": "stats_corruption",
        "label": "⚠ stats recovery needed",
        "detail": "The dashboard statistics database could not be read safely.",
        "action": "cctally db repair --db stats --yes",
    }
    assert "/private/" not in str(envelope["sync_failure"])


def test_declined_stats_heal_returns_degraded_snapshot_without_retry_loop(
    env, monkeypatch,
):
    ns, _core, store, tui, _dashboard = env
    calls = 0

    def corrupt_stats(conn):
        raise sqlite3.DatabaseError("database disk image is malformed")

    def decline(*args, **kwargs):
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setitem(ns, "build_claude_week_index", corrupt_stats)
    monkeypatch.setattr(store, "HEAL_HOOK", decline)

    snapshot = ns["_tui_build_snapshot"](
        now_utc=_NOW,
        skip_sync=True,
        display_tz_pref_override="utc",
    )
    envelope = ns["snapshot_to_envelope"](snapshot, now_utc=_NOW)

    assert calls == 1
    assert snapshot.last_sync_error is not None
    assert envelope["sync_failure"]["kind"] == "stats_corruption"
    assert envelope["sync_failure"]["action"] == (
        "cctally db repair --db stats --yes"
    )


def test_initial_retry_open_corruption_still_binds_typed_degraded_snapshot(
    env, monkeypatch,
):
    ns, _core, store, tui, dashboard = env
    real_open = dashboard.open_db
    opens = 0

    def open_then_fail():
        nonlocal opens
        opens += 1
        if opens == 1:
            return real_open()
        raise ns["StatsDbCorruptError"](
            "stats.db is still unreadable after an auto-heal rebuild "
            "(database disk image is malformed)"
        )

    def corrupt_current_week(*_args, **_kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(dashboard, "open_db", open_then_fail)
    monkeypatch.setattr(tui, "_tui_build_current_week", corrupt_current_week)
    monkeypatch.setattr(
        tui,
        "_tui_attribute_corruption",
        lambda *_args, **_kwargs: ("stats", True),
    )
    monkeypatch.setattr(store, "HEAL_HOOK", lambda *_a, **_kw: True)

    snapshot = dashboard._dashboard_initial_snapshot(
        types.SimpleNamespace(no_sync=False, host="127.0.0.1"),
        pinned_now=_NOW,
        display_tz_pref_override="utc",
    )
    envelope = ns["snapshot_to_envelope"](snapshot, now_utc=_NOW)

    assert opens == 2
    assert snapshot.hydrating is True
    assert envelope["sync_failure"]["kind"] == "stats_corruption"
    assert envelope["sync_failure"]["action"] == (
        "cctally db repair --db stats --yes"
    )


def test_background_retry_open_corruption_returns_typed_degraded_snapshot(
    env, monkeypatch,
):
    ns, _core, store, tui, _dashboard = env
    real_open = tui.open_db
    opens = 0

    def open_then_fail():
        nonlocal opens
        opens += 1
        if opens == 1:
            return real_open()
        raise ns["StatsDbCorruptError"](
            "stats.db is still unreadable after an auto-heal rebuild "
            "(database disk image is malformed)"
        )

    def corrupt_week_index(_conn):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(tui, "open_db", open_then_fail)
    monkeypatch.setitem(ns, "build_claude_week_index", corrupt_week_index)
    monkeypatch.setattr(store, "HEAL_HOOK", lambda *_a, **_kw: True)

    snapshot = ns["_tui_build_snapshot"](
        now_utc=_NOW,
        skip_sync=True,
        display_tz_pref_override="utc",
        precompute_envelope=True,
        runtime_bind="127.0.0.1",
    )
    envelope = ns["snapshot_to_envelope"](snapshot, now_utc=_NOW)

    assert opens == 2
    assert envelope["sync_failure"]["kind"] == "stats_corruption"
    assert envelope["sync_failure"]["action"] == (
        "cctally db repair --db stats --yes"
    )


def test_unrelated_crash_carry_clears_stale_stats_attribution(
    env, monkeypatch,
):
    ns, _core, _store, tui, _dashboard = env
    prior = ns["_empty_dashboard_snapshot"]()
    prior = prior.__class__(
        **{
            **prior.__dict__,
            "last_sync_error": "week-index: database disk image is malformed",
            "sync_failures": (
                tui.SyncFailureAttribution(
                    leg="week-index",
                    database="stats",
                    corruption=True,
                ),
            ),
        }
    )
    ref = ns["_SnapshotRef"](prior)

    class Hub:
        def __init__(self):
            self.published = []

        def publish(self, snapshot):
            self.published.append(snapshot)

    hub = Hub()

    def unrelated_crash(**_kwargs):
        raise RuntimeError("unrelated rebuild crash")

    monkeypatch.setitem(ns, "_tui_build_snapshot", unrelated_crash)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref,
        hub=hub,
        pinned_now=_NOW,
        display_tz_pref_override="utc",
    )
    locked(skip_sync=True)
    envelope = ns["snapshot_to_envelope"](hub.published[-1], now_utc=_NOW)

    assert hub.published[-1].sync_failures == ()
    assert envelope["sync_failure"]["kind"] == "server_sync"
