"""Shared pytest helpers for cctally tests.

Loads the main script (which has no .py extension and is not a
package) into a throwaway namespace so tests can exercise its
internals without running the CLI.
"""
import os
import pathlib
import shutil
import sys
import types

import pytest

# Dev-instance isolation and preamble parity (2026-05-26; widened #529 S4).
# The shell half of the estate neutralizes six variables in
# bin/_lib-harness-env.sh; this is the pytest half of the same contract.
#
# ASSIGN, never setdefault: setdefault lets an inherited runner value WIN,
# which is an inheritance hole rather than a pin, and it is exactly the class
# this contract exists to close. Set at conftest import because collection and
# module-level imports run before any fixture, and _cctally_core computes its
# path constants inside _init_paths_from_env() at import from these values --
# no fixture is early enough, and rewriting os.environ afterwards does not
# rebuild constants already derived. A test that wants a different value still
# uses monkeypatch.setenv, which mutates os.environ after this point and is
# reverted at teardown.
#
# A test MODULE must never set a determinism pin on os.environ, because
# pytest-xdist runs many modules per worker and the pin stays live for every
# sibling module that worker runs afterwards. This root conftest is the stated
# exception: it is imported once per worker before any test module is
# collected and applies uniformly, so it has no victim and no ordering
# dependence.
for _name, _value in (
    ("CCTALLY_DISABLE_DEV_AUTODETECT", "1"),
    ("CCTALLY_DISABLE_UPDATE_CHECK", "1"),
    ("CCTALLY_DISABLE_RETENTION_SWEEP", "1"),
):
    os.environ[_name] = _value
for _name in ("CODEX_HOME", "DO_NOT_TRACK", "CCTALLY_DISABLE_TELEMETRY"):
    os.environ.pop(_name, None)


_AGENTMEM_TEST_POLICIES = {
    "optional-local",
    "required",
    "hosted-private-unavailable",
}


def _agentmem_policy_error(
    policy: str, agentmem_path: str | None, github_actions: bool
) -> str | None:
    """Return the verification-lane dependency error, if any."""
    if policy not in _AGENTMEM_TEST_POLICIES:
        return (
            f"unsupported CCTALLY_AGENTMEM_TEST_POLICY={policy!r}; "
            f"expected one of {sorted(_AGENTMEM_TEST_POLICIES)}"
        )
    if policy == "required" and agentmem_path is None:
        return (
            "this verification lane requires pinned agentmem, but it is not "
            "on PATH; run bin/cctally-agentmem-dependency provision"
        )
    if policy == "hosted-private-unavailable" and not github_actions:
        return (
            "hosted-private-unavailable is reserved for GitHub Actions lanes "
            "that cannot read the separate private agentmem repository"
        )
    return None


def pytest_sessionstart(session) -> None:
    """Fail before collection when an enforced lane lacks agentmem."""
    policy = os.environ.get("CCTALLY_AGENTMEM_TEST_POLICY", "optional-local")
    error = _agentmem_policy_error(
        policy,
        shutil.which("agentmem"),
        os.environ.get("GITHUB_ACTIONS") == "true",
    )
    if error:
        raise pytest.UsageError(error)


def _script_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent / "bin" / "cctally"


# Compile bin/cctally once per pytest session and reuse the code object
# across every load_script() call. Each test still gets a fresh namespace via
# exec(), preserving isolation. Under pytest-xdist (`pytest -n <N>`) each
# worker is a fresh Python process, so this cache is per-worker and the
# compile cost is paid N times instead of once; see tests/requirements-dev.txt
# for the optional xdist dep.
#
# THE FIGURES THIS COMMENT USED TO CITE ARE STALE AND ARE CORRECTED HERE
# (#529 S4). It described a "26K-line script" whose compile() cost ~146 ms and
# whose exec() cost ~16 ms, and claimed the cache cut ~50 s off the suite.
# Measured on the runner today: bin/cctally is 3,602 lines and 186 KB,
# compile() takes 5.4 ms, and a warm exec() into a fresh namespace takes
# 0.2 ms. Nothing comes from a bytecode cache either — sys.dont_write_bytecode
# is True under pytest here and bin/__pycache__ does not exist — so a
# SourceFileLoader load of the same file costs 5.7 ms, which is compile plus
# exec and not a cache read.
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


