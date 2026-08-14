#!/usr/bin/env python3
"""Reproduce #508 Task C's production-shaped memory campaign.

Run only through ``bin/cctally-test-remote`` and pin each fleet runner in turn.
The script emits machine-readable measurements; it does not choose the bound.
"""
from __future__ import annotations

import json
import os
import pathlib
import platform
import statistics
import tempfile

import journal_fixture_496_s4 as F


def _pair(work: pathlib.Path, name: str, *, mutant: bool) -> dict:
    base = F.run_worker(
        work, f"{name}-base", build=F.tier1_build(), trace=True,
        compact_projection_mutant=mutant)
    extra = int(base["shape"]["counts"]["obs_quota"])
    doubled = F.run_worker(
        work, f"{name}-doubled",
        build=F.tier1_build(extra_quota_lines=extra), trace=True,
        compact_projection_mutant=mutant)
    records = (doubled["traversal"]["quota_replay"]["lines"]
               - base["traversal"]["quota_replay"]["lines"])
    encoded = (doubled["traversal"]["quota_replay"]["bytes"]
               - base["traversal"]["quota_replay"]["bytes"])
    growth = doubled["traced_peak_bytes"] - base["traced_peak_bytes"]
    projection = (doubled["compact_projection"]["deep_bytes"]
                  - base["compact_projection"]["deep_bytes"])
    return {
        "records": records,
        "encodedBytes": encoded,
        "encodedBytesPerRecord": encoded / records,
        "peakGrowthBytes": growth,
        "overheadBytesPerRecord": (growth - encoded) / records,
        "projectionDeepBytesPerRecord": projection / records,
        "passesOld2048Gate": growth <= encoded + 2048 * records,
    }


def _summary(values) -> dict:
    return {
        "min": min(values),
        "max": max(values),
        "mean": statistics.fmean(values),
        "range": max(values) - min(values),
    }


def main() -> int:
    repetitions = int(os.environ.get("CCTALLY_MEMORY_REPETITIONS", "3"))
    with tempfile.TemporaryDirectory(prefix="cctally-508-memory-") as raw:
        work = pathlib.Path(raw)
        probe = F.run_worker(
            work, "decoded-probe", build=F.tier1_build(), memory_probe=True)
        baseline = [
            _pair(work, f"baseline-{index}", mutant=False)
            for index in range(repetitions)
        ]
        mutant = [
            _pair(work, f"mutant-{index}", mutant=True)
            for index in range(repetitions)
        ]
    decoded = probe["decoded_probe"]
    payload = {
        "schemaVersion": 1,
        "runner": platform.node(),
        "repetitions": repetitions,
        "shape": probe["shape"],
        "decodedProbe": {
            "records": decoded["records"],
            "encodedBytes": _summary(decoded["encoded_bytes"]),
            "deepBytes": _summary(decoded["deep_bytes"]),
        },
        "baseline": baseline,
        "mutant": mutant,
        "baselineOverhead": _summary(
            [item["overheadBytesPerRecord"] for item in baseline]),
        "mutantOverhead": _summary(
            [item["overheadBytesPerRecord"] for item in mutant]),
        "mutantProjectionDeep": _summary(
            [item["projectionDeepBytesPerRecord"] for item in mutant]),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
