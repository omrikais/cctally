"""#630 S2 — two isolation detectors, loaded only where they are wanted.

Loaded with `-p tests._pytest_isolation_plugin` by
`bin/cctally-test-load-invariance`. Never registered through `pytest.ini` or a
`conftest.py`: both would change behaviour for anyone running pytest directly,
and `tests/conftest.py` belongs to another session.

The thread guard's contract is a REGISTRATION, not an inference. An earlier
design permitted threads created by fixtures broader than function scope until
their owning fixture finalized. That cannot be implemented against a
pre-setup baseline: such a thread is created during the first item's setup, is
absent from that item's baseline, and legitimately survives that item's
teardown, so the guard would fail it on first use. Instead, any thread created
during an item and alive after its teardown is a leak, and a fixture needing a
longer-lived thread registers it here. No such fixture exists today, so the
registration surface starts empty and the contract is enforceable rather than
aspirational.

MECHANISM. Both detectors report through `pytest_runtest_makereport` rather
than by raising inside a hookwrapper. An exception raised out of a hookwrapper
is an INTERNALERROR to pytest — it aborts the session with exit code 3 and
belongs to no item — so it could not satisfy the requirement that a leak fails
the item that caused it. Mutating the phase report attributes the failure to
that item, keeps the run going, and yields the ordinary exit code 1.

MECHANISM, the three phases. The state detector compares at setup, at call and
at teardown. The two earlier comparisons are made from `pytest_runtest_setup`
and `pytest_runtest_call` hookwrappers, which run inside the `CallInfo` for
their phase and therefore complete before that phase's report is built; each
stashes its finding on the item and `pytest_runtest_makereport` fails the
matching report. The call phase is the one that can see the F7 class at all:
`monkeypatch` reverts in a teardown finalizer, so by the time the teardown
report exists an ordinary reverting patch has already been undone, while for
the whole length of the test body every thread in the process resolves the
patched object.

The underscore prefix keeps this file out of collection and out of the
`test_*.py` estate scan. There is deliberately no `tests/__init__.py`: the
suite runs `python3 -m pytest` from the repository root, so `-m` puts that root
on `sys.path` and `tests` resolves as a namespace package.
"""
from __future__ import annotations

import sys
import threading
import traceback
import weakref

import pytest

# Weak references, not `id()`. CPython reuses an object address as soon as the
# object is collected, so a set of dead thread ids silently exempts whatever
# lands at the same address next — and the exemption is permanent, because
# nothing prunes it. A `WeakSet` drops each entry when its thread is collected,
# so the exemption lasts exactly as long as the thread it was granted for.
_REGISTERED: "weakref.WeakSet[threading.Thread]" = weakref.WeakSet()


def register_long_lived_thread(thread):
    """Exempt one thread from the leak guard, for a broader-scoped fixture."""
    _REGISTERED.add(thread)


def _live_threads():
    return {id(t): t for t in threading.enumerate()}


def _describe(thread):
    frames = ""
    try:
        frame = sys._current_frames().get(thread.ident)
        if frame is not None:
            frames = "".join(traceback.format_stack(frame)[-4:])
    except Exception:  # pragma: no cover - diagnostics must never mask the leak
        frames = "<stack unavailable>\n"
    return (
        f"    name={thread.name!r} ident={thread.ident} "
        f"daemon={thread.daemon} class={type(thread).__name__}\n{frames}"
    )


def _leaked_threads(item):
    before = getattr(item, "_isolation_threads_before", None)
    if before is None:
        return []
    return [
        t for ident, t in _live_threads().items()
        if ident not in before and t not in _REGISTERED and t.is_alive()
    ]


def _thread_failure(item, leaked):
    detail = "".join(_describe(t) for t in leaked)
    return (
        f"{item.nodeid} leaked {len(leaked)} thread(s) that outlived its "
        f"teardown:\n{detail}"
        f"A surviving thread can observe a process-global patch installed by a "
        f"later test, and the one documented contaminator in this repository "
        f"is a daemon thread, so daemons get no exemption. Join it, or "
        f"register it with register_long_lived_thread()."
    )


# --- the process-global state detector -------------------------------------
#
# Two parts, and the second is what makes this answer F9. A stdlib-only
# manifest cannot see the project module flags and caches that the five autouse
# reset fixtures in tests/conftest.py exist to restore.
#
# The manifest is FIXED rather than discovered. Walking every attribute of
# every loaded module would cost a great deal across 10,461 items; a fixed
# manifest costs tens of comparisons per phase.
#
# LIMITATION, stated rather than hidden: identity comparison cannot see a
# mutation INSIDE an object whose identity is unchanged. Mutable containers are
# therefore recorded by length as well, and a target for which neither is
# practical is listed in UNREACHABLE rather than silently claimed as covered.

