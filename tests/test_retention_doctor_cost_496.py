"""#496 S6 §7.5 — what the periodic doctor gather is allowed to cost.

Three separate claims, each with its own failure mode:

* the shallow gather performs NO walk at all, because `doctor_gather_state` is
  reached from the TUI and the dashboard snapshot precompute on every rebuild;
* the deep gather builds the reference graph exactly ONCE, not once for the
  plan and again for the summary;
* `plan_artifact_retention` is no longer quadratic in the root count, which is
  what let a corpus at the walk's own 5000-entry cap reach most of a second.

The scaling claim has two merge-path detectors here. The planner's reads of
the reference graph's inverse index are counted, which is deterministic:
re-deriving the deletion closure once per selected root (the pre-fix
`_Selection`) reads it quadratically often. The CPU instructions the planner
retires are counted too, in a single-threaded child process on macOS
(`test_the_planner_executes_linearly_in_the_root_count`, #964), over an age and
a count workload; that count also sees the regressions the inverse index does
not (re-freezing the closure per addition, rebuilding the surviving-root tally
per count-phase candidate), and a loaded runner does not move it. A shared
candidate list with a `list.remove` per selected root is not distinguished
from the current kernel at these sizes, by this count or by the wall-clock
ratio. It skips off macOS. The wall-clock ratio of
the same claim is the portable elapsed-time backstop, including on public CI's
Ubuntu lane; it runs serially, off the merge path, in
`tests/test_retention_planner_scaling.py`.
"""
from __future__ import annotations

import collections.abc
import dataclasses
import datetime as dt
import inspect
import json
import pathlib
import subprocess
import sys

import pytest

from conftest import load_script, redirect_paths
from test_retention_walk import build_production_corpus


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    import _cctally_doctor
    import _cctally_retention
    import _lib_artifact_retention

    return ns, _cctally_core, _cctally_doctor, _cctally_retention, _lib_artifact_retention


def _count_walk(monkeypatch, ret):
    ops = {"scandir": 0, "graph": 0}
    real_scandir = ret._walk_scandir
    real_build = ret._kernel.build_graph

    def counting_scandir(path):
        ops["scandir"] += 1
        return real_scandir(path)

    def counting_build(members):
        ops["graph"] += 1
        return real_build(members)

    monkeypatch.setattr(ret, "_walk_scandir", counting_scandir)
    monkeypatch.setattr(ret._kernel, "build_graph", counting_build)
    return ops


def test_the_shallow_gather_performs_no_retention_walk(tmp_path, monkeypatch):
    """`deep=False` is the dashboard and TUI path, called every rebuild."""
    _ns, core, doc, ret, _kern = _load(tmp_path, monkeypatch)
    build_production_corpus(core.APP_DIR)
    ops = _count_walk(monkeypatch, ret)

    state = doc._gather_retained_artifacts(
        dt.datetime(2026, 5, 13, tzinfo=dt.timezone.utc), deep=False,
    )

    assert ops["scandir"] == 0, "the shallow gather walked the data directory"
    assert ops["graph"] == 0
    assert state["policy_status"] == "not-scanned"
    # The cheap conditions an operator must act on survive the gate.
    assert "stuck_records" in state
    assert state["max_total_bytes"] is not None


def test_the_deep_gather_builds_the_reference_graph_exactly_once(
    tmp_path, monkeypatch,
):
    _ns, core, doc, ret, _kern = _load(tmp_path, monkeypatch)
    build_production_corpus(core.APP_DIR)
    ops = _count_walk(monkeypatch, ret)

    state = doc._gather_retained_artifacts(
        dt.datetime(2026, 5, 13, tzinfo=dt.timezone.utc), deep=True,
    )

    assert state["policy_status"] == "missing"      # no config → the defaults
    assert ops["scandir"] > 0, "the deep gather did not walk"
    assert ops["graph"] == 1, f"built the graph {ops['graph']} times"
    # Non-vacuity: the summary really was computed, not skipped by an except.
    assert state["protected_bytes"] > 0


