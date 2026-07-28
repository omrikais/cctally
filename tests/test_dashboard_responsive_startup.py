"""A1: cheap-panels first-paint seed + bind-before-build (issue #278 §1.1-§1.5).

On a normal launch ``_dashboard_initial_snapshot`` builds a CHEAP partial
snapshot — only the two sub-ms headline panels (current_week + forecast) plus
the real doctor + envelope-config precompute, ``hydrating=True`` — so the HTTP
socket binds in ~110ms instead of waiting on the ~2.2s full aggregation. The
background ``_DashboardSyncThread`` owns the first full cold build + SSE-publish.
Under ``--no-sync`` (no background thread to fill the partial) it keeps the full
pre-bind build (``hydrating=False``).

The subprocess bind-timing check is gated on the ``large`` bench fixture; it
skips where that fixture is absent (fresh CI), and is proven non-vacuous
manually by stashing the cheap-seed impl → RED against the pre-change full-seed.
"""
import datetime as dt
import json
import pathlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import types

from conftest import load_script, redirect_paths  # type: ignore

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "cctally"
OK_AS_OF = dt.datetime(2026, 4, 16, 14, 0, tzinfo=dt.timezone.utc)
LARGE_FIXTURE = (
    pathlib.Path(__import__("tempfile").gettempdir())
    / "cctally-bench" / "large-seed42"
)


def _dash_mod():
    import _cctally_dashboard  # re-imported against fresh cctally by load_script
    return _cctally_dashboard


def _seed_data_dir_from_fixture(tmp_path, scenario):
    """Copy a dashboard fixture's SQLite tree into a fresh tmp data dir and
    return ``(data_dir, claude_dir)`` for the fixture loader."""
    src = (REPO / "tests" / "fixtures" / "dashboard" / scenario
           / ".local" / "share" / "cctally")
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file():
            shutil.copy2(f, data / f.name)
    claude = tmp_path / "claude"
    (claude / "projects").mkdir(parents=True, exist_ok=True)
    return data, claude


def _load_with_fixture(monkeypatch, tmp_path, scenario):
    data, claude = _seed_data_dir_from_fixture(tmp_path, scenario)
    home = tmp_path / "home"
    codex = tmp_path / "codex"
    home.mkdir(parents=True, exist_ok=True)
    (codex / "sessions").mkdir(parents=True, exist_ok=True)
    # Pin every provider/home root BEFORE load_script so path initialization
    # and either ingest walker can only see state owned by this fixture.
    monkeypatch.setenv("CCTALLY_DATA_DIR", str(data))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("HOME", str(home))
    ns = load_script()
    return ns


def _load_with_empty_roots(monkeypatch, tmp_path):
    home = tmp_path / "home"
    codex = tmp_path / "codex"
    home.mkdir(parents=True, exist_ok=True)
    (codex / "sessions").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _write_minimal_codex_rollout(codex_home, *, session_id):
    rollout = (
        codex_home / "sessions" / "2026" / "07" / "27"
        / f"rollout-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-07-27T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/fixture/codex-project"},
        },
        {
            "timestamp": "2026-07-27T00:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5"},
        },
        {
            "timestamp": "2026-07-27T00:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 20,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 0,
                        "total_tokens": 130,
                    },
                    "total_token_usage": {"total_tokens": 130},
                },
            },
        },
    ]
    rollout.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return rollout


def _codex_schema_state(cache_path):
    conn = sqlite3.connect(cache_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        objects = tuple(
            conn.execute(
                """
                SELECT type, name, tbl_name, COALESCE(sql, '')
                  FROM sqlite_schema
                 WHERE type IN ('table', 'index')
                   AND (name LIKE 'codex_%' OR tbl_name LIKE 'codex_%')
                 ORDER BY type, name
                """
            )
        )
        return version, objects
    finally:
        conn.close()


def test_fixture_loader_replaces_inherited_codex_and_home_roots(
    monkeypatch, tmp_path,
):
    """The copied dashboard fixture owns every provider/home fallback.

    A caller-populated Codex root must not become input merely because the
    fixture loader inherited its process environment.
    """
    inherited_home = tmp_path / "inherited-home"
    inherited_codex = tmp_path / "inherited-codex"
    (inherited_codex / "sessions").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(inherited_home))
    monkeypatch.setenv("CODEX_HOME", str(inherited_codex))

    ns = _load_with_fixture(monkeypatch, tmp_path / "fixture", "ok")

    owned_home = tmp_path / "fixture" / "home"
    owned_codex = tmp_path / "fixture" / "codex"
    assert pathlib.Path.home() == owned_home
    assert ns["_codex_home_roots"]() == [owned_codex]
    assert all(
        root.is_relative_to(owned_codex)
        for root in ns["_codex_session_roots"]()
    )