STDLIB_MANIFEST = (
    ("sqlite3", "connect"),
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
    ("subprocess", "check_output"),
    ("os", "replace"), ("os", "fsync"), ("os", "read"), ("os", "link"),
    ("os", "rename"), ("os", "remove"),
    ("shutil", "which"), ("shutil", "copy2"), ("shutil", "copytree"),
    ("shutil", "rmtree"), ("shutil", "disk_usage"),
    ("urllib.request", "urlopen"),
    ("json", "loads"), ("json", "dumps"),
    ("time", "time"), ("time", "monotonic"), ("time", "sleep"),
    ("threading", "Thread"),
    ("sys", "platform"),
    ("http.client", "HTTPConnection"),
    ("socket", "socket"),
)

# The targets the five autouse reset fixtures in tests/conftest.py restore.
# Named individually, because a stdlib-only manifest cannot see any of them.
PROJECT_MANIFEST = (
    ("_lib_perf", "_ENABLED"),
    ("_lib_perf", "_LAST_BACKEND_PERF"),
    ("_cctally_core", "QUOTA_PROJECTION_RECONCILE_ENABLED"),
    ("_cctally_core", "MIGRATION_ERROR_LOG_PATH"),
    ("_lib_codex_conversation_query", "_outline_derivation_cache"),
)

UNREACHABLE = (
    "_lib_snapshot_cache's rebuild state is reached through eleven named "
    "reset_* functions rather than through exported attributes, so this "
    "detector cannot compare it. tests/conftest.py's "
    "_reset_snapshot_dispatch_state fixture calls every one of them before and "
    "after each test, and that fixture — not this detector — is what covers "
    "them.",
    "_cctally_dashboard_sources' Codex account-scope and quota-observation "
    "caches are cleared through reset_codex_account_scope_cache() and "
    "reset_codex_quota_observation_cache(), with no attribute to compare. "
    "Covered by the same conftest fixture.",
    "The process timezone is libc state that time.tzset() mutates. The TZ "
    "environment variable IS compared below, but the libc state it was applied "
    "to is not readable, so a test that calls tzset() after restoring TZ is "
    "invisible here. tests/conftest.py's _restore_process_timezone fixture "
    "re-applies TZ and re-runs tzset() at teardown, which is what covers it.",
    "Mutation INSIDE an object whose identity is unchanged is not detected in "
    "general. Mutable containers in the manifest are additionally recorded by "
    "length, which catches an addition or a removal but not a replacement.",
    "tests/_support_http._RESIDUE is a module-global this detector does not "
    "compare, and it is one the same session added to the estate this "
    "detector guards. It holds bytes a reader took off a socket and has not "
    "returned yet, keyed weakly by that socket. It is left out rather than "
    "registered because registering it would be worse than admitting it: a "
    "WeakKeyDictionary is not a dict, so _comparison_key files it under "
    "identity, and nothing ever rebinds the name — the row would compare "
    "equal on every item forever and read as coverage. Its length is not "
    "usable either, because an entry survives until its socket is collected, "
    "so a length comparison would report a leak for a connection the test "
    "closed correctly. Weak keys are what actually bound it: the residue for "
    "a closed socket is collected with the socket, so nothing has to "
    "unregister it and nothing outlives the item that created it.",
    "The project half of the manifest is compared at teardown only, because "
    "_LIVE_PHASE_KEYS holds the stdlib half alone. A project global that a "
    "test body patches and its own teardown reverts — _lib_perf's enablement "
    "flag flipped inside a body, say — is therefore never seen by the "
    "concurrency question the call phase asks; only residue after teardown is. "
    "The restriction is deliberate and its reason is recorded at "
    "_LIVE_PHASE_KEYS, but the gap it leaves is real and belongs here.",
)


def _comparison_key(value):
    """One comparison key per target, chosen by what "unchanged" means for it.

    A scalar is compared BY VALUE, because a flag restored to False is
    restored whatever object that False is. A filesystem path is compared by
    where it points, for the same reason and because `pathlib.Path` builds a
    fresh object every time: `_cctally_core.MIGRATION_ERROR_LOG_PATH` is
    re-derived whenever the module is re-initialised, and comparing its
    identity reported a leak every time that happened. A container is compared
    by identity AND length, which catches an addition or a removal but not a
    replacement. Everything else — every callable in the manifest — is
    compared by identity, which is exactly the question F7 asks.
    """
    import os

    if value is None or isinstance(value, (str, bool, int, float)):
        return ("value", value)
    if isinstance(value, os.PathLike):
        return ("path", os.fspath(value))
    if isinstance(value, (dict, list, set, tuple)):
        try:
            return ("container", id(value), len(value))
        except TypeError:  # pragma: no cover - defensive
            return ("identity", id(value))
    return ("identity", id(value))


