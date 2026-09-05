"""A1: cheap-panels first-paint seed + bind-before-build (issue #278 §1.1-§1.5).

On a normal launch ``_dashboard_initial_snapshot`` builds a CHEAP partial
snapshot — only the two sub-ms headline panels (current_week + forecast) plus
the real doctor + envelope-config precompute, ``hydrating=True`` — so the HTTP
socket binds in ~110ms instead of waiting on the ~2.2s full aggregation. The
background ``_DashboardSyncThread`` owns the first full cold build + SSE-publish.
Under ``--no-sync`` (no background thread to fill the partial) it keeps the full
pre-bind build (``hydrating=False``).

#583 S1 replaced the old subprocess bind-TIMING check with an in-process
ORDERING test. The old one skipped whenever the ``large`` bench fixture was
absent, and nothing in the suite or CI ever built that path, so it never ran.
The replacement holds the full builder on a barrier and asserts the order —
bind, accept, hydrating frame, then a non-hydrating frame only after release —
over the session-scoped ``small_corpus``, which is built rather than skipped.
"""
import datetime as dt
import json
import pathlib
import shutil
import socket
import sqlite3
import threading
import time
import types

import pytest

from conftest import (  # type: ignore
    copy_shared_corpus, load_script, redirect_paths)

from tests._support_http import (
    PRESENCE_BACKSTOP_SECONDS, shorten_sse_keepalive,
)

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "cctally"
OK_AS_OF = dt.datetime(2026, 4, 16, 14, 0, tzinfo=dt.timezone.utc)


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
    submitted = []

    class _SnapshotBounds:
        @staticmethod
        def enforce_snapshot_accelerator_bounds(**kwargs):
            submitted.append(kwargs)

    snapshot_cache = ns["_load_sibling"]("_lib_snapshot_cache")
    monkeypatch.setattr(
        snapshot_cache,
        "enforce_snapshot_accelerator_bounds",
        _SnapshotBounds.enforce_snapshot_accelerator_bounds,
    )
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
    assert submitted == [{"data_version": "dashboard-no-sync-initial"}]


def test_cache_report_qa_state_requires_explicit_master_switch(monkeypatch, tmp_path):
    ns = _load_with_empty_roots(monkeypatch, tmp_path)
    dashboard = _dash_mod()
    monkeypatch.setenv("CCTALLY_DASHBOARD_QA_STATE", "failed")
    monkeypatch.delenv("CCTALLY_DASHBOARD_QA", raising=False)
    assert dashboard._dashboard_qa_state_from_env() is None
    monkeypatch.setenv("CCTALLY_DASHBOARD_QA", "1")
    monkeypatch.setitem(ns, "_is_dev_checkout", lambda: True)
    assert dashboard._dashboard_qa_state_from_env() == "failed"
    monkeypatch.setitem(ns, "_is_dev_checkout", lambda: False)
    assert dashboard._dashboard_qa_state_from_env() is None


def test_cache_report_qa_states_preserve_real_data_or_fail_explicitly(
    monkeypatch, tmp_path,
):
    ns = _load_with_fixture(monkeypatch, tmp_path, "ok")
    dashboard = _dash_mod()
    args = types.SimpleNamespace(no_sync=True, host="127.0.0.1")
    full = dashboard._dashboard_initial_snapshot(
        args, pinned_now=OK_AS_OF, display_tz_pref_override=None,
    )

    degraded = dashboard._dashboard_apply_qa_state(full, "forensics-degraded")
    degraded_env = ns["snapshot_to_envelope"](degraded, now_utc=OK_AS_OF)
    assert degraded_env["hydrating"] is False
    assert degraded_env["sources"]["claude"]["availability"] == "partial"
    assert degraded_env["cache_report"] is not None
    assert any(
        warning["domain"] == "forensics"
        for warning in degraded_env["sources"]["claude"]["warnings"]
    )

    failed = dashboard._dashboard_apply_qa_state(full, "failed")
    failed_env = ns["snapshot_to_envelope"](failed, now_utc=OK_AS_OF)
    assert failed_env["sources"]["claude"]["availability"] == "unavailable"
    assert failed_env["sources"]["claude"]["data"] is None
    assert failed_env["cache_report"] is None

    hydrating = dashboard._dashboard_apply_qa_state(full, "hydrating")
    hydrating_env = ns["snapshot_to_envelope"](hydrating, now_utc=OK_AS_OF)
    assert hydrating_env["hydrating"] is True
    assert hydrating_env["sources"]["claude"]["data"] is None
    assert hydrating_env["cache_report"] is None


