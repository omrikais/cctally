"""G1 (#963 spec 9.1): the temp hygiene guard, observed through a nested pytest
that loads the real tests/conftest.py on its own explicit basetemp.

pytest's own reports are read through a tap plugin, because the spec requires
the offending node's TEARDOWN report to fail; a nonzero exit is not enough.
"""
from __future__ import annotations

import glob
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The tap records exactly one report per (nodeid, when). Under xdist a `-p`
# plugin loads in the controller AND in every worker, and the controller
# re-fires pytest_runtest_logreport for each report a worker forwards, so an
# unguarded tap writes every report twice under `-n 2`. The one writer is the
# process without `config.workerinput`: the serial process, the `-n 0` process
# (xdist loaded, no workers) or the `-n 2` controller. That process sees the
# final report pytest's verdict is built from in all three modes. The rule is
# the one tests/_pytest_durations_plugin.py's _writer_is_disabled applies.
TAP = '''
import json, os

_WRITER = [True]

def pytest_configure(config):
    _WRITER[0] = not hasattr(config, "workerinput")

def pytest_runtest_logreport(report):
    if not _WRITER[0]:
        return
    with open(os.environ["HYG_TAP_OUT"], "a") as fh:
        fh.write(json.dumps({"nodeid": report.nodeid, "when": report.when,
                             "outcome": report.outcome, "text": report.longreprtext}) + "\\n")
'''

CASES = '''
import os, pathlib, pytest

def _ro_chain(root):
    (root / "a" / "b").mkdir(parents=True)
    os.chmod(root / "a" / "b", 0o555)
    os.chmod(root / "a", 0o555)

def _ro_symlink_only(root):
    d = root / "s"
    d.mkdir()
    os.symlink("/nonexistent-g1-target", str(d / "link"))
    os.chmod(d, 0o555)

def _no_exec(root):
    d = root / "x"
    d.mkdir()
    (d / "f").write_text("x")
    os.chmod(d, 0o600)

def test_tmp_path_chain(tmp_path):
    _ro_chain(tmp_path)

def test_tmpdir_symlink_only(tmpdir):
    _ro_symlink_only(pathlib.Path(str(tmpdir)))

def test_factory_missing_execute(tmp_path_factory):
    _no_exec(tmp_path_factory.mktemp("direct"))

def test_legacy_factory_chain(tmpdir_factory):
    _ro_chain(pathlib.Path(str(tmpdir_factory.mktemp("legacy"))))

def test_call_failure_is_preserved(tmp_path):
    _ro_chain(tmp_path)
    assert False, "the call phase failed on its own"

@pytest.fixture
def leaky(tmp_path):
    yield tmp_path
    _ro_chain(tmp_path)

def test_finalizer_leak(leaky):
    pass

def test_restored_tree_is_green(tmp_path):
    _ro_chain(tmp_path)
    os.chmod(tmp_path / "a", 0o755)
    os.chmod(tmp_path / "a" / "b", 0o755)

def test_read_only_file_is_green(tmp_path):
    f = tmp_path / "ro.txt"
    f.write_text("x")
    os.chmod(f, 0o444)

@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    root = tmp_path_factory.mktemp("shared")
    yield root
    _ro_chain(root)

def test_shared_first(shared):
    pass

def test_shared_second(shared):
    pass
'''

STRAY = '''
import os

def test_leaves_unregistered_material(tmp_path_factory):
    stray = tmp_path_factory.getbasetemp() / "stray"
    (stray / "a").mkdir(parents=True)
    os.chmod(stray / "a", 0o555)
'''

OFFENDERS = ("test_tmp_path_chain", "test_tmpdir_symlink_only", "test_factory_missing_execute",
             "test_legacy_factory_chain", "test_finalizer_leak", "test_call_failure_is_preserved")
CONTROLS = ("test_restored_tree_is_green", "test_read_only_file_is_green", "test_shared_first",
            "test_shared_second")