def _synthetic_members(kern, count):
    """`count` incident roots, each naming one bundle. No filesystem."""
    members = []
    for i in range(count):
        incident = f"quarantine/stats.db-{i:06d}T000000Z"
        bundle = f"logs/stats.db-corruption-forensics-{i:06d}T000000Z.json"
        members.append(kern.RetentionMember(
            id=incident, kind="incident", family="stats.db",
            created_at_epoch=1000.0 + i, disk_bytes=1000, logical_bytes=1000,
            references=(bundle,), is_symlink=False, in_root=True, exists=True,
            valid=True, classification="exact", shape_token=f"s{i % 4}",
            finalized=True, active=False,
        ))
        members.append(kern.RetentionMember(
            id=bundle, kind="bundle", family="stats.db",
            created_at_epoch=1000.0 + i, disk_bytes=100, logical_bytes=100,
            references=(), is_symlink=False, in_root=True, exists=True,
            valid=True, classification="exact", shape_token=None,
            finalized=True, active=False,
        ))
    return members


#: The two scaling workloads (keyword arguments of `RetentionPolicy`). `age`
#: expires nearly every root in the age phase and so never reaches the count
#: phase; `count` has no age, byte or free-space bound, so every selection is
#: made by the count phase's surviving-root tally.
_WORKLOAD_POLICIES = {
    "age": dict(max_age_seconds=1, max_count_per_family=20,
                max_total_bytes=1, min_free_bytes=None,
                max_shape_examples=8),
    "count": dict(max_age_seconds=None, max_count_per_family=20,
                  max_total_bytes=None, min_free_bytes=None,
                  max_shape_examples=8),
}


def _workload_policy(kern, workload):
    return kern.RetentionPolicy(**_WORKLOAD_POLICIES[workload])


_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"

#: The instruction-count child (#964). It runs in its own interpreter so the
#: process-wide counter sees no other thread (the pytest worker's own threads
#: would add to it), builds the graph with this module's `_synthetic_members`
#: (its source is inlined below), and reads `ri_instructions` from
#: `proc_pid_rusage(RUSAGE_INFO_V4)` immediately before and after each
#: planning call. Its only stdout is one JSON line.
_CHILD_SOURCE = """
import ctypes, json, os, sys
args = json.loads(sys.argv[1])
sys.path.insert(0, args["bin"])
import _lib_artifact_retention as kern

# The named RUSAGE_INFO_V4 layout of bench/write-attribution/rusage.py.
_V2 = ("user", "sys", "idle_wk", "int_wk", "pageins", "wired", "resident",
       "footprint", "start", "exit", "c_user", "c_sys", "c_idle", "c_int",
       "c_pageins", "c_elapsed", "dread", "dwrite")
_V3 = ("qos_default", "qos_maintenance", "qos_background", "qos_utility",
       "qos_legacy", "qos_user_initiated", "qos_user_interactive",
       "billed_system", "serviced_system")
_V4 = ("logical_writes", "lifetime_max_footprint", "instructions", "cycles",
       "billed_energy", "serviced_energy", "interval_max_footprint",
       "runnable_time")


class RU(ctypes.Structure):
    _fields_ = ([("uuid", ctypes.c_uint8 * 16)]
                + [(n, ctypes.c_uint64) for n in _V2 + _V3 + _V4])


libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
libproc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int,
                                    ctypes.c_void_p]
libproc.proc_pid_rusage.restype = ctypes.c_int


def instructions():
    usage = RU()
    if libproc.proc_pid_rusage(os.getpid(), 4, ctypes.byref(usage)) != 0:
        print(json.dumps({"error": "proc_pid_rusage",
                          "errno": ctypes.get_errno()}))
        sys.exit(2)
    return usage.instructions


%(members)s

policy = kern.RetentionPolicy(**%(policies)r[args["workload"]])
counts, deleted = [], []
for size in args["sizes"]:
    state = kern.RetentionState(
        graph=kern.build_graph(_synthetic_members(kern, size)),
        now_epoch=10 ** 9, free_disk_bytes=10 ** 12)
    plan = kern.plan_artifact_retention(state, policy)       # warm-up
    best = None
    for _ in range(3):
        before = instructions()
        kern.plan_artifact_retention(state, policy)
        spent = instructions() - before
        best = spent if best is None else min(best, spent)
    counts.append(best)
    deleted.append(len(plan.delete_ids))
print(json.dumps({"instructions": counts, "deleted": deleted}))
""" % {"members": inspect.getsource(_synthetic_members),
       "policies": _WORKLOAD_POLICIES}


