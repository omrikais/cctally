"""Test-temp hygiene shared by tests/conftest.py and the suite (#963, spec 5.2-5.4).

Every repair here goes through bin/_lib_test_temp.py: directory modes only,
no link following, one device, blocking flags only on singly linked owned
inodes. Nothing in this module may chmod a file or walk with os.walk-and-chmod.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import tempfile
import time

import pytest

import _lib_test_generation as G
import _lib_test_isolation as _iso
import _lib_test_temp as T


def release_owned(path):
    """Release an owned tree and return the findings recorded before each repair."""
    return T.release(path).findings


def remove_owned(path):
    return T.remove(path)


def release_or_fail(path, what):
    """Release ``path``; a refusal fails the caller (a link, device or flag the
    kernel must not touch is a defect in the test that made it)."""
    res = T.release(path)
    if res.refusals:
        pytest.fail("%s: %s" % (what, "; ".join("%s: %s" % (f.detail, f.path) for f in res.refusals)),
                    pytrace=False)


# --- the guard (#963 spec 5.2) ---------------------------------------------
#
# Detect, then release (J2): every pytest temp allocation is registered with
# its owner when it is made; the owner's trees are inspected when the owner
# finishes (a test after EVERY one of its finalizers, a shared fixture when it
# finishes, everything else once at session finish), and any finding is
# recorded in the isolation ledger BEFORE the kernel releases the tree. The
# recorded finding fails the owner whether or not the release succeeds.
HYGIENE_PREFIX = "temp hygiene: "
SESSION_BACKSTOP = "<session backstop>"
PROBLEMS = pytest.StashKey()       # list[str], stashed on an item after its teardown

_ITEM = [None]                     # the item whose runtest protocol is running
_FIXTURE_STACK = []                # fixturedefs whose setup is running
_OWNED = {}                        # ("item", nodeid) | ("fixture", id) | ("session", "") -> [path]
_PROVENANCE = {}                   # id(fixturedef) -> "fixture <name> (<scope>) first used by <nodeid>"
_SEEN = set()
STATS = {"allocations": 0, "released": 0, "inspect_s": 0.0, "release_s": 0.0, "backstop_s": 0.0}


def install():
    """Route every pytest temp allocation through one registration point.

    tmp_path, tmpdir, tmp_path_factory.mktemp and tmpdir_factory.mktemp all end
    in TempPathFactory.mktemp (tmpdir wraps tmp_path; TempdirFactory delegates
    to its TempPathFactory), so one wrapper sees all four APIs.
    """
    original = pytest.TempPathFactory.mktemp
    if getattr(original, "_cctally_hygiene", False):
        return

    def mktemp(self, basename, numbered=True):
        path = original(self, basename, numbered)
        _register(path)
        return path

    mktemp._cctally_hygiene = True
    pytest.TempPathFactory.mktemp = mktemp


def _owner():
    """The innermost non-function fixture being set up, else the running item."""
    for fixturedef in reversed(_FIXTURE_STACK):
        if fixturedef.scope != "function":
            key = id(fixturedef)
            if key not in _PROVENANCE:
                first = _ITEM[0].nodeid if _ITEM[0] is not None else "collection"
                _PROVENANCE[key] = "fixture %s (%s) first used by %s" % (
                    fixturedef.argname, fixturedef.scope, first)
            return ("fixture", key)
    if _ITEM[0] is not None:
        return ("item", _ITEM[0].nodeid)
    return ("session", "")


def _register(path):
    path = os.fspath(path)
    if path in _SEEN:
        return
    _SEEN.add(path)
    STATS["allocations"] += 1
    _OWNED.setdefault(_owner(), []).append(path)


def begin_item(item):
    _ITEM[0] = item


def end_item(item):
    _ITEM[0] = None


def on_fixture_setup(fixturedef):
    _FIXTURE_STACK.append(fixturedef)


def on_fixture_setup_done(fixturedef):
    if _FIXTURE_STACK and _FIXTURE_STACK[-1] is fixturedef:
        _FIXTURE_STACK.pop()


def _why(exc):
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


def _record(provenance, findings, recorded):
    for f in findings:
        if (f.path, f.detail) not in recorded:
            recorded.add((f.path, f.detail))
            _iso.record_finding(provenance, "temp-hygiene", "teardown", f.path, f.detail)


def check_and_release(path, provenance, clock="inspect_s"):
    """Inspect one owned tree; on any finding record it, then release (J2).

    The inspection's findings are recorded BEFORE the release runs, so the
    original violation stands whatever the repair does, including when the
    entry changed between the two walks and the release found nothing. What
    the release finds besides (a refusal, an identity change) is appended;
    an identical (path, detail) pair is recorded once. A crash of either walk
    is recorded against the same owner, never swallowed."""
    recorded = set()
    try:
        t0 = time.perf_counter()
        found = T.inspect(path).findings
        STATS[clock] += time.perf_counter() - t0
        if not found:
            return
        _record(provenance, found, recorded)
        t0 = time.perf_counter()
        repaired = T.release(path).findings
        STATS["release_s"] += time.perf_counter() - t0
        STATS["released"] += 1
        _record(provenance, repaired, recorded)
    except Exception as exc:   # a crash must still fail the owner
        _record(provenance, [T.Finding(os.fspath(path), "error", "inspection error: " + _why(exc))],
                recorded)


def after_teardown(item):
    """After EVERY finalizer of ``item``: its allocations first, then its ledger."""
    nodeid = item.nodeid
    for path in _OWNED.pop(("item", nodeid), []):
        _SEEN.discard(path)
        check_and_release(path, nodeid)
    item.stash[PROBLEMS] = list(_iso.collect_test_violations(nodeid))


def on_fixture_done(fixturedef):
    """A shared fixture's trees are checked when that fixture finishes, once.

    pytest may call the post-finalizer hook more than once for one setup; the
    second call finds nothing left to check."""
    if fixturedef.scope == "function":
        return
    key = id(fixturedef)
    provenance = _PROVENANCE.pop(key, "fixture %s (%s)" % (fixturedef.argname, fixturedef.scope))
    for path in _OWNED.pop(("fixture", key), []):
        _SEEN.discard(path)
        check_and_release(path, provenance)


def apply_to_report(item, report):
    """Fail ``item``'s teardown report with what after_teardown found."""
    problems = item.stash.get(PROBLEMS, [])
    if not problems:
        return
    isolation = [p for p in problems if not p.startswith(HYGIENE_PREFIX)]
    hygiene = [p for p in problems if p.startswith(HYGIENE_PREFIX)]
    blocks = []
    if isolation:
        blocks.append("isolation contract violated by this test (#529 S4):\n  " + "\n  ".join(isolation))
    if hygiene:
        blocks.append("temp hygiene violated by this test (#963):\n  " + "\n  ".join(hygiene))
    text = "\n".join(blocks)
    if report.failed:
        report.longrepr = "%s\n\n%s" % (report.longreprtext, text)
        return
    try:
        pytest.fail(text, pytrace=False)
    except pytest.fail.Exception:
        excinfo = pytest.ExceptionInfo.from_current()
    report.outcome = "failed"
    report.longrepr = item.repr_failure(excinfo)