# --- the fail-closed production-write detector (#529 S4) -------------------
#
# Installed at conftest import, before any test module is collected, because a
# module-level write in a test file would otherwise run unguarded. The audit
# hook cannot be removed once installed, so `install()` is idempotent and later
# calls only rebind the policy it reads.
import _lib_test_isolation as _iso  # noqa: E402  -- bin/ joins sys.path above

_iso.install()
_iso.install_popen_interceptor()


@pytest.fixture(scope="session", autouse=True)
def _isolation_scratch_root(tmp_path_factory):
    """Register the pytest temp base ONCE, not one root per test.

    The interceptor asks whether a child's environment-resolved APP_DIR sits
    beneath a scratch root before deciding to protect it, and that question is
    asked on every launch. Registering per test would grow the list without
    bound and make the answer cost O(tests).
    """
    _iso.register_scratch_root(tmp_path_factory.getbasetemp())
    yield


@pytest.fixture(autouse=True)
def _isolation_detector(request):
    """Attribute writes to the running test, and fail it from the ledger.

    Defined FIRST among the autouse fixtures in this file so it sets up first
    and tears down last: a violation caused by another fixture's teardown still
    has to be attributed and reported.

    Failing from the ledger rather than from the raised exception is the whole
    point. A test that catches `ProductionWriteBlocked` and returns normally is
    still failed here, because the hook recorded the violation before it raised.
    """
    node_id = request.node.nodeid
    _iso.set_node_id_getter(lambda: node_id)
    try:
        yield
    finally:
        problems = _iso.collect_test_violations(node_id)
    if problems:
        pytest.fail(
            "isolation contract violated by this test (#529 S4):\n  "
            + "\n  ".join(problems),
            pytrace=False,
        )


# --- the agentmem degradation announcement (#529 S6, M4) -------------------
#
# When `agentmem` is absent the run still completes, but it runs a WEAKER
# contract than the remote path does, and D2 requires it to say so: which
# contract ran, and how many tests that cost.
#
# The count is over items carrying the ONE shared gate object, compared by
# IDENTITY. No custom marker is registered, and none may be — pytest.ini records
# that settled decision, and the pre-plan review's marker proposal was rejected
# on that ground.
#
# Aggregation reuses S4's worker-private-ledger plus controller-aggregation
# pattern rather than inventing a second transport: each worker writes its own
# count into its own file under the same per-session directory the isolation
# ledger uses, and the controller — the only process whose exit status the run
# honours — sums them and emits ONE line.
from _agentmem_gate import AGENTMEM_PRESENT, requires_agentmem  # noqa: E402

_AGENTMEM_SKIP_FILE = "agentmem-skips"


def _agentmem_count_path():
    return _iso.state_dir() / _AGENTMEM_SKIP_FILE


def pytest_collection_modifyitems(session, config, items):
    """Record, per worker, how many collected items this gate would skip."""
    if AGENTMEM_PRESENT:
        return
    count = 0
    for item in items:
        for mark in item.iter_markers(name="skipif"):
            # Identity, not equality: the point of the single shared object is
            # that "carries THIS gate" is a fact rather than a resemblance.
            if mark is requires_agentmem.mark:
                count += 1
                break
    path = _agentmem_count_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(count), encoding="utf-8")
    except OSError:
        # Instrumentation must never take down the run it is instrumenting.
        # A missing file reads as zero, and the controller says so explicitly.
        pass


