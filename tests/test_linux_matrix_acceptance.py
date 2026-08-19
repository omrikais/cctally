"""Threshold analyzer for the Gate 0.25 acceptance run (#621).

Every threshold is exercised in both directions. A threshold that can never
fail records nothing, and a threshold that always fails would make the run
unusable, so each one is tested with a passing input and a failing input.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import sys

import pytest


REPO = pathlib.Path(__file__).resolve().parents[1]
MODULE = REPO / "bin" / "cctally-test-linux-matrix-acceptance.py"


def _load():
    assert MODULE.is_file(), "bin/cctally-test-linux-matrix-acceptance.py is missing"
    loader = importlib.machinery.SourceFileLoader(
        "_cctally_linux_matrix_acceptance", str(MODULE)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def _lane(started, finished, samples, exit_code=0, oom=False):
    return {
        "startedAt": started,
        "finishedAt": finished,
        "exitCode": exit_code,
        "oomKilled": oom,
        "samples": samples,
    }


def _flat_samples(start, count, memory, tmp=0, step=0.25):
    return [
        {
            "t": start + index * step,
            "memoryBytes": memory,
            "tmpBytes": tmp,
            "oomEvents": 0,
        }
        for index in range(count)
    ]


def _healthy(analyzer):
    lanes = {}
    for offset, version in enumerate(("3.11", "3.12", "3.13")):
        start = 1000.0 + offset
        lanes[version] = _lane(
            start, start + 100.0, _flat_samples(start, 40, 1024, tmp=1024)
        )
    return {
        "lanes": lanes,
        "imageBuildSeconds": 12.0,
        "pytestDurations": [
            {"lane": "3.11", "nodeid": "tests/test_x.py::test_y", "seconds": 1.5}
        ],
    }


def _codes(result):
    return {violation["code"] for violation in result["violations"]}


def _concurrent(analyzer, samples):
    """Evaluate under the CONCURRENT oracle.

    Three-way concurrency was falsified and the gate now runs its lanes one at a
    time, but the concurrent thresholds are retained so a future re-attempt has
    an oracle. Every test states the schedule it covers, because the two
    threshold sets are not interchangeable.
    """
    return analyzer.evaluate_acceptance(samples, schedule="concurrent")


def _sequential(analyzer, samples):
    """Evaluate under the SEQUENTIAL oracle, which is what the gate ships."""
    return analyzer.evaluate_acceptance(samples, schedule="sequential")


def _sequential_samples():
    """Three lanes run one at a time, each taking 2,800 s.

    This is the shape the shipped driver produces: the starts are one lane
    duration apart and the phase is the sum of the three.
    """
    lanes = {}
    start = 1000.0
    for version in ("3.11", "3.12", "3.13"):
        finish = start + 2800.0
        lanes[version] = _lane(
            start, finish, _flat_samples(start, 40, 1024, tmp=1024)
        )
        start = finish + 1.0
    return {"lanes": lanes, "imageBuildSeconds": 12.0, "pytestDurations": []}


def _overlapping_peak_samples(memory_each, tmp_each=0):
    """Three lanes holding `memory_each` at the SAME instant."""
    lanes = {}
    for version in ("3.11", "3.12", "3.13"):
        samples = []
        for index in range(30):
            memory = memory_each if index == 5 else 1024 ** 3
            samples.append(
                {
                    "t": 1000.0 + index,
                    "memoryBytes": memory,
                    "tmpBytes": tmp_each,
                    "oomEvents": 0,
                }
            )
        lanes[version] = _lane(1000.0, 1100.0, samples)
    return {"lanes": lanes, "imageBuildSeconds": 0.0, "pytestDurations": []}


def test_a_healthy_run_passes_every_threshold():
    analyzer = _load()
    result = _concurrent(analyzer, _healthy(analyzer))
    assert result["passed"] is True, result["violations"]
    assert result["violations"] == []


def test_lane_start_spread_over_five_seconds_fails():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["lanes"]["3.13"]["startedAt"] = 1000.0 + 6.0
    result = _concurrent(analyzer, samples)
    assert result["passed"] is False
    assert "lane-start-spread" in _codes(result)


def test_lane_start_spread_at_the_threshold_passes():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["lanes"]["3.13"]["startedAt"] = 1000.0 + 5.0
    result = _concurrent(analyzer, samples)
    assert "lane-start-spread" not in _codes(result)


def test_a_nonzero_lane_exit_fails():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["lanes"]["3.12"]["exitCode"] = 1
    result = _concurrent(analyzer, samples)
    assert result["passed"] is False
    assert "lane-exit" in _codes(result)


def test_an_oom_killed_lane_fails():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["lanes"]["3.12"]["oomKilled"] = True
    result = _concurrent(analyzer, samples)
    assert "oom" in _codes(result)


def test_an_oom_event_in_the_samples_fails():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["lanes"]["3.12"]["samples"][10]["oomEvents"] = 1
    result = _concurrent(analyzer, samples)
    assert "oom" in _codes(result)


def test_lane_peaks_at_different_instants_do_not_sum():
    """Three lanes each peaking at 10 GiB at DIFFERENT instants total 30 GiB by
    naive per-lane summation, and must not fail the 22 GiB simultaneous
    threshold, because those peaks never coincided."""
    analyzer = _load()
    gib = 1024 ** 3
    lanes = {}
    for offset, version in enumerate(("3.11", "3.12", "3.13")):
        samples = []
        for index in range(30):
            moment = 1000.0 + index
            memory = 10 * gib if index == offset * 10 else 1 * gib
            samples.append(
                {"t": moment, "memoryBytes": memory, "tmpBytes": 0, "oomEvents": 0}
            )
        lanes[version] = _lane(1000.0, 1100.0, samples)
    result = _concurrent(analyzer,
        {"lanes": lanes, "imageBuildSeconds": 0.0, "pytestDurations": []}
    )
    assert "simultaneous-memory" not in _codes(result)
    assert result["measurements"]["peakSimultaneousMemoryBytes"] == 12 * gib


def test_lane_peaks_at_the_same_instant_do_sum():
    """Three lanes each at 8 GiB at the SAME instant total 24 GiB and must fail
    the 22 GiB simultaneous threshold."""
    analyzer = _load()
    gib = 1024 ** 3
    result = _concurrent(analyzer, _overlapping_peak_samples(8 * gib))
    assert result["passed"] is False
    assert "simultaneous-memory" in _codes(result)
    assert result["measurements"]["peakSimultaneousMemoryBytes"] == 24 * gib


def test_per_lane_tmp_over_512_mib_fails():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["lanes"]["3.11"]["samples"][3]["tmpBytes"] = 513 * 1024 ** 2
    result = _concurrent(analyzer, samples)
    assert result["passed"] is False
    assert "lane-tmp" in _codes(result)


def test_per_lane_tmp_at_512_mib_passes():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["lanes"]["3.11"]["samples"][3]["tmpBytes"] = 512 * 1024 ** 2
    result = _concurrent(analyzer, samples)
    assert "lane-tmp" not in _codes(result)


def test_simultaneous_tmp_over_one_gib_fails():
    analyzer = _load()
    mib = 1024 ** 2
    lanes = {}
    for version in ("3.11", "3.12", "3.13"):
        lanes[version] = _lane(
            1000.0, 1100.0, _flat_samples(1000.0, 8, 1024, tmp=400 * mib, step=1.0)
        )
    result = _concurrent(analyzer,
        {"lanes": lanes, "imageBuildSeconds": 0.0, "pytestDurations": []}
    )
    assert "simultaneous-tmp" in _codes(result)
    # Each lane is below the 512 MiB per-lane ceiling; only the simultaneous
    # ceiling catches this.
    assert "lane-tmp" not in _codes(result)


def test_a_lane_over_an_hour_fails():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["lanes"]["3.12"]["finishedAt"] = (
        samples["lanes"]["3.12"]["startedAt"] + 3601.0
    )
    result = _concurrent(analyzer, samples)
    assert "lane-duration" in _codes(result)


def test_a_lane_at_exactly_an_hour_passes():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["lanes"]["3.12"]["finishedAt"] = (
        samples["lanes"]["3.12"]["startedAt"] + 3600.0
    )
    result = _concurrent(analyzer, samples)
    assert "lane-duration" not in _codes(result)


def test_the_concurrent_phase_over_an_hour_fails():
    analyzer = _load()
    samples = _healthy(analyzer)
    # Each lane individually stays inside its own ceiling; only the
    # launch-to-last-exit phase exceeds it.
    for offset, version in enumerate(("3.11", "3.12", "3.13")):
        start = 1000.0 + offset
        samples["lanes"][version]["startedAt"] = start
        samples["lanes"][version]["finishedAt"] = start + 3500.0
    samples["lanes"]["3.13"]["finishedAt"] = 1000.0 + 3601.0
    result = _concurrent(analyzer, samples)
    assert "phase-duration" in _codes(result)
    assert "lane-duration" not in _codes(result)


def test_a_pytest_node_at_or_over_a_hundred_seconds_fails():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["pytestDurations"].append(
        {"lane": "3.13", "nodeid": "tests/test_slow.py::test_slow", "seconds": 100.0}
    )
    result = _concurrent(analyzer, samples)
    assert result["passed"] is False
    assert "pytest-node-duration" in _codes(result)


def test_a_pytest_node_just_under_a_hundred_seconds_passes():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["pytestDurations"].append(
        {"lane": "3.13", "nodeid": "tests/test_slow.py::test_slow", "seconds": 99.9}
    )
    result = _concurrent(analyzer, samples)
    assert "pytest-node-duration" not in _codes(result)


def test_an_empty_sample_stream_is_a_refusal_rather_than_a_pass():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["lanes"]["3.12"]["samples"] = []
    result = _concurrent(analyzer, samples)
    assert result["passed"] is False
    assert "no-samples" in _codes(result)


def test_a_lane_with_no_recorded_exit_is_a_refusal():
    analyzer = _load()
    samples = _healthy(analyzer)
    samples["lanes"]["3.12"]["exitCode"] = None
    result = _concurrent(analyzer, samples)
    assert result["passed"] is False
    assert "lane-exit" in _codes(result)


def test_no_lanes_at_all_is_a_refusal():
    analyzer = _load()
    result = _concurrent(analyzer,
        {"lanes": {}, "imageBuildSeconds": 0.0, "pytestDurations": []}
    )
    assert result["passed"] is False
    assert "no-samples" in _codes(result)


def test_cgroup_v2_memory_parsing():
    analyzer = _load()
    parsed = analyzer.parse_memory_sample(
        {
            "memory.current": "4096\n",
            "memory.peak": "8192\n",
            "memory.stat": "anon 100\nfile 200\nshmem 30\nslab 7\n",
            "memory.events": "low 0\nhigh 0\nmax 2\noom 1\noom_kill 3\n",
        }
    )
    assert parsed["currentBytes"] == 4096
    assert parsed["peakBytes"] == 8192
    assert parsed["anonBytes"] == 100
    assert parsed["fileBytes"] == 200
    assert parsed["shmemBytes"] == 30
    assert parsed["oomEvents"] == 4


def test_cgroup_v1_memory_parsing():
    analyzer = _load()
    parsed = analyzer.parse_memory_sample(
        {
            "memory.usage_in_bytes": "4096\n",
            "memory.max_usage_in_bytes": "8192\n",
            "memory.stat": "rss 100\ncache 200\nshmem 30\ntotal_rss 100\n",
            "memory.oom_control": "oom_kill_disable 0\nunder_oom 0\noom_kill 2\n",
        }
    )
    assert parsed["currentBytes"] == 4096
    assert parsed["peakBytes"] == 8192
    assert parsed["anonBytes"] == 100
    assert parsed["fileBytes"] == 200
    assert parsed["shmemBytes"] == 30
    assert parsed["oomEvents"] == 2


def test_a_truncated_cgroup_sample_degrades_to_none_rather_than_raising():
    analyzer = _load()
    parsed = analyzer.parse_memory_sample(
        {"memory.current": "", "memory.stat": None, "memory.events": None}
    )
    assert parsed["currentBytes"] is None
    assert parsed["oomEvents"] == 0


def test_pytest_durations_are_extracted_from_a_lane_log():
    analyzer = _load()
    text = (
        "some suite output\n"
        "=========================== slowest durations ===========================\n"
        "101.20s call     tests/test_slow.py::test_slow\n"
        "0.30s setup    tests/test_fast.py::test_fast\n"
        "0.01s teardown tests/test_fast.py::test_fast\n"
        "=========================== 2 passed ===========================\n"
    )
    durations = analyzer.parse_pytest_durations(text)
    assert {entry["nodeid"] for entry in durations} == {
        "tests/test_slow.py::test_slow",
        "tests/test_fast.py::test_fast",
    }
    slowest = max(durations, key=lambda entry: entry["seconds"])
    assert slowest["seconds"] == pytest.approx(101.20)
    assert slowest["phase"] == "call"


def test_pytest_duration_lines_outside_the_report_are_not_matched():
    analyzer = _load()
    text = "a log line mentioning 3.50s call somewhere in prose\n"
    assert analyzer.parse_pytest_durations(text) == []


def _with_container_fs(samples, available):
    for lane in samples["lanes"].values():
        for sample in lane["samples"]:
            sample["containerFsAvailableBytes"] = available
    return samples


def test_a_healthy_container_filesystem_passes():
    analyzer = _load()
    samples = _with_container_fs(_healthy(analyzer), 20 * 1024 ** 3)
    result = _concurrent(analyzer, samples)
    assert "container-filesystem" not in _codes(result)
    assert result["measurements"]["minContainerFsAvailableBytes"] == 20 * 1024 ** 3


def test_an_exhausted_container_filesystem_fails():
    """The first three-lane acceptance run exhausted this filesystem and pytest
    reported thousands of `could not create numbered dir` errors per lane, while
    /tmp peaked at 35 MB. A lane that cannot allocate scratch reports test errors
    that say nothing about the product, so this is a refusal."""
    analyzer = _load()
    samples = _with_container_fs(_healthy(analyzer), 20 * 1024 ** 3)
    samples["lanes"]["3.12"]["samples"][7]["containerFsAvailableBytes"] = 1024 ** 3
    result = _concurrent(analyzer, samples)
    assert result["passed"] is False
    assert "container-filesystem" in _codes(result)
    assert result["measurements"]["minContainerFsAvailableBytes"] == 1024 ** 3


def test_a_missing_container_filesystem_reading_does_not_fabricate_a_floor():
    analyzer = _load()
    result = _concurrent(analyzer, _healthy(analyzer))
    assert "container-filesystem" not in _codes(result)
    assert result["measurements"]["minContainerFsAvailableBytes"] is None


def test_the_schedule_has_no_default():
    """The driver must state the schedule it actually ran.

    A default would silently apply one oracle to the other schedule, which is
    exactly the defect this parameter fixes: the shipped gate runs its lanes one
    at a time, and the concurrent thresholds refuse a healthy sequential run.
    """
    analyzer = _load()
    with pytest.raises(TypeError):
        analyzer.evaluate_acceptance(_healthy(analyzer))


def test_an_unrecognized_schedule_is_refused():
    analyzer = _load()
    with pytest.raises(ValueError):
        analyzer.evaluate_acceptance(_healthy(analyzer), schedule="staggered")


def test_a_healthy_sequential_run_passes_every_threshold_that_applies():
    """Sequential lanes start one lane duration apart and their phase is the sum
    of the three. Under the concurrent thresholds this healthy run would report a
    5,602-second start spread and an 8,402-second phase and be refused."""
    analyzer = _load()
    result = _sequential(analyzer, _sequential_samples())
    assert result["passed"] is True, result["violations"]
    assert result["violations"] == []


def test_the_same_sequential_run_is_refused_by_the_concurrent_oracle():
    """The discriminating half of the test above: if the sequential path silently
    reused the concurrent thresholds, that test would fail with exactly these two
    violations."""
    analyzer = _load()
    result = _concurrent(analyzer, _sequential_samples())
    assert result["passed"] is False
    assert {"lane-start-spread", "phase-duration"} <= _codes(result)


def test_the_sequential_phase_ceiling_is_the_sum_of_the_per_lane_ceilings():
    """Three lanes at exactly the per-lane ceiling with dead time between them
    exceed the summed ceiling, so the sequential phase threshold is enforced
    rather than merely skipped."""
    analyzer = _load()
    lanes = {}
    start = 1000.0
    for version in ("3.11", "3.12", "3.13"):
        finish = start + analyzer.MAX_LANE_SECONDS
        lanes[version] = _lane(
            start, finish, _flat_samples(start, 40, 1024, tmp=1024)
        )
        start = finish + 500.0
    result = _sequential(
        analyzer,
        {"lanes": lanes, "imageBuildSeconds": 0.0, "pytestDurations": []},
    )
    assert "lane-duration" not in _codes(result)
    assert "phase-duration" in _codes(result)
    assert (
        result["measurements"]["phaseCeilingSeconds"]
        == 3 * analyzer.MAX_LANE_SECONDS
    )


def test_the_simultaneous_ceilings_are_not_evaluated_under_a_sequential_schedule():
    """Lanes that never ran together cannot have held memory or /tmp together,
    so the simultaneous ceilings ask a question a sequential run does not raise.
    The same input under the concurrent oracle breaches both."""
    analyzer = _load()
    gib = 1024 ** 3
    memory = _overlapping_peak_samples(8 * gib)
    assert "simultaneous-memory" in _codes(_concurrent(analyzer, memory))
    sequential = _sequential(analyzer, memory)
    assert "simultaneous-memory" not in _codes(sequential)
    # The figure is still reported, because it is evidence either way.
    assert sequential["measurements"]["peakSimultaneousMemoryBytes"] == 24 * gib

    tmp = _overlapping_peak_samples(gib, tmp_each=400 * 1024 ** 2)
    assert "simultaneous-tmp" in _codes(_concurrent(analyzer, tmp))
    assert "simultaneous-tmp" not in _codes(_sequential(analyzer, tmp))


def test_a_sequential_run_still_enforces_every_per_lane_threshold():
    """Only the three overlap-dependent thresholds are skipped. A sequential run
    that fails a lane, is OOM-killed, floods /tmp or runs a slow node is refused
    exactly as a concurrent one is."""
    analyzer = _load()
    samples = _sequential_samples()
    samples["lanes"]["3.11"]["oomKilled"] = True
    samples["lanes"]["3.12"]["exitCode"] = 1
    samples["lanes"]["3.13"]["samples"][3]["tmpBytes"] = 513 * 1024 ** 2
    samples["pytestDurations"] = [
        {"lane": "3.11", "nodeid": "tests/test_slow.py::test_slow", "seconds": 100.0}
    ]
    result = _sequential(analyzer, samples)
    assert result["passed"] is False
    assert {"oom", "lane-exit", "lane-tmp", "pytest-node-duration"} <= _codes(result)


def test_a_sequential_lane_over_its_own_ceiling_still_fails():
    analyzer = _load()
    samples = _sequential_samples()
    samples["lanes"]["3.12"]["finishedAt"] = (
        samples["lanes"]["3.12"]["startedAt"] + analyzer.MAX_LANE_SECONDS + 1.0
    )
    result = _sequential(analyzer, samples)
    assert "lane-duration" in _codes(result)


def test_the_verdict_records_which_schedule_it_evaluated():
    """The artifact is release evidence, so it must say which oracle produced
    the verdict rather than leaving a reader to infer it from the figures."""
    analyzer = _load()
    assert (
        _sequential(analyzer, _sequential_samples())["measurements"]["schedule"]
        == "sequential"
    )
    assert (
        _concurrent(analyzer, _healthy(analyzer))["measurements"]["schedule"]
        == "concurrent"
    )
