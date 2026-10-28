"""tests/quarantine.json, the reviewed flaky-test list (spec 4.3).

`bin/cctally-test-all` deselects every listed pytest node from both pytest legs
(`bin/_lib_test_selection.py`); the quick gate excludes listed
nodes and harness cases; the nightly broad run's quarantine leg runs them.
Every reader parses the list through
`bin/_lib_quarantine.py`. This module checks that each entry is complete,
that each pytest id names a node pytest can collect and each harness case a
case its harness has, and that an unreadable list refuses a run at admission.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

import _temp_hygiene as _hyg

REPO = Path(__file__).resolve().parents[1]
QUARANTINE = REPO / "tests" / "quarantine.json"
FIELDS = ("id", "evidence", "invariant", "issue", "replacement", "reviewBy")


def parser():
    path = REPO / "bin" / "_lib_quarantine.py"
    spec = importlib.util.spec_from_file_location("_lib_quarantine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_quarantine_entries_are_complete():
    entries = json.loads(QUARANTINE.read_text(encoding="utf-8"))
    assert isinstance(entries, list), type(entries)
    ids = []
    for entry in entries:
        assert isinstance(entry, dict) and set(entry) == set(FIELDS), entry
        for field in FIELDS:
            assert isinstance(entry[field], str) and entry[field].strip(), (field, entry)
        assert re.fullmatch(r"#\d+", entry["issue"]), entry
        dt.date.fromisoformat(entry["reviewBy"])
        kind, _harness, _case = parser().parse_id(entry["id"])
        if kind == "pytest":
            ids.append(entry["id"])
    assert len(set(ids)) == len(ids), ids
    parser().load(REPO)  # the shared parser every reader uses accepts the list

    # A listed node whose file is mirror-private is absent from the public
    # mirror, where its deselection selects nothing; in this (private) tree,
    # recognised by .mirror-allowlist, every listed file must exist.
    present = [i for i in ids if (REPO / i.split("::")[0]).is_file()]
    if (REPO / ".mirror-allowlist").is_file():
        assert present == ids, sorted(set(ids) - set(present))
    if not present:
        return
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT",
                        "PYTEST_XDIST_TESTRUNUID", "PYTEST_CURRENT_TEST",
                        "PYTEST_ADDOPTS")}
    # Every listed FILE is collected, not only the listed ids: bin/cctally-test-all
    # deselects with pytest's prefix semantics, so a sibling whose name merely
    # extends a listed id (a later `..._under_dst`) would be deselected with it.
    files = sorted({i.split("::")[0] for i in present})
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", *files],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=100)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    collected = {line.strip() for line in proc.stdout.splitlines() if "::" in line}
    for node in present:
        matched = sorted(c for c in collected if c.startswith(node))
        assert matched, (node, sorted(collected)[:20])
        # Only the node itself, or its parametrizations, may share the prefix.
        assert all(c == node or c.startswith(node + "[") for c in matched), (node, matched)


def test_harness_case_ids_validate_against_the_harness_inventory(tmp_path):
    # A harness case that names no scenario of its harness excludes nothing,
    # so the flaky scenario would keep failing gates while the list claims it
    # is quarantined.
    q = parser()
    harness = tmp_path / "bin" / "cctally-demo-test"
    harness.parent.mkdir()
    harness.write_text("#!/usr/bin/env bash\n"
                       "[ \"$1\" = --list-cases ] && printf 'alpha\\nbeta\\n' && exit 0\n"
                       "exit 9\n")
    harness.chmod(0o755)

    def entries(*ids):
        return q.parse([{"id": i} for i in ids])

    q.validate_harness_cases(tmp_path, entries("harness:demo::alpha", "tests/test_x.py::test_y"))
    with pytest.raises(q.QuarantineError, match="gamma"):
        q.validate_harness_cases(tmp_path, entries("harness:demo::gamma"))
    with pytest.raises(q.QuarantineError, match="does not exist"):
        q.validate_harness_cases(tmp_path, entries("harness:missing::alpha"))
    with pytest.raises(q.QuarantineError):
        q.parse([{"id": "harness:demo:alpha"}])
    assert q.harness_cases(entries("harness:demo::alpha", "tests/test_x.py::test_y"),
                           "demo") == ["alpha"]
    assert q.pytest_ids(entries("harness:demo::alpha", "tests/test_x.py::test_y")) == [
        "tests/test_x.py::test_y"]
    # The committed list's harness cases name real scenarios.
    q.validate_harness_cases(REPO, q.load(REPO))


@pytest.mark.parametrize("payload", ['{"not": "a list"}', "not json", '[{"id": "no-node-separator"}]'],
                         ids=["object", "unparseable", "bad-id"])
def test_an_unreadable_quarantine_list_is_refused_at_admission(tmp_path, payload):
    # Reset fix round (B11): a malformed tests/quarantine.json refuses the run
    # at admission, before the harness pool starts, not after it has run.
    from tests.test_test_all_scheduler import _fake_pytest_tree

    runner, calls, env = _fake_pytest_tree(tmp_path)
    repo = runner.parent.parent
    (repo / "tests/quarantine.json").write_text(payload)
    marker = tmp_path / "harness-ran"
    harness = runner.parent / "cctally-reconcile-test"
    harness.write_text("#!/usr/bin/env bash\n: > \"%s\"\nprintf 'passed: 1   failed: 0\\n'\n" % marker)
    harness.chmod(0o755)
    outcome = tmp_path / "outcome.json"
    proc = subprocess.run([str(runner)], env=dict(env, CCTALLY_TEST_ALL_OUTCOME_FILE=str(outcome)),
                          text=True, capture_output=True, timeout=60)
    _hyg.reclaim_child_suite_dirs(tmp_path)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "tests/quarantine.json could not be applied" in proc.stdout + proc.stderr
    reasons = json.loads(outcome.read_text())["reasons"]
    assert [(r["code"], r["subject"]) for r in reasons] == [
        ("quarantine-list-unreadable", "tests/quarantine.json")], reasons
    assert not marker.exists(), "the harness pool ran before the quarantine list was refused"
    assert calls.read_text() == "" if calls.exists() else True


# ---------------------------------------------------------------- harness-case quarantine (Task 2.3)
DASHBOARD_HARNESS = REPO / "bin" / "cctally-dashboard-test"
SUMMARY = re.compile(r"^passed: (\d+)   failed: (\d+)$", re.M)


def _entry(case):
    return {"id": "harness:dashboard::" + case, "evidence": "e", "invariant": "i",
            "issue": "#985", "replacement": "r", "reviewBy": "2099-01-01"}


def _dashboard_scenarios():
    proc = subprocess.run([str(DASHBOARD_HARNESS), "--list-cases"], capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split()


def _run_dashboard(tmp_path, root, *args):
    env = dict(os.environ, TMPDIR=str(tmp_path), CCTALLY_TEST_JOBS="4")
    proc = subprocess.run([str(root / "bin" / "cctally-dashboard-test"), *args], env=env,
                          capture_output=True, text=True, timeout=110)
    _hyg.reclaim_child_suite_dirs(tmp_path)
    return proc


def test_dashboard_harness_excludes_quarantined_cases_from_the_summary(tmp_path):
    # A listed flaky scenario must neither fail the run nor be counted as a
    # pass: counting it would claim coverage the run did not have, and running
    # it would keep bouncing unrelated merges (#985).
    scenarios = _dashboard_scenarios()
    assert "no-data" in scenarios and len(scenarios) > 2
    quarantined = [s for s in scenarios if s != "no-data"]
    # The harness under test, beside a quarantine list of our own: every file
    # it runs is the repository's, through symlinks it resolves.
    root = tmp_path / "tree"
    (root / "bin").mkdir(parents=True)
    for item in (REPO / "bin").iterdir():
        (root / "bin" / item.name).symlink_to(item)
    (root / "tests").mkdir()
    (root / "tests" / "fixtures").symlink_to(REPO / "tests" / "fixtures")
    (root / "dashboard").symlink_to(REPO / "dashboard")
    (root / "tests" / "quarantine.json").write_text(json.dumps([_entry(s) for s in quarantined]))
    proc = _run_dashboard(tmp_path, root)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-4000:]
    assert ("excluded: " + " ".join(quarantined)) in proc.stdout.splitlines(), out[-4000:]
    summary = SUMMARY.findall(proc.stdout)
    assert len(summary) == 1, out[-4000:]
    passed, failed = map(int, summary[0])
    assert failed == 0
    # Exactly one scenario ran; everything else counted is a non-scenario check.
    ran = [s for s in scenarios if re.search(r"^(PASS|FAIL) %s(:|$)" % re.escape(s), proc.stdout, re.M)]
    assert ran == ["no-data"], ran
    others = len(re.findall(r"^PASS (?!no-data$)", proc.stdout, re.M))
    assert passed == 1 + others, (passed, others)


def test_dashboard_harness_cases_flag_runs_exactly_the_named_scenarios(tmp_path):
    # The CI quarantine leg reruns listed cases by name; running anything
    # else, or nothing, would report a verdict about the wrong scenarios.
    proc = _run_dashboard(tmp_path, REPO, "--cases", "no-data,ok")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-4000:]
    assert re.findall(r"^PASS (\S+)$", proc.stdout, re.M) == ["ok", "no-data"], out[-4000:]
    assert SUMMARY.findall(proc.stdout) == [("2", "0")], out[-4000:]
    assert "banner" not in proc.stdout
    assert "excluded:" not in proc.stdout


def test_unknown_case_name_is_refused(tmp_path):
    # A misspelt case would otherwise run nothing and report a clean pass.
    for args in (["--cases", "ok,no-such-scenario"], ["--cases", ""], ["--bogus"]):
        proc = _run_dashboard(tmp_path, REPO, *args)
        assert proc.returncode == 2, (args, proc.stdout[-2000:], proc.stderr[-2000:])
        assert not SUMMARY.findall(proc.stdout), args
    assert "no-such-scenario" in _run_dashboard(tmp_path, REPO, "--cases", "no-such-scenario").stderr
