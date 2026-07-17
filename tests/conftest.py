"""Shared pytest helpers for cctally tests.

Loads the main script (which has no .py extension and is not a
package) into a throwaway namespace so tests can exercise its
internals without running the CLI.
"""
import os
import pathlib
import sys
import types

import pytest

# Dev-instance isolation (2026-05-26): force dev-checkout auto-detect OFF
# for the whole pytest process. Set at conftest import — earlier than any
# fixture — so the import-time _init_paths_from_env() in _cctally_core and
# every load_script() reload resolve the prod data-dir layout under the
# per-test fake HOME, not the cctally-dev layout. setdefault so an explicit
# env value (a test that WANTS to exercise auto-detect) still wins.
os.environ.setdefault("CCTALLY_DISABLE_DEV_AUTODETECT", "1")


def _script_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "bin" / "cctally"


# Compile bin/cctally once per pytest session and reuse the code object
# across every load_script() call. Profiling: compile() is ~146ms,
# exec() ~16ms; the 26K-line script is reparsed 335+ times in a full
# pytest run, so caching the code object cuts ~50s off suite wallclock.
# Each test still gets a fresh namespace via exec(), preserving isolation.
# Under pytest-xdist (`pytest -n <N>`) each worker is a fresh Python
# process, so this cache is per-worker — the compile cost is paid N
# times instead of once. That's a small constant-cost regression
# (~146ms × N) which is well within the parallel speedup; see
# tests/requirements-dev.txt for the optional xdist dep.
_SCRIPT_PATH = _script_path()
_SCRIPT_CODE = compile(_SCRIPT_PATH.read_text(), str(_SCRIPT_PATH), "exec")

# Ensure bin/ is on sys.path so tests can do `import _cctally_core` at the
# top of the file. After 2026-05-22 (issue #84) the 23 in-scope path
# globals live in _cctally_core; tests monkeypatch them via
# ``monkeypatch.setattr(_cctally_core, "X", v)``. The module-top import
# stays stable across ``load_script()`` reloads because the load_script
# preserves ``_cctally_core`` in sys.modules (see note in load_script).
_BIN_DIR = str(_SCRIPT_PATH.parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)


# Captured at import under the developer's REAL HOME — before any test
# monkeypatches HOME — so the guard below watches the ACTUAL prod log a leaking
# test would pollute, not a per-test fake-HOME path. CCTALLY_DISABLE_DEV_AUTODETECT
# (set above) means an un-redirected _cctally_core resolves APP_DIR to exactly
# this prod layout, so this is the file at risk.
_REAL_PROD_MIGRATION_LOG = (
    pathlib.Path.home() / ".local" / "share" / "cctally" / "logs" / "migration-errors.log"
)


def _migration_log_identity():
    """(mtime_ns, size) of the real prod migration-errors.log, or None if absent."""
    try:
        st = _REAL_PROD_MIGRATION_LOG.stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


@pytest.fixture(autouse=True)
def _guard_real_prod_migration_log(tmp_path, monkeypatch):
    """Isolate AND guard the migration-error log for every test (#190).

    PREVENTION. Redirect ``_cctally_core.MIGRATION_ERROR_LOG_PATH`` to a per-test
    tmp file so a migration log write that escapes a test's own path setup lands
    in tmp, not the developer's real prod log. conftest forces
    ``CCTALLY_DISABLE_DEV_AUTODETECT=1`` (above), so an un-redirected
    ``_cctally_core`` otherwise resolves that path to the real
    ``~/.local/share/cctally/logs/migration-errors.log``. ~38 migration tests run
    the dispatcher without redirecting it; a fixture whose ``session_entries``
    predates the ``speed`` column (e.g.
    ``test_cache_001_actually_runs_on_pre_framework_upgrade``) makes cache
    migration 008's ``UPDATE … speed`` fail ``no such column: speed``, and
    ``_log_migration_error`` then writes that fake failure to the developer's
    REAL prod log. The prod statusline renders it as a banner the prod binary
    never clears (it fast-paths an already-applied DB). The sentinel helpers read
    ``_cctally_core.MIGRATION_ERROR_LOG_PATH`` at CALL time, so this setattr is
    honored even by modules already imported; tests that set their OWN log path
    still win (their ``monkeypatch.setattr`` runs after this fixture).

    DETECTION. A test that re-derives the path back to prod mid-run (e.g.
    ``_init_paths_from_env()`` under the real HOME without re-redirecting) would
    escape the setattr above, so ALSO snapshot the real prod log's identity and
    assert it is untouched at teardown — naming any straggler. Run the suite
    serially when bisecting; under pytest-xdist a sibling worker's legitimate
    write could be misattributed.
    """
    import _cctally_core

    monkeypatch.setattr(
        _cctally_core, "MIGRATION_ERROR_LOG_PATH", tmp_path / "migration-errors.log"
    )
    before = _migration_log_identity()
    yield
    after = _migration_log_identity()
    assert after == before, (
        "this test wrote to the developer's REAL prod migration-errors.log "
        f"({_REAL_PROD_MIGRATION_LOG}); a migration log write escaped path "
        "isolation (the autouse redirect was overwritten mid-test — likely a "
        "bare _init_paths_from_env() under the real HOME). Re-redirect "
        "_cctally_core.MIGRATION_ERROR_LOG_PATH to a tmp path after any such "
        "re-init — use the redirect_paths(ns, monkeypatch, tmp_path) helper. "
        f"identity before={before} after={after}"
    )