def test_cache_report_qa_fixture_reaches_amber_and_mixed_signs(
    monkeypatch, tmp_path,
):
    ns = _load_with_fixture(monkeypatch, tmp_path, "cache-report-qa")
    args = types.SimpleNamespace(no_sync=True, host="127.0.0.1")
    full = _dash_mod()._dashboard_initial_snapshot(
        args, pinned_now=OK_AS_OF, display_tz_pref_override=None,
    )
    report = ns["snapshot_to_envelope"](full, now_utc=OK_AS_OF)["cache_report"]
    assert report["today"]["baseline_daily_row_count"] >= 5
    assert report["today"]["anomaly_triggered"] is True
    assert "cache_drop" in report["today"]["anomaly_reasons"]
    signs = {"negative" if day["net_usd"] < 0 else "positive" for day in report["days"]}
    assert signs == {"negative", "positive"}


#: One budget for the body of the responsive-startup test. Every wait inside
#: the body draws from this one.
#:
#: The teardown cannot draw from it, because the waits there exist precisely to
#: outlast an exhausted budget, so it gets its own — and the two must fit
#: together inside the 120-second per-test ceiling `bin/cctally-test-all`
#: applies to the pytest phase. 75 plus 30 is 105, which leaves 15 seconds of
#: margin.
#:
#: The margin is the point of the numbers, not an accident of them. An earlier
#: pair summed to exactly 120.0 against a `total > CAP_SECONDS` test — a rule
#: since corrected to reserve headroom, so that sum is now reported — and the
#: guard reported nothing while the real worst case was 160: the guard could
#: not see the two teardown deadlines, which were written inline as
#: `time.monotonic() + N` and belonged to no local name. Both halves are fixed
#: — `_clock_plus_constant` in `tests/test_timing_budget_guard.py` now counts an
#: inline deadline, and the teardown here draws from one budget instead of
#: three — and neither number is chosen to land on the boundary.
_WHOLE_TEST_BUDGET_S = 75.0

#: The teardown's own budget, shared by all three of its waits. Thirty is
#: `PRESENCE_BACKSTOP_SECONDS`, the estate's one load-safe presence backstop:
#: every wait here is a presence wait on a thread exiting, so it returns as
#: soon as that happens and spends the budget only when something is wedged.
_TEARDOWN_BUDGET_S = PRESENCE_BACKSTOP_SECONDS


def _remaining(deadline: float) -> float:
    """Seconds left of the shared budget, never zero or negative."""
    return max(1.0, deadline - time.monotonic())


def _load_with_corpus(monkeypatch, tmp_path, small_corpus):
    """Point every provider/home root at a private COPY of the bench corpus.

    The whole corpus root is copied, not just its data dir: `sync_cache` prunes
    cached rows whose source JSONL has gone, so pointing CLAUDE_CONFIG_DIR at an
    empty directory would empty the very corpus this test needs. The copy also
    keeps the shared session-scoped corpus clean — the dashboard writes WAL and
    ingest state into whatever data dir it is given.
    """
    #741: the copy goes through `conftest.copy_shared_corpus`, the ONE copier.
    # It resolves the source root, excludes the SQLite sidecars, and holds the
    # scale's build lock SHARED for the whole walk, so no copy races a rebuild.
    root = tmp_path / "corpus"
    copy_shared_corpus(small_corpus, root)
    for lock in (root / "data").glob("*.lock"):
        try:
            lock.unlink()
        except OSError:
            pass
    codex_roots = sorted(p for p in root.glob("codex-*") if p.is_dir())
    monkeypatch.setenv("CCTALLY_DATA_DIR", str(root / "data"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root / "claude"))
    monkeypatch.setenv("CODEX_HOME", ",".join(str(p) for p in codex_roots))
    monkeypatch.setenv("HOME", str(root / "home"))
    # The corpus is anchored at a fixed reference epoch, so against a real wall
    # clock every recency-filtered panel is empty and the authoritative frame
    # would be indistinguishable from the seed. The instant is the generator's
    # own stated clock, read rather than restated: the corpus's quota geometry
    # is built around it, so a second spelling that drifted would change what
    # this test sees.
    import importlib.machinery
    import importlib.util

    _loader = importlib.machinery.SourceFileLoader(
        "build_bench_fixtures", str(REPO / "bin" / "build-bench-fixtures.py"))
    _gen = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("build_bench_fixtures", _loader))
    _loader.exec_module(_gen)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        _gen.CORPUS_CLOCK_UTC.isoformat().replace("+00:00", "Z"))
    ns = load_script()
    shorten_sse_keepalive(ns, monkeypatch)
    return ns


