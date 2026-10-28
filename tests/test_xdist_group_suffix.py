"""The `@<group>` node id suffix of `--dist loadgroup` (test regime reset,
session 3).

bin/cctally-test-all runs the pytest phase under `--dist loadgroup` so that
tests/test_world_enumeration_oracle_f17.py's heavy cases occupy at most four
workers (tests/conftest.py `_assign_xdist_groups`). Under that mode an xdist
worker appends `@<group>` to every grouped item's node id, and that id is what
the collection hook and every report carry. Every consumer that records a node
id must record the plain one: a suffixed id in the durations records names no
node a later run can schedule by.
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

import conftest  # noqa: E402
from tests import _pytest_durations_plugin  # noqa: E402

PLAIN = (
    ("tests/test_a.py::test_x@f17-0", "tests/test_a.py::test_x"),
    ("tests/test_a.py::test_x[p-q]@f17-3", "tests/test_a.py::test_x[p-q]"),
    ("tests/test_a.py::Cls::test_x@g", "tests/test_a.py::Cls::test_x"),
    # An `@` inside the parameter id is not a suffix (xdist's own rule: the
    # last `@` must follow the last `]`).
    ("tests/test_a.py::test_x[a@b]", "tests/test_a.py::test_x[a@b]"),
    ("tests/test_a.py::test_x[a@b]@g", "tests/test_a.py::test_x[a@b]"),
    # An `@` in a directory name is before the last `::`, never a suffix.
    ("tests/a@b/test_a.py::test_x", "tests/a@b/test_a.py::test_x"),
    ("tests/test_a.py::test_x", "tests/test_a.py::test_x"),
)


def test_the_durations_recorder_strips_only_a_group_suffix():
    for raw, plain in PLAIN:
        assert _pytest_durations_plugin.plain_nodeid(raw) == plain, raw


GROUPED = '''
import pytest

@pytest.mark.xdist_group("g1")
@pytest.mark.parametrize("n", [0, 1, 2])
def test_grouped(n):
    pass

@pytest.mark.xdist_group("g2")
def test_grouped_alone():
    pass

def test_plain():
    pass
'''

PLAIN_IDS = sorted(["test_grouped.py::test_grouped[0]", "test_grouped.py::test_grouped[1]",
                    "test_grouped.py::test_grouped[2]", "test_grouped.py::test_grouped_alone",
                    "test_grouped.py::test_plain"])


def _run_grouped(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    (child / "test_grouped.py").write_text(GROUPED)
    durations = tmp_path / "durations.jsonl.gz"
    env = {"HOME": str(tmp_path / "home"), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(REPO),
           "CCTALLY_DURATIONS_PATH": str(durations), "CCTALLY_DURATIONS_LEG": "pytest"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "2",
         "--dist", "loadgroup", "-p", "tests._pytest_durations_plugin", "--rootdir", str(child),
         "--basetemp", str(tmp_path / "bt"), str(child)],
        env=env, capture_output=True, text=True, timeout=110, cwd=str(child))
    return proc, durations


def test_grouped_nodes_are_recorded_by_their_plain_ids(tmp_path):
    """Under `--dist loadgroup` the durations records carry the plain node ids."""
    proc, durations = _run_grouped(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    with gzip.open(durations, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    ids = sorted({row["nodeId"] for row in rows if not row.get("footer")})
    assert ids == PLAIN_IDS, ids


def test_the_conftest_marks_a_declaring_module_before_xdist_suffixes_it(tmp_path):
    """tests/conftest.py's groups must exist when xdist's worker suffixes the
    node id, or `--dist loadgroup` groups nothing. A tap records the raw ids
    the controller receives: every item of the declaring module carries one of
    its groups, and the other module's items carry none."""
    child = tmp_path / "child"
    child.mkdir()
    (child / "test_declares.py").write_text(
        "import pytest\n"
        "XDIST_GROUP_COUNT = 2\nXDIST_GROUP_PREFIX = 'decl'\n"
        "def xdist_case_cost(item):\n    return 1.0\n"
        "@pytest.mark.parametrize('n', range(4))\n"
        "def test_member(n):\n    pass\n")
    (child / "test_other.py").write_text("def test_other():\n    pass\n")
    tap = tmp_path / "tap"
    tap.mkdir()
    out = tmp_path / "raw-ids.txt"
    # Written by the controller only: `-p` loads the tap in every worker too.
    (tap / "raw_tap.py").write_text(
        "import os\n"
        "_WRITER = [True]\n"
        "def pytest_configure(config):\n"
        "    _WRITER[0] = not hasattr(config, 'workerinput')\n"
        "def pytest_runtest_logreport(report):\n"
        "    if _WRITER[0] and report.when == 'call':\n"
        "        with open(os.environ['RAW_IDS'], 'a') as fh:\n"
        "            fh.write(report.nodeid + '\\n')\n")
    env = {"HOME": str(tmp_path / "home"), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin",
           "PYTHONPATH": os.pathsep.join([str(REPO / "tests"), str(REPO / "bin"), str(tap)]),
           "RAW_IDS": str(out)}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "conftest",
         "-p", "raw_tap", "-n", "2", "--dist", "loadgroup", "--rootdir", str(child),
         "--basetemp", str(tmp_path / "bt"), str(child)],
        env=env, capture_output=True, text=True, timeout=110, cwd=str(child))
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    ids = sorted(out.read_text().splitlines())
    members = [i for i in ids if "test_member" in i]
    assert len(members) == 4 and all(i.rsplit("@", 1)[-1] in ("decl-0", "decl-1")
                                     for i in members), ids
    assert {i.rsplit("@", 1)[-1] for i in members} == {"decl-0", "decl-1"}, ids
    assert "test_other.py::test_other" in ids, ids


