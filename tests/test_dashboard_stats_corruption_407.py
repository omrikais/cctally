"""Issue #407: dashboard stats corruption recovery and attribution."""
from __future__ import annotations

import dataclasses
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
    spawned: list[str] = []
    import _cctally_update

    monkeypatch.setattr(
        _cctally_update,
        "_spawn_detached",
        lambda command: spawned.append(command) or True,
    )
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
    # #496 S3 §6: the heal DEFERS, so the faulting build is not retried against
    # a freshly published index — one open, then the typed degraded frame. The
    # caller never waits for the rebuild.
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")
    assert snapshot.last_sync_error is not None
    assert "corruption rebuild is running in the background" in (
        snapshot.last_sync_error
    )
    assert [f.database for f in snapshot.sync_failures] == ["stats"]
    assert snapshot.sync_failures[0].corruption is True
    envelope = ns["snapshot_to_envelope"](snapshot, now_utc=_NOW)
    assert envelope["sync_failure"]["kind"] == "stats_corruption"

    # Nothing was captured or replaced on the caller's thread. The preliminary
    # marker elects the detached worker, which owns both operations (#530).
    assert sorted(core.LOG_DIR.glob("stats.db-corruption-forensics-*.json")) == []
    assert store._read_stats_heal_request()["forensicsPath"] is None
    assert not (core.APP_DIR / "quarantine").exists()
    assert spawned == [store.STATS_CORRUPTION_HEAL_COMMAND]

    assert store.cmd_stats_corruption_heal_internal(types.SimpleNamespace()) == 0

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
    assert len(incidents) == 1
    assert (incidents[0] / "manifest.json").exists()
    forensics = sorted(core.LOG_DIR.glob("stats.db-corruption-forensics-*.json"))
    assert len(forensics) == 1
    assert forensics[0].stat().st_mtime_ns <= incidents[0].stat().st_mtime_ns


# ==========================================================================
# #496 S3 §8 — F16 as a class
# ==========================================================================

def _corrupt_stats_connection(tmp_path):
    """A live connection to a file that is not a database."""
    path = tmp_path / "f16-corrupt-stats.db"
    path.write_bytes(b"not a database " * 200)
    return sqlite3.connect(str(path))


def _idle_prior(tui):
    """A prior snapshot whose source bundle CANNOT idle, so the idle builder
    takes its bounded source-adapter branch and reads stats for real."""
    return dataclasses.replace(
        tui._tui_empty_snapshot(_NOW),
        source_bundle=tui._tui_hydrating_source_bundle(),
    )


def test_idle_snapshot_stats_corruption_reaches_the_heal_boundary(
    env, tmp_path,
):
    """F16, first instance. `_tui_build_idle_snapshot`'s `source_stats_conn`
    branch caught bare `Exception`, appended a plain string to `errors`, and
    fell back to the prior bundle — so it built no `SyncFailureAttribution`,
    never raised `_StatsSnapshotCorruption`, and never reached the heal."""
    _ns, _core, _store, tui, _dashboard = env
    conn = _corrupt_stats_connection(tmp_path)
    errors: list[str] = []
    try:
        with pytest.raises(tui._StatsSnapshotCorruption):
            tui._tui_build_idle_snapshot(
                _idle_prior(tui), now_utc=_NOW, precompute_envelope=False,
                runtime_bind=None, raw_config={}, errors=errors,
                source_stats_conn=conn,
            )
    finally:
        conn.close()


def test_idle_snapshot_stats_corruption_never_emits_the_cache_envelope(
    env, tmp_path,
):
    """The exact production consequence: `_sync_failure_envelope` falls to its
    RAW-TEXT matcher when no typed attribution exists, and emits
    `cache_corruption` plus `cctally cache-sync --rebuild` for a STATS fault.

    After one heal attempt the builder records rather than raises, so this
    pins the attribution itself and not merely the raise above.
    """
    ns, _core, _store, tui, _dashboard = env
    conn = _corrupt_stats_connection(tmp_path)
    errors: list[str] = []
    try:
        idle = tui._tui_build_idle_snapshot(
            _idle_prior(tui), now_utc=_NOW, precompute_envelope=False,
            runtime_bind=None, raw_config={}, errors=errors,
            source_stats_conn=conn, stats_heal_attempted=True,
        )
    finally:
        conn.close()

    assert [f.database for f in idle.sync_failures] == ["stats"]
    assert idle.sync_failures[0].corruption is True
    envelope = ns["snapshot_to_envelope"](idle, now_utc=_NOW)
    assert envelope["sync_failure"]["kind"] == "stats_corruption"
    assert envelope["sync_failure"]["action"] != "cctally cache-sync --rebuild"


