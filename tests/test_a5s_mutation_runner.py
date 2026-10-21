"""The A5s mutation runner judges detection node by node (#940 spec 6.7, 7j).

Hermetic: each case builds a throwaway git repository holding a patch
directory and its README table, and stands in for the lane's test command
with a small script that prints a pytest-style short summary chosen by the
case for the unpatched and the patched tree. Nothing here runs the real
retention modules; the runner's real lanes do that.
"""

import io
import json
import pathlib
import subprocess
import sys

import _a5s_mutation_runner as runner

N1 = "tests/test_x.py::test_one[a]"
N2 = "tests/test_x.py::test_two"
N3 = "tests/test_y.py::test_three"
P1 = "01-first.patch"
P2 = "02-second.patch"

README = """# patches

| Patch | Node | Outcome | Failure text |
|---|---|---|---|
| `01-first.patch` | `%s` | `FAILED` | `the first assertion` |
| `01-first.patch` | `%s` | `FAILED` | `the second assertion` |
| `02-second.patch` | `%s` | `ERROR` | `setup is unavailable` |

Trailing prose.
""" % (N1, N2, N3)

FAKE = r'''
import json, pathlib, sys
scenario = json.loads(pathlib.Path(sys.argv[1]).read_text())
state = pathlib.Path("state.txt").read_text().strip()
case = scenario[state]
args = sys.argv[2:]
assert args[:2] == ["-rA", "-vv"], args
pathlib.Path(scenario["log"]).open("a").write(
    json.dumps({"state": state, "nodes": args[2:]}) + "\n")
print("============================= test session starts ==============================")
print("collected %d items" % len(args[2:]))
for name, text in case.get("write", {}).items():
    pathlib.Path(name).write_text(text)
if case.get("summary", True):
    print("=========================== short test summary info ============================")
    for line in case["lines"]:
        print(line)
print("========================= %s in 0.01s =========================" % case.get("tail", "1 failed"))
sys.exit(case["rc"])
'''


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                          text=True, check=True)


def _commit(root, *args):
    """A scratch-repository commit that never asks for a signature (#940 F1
    review #11)."""
    return _git(root, "-c", "commit.gpgsign=false", "commit", *args)


def _patch_for(root, state):
    """A patch that turns `state.txt` from `base` into `state`."""
    path = root / "state.txt"
    path.write_text(state + "\n")
    diff = subprocess.run(["git", "diff", "--", "state.txt"], cwd=str(root),
                          capture_output=True, text=True, check=True).stdout
    path.write_text("base\n")
    return diff


def _repo(tmp_path, readme=README):
    root = tmp_path / "repo"
    patch_dir = root / runner.PATCH_DIR
    patch_dir.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "state.txt").write_text("base\n")
    _git(root, "add", "state.txt")
    _commit(root, "-q", "-m", "base")
    (patch_dir / P1).write_text(_patch_for(root, "one"))
    (patch_dir / P2).write_text(_patch_for(root, "two"))
    (patch_dir / "README.md").write_text(readme)
    _git(root, "add", "-A")
    _commit(root, "-q", "-m", "patches")
    return root


def _passing(*nodes):
    return {"rc": 0, "tail": "%d passed" % len(nodes),
            "lines": ["PASSED %s" % n for n in nodes]}


GOOD = {
    "base": _passing(N1, N2, N3),
    "one": {"rc": 1, "lines": [
        "FAILED %s - AssertionError: the first assertion" % N1,
        "FAILED %s - AssertionError: ('the second assertion', {'x': 1})"
        % N2]},
    "two": {"rc": 1, "lines": [
        "ERROR %s - AccountingUnavailable: setup is unavailable: no" % N3]},
}


def _run(tmp_path, scenario, root=None, only=None):
    root = root or _repo(tmp_path)
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE)
    log = tmp_path / "calls.jsonl"
    scenario = dict(scenario, log=str(log))
    spec = tmp_path / "scenario.json"
    spec.write_text(json.dumps(scenario))
    out = io.StringIO()
    rc = runner.run_qualification(root, [sys.executable, str(fake),
                                         str(spec)], only=only, out=out)
    calls = ([json.loads(line) for line in log.read_text().splitlines()]
             if log.exists() else [])
    return rc, out.getvalue(), calls, root


def _clean(root):
    return subprocess.run(["git", "status", "--porcelain",
                           "--untracked-files=all"], cwd=str(root),
                          capture_output=True, text=True).stdout == ""


