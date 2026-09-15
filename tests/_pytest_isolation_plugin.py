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

MECHANISM. Both detectors DETECT through `pytest_runtest_makereport` rather
than by raising inside a hookwrapper. An exception raised out of a hookwrapper
is an INTERNALERROR to pytest — it aborts the session with exit code 3 and
belongs to no item — so it could not satisfy the requirement that a leak fails
the item that caused it. Mutating the phase report attributes the failure to
that item, keeps the run going, and yields the ordinary exit code 1.

MECHANISM, where the verdict is REPORTED. Detection alone reported a leaking
item as both passed and errored: pytest counts the call phase separately and
its taxonomy calls a non-passing teardown an ERROR, so the summary said an item
passed while the run failed because of it. A leak is not observable before
teardown and the call report is already built by then, so the verdict is MOVED
rather than duplicated. `pytest_report_teststatus` decides which counter a
report lands in, independently of which phase produced it: it suppresses the
status of a passing call report that carries no `wasxfail`, and lets the
teardown report — the phase that can see the leak — carry the item's single
verdict. A leak therefore reads as a FAILED item named in the short summary.
The teardown report still keeps `outcome = "failed"`, so `Session.testsfailed`
increments and the run exits 1. The move is bounded on both sides: a non-strict
XPASS keeps pytest's own status, and a teardown this plugin did not fail keeps
pytest's ERROR verdict.

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

import collections
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

#: The CLOSED vocabulary a manifest row may use to declare its pristine value.
#:
#: An arbitrary callable is refused because a callable could import a module or
#: perform work, and this code runs at every setup, call and teardown of every
#: item. A "whatever appears first" sentinel is refused because it would bless
#: the very leak the row exists to detect.
PRISTINE_KINDS = ("literal", "env_flag", "derived_path", "empty_container")


class PristineSpec(collections.namedtuple("PristineSpec", ("kind", "args"))):
    """What a manifest target holds in an untouched process.

    ``args`` is a tuple of ``(name, scalar)`` pairs rather than a mapping, so
    the whole value is immutable and no row can smuggle a mutable container
    into the manifest.
    """

    __slots__ = ()

    def __new__(cls, kind, args):
        if kind not in PRISTINE_KINDS:
            raise ValueError(
                f"unknown pristine kind {kind!r}; expected one of "
                f"{PRISTINE_KINDS}")
        args = tuple(args)
        for pair in args:
            name, value = pair
            if not isinstance(name, str) or not name:
                raise TypeError(
                    f"a pristine spec argument needs a name; got {name!r}")
            if not isinstance(value, (str, bool, int, float, type(None))):
                raise TypeError(
                    f"a pristine spec argument must be an immutable scalar; "
                    f"{name} holds {type(value).__name__}")
        return super().__new__(cls, kind, args)


def literal(value):
    """A scalar the attribute holds outright.

    Scalar only: `_comparison_key` files a container under its identity, so a
    container literal could never equal the module's own object and the row
    would report a leak on every first import of that module.
    """
    return PristineSpec("literal", (("value", value),))


def env_flag(variable, true_value="1"):
    """A boolean derived from the process environment at import time.

    No manifest row uses this today, and the one candidate deliberately does
    not: see `_lib_perf._ENABLED` below.
    """
    return PristineSpec(
        "env_flag", (("variable", variable), ("true_value", true_value)))


def derived_path(module_attr, suffix):
    """A path the module derives from another of its own attributes."""
    return PristineSpec(
        "derived_path", (("module_attr", module_attr), ("suffix", suffix)))


def empty_container(type):  # noqa: A002 - the argument names what it declares
    """An empty container of a named type, e.g. `collections.OrderedDict`."""
    return PristineSpec("empty_container", (("type", type),))


def _resolve_dotted(name):
    import importlib

    module_name, _, attr = name.rpartition(".")
    return getattr(importlib.import_module(module_name), attr)