def test_populated_fixture_cache_codex_first_and_repeat_touch_are_clean(
    monkeypatch, tmp_path,
):
    """A real Codex file is clean on both touches of a copied cache fixture."""
    owned_codex = tmp_path / "codex"
    rollout = _write_minimal_codex_rollout(
        owned_codex, session_id="fixture-first-touch",
    )
    ns = _load_with_fixture(monkeypatch, tmp_path, "ok")
    cache_path = ns["_cctally_core"].CACHE_DB_PATH
    before = _codex_schema_state(cache_path)

    assert ns["_discover_session_files"](dt.datetime.min.replace(
        tzinfo=dt.timezone.utc
    )) == []
    discovered = ns["_cctally_cache"]._discover_codex_files_with_roots()
    assert [item.source_path for item in discovered] == [rollout]
    assert all(
        item.source_path.is_relative_to(owned_codex)
        for item in discovered
    )

    first = ns["_tui_build_snapshot"](
        now_utc=OK_AS_OF,
        skip_sync=False,
        precompute_envelope=True,
        runtime_bind="127.0.0.1",
    )
    after_first = _codex_schema_state(cache_path)
    second = ns["_tui_build_snapshot"](
        now_utc=OK_AS_OF,
        skip_sync=False,
        precompute_envelope=True,
        runtime_bind="127.0.0.1",
    )
    after_second = _codex_schema_state(cache_path)

    assert first.last_sync_error is None
    assert second.last_sync_error is None
    assert after_first == after_second
    assert after_first[0] >= before[0]
    conn = sqlite3.connect(cache_path)
    try:
        assert conn.execute(
            "SELECT path FROM codex_session_files"
        ).fetchall() == [(str(rollout),)]
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone() == (1,)
    finally:
        conn.close()


def test_cheap_seed_normal_launch_shape(monkeypatch, tmp_path):
    ns = _load_with_fixture(monkeypatch, tmp_path, "ok")
    args = types.SimpleNamespace(no_sync=False, host="127.0.0.1")
    seed = _dash_mod()._dashboard_initial_snapshot(
        args, pinned_now=OK_AS_OF, display_tz_pref_override=None,
    )
    # Hydrating partial: the two headline panels are filled, heavy panels empty.
    assert seed.hydrating is True
    assert seed.current_week is not None
    assert seed.forecast is not None
    assert seed.sessions == []
    assert seed.weekly_periods == []
    assert seed.source_bundle is not None
    assert seed.source_bundle.sources["claude"].availability == "partial"
    assert seed.source_bundle.sources["codex"].data is None
    assert seed.monthly_periods == []
    assert seed.daily_panel == []
    assert seed.blocks_panel == []
    # Real doctor + well-formed envelope precompute so snapshot_to_envelope does
    # NOT hit the per-connection inline-doctor branch and does NOT KeyError.
    assert seed.doctor_payload is not None
    assert seed.envelope_precompute is not None
    assert {"config", "update_state", "update_suppress"} <= set(
        seed.envelope_precompute.keys()
    )
    env = ns["snapshot_to_envelope"](seed, now_utc=OK_AS_OF)
    assert env["hydrating"] is True
    assert env["current_week"] is not None