def test_every_patch_detected_qualifies_and_leaves_the_tree_clean(tmp_path):
    rc, out, calls, root = _run(tmp_path, GOOD)
    assert rc == 0, out
    assert "a5s-mutation-runner: QUALIFIED" in out
    assert "01-first.patch: DETECTED" in out
    assert "02-second.patch: DETECTED" in out
    assert [c["state"] for c in calls] == ["base", "one", "two"]
    assert calls[0]["nodes"] == [N1, N2, N3]
    assert calls[1]["nodes"] == [N1, N2]
    assert calls[2]["nodes"] == [N3]
    assert _clean(root)
    assert (root / "state.txt").read_text() == "base\n"


def test_a_partial_rejection_is_not_detection(tmp_path):
    scenario = dict(GOOD, one={"rc": 1, "lines": [
        "FAILED %s - AssertionError: the first assertion" % N1,
        "PASSED %s" % N2]})
    rc, out, _calls, root = _run(tmp_path, scenario)
    assert rc == 1, out
    assert "01-first.patch: NOT DETECTED" in out
    assert "%s: reported PASSED" % N2 in out
    assert "02-second.patch: DETECTED" in out
    assert "a5s-mutation-runner: NOT QUALIFIED" in out
    assert _clean(root)


def test_a_missing_node_is_not_detection(tmp_path):
    scenario = dict(GOOD, one={"rc": 1, "lines": [
        "FAILED %s - AssertionError: the first assertion" % N1]})
    rc, out, _calls, _root = _run(tmp_path, scenario)
    assert rc == 1, out
    assert "%s: not reported (missing or uncollected)" % N2 in out


def test_a_skipped_node_is_not_detection(tmp_path):
    scenario = dict(GOOD, one={"rc": 1, "lines": [
        "FAILED %s - AssertionError: the first assertion" % N1,
        "SKIPPED [1] tests/test_x.py:9: no runtime"]})
    rc, out, _calls, _root = _run(tmp_path, scenario)
    assert rc == 1, out
    assert "a target node was skipped" in out
    assert "%s: not reported" % N2 in out


def test_a_node_named_skipped_is_not_detection(tmp_path):
    scenario = dict(GOOD, two={"rc": 1, "lines": [
        "SKIPPED %s - no runtime" % N3]})
    rc, out, _calls, _root = _run(tmp_path, scenario)
    assert rc == 1, out
    assert "%s: reported SKIPPED" % N3 in out


def test_the_wrong_failure_text_is_not_detection(tmp_path):
    scenario = dict(GOOD, one={"rc": 1, "lines": [
        "FAILED %s - AssertionError: the first assertion" % N1,
        "FAILED %s - AssertionError: an unrelated assertion" % N2]})
    rc, out, _calls, _root = _run(tmp_path, scenario)
    assert rc == 1, out
    assert "%s: did not fail at its designated assertion" % N2 in out


def test_the_wrong_outcome_is_not_detection(tmp_path):
    scenario = dict(GOOD, two={"rc": 1, "lines": [
        "FAILED %s - AccountingUnavailable: setup is unavailable" % N3]})
    rc, out, _calls, _root = _run(tmp_path, scenario)
    assert rc == 1, out
    assert "%s: did not fail at its designated assertion" % N3 in out


def test_a_failure_text_on_a_continuation_line_is_found(tmp_path):
    scenario = dict(GOOD, one={"rc": 1, "lines": [
        "FAILED %s - AssertionError: (" % N1,
        "  'the first assertion', {})",
        "FAILED %s - AssertionError: the second assertion" % N2]})
    rc, out, _calls, _root = _run(tmp_path, scenario)
    assert rc == 0, out


def test_an_infrastructure_exit_is_not_detection(tmp_path):
    for code in (2, 3, 4, 75, 255):
        scenario = dict(GOOD, two=dict(GOOD["two"], rc=code))
        rc, out, _calls, root = _run(tmp_path / str(code), scenario)
        assert rc == 1, out
        assert "pytest exited %d, not 1" % code in out
        assert _clean(root)


def test_a_command_that_prints_no_summary_is_not_detection(tmp_path):
    scenario = dict(GOOD, one={"rc": 1, "summary": False, "lines": []})
    rc, out, _calls, _root = _run(tmp_path, scenario)
    assert rc == 1, out
    assert "%s: not reported" % N1 in out


def test_a_failing_baseline_runs_no_patch(tmp_path):
    scenario = dict(GOOD, base={"rc": 1, "lines": [
        "PASSED %s" % N1, "PASSED %s" % N2,
        "FAILED %s - AssertionError: broken" % N3]})
    rc, out, calls, root = _run(tmp_path, scenario)
    assert rc == 1, out
    assert "baseline FAILED; no patch was run" in out
    assert "%s: FAILED unpatched" % N3 in out
    assert [c["state"] for c in calls] == ["base"]
    assert _clean(root)