def _spec_matches(spec, module, value):
    """Whether ``value`` is what ``spec`` says an untouched process holds.

    ``module`` is the live module object, because two of the four kinds are
    relative to it: a derived path is built from a sibling attribute, and a
    row whose base attribute does not exist yet cannot be judged at all and is
    accepted rather than reported.
    """
    import os

    args = dict(spec.args)
    if spec.kind == "literal":
        return _comparison_key(value) == _comparison_key(args["value"])
    if spec.kind == "env_flag":
        return bool(value) is (
            os.environ.get(args["variable"]) == args["true_value"])
    if spec.kind == "derived_path":
        base = getattr(module, args["module_attr"], None)
        if base is None:
            # The base attribute is what the derived one is measured against,
            # and a module that does not carry it yet cannot be judged. This
            # is the residual limitation recorded on the row itself: a first
            # import that coherently leaks BOTH the base and the derived path
            # is outside this row's reach.
            return True
        return (_comparison_key(value)
                == ("path", os.path.join(os.fspath(base), args["suffix"])))
    if spec.kind == "empty_container":
        try:
            expected_type = _resolve_dotted(args["type"])
        except (ImportError, AttributeError):  # pragma: no cover - defensive
            return True
        if type(value) is not expected_type:
            return False
        try:
            return len(value) == 0
        except TypeError:  # pragma: no cover - defensive
            return False
    raise ValueError(f"unknown pristine kind {spec.kind!r}")


