#!/usr/bin/env python3
"""Threshold analyzer for the Gate 0.25 acceptance run (#621).

Pure functions only. The gate collects raw per-lane samples, container exit and
OOM state, and lane logs; everything that decides whether three-way concurrency
is safe is decided here, so it can be tested without Docker.

Enforcing the thresholds here rather than leaving an operator to read three
logs is the point: a threshold nobody evaluates records nothing.
"""

from __future__ import annotations

import re


SCHEDULE_SEQUENTIAL = "sequential"
SCHEDULE_CONCURRENT = "concurrent"
SCHEDULES = (SCHEDULE_SEQUENTIAL, SCHEDULE_CONCURRENT)

# Concurrency-only. Three lanes launched together must start together, and the
# whole phase is one lane's work; three lanes run one at a time start one lane
# duration apart and take the sum of the three, so neither ceiling means
# anything under a sequential schedule.
MAX_LANE_START_SPREAD_SECONDS = 5.0
MAX_SIMULTANEOUS_MEMORY_BYTES = 22 * 1024 ** 3
MAX_LANE_TMP_BYTES = 512 * 1024 ** 2
MAX_SIMULTANEOUS_TMP_BYTES = 1024 ** 3
# The floor is on the CONTAINER filesystem, not on /tmp. The first
# three-lane acceptance run exhausted it and pytest reported 2,354 to 3,111
# `could not create numbered dir` errors per lane, while /tmp peaked at
# 35 MB. One lane was then measured at 5.26 GB of container-filesystem
# growth, so three lanes need about 16 GB and 14.09 GB were free.
MIN_CONTAINER_FS_AVAILABLE_BYTES = 2 * 1024 ** 3
MAX_LANE_SECONDS = 3600.0
MAX_PHASE_SECONDS = 3600.0
# 20 seconds of margin below the 120-second pytest timeout in
# bin/cctally-test-all, so a node that is merely slow under three-way
# concurrency is reported before it becomes a timeout.
MAX_PYTEST_NODE_SECONDS = 100.0
# Wider than the sampler's 250 ms period, so a lane that is alive during a bin
# always contributes a sample to it.
ALIGNMENT_BIN_SECONDS = 1.0

_DURATION_LINE = re.compile(
    r"^(?P<seconds>\d+(?:\.\d+)?)s\s+(?P<phase>call|setup|teardown)\s+(?P<nodeid>\S+)\s*$"
)
_DURATION_HEADER = re.compile(r"slowest\s+.*durations", re.IGNORECASE)


def _read_int(text: str | None) -> int | None:
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