def _aggregate_agentmem_skips() -> int:
    """The run's count, which is the MAXIMUM across workers rather than the sum.

    Under `pytest -n N` every worker collects the WHOLE estate and only runs its
    share, so each worker's hook sees the same item list and records the same
    number. Summing them would announce N times the real figure — a number that
    is not merely imprecise but false, and false in a way that grows with the
    worker count. The maximum is the count of one complete collection, which is
    what "how many tests this run could not execute" means.
    """
    counts = [0]
    base = _iso.session_dir()
    try:
        children = sorted(base.iterdir())
    except OSError:
        children = []
    seen = set()
    for child in children:
        path = child / _AGENTMEM_SKIP_FILE
        if not path.is_file():
            continue
        seen.add(str(path))
        try:
            counts.append(int(path.read_text(encoding="utf-8").strip() or 0))
        except (OSError, ValueError):
            continue
    own = _agentmem_count_path()
    if own.is_file() and str(own) not in seen:
        try:
            counts.append(int(own.read_text(encoding="utf-8").strip() or 0))
        except (OSError, ValueError):
            pass
    return max(counts)


def _announce_agentmem_contract(session) -> None:
    """One line, from the controller, naming the contract and the cost."""
    policy = os.environ.get("CCTALLY_AGENTMEM_TEST_POLICY", "optional-local")
    if AGENTMEM_PRESENT:
        message = (
            f"agentmem contract: {policy} — agentmem is present, so the "
            f"agentmem-gated tests ran"
        )
    else:
        skipped = _aggregate_agentmem_skips()
        message = (
            f"agentmem contract: {policy} — agentmem is ABSENT, so "
            f"{skipped} agentmem-gated test(s) did not run; this run verified "
            f"less than a run with agentmem present"
        )
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(message)
    else:  # pragma: no cover - only when the terminal plugin is disabled
        print(message, file=sys.stderr)