def test_lpt_groups_balance_and_are_deterministic():
    costs = [50.0, 47.0, 36.0, 25.0, 25.0, 11.0, 11.0, 11.0, 2.0]
    keys = ["k%d" % i for i in range(len(costs))]
    groups = conftest.lpt_groups(costs, 4, keys)
    assert groups == conftest.lpt_groups(list(costs), 4, list(keys))
    loads = [sum(c for c, g in zip(costs, groups) if g == k) for k in range(4)]
    assert sorted(groups[:4]) == [0, 1, 2, 3], groups   # the four largest spread first
    assert max(loads) - min(loads) <= max(costs[4:]), loads


def test_the_pytest_phase_runs_under_loadgroup():
    """bin/cctally-test-all's bulk leg pairs `-n` with `--dist loadgroup`;
    without it the module's groups are ignored and its heavy cases cluster at
    the tail. The legs are built by bin/_lib_test_selection.py (test regime
    reset, Task 3.4), which the aggregator calls."""
    import _lib_test_selection as selection

    bulk, _serial = selection.pytest_legs(REPO, (), jobs=4)
    assert bulk[bulk.index("-n"):bulk.index("-n") + 4] == ["-n", "4", "--dist", "loadgroup"]
    text = (REPO / "bin" / "cctally-test-all").read_text(encoding="utf-8")
    assert "_lib_test_selection.py" in text and "legs_args=(legs " in text


def test_the_failed_node_parser_strips_a_group_suffix(tmp_path):
    """The failing node ids bin/cctally-test-all prints are ids a reader can
    pass back to pytest."""
    script = (REPO / "bin" / "cctally-test-all").read_text(encoding="utf-8")
    start = script.index("_parse_pytest_failed_nodes() {")
    body = script[start:script.index("\n}\n", start) + 3]
    log = tmp_path / "pytest.log"
    log.write_text(
        "FAILED tests/test_a.py::test_x[p]@f17-2 - AssertionError: boom\n"
        "ERROR tests/test_a.py::test_y@f17-0\n"
        "FAILED tests/test_a.py::test_z[a@b] - x\n"
        "FAILED tests/test_a.py::test_w\n")
    proc = subprocess.run(["/bin/bash", "-c", body + '\n_parse_pytest_failed_nodes "$1"', "bash",
                           str(log)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "FAILED tests/test_a.py::test_x[p] - AssertionError: boom",
        "ERROR tests/test_a.py::test_y",
        "FAILED tests/test_a.py::test_z[a@b] - x",
        "FAILED tests/test_a.py::test_w",
    ], proc.stdout


def test_a_run_without_xdist_knows_the_group_marker(tmp_path):
    """Review I3c F11: xdist registers `xdist_group` itself, so a `-p no:xdist`
    run met tests/conftest.py's groups as an unknown mark and warned on every
    grouped item. The conftest registers the marker, and such a run is
    silent."""
    child = tmp_path / "child"
    child.mkdir()
    (child / "test_declares.py").write_text(
        "import pytest\n"
        "XDIST_GROUP_COUNT = 2\nXDIST_GROUP_PREFIX = 'decl'\n"
        "def xdist_case_cost(item):\n    return 1.0\n"
        "@pytest.mark.parametrize('n', range(2))\n"
        "def test_member(n):\n    pass\n")
    env = {"HOME": str(tmp_path / "home"), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin",
           "PYTHONPATH": os.pathsep.join([str(REPO / "tests"), str(REPO / "bin")])}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         "-p", "conftest", "-W", "error::pytest.PytestUnknownMarkWarning",
         "--rootdir", str(child), "--basetemp", str(tmp_path / "bt"), str(child)],
        env=env, capture_output=True, text=True, timeout=110, cwd=str(child))
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "2 passed" in combined and "xdist_group" not in combined, combined