def _read_pairs(text: str | None) -> dict[str, int]:
    pairs: dict[str, int] = {}
    if not text:
        return pairs
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pairs[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return pairs


def parse_memory_sample(cgroup: dict[str, str | None]) -> dict:
    """Parse one raw cgroup sample under either cgroup version.

    The sampler copies the files out verbatim rather than interpreting them, so
    both versions are parsed here where the parsing can be tested. A file that
    is absent or truncated degrades to ``None`` rather than raising: a sampler
    that lost one read must not destroy the whole measurement.
    """
    stat = _read_pairs(cgroup.get("memory.stat"))
    events = _read_pairs(cgroup.get("memory.events"))
    oom_control = _read_pairs(cgroup.get("memory.oom_control"))

    current = _read_int(cgroup.get("memory.current"))
    if current is None:
        current = _read_int(cgroup.get("memory.usage_in_bytes"))
    peak = _read_int(cgroup.get("memory.peak"))
    if peak is None:
        peak = _read_int(cgroup.get("memory.max_usage_in_bytes"))

    if "anon" in stat:
        anon = stat.get("anon")
        cached = stat.get("file")
    else:
        anon = stat.get("rss")
        cached = stat.get("cache")
    shmem = stat.get("shmem")

    if events:
        oom_events = events.get("oom", 0) + events.get("oom_kill", 0)
    elif oom_control:
        oom_events = oom_control.get("oom_kill", 0)
    else:
        oom_events = 0

    return {
        "currentBytes": current,
        "peakBytes": peak,
        "anonBytes": anon,
        "fileBytes": cached,
        "shmemBytes": shmem,
        "oomEvents": oom_events,
    }


def parse_pytest_durations(text: str) -> list[dict]:
    """Extract node durations from a lane log's ``--durations=0`` report.

    Only lines inside a slowest-durations report are matched. A duration-shaped
    fragment in ordinary suite prose is not a reported node duration, and
    treating it as one would let unrelated output fail or pass the threshold.
    """
    durations: list[dict] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if not _DURATION_HEADER.search(lines[index]):
            index += 1
            continue
        index += 1
        while index < len(lines):
            line = lines[index]
            if line.startswith("="):
                break
            match = _DURATION_LINE.match(line.strip())
            if match:
                durations.append(
                    {
                        "nodeid": match.group("nodeid"),
                        "phase": match.group("phase"),
                        "seconds": float(match.group("seconds")),
                    }
                )
            index += 1
    return durations


def align_peak_total(
    lanes: dict, key: str, bin_seconds: float = ALIGNMENT_BIN_SECONDS
) -> float:
    """Peak of the summed per-lane value over a common timebase.

    Summing per-lane peaks would report a total the machine never held, because
    three peaks at three different instants never coincided. Samples are binned
    on the shared wall clock instead, each lane contributes its maximum within a
    bin, and a lane with no sample in a bin contributes nothing because it was
    not running.
    """
    binned: dict[int, dict[str, float]] = {}
    for version, lane in lanes.items():
        for sample in lane.get("samples") or []:
            moment = sample.get("t")
            value = sample.get(key)
            if moment is None or value is None:
                continue
            slot = int(float(moment) // bin_seconds)
            bucket = binned.setdefault(slot, {})
            bucket[version] = max(bucket.get(version, 0.0), float(value))
    if not binned:
        return 0.0
    return max(sum(bucket.values()) for bucket in binned.values())


def evaluate_acceptance(samples: dict, *, schedule: str) -> dict:
    """Decide whether a run held every acceptance threshold that applies to it.

    ``schedule`` has no default because the two threshold sets are not
    interchangeable. Three concurrent lanes must start within seconds of each
    other and the whole phase is one lane's work, so the spread and phase
    ceilings are meaningful; three sequential lanes start one lane duration
    apart and take the sum of the three, and applying the concurrent ceilings to
    them refuses a healthy run. The simultaneous memory and ``/tmp`` ceilings ask
    what the machine held at one instant, which is a question only overlapping
    lanes raise. Everything else — lane exit, OOM, per-lane duration, per-lane
    ``/tmp``, the container filesystem floor and pytest node duration — applies
    under both.

    A skipped threshold is still MEASURED and reported, because the figure is
    release evidence either way; only its enforcement is conditional.
    """
    if schedule not in SCHEDULES:
        raise ValueError(f"schedule must be one of {SCHEDULES}, not {schedule!r}")
    concurrent = schedule == SCHEDULE_CONCURRENT
    violations: list[dict] = []
    lanes = samples.get("lanes") or {}

    def _violate(code: str, detail: str) -> None:
        violations.append({"code": code, "detail": detail})

    if not lanes:
        _violate("no-samples", "the acceptance run recorded no lanes at all")

    starts = []
    finishes = []
    for version in sorted(lanes):
        lane = lanes[version]
        lane_samples = lane.get("samples") or []
        if not lane_samples:
            _violate(
                "no-samples",
                f"Python {version} recorded no samples, so its resource use is "
                "unknown rather than acceptable",
            )
        started = lane.get("startedAt")
        finished = lane.get("finishedAt")
        if started is None or finished is None:
            _violate(
                "no-samples",
                f"Python {version} recorded no start or finish instant",
            )
        else:
            starts.append(float(started))
            finishes.append(float(finished))
            elapsed = float(finished) - float(started)
            if elapsed > MAX_LANE_SECONDS:
                _violate(
                    "lane-duration",
                    f"Python {version} ran {elapsed:.0f}s, over the "
                    f"{MAX_LANE_SECONDS:.0f}s ceiling",
                )

        exit_code = lane.get("exitCode")
        if exit_code != 0:
            _violate(
                "lane-exit",
                f"Python {version} exited {exit_code!r} rather than 0",
            )
        if lane.get("oomKilled"):
            _violate("oom", f"Python {version} was OOM-killed")
        oom_events = max(
            (sample.get("oomEvents") or 0 for sample in lane_samples), default=0
        )
        if oom_events:
            _violate(
                "oom",
                f"Python {version} recorded {oom_events} cgroup OOM events",
            )

        lane_tmp = max(
            (sample.get("tmpBytes") or 0 for sample in lane_samples), default=0
        )
        if lane_tmp > MAX_LANE_TMP_BYTES:
            _violate(
                "lane-tmp",
                f"Python {version} peaked at {lane_tmp} bytes in /tmp, over the "
                f"{MAX_LANE_TMP_BYTES} byte per-lane ceiling",
            )

    spread = (max(starts) - min(starts)) if starts else 0.0
    if concurrent and spread > MAX_LANE_START_SPREAD_SECONDS:
        _violate(
            "lane-start-spread",
            f"lanes started {spread:.1f}s apart, over the "
            f"{MAX_LANE_START_SPREAD_SECONDS:.0f}s ceiling",
        )

    phase = (max(finishes) - min(starts)) if starts and finishes else 0.0
    phase_ceiling = (
        MAX_PHASE_SECONDS
        if concurrent
        else MAX_LANE_SECONDS * max(len(starts), 1)
    )
    if phase > phase_ceiling:
        _violate(
            "phase-duration",
            f"the {schedule} lane phase took {phase:.0f}s, over the "
            f"{phase_ceiling:.0f}s ceiling",
        )

    peak_memory = align_peak_total(lanes, "memoryBytes")
    if concurrent and peak_memory > MAX_SIMULTANEOUS_MEMORY_BYTES:
        _violate(
            "simultaneous-memory",
            f"peak simultaneous memory was {peak_memory:.0f} bytes, over the "
            f"{MAX_SIMULTANEOUS_MEMORY_BYTES} byte ceiling",
        )

    container_fs_floor = None
    for lane in lanes.values():
        for sample in lane.get("samples") or []:
            available = sample.get("containerFsAvailableBytes")
            if available is None:
                continue
            if container_fs_floor is None or available < container_fs_floor:
                container_fs_floor = available
    if (
        container_fs_floor is not None
        and container_fs_floor < MIN_CONTAINER_FS_AVAILABLE_BYTES
    ):
        _violate(
            "container-filesystem",
            f"the container filesystem fell to {container_fs_floor} bytes free, "
            f"under the {MIN_CONTAINER_FS_AVAILABLE_BYTES} byte floor; a lane "
            "that cannot allocate scratch reports test errors that say nothing "
            "about the product",
        )

    peak_tmp = align_peak_total(lanes, "tmpBytes")
    if concurrent and peak_tmp > MAX_SIMULTANEOUS_TMP_BYTES:
        _violate(
            "simultaneous-tmp",
            f"peak simultaneous /tmp was {peak_tmp:.0f} bytes, over the "
            f"{MAX_SIMULTANEOUS_TMP_BYTES} byte ceiling",
        )

    durations = samples.get("pytestDurations") or []
    slowest = None
    for entry in durations:
        seconds = float(entry.get("seconds") or 0.0)
        if slowest is None or seconds > float(slowest.get("seconds") or 0.0):
            slowest = entry
        if seconds >= MAX_PYTEST_NODE_SECONDS:
            _violate(
                "pytest-node-duration",
                f"{entry.get('nodeid')} took {seconds:.1f}s on Python "
                f"{entry.get('lane')}, at or over the "
                f"{MAX_PYTEST_NODE_SECONDS:.0f}s ceiling",
            )

    return {
        "passed": not violations,
        "violations": violations,
        "measurements": {
            "schedule": schedule,
            "laneStartSpreadSeconds": spread,
            "phaseSeconds": phase,
            "phaseCeilingSeconds": phase_ceiling,
            "laneSeconds": {
                version: (
                    float(lane["finishedAt"]) - float(lane["startedAt"])
                    if lane.get("finishedAt") is not None
                    and lane.get("startedAt") is not None
                    else None
                )
                for version, lane in sorted(lanes.items())
            },
            "peakSimultaneousMemoryBytes": peak_memory,
            "peakSimultaneousTmpBytes": peak_tmp,
            "minContainerFsAvailableBytes": container_fs_floor,
            "peakLaneTmpBytes": {
                version: max(
                    (
                        sample.get("tmpBytes") or 0
                        for sample in (lane.get("samples") or [])
                    ),
                    default=0,
                )
                for version, lane in sorted(lanes.items())
            },
            "imageBuildSeconds": samples.get("imageBuildSeconds"),
            "slowestPytestNode": slowest,
            "pytestNodeCount": len(durations),
        },
    }