def pytest_sessionfinish(session, exitstatus):
    """Late arrivals fail the SESSION, because their test's report is final.

    A child that writes after its launching test has already been reported
    cannot fail that test. E4 promises that no test PASSES when a covered write
    happens, not that the causing test is always the one that turns red, and
    this is the half of the criterion that difference exists for.

    THE CONTROLLER IS THE ONLY PROCESS WHOSE EXIT STATUS THE RUN HONOURS. Under
    ``pytest -n`` -- which is the configuration the authoritative gate runs, see
    ``bin/cctally-test-all`` phase 3 -- a worker's ``session.exitstatus`` never
    reaches the controller's exit code, measured as ``-n 2`` exiting 0 while
    this hook fired in gw0 and gw1. A worker therefore records what it found and
    returns; the controller reads every worker's ledger and decides the run.
    """
    if os.environ.get("PYTEST_XDIST_WORKER"):
        _iso.flush_late_handshake_problems()
        return
    # Controller-only, for the same reason the isolation aggregation is: under
    # `pytest -n` every worker reaches this hook, and announcing from each of
    # them would print the line once per worker instead of once per run.
    _announce_agentmem_contract(session)
    problems = _iso.collect_session_violations()
    if not problems:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    message = (
        "isolation contract violated after the reporting test finished "
        "(#529 S4):\n  " + "\n  ".join(problems)
    )
    if reporter is not None:
        reporter.write_line(message, red=True)
    else:  # pragma: no cover - only when the terminal plugin is disabled
        print(message, file=sys.stderr)
    session.exitstatus = 1


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
    # LOG_DIR too (#529 S4). `_log_migration_error` does
    # `_cctally_core.LOG_DIR.mkdir(parents=True, exist_ok=True)` BEFORE it opens
    # the log path, so pinning only the file left the DIRECTORY unpinned: every
    # test that reached this path created the real ~/.local/share/cctally/logs,
    # and by extension ~/.local/share/cctally. The prevention half of this
    # fixture was therefore incomplete in exactly the way its detection half was
    # never able to see — the identity check watches the log FILE, and creating
    # its parent directory does not change that file.
    #
    # THIS PIN IS WIDER THAN THIS FIXTURE'S NAME. LOG_DIR is not only the
    # migration log's parent: `HOOK_TICK_LOG_DIR` and the update log resolve
    # beside it, and this fixture is autouse, so from here on LOG_DIR is
    # decoupled from APP_DIR for EVERY test rather than only for the ones that
    # touch a migration. Nothing depends on the two agreeing today. What a
    # future reader must not do is write a negative assertion over LOG_DIR in a
    # module that redirects only APP_DIR and read a pass as evidence: it would
    # pass because of this line, not because of the code under test.
    monkeypatch.setattr(_cctally_core, "LOG_DIR", tmp_path / "logs")
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
def _stats_write_sanction(request):
    """#386: a pytest process is itself a sanctioned stats.db writer.

    `_cctally_store.arm_stats_authorizer` installs a SQLite authorizer on every
    stats connection the store hands out; outside a sanctioned write scope it
    returns `SQLITE_DENY`, and on a dev checkout (which every test run is) a
    denial RAISES. Spec section 3.1's regimes exist to serialize writers ACROSS
    PROCESSES under the multi-agent hook storm. An in-process pytest test has no
    storm and no second writer: it holds the DB alone, and its `open_db()`
    fixtures deliberately hand-construct states — pre-cutover legacy installs,
    orphaned milestones, conflicting journals — that no sanctioned path can
    produce. The fixture IS the serialized writer, so declaring it one is the
    truthful statement, not an exemption.

    **This does not disarm the guard.** The enforcement that matters runs in
    REAL CLI processes, which this fixture never touches:

      - every pytest test that spawns `bin/cctally` as a child. The child is a
        fresh process and inherits `PYTEST_CURRENT_TEST`, so
        `_guard_should_raise()` is True there — including all 24 real-subprocess
        cases in `tests/test_stats_writer_storm_386.py`;
      - `bin/cctally-*-test` shell harnesses that do NOT set
        `CCTALLY_DISABLE_DEV_AUTODETECT`, where `_is_dev_checkout()` is True;
      - a developer invoking `cctally` from the checkout.

    Measured, not assumed: `_is_dev_checkout()` returns False whenever
    `CCTALLY_DISABLE_DEV_AUTODETECT` is set (`bin/_cctally_core.py`), which
    several harness scenarios do — those run the guard in LOG-ONLY mode, and a
    green harness there is NOT evidence that a path is sanctioned. The
    subprocess-from-pytest surface above is the one that actually enforces.

    A module opts out by setting `CCTALLY_STATS_GUARD_LIVE = True` at module
    scope; `tests/test_stats_writer_guard_386.py` does, because its whole
    subject is what happens with no scope held.
    """
    if getattr(request.module, "CCTALLY_STATS_GUARD_LIVE", False):
        yield
        return
    import _cctally_store

    with _cctally_store.stats_write_scope("pytest-in-process"):
        yield


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
            "reset_codex_accounting_cache_state",
            "reset_doctor_memo",
            "reset_bugk_segment_state",
            # #271 M4: the projects-envelope current-week accumulator slot —
            # driven directly by the accumulator unit tests, so isolate it.
            "reset_projects_env_current_state",
            # #279 S5 F6.3: the owner-thread tripwire. Any test that runs the
            # real locked rebuild body arms it, and an arming that happened on
            # a server request thread that has since exited makes every LATER
            # test raise "mutation from non-owner thread" the moment it touches
            # the snapshot cache. Resetting it here makes that discipline
            # structural rather than a per-test `finally` somebody has to
            # remember. The tests that assert the tripwire stays armed
            # (tests/test_snapshot_cache_owner_thread.py) arm it INSIDE the
            # test, and this fixture runs between tests, so they are unaffected.
            "reset_owner_thread",
        ):
            _fn = getattr(_sc, _name, None)
            if _fn is not None:
                try:
                    _fn()
                except Exception:
                    pass
    try:
        import _cctally_dashboard_sources as _sources
        _sources.reset_codex_account_scope_cache()
        _sources.reset_codex_quota_observation_cache()
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