def test_cheap_seed_empty_data(monkeypatch, tmp_path):
    ns = _load_with_empty_roots(monkeypatch, tmp_path)
    args = types.SimpleNamespace(no_sync=False, host="127.0.0.1")
    seed = _dash_mod()._dashboard_initial_snapshot(
        args, pinned_now=None, display_tz_pref_override=None,
    )
    assert seed.hydrating is True
    assert seed.sessions == []
    assert seed.weekly_periods == []
    assert seed.source_bundle is not None
    # Even with no data, the real doctor + envelope precompute run so the
    # envelope serializes without the inline-doctor fork / KeyError.
    assert seed.doctor_payload is not None
    assert seed.envelope_precompute is not None
    assert {"config", "update_state", "update_suppress"} <= set(
        seed.envelope_precompute.keys()
    )
    env = ns["snapshot_to_envelope"](
        seed, now_utc=dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.timezone.utc)
    )
    assert env["hydrating"] is True


def test_no_sync_keeps_full_build(monkeypatch, tmp_path):
    ns = _load_with_fixture(monkeypatch, tmp_path, "ok")
    args = types.SimpleNamespace(no_sync=True, host="127.0.0.1")
    full = _dash_mod()._dashboard_initial_snapshot(
        args, pinned_now=OK_AS_OF, display_tz_pref_override=None,
    )
    # Frozen-data mode: the full pre-bind build, heavy panels populated,
    # hydrating cleared.
    assert full.hydrating is False
    assert full.current_week is not None
    assert len(full.sessions) > 0
    env = ns["snapshot_to_envelope"](full, now_utc=OK_AS_OF)
    assert env["hydrating"] is False


def _read_url_port(proc, deadline_s):
    """Block on the subprocess stdout until the 'serving …:PORT' line (printed
    right after the socket bind), returning (port, elapsed_since_start)."""
    import re
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError("dashboard exited before binding")
            continue
        m = re.search(r"://[^ ]*?:(\d+)/", line)
        if m:
            return int(m.group(1)), time.monotonic() - t0
    raise RuntimeError("timed out waiting for the serving line")


def test_bind_before_build_timing(tmp_path):
    """Bind-before-build on the heavy fixture: the socket accepts a TCP
    connection WELL BEFORE the full-data SSE frame (hydrating=false, non-empty
    sessions) arrives — proving the bind no longer waits on the ~2.2s
    aggregation. Skips where the large bench fixture is absent.

    Non-vacuous by construction: under the pre-change full-seed the first SSE
    frame IS the full snapshot published at bind, so time-to-accept ≈
    time-to-full-data and the ``bind precedes full data by ≥1s`` assertion
    fails RED (measured: pre-change time-to-accept ~5.0s == time-to-full-data;
    post-change ~2.0s vs ~5.0s). The absolute bound is a loose secondary guard
    (fixed process overhead — module import + self-heal + the 720MB cache
    migration-open — dominates the residual, NOT aggregation)."""
    if not (LARGE_FIXTURE / "data" / "cache.db").exists():
        import pytest
        pytest.skip("large bench fixture absent (build via build-bench-fixtures.py)")
    # Fresh copy so the launch's background sync never dirties the shared
    # fixture. APFS clone (cp -c) is instant + space-free on darwin; fall back
    # to a byte copy elsewhere.
    data = tmp_path / "data"
    src = LARGE_FIXTURE / "data"
    cloned = False
    if sys.platform == "darwin":
        rc = subprocess.run(["cp", "-c", "-R", str(src), str(data)]).returncode
        cloned = rc == 0
    if not cloned:
        shutil.copytree(src, data)
    # Drop any stale lock so the fresh process can take the flock.
    for lk in data.glob("*.lock"):
        try:
            lk.unlink()
        except OSError:
            pass
    claude = tmp_path / "claude" / "projects"
    claude.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    codex = tmp_path / "codex"
    home.mkdir(parents=True, exist_ok=True)
    (codex / "sessions").mkdir(parents=True, exist_ok=True)
    env = dict(__import__("os").environ)
    env["CCTALLY_DATA_DIR"] = str(data)
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude")
    env["CODEX_HOME"] = str(codex)
    env["HOME"] = str(home)
    t0 = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, str(BIN), "dashboard", "--port", "0",
         "--no-browser", "--host", "127.0.0.1"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    try:
        # Generous deadlines: under an 8-way parallel test-all the heavy
        # background build is CPU-starved and its wall time balloons, so a tight
        # deadline would false-fail. The assertion below is RELATIVE (bind
        # precedes full data), so it stays valid regardless of absolute wall
        # time; the deadlines only bound a genuine hang.
        port, time_to_accept = _read_url_port(proc, deadline_s=90.0)
        # time-to-URL ≈ time-to-bind ≈ time-to-accept (the serving line prints
        # right after TCPServer.__init__ bound + listened).
        with socket.create_connection(("127.0.0.1", port), timeout=5.0):
            pass
        # Full data arrives later over SSE: read /api/events until a
        # hydrating=false frame with non-empty sessions lands.
        import json as _json
        import urllib.request
        time_to_full = None
        req = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/events", timeout=90
        )
        end = time.monotonic() + 90.0
        data_lines: list[str] = []
        while time.monotonic() < end and time_to_full is None:
            raw = req.readline()
            if not raw:
                break
            s = raw.decode("utf-8", "replace").rstrip("\n")
            if s.startswith("data:"):
                data_lines.append(s[5:].strip())
            elif s == "" and data_lines:
                payload = "".join(data_lines)
                data_lines = []
                try:
                    frame = _json.loads(payload)
                except ValueError:
                    continue
                if frame.get("hydrating") is False and frame.get("sessions", {}).get("rows"):
                    time_to_full = time.monotonic() - t0
        req.close()
        assert time_to_full is not None, (
            "no full-data (hydrating=false, non-empty sessions) SSE frame"
        )
        # Core A1 property, and the ONLY assertion here: the socket binds WELL
        # BEFORE the heavy build finishes — i.e. the bind no longer waits on the
        # aggregation. This is machine- AND load-independent (both scale up under
        # contention, but the ≥2s CPU-bound background build always keeps the gap
        # ≥1s), so it survives the parallel test-all where an absolute wall-clock
        # bound would false-fail. It is naturally RED under the pre-change
        # full-seed (time-to-accept ≈ time-to-full-data). Absolute figures
        # (~2s accept / ~5s full on the heavy fixture) are recorded in
        # docs/backend-performance.md §6, not asserted (fixed process overhead
        # makes them machine-dependent).
        assert time_to_accept < time_to_full - 1.0, (
            f"bind did not precede full data by >=1s: "
            f"time_to_accept={time_to_accept:.3f}s time_to_full={time_to_full:.3f}s"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# A2 — decoupled throttled progressive ingest fill (§2.1-§2.5).
# ---------------------------------------------------------------------------

EMPTY_NOW = dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.timezone.utc)