@pytest.fixture(autouse=True)
def _restore_process_timezone():
    """Immunize the whole suite against cross-test timezone leaks.

    Several tests pin a non-UTC host tz with ``monkeypatch.setenv("TZ", ...)``
    followed by ``time.tzset()`` so ``datetime.astimezone()`` observes that
    zone (e.g. test_derive_week_utc_anchor and test_dashboard_period_builders
    use ``America/Los_Angeles``). ``monkeypatch`` restores the TZ *env var* at
    teardown but NOT the process-global libc tz state that ``tzset()`` mutated
    — so the leaked zone persists for every later test sharing the same
    pytest-xdist worker, flipping tz-derived date/bucket boundaries by a day.
    That surfaced on Linux CI as flaky, scheduling-dependent failures in
    test_share_top_projects / test_share_period_resolver / test_project_budget_alerts.
    Snapshot TZ at setup, then re-apply it + re-run tzset() at teardown so libc
    always reverts — independent of which test leaked or monkeypatch's
    finalization order. tzset() is a no-op on the rare non-POSIX host.
    """
    import time

    saved_tz = os.environ.get("TZ")
    try:
        yield
    finally:
        if saved_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved_tz
        if hasattr(time, "tzset"):
            time.tzset()


@pytest.fixture(autouse=True)
def _reset_snapshot_dispatch_state():
    """Clear ALL #268 rebuild-cache module state before each test.

    ``_lib_snapshot_cache`` holds several pieces of process-global rebuild state:
    the idle-path ``(signature, snapshot)`` memo, the Group A ``BucketCache`` +
    per-builder watermarks, the ``SessionCache`` + its watermark, and the doctor
    payload memo. NONE of those signatures encode the DB path, so two tests with
    structurally identical (e.g. empty) DBs produce the SAME keys — without this
    reset a prior test's leftover snapshot or cached past-bucket could be served
    into a later test's first ``_tui_build_snapshot`` call, returning stale rows
    from a different tmp DB. Resetting every cache before each test isolates them;
    a no-op for the vast majority of tests that never build a snapshot. Each reset
    is guarded so a run that hasn't loaded the module (or an older module missing a
    helper) is unaffected. (The monotonic generation counter is intentionally NOT
    reset — it bears no cached data, and the dispatch-state reset already prevents
    a stale idle-serve.)
    """
    try:
        import _lib_snapshot_cache as _sc  # bin/ is on sys.path (see top)
    except Exception:
        _sc = None
    if _sc is not None:
        for _name in (
            "reset_dispatch_state",
            "reset_group_a_state",
            "reset_session_cache_state",
            "reset_doctor_memo",
            "reset_bugk_segment_state",
            # #271 M4: the projects-envelope current-week accumulator slot —
            # driven directly by the accumulator unit tests, so isolate it.
            "reset_projects_env_current_state",
        ):
            _fn = getattr(_sc, _name, None)
            if _fn is not None:
                try:
                    _fn()
                except Exception:
                    pass
    yield