def _await_handler_exit(before, *, deadline, name_contains="process_request_thread"):
    """Wait for a thread this test caused to be started to notice and exit.

    `cmd_dashboard` runs a daemon-threaded server that joins no handler, and
    the handler learns its client has gone only on the keep-alive write after
    `_SSE_KEEPALIVE_SECONDS`, which `_load_with_corpus` shortens. This is a
    presence wait on that exit, so it returns as soon as the thread is gone.
    """
    while True:
        # Bound unconditionally, and probed at least once. Reading `alive`
        # after the loop was legal only because the single caller happened to
        # guarantee one iteration; an elapsed deadline raised UnboundLocalError
        # in place of the named diagnostic.
        alive = [
            t for t in threading.enumerate()
            if t not in before and name_contains in t.name
        ]
        if not alive:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    raise AssertionError(
        "a %r thread outlived this test: " % name_contains
        + ", ".join(repr(t.name) for t in alive))


def test_await_handler_exit_names_the_thread_on_an_already_elapsed_deadline():
    """The diagnostic must survive a caller that arrives with no time left.

    `alive` used to be bound only inside the `while`, so the single caller's
    guarantee of one iteration was the only thing keeping the `raise` legal. A
    caller passing an elapsed deadline got `UnboundLocalError` instead of the
    named diagnostic this helper exists to produce.
    """
    before = set(threading.enumerate())
    release = threading.Event()
    probe = threading.Thread(
        target=lambda: release.wait(60),
        name="Thread-probe (process_request_thread)", daemon=True)
    probe.start()
    try:
        with pytest.raises(AssertionError, match="process_request_thread"):
            _await_handler_exit(before, deadline=time.monotonic() - 1.0)
    finally:
        release.set()
        # timing-budget: cleanup join for a thread this test has just released
        probe.join(timeout=PRESENCE_BACKSTOP_SECONDS)
    assert not probe.is_alive()


def _sse_frames(port, deadline):
    """Yield decoded SSE payload objects from /api/events until `deadline`."""
    import urllib.request
    req = urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/events", timeout=_remaining(deadline))
    try:
        chunks: list[str] = []
        while time.monotonic() < deadline:
            raw = req.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("data:"):
                chunks.append(line[5:].strip())
            elif line == "" and chunks:
                payload = "".join(chunks)
                chunks = []
                try:
                    yield json.loads(payload)
                except ValueError:
                    continue
    finally:
        req.close()