class _CapturingHub:
    """Minimal SSEHub stand-in that records every published snapshot."""

    def __init__(self):
        self.published = []

    def publish(self, snap):
        self.published.append(snap)


def test_a2_throttle_clock_is_completion_measured(monkeypatch, tmp_path):
    load_script()
    import _cctally_tui as tui
    clk = tui._A2ThrottleClock(2.0, start=100.0)
    assert clk.should_fire(101.9) is False   # < T since sync start
    assert clk.should_fire(102.0) is True    # == T since sync start
    clk.mark_done(102.5)                      # a partial completed at 102.5
    assert clk.should_fire(104.0) is False   # < T since completion
    assert clk.should_fire(104.5) is True    # == T since completion


def test_a2_progress_cb_fires_throttled_and_publishes_hydrating(monkeypatch, tmp_path):
    ns = _load_with_empty_roots(monkeypatch, tmp_path)
    import _cctally_tui as tui
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = _CapturingHub()
    clock = {"t": 100.0}
    throttle = tui._A2ThrottleClock(2.0, start=100.0)
    perf_off = types.SimpleNamespace(enabled=lambda: False)
    cb = tui._make_a2_progress_cb(
        ref=ref, hub=hub,
        build_partial=lambda: ns["_empty_dashboard_snapshot"](),
        throttle=throttle, monotonic=lambda: clock["t"], perf=perf_off,
    )
    cb(None)  # t=100 → < T → no fire
    assert hub.published == []
    clock["t"] = 102.0
    cb(None)  # ≥ T → fire
    assert len(hub.published) == 1
    assert hub.published[0].hydrating is True   # publish carries the latch
    assert ref.get().hydrating is False         # ref/memo keep the clean object
    clock["t"] = 103.0
    cb(None)  # < T since completion(102) → no fire
    assert len(hub.published) == 1
    clock["t"] = 104.0
    cb(None)  # ≥ T since completion → fire
    assert len(hub.published) == 2