@pytest.fixture(autouse=True)
def _reset_perf_state():
    """Isolate the opt-in backend-perf collector (#276) between tests.

    ``_lib_perf`` holds two pieces of process-global state: the module
    ``_ENABLED`` flag and the ``_LAST_BACKEND_PERF`` stash slot. A test that
    enables tracing and builds a snapshot (e.g. ``test_perf_snapshot``) stashes
    a completed ``snapshot`` tree into that slot; without a reset the slot then
    LEAKS into a later same-process test that reads ``last_backend_perf()`` —
    the ``/api/debug/backend`` shape test asserts ``phases is None`` when
    tracing is off, and a leaked stash flips it non-null. (It only survived the
    parallel suite because the two files happened to land on different
    pytest-xdist workers.) Force the flag off, clear the stash, and clear this
    thread's in-flight tree before AND after every test. A no-op for the vast
    majority that never load the module; mirrors ``_reset_snapshot_dispatch_state``.
    """
    try:
        import _lib_perf as _perf  # bin/ is on sys.path (see top)
    except Exception:
        _perf = None

    def _reset():
        if _perf is None:
            return
        try:
            _perf.set_enabled(False)
            _perf.reset_thread()
            _perf._LAST_BACKEND_PERF = None
        except Exception:
            pass

    _reset()
    yield
    _reset()


def load_script():
    """Execute the main script and return its globals dict.

    The dict IS the namespace of a real types.ModuleType registered in
    sys.modules['cctally']. Two facts make this work without behaviour
    change for tests:
      1. exec(code, mod.__dict__) populates the module's namespace from
         the script's globals, and `mod.__dict__ is ns` afterwards.
      2. Attribute lookup on a module reads its __dict__; mutating the
         dict (monkeypatch.setitem(ns, "X", v)) is immediately visible
         as mod.X for siblings that import cctally.

    Net: tests keep their `ns["X"]` / `monkeypatch.setitem(ns, "X", v)`
    patterns AND `import cctally; cctally.X` from sibling lazy modules
    sees the same value. Per-test isolation: each call rebuilds a fresh
    module and re-binds sys.modules['cctally'] (latest call wins).

    Drops cached `_cctally_*.py` sibling modules from sys.modules so
    that when PEP 562 (or the dispatch thunk) next triggers
    `_load_sibling("_cctally_release")`, the sibling re-executes its
    `import cctally` against the FRESH cctally module — not the stale
    instance from the previous test's load_script(). Without this clear,
    `_cctally_release.cctally` remains pinned to the prior module, so
    monkeypatches on the new `cctally.CHANGELOG_PATH` don't propagate
    into MOVED helpers, and tests that monkeypatch real-path constants
    leak writes to the on-disk repo. Spec §5.5 (circular-import safety)
    + §6.0a.

    EXCEPTION: ``_cctally_core`` is the kernel and does NOT
    ``import cctally`` (it uses the call-time ``_cctally()`` accessor),
    so its module-load state is safe across reloads. After 2026-05-22
    (issue #84) the 23 in-scope path globals live in
    ``_cctally_core``; keeping the same instance in sys.modules lets
    tests monkeypatch ``_cctally_core.X`` via a stable module-top
    ``import _cctally_core`` reference without it going stale on the
    next ``load_script()`` call. To preserve the pre-#84 behavior where
    each ``load_script()`` re-derived path constants from the current
    HOME env var, we explicitly call
    ``_cctally_core._init_paths_from_env()`` here. That re-runs the
    same Path.home() / "..." logic against the current env without
    needing a fresh import, so tests doing ``setenv("HOME", tmp) +
    load_script()`` see fresh, HOME-derived path constants — same
    contract as before #84.

    TRAP — patch ordering matters. ``_init_paths_from_env()`` runs at
    the top of EVERY ``load_script()`` call and rebinds every promoted
    global (``APP_DIR``, ``DB_PATH``, ``CLAUDE_SETTINGS_PATH``, etc.)
    from the current ``HOME`` env var. This will CLOBBER any prior
    ``monkeypatch.setattr(_cctally_core, "X", v)`` that ran BEFORE
    ``load_script()``. The correct ordering is ALWAYS:

        ns = load_script()                                        # FIRST
        monkeypatch.setattr(_cctally_core, "X", tmp)              # THEN

    or use the ``redirect_paths(ns, monkeypatch, tmp_path)`` helper
    below which handles ordering correctly. Reversing the order
    silently leaks the patched paths to the host machine — no
    exception, no warning, just stale values from the unpatched
    ``_init_paths_from_env()`` reset.

    Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md §6.0a
    """
    for _name in [n for n in sys.modules if n.startswith("_cctally_") and n != "_cctally_core"]:
        del sys.modules[_name]
    # Re-derive _cctally_core's path constants from the current HOME env
    # var. Tests doing `setenv("HOME", tmp) + load_script()` rely on
    # this to surface a fresh path set under the test's HOME without
    # re-importing _cctally_core. Must run BEFORE the bin/cctally exec
    # below so the script's `APP_DIR = _cctally_core.APP_DIR` re-export
    # block snapshots the updated values.
    core = sys.modules.get("_cctally_core")
    if core is not None and hasattr(core, "_init_paths_from_env"):
        core._init_paths_from_env()
    mod = types.ModuleType("cctally")
    mod.__file__ = str(_SCRIPT_PATH)
    sys.modules["cctally"] = mod
    exec(_SCRIPT_CODE, mod.__dict__)
    return mod.__dict__