@pytest.fixture(autouse=True)
def _reset_quota_projection_reconcile_flag():
    """Isolate the #496 S5b per-process quota-projection arming between tests.

    ``_cctally_core.QUOTA_PROJECTION_RECONCILE_ENABLED`` is a PROCESS global,
    deliberately (see `enable_quota_projection_reconciliation`), and
    ``load_script()`` keeps ``_cctally_core`` across reloads so that module — and
    therefore the flag — outlives every test that set it. ``cmd_cache_sync``
    calls the setter for real, so any test exercising `cctally cache-sync` arms
    the whole worker process; a later test asserting that an UNARMED open never
    reads the journal then runs against an armed one and fails. That is not
    hypothetical: `test_an_ordinary_open_never_reads_the_journal_to_reconcile`
    failed exactly this way when `pytest -n`'s default `--dist load` put
    `tests/test_cache_sync_cli.py` on the same worker ahead of it.

    Tests that want the flag on still set it with ``monkeypatch``; this fixture
    only guarantees the starting state.
    """
    import _cctally_core

    saved = _cctally_core.QUOTA_PROJECTION_RECONCILE_ENABLED
    _cctally_core.QUOTA_PROJECTION_RECONCILE_ENABLED = False
    try:
        yield
    finally:
        _cctally_core.QUOTA_PROJECTION_RECONCILE_ENABLED = saved


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
        "CONVERSATIONS_DB_PATH": share / "conversations.db",
        "CACHE_LOCK_PATH": share / "cache.db.lock",
        "CACHE_LOCK_CODEX_PATH": share / "cache.db.codex.lock",
        "CACHE_LOCK_MAINTENANCE_PATH": share / "cache.db.maintenance.lock",
        "CONVERSATIONS_LOCK_PATH": share / "conversations.db.lock",
        "CONVERSATIONS_LOCK_CODEX_PATH": share / "conversations.db.codex.lock",
        "CONVERSATIONS_LOCK_MAINTENANCE_PATH": share / "conversations.db.maintenance.lock",
        "STATS_LOCK_MAINTENANCE_PATH": share / "stats.db.maintenance.lock",
        # Append-only observation journal (2026-07-22 DB journal redesign).
        # Pinned so any journal append/ingest a test triggers lands in the
        # per-test tmp APP_DIR, never the developer's real prod journal dir.
        "JOURNAL_DIR": share / "journal",
        "JOURNAL_LOCK_PATH": share / "journal.lock",
        "JOURNAL_INGEST_LOCK_PATH": share / "journal.ingest.lock",
        # Retained-evidence reclamation flock (#496 S6). Pinned for the same
        # reason as the journal locks: every producer takes it, so an unpinned
        # constant would serialize the whole test suite on the developer's real
        # prod lock file.
        "ARTIFACT_RETENTION_LOCK_PATH": share / "artifact-retention.lock",
        # Claude identity file (#341). Pinned so account-attribution reads (the
        # epoch-transition coordinator, record-usage/statusline stamping) hit the
        # per-test fake HOME, never the developer's real ~/.claude.json.
        "CLAUDE_JSON_PATH": tmp_path / ".claude.json",
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
        "STATUSLINE_CANDIDATE_DIR": share / "statusline-candidates",
        "STATUSLINE_SELECTED_PATH": share / "statusline-selected.json",
        "STATUSLINE_TRANSPORT_MARKER_PATH": share / "statusline-transport.last",
        "STATUSLINE_AUTHORITATIVE_7D_PATH": share / "statusline-authoritative-7d.json",
        "STATUSLINE_AUTHORITATIVE_5H_PATH": share / "statusline-authoritative-5h.json",
        # Host-global statusline cache (#529 S4). NOT under share/ — production
        # keeps it in /tmp because the real Claude Code statusline shares it, so
        # the fixture analogue is a per-test tmp file rather than an APP_DIR
        # child. A str, matching the production constant's type.
        "STATUSLINE_OAUTH_CACHE_PATH": str(tmp_path / "statusline-usage-cache.json"),
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


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Resolve every HOME-derived path constant under a per-test directory.

    Opt a whole module in with, at module scope::

        pytestmark = pytest.mark.usefixtures("isolated_home")

    This is the narrow pin for the module whose tests call ``load_script()``
    themselves and never take ``tmp_path``. ``redirect_paths`` cannot serve
    them: it patches ``_cctally_core`` attributes, and the very next
    ``load_script()`` re-runs ``_init_paths_from_env()`` and rebinds every one
    of them from the current ``HOME`` — the ordering trap ``load_script``'s own
    docstring describes. Pinning ``HOME`` instead survives an arbitrary number
    of later ``load_script()`` calls, because each of them re-derives from it.

    Introduced by #529 S4, when the write detector caught 19 modules whose
    ``load_config()`` reached ``ensure_dirs()`` and created the maintainer's
    REAL ``~/.local/share/cctally``. Use ``redirect_paths`` instead wherever a
    test already receives ``tmp_path`` and wants the full explicit surface.

    It deliberately does NOT call ``_init_paths_from_env()`` itself. Doing so
    rebinds all 23 constants, which silently destroys a patch an earlier
    fixture already applied — ``TestSelfHealCurrentVersion``'s class-scoped
    autouse fixture pins ``CHANGELOG_PATH`` away from the dev tree, and
    re-deriving put it back, so the issue #42 dev-clone guard short-circuited
    and the test read a stale version. Pinning only the environment leaves
    every existing patch intact and lets the next ``load_script()`` do the
    re-derivation, which is what it already does at its top.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    yield