def test_a2_progress_cb_suppressed_under_perf_tracing(monkeypatch, tmp_path):
    ns = _load_with_empty_roots(monkeypatch, tmp_path)
    import _cctally_tui as tui
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = _CapturingHub()
    throttle = tui._A2ThrottleClock(0.0, start=0.0)  # would otherwise always fire
    perf_on = types.SimpleNamespace(enabled=lambda: True)
    cb = tui._make_a2_progress_cb(
        ref=ref, hub=hub,
        build_partial=lambda: ns["_empty_dashboard_snapshot"](),
        throttle=throttle, monotonic=lambda: 999.0, perf=perf_on,
    )
    cb(None)
    cb(None)
    assert hub.published == []  # progressive fill suppressed during a trace


def test_a2_warm_sync_yields_single_publish(monkeypatch, tmp_path):
    # Empty CLAUDE dir → the real sync_cache finishes far under T → the throttle
    # never fires → exactly one publish (the final, hydrating=False).
    ns = _load_with_empty_roots(monkeypatch, tmp_path)
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = _CapturingHub()
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=EMPTY_NOW, display_tz_pref_override=None,
    )
    locked(skip_sync=False)
    assert len(hub.published) == 1
    assert hub.published[0].hydrating is False


def test_a2_progressive_multi_frame(monkeypatch, tmp_path):
    # A slow first-run sync (faked: progress fires twice) crossing T (patched to
    # 0) yields MULTIPLE hydrating=true partial frames, ending in a
    # hydrating=false complete frame. Non-vacuous vs the pre-change single frame.
    ns = _load_with_empty_roots(monkeypatch, tmp_path)
    import _cctally_tui as tui
    monkeypatch.setattr(tui, "_A2_PARTIAL_THROTTLE_S", 0.0)
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = _CapturingHub()

    def fake_sync(conn, *, progress=None, **kw):
        if progress is not None:
            progress(None)
            progress(None)
        return None

    monkeypatch.setitem(ns, "sync_cache", fake_sync)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=EMPTY_NOW, display_tz_pref_override=None,
    )
    locked(skip_sync=False)
    hydrating_frames = [s for s in hub.published if getattr(s, "hydrating", False)]
    assert len(hydrating_frames) >= 2, f"expected ≥2 partials, got {hub.published}"
    assert hub.published[-1].hydrating is False, "final frame must be complete"


def test_a2_perf_trace_suppresses_partials_integration(monkeypatch, tmp_path):
    ns = _load_with_empty_roots(monkeypatch, tmp_path)
    import _cctally_tui as tui
    import _lib_perf as perf
    monkeypatch.setattr(tui, "_A2_PARTIAL_THROTTLE_S", 0.0)
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = _CapturingHub()

    def fake_sync(conn, *, progress=None, **kw):
        if progress is not None:
            progress(None)
            progress(None)
        return None

    monkeypatch.setitem(ns, "sync_cache", fake_sync)
    perf.set_enabled(True)
    try:
        locked = ns["_make_run_sync_now_locked"](
            ref=ref, hub=hub, pinned_now=EMPTY_NOW, display_tz_pref_override=None,
        )
        locked(skip_sync=False)
    finally:
        perf.set_enabled(False)
        perf.reset_thread()
    hydrating_frames = [s for s in hub.published if getattr(s, "hydrating", False)]
    assert hydrating_frames == []          # partials suppressed during a trace
    assert len(hub.published) == 1         # only the final full build
    assert hub.published[0].hydrating is False