def _snapshot_state(manifest=None):
    """Capture one comparison key for every manifest target that is loaded.

    `manifest` exists so a caller can ask about targets of its own without
    rebinding the module-level manifest, which this plugin reads on every hook
    of every item.

    `sys.modules.get`, never `importlib.import_module`. Importing a target that
    is not loaded yet would make this plugin the FIRST importer of it, and some
    of those module bodies do real work — `_cctally_core`'s calls
    `_init_paths_from_env()`. This runs at every setup, call and teardown, so
    in the load lane it would win that race against the item's own
    `load_script()`. Reading `sys.modules` answers the same question without
    changing the process, and it makes the "target absent, so skipped" branch
    honest rather than self-fulfilling.
    """
    import os

    if manifest is None:
        manifest = STDLIB_MANIFEST + PROJECT_MANIFEST
    seen = {}
    for module_name, attr in manifest:
        module = sys.modules.get(module_name)
        if module is None or not hasattr(module, attr):
            continue
        seen[(module_name, attr)] = _comparison_key(getattr(module, attr))
    seen[("os.environ", "TZ")] = ("value", os.environ.get("TZ"))
    return seen


# The setup and call phases compare the STDLIB half only, and the reason is a
# measured one rather than a preference. `tests/conftest.py` repoints
# `_cctally_core.MIGRATION_ERROR_LOG_PATH` and `LOG_DIR` from an autouse
# fixture, and an in-body `load_script()` plus `redirect_paths()` repoints them
# again; both are the reset stack doing its job, and comparing the project half
# while a test is still running would report every item in the estate. The
# project half is a RESIDUE question — did the reset stack put it back — and
# residue is exactly what the teardown comparison asks. The stdlib half is a
# CONCURRENCY question, live for as long as the patch is installed, so it is
# asked while the patch is still there.
#
# `os.environ["TZ"]` is compared at teardown only, for the same reason plus one
# more: process environment has no importer-local form, so a call-phase finding
# over it would name no repair.
_LIVE_PHASE_KEYS = frozenset(STDLIB_MANIFEST)


def _compare_state(before, after, *, only=None):
    changed = []
    for key, was in before.items():
        if only is not None and key not in only:
            continue
        now = after.get(key)
        if now is None:
            changed.append(f"{key[0]}.{key[1]}: {was} -> <absent>")
        elif now != was:
            changed.append(f"{key[0]}.{key[1]}: {was} -> {now}")
    return changed


_PHASE_DIAGNOSIS = {
    "setup": (
        "A fixture rebound a shared stdlib object and it is still rebound when "
        "the test body starts. Every thread in the process resolves the patched "
        "object for as long as it is installed, so a concurrent handler thread "
        "or worker sees it even though this fixture reverts correctly."
    ),
    "call": (
        "The test body rebound a shared stdlib object and it was still rebound "
        "when the body finished, before any finalizer ran. Reverting at "
        "teardown does not remove this: for the whole length of the body every "
        "thread in the process resolves the patched object."
    ),
    "teardown": (
        "A residual global is observable by every later test on this worker, "
        "and by any thread running concurrently with this one."
    ),
}


def _state_failure(item, phase, changed):
    return (
        f"{item.nodeid} modified process-global state at the {phase} phase:\n"
        "    " + "\n    ".join(changed) + "\n"
        + _PHASE_DIAGNOSIS[phase] + " Rebind the name on the IMPORTING module — "
        "build a types.SimpleNamespace copy of the stdlib module, patch the "
        "copy, and monkeypatch the importer's reference to it — instead of "
        "mutating the shared object."
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Snapshot both baselines before setup, for this item only."""
    item._isolation_threads_before = _live_threads()
    item._isolation_state_before = _snapshot_state()
    item._isolation_state_after_setup = None
    item._isolation_phase_problems = {}
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    """Compare after every fixture has set up, and keep that as the call baseline."""
    yield
    after = _snapshot_state()
    item._isolation_state_after_setup = after
    before = getattr(item, "_isolation_state_before", None)
    if not before:
        return
    changed = _compare_state(before, after, only=_LIVE_PHASE_KEYS)
    if changed:
        item._isolation_phase_problems["setup"] = _state_failure(
            item, "setup", changed)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Compare after the body runs and before any finalizer reverts anything.

    The baseline is the post-setup snapshot rather than the pre-setup one, so a
    patch a fixture installed is attributed to the setup phase that installed
    it and is not reported twice.
    """
    yield
    before = getattr(item, "_isolation_state_after_setup", None)
    if not before:
        return
    changed = _compare_state(before, _snapshot_state(), only=_LIVE_PHASE_KEYS)
    if changed:
        item._isolation_phase_problems["call"] = _state_failure(
            item, "call", changed)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    problems = []
    stashed = getattr(item, "_isolation_phase_problems", None) or {}
    if report.when in stashed:
        problems.append(stashed[report.when])
    if report.when == "teardown":
        leaked = _leaked_threads(item)
        if leaked:
            problems.append(_thread_failure(item, leaked))
        before = getattr(item, "_isolation_state_before", None)
        if before:
            changed = _compare_state(before, _snapshot_state())
            if changed:
                problems.append(_state_failure(item, "teardown", changed))
    if not problems:
        return
    joined = "\n\n".join(problems)
    if report.outcome == "failed" and report.longrepr is not None:
        report.longrepr = f"{report.longrepr}\n\n{joined}"
    else:
        report.outcome = "failed"
        report.longrepr = joined