def _run(tmp_path, source, *extra, env_extra=None):
    child = tmp_path / "child"
    child.mkdir()
    (child / "test_g1_cases.py").write_text(source)
    tap = tmp_path / "tap"
    tap.mkdir()
    (tap / "hyg_tap.py").write_text(TAP)
    out = tap / "reports.jsonl"
    # The fresh-environment shape tests/test_isolation_contract.py::_fresh_env
    # builds, so the child starts its own isolation session.
    env = {"HOME": str(tmp_path / "fake-home"), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin",
           "PYTHONPATH": os.pathsep.join([str(REPO / "tests"), str(REPO / "bin"), str(tap)]),
           "HYG_TAP_OUT": str(out)}
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "hyg_tap", "-q",
         "-p", "no:cacheprovider", "--basetemp", str(tmp_path / "bt"), *extra, str(child)],
        env=env, capture_output=True, text=True, timeout=110, cwd=str(child))
    reports = [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []
    return proc, reports


def _report(reports, name, when):
    hits = [r for r in reports if r["nodeid"].endswith("::" + name) and r["when"] == when]
    assert len(hits) == 1, "the tap recorded %d %s reports for %s (expected exactly one): %r" % (
        len(hits), when, name, reports)
    return hits[0]


def _undeletable(root):
    """Directories below ``root`` missing an owner permission bit (by lstat)."""
    bad = []
    for dirpath, dirnames, _files in os.walk(root):
        for d in dirnames:
            st = os.lstat(os.path.join(dirpath, d))
            if stat.S_ISDIR(st.st_mode) and st.st_mode & 0o700 != 0o700:
                bad.append(os.path.join(dirpath, d))
    return bad


@pytest.mark.parametrize("jobs", [None, "2"])
def test_a_violation_fails_its_owning_nodes_teardown(tmp_path, jobs):
    proc, reports = _run(tmp_path, CASES, *(() if jobs is None else ("-n", jobs)))
    for name in OFFENDERS:
        rep = _report(reports, name, "teardown")
        assert rep["outcome"] == "failed", (
            "the teardown report did not fail for %s:\n%s" % (name, proc.stdout))
        assert "temp hygiene: " in rep["text"] and name in rep["text"], rep["text"]
    assert "left directory missing owner rwx (mode 0o555) by " in \
        _report(reports, "test_tmp_path_chain", "teardown")["text"]
    assert "left directory missing owner rwx (mode 0o600) by " in \
        _report(reports, "test_factory_missing_execute", "teardown")["text"]
    call = _report(reports, "test_call_failure_is_preserved", "call")
    assert call["outcome"] == "failed" and "the call phase failed on its own" in call["text"], call
    for name in CONTROLS:
        assert _report(reports, name, "teardown")["outcome"] == "passed", name
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 1, combined
    assert "by fixture shared (module) first used by " in combined, (
        "a shared fixture's violation did not fail the session with its provenance:\n" + combined)
    assert _undeletable(tmp_path / "bt") == [], "the guard reported a tree it did not release"


def test_unregistered_material_fails_the_session_with_its_provenance(tmp_path):
    proc, reports = _run(tmp_path, STRAY)
    assert _report(reports, "test_leaves_unregistered_material", "teardown")["outcome"] == "passed"
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 1, "the session did not fail on unregistered material:\n" + combined
    assert "left directory missing owner rwx (mode 0o555) by <session backstop>" in combined, combined


RECLAIM = '''
import os, pytest

def _fill(root, name):
    (root / "data").mkdir()
    (root / "data" / ("marker-" + name)).write_bytes(b"x" * 4096)

def test_pass_tmp_path(tmp_path):
    _fill(tmp_path, "pass_tmp_path")

def test_pass_factory(tmp_path_factory):
    _fill(tmp_path_factory.mktemp("passfactory"), "pass_factory")

def test_pass_skipped(tmp_path):
    _fill(tmp_path, "pass_skipped")
    pytest.skip("a skip is not a failure")

def test_fail_call(tmp_path):
    _fill(tmp_path, "fail_call")
    assert False, "the call failed"

@pytest.fixture
def setup_fails(tmp_path):
    _fill(tmp_path, "fail_setup")
    raise RuntimeError("the setup failed")

def test_fail_setup(setup_fails):
    pass

@pytest.fixture
def teardown_fails(tmp_path):
    yield tmp_path
    raise RuntimeError("the teardown failed")

def test_fail_teardown(teardown_fails):
    _fill(teardown_fails, "fail_teardown")

@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    root = tmp_path_factory.mktemp("sharedmod")
    _fill(root, "shared_fixture")
    return root

def test_pass_uses_shared(shared):
    pass

@pytest.mark.xfail(reason="an expected failure keeps its evidence")
def test_xfail_expected(tmp_path):
    _fill(tmp_path, "xfail")
    assert False

@pytest.mark.xfail(reason="an unexpected pass keeps its evidence too")
def test_xpass_unexpected(tmp_path):
    _fill(tmp_path, "xpass")

def test_pass_with_only_an_isolation_problem(tmp_path):
    import _lib_test_isolation as iso
    _fill(tmp_path, "isolation_problem")
    node = os.environ["PYTEST_CURRENT_TEST"].rsplit(" ", 1)[0]
    iso.record_finding(node, "write", "open", "/synthetic", "a synthetic isolation finding")
'''


@pytest.mark.parametrize("jobs", [None, "2"])
def test_a_passing_nodes_temp_is_reclaimed_and_a_failing_nodes_is_kept(tmp_path, jobs):
    """Task 3.3b: a passing test's own temp is removed when it finishes.

    Every passing test used to keep its tree until the suite generation was
    removed, and the public projection's pytest leg peaked at about 24 GB of
    retained temp on omri-162; the hosted Linux runner ran out of disk on it.
    A test that fails in setup, call or teardown keeps its tree as evidence,
    so does an xfail or xpass test and a test whose only problem is an
    isolation finding, and a shared fixture's tree is left to the fixture's
    own owner. A skipped test's tree is reclaimed (review D5, D6)."""
    proc, reports = _run(tmp_path, RECLAIM, *(() if jobs is None else ("-n", jobs)))
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 1, combined
    assert _report(reports, "test_fail_call", "call")["outcome"] == "failed", combined
    assert _report(reports, "test_fail_setup", "setup")["outcome"] == "failed", combined
    assert _report(reports, "test_fail_teardown", "teardown")["outcome"] == "failed", combined
    assert _report(reports, "test_pass_with_only_an_isolation_problem", "call")["outcome"] == \
        "passed", combined
    assert _report(reports, "test_pass_with_only_an_isolation_problem", "teardown")["outcome"] == \
        "failed", combined
    for name in ("test_pass_tmp_path", "test_pass_factory", "test_pass_uses_shared"):
        assert _report(reports, name, "teardown")["outcome"] == "passed", (name, combined)
    left = sorted(p.name[len("marker-"):] for p in (tmp_path / "bt").rglob("marker-*"))
    assert left == ["fail_call", "fail_setup", "fail_teardown", "isolation_problem",
                    "shared_fixture", "xfail", "xpass"], (left, combined)
    # Every test also gets a tmp_path from the conftest's autouse migration-log
    # pin, so the reclaimed trees are test_pass_tmp_path's one, test_pass_factory's
    # two (tmp_path and the factory allocation), test_pass_skipped's one and
    # test_pass_uses_shared's one: five, with nothing left behind.
    summary = [line for line in combined.splitlines() if "temp hygiene summary:" in line]
    assert len(summary) == 1, combined
    assert " reclaimed=5 reclaim_left=0 reclaim_s_max=" in summary[0], summary


REUSE = '''
import os, pytest

def _note(tmp_path):
    assert not os.listdir(str(tmp_path)), "a fresh tmp_path already holds %r" % os.listdir(str(tmp_path))
    (tmp_path / "mine").write_text("x")
    with open(os.environ["HYG_PATHS_OUT"], "a") as fh:
        fh.write(str(tmp_path) + "\\n")

def test_a_long_name_that_shares_thirty_chars_first(tmp_path):
    _note(tmp_path)

def test_a_long_name_that_shares_thirty_chars_second(tmp_path):
    _note(tmp_path)

@pytest.mark.parametrize("n", [0, 1])
def test_a_parametrized_pair_shares_one_prefix(tmp_path, n):
    _note(tmp_path)
'''


@pytest.mark.parametrize("jobs", [None, "1"])
def test_a_reclaimed_name_is_never_handed_to_a_later_test(tmp_path, jobs):
    """Review D1: pytest numbers a new tmp_path one past the highest existing
    number under the first 30 characters of the test's name. Freeing a passing
    test's name let the next test with that prefix get the same directory, and
    a process the earlier test left behind could write into the later test's
    tree. The reclaimed name must stay taken, and its directory must be
    empty."""
    out = tmp_path / "paths.txt"
    proc, reports = _run(tmp_path, REUSE, *(() if jobs is None else ("-n", jobs)),
                         env_extra={"HYG_PATHS_OUT": str(out)})
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    paths = out.read_text().splitlines()
    assert len(paths) == 4, (paths, combined)
    assert len(set(paths)) == 4, "a reclaimed tmp_path was reused: %r" % paths
    for path in paths:
        assert os.path.isdir(path) and os.listdir(path) == [], (path, combined)
    summary = [line for line in combined.splitlines() if "temp hygiene summary:" in line]
    assert len(summary) == 1 and " reclaimed=4 reclaim_left=0 " in summary[0], (summary, combined)


LINK_INTO_PROTECTED = '''
import os, pwd

def test_a_passing_test_with_a_link_to_a_protected_root(tmp_path):
    os.symlink(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".claude"), str(tmp_path / "link"))
'''


def test_a_link_into_a_protected_root_is_reclaimed_without_a_violation(tmp_path):
    """Review D2: removing a directory entry writes the directory that holds
    it, never the target of a link. Reclaiming a passing test's tree that holds
    a link into the real ~/.claude must not be charged as a production write."""
    import pwd

    target = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".claude")
    target_existed = os.path.exists(target)
    proc, reports = _run(tmp_path, LINK_INTO_PROTECTED)
    combined = proc.stdout + proc.stderr
    assert "write violation" not in combined, combined
    assert proc.returncode == 0, combined
    # pytest names a tmp_path after the first 30 characters of the test's
    # name, so the directory is `test_a_passing_test_with_a_lin0`; the
    # untruncated name never exists, and a check against it asserts nothing
    # (review I3c F3). The reclaim leaves the directory as a tombstone.
    trees = glob.glob(str(tmp_path / "bt" / "test_a_passing_test_with_a_lin*0"))
    assert len(trees) == 1, (trees, combined)
    assert os.path.isdir(trees[0]), (trees, combined)
    assert not os.path.lexists(os.path.join(trees[0], "link")), (os.listdir(trees[0]), combined)
    # Removing the link removed the link, never what it pointed at.
    assert os.path.exists(target) == target_existed, combined