@pytest.fixture
def isolated_paths(isolated_home):
    """``isolated_home`` plus an immediate re-derivation of the constants.

    Opt in the same way::

        pytestmark = pytest.mark.usefixtures("isolated_paths")

    Use this, and not ``isolated_home``, for a module that loads ``bin/cctally``
    or a sibling through ``SourceFileLoader`` or at module import. Those paths
    never call ``_init_paths_from_env()``, so pinning ``HOME`` alone changes
    nothing: the constants keep whatever values the previous test on this xdist
    worker left behind. That is why the modules needing this were green when run
    alone and red under ``-n 4`` — the difference was which module happened to
    run first on the worker, which is the machine dependence #529 S4 exists to
    remove.

    It re-derives eagerly, which REBINDS all 23 constants and would destroy a
    patch applied earlier in the same test. That is safe only because it runs
    before the test body and before any fixture that patches a path constant;
    where that ordering does not hold, use ``isolated_home`` and let the
    module's own ``load_script()`` do the re-derivation.
    """
    import _cctally_core

    _cctally_core._init_paths_from_env()
    yield


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


@pytest.fixture(autouse=True)
def _reset_outline_derivation_cache():
    """Isolate the #463 S4 outline derivation cache between tests.

    ``_lib_codex_conversation_query`` retains up to four ``EventDerivation``
    objects keyed by conversation key for the process lifetime. Nothing has
    collided yet only because the key embeds a per-test ``tmp_path`` hash, so
    two tests that staged the same conversation under one root would cross-
    contaminate — which is a property of the fixtures, not of the cache.
    Mirrors ``_reset_perf_state``: a no-op for every test that never loads the
    module.
    """
    try:
        import _lib_codex_conversation_query as _q  # bin/ is on sys.path
    except Exception:
        _q = None

    def _reset():
        if _q is None:
            return
        try:
            _q.reset_outline_derivation_cache()
        except Exception:
            pass

    _reset()
    yield
    _reset()


# ── #583 S1: the shared bench corpus ──────────────────────────────────────