_WORKER_ROOT = re.compile(r"^popen-gw[0-9]+$")


def session_backstop(session):
    """Every allocation whose owner never finished, and anything unregistered
    beneath this process's basetemp. A worker walks its own basetemp; only an
    xdist controller, which has workers, skips their ``popen-gw<N>`` roots. A
    serial or ``-n 0`` session inspects a directory of that name like any
    other. A basetemp that cannot be listed is an inspection error of the
    session, never an empty directory (spec 5.2, fail closed)."""
    t0 = time.perf_counter()
    for key, paths in list(_OWNED.items()):
        if key[0] == "fixture":
            provenance = _PROVENANCE.get(key[1], SESSION_BACKSTOP)
        elif key[0] == "item":
            provenance = key[1]
        else:
            provenance = SESSION_BACKSTOP
        for path in paths:
            check_and_release(path, provenance, "backstop_s")
    _OWNED.clear()
    _SEEN.clear()
    factory = getattr(session.config, "_tmp_path_factory", None)
    basetemp = getattr(factory, "_basetemp", None)   # never create one here
    if basetemp is not None:
        controller = (not os.environ.get("PYTEST_XDIST_WORKER")
                      and session.config.pluginmanager.has_plugin("dsession"))
        try:
            names = sorted(os.listdir(basetemp))
        except OSError as exc:
            _record(SESSION_BACKSTOP, [T.Finding(os.fspath(basetemp), "error",
                                                 "inspection error: " + _why(exc))], set())
            names = []
        for name in names:
            if controller and _WORKER_ROOT.match(name):
                continue
            check_and_release(os.path.join(str(basetemp), name), SESSION_BACKSTOP, "backstop_s")
    STATS["backstop_wall_s"] = time.perf_counter() - t0


def write_stats():
    name = "temp-hygiene-%s.json" % (os.environ.get("PYTEST_XDIST_WORKER") or "main")
    try:
        (_iso.session_dir() / name).write_text(json.dumps(STATS))
    except OSError:
        pass