def test_a2_decouple_parity_byte_identical(monkeypatch, tmp_path):
    # The decoupled path's final published snapshot is byte-identical to today's
    # _tui_build_snapshot(skip_sync=False) over the same cache — proving the
    # decoupling (and any intermediate partials) don't change the final result.
    inherited_codex = tmp_path / "inherited-codex"
    _write_minimal_codex_rollout(
        inherited_codex, session_id="must-not-be-discovered",
    )
    monkeypatch.setenv("CODEX_HOME", str(inherited_codex))
    ns = _load_with_fixture(monkeypatch, tmp_path, "ok")
    import _lib_snapshot_cache as sc
    BIND = "127.0.0.1"

    assert ns["_cctally_cache"]._discover_codex_files_with_roots() == []
    assert ns["_discover_session_files"](
        dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    ) == []

    sc.reset_dispatch_state()
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = _CapturingHub()
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=OK_AS_OF,
        display_tz_pref_override=None, runtime_bind=BIND,
    )
    locked(skip_sync=False)
    decoupled = hub.published[-1]
    env_decoupled = ns["snapshot_to_envelope"](
        decoupled, now_utc=OK_AS_OF, monotonic_now=None, runtime_bind=BIND,
    )

    # A fresh, non-idle direct full build over the same (unchanged) cache.
    sc.reset_dispatch_state()
    direct = ns["_tui_build_snapshot"](
        now_utc=OK_AS_OF, skip_sync=False,
        precompute_envelope=True, runtime_bind=BIND,
    )
    env_direct = ns["snapshot_to_envelope"](
        direct, now_utc=OK_AS_OF, monotonic_now=None, runtime_bind=BIND,
    )

    assert json.dumps(env_decoupled, sort_keys=True) == json.dumps(
        env_direct, sort_keys=True
    )
    assert decoupled.last_sync_error is None
    assert direct.last_sync_error is None
    assert decoupled.hydrating is False


def test_a2_decouple_threads_sync_cache_error(monkeypatch, tmp_path):
    # A raising standalone sync_cache in the decoupled skip_sync=False path must
    # surface on the merged last_sync_error with the `sync-cache:` prefix —
    # matching the INTERNAL _tui_build_snapshot(skip_sync=False) error surface
    # (its `sync` phase records errors[0] = f"sync-cache: {exc}"). This locks the
    # sync-error-threading parity: decoupling the ingest must not change the
    # error wording the UI sees.
    ns = _load_with_empty_roots(monkeypatch, tmp_path)
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = _CapturingHub()

    def boom_sync(conn, *, progress=None, **kw):
        raise RuntimeError("disk gone")

    monkeypatch.setitem(ns, "sync_cache", boom_sync)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=EMPTY_NOW, display_tz_pref_override=None,
    )
    locked(skip_sync=False)
    # boom_sync raises before firing any partial → exactly one (final) publish.
    assert len(hub.published) == 1
    published = hub.published[-1]
    assert published.last_sync_error is not None
    assert published.last_sync_error.startswith("sync-cache: ")
    assert "disk gone" in published.last_sync_error
    # The final build still completed and cleared the hydration latch — the sync
    # error is threaded through, not fatal.
    assert published.hydrating is False


def test_a2_decouple_declines_unconfirmed_cache_corruption(
    monkeypatch, tmp_path,
):
    ns = _load_with_empty_roots(monkeypatch, tmp_path)
    import _cctally_tui as tui
    monkeypatch.setattr(tui, "_A2_PARTIAL_THROTTLE_S", 0.0)
    cache_mod = ns["_cctally_cache"]
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = _CapturingHub()
    real_sync = ns["sync_cache"]
    attempts = 0

    def corrupt_once(conn, *, progress=None, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_sync(conn, progress=progress, **kwargs)

    monkeypatch.setitem(ns, "sync_cache", corrupt_once)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=EMPTY_NOW,
        display_tz_pref_override=None,
    )
    locked(skip_sync=False)

    # A classified exception is only a trigger for the locked forensics probe.
    # This fixture's cache is healthy, so recovery must fail closed: preserve
    # the original exception path and never retry or quarantine the cache.
    assert attempts == 1
    assert len(hub.published) == 1
    assert hub.published[-1].last_sync_error is not None
    assert hub.published[-1].last_sync_error.startswith("sync-cache: ")
    assert "database disk image is malformed" in (
        hub.published[-1].last_sync_error
    )
    assert hub.published[-1].hydrating is False
    incidents = list(
        (pathlib.Path(ns["_cctally_core"].APP_DIR) / "quarantine").glob(
            "cache.db-*"
        )
    )
    assert incidents == []