# The targets the five autouse reset fixtures in tests/conftest.py restore.
# Named individually, because a stdlib-only manifest cannot see any of them.
#
# A row is `(module, attr)` or `(module, attr, pristine)`. The optional third
# element is what the attribute holds in an untouched process, and it exists
# because a key that is ABSENT at setup and PRESENT at teardown is compared
# against nothing by the before-keyed residue check: every `import
# _lib_ingest_frontier` in tests/ is inside a function body, so on a worker
# where the bench test runs first the row below would have stayed silent
# through the very leak it names. Eager-importing the module here is ruled out
# — see `_snapshot_state` — so a row that wants the appeared case covered
# declares its pristine value instead.
PROJECT_MANIFEST = (
    # `literal(False)`, NOT `env_flag`. The value IS environment-derived at
    # import (`bin/_lib_perf.py:25` reads CCTALLY_PERF_TRACE), but the per-item
    # pristine contract is defined by the FIXTURE, not by the environment:
    # `tests/conftest.py:529`'s autouse `_reset_perf_state` calls
    # `set_enabled(False)` before and after every item, and this plugin's
    # baseline is captured before fixture setup. An `env_flag` spec would
    # expect True on the first item of a run with CCTALLY_PERF_TRACE=1 while
    # the correct post-fixture state is False, which is a false positive.
    ("_lib_perf", "_ENABLED", literal(False)),
    ("_lib_perf", "_LAST_BACKEND_PERF", literal(None)),
    # `bin/_lib_perf.py:307`, beside its sibling and reset by the same fixture.
    # The manifest simply omitted it.
    ("_lib_perf", "_LAST_INGEST_PERF", literal(None)),
    ("_cctally_core", "QUOTA_PROJECTION_RECONCILE_ENABLED", literal(False)),
    # Derived from the module's own LOG_DIR at `bin/_cctally_core.py:158`, so
    # no literal can encode it: the directory moves with the data dir.
    # RESIDUAL LIMITATION: the spec is evaluated against LOG_DIR as the module
    # currently holds it, so a first-import test that coherently leaks BOTH the
    # base and the derived path is outside this row's reach. A row whose base
    # attribute is absent is accepted rather than reported, because it cannot
    # be judged at all.
    ("_cctally_core", "MIGRATION_ERROR_LOG_PATH",
     derived_path(module_attr="LOG_DIR", suffix="migration-errors.log")),
    # An empty `collections.OrderedDict` in an untouched process. No literal
    # can encode that, because `_comparison_key` files a container under its
    # identity, which a manifest cannot predict. When the key APPEARS the spec
    # compares type and zero length; when it was already present the ordinary
    # residue comparison covers it by identity plus length.
    ("_lib_codex_conversation_query", "_outline_derivation_cache",
     empty_container(type="collections.OrderedDict")),
    # #740: `bin/cctally-bench` rebinds this to `float("inf")` for the length of
    # a measurement run, on the module object `_load_sibling` shares through
    # `sys.modules`. An unrestored assignment made every later certificate test
    # on the same xdist worker read `inf` as its age bound, and the failure was
    # reported against that later test rather than against the bench run. The
    # scoped restore in `_suspend_frontier_expiry` is the fix; this row is what
    # names the culprit if the class recurs.
    # 120.0 is `_lib_ingest_frontier`'s own documented default, restated here
    # because reading it would require importing the module this plugin must
    # not import. A change to that default must be mirrored into this row, or
    # the appeared-key check reports every first import of the module.
    ("_lib_ingest_frontier", "FRONTIER_CERTIFICATE_MAX_AGE_SECONDS",
     literal(120.0)),
    # #769 S6 added two process-global containers to the same module, and
    # neither was reachable by this detector. `_FRONTIER_COUNTERS` accumulates
    # the deterministic plan/visit counts a benchmark asserts the SHAPE of a
    # tick from, so residue in it silently inflates a later reading.
    # `_FRONTIER_GENERATIONS` is the worse of the two: it holds one coordinator
    # per data directory, and a coordinator holds CONSUMER REGISTRATIONS, so a
    # leaked entry is precisely the state in which a later test sees a
    # registration made by an earlier one — a generation that never retires,
    # or a peer acknowledgement reconciled against a store that is gone.
    #
    # Both are empty in an untouched process, and both are dicts, so the
    # ordinary residue comparison covers them by identity plus length once the
    # key exists; the declared pristine value is what covers the APPEARED case,
    # because every `import _lib_ingest_frontier` in tests/ is inside a
    # function body and the module may not be present at baseline capture.
    ("_lib_ingest_frontier", "_FRONTIER_COUNTERS",
     empty_container(type="builtins.dict")),
    ("_lib_ingest_frontier", "_FRONTIER_GENERATIONS",
     empty_container(type="builtins.dict")),
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


#: Every manifest row that declares a pristine value, by target. Built once, at
#: module level, because it is read on every teardown of every item.
_DECLARED_SPECS = {
    (entry[0], entry[1]): entry[2]
    for entry in STDLIB_MANIFEST + PROJECT_MANIFEST
    if len(entry) > 2
}


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
    for entry in manifest:
        # Only the first two elements: a row may also declare a pristine value.
        module_name, attr = entry[0], entry[1]
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
_LIVE_PHASE_KEYS = frozenset(
    (entry[0], entry[1]) for entry in STDLIB_MANIFEST)


def _compare_state(before, after, *, only=None):
    """Every manifest key whose value moved between two snapshots.

    Two passes, because a key can be missing from either side. The first
    compares each key present in `before` against `after`, which is the residue
    question. The second covers the case the first structurally cannot see: a
    key ABSENT at setup because its module was not loaded yet, and PRESENT at
    teardown because the item imported it. Such a key has no baseline, so it is
    compared against the pristine value its manifest row DECLARES, and only a
    row that declares one is examined. Reporting every appeared key would fire
    on every item that first imports a manifest module, which is noise; a row
    with a declared default reports only a value that differs from it.
    """
    changed = []
    for key, was in before.items():
        if only is not None and key not in only:
            continue
        now = after.get(key)
        if now is None:
            changed.append(f"{key[0]}.{key[1]}: {was} -> <absent>")
        elif now != was:
            changed.append(f"{key[0]}.{key[1]}: {was} -> {now}")
    for key, spec in _DECLARED_SPECS.items():
        if only is not None and key not in only:
            continue
        if key in before:
            continue
        now = after.get(key)
        if now is None:
            continue
        module = sys.modules.get(key[0])
        if module is None or not hasattr(module, key[1]):
            continue
        # Read ONCE. Judging a live read and then reporting the teardown
        # snapshot would let the message name a value the verdict did not use.
        live = getattr(module, key[1])
        if _spec_matches(spec, module, live):
            continue
        changed.append(
            f"{key[0]}.{key[1]}: <module not loaded at setup> -> {live}, "
            f"against the declared pristine value {spec}")
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
        if report.when == "teardown":
            _BLAMED_TEARDOWNS.add(report.nodeid)
        report.outcome = "failed"
        report.longrepr = joined


#: Node ids whose CALL phase passed. `pytest_report_teststatus` sees a report
#: rather than an item, and a teardown report alone cannot tell a clean item
#: from one that errored in SETUP and so never ran its call phase at all.
_CALL_PASSED: "set[str]" = set()

#: Node ids whose TEARDOWN report THIS PLUGIN failed, written by
#: `pytest_runtest_makereport` at the one branch that sets `report.outcome`.
#: `pytest_report_teststatus` reads it and reclassifies a failed teardown as
#: FAILED only for a member. Without it the reclassification fired on any
#: failed teardown report, so an ordinary fixture finalizer that raised was
#: reported as FAILED instead of pytest's ERROR, and an item that errored in
#: both setup and teardown read `1 error, 1 failed` where pytest reads
#: `2 errors`. The plugin changes the verdict only for problems it raised.
_BLAMED_TEARDOWNS: "set[str]" = set()


def pytest_sessionstart(session):
    """Clear both node-id sets, because a process can run more than one session.

    Within one session each set is bounded by the run. Across two in-process
    `pytest.main()` calls a repeated node id would otherwise inherit the
    earlier session's verdict — a stale PASSED, or a stale FAILED reclassifying
    a teardown error the plugin did not raise this time.
    """
    _CALL_PASSED.clear()
    _BLAMED_TEARDOWNS.clear()


@pytest.hookimpl(tryfirst=True)
def pytest_report_teststatus(report, config):
    """Emit each item's single verdict from the phase that can see a leak.

    pytest's own taxonomy calls a non-passing TEARDOWN an ERROR and counts the
    CALL phase separately, so a leaking item was reported as both passed and
    errored — a summary line saying an item passed when the run failed because
    of it. A leak is not observable before teardown and the call report is
    already built by then, so the fix is to move the verdict rather than to
    mutate a completed phase: suppress the status of a passing call report, and
    let the teardown report carry passed or failed for the whole item.

    The suppression covers every passing call report that carries no
    `wasxfail`, because at call time nothing yet knows whether the item will
    leak. Every other outcome is left alone. A call-phase FAILURE keeps its own
    verdict here and gains no second one at teardown. A skip reports outcome
    `skipped`, so it never enters this branch. A non-strict XPASS DOES report
    outcome `passed`, and it is excluded by name: pytest marks such a report
    with a `wasxfail` attribute, and suppressing it turned an XPASS into a
    PASSED emitted from teardown. An item that errored in SETUP never entered
    `_CALL_PASSED`, so it acquires no teardown pass.

    A failed teardown is reclassified as FAILED only when this plugin is the
    one that failed it. Any other failed teardown — a fixture finalizer that
    raised on its own — falls through to pytest's own ERROR verdict, which is
    also why that path returns rather than reaching the `_CALL_PASSED` check
    below: an item whose teardown really errored must not be reported PASSED.
    """
    if (report.when == "call" and report.outcome == "passed"
            and not hasattr(report, "wasxfail")):
        _CALL_PASSED.add(report.nodeid)
        return "", "", ""
    if report.when == "teardown":
        if report.outcome == "failed":
            if report.nodeid in _BLAMED_TEARDOWNS:
                return "failed", "F", "FAILED"
            return None
        if report.nodeid in _CALL_PASSED:
            return "passed", ".", "PASSED"
    return None