def redirect_paths(ns, monkeypatch, tmp_path):
    """Pin the kernel's path constants to a tmp dir.

    After 2026-05-22 (issue #84), the 23 in-scope path constants live
    in bin/_cctally_core.py and `_cctally_core` is the single legal
    monkeypatch target. Every reader (every sibling AND bin/cctally
    itself) goes through `_cctally_core.X` at call time.

    The `ns[X]` MIRROR below is NOT a second patch surface — it just
    keeps `bin/cctally`'s eager re-exports (`cctally.APP_DIR` etc.) in
    sync with the kernel patches, so tests that *read* `ns["X"]` to
    introspect values (e.g. `ns["CONFIG_PATH"].read_text()`) see the
    fixture-redirected paths instead of stale module-load snapshots.
    Tests must STILL patch via `monkeypatch.setattr(_cctally_core,
    "X", v)` (the AST guard at `test_kernel_extraction_invariants.py`
    enforces this for `test_*.py` files; conftest itself is exempt).

    Note: ``CHANGELOG_PATH`` is intentionally NOT redirected. It
    resolves to ``<repo>/CHANGELOG.md`` based on the binary's own
    filesystem location, not HOME — there is no fixture analogue
    inside ``tmp_path`` to point it at, and existing tests that need
    to override it (e.g. tests/test_release_internals.py) do so with
    their own `monkeypatch.setattr(_cctally_core, "CHANGELOG_PATH",
    …)` in the per-test fixture.

    As of the data-globals promotion (2026-05-22, #84), `_cctally_db`
    reads its four path constants
    (``DB_PATH``/``CACHE_DB_PATH``/``LOG_DIR``/``MIGRATION_ERROR_LOG_PATH``)
    via ``_cctally_core.X`` at call time, so the kernel patches above
    propagate directly without a sibling-side re-patch block — the
    previous seed-and-re-patch pair was a vestige of the pre-#84
    bare-name pattern and has been removed.
    """
    share = tmp_path / ".local" / "share" / "cctally"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    paths = {
        "APP_DIR": share,
        "LEGACY_APP_DIR": tmp_path / ".local" / "share" / "ccusage-subscription",
        "LOG_DIR": share / "logs",
        "DB_PATH": share / "stats.db",
        "CACHE_DB_PATH": share / "cache.db",
        "CACHE_LOCK_PATH": share / "cache.db.lock",
        "CACHE_LOCK_CODEX_PATH": share / "cache.db.codex.lock",
        "CACHE_LOCK_MAINTENANCE_PATH": share / "cache.db.maintenance.lock",
        "CONFIG_LOCK_PATH": share / "config.json.lock",
        "CONFIG_PATH": share / "config.json",
        "MIGRATION_ERROR_LOG_PATH": share / "logs" / "migration-errors.log",
        "HOOK_TICK_LOG_DIR": share / "logs",
        "HOOK_TICK_LOG_PATH": share / "logs" / "hook-tick.log",
        "HOOK_TICK_LOG_ROTATED_PATH": share / "logs" / "hook-tick.log.1",
        "HOOK_TICK_THROTTLE_PATH": share / "hook-tick.last-fetch",
        "HOOK_TICK_THROTTLE_LOCK_PATH": share / "hook-tick.last-fetch.lock",
        # Statusline usage-persistence markers + lock (spec 2026-07-17).
        # Pinned so a persist/backoff write during any test lands in the
        # per-test tmp APP_DIR, never the developer's real prod data dir.
        "STATUSLINE_OBSERVE_MARKER_PATH": share / "statusline-observe.last",
        "STATUSLINE_PERSIST_LOCK_PATH": share / "statusline-persist.lock",
        "OAUTH_BACKOFF_MARKER_PATH": share / "oauth-backoff.until",
        "OAUTH_BACKOFF_COUNT_PATH": share / "oauth-backoff.count",
        "UPDATE_STATE_PATH": share / "update-state.json",
        "UPDATE_SUPPRESS_PATH": share / "update-suppress.json",
        "UPDATE_LOCK_PATH": share / "update.lock",
        "UPDATE_LOG_PATH": share / "update.log",
        "UPDATE_LOG_ROTATED_PATH": share / "update.log.1",
        "UPDATE_CHECK_LAST_FETCH_PATH": share / "update-check.last-fetch",
        # Anonymous install-count telemetry markers (spec 2026-07-07). Pinned
        # here so a beat/arm during a test writes install_id + markers to the
        # per-test tmp APP_DIR, never the developer's real prod data dir.
        "TELEMETRY_INSTALL_ID_PATH": share / "install_id",
        "TELEMETRY_LAST_BEAT_PATH": share / "telemetry.last-beat",
        "TELEMETRY_NOTICE_SHOWN_PATH": share / "telemetry.notice-shown",
        "TELEMETRY_FIRST_SEEN_PATH": share / "telemetry.first-seen",
        "CLAUDE_SETTINGS_PATH": tmp_path / ".claude" / "settings.json",
    }

    core = sys.modules["_cctally_core"]
    for name, value in paths.items():
        monkeypatch.setattr(core, name, value)
        # Mirror the patch into bin/cctally's namespace so tests that
        # read `ns["X"]` for introspection see the fixture-redirected
        # paths. NOT a second patch target — only the `_cctally_core`
        # patch above propagates to actual readers. This mirror is for
        # test introspection only; per-test `setitem(ns, "<PROMOTED>",
        # …)` from a test_*.py file is still forbidden (AST guard at
        # `tests/test_kernel_extraction_invariants.py`).
        monkeypatch.setitem(ns, name, value)

    # Note: `_cctally_db` used to require sibling-side re-patching of
    # DB_PATH / CACHE_DB_PATH / LOG_DIR / MIGRATION_ERROR_LOG_PATH
    # because it consumed them via bare-name reads against a seeded
    # `_cctally_db.__dict__`. As of the data-globals promotion
    # (2026-05-22, #84) it reads via `_cctally_core.X` at call time, so
    # the kernel patches above propagate directly — no extra block here.

    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)