def test_bind_precedes_the_full_build_and_hydration_clears(
    monkeypatch, tmp_path, small_corpus
):
    """Bind-before-build, proven as an EVENT ORDER rather than a duration.

    The full builder is held on a barrier. While it is held, the real server
    must already have bound, must accept a TCP connection, and must serve an SSE
    frame whose `hydrating` is true. Only after the barrier is released may the
    AUTHORITATIVE frame arrive — non-hydrating AND carrying sessions, which is
    a state no publication before the release can reach. That ordering — bind,
    then progressive fill — is the product invariant #278 A1 established; how
    many seconds a particular corpus takes to aggregate is not, and asserting
    it made this test machine-sensitive for no gain.

    This replaces a SUBPROCESS test that could not do any of the above. A
    `threading.Event` cannot reach a background builder across a process
    boundary, so the old test could only compare two wall-clock durations, and
    it guarded on `LARGE_FIXTURE ... .exists()` where nothing in the suite or CI
    ever built that path — so it SKIPPED, silently, from the day it was written.
    The corpus now arrives from the session-scoped `small_corpus` fixture, which
    builds it and fails rather than skipping.
    """
    ns = _load_with_corpus(monkeypatch, tmp_path, small_corpus)
    dash = _dash_mod()
    before_handlers = set(threading.enumerate())

    deadline = time.monotonic() + _WHOLE_TEST_BUDGET_S

    # The barrier. `_tui_build_snapshot` is the FULL builder; the A1 cheap seed
    # deliberately does not call it (it uses the individual panel builders), so
    # holding it here blocks the background build and nothing else. Patched via
    # `setitem` on the cctally namespace, which is the documented way to reach
    # the call site inside `_make_run_sync_now_locked`.
    entered = threading.Event()
    release = threading.Event()
    real_build = ns["_tui_build_snapshot"]

    def barrier_build(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=_remaining(deadline)):
            raise AssertionError("the barrier was never released")
        return real_build(*args, **kwargs)

    monkeypatch.setitem(ns, "_tui_build_snapshot", barrier_build)

    # Capture the REAL server instance so the test learns its bound port
    # without parsing stdout from another thread.
    bound = {}
    real_server_cls = dash._QuietThreadingHTTPServer

    class _RecordingServer(real_server_cls):
        def __init__(self, address, handler):
            super().__init__(address, handler)
            bound["srv"] = self

    monkeypatch.setattr(dash, "_QuietThreadingHTTPServer", _RecordingServer)

    # `cmd_dashboard` blocks on a signal at the end; a thread cannot install a
    # signal handler, so the wait is replaced by one this test can end.
    stop = threading.Event()
    monkeypatch.setattr(
        dash, "_dashboard_wait_for_signal",
        lambda *a, **k: stop.wait(timeout=_remaining(deadline)))

    args = ns["build_parser"]().parse_args(
        ["dashboard", "--port", "0", "--no-browser", "--host", "127.0.0.1"])
    outcome = []
    server_thread = threading.Thread(
        target=lambda: outcome.append(dash.cmd_dashboard(args)),
        daemon=True, name="s1-dashboard-under-test")
    server_thread.start()
    frames = None
    try:
        while "srv" not in bound and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "srv" in bound, "the server never bound"
        port = bound["srv"].server_address[1]

        # An `assert not release.is_set()` stood here and could never fail:
        # only this test sets that event, at a later line. It is deleted rather
        # than replaced. Nothing may BLOCK here either — the dashboard
        # republishes the seed with the hydration latch cleared about 50 ms
        # after it, so waiting before connecting loses the hydrating frame and
        # the assertion below fails for a reason that is not the product's.
        # `entered.wait()` therefore stays where it is, after the first read.

        with socket.create_connection(
            ("127.0.0.1", port), timeout=min(5.0, _remaining(deadline))
        ):
            pass

        frames = _sse_frames(port, deadline)
        first = next(frames, None)
        assert first is not None, "no SSE frame arrived while the builder was held"
        assert first.get("hydrating") is True, (
            "the first frame served before the full build must be hydrating; "
            f"got hydrating={first.get('hydrating')!r}")
        assert entered.wait(timeout=_remaining(deadline)), (
            "the background full build never started, so nothing was held and "
            "the hydrating frame above proves nothing")

        release.set()

        # The terminal condition must be one ONLY the authoritative build can
        # satisfy, which is why it is `sessions.rows` and not `hydrating`
        # alone. The dashboard republishes the still-empty seed with the
        # hydration latch cleared about 50 ms after the seed and BEFORE this
        # release, so a test that stopped at the first `hydrating is False`
        # frame accepted a republished seed and never read the real build —
        # and would have passed even if the released build never returned.
        # Measured, with the frames timestamped relative to `release.set()`
        # (session counts are whatever the profile carries; what matters is
        # empty versus non-empty, and which side of the release each lands on):
        #   t<0, before the release  hydrating=True   sessions=0
        #   t<0, before the release  hydrating=False  sessions=0  <- republished seed
        #   t>0, after the release   hydrating=False  sessions>0  <- authoritative
        filled = None
        for frame in frames:
            if (frame.get("hydrating") is False
                    and frame.get("sessions", {}).get("rows")):
                filled = frame
                break
        assert filled is not None, (
            "no authoritative frame (hydrating false AND non-empty sessions) "
            "arrived after the builder was released")
    finally:
        release.set()
        stop.set()
        # The generator holds the SSE response open, and `cmd_dashboard` runs a
        # daemon-threaded server that joins no handler. Closing it here runs the
        # generator's own `finally`, which releases the socket; without that the
        # `/api/events` handler stays blocked in `q.get` and outlives this item.
        if frames is not None:
            frames.close()
        # None of these three may draw from the body's budget: by the time the
        # test reaches its teardown that budget may be spent, and `_remaining`
        # would then hand each of them one second. Under contention the server
        # thread does not always exit inside that, and the leak guard then
        # fails the item for a thread the teardown meant to reap.
        #
        # They draw from ONE teardown budget rather than three of their own,
        # which is what keeps the test's worst case under the pytest cap. Three
        # independent waits of 30, 10 and 30 put the worst case at 160 against
        # a cap of 120, so a wedged product spent the whole budget and
        # pytest-timeout killed the worker mid-teardown — the undiagnosable red
        # #630 S1 removed — instead of one of these waits naming the thread it
        # was waiting for.
        teardown_deadline = time.monotonic() + _TEARDOWN_BUDGET_S
        server_thread.join(timeout=_remaining(teardown_deadline))
        _await_handler_exit(before_handlers, deadline=teardown_deadline)
        # `cmd_dashboard` also starts the conversation-sync daemon, which
        # `stop` above releases only between iterations. `bin/cctally-test-load-
        # invariance` caught it mid-prune, holding a commit past this item's
        # teardown, where a later test's process-global patch would have been
        # visible to it.
        _await_handler_exit(
            before_handlers, deadline=teardown_deadline,
            name_contains="dashboard-conversations-sync")


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
    # The real `_lib_perf`, not a stub: the callback now isolates the partial
    # build's thread state rather than consulting `enabled()` (#583 S1 §2.1).
    cb = tui._make_a2_progress_cb(
        ref=ref, hub=hub,
        build_partial=lambda: ns["_empty_dashboard_snapshot"](),
        throttle=throttle, monotonic=lambda: clock["t"],
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


def test_a2_progress_cb_publishes_under_perf_tracing(monkeypatch, tmp_path):
    """#583 S1 §2.1: isolation replaced suppression.

    Arming the trace used to switch progressive fill off — a diagnostic
    silently changing product behaviour, which is the second half of F32. The
    hazard it guarded against is real but narrower than a blanket skip: the
    partial build's unconditional `reset_thread()` rebinds `_tls.stack` while
    the enclosing `walk` phase still holds the ORIGINAL list in `Phase._stack`,
    so the outer phase would later close into a detached root. The partial now
    runs inside `isolated_thread_state()`, so both properties hold at once —
    the caller's tree survives and the partial publishes.
    """
    ns = _load_with_empty_roots(monkeypatch, tmp_path)
    import _cctally_tui as tui
    import _lib_perf as perf
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    hub = _CapturingHub()
    throttle = tui._A2ThrottleClock(0.0, start=0.0)  # always fires

    def build_partial():
        # Exactly what the real partial build does to this thread.
        perf.reset_thread()
        with perf.phase("partial-build"):
            pass
        return ns["_empty_dashboard_snapshot"]()

    cb = tui._make_a2_progress_cb(
        ref=ref, hub=hub, build_partial=build_partial,
        throttle=throttle, monotonic=lambda: 999.0,
    )
    perf.set_enabled(True)
    try:
        perf.reset_thread()
        walk = perf.phase("walk")
        walk.__enter__()
        stack_before = perf._tls.stack
        cb(None)
        cb(None)
        assert perf._tls.stack is stack_before, "the caller's stack was rebound"
        walk.__exit__(None, None, None)
        root = perf.current_root()
    finally:
        perf.set_enabled(False)
        perf.reset_thread()

    assert len(hub.published) == 2, "progressive fill was suppressed by tracing"
    assert all(s.hydrating for s in hub.published)
    assert root is walk, "the outer phase closed into a detached root"
    assert [c.name for c in root.children] == [], (
        "the isolated partial build's phases attached to the caller's tree")


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


def test_a2_partials_survive_a_live_perf_trace_integration(monkeypatch, tmp_path):
    """The whole refresh, with the trace armed: partials publish AND the
    final build's stashed tree is a well-formed `snapshot` root (§2.1)."""
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
    assert len(hydrating_frames) == 2, (
        f"tracing suppressed progressive fill: {hub.published}")
    assert hub.published[-1].hydrating is False, "final frame must be complete"
    last = perf.last_backend_perf()
    assert last is not None and last["phases"]["name"] == "snapshot", (
        f"the armed trace stashed no well-formed build root: {last}")


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

    # #583 S2: `sync_activity.server_epoch` is minted per SERVER PROCESS by
    # `_SnapshotRef`, so the published frame carries one and a direct build —
    # which never passes through a reference — cannot. It is publication state
    # rather than build output, and it is the ONE field excluded here; every
    # counter beside it stays under the byte-identity claim, which is what
    # proves the decoupling did not move the activity state either.
    for env in (env_decoupled, env_direct):
        env["sync_activity"]["server_epoch"] = "__EPOCH__"

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