SESSION_FIXTURE = '''
import os, pytest

@pytest.fixture(scope="session")
def session_shared(tmp_path_factory):
    root = tmp_path_factory.mktemp("session-shared")
    yield root
    (root / "a" / "b").mkdir(parents=True)
    os.chmod(root / "a" / "b", 0o555)
    os.chmod(root / "a", 0o555)

def test_session_first(session_shared):
    pass

def test_session_second(session_shared):
    pass
'''


def test_a_session_fixture_leak_names_the_fixture_under_a_path_conftest(tmp_path):
    """The repository's own layout: tests/conftest.py is a PATH conftest below
    the rootdir's pytest.ini, not a `-p` plugin. pytest calls a session-scoped
    fixture's setup and finalizer hooks through the Session's hook proxy, which
    leaves such a conftest out, so hooks defined in the conftest itself would
    blame the tree on the first test and report it only from the backstop. The
    guard must own it by the fixture and name the fixture (spec 5.2)."""
    child = tmp_path / "child"
    (child / "tests").mkdir(parents=True)
    (child / "pytest.ini").write_text("[pytest]\n")
    os.symlink(str(REPO / "tests" / "conftest.py"), str(child / "tests" / "conftest.py"))
    (child / "tests" / "test_g1_session.py").write_text(SESSION_FIXTURE)
    tap = tmp_path / "tap"
    tap.mkdir()
    (tap / "hyg_tap.py").write_text(TAP)
    out = tap / "reports.jsonl"
    env = {"HOME": str(tmp_path / "fake-home"), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin",
           "PYTHONPATH": os.pathsep.join([str(REPO / "tests"), str(REPO / "bin"), str(tap)]),
           "HYG_TAP_OUT": str(out)}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "hyg_tap", "-q", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "bt"), "tests"],
        env=env, capture_output=True, text=True, timeout=110, cwd=str(child))
    reports = [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []
    combined = proc.stdout + proc.stderr
    for name in ("test_session_first", "test_session_second"):
        assert _report(reports, name, "teardown")["outcome"] == "passed", (name, combined)
    assert proc.returncode == 1, "a session fixture's leak did not fail the session:\n" + combined
    assert "left directory missing owner rwx (mode 0o555) by fixture session_shared (session) " \
        "first used by tests/test_g1_session.py::test_session_first" in combined, combined
    assert _undeletable(tmp_path / "bt") == [], "the guard reported a tree it did not release"


def test_a_finding_whose_entry_changes_before_the_release_is_still_recorded(tmp_path, monkeypatch):
    """F-10: the inspection found a violation, then the entry changed so the
    release found nothing. The owner must still be failed with what the
    inspection saw; once something was found, nothing is never recorded."""
    import _lib_test_isolation as iso
    import _lib_test_temp as T
    import _temp_hygiene as hyg

    seen = T.Finding(str(tmp_path / "d"), "mode", "directory missing owner rwx (mode 0o555)")
    monkeypatch.setattr(T, "inspect", lambda path, ops=None: T.Result(findings=[seen]))
    monkeypatch.setattr(T, "release", lambda path, ops=None: T.Result())
    recorded = []
    monkeypatch.setattr(iso, "record_finding",
                        lambda *args, **kw: recorded.append(args))
    hyg.check_and_release(str(tmp_path), "tests/test_x.py::test_owner")
    assert recorded == [("tests/test_x.py::test_owner", "temp-hygiene", "teardown",
                         seen.path, seen.detail)], recorded


def _recording(monkeypatch):
    import _lib_test_isolation as iso
    recorded = []
    monkeypatch.setattr(iso, "record_finding", lambda *args, **kw: recorded.append(args))
    return recorded


def test_the_inspection_is_recorded_before_the_release_and_new_repair_findings_append(
        tmp_path, monkeypatch):
    """963-CD-K3 (J2): the original violation is recorded BEFORE any release,
    and what the release finds besides is appended, never substituted; an
    identical (path, detail) pair is recorded once."""
    import _lib_test_temp as T
    import _temp_hygiene as hyg

    seen = T.Finding(str(tmp_path / "d"), "mode", "directory missing owner rwx (mode 0o555)")
    repair = T.Finding(str(tmp_path / "d" / "e"), "identity",
                       "repair refused: entry changed during traversal")
    recorded = _recording(monkeypatch)
    at_release = []

    def release(path, ops=None):
        at_release.append(list(recorded))
        return T.Result(findings=[seen, repair])

    monkeypatch.setattr(T, "inspect", lambda path, ops=None: T.Result(findings=[seen]))
    monkeypatch.setattr(T, "release", release)
    hyg.check_and_release(str(tmp_path), "tests/test_x.py::test_owner")
    first = ("tests/test_x.py::test_owner", "temp-hygiene", "teardown", seen.path, seen.detail)
    assert at_release == [[first]], "the release ran before the violation was recorded"
    assert recorded == [first, ("tests/test_x.py::test_owner", "temp-hygiene", "teardown",
                                repair.path, repair.detail)], recorded


def test_an_unreadable_basetemp_is_an_inspection_error_of_the_session(tmp_path, monkeypatch):
    """963-CD-K2 (spec 5.2, fail closed): the backstop never reads an
    unlistable basetemp as empty; it records an inspection error against the
    session. Run in-process on PATCHED registries: the live session's own
    registrations must not be consumed here."""
    import types
    import _temp_hygiene as hyg

    basetemp = tmp_path / "bt"
    basetemp.mkdir()
    real_listdir = os.listdir

    def listdir(path):
        if os.fspath(path) == str(basetemp):
            raise PermissionError(13, "Permission denied", str(basetemp))
        return real_listdir(path)

    monkeypatch.setattr(hyg, "_OWNED", {})
    monkeypatch.setattr(hyg, "_SEEN", set())
    monkeypatch.setattr(hyg, "STATS", dict(hyg.STATS))
    monkeypatch.setattr(hyg.os, "listdir", listdir)
    recorded = _recording(monkeypatch)
    plugins = types.SimpleNamespace(has_plugin=lambda name: False)
    session = types.SimpleNamespace(config=types.SimpleNamespace(
        _tmp_path_factory=types.SimpleNamespace(_basetemp=basetemp), pluginmanager=plugins))
    hyg.session_backstop(session)
    assert recorded == [(hyg.SESSION_BACKSTOP, "temp-hygiene", "teardown", str(basetemp),
                         "inspection error: Permission denied")], recorded


STRAY_WORKER_NAME = '''
import os

def test_leaves_a_worker_shaped_directory(tmp_path_factory):
    stray = tmp_path_factory.getbasetemp() / "popen-gw-not-a-worker"
    (stray / "a").mkdir(parents=True)
    os.chmod(stray / "a", 0o555)
'''


def test_a_serial_session_inspects_a_worker_shaped_directory(tmp_path):
    """963-CD-K1: only an xdist controller skips its workers' roots. A serial
    session has no workers, so an unregistered directory whose name merely
    resembles one is inspected like any other."""
    proc, _reports = _run(tmp_path, STRAY_WORKER_NAME)
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 1, "a serial session skipped a popen-gw* directory:\n" + combined
    assert "popen-gw-not-a-worker/a left directory missing owner rwx (mode 0o555) " \
        "by <session backstop>" in combined, combined
    assert _undeletable(tmp_path / "bt") == [], "the guard reported a tree it did not release"


def _patched_guard(monkeypatch, owned):
    import _temp_hygiene as hyg
    monkeypatch.setattr(hyg, "_OWNED", owned)
    monkeypatch.setattr(hyg, "_SEEN", {p for paths in owned.values() for p in paths})
    monkeypatch.setattr(hyg, "STATS", dict(hyg.STATS, reclaimed=0, reclaim_left=0))
    return hyg


def test_a_node_with_a_pending_handshake_keeps_its_tree(tmp_path, monkeypatch):
    """Review D3: a Python child whose handshake had not arrived when its test
    finished is resolved at session finish, and a missing one then fails the
    session naming that test. Its tree is that failure's evidence, so it is
    not reclaimed while the handshake is pending. Run in-process on PATCHED
    registries; the control node, with nothing pending, is reclaimed."""
    import types
    import _lib_test_isolation as iso

    pending_tree, control_tree = tmp_path / "pending", tmp_path / "control"
    for tree in (pending_tree, control_tree):
        (tree / "data").mkdir(parents=True)
    pending, control = "tests/test_x.py::test_launches_a_child", "tests/test_x.py::test_control"
    hyg = _patched_guard(monkeypatch, {("item", pending): [str(pending_tree)],
                                       ("item", control): [str(control_tree)]})
    monkeypatch.setattr(iso, "collect_test_violations", lambda nodeid, **kw: [])
    monkeypatch.setattr(iso, "_PENDING_HANDSHAKES", {4242: types.SimpleNamespace(node_id=pending)})
    for node in (pending, control):
        hyg.after_teardown(types.SimpleNamespace(nodeid=node, stash={}))
    assert (pending_tree / "data").is_dir(), "a tree with a pending handshake was reclaimed"
    assert os.listdir(str(control_tree)) == [], "the control tree was not reclaimed"
    assert (hyg.STATS["reclaimed"], hyg.STATS["reclaim_left"]) == (1, 0), hyg.STATS


def test_the_reclaim_records_what_the_removal_found_and_counts_only_what_stayed(
        tmp_path, monkeypatch):
    """Review D4: a violation made between the inspection and the removal (by
    a process the test left behind) must not be silently repaired; it is
    recorded against the test as a late hygiene finding. A refusal is not a
    hygiene finding of its own: what it leaves behind is counted in
    reclaim_left, and reclaim_left counts only what the removal left."""
    import _lib_test_temp as T

    hyg = _patched_guard(monkeypatch, {})
    recorded = _recording(monkeypatch)
    node = "tests/test_x.py::test_owner"
    late = T.Finding(str(tmp_path / "a" / "d"), "mode", "directory missing owner rwx (mode 0o555)")
    refused = T.Finding(str(tmp_path / "b" / "e"), "identity",
                        "repair refused: entry changed during traversal")
    results = {str(tmp_path / "a"): T.Result(findings=[late], removed=True),
               str(tmp_path / "b"): T.Result(findings=[refused], left=[refused.path])}
    monkeypatch.setattr(T, "remove", lambda path, ops=None: results[path])
    hyg.reclaim(str(tmp_path / "a"), node)
    hyg.reclaim(str(tmp_path / "b"), node)
    assert recorded == [(node, "temp-hygiene", "teardown", late.path, late.detail)], recorded
    assert (hyg.STATS["reclaimed"], hyg.STATS["reclaim_left"]) == (1, 1), hyg.STATS


@pytest.mark.parametrize("error", [FileExistsError, PermissionError],
                         ids=["exists", "permission"])
def test_a_tombstone_that_cannot_be_made_is_a_late_finding(tmp_path, monkeypatch, error):
    """Review I3c F12: only an existing entry at the reclaimed name keeps that
    name taken. Any other failure to make the tombstone leaves the name free
    for a later test (review D1), so it is recorded against the test as a late
    hygiene finding and counted as left, never passed over silently."""
    import _lib_test_temp as T

    hyg = _patched_guard(monkeypatch, {})
    recorded = _recording(monkeypatch)
    node = "tests/test_x.py::test_owner"
    tree = str(tmp_path / "tree")
    monkeypatch.setattr(T, "remove", lambda path, ops=None: T.Result(removed=True))
    real_mkdir = os.mkdir

    def mkdir(path, *args, **kwargs):
        if os.fspath(path) == tree:
            raise error(13 if error is PermissionError else 17, "refused", tree)
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(hyg.os, "mkdir", mkdir)
    hyg.reclaim(tree, node)
    monkeypatch.setattr(hyg.os, "mkdir", real_mkdir)
    if error is FileExistsError:
        assert recorded == [], recorded
        assert (hyg.STATS["reclaimed"], hyg.STATS["reclaim_left"]) == (1, 0), hyg.STATS
    else:
        assert [(r[0], r[1], r[2], r[3]) for r in recorded] == [
            (node, "temp-hygiene", "teardown", tree)], recorded
        assert "tombstone" in recorded[0][4], recorded
        assert (hyg.STATS["reclaimed"], hyg.STATS["reclaim_left"]) == (0, 1), hyg.STATS