def load_isolated_cctally_module(tmp_path, monkeypatch):
    """Load bin/cctally as a real module under the canonical isolated data dir.

    Shared by the ``*_ns_patch.py`` ``cctally_mod`` fixtures. These fixtures
    patch ``cctally_mod.<X>`` and assert the handler reaches those names via
    the ``_cctally()`` accessor, so they need the module OBJECT (not just the
    globals dict) — but they ALSO need the same ``_cctally_core`` path
    redirection every other test gets.

    Issue #127: the previous bespoke loader only ``setenv("HOME", …)`` and
    relied on ``_cctally_core``'s import-time ``_init_paths_from_env()`` to
    pick up the tmp HOME. That holds ONLY when ``_cctally_core`` is imported
    fresh (test run in isolation). Once any prior test has cached
    ``_cctally_core`` in ``sys.modules`` (every ``load_script()`` user does),
    the bespoke loader skipped re-derivation and the handler read the
    developer's REAL ``~/.local/share/cctally/stats.db`` — intermittently
    failing once that DB held a ``week_reset_events`` row matching the current
    week. Going through ``load_script() + redirect_paths()`` pins
    ``_cctally_core``'s path constants to ``tmp_path`` deterministically,
    independent of import order.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return sys.modules["cctally"]


@pytest.fixture(scope="session")
def cctally_module():
    """Expose bin/cctally as an attribute-accessible namespace.

    Wraps load_script()'s dict in a SimpleNamespace so unit tests can
    write `cctally_module.add_column_if_missing(...)` instead of dict
    indexing. Reuses the cached compiled code object so this is cheap.
    """
    return types.SimpleNamespace(**load_script())