def _load_bench_generator():
    import importlib.machinery
    import importlib.util

    bin_dir = pathlib.Path(__file__).resolve().parents[1] / "bin"
    if str(bin_dir) not in sys.path:
        sys.path.insert(0, str(bin_dir))
    loader = importlib.machinery.SourceFileLoader(
        "build_bench_fixtures", str(bin_dir / "build-bench-fixtures.py"))
    spec = importlib.util.spec_from_loader("build_bench_fixtures", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def corpus_lock_path(corpus_root, scale):
    """The flock file serialising one scale's build. Public so a test that
    rebuilds the shared corpus takes the SAME lock the fixture takes."""
    return pathlib.Path(corpus_root) / f".build-{scale}.lock"


@pytest.fixture(scope="session")
def corpus_root(tmp_path_factory):
    """One shared bench-corpus root per RUN, in both execution modes.

    Under xdist `getbasetemp()` is `<numbered>/popen-gwN`, so the run's own
    numbered directory is its parent and every worker resolves to the same
    place. Serially there is no worker directory and `getbasetemp()` IS the
    numbered directory. Taking the parent unconditionally was wrong in the
    serial mode reachable through `CCTALLY_PYTEST_JOBS=1`, `CCTALLY_TEST_JOBS=1`
    or a checkout without pytest-xdist: it resolved to `<tmp>/pytest-of-<user>`,
    which pytest never rotates, so the corpus persisted across runs and two
    concurrent runs shared it.
    """
    base = tmp_path_factory.getbasetemp()
    numbered = base.parent if base.name.startswith("popen-gw") else base
    root = numbered / "s1-bench-corpus"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture(scope="session")
def shared_corpus(corpus_root):
    """Build (or reuse) a bench corpus by scale. FAILS on error; never skips.

    F28 is the worked example of the alternative: a gate that skips when its
    fixture is absent, whose fixture nothing builds, is silently inert forever.

    `build_fixture` now takes its OWN flock, on `build_lock_path(root)`, which
    is a sibling of the corpus root. This fixture's lock is a DIFFERENT file
    (`<corpus_root>/.build-<scale>.lock`), so the two never deadlock — and the
    outer one is still wanted, for a reason the inner one cannot serve. The
    inner lock is taken only AFTER `build_fixture` has re-checked its marker
    and decided to rebuild, so it serialises rebuilds; the outer lock also
    serialises the marker check itself and the `open_fixture_db` reads this
    fixture's callers make immediately afterwards, against a concurrent
    worker's `_clear_previous_corpus`. Without it a reader can open `cache.db`
    in the window between another worker deleting the corpus and rebuilding it.

    A per-test tmp_path would be safe but would discard reuse, so every gate
    would pay a full production ingest. The flock gives one build per scale per
    run instead — the first worker in builds, the rest block briefly and then
    hit the marker.

    `build_fixture_isolated` restores the pinned environment on return;
    `build_fixture` itself deliberately leaves the process pinned.
    """
    import fcntl

    generator = _load_bench_generator()
    built = {}

    def _build(scale):
        if scale in built:
            return built[scale]
        root = corpus_root / scale
        root.mkdir(parents=True, exist_ok=True)
        with open(corpus_lock_path(corpus_root, scale), "w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                built[scale] = generator.build_fixture_isolated(
                    scale=scale, seed=42, root=root)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return built[scale]

    return _build


@pytest.fixture(scope="session")
def small_corpus(shared_corpus):
    """The built `small` corpus data dir — the larger half of the >=10x pair."""
    return shared_corpus("small")


@pytest.fixture(scope="session")
def tiny_corpus(shared_corpus):
    """The built `tiny` corpus data dir — the cheap half of the >=10x pair.

    Spec §7.1 needs one tick over two corpora whose Claude AND Codex row counts
    differ by at least 10x. `tiny` and `small` differ by 14.3x and 12.5x and
    both build in the ordinary suite; `large` is the maintainer receipt and is
    far too slow for a gate under the pytest phase's --timeout=120.
    """
    return shared_corpus("tiny")