def _plan_instructions(workload: str) -> "tuple[list[int], list[int]]":
    """(instructions, deleted) at 400 and 3,200 roots, from the child. A
    child that fails or prints anything but its one JSON line fails the
    test; it never skips it."""
    request = json.dumps({"bin": str(_BIN), "workload": workload,
                          "sizes": [400, 3200]})
    child = subprocess.run([sys.executable, "-c", _CHILD_SOURCE, request],
                           capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, (
        "the instruction-count child failed", child.returncode,
        child.stdout, child.stderr)
    lines = child.stdout.splitlines()
    assert len(lines) == 1, ("the child did not print exactly one line",
                             child.stdout)
    answer = json.loads(lines[0])
    return answer["instructions"], answer["deleted"]


class _CountingIndex(collections.abc.Mapping):
    """`RetentionGraph.inbound_roots`, counting every entry it hands out:
    each lookup and each key an iteration yields."""

    def __init__(self, inner):
        self._inner = inner
        self.reads = 0

    def __getitem__(self, key):
        self.reads += 1
        return self._inner[key]

    def __iter__(self):
        for key in self._inner:
            self.reads += 1
            yield key

    def __len__(self):
        return len(self._inner)


def _inverse_index_reads(kern, count, policy):
    graph = kern.build_graph(_synthetic_members(kern, count))
    index = _CountingIndex(graph.inbound_roots)
    state = kern.RetentionState(
        graph=dataclasses.replace(graph, inbound_roots=index),
        now_epoch=10 ** 9, free_disk_bytes=10 ** 12,
    )
    plan = kern.plan_artifact_retention(state, policy)
    return index.reads, plan


def test_the_planner_reads_the_reference_graph_linearly_in_the_root_count(
    tmp_path, monkeypatch,
):
    """Selecting every root reads each graph entry a bounded number of times.

    Detects the #496 S6 regression in which `_Selection` re-derived the whole
    deletion closure for every root it added (`deletion_closure` over all
    roots selected so far): `plan_artifact_retention` became quadratic in the
    root count, and the periodic doctor gather at the walk's 5000-entry cap
    spent most of a second planning. Reads are counted, not timed, so a
    loaded runner cannot move the result. The pre-fix planner reads about 16x
    as often over a 4x root increase; the incremental one about 4x.
    """
    _ns, _core, _doc, _ret, kern = _load(tmp_path, monkeypatch)
    policy = kern.RetentionPolicy(
        max_age_seconds=1, max_count_per_family=20,
        max_total_bytes=1, min_free_bytes=None, max_shape_examples=8,
    )
    small, small_plan = _inverse_index_reads(kern, 100, policy)
    large, large_plan = _inverse_index_reads(kern, 400, policy)
    # Non-vacuity: the selection grows with the roots (each selected root
    # deletes its bundle with it), and the index is read at all.
    deleted = (len(small_plan.delete_ids), len(large_plan.delete_ids))
    assert deleted[1] > 3 * deleted[0] > 0, deleted
    assert small > 0
    assert large < 8 * small, (
        f"the planner read the inverse index {large} times for 400 roots "
        f"against {small} for 100: re-derived closures per selected root")


@pytest.mark.parametrize("workload", ["age", "count"])
def test_the_planner_executes_linearly_in_the_root_count(workload):
    """An 8x root increase executes well under 16x the CPU instructions.

    Detects a planner that has become superlinear in the root count, which on
    the periodic doctor gather at the walk's 5000-entry cap pegs the CPU
    (#496 S6, #964). Each shape below is caught by both workloads unless
    noted: re-deriving the closure after every addition (about 61x and 66x),
    rebuilding the surviving-root tally per count-phase candidate (`count`
    only, about 61x; the age workload never reaches the count phase), and
    re-freezing the closure after every addition (about 31x). The current
    planner executes about 8.2-8.6x, idle and under 2 x ncpu CPU burners
    alike, because the count is of instructions this process retires, not of
    elapsed time. It needs macOS's `proc_pid_rusage` instruction counter, so
    it skips elsewhere; the serial wall-clock backstop
    (`tests/test_retention_planner_scaling.py`) keeps the contract checked
    there.
    """
    if sys.platform != "darwin":
        pytest.skip("needs macOS proc_pid_rusage instruction counts")
    instructions, deleted = _plan_instructions(workload)
    if 0 in instructions:
        pytest.skip("the instruction counter reads zero (no performance "
                    "counters, e.g. a VM)")
    # Non-vacuity: the selection grows with the roots.
    assert deleted[1] > 3 * deleted[0] > 0, deleted
    small, large = instructions
    ratio = large / small
    assert ratio < 16, (
        f"the planner executed {large} instructions for 3200 roots against "
        f"{small} for 400 ({ratio:.2f}x over an 8x root increase, "
        f"{workload} workload): superlinear in the root count")


def test_the_incremental_selection_matches_the_pure_closure_at_every_step(
    tmp_path, monkeypatch,
):
    """The speedup must not move the decision.

    `deletion_closure` stays the authoritative statement of §3.1, and the
    incremental selection is asserted equal to it after EVERY addition — on a
    graph with a shared target, which is the case a wrong incremental update
    would get wrong.
    """
    _ns, _core, _doc, _ret, kern = _load(tmp_path, monkeypatch)
    shared = "logs/stats.db-corruption-forensics-20200101T000000Z.json"
    members = [
        kern.RetentionMember(
            id=f"quarantine/stats.db-2020010{i}T000000Z", kind="incident",
            family="stats.db", created_at_epoch=1000.0 + i, disk_bytes=10,
            logical_bytes=10, references=(shared,), is_symlink=False,
            in_root=True, exists=True, valid=True, classification="exact",
            shape_token=None, finalized=True, active=False,
        )
        for i in (1, 2, 3)
    ]
    members.append(kern.RetentionMember(
        id=shared, kind="bundle", family="stats.db", created_at_epoch=999.0,
        disk_bytes=7000, logical_bytes=7000, references=(), is_symlink=False,
        in_root=True, exists=True, valid=True, classification="exact",
        shape_token=None, finalized=True, active=False,
    ))
    graph = kern.build_graph(members)
    selection = kern._Selection(graph)
    for root in graph.roots:
        selection.add(root.id, "test")
        expected = kern.deletion_closure(graph, set(selection.root_ids))
        assert selection.closure == expected, selection.root_ids
        assert selection.bytes_reclaimed == sum(
            graph.members[m].disk_bytes for m in expected
        )
        assert selection.surviving_root_counts() == _reference_counts(
            graph, expected,
        )
    # Non-vacuity: the shared bundle really did enter only at the last add.
    assert shared in selection.closure
    assert len(graph.roots) == 3


def _reference_counts(graph, closure):
    counts = {}
    for root in graph.roots:
        if root.id in closure:
            continue
        counts[root.family] = counts.get(root.family, 0) + 1
    return counts