def test_a_baseline_with_a_missing_node_runs_no_patch(tmp_path):
    scenario = dict(GOOD, base=_passing(N1, N2))
    rc, out, calls, _root = _run(tmp_path, scenario)
    assert rc == 1, out
    assert "%s: not reported" % N3 in out
    assert len(calls) == 1


def test_a_tree_left_dirty_after_reversal_is_not_qualified(tmp_path):
    """#940 F1 review #10: a patched run that leaves a stray file behind
    fails qualification after the patch is reversed."""
    scenario = dict(GOOD, one=dict(GOOD["one"], write={"stray.txt": "x"}))
    rc, out, calls, _root = _run(tmp_path, scenario)
    assert rc == 1, out
    assert ("NOT QUALIFIED: the tree is not clean after reversing "
            "01-first.patch") in out
    assert "a5s-mutation-runner: QUALIFIED" not in out
    assert [c["state"] for c in calls] == ["base", "one"]


def test_a_patch_that_does_not_reverse_is_not_qualified(tmp_path):
    """#940 F1 review #10: a patched run that changes the patched file makes
    `git apply -R` fail, which fails qualification."""
    scenario = dict(GOOD, one=dict(GOOD["one"],
                                   write={"state.txt": "rewritten\n"}))
    rc, out, calls, _root = _run(tmp_path, scenario)
    assert rc == 1, out
    assert "NOT QUALIFIED: 01-first.patch did not reverse" in out
    assert "a5s-mutation-runner: QUALIFIED" not in out
    assert [c["state"] for c in calls] == ["base", "one"]


def test_a_dirty_tree_is_refused_before_the_baseline(tmp_path):
    root = _repo(tmp_path)
    (root / "stray.txt").write_text("x")
    rc, out, calls, _root = _run(tmp_path, GOOD, root=root)
    assert rc == 1, out
    assert "the tree is not clean before the baseline" in out
    assert calls == []


def test_a_patch_set_that_differs_from_the_readme_is_refused(tmp_path):
    root = _repo(tmp_path)
    (root / runner.PATCH_DIR / "03-extra.patch").write_text("")
    _git(root, "add", "-A")
    _commit(root, "-q", "-m", "extra")
    rc, out, calls, _root = _run(tmp_path, GOOD, root=root)
    assert rc == 1, out
    assert "are not the README's patches" in out
    assert calls == []


def test_a_patch_that_no_longer_applies_is_not_detection(tmp_path):
    root = _repo(tmp_path)
    (root / "state.txt").write_text("moved\n")
    _commit(root, "-q", "-am", "moved")
    scenario = dict(GOOD, base=GOOD["base"], moved=_passing(N1, N2, N3))
    rc, out, _calls, _root = _run(tmp_path, scenario, root=root)
    assert rc == 1, out
    assert "the patch does not apply" in out


def test_one_patch_can_be_selected(tmp_path):
    rc, out, calls, _root = _run(tmp_path, GOOD, only=["02"])
    assert rc == 0, out
    assert [c["state"] for c in calls] == ["base", "two"]
    assert calls[0]["nodes"] == [N3]


def test_the_readme_table_is_parsed_in_order():
    plan = runner.parse_readme(README)
    assert list(plan) == [P1, P2]
    assert plan[P1] == [runner.Target(N1, "FAILED", "the first assertion"),
                        runner.Target(N2, "FAILED", "the second assertion")]
    assert plan[P2] == [runner.Target(N3, "ERROR", "setup is unavailable")]


def test_a_malformed_readme_table_is_refused():
    bad = README.replace("`ERROR`", "`SKIPPED`")
    for text in (bad, "# no table\n", README.replace("`%s`" % N3, N3)):
        try:
            runner.parse_readme(text)
        except runner.QualificationError:
            continue
        raise AssertionError("accepted: %r" % text[:80])


def test_usage_errors_exit_2():
    assert runner.main([]) == 2
    assert runner.main(["--"]) == 2
    assert runner.main(["--bogus", "--", "true"]) == 2


def test_a_command_that_cannot_start_is_not_qualified(tmp_path):
    root = _repo(tmp_path)
    out = io.StringIO()
    rc = runner.run_qualification(
        root, [str(tmp_path / "no-such-command")], out=out)
    assert rc == 1, out.getvalue()
    assert "cannot start the command" in out.getvalue()
    assert "baseline FAILED" in out.getvalue()
    assert pathlib.Path(root / "state.txt").read_text() == "base\n"