def summary_line():
    """One controller line for P1: totals, and each clock's slowest process."""
    rows = []
    for path in sorted(_iso.session_dir().glob("temp-hygiene-*.json")):
        try:
            rows.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    if not rows or not sum(r.get("allocations", 0) for r in rows):
        return ""
    return ("temp hygiene summary: allocations=%d released=%d inspect_s_max=%.2f "
            "release_s_max=%.2f backstop_s_max=%.2f processes=%d" % (
                sum(r.get("allocations", 0) for r in rows), sum(r.get("released", 0) for r in rows),
                max(r.get("inspect_s", 0.0) for r in rows), max(r.get("release_s", 0.0) for r in rows),
                max(r.get("backstop_wall_s", 0.0) for r in rows), len(rows)))


class Plugin(object):
    """The guard's pytest hooks, registered as a GLOBAL plugin (#963).

    Not conftest hooks: pytest calls pytest_fixture_setup and
    pytest_fixture_post_finalizer through ``request.node.ihook``, and for a
    session-scoped fixture that node is the Session, whose hook proxy leaves
    out every conftest below the rootdir -- tests/conftest.py sits below the
    repository-root pytest.ini, so its fixture hooks never see a session
    fixture (measured on pytest 9.1.1). A plugin registered through the plugin
    manager receives every hook call regardless of path.
    """

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        begin_item(item)
        try:
            return (yield)
        finally:
            end_item(item)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        on_fixture_setup(fixturedef)
        try:
            return (yield)
        finally:
            on_fixture_setup_done(fixturedef)

    def pytest_fixture_post_finalizer(self, fixturedef, request):
        on_fixture_done(fixturedef)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self, item, nextitem):
        """Hygiene, then the isolation ledger, after EVERY finalizer of ``item``.

        Nothing is raised from here: the findings are stashed and the teardown
        report is failed in pytest_runtest_makereport, which keeps the call
        report intact and never turns a finding into an INTERNALERROR."""
        try:
            return (yield)
        finally:
            after_teardown(item)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        if call.when == "teardown":
            apply_to_report(item, report)
        return report


PLUGIN_NAME = "cctally-temp-hygiene"


def register(config):
    """Register the guard's hooks once per process (idempotent)."""
    if config.pluginmanager.get_plugin(PLUGIN_NAME) is None:
        config.pluginmanager.register(Plugin(), PLUGIN_NAME)


# --- nested launches (#963 spec 5.4, J4) ------------------------------------
_GENERATION = re.compile(r"^ctg-[0-9a-f]{8}$")


def nested_basetemp_args(parent, name="nested-bt"):
    """``--basetemp`` for a nested pytest session, inside the launching test's
    allocation. A fresh directory for every launch: pytest empties a basetemp
    before using it, so two launches must never share one, and the caller keeps
    it until it has read the child's results."""
    return ["--basetemp", tempfile.mkdtemp(prefix=name + "-", dir=str(parent))]


def child_aggregator_env(env, parent):
    """The environment for a test-launched bin/cctally-test-all.

    TMPDIR, TMP and TEMP name ``parent``/child-tmp, inside the launching
    test's allocation, so the child's suite generation, retained by design
    when the child is meant to fail, lies inside the outer generation. The
    outer run's PYTEST_DEBUG_TEMPROOT is dropped: the child claims its own.
    The outer run's owner-only variables are removed too (spec 4.3, #966(c)):
    the child never inherits the parent's outcome record, run id or evidence
    store. Apply the caller's explicit overrides AFTER this call; they may
    still name a child-local outcome destination.
    """
    child = dict(env)
    G.scrub_owner_env(child)
    child.pop("PYTEST_DEBUG_TEMPROOT", None)
    root = pathlib.Path(parent) / "child-tmp"
    root.mkdir(parents=True, exist_ok=True)
    for name in ("TMPDIR", "TMP", "TEMP"):
        child[name] = str(root)
    return child


def reclaim_child_generations(parent):
    """Remove, through the kernel, every suite generation a test-launched child
    aggregator left under ``parent``/child-tmp, once the caller has consumed
    the child's results. U5's retention governs only the outermost generation;
    a child's lives and dies with its test. A refusal fails the caller."""
    root = pathlib.Path(parent) / "child-tmp"
    results = []
    if not root.is_dir():
        return results
    for path in sorted(root.iterdir()):
        if not _GENERATION.match(path.name):
            continue
        res = T.remove(str(path))
        results.append(res)
        if res.refusals or not res.removed:
            pytest.fail("could not remove child generation %s: %s" % (
                path, "; ".join("%s: %s" % (f.detail, f.path) for f in res.findings)),
                pytrace=False)
    return results