def test_the_debug_helpers_attribute_a_stats_fault_without_a_heal(
    env, tmp_path, monkeypatch,
):
    """A DELIBERATE deviation from F16's literal wording, recorded not hidden.

    F16 asks for "a typed stats attribution AND reaches the heal". These two
    are short-lived debug reads that deliberately bypass the corruption
    boundary for cost, and making a debug endpoint able to trigger a rebuild is
    a worse outcome than making it honest. The dashboard's main build path
    already reaches the heal, so a fault they attribute is healed on the next
    tick.
    """
    ns, _core, store, _tui, dashboard = env

    def corrupt_stats_open():
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(dashboard, "_stats_ro_guarded", corrupt_stats_open)
    monkeypatch.setattr(
        store,
        "HEAL_HOOK",
        lambda *_a, **_kw: pytest.fail("a debug read authorized a heal"),
    )
    cache_conn = ns["open_cache_db"]()
    try:
        faults: list[dict] = []
        dashboard._debug_source_counts(cache_conn, None, faults=faults)
        dashboard._debug_cache_state(cache_conn, faults=faults)
    finally:
        cache_conn.close()

    assert len(faults) == 2, faults
    # One fault per leg. The endpoint hands ONE list to several helpers, so a
    # helper that appended twice — or a second call to the same helper sharing
    # the list — would report one failure as two to the reader.
    assert sorted(f["leg"] for f in faults) == [
        "debug-cache-state", "debug-source-counts",
    ]
    for fault in faults:
        assert fault["database"] == "stats"
        assert fault["corruption"] is True
        assert isinstance(fault["leg"], str) and fault["leg"]
    # The wire form is exactly what the existing envelope reader consumes, so
    # the attribution travels on the established vocabulary with no new kind.
    import _cctally_dashboard_envelope as envelope_mod
    assert envelope_mod._sync_failure_envelope(
        "debug read failed", faults
    )["kind"] == "stats_corruption"


def test_every_stats_reporting_surface_is_classified(env):
    """The class, bounded by an explicit inventory the scan is compared against.

    A purely structural scan is not trustworthy here — this repository has been
    bitten by scans that missed the extensionless `bin/cctally` entry point and
    by a shipped pattern that matched nothing real. Modelled on
    `tests/test_stats_writer_surface_386.py`: the inventory is the authority,
    and the scan fails BOTH on an unknown new site and on a vanished known one.

    Recorded limitation, stated rather than papered over: this resolves only
    LEXICALLY named calls. A site that reached stats through a variable, a
    getattr, or a callback passed in from elsewhere is invisible to it, exactly
    as the #386 freeze cannot resolve dynamic SQL targets.
    """
    import ast

    root = pathlib.Path(__file__).resolve().parents[1] / "bin"
    # Every function that can put a stats fault in front of a user: the two
    # snapshot builders' capture points, and the dashboard's read-only helpers.
    markers = {
        "_tui_capture_sync_failure": "heal",
        "_StatsSnapshotCorruption": "heal",
        "_stats_ro_guarded": "raw-read",
        "_debug_stats_fault": "attribution-only",
    }
    inventory = {
        # #583 S1 split `_tui_build_snapshot` into a thin boundary that
        # opens the standalone tick context and this body, which is where
        # the post-query stats heal and its degraded frames now live. The
        # boundary reports no stats fault of its own, so the classified
        # surface MOVED rather than gaining a second entry.
        ("_cctally_tui.py", "_tui_build_snapshot_impl"): "heal",
        ("_cctally_tui.py", "_tui_build_snapshot_once"): "heal",
        ("_cctally_tui.py", "capture_failure"): "heal",
        ("_cctally_tui.py", "_tui_capture_sync_failure"): "heal",
        ("_cctally_tui.py", "_tui_build_idle_snapshot"): "heal",
        ("_cctally_dashboard.py", "_dashboard_initial_snapshot"): "heal",
        ("_cctally_dashboard.py", "_dashboard_initial_snapshot_once"): "heal",
        ("_cctally_dashboard.py", "_debug_source_counts"): "attribution-only",
        ("_cctally_dashboard.py", "_debug_cache_state"): "attribution-only",
    }
    #: Sites that reach stats but are exempt, each with a stated reason. Each
    #: one is required below to match a real scanned site: an exemption the
    #: scan never produces removes nothing and asserts nothing.
    exempt = {
        ("_cctally_dashboard.py", "_stats_ro_guarded"):
            "the guarded opener itself; it raises to its callers",
        ("_cctally_dashboard.py", "_debug_stats_fault"):
            "the attribution constructor itself; its callers are classified",
    }

    found: dict[tuple[str, str], set[str]] = {}
    for name in ("_cctally_tui.py", "_cctally_dashboard.py"):
        module = root / name
        assert module.exists(), module
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # A function whose own NAME is a marker is the site itself, not a
            # caller of one. Without this the scan sees `_stats_ro_guarded` and
            # `_debug_stats_fault` only through their callers, and the exempt
            # list below removes entries that were never there.
            hits = (
                {markers[node.name]} if node.name in markers else set()
            ) | {
                markers[child.id]
                for child in ast.walk(node)
                if isinstance(child, ast.Name) and child.id in markers
            } | {
                markers[child.attr]
                for child in ast.walk(node)
                if isinstance(child, ast.Attribute) and child.attr in markers
            }
            if hits:
                found[(name, node.name)] = hits

    for key, reason in exempt.items():
        assert key in found, (
            f"the exemption for {key} ({reason}) matches no scanned site, so "
            "it exempts nothing. Fix the scan so it sees the site, or delete "
            "the entry."
        )
        found.pop(key)

    unknown = sorted(set(found) - set(inventory))
    assert not unknown, (
        "a new surface can report a stats fault and is not classified: "
        f"{unknown}. Add it to the inventory with its disposition, or to the "
        "exempt list with a stated reason."
    )
    vanished = sorted(set(inventory) - set(found))
    assert not vanished, (
        f"a classified stats-reporting surface disappeared: {vanished}. The "
        "inventory must not silently rot."
    )
    for key, disposition in inventory.items():
        if disposition == "heal":
            assert "heal" in found[key], (
                f"{key} no longer reaches the heal boundary: {found[key]}"
            )
        else:
            assert found[key] == {"raw-read", "attribution-only"}, (
                f"{key} must record a typed attribution without authorizing a "
                f"heal; it does: {found[key]}"
            )


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
