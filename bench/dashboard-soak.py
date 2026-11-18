#!/usr/bin/env python3
"""Production-shaped dashboard memory/background-work soak (#684).

The harness owns an isolated benchmark root, launches the real dashboard with
both providers active, samples process RSS/CPU and the loopback diagnostic,
stresses reconnect/privacy/reader/diagnosis paths, performs source add/remove
and cache-rebuild invalidations, and verifies clean shutdown.  Its JSON receipt
is deliberately suitable for identical before/after runs from two checkouts.

Browser heap/DOM/listener ownership is the sibling Playwright receipt:
``dashboard/web/e2e/memory-soak.spec.ts``.  Set
``CCTALLY_BROWSER_SOAK_CYCLES=120`` for a long browser pass.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import contextlib
import ctypes
import dataclasses
import datetime as dt
import fcntl
import fractions
import functools
import hashlib
import http.client
import importlib
import importlib.machinery
import importlib.util
import io
import json
import math
import os
import pathlib
import platform
import plistlib
import pwd
import re
import selectors
import secrets
import shlex
import shutil
import signal
import socket
import sqlite3
import stat
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import types
import urllib.parse

REPO = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO / "bin" / "cctally"
FIXTURE_BUILDER = REPO / "bin" / "build-bench-fixtures.py"
PROCESS_CEILING_BYTES = 1536 * 1024 * 1024
RSS_SLOPE_CEILING_BYTES_PER_SECOND = 4 * 1024 * 1024 / 60
COMBINED_CPU_DUTY_CEILING = 0.25
API_P95_CEILING_MS = 3000
THREAD_COUNT_CEILING = 64
DISK_IO_OPS_PER_SECOND_CEILING = 500
# Counts I/O operations, not bytes: it passed through the #901 defect, so it is kept as a separate, non-discriminating gate; byte volume is the write-budget leg's.
PROCESS_CPU_PERCENT_CEILING = 50.0
IDLE_CPU_PERCENT_CEILING = 5.0
IDLE_READINESS_TIMEOUT_SECONDS = 3 * 60 * 60
RETENTION_PREPASS_TIMEOUT_SECONDS = 4 * 60 * 60
# Setup bound for one `cctally db vacuum --db conversations` of a copied store.
# Measured 368 s for the 23.1 GB full-size copy on 2026-09-25 (#857 Task B);
# the bound sits outside every measured interval.
RECLAIM_COMPACTION_TIMEOUT_SECONDS = 60 * 60
# Setup bound for a restarted dashboard to clear the product's stale reclaim
# record after a compaction; its first conversation pass does that.
RECLAIM_SETTLE_TIMEOUT_SECONDS = 30 * 60
# Setup bound for one foreground `cctally db rebuild --db stats` that brings a
# copied older-epoch stats index to the candidate's epoch before normalization.
# Measured 531 s on the full-size copy on 2026-09-25 (#857
# Task B); the bound sits outside every measured interval.
STATS_INDEX_REBUILD_TIMEOUT_SECONDS = 60 * 60
# Setup bound for an active regime's live dashboard to publish its first warm
# tick after the cold startup build and the cooldown that build earns (#313).
# Measured about 43 s on the full-size source on 2026-09-26 (#857 Task B); the
# bound sits outside every measured interval.
ACTIVE_WARM_ADMISSION_TIMEOUT_SECONDS = 10 * 60
# The pre-#901 binary's next-cycle escalation threshold. #901 retired it as a
# trigger; the harness keeps it only to read a LEGACY reclaim record that a
# baseline binary wrote (the pre-epic baseline arms).
LEGACY_RECLAIM_ESCALATION_BYTES = 256 * 1024 * 1024
# #901 §5.4 paced reclaim. Pinned copies of the product policy, kept equal by
# ``test_dashboard_soak_reclaim_policy_matches_the_product``.
RETENTION_RECLAIM_START_BYTES = 2 * 1024 ** 3
RETENTION_RECLAIM_START_RATIO = 0.20
RETENTION_RECLAIM_STOP_BYTES = 1024 ** 3
RETENTION_RECLAIM_STOP_RATIO = 0.10
RETENTION_POLICY_VERSION = 1
PUBLISH_P95_CEILING_MS = 10_000.0
CONVERSATION_P95_CEILING_MS = 10_000.0
PUBLICATION_GAP_CEILING_MS = 15_000.0
FULL_BUILD_P50_CEILING_MS = 5_000.0
FULL_BUILD_P95_CEILING_MS = 10_000.0
OWNER_TOTAL_CEILING_BYTES = 768 * 1024 * 1024
FRESHNESS_CEILING_MS = 10_000.0
QUIET_MIN_SECONDS = 150.0  # longer than the production 120s frontier expiry
QUIET_MIN_SAMPLES = 6
MIN_PRODUCTION_CONVERSATION_BYTES = 2 * 1024 * 1024 * 1024
PRE_EPIC_BASELINE = "2eb71fe3305fa91f2936a14fa935212283d72962"
REQUIRED_REGIMES = (
    "idle", "claudeActive", "codexActive", "bothActive",
    "missingHook", "ineffectiveHook", "mutationRace", "pricingSkew",
    "degradedConversation", "requestOverload",
)
FULL_SIZE_REGIMES = ("idle", "bothActive")
SMALL_FIXTURE_REGIMES = (
    "claudeActive", "codexActive", "missingHook", "ineffectiveHook",
    "mutationRace", "pricingSkew", "degradedConversation", "requestOverload",
)
assert set(FULL_SIZE_REGIMES + SMALL_FIXTURE_REGIMES) == set(REQUIRED_REGIMES)
# By operator decision (#857 Task B), the full-size ``bothActive`` gate narrows
# to event visibility inside the unchanged probe window, RSS, retained-owner
# bytes and API p95: at full size the product cannot meet the CPU, duty,
# build, freshness and cadence ceilings. Those limits moved unchanged into
# #862 Task C's acceptance. ``bothActive`` still records these fields as
# evidence, a value or null when unmeasurable, but none carries a ceiling.
BOTH_ACTIVE_UNGATED_FIELDS = (
    "processCpuPercent", "combinedCpuDuty", "fullBuildP50Ms",
    "fullBuildP95Ms", "publishP95Ms", "publishMaxMs",
    "conversationPublishP95Ms", "conversationPublishMaxMs",
    "mutationToRenderMs",
)
PUBLISH_P95_TOLERANCE_PCT = 0.15
PUBLISH_P95_TOLERANCE_FLOOR_MS = 1500.0
CONVERSATION_P95_TOLERANCE_PCT = 0.15
CONVERSATION_P95_TOLERANCE_FLOOR_MS = 1000.0
HTTP_TOLERANCE_FLOOR_MS = 10.0
DIAGNOSIS_PATH = (
    "/api/diagnosis?source=all"
    "&start_at=2025-12-31T00:00:00Z"
    "&end_at=2026-01-08T00:00:00Z"
)


def materialize_checkout_ref(ref: str, destination: pathlib.Path, *,
                             env: "dict | None" = None) -> pathlib.Path:
    """Materialize one committed baseline without mutating repository state.
    `env` (preparation v2) is the git child's environment."""
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=False)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref],
        cwd=REPO, capture_output=True, check=True, env=env,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if destination not in target.parents and target != destination:
                raise ValueError(f"archive member escapes checkout: {member.name}")
        bundle.extractall(destination, filter="fully_trusted")
    return destination


def percentile(values, q: float) -> float | None:
    values = sorted(float(value) for value in values)
    if not values:
        return None
    index = max(0, min(len(values) - 1, math.ceil(q * len(values)) - 1))
    return values[index]


def linear_slope(samples: list[dict], x: str, y: str) -> float:
    points = [(float(row[x]), float(row[y])) for row in samples]
    if len(points) < 2:
        return 0.0
    x_mean = statistics.fmean(point[0] for point in points)
    y_mean = statistics.fmean(point[1] for point in points)
    denominator = sum((px - x_mean) ** 2 for px, _py in points)
    if denominator == 0:
        return 0.0
    return sum(
        (px - x_mean) * (py - y_mean) for px, py in points
    ) / denominator


_ONE_SIDED_95_T = (
    0.0, 6.314, 2.920, 2.353, 2.132, 2.015, 1.943, 1.895,
    1.860, 1.833, 1.812, 1.796, 1.782, 1.771, 1.761, 1.753,
    1.746, 1.740, 1.734, 1.729, 1.725, 1.721, 1.717, 1.714,
    1.711, 1.708, 1.706, 1.703, 1.701, 1.699, 1.697,
)


def linear_slope_confidence_bound(
    samples: list[dict], x: str, y: str,
) -> dict | None:
    """Return the ordinary-least-squares 95% one-sided upper slope bound.

    The residual standard error uses ``n - 2`` degrees of freedom and a
    Student-t critical value. Degrees above 30 deliberately keep the df=30
    value (1.697), which is slightly more conservative than the asymptotic
    normal 1.645 value. Fewer than three points or a zero-width time axis is
    unmeasured and therefore fails closed at the caller.
    """
    points = [(float(row[x]), float(row[y])) for row in samples]
    if len(points) < 3:
        return None
    x_mean = statistics.fmean(px for px, _py in points)
    y_mean = statistics.fmean(py for _px, py in points)
    denominator = sum((px - x_mean) ** 2 for px, _py in points)
    if denominator <= 0:
        return None
    estimate = sum(
        (px - x_mean) * (py - y_mean) for px, py in points
    ) / denominator
    intercept = y_mean - estimate * x_mean
    residual_ss = sum(
        (py - (intercept + estimate * px)) ** 2 for px, py in points
    )
    degrees = len(points) - 2
    standard_error = math.sqrt((residual_ss / degrees) / denominator)
    critical = _ONE_SIDED_95_T[min(degrees, 30)]
    upper = estimate + critical * standard_error
    return {
        "confidence": 0.95,
        "estimate": estimate,
        "upper": upper,
        "samples": len(points),
    }


def sparkline(values) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    values = [float(value) for value in values]
    if not values:
        return ""
    low, high = min(values), max(values)
    if high <= low:
        return blocks[0] * len(values)
    return "".join(
        blocks[min(7, int((value - low) * 7 / (high - low)))]
        for value in values
    )


def _cpu_duty(rows) -> float | None:
    paired = [
        (int(row.get("cpu_ns") or 0), int(row["period_ns"]))
        for row in rows or ()
        if row.get("cpu_ns") is not None
        and row.get("period_ns") is not None
        and int(row["period_ns"]) > 0
    ]
    if not paired:
        return None
    return sum(cpu for cpu, _period in paired) / sum(
        period for _cpu, period in paired)


def _tick_measurements(samples: list[dict], diagnostics: list[dict]) -> dict:
    """Use distinct published ticks; polling a ring must not weight old builds."""
    by_seq = {}
    for diagnostic in diagnostics:
        for row in (diagnostic.get("tick") or {}).get("records", ()):
            if row.get("dispatch") == "full" and row.get("seq") is not None:
                by_seq[int(row["seq"])] = row
    full_builds = [
        float(row["duration_ns"]) / 1_000_000
        for _seq, row in sorted(by_seq.items())
        if row.get("duration_ns") is not None
    ]
    return {"fullBuildMs": full_builds}


def _parse_ps_cpu_time(value: str) -> float:
    """Parse ps cumulative process CPU time, including day and centisecond forms."""
    clock = value.strip()
    days = 0
    if "-" in clock:
        day_text, clock = clock.split("-", 1)
        days = int(day_text)
    fields = clock.split(":")
    if not 2 <= len(fields) <= 3:
        raise ValueError(f"invalid ps CPU time: {value!r}")
    seconds = float(fields[-1])
    minutes = int(fields[-2])
    hours = int(fields[-3]) if len(fields) == 3 else 0
    result = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"invalid ps CPU time: {value!r}")
    return result


def _process_cpu_seconds(pid: int) -> float:
    completed = subprocess.run(
        ["ps", "-o", "time=", "-p", str(pid)],
        capture_output=True, text=True, check=True,
    )
    if not completed.stdout.strip():
        raise RuntimeError(f"could not sample cumulative CPU for {pid}")
    return _parse_ps_cpu_time(completed.stdout)


def _quiet_record_verdict(tick: dict, start_ns: int, end_ns: int) -> dict:
    """Prove the quiet interval was caught up, not merely request-free."""
    records = [
        row for row in tick.get("records", ())
        if start_ns <= int(row.get("published_ns") or -1) < end_ns
    ]
    conversations = [
        row for row in tick.get("conversation_sync", ())
        if start_ns <= int(row.get("started_ns") or -1) < end_ns
    ]
    margin_ns = 15_000_000_000
    main_covered = bool(records) and (
        int(records[0]["published_ns"]) <= start_ns + margin_ns
        and int(records[-1]["published_ns"]) >= end_ns - margin_ns)
    conversation_covered = bool(conversations) and (
        int(conversations[0]["started_ns"]) <= start_ns + margin_ns
        and int(conversations[-1]["started_ns"]) >= end_ns - margin_ns)

    def counts(rows, field):
        result = {}
        for row in rows:
            value = row.get(field)
            key = value if isinstance(value, str) and value else "missing"
            result[key] = result.get(key, 0) + 1
        return dict(sorted(result.items()))

    def seconds(rows, field):
        return sum(max(0, int(row.get(field) or 0)) for row in rows) / 1e9

    def caught_up_or_revalidating(field):
        modes = [row.get(field) for row in conversations]
        return (
            "caught_up" in modes
            and all(mode in ("caught_up", "full") for mode in modes)
        )

    return {
        "tickCount": len(records),
        "conversationPassCount": len(conversations),
        "profile": {
            "coverage": {
                "main": main_covered,
                "conversation": conversation_covered,
            },
            "main": {
                "dispatchCounts": counts(records, "dispatch"),
                "cpuSeconds": seconds(records, "cpu_ns"),
                "durationSeconds": seconds(records, "duration_ns"),
                "ingestSeconds": seconds(records, "ingest_ns"),
                "builderSeconds": seconds(records, "builder_ns"),
                "cachePinSeconds": seconds(records, "cache_pin_ns"),
            },
            "conversation": {
                "statusCounts": counts(conversations, "status"),
                "claudeModeCounts": counts(conversations, "claude_mode"),
                "codexModeCounts": counts(conversations, "codex_mode"),
                "cpuSeconds": seconds(conversations, "cpu_ns"),
                "durationSeconds": seconds(conversations, "duration_ns"),
                "claudeFiles": sum(max(
                    0, int(row.get("claude_files") or 0))
                    for row in conversations),
                "codexFiles": sum(max(
                    0, int(row.get("codex_files") or 0))
                    for row in conversations),
            },
        },
        "quiet": (
            len(records) >= 4 and len(conversations) >= 4
            and main_covered and conversation_covered
            and all(row.get("dispatch") == "idle" for row in records)
            and all(
                row.get("status") == "ok"
                for row in conversations)
            and caught_up_or_revalidating("claude_mode")
            and caught_up_or_revalidating("codex_mode")
        ),
    }


def _measure_quiet_window(port: int, proc: subprocess.Popen, seconds: float) -> dict:
    """No harness HTTP, refresh or source mutation during the measured span."""
    time.sleep(10)  # settle the preceding manual refresh outside the interval
    started_cpu = _process_cpu_seconds(proc.pid)
    started_ns = time.monotonic_ns()
    readings = [{"elapsedSeconds": 0.0, "cpuSeconds": started_cpu}]
    while (time.monotonic_ns() - started_ns) / 1e9 < seconds:
        elapsed = (time.monotonic_ns() - started_ns) / 1e9
        time.sleep(min(20.0, max(0.0, seconds - elapsed)))
        if proc.poll() is not None:
            raise RuntimeError("dashboard exited during quiet interval")
        readings.append({
            "elapsedSeconds": (time.monotonic_ns() - started_ns) / 1e9,
            "cpuSeconds": _process_cpu_seconds(proc.pid),
        })
    ended_ns = time.monotonic_ns()
    wall_seconds = (ended_ns - started_ns) / 1e9
    cpu_seconds = readings[-1]["cpuSeconds"] - started_cpu
    status, raw, _latency = _request(port, "/api/debug/backend")
    if status != 200:
        raise RuntimeError("debug endpoint unavailable after quiet interval")
    tick = json.loads(raw).get("tick") or {}
    return {
        "durationSeconds": wall_seconds,
        "sampleCount": len(readings),
        "cpuPercent": (100 * cpu_seconds / wall_seconds
                       if cpu_seconds >= 0 and wall_seconds > 0 else None),
        "cpuSeconds": cpu_seconds,
        "readings": readings,
        **_quiet_record_verdict(tick, started_ns, ended_ns),
    }


def _finite_nonnegative(value) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def _finite_positive_series(values, minimum: int = 1) -> bool:
    return len(values) >= minimum and all(
        _finite_nonnegative(value) and float(value) > 0 for value in values)


def evaluate_receipt(receipt: dict) -> list[str]:
    """Return every numeric gate breach; empty means the receipt passes."""
    ceilings = receipt.get("ceilings") or {}
    problems: list[str] = []
    samples = receipt.get("samples") or []
    if len(samples) < 4:
        problems.append("fewer than four process samples; plateau is unmeasured")
    else:
        valid_rss = all(
            _finite_nonnegative(row.get("rssBytes"))
            and _finite_nonnegative(row.get("elapsedSeconds"))
            for row in samples)
        if not valid_rss:
            problems.append("RSS samples are missing or non-finite")
        else:
            peak = max(int(row["rssBytes"]) for row in samples)
            maximum = min(int(ceilings.get("processRssBytes", PROCESS_CEILING_BYTES)),
                          PROCESS_CEILING_BYTES)
            if peak > maximum:
                problems.append(f"process RSS {peak} exceeds {maximum}")
            warm = samples[len(samples) // 2:]
            slope_bound = linear_slope_confidence_bound(
                warm, "elapsedSeconds", "rssBytes")
            slope_max = min(float(ceilings.get(
                "rssSlopeBytesPerSecond", RSS_SLOPE_CEILING_BYTES_PER_SECOND)),
                RSS_SLOPE_CEILING_BYTES_PER_SECOND)
            if slope_bound is None:
                problems.append(
                    "post-warmup RSS slope confidence bound is unmeasured")
            elif float(slope_bound["upper"]) > slope_max:
                problems.append(
                    "post-warmup RSS slope 95% upper bound "
                    f"{float(slope_bound['upper']):.1f} exceeds {slope_max:.1f} B/s "
                    f"(estimate {float(slope_bound['estimate']):.1f})")
        valid_cpu = all(_finite_nonnegative(row.get("cpuPercent"))
                        for row in samples)
        if not valid_cpu:
            problems.append("CPU samples are missing or non-finite")
        else:
            cpu_peak = max(float(row["cpuPercent"]) for row in samples)
            cpu_max = min(float(ceilings.get(
                "processCpuPercent", PROCESS_CPU_PERCENT_CEILING)),
                PROCESS_CPU_PERCENT_CEILING)
            if cpu_peak > cpu_max:
                problems.append(
                    f"whole-process CPU {cpu_peak:.3f}% exceeds {cpu_max:.3f}%")
    owners = receipt.get("owners") or {}
    if not owners:
        problems.append("no retained-memory owners were reported")
    owner_total = sum(int(owner.get("estimatedBytes", 0) or 0)
                      for owner in owners.values())
    if owner_total > OWNER_TOTAL_CEILING_BYTES:
        problems.append(
            f"retained-owner bytes {owner_total} exceed {OWNER_TOTAL_CEILING_BYTES}")
    for name, owner in owners.items():
        estimated = int(owner.get("estimatedBytes", 0) or 0)
        maximum = int(owner.get("maxBytes", 0) or 0)
        entries = int(owner.get("entryCount", 0) or 0)
        max_entries = int(owner.get("maxEntries", 0) or 0)
        if maximum <= 0 or max_entries <= 0:
            problems.append(f"owner {name} has no positive byte/entry ceiling")
        if estimated > maximum:
            problems.append(f"owner {name} retains {estimated} over {maximum}")
        if entries > max_entries:
            problems.append(f"owner {name} retains {entries} entries over {max_entries}")
    combined = receipt.get("combinedCpuDuty")
    combined_max = min(float(ceilings.get(
        "combinedCpuDuty", COMBINED_CPU_DUTY_CEILING)),
        COMBINED_CPU_DUTY_CEILING)
    if not _finite_nonnegative(combined):
        problems.append("combined CPU duty is unmeasured or non-finite")
    elif float(combined) > combined_max:
        problems.append(f"combined CPU duty {float(combined):.3f} exceeds {combined_max:.3f}")
    api_latencies = receipt.get("apiLatencyMs") or []
    p95 = percentile(api_latencies, 0.95) if _finite_positive_series(
        api_latencies) else None
    api_max = min(float(ceilings.get("apiP95Ms", API_P95_CEILING_MS)),
                  API_P95_CEILING_MS)
    if p95 is None:
        problems.append("API latency is unmeasured or non-finite")
    elif p95 > api_max:
        problems.append(f"API p95 {p95:.1f}ms exceeds {api_max:.1f}ms")
    idle_cpu = receipt.get("idleCpuPercent")
    quiet = receipt.get("quietWindow") or {}
    if not (
        _finite_nonnegative(quiet.get("durationSeconds"))
        and float(quiet["durationSeconds"]) >= QUIET_MIN_SECONDS
        and int(quiet.get("sampleCount") or 0) >= QUIET_MIN_SAMPLES
        and int(quiet.get("tickCount") or 0) >= 4
        and int(quiet.get("conversationPassCount") or 0) >= 4
        and quiet.get("quiet") is True
        and _finite_nonnegative(quiet.get("cpuPercent"))
        and _finite_nonnegative(idle_cpu)
        and abs(float(quiet["cpuPercent"]) - float(idle_cpu)) < 1e-6
    ):
        problems.append(
            "quiet idle interval is absent, busy, too short or insufficiently sampled")
    if not _finite_nonnegative(idle_cpu):
        problems.append("true idle CPU is unmeasured")
    elif float(idle_cpu) > IDLE_CPU_PERCENT_CEILING:
        problems.append(
            f"idle CPU {float(idle_cpu):.3f}% exceeds "
            f"{IDLE_CPU_PERCENT_CEILING:.3f}%")
    builds = receipt.get("fullBuildMs") or []
    for label, quantile, maximum in (
        ("p50", 0.50, FULL_BUILD_P50_CEILING_MS),
        ("p95", 0.95, FULL_BUILD_P95_CEILING_MS),
    ):
        value = percentile(builds, quantile) if _finite_positive_series(
            builds, minimum=2) else None
        if value is None:
            problems.append(f"full-build {label} is unmeasured")
        elif value > maximum:
            problems.append(
                f"full-build {label} {value:.1f}ms exceeds {maximum:.1f}ms")
    for field, label in (
        ("publishPeriodsNs", "main publication cadence"),
        ("conversationPeriodsNs", "conversation publication cadence"),
        ("reconnectLatencyMs", "SSE reconnect latency"),
        ("railLatencyMs", "conversation rail latency"),
        ("liveTailLatencyMs", "dedicated live-tail latency"),
        ("readerLatencyMs", "large-reader latency"),
        ("manualRefreshLatencyMs", "manual-refresh latency"),
    ):
        if not receipt.get(field):
            problems.append(f"{label} is unmeasured")
    for field, label, maximum in (
        ("publishPeriodsNs", "main publication cadence", PUBLISH_P95_CEILING_MS),
        ("conversationPeriodsNs", "conversation publication cadence",
         CONVERSATION_P95_CEILING_MS),
    ):
        values = receipt.get(field) or []
        if _finite_positive_series(values, minimum=4):
            periods_ms = [float(value) / 1_000_000 for value in values]
            p95_ms = percentile(periods_ms, 0.95)
            if p95_ms > maximum:
                problems.append(
                    f"{label} p95 {p95_ms:.1f}ms exceeds {maximum:.1f}ms")
            longest = max(periods_ms)
            if longest > PUBLICATION_GAP_CEILING_MS:
                problems.append(
                    f"{label} max {longest:.1f}ms exceeds "
                    f"{PUBLICATION_GAP_CEILING_MS:.1f}ms")
        else:
            problems.append(f"{label} has too few or non-finite periods")
    disk_duty = receipt.get("diskIoOpsPerSecond")
    disk_max = float(ceilings.get(
        "diskIoOpsPerSecond", DISK_IO_OPS_PER_SECOND_CEILING))
    if disk_duty is None:
        problems.append("dashboard disk I/O duty is unmeasured")
    elif float(disk_duty) > disk_max:
        problems.append(
            f"disk I/O duty {float(disk_duty):.1f} exceeds {disk_max:.1f} ops/s")
    thread_peak = max(
        (int(row.get("threadCount", 0) or 0) for row in samples), default=0)
    thread_max = int(ceilings.get("threadCount", THREAD_COUNT_CEILING))
    if thread_peak <= 0:
        problems.append("server thread count is unmeasured")
    elif thread_peak > thread_max:
        problems.append(f"server thread count {thread_peak} exceeds {thread_max}")
    before = receipt.get("sqliteBefore") or {}
    after = receipt.get("sqliteAfter") or {}
    if before != after:
        problems.append("SQLite cache_size/temp_store/page_size/mmap_size changed")
    shutdown = receipt.get("shutdown") or {}
    if not shutdown.get("clean"):
        problems.append("dashboard did not shut down cleanly")
    if not shutdown.get("rssReleased"):
        problems.append("dashboard process still owned RSS after shutdown")
    stress = receipt.get("stress") or {}
    for name in (
        "privacyVariants", "providerSourceAddRemove", "accountIdentityRotation",
        "diagnosisRecovered", "cacheRebuild", "bothProvidersConfigured",
        "largeReader", "manualRefresh", "dedicatedLiveTail",
        "memoryAdmissionMeasured",
    ):
        if not stress.get(name):
            problems.append(f"stress regime {name} was not exercised")
    if int(stress.get("diagnosisTransient503Count", 0) or 0) <= 0:
        problems.append("diagnosis recovery did not observe an injected 503")
    admission = receipt.get("memoryAdmission") or {}
    targets = admission.get("targetGenerations") or {}
    observed = admission.get("measuredGenerations") or {}
    for name, target in targets.items():
        if int(observed.get(name, -1)) < int(target):
            problems.append(
                f"owner {name} was not measured after stress generation {target}")
    if receipt.get("fixtureKind") != "isolatedProductionCopy" or int(
        receipt.get("conversationStoreBytes") or 0) < MIN_PRODUCTION_CONVERSATION_BYTES:
        problems.append("production-shaped multi-GB isolated copy is unmeasured")
    frontier = receipt.get("frontierPolicy") or {}
    expiry = frontier.get("expirySeconds")
    if not _finite_nonnegative(expiry) or not (0 < float(expiry) <= 120):
        problems.append("production frontier expiry is absent or disabled")
    if frontier.get("trustEnabled") is not True:
        problems.append("production frontier trust policy was not exercised")
    retention = receipt.get("retention") or {}
    if not (int(retention.get("days") or 0) > 0
            and retention.get("dueAtStart") is True
            and retention.get("ran") is True):
        problems.append("production retention when due was not exercised")
    upgrades = receipt.get("priorSchemaUpgrades") or []
    for required_head in ("cache-044", "conversations-009"):
        matches = [row for row in upgrades if isinstance(row, dict)
                   and row.get("from") == required_head]
        if not any(row.get("passed") is True and row.get("evidence")
                   for row in matches):
            problems.append(
                f"prior-schema upgrade from {required_head} is unmeasured or failed")
    for name in REQUIRED_REGIMES:
        regime = (receipt.get("regimes") or {}).get(name) or {}
        if regime.get("passed") is not True or not regime.get("evidence"):
            problems.append(f"regime {name} is unmeasured or failed")
    if receipt.get("externalEvidenceVerified") is not True:
        problems.append(
            "verified external matrix is absent; receipt references alone cannot certify")
    freshness = receipt.get("mutationToRenderMs")
    if not _finite_nonnegative(freshness):
        problems.append("mutation-to-render freshness is unmeasured")
    elif float(freshness) > FRESHNESS_CEILING_MS:
        problems.append(
            f"mutation-to-render freshness {float(freshness):.1f}ms exceeds "
            f"{FRESHNESS_CEILING_MS:.1f}ms")
    return problems


def _request(port: int, path: str, *, host_header="127.0.0.1") -> tuple[int, bytes, float]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    started = time.perf_counter()
    try:
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", f"{host_header}:{port}")
        conn.putheader("Accept", "application/json")
        conn.endheaders()
        response = conn.getresponse()
        body = response.read()
        return response.status, body, (time.perf_counter() - started) * 1000
    finally:
        conn.close()


def _sse_reconnect(
    port: int, *, path="/api/events", host_header="127.0.0.1",
) -> float:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    started = time.perf_counter()
    try:
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", f"{host_header}:{port}")
        conn.endheaders()
        response = conn.getresponse()
        response.fp.read1(4096)
        return (time.perf_counter() - started) * 1000
    finally:
        conn.close()


def _post(port: int, path: str, body=b"{}") -> tuple[int, bytes, float]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    started = time.perf_counter()
    try:
        conn.request("POST", path, body=body, headers={
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{port}",
            "Host": f"127.0.0.1:{port}",
        })
        response = conn.getresponse()
        payload = response.read()
        return response.status, payload, (time.perf_counter() - started) * 1000
    finally:
        conn.close()


def _settle_manual_refresh(
    port: int, status: int, body: bytes, initial_latency_ms: float,
    initial_debug: dict,
) -> float:
    """Return request-to-settlement latency for synchronous or queued refresh.

    A queued request cannot start until the sync loop's 50%-duty floor permits
    it.  Immediately after a completed tick that means waiting as long as the
    preceding tick took, then running the requested rebuild itself.  Budget
    three observed tick durations (wait plus a requested rebuild allowed to be
    twice as slow), with the historical 180-second floor for small installs.
    """
    if status == 204:
        return initial_latency_ms
    if status != 202:
        raise RuntimeError(f"manual refresh answered {status}: {body[:400]!r}")
    request_id = int(json.loads(body)["request_id"])
    started = time.perf_counter() - initial_latency_ms / 1000
    tick = initial_debug.get("tick") or {}
    initial_tick_seq = int(tick.get("tick_seq", 0) or 0)
    records = tick.get("records") or ()
    last_record = next(
        (row for row in reversed(records)
         if int(row.get("seq", initial_tick_seq) or 0) == initial_tick_seq),
        records[-1] if records else {},
    )
    try:
        last_tick_seconds = max(
            0.0, float(last_record.get("duration_ns", 0) or 0) / 1e9)
    except (TypeError, ValueError):
        last_tick_seconds = 0.0
    if not math.isfinite(last_tick_seconds):
        last_tick_seconds = 0.0
    settle_timeout = max(180.0, 3.0 * last_tick_seconds)
    deadline = time.monotonic() + settle_timeout
    while time.monotonic() < deadline:
        debug_status, raw, _latency = _request(port, "/api/debug/backend")
        if debug_status == 200:
            debug = json.loads(raw)
            activity = debug.get("activity") or {}
            if int(activity.get("settled_id", 0) or 0) >= request_id:
                return (time.perf_counter() - started) * 1000
            tick_seq = int((debug.get("tick") or {}).get("tick_seq", 0))
            if not activity and tick_seq > initial_tick_seq:
                return (time.perf_counter() - started) * 1000
        time.sleep(0.1)
    raise RuntimeError(f"manual refresh request {request_id} did not settle")


def _append_initial_sync_timeline(
    diagnostics: dict, elapsed: float, status: int | None, raw: bytes,
) -> None:
    """Keep bounded raw loopback observations without retaining source JSONL."""
    rows = diagnostics.setdefault("timeline", [])
    if len(rows) >= 384:
        rows.pop(1 if len(rows) > 1 else 0)
        diagnostics["droppedObservations"] = (
            diagnostics.get("droppedObservations", 0) + 1)
    rows.append({
        "elapsedSeconds": round(elapsed, 3),
        "httpStatus": status,
        "rawDebug": raw[:65536].decode("utf-8", errors="replace"),
        "rawBytes": len(raw),
        "truncated": len(raw) > 65536,
    })


def _bounded_diagnostic_command(command: list[str]) -> dict:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, errors="replace",
            timeout=8,
        )
        return {
            "argv": command, "exitCode": result.returncode,
            "stdout": result.stdout[:131072],
            "stderr": result.stderr[:8192],
            "truncated": len(result.stdout) > 131072 or len(result.stderr) > 8192,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": command, "error": type(exc).__name__}


def _initial_sync_native_snapshot(
    pid: int, data_dir: pathlib.Path, label: str, elapsed: float,
) -> dict:
    """Capture native stacks and scoped DB/lock owners on the isolated root."""
    native_command = (["sample", str(pid), "1", "10"]
                      if sys.platform == "darwin" else
                      ["ps", "-L", "-p", str(pid), "-o", "pid,tid,stat,wchan,comm"])
    try:
        lock_paths = sorted(
            path for path in data_dir.iterdir()
            if path.is_file() and
            (path.name.endswith((".db", ".db-wal", ".db-shm", ".lock")))
        )[:32]
    except OSError:
        lock_paths = []
    return {
        "label": label, "elapsedSeconds": round(elapsed, 3), "pid": pid,
        "nativeThreads": _bounded_diagnostic_command(native_command),
        "lockHolders": _bounded_diagnostic_command(
            ["lsof", "-nP", "-Fpcfnl", *map(str, lock_paths)]
        ) if lock_paths else {"error": "no isolated DB or lock files"},
    }


def _write_initial_sync_diagnostics(path: pathlib.Path, attempt: dict) -> None:
    existing = _strict_json(path.read_bytes()) if path.exists() else {
        "schemaVersion": 1, "attempts": [],
    }
    existing["attempts"].append(attempt)
    payload = (json.dumps(existing, indent=2, sort_keys=True,
                          allow_nan=False) + "\n").encode()
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _wait_for_initial_sync(
    port: int, *, timeout: float = 1800, diagnostics: dict | None = None,
    proc: subprocess.Popen | None = None,
    data_dir: pathlib.Path | None = None,
) -> tuple[dict, float]:
    """Wait for the first real dashboard rebuild, not merely a bound port.

    The loopback server deliberately binds before its production-sized first
    snapshot is ready.  Debug and diagnosis routes can therefore answer while
    the periodic owner still holds its initial rebuilding claim.  Workload
    probes must begin only after that claim clears and a tick was published;
    otherwise a queued machine nudge is timing the cold startup build rather
    than the queue-drain contract it is meant to certify.
    """
    started = time.monotonic()
    deadline = started + timeout
    last: dict = {}
    last_raw = b""
    last_status = None
    next_timeline = started
    native_marks = (20 * 60, 30 * 60)
    captured_marks: set[int] = set()

    def capture_native(label: str, elapsed: float) -> None:
        if diagnostics is None or proc is None or data_dir is None:
            return
        diagnostics.setdefault("native", []).append(
            _initial_sync_native_snapshot(proc.pid, data_dir, label, elapsed))

    capture_native("startup+0m", 0.0)
    while time.monotonic() < deadline:
        try:
            status, raw, latency = _request(port, "/api/debug/backend")
        except Exception:
            if diagnostics is not None:
                diagnostics["outcome"] = "request-error"
                capture_native("decisive-failure", time.monotonic() - started)
            raise
        elapsed = time.monotonic() - started if diagnostics is not None else 0.0
        last_raw, last_status = raw, status
        if diagnostics is not None and started + elapsed >= next_timeline:
            _append_initial_sync_timeline(diagnostics, elapsed, status, raw)
            next_timeline = started + elapsed + 30
        for mark in native_marks:
            if mark not in captured_marks and elapsed >= mark:
                captured_marks.add(mark)
                capture_native(f"startup+{mark // 60}m", elapsed)
        if status == 200:
            last = _strict_json(raw)
            activity = last.get("activity") or {}
            tick = last.get("tick") or {}
            if (not bool(activity.get("rebuilding"))
                    and int(tick.get("tick_seq", 0) or 0) > 0):
                if diagnostics is not None:
                    _append_initial_sync_timeline(
                        diagnostics, elapsed, status, raw)
                    diagnostics["outcome"] = "ready"
                return last, latency
        time.sleep(0.5)
    if diagnostics is not None:
        _append_initial_sync_timeline(
            diagnostics, time.monotonic() - started, last_status, last_raw)
        diagnostics["outcome"] = "timeout"
        capture_native("decisive-failure", time.monotonic() - started)
    activity = last.get("activity") or {}
    tick = last.get("tick") or {}
    raise RuntimeError(
        "dashboard initial sync did not settle: "
        f"activity={activity!r} tick_seq={tick.get('tick_seq', 0)!r}")


def _wait_for_diagnosis(port: int, *, timeout: float = 180) -> tuple[int, list[float]]:
    """Wait through fail-closed generation transitions for one coherent read."""
    deadline = time.monotonic() + timeout
    transient = 0
    latencies = []
    while time.monotonic() < deadline:
        status, body, latency = _request(port, DIAGNOSIS_PATH)
        latencies.append(latency)
        if status == 200:
            return transient, latencies
        if status != 503:
            raise RuntimeError(
                f"{DIAGNOSIS_PATH} answered {status}: {body[:400]!r}")
        transient += 1
        time.sleep(0.5)
    raise RuntimeError(
        f"{DIAGNOSIS_PATH} did not become coherent after {transient} transient 503s")


def _exercise_diagnosis_recovery(
    port: int, data_dir: pathlib.Path,
) -> tuple[int, list[float]]:
    """Force one read-only store outage, then prove a real 503 -> 200 cycle."""
    stats_path = data_dir / "stats.db"
    held_path = data_dir / "stats.db.dashboard-soak-held"
    stats_path.replace(held_path)
    try:
        status, body, latency = _request(port, DIAGNOSIS_PATH)
    finally:
        held_path.replace(stats_path)
    if status != 503:
        raise RuntimeError(
            "diagnosis outage injection did not fail closed: "
            f"status={status} body={body[:400]!r}"
        )
    transient, recovery_latency = _wait_for_diagnosis(port, timeout=60)
    return 1 + transient, [latency, *recovery_latency]


def _peak_memory_owners(diagnostics: list[dict]) -> dict:
    """Retain the maximum observed bytes/entries for every named owner."""
    peaks: dict[str, dict] = {}
    for diagnostic in diagnostics:
        owners = (diagnostic.get("memory") or {}).get("owners") or {}
        for name, owner in owners.items():
            peak = peaks.setdefault(name, {})
            for field in (
                "estimatedBytes", "maxBytes", "entryCount", "maxEntries",
                "evictionCount", "fallbackCount", "measurementErrorCount",
            ):
                peak[field] = max(
                    int(peak.get(field, 0) or 0),
                    int(owner.get(field, 0) or 0),
                )
    return peaks


def _combined_cpu_duty(diagnostic: dict) -> float | None:
    tick = diagnostic.get("tick") or {}
    main = _cpu_duty(tick.get("records"))
    conversation = _cpu_duty(tick.get("conversation_sync"))
    if main is None or conversation is None:
        return None
    return main + conversation


def _process_sample(pid: int) -> dict[str, float | int]:
    completed = subprocess.run(
        ["ps", "-o", "rss=,%cpu=,inblk=,oublk=", "-p", str(pid)],
        capture_output=True, text=True, check=True,
    )
    parts = completed.stdout.split()
    if len(parts) < 2:
        raise RuntimeError(f"could not sample process {pid}")
    io_values = []
    for value in parts[2:4]:
        try:
            io_values.append(int(value))
        except ValueError:
            io_values.append(0)
    if sys.platform == "darwin":
        thread_rows = subprocess.run(
            ["ps", "-M", "-p", str(pid)], capture_output=True, text=True,
            check=True,
        ).stdout.splitlines()
        thread_count = max(0, len(thread_rows) - 1)
    else:
        thread_text = subprocess.run(
            ["ps", "-o", "nlwp=", "-p", str(pid)], capture_output=True,
            text=True, check=True,
        ).stdout.strip()
        thread_count = int(thread_text or 0)
    return {
        "rssBytes": int(parts[0]) * 1024,
        "cpuPercent": float(parts[1]),
        "diskReadOps": io_values[0] if io_values else 0,
        "diskWriteOps": io_values[1] if len(io_values) > 1 else 0,
        "threadCount": thread_count,
    }


def _sqlite_settings(data_dir: pathlib.Path) -> dict:
    result = {}
    for name in ("cache.db", "conversations.db", "stats.db"):
        path = data_dir / name
        if not path.exists():
            continue
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            result[name] = {
                "cache_size": int(conn.execute("PRAGMA cache_size").fetchone()[0]),
                "temp_store": int(conn.execute("PRAGMA temp_store").fetchone()[0]),
                "page_size": int(conn.execute("PRAGMA page_size").fetchone()[0]),
                "mmap_size": int(conn.execute("PRAGMA mmap_size").fetchone()[0]),
            }
        finally:
            conn.close()
    return result


def _disk_bytes(data_dir: pathlib.Path) -> int:
    return sum(
        path.stat().st_size for path in data_dir.iterdir()
        if path.is_file() and (
            path.name.endswith(".db") or "-wal" in path.name or "-shm" in path.name)
    )


def _build_fixture(root: pathlib.Path, scale: str, seed: int, *,
                   env: "dict | None" = None) -> None:
    subprocess.run(
        [sys.executable, str(FIXTURE_BUILDER), "--scale", scale,
         "--seed", str(seed), "--out", str(root)],
        check=True, env=env,
    )


def _prepare_fixture(
    root: pathlib.Path, fixture_copy: pathlib.Path | None,
    scale: str, seed: int,
) -> str:
    """Clone an operator-prepared copy; never run mutations in its source."""
    if fixture_copy is None:
        _build_fixture(root, scale, seed)
        return "synthetic"
    source = pathlib.Path(fixture_copy).expanduser().resolve()
    root = root.expanduser().resolve()
    if source == root or source in root.parents or root in source.parents:
        raise ValueError("fixture source and measurement root must not overlap")
    if not (source / "data" / "conversations.db").is_file():
        raise ValueError("fixture copy lacks data/conversations.db")
    if not (source / "claude" / "projects").is_dir() or not list(
        source.glob("codex-*/sessions")):
        raise ValueError("fixture copy needs Claude and Codex source roots")
    _refuse_surviving_foreign_corpus(source)
    for walk_root, directories, files in os.walk(source, followlinks=False):
        for name in (*directories, *files):
            if (pathlib.Path(walk_root) / name).is_symlink():
                raise ValueError("fixture copy contains a symlink")
    shutil.copytree(source, root)
    return "isolatedProductionCopy"


_NORMALIZED_MARKER = ".cctally-soak-normalized.json"
_MARKER_BASE_KEYS = frozenset({"schemaVersion", "root", "fixtureSourceFingerprint"})
_MARKER_RETAINED_KEYS = frozenset({"retainedPriorSourceRoot",
                                   "retainedPriorSourceSha256"})
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _fixture_marker_roots(source: pathlib.Path) -> dict:
    """#879 §5.1 step 1: the roots an operator fixture's normalization marker
    names, {label: absolute root}. No marker is acceptable (safety never comes
    from the marker); a present one must be a regular file holding exactly
    one of the two shapes the harness writes, or the copy refuses."""
    marker = source / _NORMALIZED_MARKER
    if not os.path.lexists(marker):
        return {}
    if marker.is_symlink() or not marker.is_file():
        raise ValueError(f"fixture marker {marker} is malformed: it must be a "
                         "regular file, not a symlink")
    try:
        value = _strict_json(marker.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError(f"fixture marker {marker} is malformed: {exc}") from exc
    keys = set(value)
    if keys not in (_MARKER_BASE_KEYS, _MARKER_BASE_KEYS | _MARKER_RETAINED_KEYS):
        missing = sorted((_MARKER_BASE_KEYS | (
            _MARKER_RETAINED_KEYS if keys & _MARKER_RETAINED_KEYS else set()))
            - keys)
        extra = sorted(keys - _MARKER_BASE_KEYS - _MARKER_RETAINED_KEYS)
        raise ValueError(f"fixture marker {marker} is malformed: missing "
                         f"field(s) {missing}, unexpected field(s) {extra}")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ValueError(f"fixture marker {marker} is malformed: field "
                         f"schemaVersion is {value['schemaVersion']!r}, not 1")
    for field in ("root", "retainedPriorSourceRoot"):
        if field in value and (not isinstance(value[field], str)
                               or not os.path.isabs(value[field])):
            raise ValueError(f"fixture marker {marker} is malformed: field "
                             f"{field} is not an absolute path")
    for field in ("fixtureSourceFingerprint", "retainedPriorSourceSha256"):
        if field in value and (not isinstance(value[field], str)
                               or not _HEX64.fullmatch(value[field])):
            raise ValueError(f"fixture marker {marker} is malformed: field "
                             f"{field} is not 64 lowercase hex digits")
    roots = {"marker root": value["root"]}
    if "retainedPriorSourceRoot" in value:
        roots["retained prior root"] = value["retainedPriorSourceRoot"]
    return roots


def _refuse_surviving_foreign_corpus(source: pathlib.Path) -> None:
    """#879 §5.1: refuse an operator fixture, before any copy, when its
    normalization would keep transcript rows describing a corpus still on
    this host. Only `conversation_source_files` keeps out-of-scope rows across
    a rebuild, and only for paths that are not genuinely absent (an `lstat`
    that raised ENOENT or ENOTDIR); any other error keeps the row, so it
    counts as surviving. Reads one table read-only, never the corpus."""
    source = pathlib.Path(source)
    roots = _fixture_marker_roots(source)
    store = source / "data" / "conversations.db"
    try:
        conn = sqlite3.connect(
            f"file:{urllib.parse.quote(str(store))}?mode=ro&immutable=1",
            uri=True)
        try:
            present_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'conversation_source_files'").fetchone()
            paths = ([row[0] for row in conn.execute(
                "SELECT path FROM conversation_source_files")]
                if present_table else [])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ValueError(f"fixture copy's {store} is unreadable, so whether "
                         f"its transcript rows survive normalization is "
                         f"unknown: {exc}") from exc
    present, unreadable = [], []
    for stored in paths:
        if not isinstance(stored, str):
            continue
        try:
            os.lstat(stored)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            unreadable.append((stored, exc.errno))
        else:
            present.append(stored)
    surviving = sorted([*present, *(path for path, _errno in unreadable)])
    if not surviving:
        return
    first = surviving[0]
    errnos = dict(unreadable)
    example = first if first not in errnos else f"{first} (errno {errnos[first]})"
    located = []
    for label, root in roots.items():
        prefix = root.rstrip("/") + "/"
        inside = sum(1 for path in surviving if path == root
                     or path.startswith(prefix))
        if inside:
            located.append(f"{inside} under the {label} {root}")
    where = f"; {', '.join(located)}" if located else ""
    raise ValueError(
        f"fixture conversations.db keeps {len(surviving)} transcript row(s) "
        f"whose source is still on this host ({len(present)} present, "
        f"{len(unreadable)} unreadable), e.g. {example}{where}; normalizing a "
        "copy would keep them and refuse after the full rebuild. Remedy: run "
        "on a host where those paths are absent, or move that corpus away "
        "first (never a #857 artifact without the operator's approval of the "
        "exact paths); for the §6.4 write-budget leg, use preparation v2, "
        "which mounts the fixture at its own root.")


def _fixture_source_fingerprint(source: pathlib.Path) -> str:
    """Digest source population metadata without reading multi-GB DB bytes."""
    source = pathlib.Path(source).expanduser().resolve()
    digest = hashlib.sha256()
    for walk_root, directories, files in os.walk(source, followlinks=False):
        directories.sort()
        for name in sorted(files):
            path = pathlib.Path(walk_root) / name
            stat = path.stat()
            relative = path.relative_to(source).as_posix()
            digest.update(
                f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def _fixture_identity_problems(before: dict, after: dict) -> list[str]:
    problems = []
    first = before.get("fixtureSourceFingerprint")
    second = after.get("fixtureSourceFingerprint")
    if not first or not second or first != second:
        problems.append(
            "baseline and candidate fixture population fingerprints differ or are missing")
    before_host = before.get("measurementHost")
    after_host = after.get("measurementHost")
    if not before_host or not after_host or before_host != after_host:
        problems.append(
            "baseline and candidate measurement hosts differ or are missing")
    return problems


def _retention_state(data_dir: pathlib.Path, now_utc: dt.datetime) -> dict:
    """Read the copied store's effective retention and pre-run due state."""
    try:
        config = json.loads((data_dir / "config.json").read_text())
    except (OSError, ValueError):
        config = {}
    block = config.get("conversation") if isinstance(config, dict) else None
    raw_days = block.get("retention_days", 90) if isinstance(block, dict) else 90
    try:
        days = int(raw_days) if not isinstance(raw_days, bool) else None
    except (TypeError, ValueError):
        days = None
    if days is None or days < 0:
        return {"days": None, "dueAtStart": None}
    path = data_dir / "conversations.db"
    if not path.is_file():
        return {"days": days, "dueAtStart": None}
    try:
        uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute(
                "SELECT value FROM cache_meta WHERE key=?",
                ("conversation_retention_last_prune_at",),
            ).fetchone()
    except sqlite3.Error:
        return {"days": days, "dueAtStart": None}
    if row is None or not row[0]:
        return {"days": days, "dueAtStart": True}
    try:
        last = dt.datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return {"days": days, "dueAtStart": True}
    if last.tzinfo is None or last.utcoffset() is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    return {
        "days": days,
        "dueAtStart": (now_utc - last).total_seconds() >= 24 * 60 * 60,
    }


def _make_retention_due(
    data_dir: pathlib.Path, now_utc: dt.datetime,
) -> None:
    """Age only the isolated clone's throttle so due retention is exercised."""
    due_marker = (now_utc - dt.timedelta(days=2)).isoformat()
    with contextlib.closing(sqlite3.connect(data_dir / "conversations.db")) as conn:
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("conversation_retention_last_prune_at", due_marker),
        )
        conn.commit()


def _reclaim_record_pending(state) -> "tuple[bool, int]":
    """Whether a durable reclaim record still asks for reclaim, and its
    backlog. A #901 record (`policy_version`) carries the pacing ledger for
    good, so its presence means nothing: it is pending only while its episode
    is eligible. A legacy record (a pre-#901 binary) is pending whenever it
    exists. Malformed records raise, so callers fail closed."""
    if not isinstance(state, dict):
        raise ValueError("malformed reclaim backlog")
    if state.get("policy_version") is not None:
        raw = state.get("unreclaimed_bytes", 0)
        pending = state.get("eligible") is True
    else:
        if "unreclaimed_bytes" not in state:
            raise ValueError("malformed reclaim backlog")
        raw = state["unreclaimed_bytes"]
        pending = True
    try:
        backlog = int(raw or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("malformed reclaim backlog") from exc
    if backlog < 0:
        raise ValueError("malformed reclaim backlog")
    return pending, backlog


def _read_reclaim_record(conn) -> "dict | None":
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key=?",
        ("conversation_retention_reclaim_pending",),
    ).fetchone()
    if row is None or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("malformed reclaim backlog") from exc


def _reclaim_record_is_legacy(data_dir: pathlib.Path) -> bool:
    path = data_dir / "conversations.db"
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        state = _read_reclaim_record(conn)
    return isinstance(state, dict) and state.get("policy_version") is None


def _require_no_reclaim_backlog(data_dir: pathlib.Path) -> dict:
    """Fail closed unless the copied conversation store has no reclaim
    backlog: an empty freelist and no durable record still asking for one."""
    path = data_dir / "conversations.db"
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        state = _read_reclaim_record(conn)
        if state is not None:
            pending, backlog = _reclaim_record_pending(state)
            if pending:
                raise ValueError(f"reclaim backlog remains: {backlog} bytes")
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    if freelist:
        raise ValueError(f"reclaim backlog remains: {freelist * page_size} freelist bytes")
    return {"pendingBytes": 0, "freelistBytes": 0}


def _reclaim_backlog_snapshot(data_dir: pathlib.Path) -> dict:
    """Record the copied store's reclaim state without judging it.

    ``pendingBytes`` is the durable record's backlog while that record still
    asks for reclaim (None otherwise) and ``freelistBytes`` the physical
    freelist; the two differ once a compaction has emptied the freelist
    behind a record the product has not yet updated.
    """
    path = data_dir / "conversations.db"
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        state = _read_reclaim_record(conn)
        pending = None
        if state is not None:
            asks, backlog = _reclaim_record_pending(state)
            pending = backlog if asks else None
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    return {"pendingBytes": pending, "freelistBytes": freelist * page_size}


def _compact_reclaim_backlog(
    binary: pathlib.Path, env: dict[str, str], data_dir: pathlib.Path, *,
    timeout_seconds: float, run=None,
) -> dict:
    """Drain a copied store's reclaim backlog with the product's compaction.

    Production reclaim is paced (#901: at most 4 MiB of maintenance writes a
    minute, and only above 2 GiB and 20% free), and the pre-#901 binary went
    dormant below ``LEGACY_RECLAIM_ESCALATION_BYTES`` until its next daily run,
    so it cannot bring a multi-GB clone to zero inside any setup bound: the
    2026-09-24 full-size copy drained about 143 pages a second and needed about
    19 hours just to reach dormancy. ``cctally db vacuum --db conversations`` is
    the product's documented manual remedy. It leaves the durable pending
    record behind, which the product clears on its next reclaim pass. Below the
    escalation threshold that pass waits a day, so only then is the clone's
    throttle aged to make it the next one. The caller runs the dashboard and
    waits for the record to clear.
    """
    try:
        _require_no_reclaim_backlog(data_dir)
    except ValueError:
        pass
    else:
        return {"ran": False}
    before = _reclaim_backlog_snapshot(data_dir)
    family_before = _conversation_family_bytes(data_dir)
    started = time.monotonic()
    runner = subprocess.run if run is None else run
    try:
        result = runner(
            [str(binary), "db", "vacuum", "--db", "conversations"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "conversation compaction exceeded its setup bound") from exc
    duration = time.monotonic() - started
    if result.returncode != 0:
        raise RuntimeError(
            "conversation compaction failed: " + (result.stderr or "")[-2000:])
    after = _reclaim_backlog_snapshot(data_dir)
    family_after = _conversation_family_bytes(data_dir)
    if after["freelistBytes"]:
        raise RuntimeError(
            f"conversation compaction left {after['freelistBytes']} freelist bytes")
    made_due = (_reclaim_record_is_legacy(data_dir)
                and after["pendingBytes"] is not None
                and after["pendingBytes"] < LEGACY_RECLAIM_ESCALATION_BYTES)
    if made_due:
        _make_retention_due(data_dir, dt.datetime.now(dt.timezone.utc))
    return {
        "ran": True,
        "command": "cctally db vacuum --db conversations",
        "durationSeconds": round(duration, 3),
        "pendingBytesBefore": before["pendingBytes"] or 0,
        "freelistBytesBefore": before["freelistBytes"],
        "familyBytesBefore": family_before,
        "familyBytesAfter": family_after,
        "reclaimedBytes": family_before - family_after,
        "stalePendingBytes": after["pendingBytes"],
        "retentionMadeDue": made_due,
    }


def _retention_payload_bytes(data_dir: pathlib.Path) -> int:
    """Count retained transcript payload bytes, excluding index overhead."""
    path = data_dir / "conversations.db"
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        claude = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(text) + LENGTH(blocks_json)), 0) "
            "FROM conversation_messages"
        ).fetchone()[0]
        codex = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(payload_json)), 0) "
            "FROM codex_conversation_events"
        ).fetchone()[0]
    return int(claude) + int(codex)


def _conversation_family_bytes(data_dir: pathlib.Path) -> int:
    base = data_dir / "conversations.db"
    return sum(path.stat().st_size for path in (
        base, pathlib.Path(f"{base}-wal"), pathlib.Path(f"{base}-shm"),
    ) if path.exists())


def _collect_retention_phases(live, phases: dict) -> None:
    """Fold one dashboard's published maintenance ring into ``phases``."""
    if live.proc.poll() is not None:
        raise RuntimeError("retention dashboard exited")
    status, raw, _latency = _request(live.port, "/api/debug/backend")
    if status == 200:
        tick = (_strict_json(raw).get("tick") or {})
        for phase in tick.get("maintenance") or ():
            phases[int(phase["seq"])] = phase


def _settle_reclaim_backlog(
    root: pathlib.Path, binary: pathlib.Path, env: dict[str, str],
    sync_interval: float, *, deadline: float,
) -> dict:
    """Leave a stopped clone with no reclaim backlog, using only the product.

    Compacts with ``_compact_reclaim_backlog`` and, when that leaves the
    product's durable record behind, runs the real dashboard until the
    product's own reclaim pass clears it. Returns the compaction record with
    the settle dashboard's maintenance phases.
    """
    data_dir = root / "data"
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("reclaim settle reached its setup bound before compaction")
    compaction = _compact_reclaim_backlog(
        binary, env, data_dir,
        timeout_seconds=min(RECLAIM_COMPACTION_TIMEOUT_SECONDS, remaining))
    phases = {}
    settle_started = time.monotonic()
    if compaction["ran"]:
        with _live_regime_dashboard(
            root, binary, env, sync_interval, admit_initial_sync=False,
        ) as live:
            while time.monotonic() < deadline:
                _collect_retention_phases(live, phases)
                try:
                    _require_no_reclaim_backlog(data_dir)
                except (ValueError, sqlite3.Error):
                    pass
                else:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(
                    "compacted reclaim record was not cleared before setup bound")
    return {**compaction,
            "settleSeconds": round(time.monotonic() - settle_started, 3),
            "settleMaintenancePhases": [phases[key] for key in sorted(phases)]}


def _run_retention_prepass(args, receipt: dict) -> dict:
    """Run the one due deletion, then drain its reclaim before staging.

    The product's own dashboard performs the due deletion and its budgeted
    reclaim continuation. When that continuation drains the store, as on the
    small fixture, nothing else runs. Otherwise the remainder is compacted with
    the product's ``db vacuum`` and a second dashboard lets the product clear
    its now-stale durable record, because budgeted reclaim cannot finish a
    multi-GB backlog inside a setup bound (see ``_compact_reclaim_backlog``).
    Both phases share one setup bound, and the evidence names which mechanism
    drained the store.
    """
    root = pathlib.Path(args.root).expanduser().resolve()
    data_dir = root / "data"
    checkout = pathlib.Path(args.checkout or REPO).expanduser().resolve()
    fingerprint = receipt["fixtureSourceFingerprint"]
    if _fixture_source_fingerprint(args.fixture_copy) != fingerprint:
        raise RuntimeError("operator fixture provenance changed before retention")
    inherited = _reclaim_backlog_snapshot(data_dir)
    _make_retention_due(data_dir, dt.datetime.now(dt.timezone.utc))
    due = _retention_state(data_dir, dt.datetime.now(dt.timezone.utc))
    if due.get("dueAtStart") is not True or int(due.get("days") or 0) <= 0:
        raise ValueError("retention prepass did not start when due")
    payload_before = _retention_payload_bytes(data_dir)
    family_before = _conversation_family_bytes(data_dir)
    started_at = dt.datetime.now(dt.timezone.utc)
    started = time.monotonic()
    maintenance = {}
    deadline = started + RETENTION_PREPASS_TIMEOUT_SECONDS
    binary = checkout / "bin" / "cctally"
    env = _fixture_env(root, production_shaped=True)
    final = None
    with _live_regime_dashboard(
        root, binary, env, args.sync_interval, admit_initial_sync=False,
    ) as live:
        while time.monotonic() < deadline:
            _collect_retention_phases(live, maintenance)
            deleted = [seq for seq, row in maintenance.items()
                       if row.get("phase") == "delete" and row.get("outcome") == "ok"]
            if deleted:
                try:
                    final = _require_no_reclaim_backlog(data_dir)
                except (ValueError, sqlite3.Error):
                    if any(seq > min(deleted) and row.get("phase") == "reclaim"
                           for seq, row in maintenance.items()):
                        break
                else:
                    break
            time.sleep(0.5)
        else:
            raise RuntimeError(
                "retention prepass did not run its due deletion before setup bound")
    backlog = _reclaim_backlog_snapshot(data_dir)
    production = {
        "durationSeconds": round(time.monotonic() - started, 3),
        "familyBytesAfter": _conversation_family_bytes(data_dir),
        "backlogBytes": max(backlog["pendingBytes"] or 0, backlog["freelistBytes"]),
    }
    compaction = {"ran": False}
    settle = None
    if final is None:
        settled = _settle_reclaim_backlog(
            root, binary, env, args.sync_interval, deadline=deadline)
        settle_phases = settled.pop("settleMaintenancePhases")
        settle = {
            "durationSeconds": settled.pop("settleSeconds"),
            # A second due deletion happens only when the compacted record was
            # dormant and its throttle had to be aged: setup cleanup, labelled.
            "deletionRan": any(row.get("phase") == "delete" for row in settle_phases),
            "phases": settle_phases,
        }
        compaction = settled
        final = _require_no_reclaim_backlog(data_dir)
    payload_after = _retention_payload_bytes(data_dir)
    family_after = _conversation_family_bytes(data_dir)
    if payload_after > payload_before or family_after > family_before:
        raise RuntimeError("retention prepass grew copied conversation store")
    if _fixture_source_fingerprint(args.fixture_copy) != fingerprint:
        raise RuntimeError("operator fixture provenance changed during retention")
    duration = time.monotonic() - started
    finished_at = dt.datetime.now(dt.timezone.utc)
    measurements = {
        "durationSeconds": duration,
        "deletedPayloadBytes": payload_before - payload_after,
        "reclaimedBytes": family_before - family_after,
        **final,
        "dueAtStart": True, "ran": True, "complete": True,
        "days": due["days"],
        "familyBytesBefore": family_before,
        "familyBytesAfter": family_after,
        "inheritedPendingBytes": inherited["pendingBytes"] or 0,
        "inheritedFreelistBytes": inherited["freelistBytes"],
        "drainMechanism": (
            "production+compaction" if compaction["ran"] else "production"),
        "maintenancePhases": [maintenance[key] for key in sorted(maintenance)],
        "production": production,
        "compaction": compaction,
        "settle": settle,
    }
    return {
        "provenance": {
            "host": receipt["measurementHost"],
            "fixtureSourceFingerprint": fingerprint,
            "startedAt": started_at.isoformat(),
            "finishedAt": finished_at.isoformat(),
            "command": {"argv": [sys.executable, *sys.argv],
                        "exitCode": 0},
        },
        "execution": {"durationMs": duration * 1000, "stdout": "", "stderr": ""},
        "measurements": measurements,
    }


def _frontier_expiry_from_checkout(checkout: pathlib.Path) -> float | None:
    """Read the literal runtime policy without importing a foreign checkout."""
    try:
        tree = ast.parse(
            (checkout / "bin" / "_lib_ingest_frontier.py").read_text())
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and
                   target.id == "FRONTIER_CERTIFICATE_MAX_AGE_SECONDS"
                   for target in node.targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(
            node.value.value, (int, float)) and not isinstance(
            node.value.value, bool):
            value = float(node.value.value)
            return value if math.isfinite(value) else None
        return None
    return None


def _fixture_env(
    root: pathlib.Path, *, production_shaped: bool = False,
) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "CCTALLY_DATA_DIR": str(root / "data"),
        "CLAUDE_CONFIG_DIR": str(root / "claude"),
        "CODEX_HOME": ",".join(str(path) for path in sorted(root.glob("codex-*"))),
        "HOME": str(root / "home"),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
    })
    if production_shaped:
        # A production copy must exercise current retention, windows and
        # freshness. Inheriting a fixture pin can move all three out of the
        # copied population and make a large-store run vacuous.
        env.pop("CCTALLY_AS_OF", None)
    else:
        env["CCTALLY_AS_OF"] = "2026-01-07T00:00:00Z"
    return env


def _isolated_frontier_hook_documents(
    binary: pathlib.Path, root: pathlib.Path,
    *, hook_key_root: pathlib.Path | None = None,
) -> dict[pathlib.Path, str]:
    """Render the exact hook files installed by this soak harness."""
    claude_command = f"{shlex.quote(str(binary))} hook-tick"
    settings_path = root / "home" / ".claude" / "settings.json"
    expected = {
        settings_path: json.dumps({
            "hooks": {
                event: [{
                    "matcher": "*" if event == "PostToolBatch" else "",
                    "hooks": [{
                        "type": "command", "command": claude_command,
                    }],
                }]
                for event in ("PostToolBatch", "Stop", "SubagentStop")
            },
        }, indent=2, sort_keys=True) + "\n",
    }

    codex_command = (
        f"{shlex.quote(str(binary))} hook-tick --foreground --source codex")
    codex_payload = json.dumps({
        "hooks": {
            event: [{"hooks": [{
                "type": "command", "command": codex_command, "timeout": 30,
            }]}]
            for event in ("Stop", "SubagentStop")
        },
    }, indent=2, sort_keys=True) + "\n"
    for codex_home in sorted(root.glob("codex-*")):
        if not (codex_home / "sessions").is_dir():
            continue
        hooks_path = codex_home / "hooks.json"
        config_path = codex_home / "config.toml"
        state_rows = []
        for token in ("stop", "subagent_stop"):
            key_path = ((hook_key_root / codex_home.name / "hooks.json")
                        if hook_key_root is not None else hooks_path)
            key = f"{key_path}:{token}:0:0"
            state_rows.append(
                f"{json.dumps(key)} = {{ trusted_hash = "
                f"\"cctally-soak-isolated\", enabled = true }}")
        expected[hooks_path] = codex_payload
        expected[config_path] = (
            "[hooks.state]\n" + "\n".join(state_rows) + "\n")
    return expected


def _prepare_isolated_frontier_hooks(
    binary: pathlib.Path, root: pathlib.Path,
) -> None:
    """Refresh recognized generated hooks and log inside a disposable clone.

    A normalized source may carry documents from an earlier checkout and
    measurement root. Validate every existing document against that recorded
    origin before changing any file in the clone.
    """
    binary = pathlib.Path(binary).expanduser().resolve()
    root = pathlib.Path(root).expanduser().resolve()
    expected = _isolated_frontier_hook_documents(binary, root)
    log_path = root / "dashboard-soak.log"
    if any(path.is_symlink() or (path.exists() and not path.is_file())
           for path in expected) or (
        log_path.is_symlink() or (log_path.exists() and not log_path.is_file())
    ):
        raise ValueError("unrecognized pre-existing isolated measurement artifact")
    existing = {path: path.read_text() for path in expected if path.is_file()}
    if existing and set(existing) != set(expected):
        raise ValueError("incomplete pre-existing isolated measurement hook set")

    old_expected = {}
    marker_path = root / ".cctally-soak-normalized.json"
    if marker_path.is_symlink():
        raise ValueError("invalid prior hook provenance")
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text())
            origin_root = pathlib.Path(marker["root"])
            fingerprint = marker["fixtureSourceFingerprint"]
            base_fields = {"schemaVersion", "root", "fixtureSourceFingerprint"}
            retained_fields = base_fields | {
                "retainedPriorSourceRoot", "retainedPriorSourceSha256"}
            if (set(marker) not in (base_fields, retained_fields)
                    or marker["schemaVersion"] != 1
                    or not origin_root.is_absolute()
                    or not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None):
                raise ValueError("invalid prior hook provenance")
            if set(marker) == retained_fields:
                prior_root = pathlib.Path(marker["retainedPriorSourceRoot"])
                prior_digest = marker["retainedPriorSourceSha256"]
                if (origin_root != root or not prior_root.is_absolute()
                        or prior_root.is_symlink()
                        or prior_root.resolve() != prior_root
                        or prior_root == root
                        or not isinstance(prior_digest, str)
                        or re.fullmatch(r"[0-9a-f]{64}", prior_digest) is None
                        or _source_tree_digest(prior_root) != prior_digest):
                    raise ValueError("invalid prior hook provenance")
            if existing:
                settings_path = root / "home" / ".claude" / "settings.json"
                settings = json.loads(existing[settings_path])
                old_command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
                command = shlex.split(old_command)
                old_binary = pathlib.Path(command[0])
                if (command[1:] != ["hook-tick"]
                        or not old_binary.is_absolute()
                        or old_binary.name != "cctally"
                        or old_binary.parent.name != "bin"):
                    raise ValueError("invalid prior hook provenance")
                old_expected = _isolated_frontier_hook_documents(
                    old_binary, root, hook_key_root=origin_root)
        except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
            raise ValueError("invalid prior hook provenance") from exc

    for path, payload in existing.items():
        if payload != expected[path] and payload != old_expected.get(path):
            raise ValueError(
                f"unrecognized pre-existing isolated measurement hook document: {path}")
    if log_path.exists() and (
        not old_expected or set(existing) != set(expected)
    ):
        raise ValueError("unrecognized pre-existing isolated measurement log")

    if log_path.exists():
        log_path.unlink()
    for path, payload in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if existing.get(path) != payload:
            path.write_text(payload)


def _copy_stress_sources(root: pathlib.Path) -> list[pathlib.Path]:
    created = []
    claude_sources = sorted((root / "claude" / "projects").rglob("*.jsonl"))
    codex_sources = sorted(root.glob("codex-*/sessions/**/*.jsonl"))
    if claude_sources:
        target = root / "claude" / "projects" / "soak-added" / "soak.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(claude_sources[0], target)
        created.append(target)
    if codex_sources:
        codex_root = next(
            parent for parent in codex_sources[0].parents
            if parent.name.startswith("codex-"))
        target = codex_root / "sessions" / "soak-added" / "soak.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(codex_sources[0], target)
        created.append(target)
    return created


def _cleanup_stress_sources(paths: list[pathlib.Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def _rotate_codex_accounts(root: pathlib.Path) -> dict[pathlib.Path, bytes]:
    """Rotate scratch auth identities so account-scoped caches re-key live."""
    auth_paths = sorted(root.glob("codex-*/auth.json"))
    if len(auth_paths) < 2:
        return {}
    originals = {path: path.read_bytes() for path in auth_paths}
    for index, path in enumerate(auth_paths):
        path.write_bytes(originals[auth_paths[(index + 1) % len(auth_paths)]])
    return originals


def _restore_codex_accounts(originals: dict[pathlib.Path, bytes]) -> None:
    for path, payload in originals.items():
        path.write_bytes(payload)


def _set_offline_config(env: dict[str, str], binary: pathlib.Path) -> None:
    subprocess.run(
        [str(binary), "config", "set", "update.check.enabled", "false"],
        env=env, stdout=subprocess.DEVNULL, check=True,
    )


def _initialize_production_clone(
    binary: pathlib.Path, env: dict[str, str], data_dir: pathlib.Path,
    fixture_fingerprint: str,
) -> None:
    """Rebuild relocated derived stores while preserving retention due state."""
    conversations = data_dir / "conversations.db"
    marker = None
    marker_present = False
    with contextlib.closing(sqlite3.connect(conversations)) as conn:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            ("conversation_retention_last_prune_at",),
        ).fetchone()
        if row is not None:
            marker_present = True
            marker = row[0]

    # A copied store keeps its source build's stats epoch. The candidate's
    # first stats read would detach the product's epoch rebuild and refuse
    # with "retry shortly", so run the manual product form in the foreground
    # before anything reads the index.
    try:
        stats = subprocess.run(
            [str(binary), "db", "rebuild", "--db", "stats", "--json"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=STATS_INDEX_REBUILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "stats index rebuild exceeded its setup bound") from exc
    if stats.returncode != 0:
        raise RuntimeError(
            "production clone stats index rebuild failed: "
            + (stats.stderr or stats.stdout)[-2000:])

    result = subprocess.run(
        [str(binary), "cache-sync", "--rebuild", "--source", "all"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "production clone cache rebuild failed: " + result.stderr[-2000:])

    with contextlib.closing(sqlite3.connect(conversations)) as conn:
        if marker_present:
            conn.execute(
                "INSERT INTO cache_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("conversation_retention_last_prune_at", marker),
            )
        else:
            conn.execute(
                "DELETE FROM cache_meta WHERE key=?",
                ("conversation_retention_last_prune_at",),
            )
        conn.commit()
    marker = {
        "schemaVersion": 1,
        "root": str(data_dir.parent.resolve()),
        "fixtureSourceFingerprint": fixture_fingerprint,
    }
    (data_dir.parent / ".cctally-soak-normalized.json").write_text(
        json.dumps(marker, sort_keys=True) + "\n")


def _initialize_deferred_retention_clone(
    binary: pathlib.Path, env: dict[str, str], data_dir: pathlib.Path,
    fixture_fingerprint: str,
) -> None:
    """Keep the copied corpus intact until the recorded retention prepass.

    A from-zero cache rebuild forces transcript pruning even when the daily
    throttle is fresh. Disable retention only around that rebuild, then
    restore the clone's exact configuration before the dashboard starts.
    """
    config_path = data_dir / "config.json"
    original_config = config_path.read_bytes()
    config = _strict_json(original_config)
    if not isinstance(config, dict):
        raise ValueError("copied fixture config is not an object")
    conversation = config.setdefault("conversation", {})
    if not isinstance(conversation, dict):
        raise ValueError("copied fixture conversation config is not an object")
    conversation["retention_days"] = 0
    with contextlib.closing(sqlite3.connect(data_dir / "conversations.db")) as conn:
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("conversation_retention_last_prune_at",
             dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        conn.commit()
    config_path.write_text(json.dumps(config, sort_keys=True) + "\n")
    try:
        _initialize_production_clone(binary, env, data_dir, fixture_fingerprint)
    finally:
        config_path.write_bytes(original_config)


def _vacuum_small_fixture_rebuild_freelist(
    binary: pathlib.Path, env: dict[str, str], data_dir: pathlib.Path,
) -> None:
    """Drain pages freed by small-fixture normalization before its probes."""
    try:
        _require_no_reclaim_backlog(data_dir)
        return
    except ValueError as exc:
        if "freelist bytes" not in str(exc):
            raise
    result = subprocess.run(
        [str(binary), "db", "vacuum", "--db", "conversations"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "small fixture rebuild-page vacuum failed: " + result.stderr[-2000:])
    _require_no_reclaim_backlog(data_dir)


def _prepared_clone_matches(
    root: pathlib.Path, fixture_fingerprint: str,
) -> bool:
    try:
        marker = json.loads(
            (root / ".cctally-soak-normalized.json").read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(marker, dict):
        return False
    base_marker = {
            "schemaVersion": 1,
            "root": str(root.resolve()),
            "fixtureSourceFingerprint": fixture_fingerprint,
        }
    prior_root = marker.get("retainedPriorSourceRoot")
    prior_digest = marker.get("retainedPriorSourceSha256")
    if prior_root is not None or prior_digest is not None:
        if (not isinstance(prior_root, str) or
                not isinstance(prior_digest, str) or
                len(prior_digest) != 64 or
                not pathlib.Path(prior_root).is_absolute() or
                pathlib.Path(prior_root).is_symlink() or
                pathlib.Path(prior_root).resolve() == root.resolve()):
            return False
        base_marker["retainedPriorSourceRoot"] = prior_root
        base_marker["retainedPriorSourceSha256"] = prior_digest
    structure_matches = (
        marker == base_marker
        and (root / "data" / "cache.db").is_file()
        and (root / "data" / "conversations.db").is_file()
        and (root / "claude" / "projects").is_dir()
        and bool(list(root.glob("codex-*/sessions")))
    )
    if not structure_matches:
        return False
    try:
        if prior_root is not None and _source_tree_digest(
            pathlib.Path(prior_root)) != prior_digest:
            return False
        return bool(_prepared_source_cursor_evidence(root))
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return False


def _prepared_source_cursor_evidence(root: pathlib.Path) -> dict:
    """Require both derived stores to describe the current source generation."""
    claude = sorted((root / "claude" / "projects").rglob("*.jsonl"))
    codex = sorted(root.glob("codex-*/sessions/**/*.jsonl"))
    if not claude or not codex:
        raise ValueError("prepared clone lacks a provider source population")
    expected = {}
    for provider, paths in (("claude", claude), ("codex", codex)):
        entries = {}
        for path in paths:
            if not path.is_file() or path.is_symlink():
                raise ValueError("prepared source is missing or symlinked")
            entries[str(path)] = path.stat()
        expected[provider] = entries
    marker = _strict_json((root / ".cctally-soak-normalized.json").read_bytes())
    prior_root_text = marker.get("retainedPriorSourceRoot")
    prior_expected = {}
    if prior_root_text is not None:
        prior_root = pathlib.Path(prior_root_text)
        if (not prior_root.is_absolute() or prior_root.is_symlink() or
                not prior_root.is_dir() or
                not isinstance(marker.get("retainedPriorSourceSha256"), str)):
            raise ValueError("retained prior source provenance changed")
        for path in claude:
            old = prior_root / path.relative_to(root)
            if old.is_symlink() or not old.is_file():
                raise ValueError("retained prior Claude source is missing")
            prior_expected[str(old)] = old.stat()
    rails = (
        ("cache.db", "session_files", "claude", False, False),
        ("cache.db", "codex_session_files", "codex", True, True),
        ("conversations.db", "conversation_source_files", "claude", True, False),
        ("conversations.db", "codex_conversation_source_files", "codex", True, False),
    )
    evidence = {}
    for db_name, table, provider, has_inode, has_complete in rails:
        columns = "path,size_bytes,mtime_ns,last_byte_offset"
        if has_inode:
            columns += ",inode"
        if has_complete:
            columns += ",ingest_complete"
        uri = (root / "data" / db_name).resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            rows = conn.execute(f"SELECT {columns} FROM {table}").fetchall()
        if len(rows) < len(expected[provider]):
            raise ValueError(f"{table} source population is incomplete")
        seen = set()
        retained_prior = 0
        for row in rows:
            path, size, mtime, offset = row[:4]
            st = expected[provider].get(path)
            if st is None and table == "conversation_source_files":
                st = prior_expected.get(path)
                if st is not None:
                    retained_prior += 1
            if (st is None or path in seen or size != st.st_size or
                    mtime != st.st_mtime_ns or offset != st.st_size):
                raise ValueError(f"{table} cursor identity or offset mismatches source")
            seen.add(path)
            if has_inode and row[4] != st.st_ino:
                raise ValueError(f"{table} inode mismatches source")
            if has_complete and row[5] != 1:
                raise ValueError(f"{table} ingest is incomplete")
        if not set(expected[provider]).issubset(seen):
            raise ValueError(f"{table} source population is incomplete")
        if len(rows) != len(expected[provider]) + retained_prior:
            raise ValueError(f"{table} source population has unexpected rows")
        evidence[table] = len(expected[provider])
        if retained_prior:
            evidence["retainedPriorClaudeConversationSources"] = retained_prior
    uri = (root / "data" / "cache.db").resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        markers = dict(conn.execute(
            "SELECT key,value FROM cache_meta WHERE key IN "
            "('claude_ingest_walk_complete',"
            "'dashboard_codex_full_walk_complete')"))
    claude_marker = markers.get("claude_ingest_walk_complete")
    try:
        claude_finished = dt.datetime.fromisoformat(str(claude_marker))
    except ValueError as exc:
        raise ValueError("prepared clone lacks caught-up cache markers") from exc
    if (set(markers) != {"claude_ingest_walk_complete",
                         "dashboard_codex_full_walk_complete"}
            or claude_finished.tzinfo is None
            or markers["dashboard_codex_full_walk_complete"] != "1"):
        raise ValueError("prepared clone lacks caught-up cache markers")
    evidence["cacheMarkers"] = markers
    return evidence


def run_soak(args) -> dict:
    checkout = pathlib.Path(args.checkout or REPO).expanduser().resolve()
    binary = checkout / "bin" / "cctally"
    if not binary.is_file():
        raise ValueError(f"checkout has no bin/cctally: {checkout}")
    root = pathlib.Path(args.root).expanduser().resolve()
    fixture_source = getattr(args, "fixture_copy", None)
    source_fingerprint = (
        _fixture_source_fingerprint(fixture_source) if fixture_source else None)
    reuse_prepared = bool(getattr(args, "reuse_prepared_root", False))
    if reuse_prepared:
        if not fixture_source or not _prepared_clone_matches(
            root, source_fingerprint
        ):
            raise ValueError("prepared production clone is missing or mismatched")
        fixture_kind = "isolatedProductionCopy"
    else:
        fixture_kind = _prepare_fixture(
            root, fixture_source, args.scale, args.seed)
    if fixture_source and _fixture_source_fingerprint(fixture_source) != source_fingerprint:
        raise RuntimeError("operator fixture copy changed during materialization")
    env = _fixture_env(root, production_shaped=fixture_source is not None)
    _set_offline_config(env, binary)
    if fixture_source is not None:
        _prepare_isolated_frontier_hooks(binary, root)
    data_dir = root / "data"
    source_conversation_store_bytes = (data_dir / "conversations.db").stat().st_size
    if fixture_source is not None and not reuse_prepared:
        if getattr(args, "defer_retention_for_evidence", False):
            _initialize_deferred_retention_clone(
                binary, env, data_dir, source_fingerprint)
        else:
            _initialize_production_clone(
                binary, env, data_dir, source_fingerprint)
            _make_retention_due(data_dir, dt.datetime.now(dt.timezone.utc))
    retention = _retention_state(data_dir, dt.datetime.now(dt.timezone.utc))
    sqlite_before = _sqlite_settings(data_dir)
    log_path = root / "dashboard-soak.log"
    log = log_path.open("w+", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(binary), "dashboard", "--port", "0",
         "--host", "127.0.0.1", "--no-browser", "--sync-interval",
         str(args.sync_interval), "--tz", "Etc/UTC"],
        stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1, env=env,
    )
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None
    selector.register(proc.stdout, selectors.EVENT_READ)
    port = None
    startup = []
    deadline = time.monotonic() + 180
    while port is None and time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        for key, _mask in selector.select(timeout=0.2):
            line = key.fileobj.readline()
            if not line:
                continue
            startup.append(line.rstrip())
            match = re.search(r"localhost:(\d+)", line)
            if match:
                port = int(match.group(1))
                break
    selector.close()
    if port is None:
        log.seek(0)
        raise RuntimeError(
            f"dashboard did not bind: stdout={startup!r} stderr={log.read()!r}")

    initial_debug, initial_ready_latency = _wait_for_initial_sync(port)
    diagnosis_503_count, initial_diagnosis_latency = _wait_for_diagnosis(port)
    injected_503_count, recovery_latency = _exercise_diagnosis_recovery(
        port, data_dir)
    diagnosis_503_count += injected_503_count

    samples = []
    api_latency = [
        initial_ready_latency, *initial_diagnosis_latency, *recovery_latency,
    ]
    reconnect_latency = []
    rail_latency = []
    live_tail_latency = []
    reader_latency = []
    diagnostics = [initial_debug]
    created: list[pathlib.Path] = []
    auth_originals: dict[pathlib.Path, bytes] = {}
    added = rebuilt = rebuild_executed = False
    rebuild_due = False
    rebuild_recovery = None
    mutation_started: float | None = None
    mutation_from_tick_seq: int | None = None
    mutation_to_render_ms: float | None = None
    memory_admission_measured = False
    memory_admission_targets: dict[str, int] = {}
    memory_admission_observed: dict[str, int] = {}
    initial_tick_seq = int(
        (initial_debug.get("tick") or {}).get("tick_seq", 0))
    queued_status, queued_body, queued_response_latency = _post(
        port, "/api/sync?refresh=0&queue=1")
    manual_queued_latency = _settle_manual_refresh(
        port, queued_status, queued_body, queued_response_latency,
        initial_debug)
    manual_status = None
    manual_refresh_latency: list[float] = []
    for _attempt in range(30):
        debug_status, debug_raw, _ = _request(port, "/api/debug/backend")
        if debug_status != 200:
            raise RuntimeError("debug endpoint unavailable before manual refresh")
        debug = json.loads(debug_raw)
        status, body, response_latency = _post(port, "/api/sync?refresh=0")
        settled_latency = _settle_manual_refresh(
            port, status, body, response_latency, debug)
        manual_status = status
        if status == 204:
            manual_refresh_latency.append(settled_latency)
            if len(manual_refresh_latency) == 21:
                break
    if len(manual_refresh_latency) < 21:
        raise RuntimeError("manual refresh did not produce 21 synchronous samples")
    large_reader_exercised = False
    quiet_window = None
    try:
        quiet_window = _measure_quiet_window(
            port, proc, getattr(args, "quiet_seconds", 180.0))
        started = time.monotonic()
        while time.monotonic() - started < args.duration_seconds:
            elapsed = time.monotonic() - started
            if not added and elapsed >= args.duration_seconds / 3:
                mutation_started = time.monotonic()
                mutation_from_tick_seq = int(
                    samples[-1]["tickSeq"] if samples else initial_tick_seq)
                created = _copy_stress_sources(root)
                auth_originals = _rotate_codex_accounts(root)
                added = True
            if not rebuilt and elapsed >= args.duration_seconds * 2 / 3:
                _cleanup_stress_sources(created)
                created = []
                _restore_codex_accounts(auth_originals)
                auth_originals = {}
                if not getattr(args, "skip_rebuild_stress", False):
                    rebuild_due = True
                rebuilt = True

            for host_header in ("127.0.0.1", "invalid.example"):
                status, _body, latency = _request(
                    port, "/api/data", host_header=host_header)
                if status != 200:
                    raise RuntimeError(f"/api/data answered {status}")
                api_latency.append(latency)
                reconnect_latency.append(
                    _sse_reconnect(port, host_header=host_header))
            status, body, latency = _request(
                port, "/api/conversations?limit=25")
            if status not in (200, 404):
                raise RuntimeError(
                    f"/api/conversations answered {status}: {body[:400]!r}")
            api_latency.append(latency)
            rail_latency.append(latency)
            if not large_reader_exercised and status == 200:
                browse = json.loads(body)
                conversations = browse.get("conversations") or []
                if conversations:
                    session_id = str(conversations[0].get("session_id") or "")
                    if session_id:
                        encoded = urllib.parse.quote(session_id, safe="")
                        live_tail_latency.append(_sse_reconnect(
                            port, path=f"/api/conversation/{encoded}/events"))
                        for suffix in ("?limit=500", "/outline"):
                            status, detail, detail_latency = _request(
                                port, f"/api/conversation/{encoded}{suffix}")
                            if status != 200:
                                raise RuntimeError(
                                    f"large reader {suffix} answered {status}: "
                                    f"{detail[:400]!r}")
                            reader_latency.append(detail_latency)
                        large_reader_exercised = True
            status, body, latency = _request(port, DIAGNOSIS_PATH)
            if status == 503:
                diagnosis_503_count += 1
            elif status != 200:
                raise RuntimeError(
                    f"{DIAGNOSIS_PATH} answered {status}: {body[:400]!r}")
            api_latency.append(latency)

            status, raw, latency = _request(port, "/api/debug/backend")
            if status != 200:
                raise RuntimeError(f"debug endpoint answered {status}")
            api_latency.append(latency)
            diagnostic = json.loads(raw)
            diagnostics.append(diagnostic)
            process_sample = _process_sample(proc.pid)
            memory = diagnostic.get("memory") or {}
            latest_tick = ((diagnostic.get("tick") or {}).get("records") or [])
            if (
                mutation_to_render_ms is None
                and mutation_started is not None
                and latest_tick
                and int(latest_tick[-1].get("seq") or -1)
                    > int(mutation_from_tick_seq or -1)
                and latest_tick[-1].get("dispatch") == "full"
            ):
                mutation_to_render_ms = (
                    time.monotonic() - mutation_started) * 1000
            samples.append({
                "elapsedSeconds": round(elapsed, 3),
                **process_sample,
                "diskBytes": _disk_bytes(data_dir),
                "ownerEstimatedBytes": int(memory.get("ownerEstimatedBytes", 0)),
                "ownerCeilingBytes": int(memory.get("ownerCeilingBytes", 0)),
                "tickSeq": int((diagnostic.get("tick") or {}).get("tick_seq", 0)),
                "dispatch": latest_tick[-1].get("dispatch") if latest_tick else None,
            })
            remaining = args.sample_seconds
            while remaining > 0 and proc.poll() is None:
                chunk = min(0.25, remaining)
                time.sleep(chunk)
                remaining -= chunk
            if proc.poll() is not None:
                raise RuntimeError(f"dashboard exited early with {proc.returncode}")
        final_transient, final_latency = _wait_for_diagnosis(port, timeout=60)
        diagnosis_503_count += final_transient
        api_latency.extend(final_latency)
        # The source aggregate is verified off the publisher thread. Wait for
        # the latest stress generation so a zero/stale counter cannot pass as
        # memory evidence. Baseline checkouts predate this field and are not
        # held to a contract they do not implement.
        # The two deep measurements are deliberately serialized and each is
        # capped at 25% of one core. Give the second owner enough time to prove
        # the post-stress generation without trading that CPU bound for speed.
        admission_deadline = time.monotonic() + 120
        while time.monotonic() < admission_deadline:
            status, raw, _latency = _request(port, "/api/debug/backend")
            if status != 200:
                raise RuntimeError("debug endpoint unavailable during memory settle")
            diagnostic = json.loads(raw)
            owners = (diagnostic.get("memory") or {}).get("owners") or {}
            measured_owners = {
                name: owners[name]
                for name in (
                    "codexSourceAccelerators", "snapshotAccelerators")
                if name in owners
            }
            if not measured_owners:
                memory_admission_measured = True
                break
            if not memory_admission_targets:
                memory_admission_targets = {
                    name: int(owner.get("currentGeneration", 0))
                    for name, owner in measured_owners.items()
                }
            for name, owner in measured_owners.items():
                measured_generation = int(owner.get("measuredGeneration", -1))
                if (
                    int(owner.get("estimatedBytes", 0) or 0) > 0
                    and measured_generation >= memory_admission_targets[name]
                    and int(owner.get("measurementErrorCount", 0) or 0) == 0
                ):
                    memory_admission_observed[name] = measured_generation
            diagnostics.append(diagnostic)
            if set(memory_admission_observed) == set(memory_admission_targets):
                memory_admission_measured = True
                break
            time.sleep(0.25)
    finally:
        _cleanup_stress_sources(created)
        _restore_codex_accounts(auth_originals)
        if proc.poll() is None:
            proc.terminate()
        shutdown_started = time.monotonic()
        try:
            proc.wait(timeout=15)
            clean = proc.returncode == 0
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            clean = False
        shutdown_seconds = time.monotonic() - shutdown_started
        log.close()

    # Transcript recovery deliberately refuses to replace conversations.db
    # while any process has the DB family open. Finish the live measurement,
    # stop its dashboard, then rebuild and prove a new dashboard can publish.
    if rebuild_due:
        result = subprocess.run(
            [str(binary), "cache-sync", "--rebuild", "--source", "all"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "post-dashboard cache rebuild failed: "
                + result.stderr[-2000:])
        with _live_regime_dashboard(
            root, binary, env, args.sync_interval,
            diagnostic_path=root / "post-rebuild-initial-sync-diagnostics.json",
            diagnostic_phase="post-rebuild",
        ) as recovered:
            status, _body, latency = _request(recovered.port, "/api/data")
            if status != 200:
                raise RuntimeError(
                    f"post-rebuild dashboard /api/data answered {status}")
            rebuild_recovery = {"apiStatus": status, "apiLatencyMs": latency}
        rebuild_executed = True

    latest = diagnostics[-1] if diagnostics else {}
    tick = latest.get("tick") or {}
    retention["ran"] = any(
        row.get("phase") == "delete" and row.get("outcome") == "ok"
        for diagnostic in diagnostics
        for row in (diagnostic.get("tick") or {}).get("maintenance", ())
    )
    main_duty = _cpu_duty(tick.get("records"))
    conversation_duty = _cpu_duty(tick.get("conversation_sync"))
    combined_samples = [
        duty for duty in (_combined_cpu_duty(row) for row in diagnostics)
        if duty is not None
    ]
    combined = max(combined_samples) if combined_samples else None
    owners = _peak_memory_owners(diagnostics)
    tick_measurements = _tick_measurements(samples, diagnostics)
    warm = samples[len(samples) // 2:]
    disk_io_duty = None
    if len(warm) >= 2:
        elapsed_span = float(warm[-1]["elapsedSeconds"]) - float(
            warm[0]["elapsedSeconds"])
        if elapsed_span > 0:
            first_ops = int(warm[0]["diskReadOps"]) + int(warm[0]["diskWriteOps"])
            last_ops = int(warm[-1]["diskReadOps"]) + int(warm[-1]["diskWriteOps"])
            disk_io_duty = max(0, last_ops - first_ops) / elapsed_span
    receipt = {
        "schemaVersion": 1,
        "checkout": str(checkout),
        "measurementHost": socket.gethostname(),
        "checkoutRef": getattr(args, "checkout_label", None),
        "scale": args.scale,
        "fixtureKind": fixture_kind,
        "fixtureSourceFingerprint": source_fingerprint,
        "conversationStoreBytes": source_conversation_store_bytes,
        "retention": retention,
        "frontierPolicy": {
            "expirySeconds": _frontier_expiry_from_checkout(checkout),
            "trustEnabled": False,  # requires separate hook-effectiveness receipt
        },
        "seed": args.seed,
        "durationSeconds": args.duration_seconds,
        "sampleSeconds": args.sample_seconds,
        "ceilings": {
            "processRssBytes": PROCESS_CEILING_BYTES,
            "rssSlopeBytesPerSecond": RSS_SLOPE_CEILING_BYTES_PER_SECOND,
            "combinedCpuDuty": COMBINED_CPU_DUTY_CEILING,
            "processCpuPercent": PROCESS_CPU_PERCENT_CEILING,
            "apiP95Ms": API_P95_CEILING_MS,
            "threadCount": THREAD_COUNT_CEILING,
            "diskIoOpsPerSecond": DISK_IO_OPS_PER_SECOND_CEILING,
        },
        "samples": samples,
        "graphs": {
            "rss": sparkline(row["rssBytes"] for row in samples),
            "ownerBytes": sparkline(row["ownerEstimatedBytes"] for row in samples),
            "cpu": sparkline(row["cpuPercent"] for row in samples),
            "disk": sparkline(row["diskBytes"] for row in samples),
        },
        "postWarmupRssSlopeBytesPerSecond": linear_slope(
            warm, "elapsedSeconds", "rssBytes"),
        "postWarmupRssSlopeConfidence": linear_slope_confidence_bound(
            warm, "elapsedSeconds", "rssBytes"),
        "owners": owners,
        "memoryAdmission": {
            "targetGenerations": memory_admission_targets,
            "measuredGenerations": memory_admission_observed,
        },
        "mainCpuDuty": main_duty,
        "conversationCpuDuty": conversation_duty,
        "combinedCpuDuty": combined,
        "diskIoOpsPerSecond": disk_io_duty,
        "publishPeriodsNs": [
            row.get("period_ns") for row in tick.get("records", ())
            if row.get("period_ns") is not None
        ],
        "conversationPeriodsNs": [
            row.get("period_ns") for row in tick.get("conversation_sync", ())
            if row.get("period_ns") is not None
        ],
        "apiLatencyMs": api_latency,
        **tick_measurements,
        "quietWindow": quiet_window,
        "idleCpuPercent": (quiet_window or {}).get("cpuPercent"),
        "mutationToRenderMs": mutation_to_render_ms,
        "externalEvidenceVerified": False,
        "apiP95Ms": percentile(api_latency, 0.95),
        "reconnectLatencyMs": reconnect_latency,
        "railLatencyMs": rail_latency,
        "liveTailLatencyMs": live_tail_latency,
        "readerLatencyMs": reader_latency,
        "manualRefreshLatencyMs": manual_refresh_latency,
        "queuedRefreshLatencyMs": [manual_queued_latency],
        "sqliteBefore": sqlite_before,
        "sqliteAfter": _sqlite_settings(data_dir),
        "stress": {
            "privacyVariants": True,
            "providerSourceAddRemove": added,
            "accountIdentityRotation": added,
            "diagnosisTransient503Count": diagnosis_503_count,
            "diagnosisRecovered": diagnosis_503_count > 0,
            "cacheRebuild": rebuild_executed,
            "cacheRebuildAfterShutdown": rebuild_executed,
            "cacheRebuildRecovery": rebuild_recovery,
            "bothProvidersConfigured": bool(env["CODEX_HOME"]),
            "largeReader": large_reader_exercised,
            "manualRefresh": (
                len(manual_refresh_latency) == 21 and queued_status == 202),
            "dedicatedLiveTail": bool(live_tail_latency),
            "memoryAdmissionMeasured": memory_admission_measured,
        },
        "shutdown": {
            "clean": clean,
            "seconds": round(shutdown_seconds, 3),
            "rssReleased": proc.poll() is not None,
        },
    }
    receipt["problems"] = evaluate_receipt(receipt)
    return receipt


def _comparison(before: dict, after: dict) -> dict:
    def peak(receipt):
        return max((row["rssBytes"] for row in receipt.get("samples", ())), default=0)
    def metric(receipt, field, q):
        values = receipt.get(field) or []
        if field.endswith("PeriodsNs"):
            values = [float(value) / 1_000_000 for value in values]
        return percentile(values, q)
    result = {
        "beforePeakRssBytes": peak(before),
        "afterPeakRssBytes": peak(after),
        "peakRssDeltaBytes": peak(after) - peak(before),
        "beforeApiP95Ms": before.get("apiP95Ms"),
        "afterApiP95Ms": after.get("apiP95Ms"),
        "beforeCombinedCpuDuty": before.get("combinedCpuDuty"),
        "afterCombinedCpuDuty": after.get("combinedCpuDuty"),
        "beforeGraphs": before.get("graphs"),
        "afterGraphs": after.get("graphs"),
    }
    for label, field in (
        ("Publish", "publishPeriodsNs"),
        ("Conversation", "conversationPeriodsNs"),
        ("Reconnect", "reconnectLatencyMs"),
        ("Rail", "railLatencyMs"),
        ("LiveTail", "liveTailLatencyMs"),
        ("Reader", "readerLatencyMs"),
        ("ManualRefresh", "manualRefreshLatencyMs"),
    ):
        for suffix, q in (("P50Ms", 0.50), ("P95Ms", 0.95)):
            result[f"before{label}{suffix}"] = metric(before, field, q)
            result[f"after{label}{suffix}"] = metric(after, field, q)
    return result


def _comparison_problems(comparison: dict) -> list[str]:
    """Fail closed on a no-slower regression outside measurement resolution."""
    problems = []
    for label in (
        "Publish", "Conversation", "Reconnect", "Rail", "LiveTail",
        "Reader", "ManualRefresh",
    ):
        for suffix in ("P50Ms", "P95Ms"):
            before = comparison.get(f"before{label}{suffix}")
            after = comparison.get(f"after{label}{suffix}")
            if before is None or after is None:
                problems.append(f"{label} {suffix} comparison is unmeasured")
            else:
                before_value = float(before)
                after_value = float(after)
                # Scheduler periods are quantized around their configured
                # cadence; sub-5 ms HTTP differences are runner/socket noise.
                if label == "Publish" and suffix == "P95Ms":
                    # A 120-second receipt has only about two dozen scheduler
                    # periods. Five identical baseline runs spanned 8.30-9.66s
                    # at P95 while their medians stayed within 0.12s. Preserve
                    # the strict 5% median cadence gate, but give this sparse
                    # tail its measured same-machine repeatability.
                    tolerance = max(
                        PUBLISH_P95_TOLERANCE_FLOOR_MS,
                        before_value * PUBLISH_P95_TOLERANCE_PCT,
                    )
                elif label == "Conversation" and suffix == "P95Ms":
                    tolerance = max(
                        CONVERSATION_P95_TOLERANCE_FLOOR_MS,
                        before_value * CONVERSATION_P95_TOLERANCE_PCT,
                    )
                elif label in ("Publish", "Conversation"):
                    tolerance = before_value * 0.05
                else:
                    tolerance = max(
                        HTTP_TOLERANCE_FLOOR_MS, before_value * 0.05)
                if after_value <= before_value + tolerance:
                    continue
                problems.append(
                    f"{label} {suffix} slowed from {before_value:.3f}ms "
                    f"to {after_value:.3f}ms beyond {tolerance:.3f}ms tolerance")
    return problems


def _strict_json(raw: bytes) -> dict:
    def object_without_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key}")
            result[key] = value
        return result

    value = json.loads(
        raw, object_pairs_hook=object_without_duplicates,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"nonfinite JSON constant {value}")),
    )
    if not isinstance(value, dict):
        raise ValueError("evidence JSON must be an object")
    return value


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _evidence_execution(command: list[str], *, cwd: pathlib.Path,
                        env: dict[str, str] | None = None) -> dict:
    started_at = _utc_now()
    started = time.perf_counter()
    completed = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True,
        errors="replace",
    )
    duration_ms = (time.perf_counter() - started) * 1000
    finished_at = _utc_now()
    stdout = completed.stdout[-200_000:]
    stderr = completed.stderr[-200_000:]
    return {
        "startedAt": started_at,
        "finishedAt": finished_at,
        "command": {"argv": command, "exitCode": completed.returncode},
        "execution": {
            "durationMs": duration_ms,
            "stdout": stdout,
            "stderr": stderr,
        },
    }


_ACTIVE_MEASUREMENT_FIELDS = (
    "sampleCount", "processCpuPercent", "combinedCpuDuty", "rssBytes",
    "retainedOwnerBytes", "fullBuildP50Ms", "fullBuildP95Ms", "apiP95Ms",
    "publishP95Ms", "conversationPublishP95Ms", "publishMaxMs",
    "conversationPublishMaxMs", "mutationToRenderMs",
)


def _validate_regime_execution_matrix(probes: dict[str, dict]) -> None:
    """Reject unit-test claims and evidence reused under several regime names."""
    if not isinstance(probes, dict) or set(probes) != set(REQUIRED_REGIMES):
        raise ValueError("regime execution matrix is incomplete")
    run_ids = set()
    active_signatures = set()
    for name in REQUIRED_REGIMES:
        probe = probes[name]
        command = ((probe.get("provenance") or {}).get("command") or {})
        argv = command.get("argv") or []
        if any(pathlib.Path(str(arg)).name in {"pytest", "py.test"}
               for arg in argv):
            raise ValueError(
                f"{name} pytest result is not a production measurement")
        execution = probe.get("execution") or {}
        if "passedCases" in execution:
            raise ValueError(
                f"{name} pytest pass count is not a production measurement")
        metrics = probe.get("measurements") or {}
        run_id = metrics.get("measurementRunId")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError(f"{name} measurementRunId is missing")
        if run_id in run_ids:
            raise ValueError(f"regime measurementRunId {run_id} was reused")
        run_ids.add(run_id)
        _evidence_number(
            metrics, "runtimeObservationCount", 1, integer=True)
        if name in ("claudeActive", "codexActive", "bothActive"):
            # A null bothActive evidence-only field (#862 Task C) is part of
            # the signature like any value; this check applies no ceiling.
            signature = tuple(metrics.get(field)
                              for field in _ACTIVE_MEASUREMENT_FIELDS)
            if signature in active_signatures:
                raise ValueError("active regimes reuse common metrics")
            active_signatures.add(signature)


def _cache_count(data_dir: pathlib.Path, table: str) -> int:
    with contextlib.closing(sqlite3.connect(data_dir / "cache.db")) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _append_provider_event(
    root: pathlib.Path, provider: str, marker: str,
) -> pathlib.Path:
    target, row = _provider_event_row(root, provider, marker)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    return target


def _sync_provider(binary: pathlib.Path, env: dict[str, str], provider: str) -> float:
    started = time.perf_counter()
    completed = subprocess.run(
        [str(binary), "cache-sync", "--source", provider], env=env,
        capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{provider} cache sync failed: {completed.stderr[-2000:]}")
    return (time.perf_counter() - started) * 1000


def _load_candidate_namespace(checkout: pathlib.Path, env: dict[str, str]) -> dict:
    import runpy
    os.environ.clear()
    os.environ.update(env)
    sys.path.insert(0, str(checkout / "bin"))
    return runpy.run_path(str(checkout / "bin" / "cctally"))


@contextlib.contextmanager
def _live_regime_dashboard(
    root: pathlib.Path, binary: pathlib.Path, env: dict[str, str],
    sync_interval: float, *, diagnostic_path: pathlib.Path | None = None,
    diagnostic_phase: str | None = None,
    admit_initial_sync: bool = True,
    reap_family: bool = False,
    final_write_bytes=None,
    log_path: pathlib.Path | None = None,
    on_spawn=None,
):
    """Keep one real isolated dashboard alive for a named regime measurement.

    ``reap_family`` (#901 Q18, the write-budget leg): the dashboard starts in
    its own session; teardown signals it, reads ``final_write_bytes(pid)``
    while it is an unreaped zombie (its terminal write volume), reaps it and
    kills whatever is left of its process group, and leaves the evidence on
    the yielded namespace's ``teardown``."""
    # Preparation v2 passes the arm's evidence root's log (never the data
    # root inside the image) and records the dashboard with the job.
    log_path = root / "regime-dashboard.log" if log_path is None else log_path
    log = log_path.open("w+", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(binary), "dashboard", "--port", "0",
         "--host", "127.0.0.1", "--no-browser", "--sync-interval",
         str(sync_interval), "--tz", "Etc/UTC"],
        stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1, env=env,
        start_new_session=reap_family,
    )
    if on_spawn is not None:
        on_spawn(proc.pid)
    live = types.SimpleNamespace(proc=proc, port=None, teardown=None)
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None
    selector.register(proc.stdout, selectors.EVENT_READ)
    port = None
    startup = []
    deadline = time.monotonic() + 180
    diagnostics = ({"phase": diagnostic_phase, "pid": proc.pid}
                   if diagnostic_path is not None else None)
    try:
        while port is None and time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            for key, _mask in selector.select(timeout=0.2):
                line = key.fileobj.readline()
                if not line:
                    continue
                startup.append(line.rstrip())
                match = re.search(r"localhost:(\d+)", line)
                if match:
                    port = int(match.group(1))
                    break
        if port is None:
            log.seek(0)
            raise RuntimeError(
                "regime dashboard did not bind: "
                f"stdout={startup!r} stderr={log.read()!r}")
        if admit_initial_sync:
            if diagnostics is None:
                _wait_for_initial_sync(port)
            else:
                _wait_for_initial_sync(
                    port, diagnostics=diagnostics, proc=proc,
                    data_dir=root / "data")
        live.port = port
        yield live
    finally:
        try:
            selector.close()
            if reap_family:
                live.teardown = _terminate_family(
                    proc, grace=FAMILY_TERM_GRACE_SECONDS,
                    final_reader=final_write_bytes)
            else:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
            log.close()
        finally:
            if diagnostic_path is not None and diagnostics is not None:
                _write_initial_sync_diagnostics(diagnostic_path, diagnostics)


def _record_regime_activity(
    namespace: dict, root: pathlib.Path, provider: str, source: pathlib.Path,
) -> bool:
    frontier = namespace["_load_sibling"]("_lib_ingest_frontier")
    return bool(frontier.record_activity(root / "data", provider, source))


_PROVIDER_ENTRY_TABLES = {
    "claude": "session_entries", "codex": "codex_session_entries",
}


def _live_debug(port: int) -> tuple[dict, float]:
    status, raw, latency = _request(port, "/api/debug/backend")
    if status != 200:
        raise RuntimeError("regime dashboard debug endpoint unavailable")
    return _strict_json(raw), float(latency)


def _debug_entry_counts(debug: dict, providers: tuple[str, ...]) -> dict:
    dataset = debug.get("dataset") or {}
    counts = {}
    for provider in providers:
        value = dataset.get(_PROVIDER_ENTRY_TABLES[provider])
        if type(value) is not int or value < 0:
            raise RuntimeError(f"{provider} dashboard population is unmeasured")
        counts[provider] = value
    return counts


def _final_full_seq(debug: dict) -> int | None:
    tick = debug.get("tick") or {}
    records = tick.get("records") or []
    if not records:
        return None
    latest = records[-1]
    if latest.get("dispatch") != "full" or latest.get("publication") != "final":
        return None
    seq = latest.get("seq")
    if type(seq) is not int or seq != tick.get("tick_seq"):
        return None
    return seq


def _published_provider_ids(debug: dict, providers: tuple[str, ...]) -> dict[str, int]:
    """Read the cache-entry IDs held by the published provider snapshots.

    Unlike ``dataset`` and ``cache_state.signature``, ``sources`` comes from
    the held source bundle. Its version starts with the provider's max cache
    entry ID; matching that ID to the on-demand signature proves the final
    publication incorporated the observed cache population.
    """
    sources = debug.get("sources") or {}
    signature = (debug.get("cache_state") or {}).get("signature") or {}
    result = {}
    for provider in providers:
        version = (sources.get(provider) or {}).get("data_version")
        field = "max_entry_id" if provider == "claude" else "max_codex_id"
        current_id = signature.get(field)
        if (not isinstance(version, str) or
                not version.startswith(f"{provider}:") or
                type(current_id) is not int or current_id < 0):
            raise RuntimeError(f"{provider} published cache identity is unmeasured")
        try:
            result[provider] = int(version.split(":", 2)[1])
        except (IndexError, ValueError) as exc:
            raise RuntimeError(
                f"{provider} published cache identity is malformed") from exc
    return result


def _publication_holds_mutations(
    debug: dict, providers: tuple[str, ...], counts: dict[str, int],
    before: dict[str, int], initial_tick: int, initial_ids: dict[str, int],
) -> bool:
    """Prove the held snapshot contains the mutations after a full publication.

    The held published snapshot must contain every mutated provider's rows,
    and a final full publication newer than admission must exist.

    The debug dataset is an on-demand cache read. Its count alone cannot prove
    the held dashboard snapshot contains the mutation. The published provider
    version and current cache signature must agree, even if the first poll
    occurs after the dashboard already published the new state. A count that
    rose from a later ingest still in progress leaves the published version
    behind the signature, so that publication is refused.
    """
    if not all(counts[provider] > before[provider] for provider in providers):
        return False
    final_seq = _final_full_seq(debug)
    published = _published_provider_ids(debug, providers)
    signature = (debug.get("cache_state") or {}).get("signature") or {}
    return (final_seq is not None and final_seq > initial_tick and
            all(published[provider] > initial_ids[provider] and
                published[provider] == signature[
                    "max_entry_id" if provider == "claude"
                    else "max_codex_id"] for provider in providers))


def _await_published_mutations(
    port: int, providers: tuple[str, ...], before: dict[str, int],
    initial_tick: int, initial_ids: dict[str, int], started: float, name: str,
) -> tuple[dict[str, int], float, int]:
    """Require a final full publication containing the increased cache IDs."""
    deadline = time.monotonic() + FRESHNESS_CEILING_MS / 1000 + 2
    observations = 0
    while time.monotonic() < deadline:
        debug, _latency = _live_debug(port)
        observations += 1
        counts = _debug_entry_counts(debug, providers)
        if _publication_holds_mutations(
                debug, providers, counts, before, initial_tick, initial_ids):
            deltas = {provider: counts[provider] - before[provider]
                      for provider in providers}
            return deltas, (time.perf_counter() - started) * 1000, observations
        time.sleep(0.05)
    raise RuntimeError(
        f"{name} mutation was not visible in a published dashboard tick")


def _await_warm_publication(port: int, name: str, *,
                            deadline: "float | None" = None) -> None:
    """Admit an active probe only once a warm tick has published.

    The live dashboard's first tick is a cold build, and the product then
    cools down for as long as that build took (#313). On the full-size source
    the cold build took 21.6 s, so a mutation made just after the first
    publication waited 21.6 s for any tick at all (#857 Task B, 2026-09-26):
    that measures the startup build, not the cadence the probe is for. The
    measured interval still starts at the mutation; this wait is setup.
    `deadline` (a monotonic instant) replaces the active-regime timeout when
    the caller owns one admission deadline (the write-budget leg).
    """
    if deadline is None:
        deadline = time.monotonic() + ACTIVE_WARM_ADMISSION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        debug, _latency = _live_debug(port)
        records = (debug.get("tick") or {}).get("records") or []
        latest = records[-1] if records else {}
        if latest.get("publication") == "final" and latest.get("cold") is False:
            return
        time.sleep(0.25)
    raise RuntimeError(f"{name} live dashboard did not publish a warm tick")


def _measure_active_regime(name: str, _receipt: dict, root: pathlib.Path,
                           binary: pathlib.Path, env: dict[str, str],
                           namespace: dict, sync_interval: float) -> dict:
    providers = {
        "claudeActive": ("claude",), "codexActive": ("codex",),
        "bothActive": ("claude", "codex"),
    }[name]
    # Full-size bothActive gates visibility and memory only (operator
    # decision, #857; the moved limits are #862 Task C's). A full-size
    # activity tick takes about 7 s and the duty cap stretches the cadence
    # past 12 s, so it neither abandons at the freshness ceiling nor
    # restimulates for cadence samples: a batch appended near the deadline
    # could not render in time and would read as a missed event.
    narrowed = name == "bothActive"
    with _live_regime_dashboard(root, binary, env, sync_interval) as live:
        _await_warm_publication(live.port, name)
        initial, _latency = _live_debug(live.port)
        initial_tick = int((initial.get("tick") or {}).get("tick_seq", 0))
        initial_conversation = max((int(row.get("started_ns") or 0)
            for row in (initial.get("tick") or {}).get("conversation_sync") or []),
            default=0)
        before = _debug_entry_counts(initial, providers)
        initial_ids = _published_provider_ids(initial, providers)
        started = time.perf_counter()
        appended = dict.fromkeys(providers, 0)
        for provider in providers:
            source = _append_provider_event(
                root, provider, f"{name}-{secrets.token_hex(8)}")
            if not _record_regime_activity(namespace, root, provider, source):
                raise RuntimeError(
                    f"{name} could not publish {provider} activity evidence")
            appended[provider] += 1
        deadline = time.monotonic() + max(30.0, sync_interval * 8 + 5)
        observations = 0
        latency = None
        after = {}
        samples = []
        diagnostics = []
        api_latencies = []
        main_records: dict[int, dict] = {}
        conversation_records: dict[int, dict] = {}
        last_stimulated_seq = initial_tick
        next_process_sample = 0.0
        while time.monotonic() < deadline:
            debug, api_latency = _live_debug(live.port)
            observations += 1
            api_latencies.append(api_latency)
            now = time.monotonic()
            if now >= next_process_sample:
                diagnostics.append(debug)
                samples.append({**_process_sample(live.proc.pid),
                                "apiLatencyMs": api_latency})
                next_process_sample = now + min(1.0, max(0.25, sync_interval / 5))
            tick = debug.get("tick") or {}
            for row in tick.get("records") or []:
                seq = row.get("seq")
                if type(seq) is int and seq > initial_tick:
                    main_records[seq] = row
            for row in tick.get("conversation_sync") or []:
                started_ns = row.get("started_ns")
                if type(started_ns) is int and started_ns > initial_conversation:
                    conversation_records[started_ns] = row
            candidate = _debug_entry_counts(debug, providers)
            # Credit the first final full publication that holds the
            # mutation. The count rises during a tick's ingest, before that
            # tick publishes, so one poll can first see both.
            # Waiting for a later publication then over-reported visibility
            # by one cadence period (#857 Task B).
            if latency is None and _publication_holds_mutations(
                    debug, providers, candidate, before, initial_tick,
                    initial_ids):
                latency = (time.perf_counter() - started) * 1000
                after = candidate
            if (not narrowed and latency is None
                    and (time.perf_counter() - started) * 1000 > (
                        FRESHNESS_CEILING_MS + 2000)):
                break
            final_seq = _final_full_seq(debug)
            if (not narrowed and final_seq is not None
                    and final_seq > last_stimulated_seq):
                last_stimulated_seq = final_seq
                if sum(row.get("dispatch") == "full"
                       and row.get("publication") == "final"
                       for row in main_records.values()) < 4:
                    for provider in providers:
                        source = _append_provider_event(
                            root, provider, f"{name}-{secrets.token_hex(8)}")
                        if not _record_regime_activity(
                            namespace, root, provider, source
                        ):
                            raise RuntimeError(
                                f"{name} could not publish {provider} activity evidence")
            usable_main = [row for row in main_records.values()
                           if _finite_positive_series([row.get("period_ns")])]
            usable_conversation = [row for row in conversation_records.values()
                                   if _finite_positive_series([row.get("period_ns")])]
            full_count = sum(row.get("dispatch") == "full"
                             and row.get("publication") == "final"
                             and _finite_positive_series([row.get("duration_ns")])
                             for row in main_records.values())
            if (latency is not None and len(samples) >= 4
                    and full_count >= 2 and len(usable_main) >= 4
                    and len(usable_conversation) >= 4):
                break
            time.sleep(0.05)
        if latency is None:
            raise RuntimeError(
                f"{name} mutation was not visible in a published dashboard tick")
        full = [row for row in main_records.values()
                if row.get("dispatch") == "full"
                and row.get("publication") == "final"
                and _finite_positive_series([row.get("duration_ns")])]
        publish = [float(row["period_ns"]) / 1_000_000
                   for row in main_records.values()
                   if _finite_positive_series([row.get("period_ns")])]
        conversation = [float(row["period_ns"]) / 1_000_000
                        for row in conversation_records.values()
                        if _finite_positive_series([row.get("period_ns")])]
        if narrowed:
            # sampleCount stays gated; the build and cadence series do not.
            if len(samples) < 4:
                raise RuntimeError(f"{name} live dashboard process samples are incomplete")
        elif (len(samples) < 4 or len(full) < 2 or len(publish) < 4
                or len(conversation) < 4):
            raise RuntimeError(f"{name} live dashboard resource/build/cadence samples are incomplete")
        owners = _peak_memory_owners(diagnostics)
        if not owners:
            raise RuntimeError(f"{name} live dashboard retained owners are unmeasured")
        main_duty = _cpu_duty(main_records.values())
        conversation_duty = _cpu_duty(conversation_records.values())
        combined_duty = (None if main_duty is None or conversation_duty is None
                         else main_duty + conversation_duty)
        if combined_duty is None and not narrowed:
            raise RuntimeError(f"{name} live dashboard CPU duty is unmeasured")
        builds = [float(row["duration_ns"]) / 1_000_000 for row in full]
        # The single-provider regimes raised above unless every series is
        # measured; only bothActive's evidence-only fields can be null here.
        metrics = {
            "sampleCount": len(samples),
            "processCpuPercent": max(row["cpuPercent"] for row in samples),
            "combinedCpuDuty": combined_duty,
            "rssBytes": max(row["rssBytes"] for row in samples),
            "retainedOwnerBytes": sum(int(owner.get("estimatedBytes", 0))
                                      for owner in owners.values()),
            # One build is measurable: without restimulation a full-size run
            # usually has exactly one activity build, and later ticks reuse.
            "fullBuildP50Ms": percentile(builds, 0.50),
            "fullBuildP95Ms": percentile(builds, 0.95),
            "apiP95Ms": percentile(api_latencies, 0.95),
            "publishP95Ms": percentile(publish, 0.95),
            "conversationPublishP95Ms": percentile(conversation, 0.95),
            "publishMaxMs": max(publish, default=None),
            "conversationPublishMaxMs": max(conversation, default=None),
            "mutationToRenderMs": latency,
            "liveDiagnostics": {"samples": samples,
                                "mainRecords": list(main_records.values()),
                                "conversationRecords": list(conversation_records.values()),
                                "owners": owners},
        }
    for provider in providers:
        metrics[f"observed{provider.title()}Events"] = after[provider] - before[provider]
    if narrowed:
        for provider in providers:
            metrics[f"appended{provider.title()}Events"] = appended[provider]
        metrics["ungatedFields"] = list(BOTH_ACTIVE_UNGATED_FIELDS)
    metrics["runtimeObservationCount"] = (
        len(samples) + observations +
        sum(after[provider] - before[provider] for provider in providers)
    )
    return metrics


def _measure_frontier_regime(name: str, receipt: dict, root: pathlib.Path,
                             binary: pathlib.Path, env: dict[str, str],
                             namespace: dict, sync_interval: float = 5.0) -> dict:
    frontier = namespace["_load_sibling"]("_lib_ingest_frontier")
    source = sorted((root / "claude" / "projects").rglob("*.jsonl"))[0]
    frontier.record_activity(root / "data", "claude", source)
    _sync_provider(binary, env, "claude")
    state = frontier.DashboardIngestFrontier(root / "data")
    with contextlib.closing(sqlite3.connect(root / "data" / "cache.db")) as conn:
        roots = (source.parent,)
        cutoff = state.capture_cutoff()
        seeded = state.seed_provider(
            "claude", conn, roots=roots, trusted=True, cutoff=cutoff)
        fresh = state.plan_provider("claude", conn, roots=roots)
        if name == "missingHook":
            rejected = not state.seed_provider(
                "claude", conn, roots=roots, trusted=False, cutoff=cutoff)
            stale = state.plan_provider("claude", conn, roots=roots)
        else:
            marker = frontier.activity_marker_path(root / "data")
            with marker.open("ab") as handle:
                handle.write(b"not-json\n")
            stale = state.plan_provider("claude", conn, roots=roots)
            rejected = stale.mode == "full"
    with _live_regime_dashboard(root, binary, env, sync_interval) as live:
        initial, _latency = _live_debug(live.port)
        before = _debug_entry_counts(initial, ("claude",))
        initial_tick = int((initial.get("tick") or {}).get("tick_seq", 0))
        initial_ids = _published_provider_ids(initial, ("claude",))
        started = time.perf_counter()
        # Deliberately do not emit a hook ticket. The dashboard must discover
        # this source change through its live fallback and publish a new tree.
        _append_provider_event(root, "claude", f"{name}-{secrets.token_hex(8)}")
        deltas, latency, observations = _await_published_mutations(
            live.port, ("claude",), before, initial_tick, initial_ids,
            started, name)
    return {
        "frontierExpirySeconds": (receipt.get("frontierPolicy") or {}).get(
            "expirySeconds"),
        "invalidHookRejected": bool(rejected),
        "fallbackRefreshCount": int(stale.mode == "full"),
        "trustedFreshFrontierCount": int(seeded and fresh.mode == "caught_up"),
        "untrustedStaleFrontierCount": int(stale.mode == "full"),
        "observedMutationCount": deltas["claude"],
        "mutationToRenderMs": latency,
        "runtimeObservationCount": 3 + observations + deltas["claude"],
    }


def _restore_frontier_complete_store(binary: pathlib.Path, env: dict[str, str]) -> None:
    """Remove the broad soak's temporary Claude file before frontier seeding.

    The broad preamble copies one JSONL to ``soak-added`` and then removes it.
    A normal sync keeps that shared-session cursor as an orphan and withdraws
    the walk-complete sentinel, so a trusted frontier cannot be seeded. This
    rebuild is setup outside the frontier measurement interval.
    """
    command = [str(binary), "cache-sync", "--source", "claude", "--rebuild"]
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            "frontier preparation Claude rebuild failed: " + result.stderr[-2000:])


def _measure_mutation_race(root: pathlib.Path, binary: pathlib.Path,
                           env: dict[str, str], sync_interval: float = 5.0) -> dict:
    providers = ("claude", "codex")
    with _live_regime_dashboard(root, binary, env, sync_interval) as live:
        initial, _latency = _live_debug(live.port)
        before = _debug_entry_counts(initial, providers)
        initial_tick = int((initial.get("tick") or {}).get("tick_seq", 0))
        initial_ids = _published_provider_ids(initial, providers)
        marker = secrets.token_hex(8)
        started = time.perf_counter()
        _append_provider_event(root, "claude", f"race-claude-{marker}")
        first = subprocess.Popen(
            [str(binary), "cache-sync", "--source", "all"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        _append_provider_event(root, "codex", f"race-codex-{marker}")
        _stdout, stderr = first.communicate()
        if first.returncode != 0:
            raise RuntimeError(f"racing cache sync failed: {stderr[-2000:]}")
        _sync_provider(binary, env, "all")
        deltas, latency, observations = _await_published_mutations(
            live.port, providers, before, initial_tick, initial_ids, started,
            "mutationRace")
    observed = 2
    rendered = sum(deltas.values())
    return {
        "observedMutationCount": observed,
        "renderedMutationCount": rendered,
        "lostUpdateCount": max(0, observed - rendered),
        "mutationToRenderMs": latency,
        "runtimeObservationCount": observed + rendered + observations,
    }


def _measure_pricing_skew(root: pathlib.Path, namespace: dict) -> dict:
    cache = namespace["_load_sibling"]("_cctally_cache")
    with contextlib.closing(cache.open_conversations_db()) as conn:
        rows = conn.execute(
            "SELECT session_id,cost_usd FROM conversation_sessions "
            "WHERE cost_usd IS NOT NULL ORDER BY session_id LIMIT 2"
        ).fetchall()
        if not rows:
            raise RuntimeError("production clone has no priced conversations")
        expected = {str(row[0]): float(row[1]) for row in rows}
        for session_id, cost in expected.items():
            conn.execute(
                "UPDATE conversation_sessions SET cost_usd=? WHERE session_id=?",
                (cost + 1.0, session_id))
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
            (cache.CONVERSATION_ROLLUP_PRICING_FP_KEY, "1900-01-01"))
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
            ("conversation_sessions_backfill_pending", "1"))
        conn.commit()
        started = time.perf_counter()
        cache.sync_claude_conversations(conn)
        duration = (time.perf_counter() - started) * 1000
        actual = dict(conn.execute(
            "SELECT session_id,cost_usd FROM conversation_sessions WHERE "
            f"session_id IN ({','.join('?' for _ in expected)})",
            tuple(expected),
        ))
    errors = [abs(float(actual[key]) - value) for key, value in expected.items()]
    recalculated = sum(error <= 1e-9 for error in errors)
    return {
        "priceMismatchCount": len(expected),
        "recalculatedCount": recalculated,
        "maxCostErrorUsd": max(errors, default=float("inf")),
        "runtimeObservationCount": len(expected) * 2,
        "recalculationDurationMs": duration,
    }


def _measure_degraded_conversation(
    root: pathlib.Path, binary: pathlib.Path, env: dict[str, str],
    sync_interval: float,
) -> dict:
    lock_path = root / "data" / "conversations.db.maintenance.lock"
    lock_path.touch(exist_ok=True)
    degraded = 0
    leaks = 0
    latencies = []
    recovered = 0
    observations = 0
    route = "/api/conversations?limit=25"
    with _live_regime_dashboard(root, binary, env, sync_interval) as live:
        with lock_path.open("r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                status, raw, latency = _request(live.port, route)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        observations += 1
        latencies.append(latency)
        try:
            body = _strict_json(raw)
        except (ValueError, json.JSONDecodeError):
            body = {}
        degraded = int(
            status == 200 and body.get("status") == "degraded"
            and body.get("degraded_reason") == "maintenance")
        rendered = raw.decode("utf-8", errors="replace").lower()
        leaks += int(any(token in rendered for token in (
            "select", "sqlite", str(root).lower())))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status, raw, latency = _request(live.port, route)
            observations += 1
            latencies.append(latency)
            if status == 200:
                try:
                    body = _strict_json(raw)
                except (ValueError, json.JSONDecodeError):
                    body = {}
                if body.get("status") != "degraded":
                    recovered = 1
                    break
            time.sleep(0.05)
    return {
        "degradedCount": degraded, "recoveredCount": recovered,
        "unavailableContentLeaks": leaks,
        "conversationPublishP95Ms": percentile(latencies, 0.95),
        "runtimeObservationCount": observations,
    }


def _measure_request_overload(
    root: pathlib.Path, binary: pathlib.Path, env: dict[str, str],
    sync_interval: float,
) -> dict:
    launched = threading.Barrier(81)
    observations: list[tuple[int, bytes, float]] = []
    latencies = []
    concurrency = {"active": 0, "peak": 0}
    concurrency_lock = threading.Lock()
    observation_lock = threading.Lock()

    with _live_regime_dashboard(root, binary, env, sync_interval) as live:
        def request():
            with concurrency_lock:
                concurrency["active"] += 1
                concurrency["peak"] = max(
                    concurrency["peak"], concurrency["active"])
            try:
                launched.wait(20)
                result = _request(live.port, DIAGNOSIS_PATH)
                with observation_lock:
                    observations.append(result)
                    latencies.append(float(result[2]))
            finally:
                with concurrency_lock:
                    concurrency["active"] -= 1

        process_samples = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=80) as executor:
            futures = [executor.submit(request) for _ in range(80)]
            launched.wait(20)
            process_samples.append(_process_sample(live.proc.pid))
            deadline = time.monotonic() + 30
            while any(not future.done() for future in futures):
                if time.monotonic() >= deadline:
                    raise RuntimeError("HTTP overload burst did not finish")
                process_samples.append(_process_sample(live.proc.pid))
                time.sleep(0.02)
            for future in futures:
                future.result(timeout=1)
        transient, recovery_latencies = _wait_for_diagnosis(
            live.port, timeout=60)
        latencies.extend(recovery_latencies)
        recovered = 1
        process_samples.append(_process_sample(live.proc.pid))
    overload = 0
    unexpected = 0
    for status, raw, _latency in observations:
        try:
            body = _strict_json(raw)
        except (ValueError, json.JSONDecodeError):
            body = {}
        if status == 503 and body.get("code") == "diagnosis_overloaded":
            overload += 1
        elif status != 200:
            unexpected += 1
    return {
        "requestCount": len(observations),
        "maxConcurrentRequests": concurrency["peak"],
        "overloadCount": overload,
        "recoveredCount": recovered,
        "unexpectedErrorCount": unexpected,
        "peakRequestThreads": max(
            (int(row.get("threadCount", 0)) for row in process_samples),
            default=0),
        "peakRssBytes": max(
            (int(row.get("rssBytes", 0)) for row in process_samples), default=0),
        "apiP95Ms": percentile(latencies, 0.95),
        "runtimeObservationCount": (
            len(observations) + len(process_samples) + recovered + transient),
    }


def _idle_reclaim_is_dormant(data_dir: pathlib.Path) -> bool:
    """Whether durable reclaim is intentionally not running.

    A #901 record is dormant exactly when its episode is not eligible: the
    product reclaims at most once a minute and only above 2 GiB and 20% free,
    so retained slack below that is the steady state, not a pending tail. A
    legacy record keeps the pre-#901 rule: positive, progress-making,
    deadline-limited, and below the old escalation threshold. Malformed,
    locked and pending states fail closed.
    """
    path = data_dir / "conversations.db"
    try:
        uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
        with contextlib.closing(
            sqlite3.connect(uri, uri=True, timeout=0.1)
        ) as conn:
            state = _read_reclaim_record(conn)
    except (sqlite3.Error, ValueError):
        return False
    if state is None:
        return True
    try:
        pending, backlog = _reclaim_record_pending(state)
    except ValueError:
        return False
    if state.get("policy_version") is not None:
        return not pending
    return (
        0 < backlog < LEGACY_RECLAIM_ESCALATION_BYTES
        and state.get("made_progress") is True
        and state.get("deadline_hit") is True
    )


def _wait_for_idle_conversation_ready(
    port: int, *, data_dir: pathlib.Path,
    timeout: float = IDLE_READINESS_TIMEOUT_SECONDS,
) -> dict:
    """Wait for a fresh process-local transcript frontier certificate.

    The idle interval must include the scheduled 120-second exhaustive
    revalidation, but it must not inherit a certificate aged by the broad
    soak's diagnosis and manual-refresh preamble.  A caught-up pass proves the
    new dashboard completed its initial full pass and is now inside a fresh
    certificate window.  A pending maintenance tail is settled only when its
    durable reclaim record is the production policy's dormant state: positive,
    progress-making, deadline-limited, and below the threshold that would keep
    it running every cycle.  Observe one later exhaustive revalidation, then
    require two maintenance-stable caught-up passes.  This keeps startup
    convergence and active production reclaim outside the interval called
    idle, while the 180-second interval still crosses the next unchanged
    120-second frontier expiry.  The three-hour setup bound covers the measured
    production-clone deletion, reclaim-to-dormancy, and subsequent revalidation
    sequence; it changes no measured duration or ceiling.
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    candidate: tuple[int, int] | None = None
    settled_caught_up = False
    revalidated_maintenance_seq: int | None = None
    while time.monotonic() < deadline:
        status, raw, _latency = _request(port, "/api/debug/backend")
        if status == 200:
            last = _strict_json(raw)
            tick = last.get("tick") or {}
            records = tick.get("conversation_sync") or ()
            latest = records[-1] if records else {}
            caught_up = (
                latest.get("status") == "ok"
                and latest.get("claude_mode") == "caught_up"
                and latest.get("codex_mode") == "caught_up"
            )
            full = (
                latest.get("status") == "ok"
                and latest.get("claude_mode") == "full"
                and latest.get("codex_mode") == "full"
            )
            maintenance = tick.get("maintenance") or ()
            latest_maintenance_by_phase = {
                str(row.get("phase") or ""): row for row in maintenance
            }
            maintenance_seq = int(
                (maintenance[-1] if maintenance else {}).get("seq") or 0)
            maintenance_settled = not any(
                bool(row.get("pending"))
                for row in latest_maintenance_by_phase.values()
            ) or _idle_reclaim_is_dormant(data_dir)
            if not maintenance_settled:
                candidate = None
                settled_caught_up = False
                revalidated_maintenance_seq = None
            elif full and settled_caught_up:
                candidate = None
                revalidated_maintenance_seq = maintenance_seq
            elif caught_up:
                if not settled_caught_up:
                    settled_caught_up = True
                    candidate = None
                elif revalidated_maintenance_seq != maintenance_seq:
                    candidate = None
                    revalidated_maintenance_seq = None
                else:
                    current = (int(latest.get("seq") or 0), maintenance_seq)
                    if (
                        candidate is not None
                        and current[0] > candidate[0]
                        and current[1] == candidate[1]
                    ):
                        return last
                    if candidate is None or current[0] > candidate[0]:
                        candidate = current
            else:
                candidate = None
        time.sleep(0.5)
    records = (last.get("tick") or {}).get("conversation_sync") or ()
    raise RuntimeError(
        "idle conversation frontier did not become caught up: "
        f"latest={records[-1] if records else None!r}")


def _measure_idle_regime(args) -> dict:
    """Settle maintenance, then measure idle from a fresh dashboard process."""
    checkout = pathlib.Path(args.checkout or REPO).expanduser().resolve()
    root = pathlib.Path(args.root).expanduser().resolve()
    binary = checkout / "bin" / "cctally"
    env = _fixture_env(root, production_shaped=True)
    diagnostic_path = getattr(args, "diagnostic_output", None)
    with _live_regime_dashboard(
        root, binary, env, args.sync_interval,
        diagnostic_path=diagnostic_path, diagnostic_phase="settle",
    ) as live:
        _wait_for_idle_conversation_ready(
            live.port, data_dir=root / "data")
    # Due retention over a multi-GB clone can keep this process alive for
    # hours and leave deferred interpreter/allocator work behind after the
    # durable store has already converged.  That tail is part of maintenance,
    # not true idle.  Restart, then independently prove a fresh process-local
    # frontier certificate before opening the request-free measurement span.
    with _live_regime_dashboard(
        root, binary, env, args.sync_interval,
        diagnostic_path=diagnostic_path, diagnostic_phase="measure",
    ) as live:
        _wait_for_idle_conversation_ready(
            live.port, data_dir=root / "data")
        return _measure_quiet_window(live.port, live.proc, args.quiet_seconds)


def _execute_regime(args, name: str) -> dict:
    namespace = None
    root = pathlib.Path(args.root).expanduser().resolve()
    if name == "idle":
        metrics = _measure_idle_regime(args)
        metrics["runtimeObservationCount"] = int(metrics.get("sampleCount", 0))
    else:
        checkout = pathlib.Path(args.checkout or REPO).expanduser().resolve()
        binary = checkout / "bin" / "cctally"
        run_args = argparse.Namespace(**vars(args))
        run_args.quiet_seconds = 0.01
        run_args.duration_seconds = min(args.duration_seconds, 20.0)
        run_args.skip_rebuild_stress = True
        receipt = run_soak(run_args)
        env = _fixture_env(root, production_shaped=True)
        if name in ("claudeActive", "codexActive", "bothActive"):
            namespace = _load_candidate_namespace(checkout, env)
            metrics = _measure_active_regime(
                name, receipt, root, binary, env, namespace,
                args.sync_interval)
        elif name == "degradedConversation":
            metrics = _measure_degraded_conversation(
                root, binary, env, args.sync_interval)
        elif name == "requestOverload":
            metrics = _measure_request_overload(
                root, binary, env, args.sync_interval)
        else:
            namespace = _load_candidate_namespace(checkout, env)
            if name in ("missingHook", "ineffectiveHook"):
                _restore_frontier_complete_store(binary, env)
                metrics = _measure_frontier_regime(
                    name, receipt, root, binary, env, namespace,
                    args.sync_interval)
            elif name == "mutationRace":
                metrics = _measure_mutation_race(
                    root, binary, env, args.sync_interval)
            elif name == "pricingSkew":
                metrics = _measure_pricing_skew(root, namespace)
            else:
                raise ValueError(f"unknown evidence regime {name}")
    metrics["measurementRunId"] = hashlib.sha256(
        f"{name}\0{root}\0{time.time_ns()}\0{secrets.token_hex(16)}".encode()
    ).hexdigest()
    try:
        _validate_external_metrics("regime", name, metrics)
    except ValueError as exc:
        if name != "idle":
            raise

        def diagnostic_value(value):
            if isinstance(value, float):
                return value if math.isfinite(value) else repr(value)
            if isinstance(value, (type(None), bool, int, str)):
                return value
            if isinstance(value, dict):
                return {
                    str(key): diagnostic_value(item)
                    for key, item in value.items()
                }
            if isinstance(value, (list, tuple)):
                return [diagnostic_value(item) for item in value]
            return repr(value)

        diagnostic = {}
        for field in (
            "cpuPercent", "cpuSeconds", "durationSeconds", "quiet",
            "tickCount", "conversationPassCount", "sampleCount", "profile",
            "readings",
        ):
            diagnostic[field] = diagnostic_value(metrics.get(field))
        raise ValueError(
            f"{exc}; rejected idle measurements="
            f"{json.dumps(diagnostic, sort_keys=True, allow_nan=False)}"
        ) from exc
    return metrics


def _ensure_prepared_regime_template(args, receipt: dict) -> None:
    if (getattr(args, "_prepared_regime_root", None) is not None and
            getattr(args, "_prepared_regime_template", None) is not None):
        return
    checkout = pathlib.Path(args.checkout or REPO).expanduser().resolve()
    temporary = pathlib.Path(tempfile.mkdtemp(
        prefix="cctally-regime-template-",
        dir=pathlib.Path(args.output).parent,
    ))
    regime_root = temporary / "root"
    template = temporary / "template"
    try:
        _prepare_fixture(regime_root, args.fixture_copy, "large", 42)
        env = _fixture_env(regime_root, production_shaped=True)
        binary = checkout / "bin" / "cctally"
        _set_offline_config(env, binary)
        _initialize_production_clone(
            binary, env, regime_root / "data",
            receipt["fixtureSourceFingerprint"],
        )
        _require_no_reclaim_backlog(regime_root / "data")
        shutil.copytree(regime_root, template)
    except Exception as exc:
        raise RuntimeError(
            f"regime template setup failed; partial output retained at {temporary}: {exc}"
        ) from exc
    args._prepared_regime_temporary = temporary
    args._prepared_regime_root = regime_root
    args._prepared_regime_template = template


def _replay_template_digest(template: pathlib.Path) -> str:
    """Bind names, bytes, modes, and mtimes of the replay input."""
    digest = hashlib.sha256()
    for parent, directories, files in os.walk(template, followlinks=False):
        base = pathlib.Path(parent)
        for name in sorted([*directories, *files]):
            path = base / name
            relative = path.relative_to(template).as_posix().encode()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"replay template contains a symlink: {path}")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError(f"replay template contains a special file: {path}")
            digest.update(b"D" if stat.S_ISDIR(info.st_mode) else b"F")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(stat.S_IMODE(info.st_mode).to_bytes(4, "big"))
            digest.update(info.st_mtime_ns.to_bytes(8, "big", signed=True))
            if stat.S_ISREG(info.st_mode):
                digest.update(info.st_size.to_bytes(8, "big"))
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
    return digest.hexdigest()


def _stage_regime_template_from_direct(
    args, receipt: dict, candidate_sha: str,
) -> None:
    """Reuse the direct soak's normalized root for every isolated regime."""
    regime_root = pathlib.Path(args.root).expanduser().resolve()
    fingerprint = receipt["fixtureSourceFingerprint"]
    if not _prepared_clone_matches(regime_root, fingerprint):
        raise RuntimeError("direct soak root is not a reusable normalized clone")
    temporary = pathlib.Path(tempfile.mkdtemp(
        prefix="cctally-regime-template-",
        dir=pathlib.Path(args.output).parent,
    ))
    template = temporary / "template"
    try:
        # The direct soak's post-measurement `cache-sync --rebuild` replays
        # every rail and force-prunes it again; drain that before the template
        # is copied, so no regime inherits it.
        checkout = pathlib.Path(getattr(args, "checkout", None) or REPO)
        binary = checkout.expanduser().resolve() / "bin" / "cctally"
        settle = _settle_reclaim_backlog(
            regime_root, binary, _fixture_env(regime_root, production_shaped=True),
            getattr(args, "sync_interval", 5.0),
            deadline=time.monotonic() + RECLAIM_COMPACTION_TIMEOUT_SECONDS
            + RECLAIM_SETTLE_TIMEOUT_SECONDS)
        with (temporary / "direct-reclaim-settle.json").open(
                "x", encoding="utf-8") as handle:
            json.dump(settle, handle, indent=2, sort_keys=True)
            handle.write("\n")
        _require_no_reclaim_backlog(regime_root / "data")
        shutil.copytree(regime_root, template)
        stamp = temporary / "replay-provenance.json"
        provenance = {
            "schemaVersion": 1,
            "candidateSha": candidate_sha,
            "fixtureSourceFingerprint": fingerprint,
            "sourceRoot": str(regime_root),
            "templatePath": str(template.resolve()),
            "contentSha256": _replay_template_digest(template),
        }
        with stamp.open("x", encoding="utf-8") as handle:
            json.dump(provenance, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception as exc:
        raise RuntimeError(
            f"regime template staging failed; partial output retained at {temporary}: {exc}"
        ) from exc
    args._prepared_regime_temporary = temporary
    args._prepared_regime_root = regime_root
    args._prepared_regime_template = template
    args._prepared_regime_stamp = stamp
    args._prepared_regime_candidate_sha = candidate_sha


def _prepare_small_fixture(args, label: str = "small") -> tuple[pathlib.Path, pathlib.Path, str]:
    """Build and normalize a retained small source, separate from the operator copy."""
    base = pathlib.Path(args.output).expanduser().resolve().parent
    source = base / f"{label}-fixture-source"
    root = base / f"{label}-fixture-root"
    if source.exists() or root.exists():
        raise ValueError("small fixture output already exists")
    # The benchmark builder writes fixed example hooks, including paths that
    # refer to its original root. Treat that build as disposable staging; only
    # copy the verified corpus into durable evidence output, excluding those
    # generated hook documents. The soak installs current-root hooks later.
    with tempfile.TemporaryDirectory(prefix="cctally-small-corpus-build-") as scratch:
        staging = pathlib.Path(scratch) / "corpus"
        _build_fixture(staging, "small", args.seed)
        marker = _strict_json((staging / "data" / ".bench-fixture.json").read_bytes())
        if marker.get("scale") != "small" or marker.get("seed") != args.seed:
            raise ValueError("small fixture builder marker mismatch")
        config_path = staging / "data" / "config.json"
        config = _strict_json(config_path.read_bytes())
        if config.get("conversation") != {"retention_days": 0}:
            raise ValueError("small fixture builder retention config mismatch")
        config["conversation"]["retention_days"] = 90
        config_path.write_text(json.dumps(config, sort_keys=True) + "\n")

        def ignore_generated_hooks(parent, names):
            relative = pathlib.Path(parent).relative_to(staging)
            if relative == pathlib.Path("home/.claude"):
                return {"settings.json"} & set(names)
            if relative.parent == pathlib.Path(".") and relative.name.startswith("codex-"):
                return {"hooks.json", "config.toml"} & set(names)
            return set()

        shutil.copytree(staging, source, ignore=ignore_generated_hooks)
    fingerprint = _fixture_source_fingerprint(source)
    _prepare_fixture(root, source, "small", args.seed)
    checkout = pathlib.Path(args.checkout or REPO).expanduser().resolve()
    binary = checkout / "bin" / "cctally"
    env = _fixture_env(root, production_shaped=True)
    _set_offline_config(env, binary)
    _initialize_deferred_retention_clone(
        binary, env, root / "data", fingerprint)
    _vacuum_small_fixture_rebuild_freelist(binary, env, root / "data")
    if not _prepared_clone_matches(root, fingerprint):
        raise RuntimeError("small fixture normalization is incomplete")
    _require_no_reclaim_backlog(root / "data")
    if _fixture_source_fingerprint(source) != fingerprint:
        raise RuntimeError("small fixture source changed during normalization")
    return source, root, fingerprint


def _source_tree_digest(root: pathlib.Path) -> str:
    """Prove source bytes, names and metadata across a copied generation."""
    digest = hashlib.sha256()
    paths = sorted([
        *(root / "claude" / "projects").rglob("*.jsonl"),
        *root.glob("codex-*/sessions/**/*.jsonl"),
    ], key=lambda path: path.relative_to(root).as_posix())
    if not paths:
        raise ValueError("source tree is empty")
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError("source tree contains a missing or linked file")
        st = path.stat()
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(st.st_size.to_bytes(8, "big"))
        digest.update(st.st_mtime_ns.to_bytes(8, "big", signed=True))
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        after = path.stat()
        if (after.st_ino, after.st_size, after.st_mtime_ns) != (
            st.st_ino, st.st_size, st.st_mtime_ns
        ):
            raise RuntimeError(f"source changed while hashing: {relative!r}")
    return digest.hexdigest()


def _prepare_regime_clone(
    args, name: str, root: pathlib.Path, template: pathlib.Path,
    checkout: pathlib.Path, fingerprint: str,
) -> dict:
    """Pay replacement ingest outside the independently measured regime."""
    started = time.monotonic()
    raw_dir = pathlib.Path(args.output).parent / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schemaVersion": 1, "regime": name,
        "fixtureSourceFingerprint": fingerprint,
        "restoreRoot": str(root.resolve()),
        "templateSha256": _replay_template_digest(template),
        "outcome": "failed",
    }
    path = raw_dir / f"{name}-cold-preparation.json"
    try:
        if _fixture_source_fingerprint(args.fixture_copy) != fingerprint:
            raise RuntimeError("operator fixture provenance changed before preparation")
        source_digest = _source_tree_digest(template)
        if (_source_tree_digest(root) != source_digest or
                _source_tree_digest(pathlib.Path(args.fixture_copy)) != source_digest):
            raise RuntimeError("restored source bytes or provenance mismatch")
        record["sourceSha256"] = source_digest
        binary = checkout / "bin" / "cctally"
        env = _fixture_env(root, production_shaped=True)
        _set_offline_config(env, binary)
        _prepare_isolated_frontier_hooks(binary, root)
        # The copied marker must describe its original root while the hook
        # validator checks inherited Codex hook keys. Bind this clone after
        # that check, recording the still-live prior root so its retained
        # conversation cursors can be verified rather than hidden.
        marker_path = root / ".cctally-soak-normalized.json"
        marker = _strict_json(marker_path.read_bytes())
        prior_root = marker.get("root")
        expected_prior_root = str(pathlib.Path(args.root).expanduser().resolve())
        stamp_path = getattr(args, "_prepared_regime_stamp", None)
        if stamp_path is not None:
            stamp = _strict_json(pathlib.Path(stamp_path).read_bytes())
            if (stamp.get("sourceRoot") != expected_prior_root or
                    stamp.get("templatePath") != str(template.resolve()) or
                    stamp.get("contentSha256") != record["templateSha256"]):
                raise RuntimeError("regime clone replay provenance changed")
        if (marker != {
                "schemaVersion": 1, "root": prior_root,
                "fixtureSourceFingerprint": fingerprint,
            } or prior_root != expected_prior_root or
                _source_tree_digest(pathlib.Path(prior_root)) != source_digest):
            raise RuntimeError("regime clone provenance changed during hook refresh")
        marker["root"] = str(root.resolve())
        marker["retainedPriorSourceRoot"] = prior_root
        marker["retainedPriorSourceSha256"] = source_digest
        marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n")
        # The dashboard's size-only Claude transcript fast path skips a copied
        # JSONL whose bytes and size are unchanged, leaving its stored inode
        # stale. Rebuild that rail before waiting for the Codex replacement
        # ingest and a caught-up dashboard tick.
        rebuild_started = time.monotonic()
        rebuild_command = [str(binary), "cache-sync", "--source", "claude",
                           "--rebuild"]
        rebuild = subprocess.run(
            rebuild_command, cwd=checkout, env=env, capture_output=True,
            text=True, errors="replace",
        )
        record["claudeConversationRebuildSeconds"] = round(
            time.monotonic() - rebuild_started, 3)
        record["claudeConversationRebuildExitCode"] = rebuild.returncode
        if rebuild.returncode != 0:
            raise RuntimeError(
                "cold preparation Claude conversation rebuild failed: "
                + rebuild.stderr[-2000:])
        last = None
        deadline = time.monotonic() + IDLE_READINESS_TIMEOUT_SECONDS
        with _live_regime_dashboard(
            root, binary, env, args.sync_interval, admit_initial_sync=False,
        ) as live:
            while time.monotonic() < deadline:
                if live.proc.poll() is not None:
                    raise RuntimeError("cold preparation dashboard exited")
                status, raw, _latency = _request(
                    live.port, "/api/debug/backend")
                if status == 200:
                    last = _strict_json(raw)
                    tick = last.get("tick") or {}
                    records = tick.get("conversation_sync") or ()
                    latest = records[-1] if records else {}
                    if (int(tick.get("tick_seq") or 0) > 0
                            and latest.get("status") == "ok"
                            and latest.get("claude_mode") == "caught_up"
                            and latest.get("codex_mode") == "caught_up"):
                        try:
                            cursors = _prepared_source_cursor_evidence(root)
                        except (OSError, sqlite3.Error, ValueError, TypeError):
                            pass
                        else:
                            record["sourceCursorCounts"] = cursors
                            record["caughtUpConversationSeq"] = latest.get("seq")
                            record["caughtUpTickSeq"] = tick["tick_seq"]
                            break
                time.sleep(0.5)
            else:
                raise RuntimeError(
                    "cold preparation did not restore both source rails: "
                    f"latest={(last or {}).get('tick', {}).get('conversation_sync', [])[-1:]!r}")
        # The from-zero Claude replay and the rails' re-ingest restore rows
        # older than retention and prune them again. On a full-size clone that
        # leaves a backlog the budgeted reclaim cannot drain before the
        # measured regime, so drain it on the stopped clone once both rails
        # have caught up.
        record["reclaimSettle"] = _settle_reclaim_backlog(
            root, binary, env, args.sync_interval,
            deadline=time.monotonic() + RECLAIM_COMPACTION_TIMEOUT_SECONDS
            + RECLAIM_SETTLE_TIMEOUT_SECONDS)
        if (_source_tree_digest(root) != source_digest or
                _fixture_source_fingerprint(args.fixture_copy) != fingerprint):
            raise RuntimeError("source bytes or operator provenance changed during preparation")
        _prepared_source_cursor_evidence(root)
        _require_no_reclaim_backlog(root / "data")
        record["outcome"] = "ready"
    except Exception as exc:
        record["failure"] = str(exc)[-2000:]
        raise
    finally:
        record["durationSeconds"] = round(time.monotonic() - started, 3)
        _write_raw_artifact(path, record)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "durationSeconds": record["durationSeconds"]}


def _run_evidence_probe(args, name: str, receipt: dict) -> dict:
    checkout = pathlib.Path(args.checkout or REPO).expanduser().resolve()
    _ensure_prepared_regime_template(args, receipt)
    regime_root = pathlib.Path(args._prepared_regime_root)
    template = pathlib.Path(args._prepared_regime_template)
    stamp_path = getattr(args, "_prepared_regime_stamp", None)
    if stamp_path is not None:
        stamp = _strict_json(pathlib.Path(stamp_path).read_bytes())
        if (stamp.get("templatePath") != str(template.resolve()) or
                stamp.get("sourceRoot") != str(regime_root.resolve()) or
                stamp.get("candidateSha") !=
                getattr(args, "_prepared_regime_candidate_sha", None) or
                stamp.get("fixtureSourceFingerprint") !=
                receipt["fixtureSourceFingerprint"] or
                stamp.get("contentSha256") != _replay_template_digest(template)):
            raise RuntimeError("replay template identity drifted before restore")
    # Preserve the source root and every prior probe. Each regime receives a
    # uniquely named clone of the stamped template and keeps it for read-back.
    marker = _strict_json((regime_root / ".cctally-soak-normalized.json").read_bytes())
    if marker != {
        "schemaVersion": 1, "root": str(regime_root.resolve()),
        "fixtureSourceFingerprint": receipt["fixtureSourceFingerprint"],
    } or regime_root.is_symlink():
        raise ValueError("regime source clone marker mismatch before staging")
    probe_root = template.parent / f"probe-{name}-root"
    if probe_root.exists() or probe_root.is_symlink():
        raise ValueError(f"regime probe root already exists: {probe_root}")
    shutil.copytree(template, probe_root)
    preparation = _prepare_regime_clone(
        args, name, probe_root, template, checkout,
        receipt["fixtureSourceFingerprint"],
    )
    command = [
        sys.executable, str(checkout / "bench" / "dashboard-soak.py"),
        "--root", str(probe_root),
        "--fixture-copy", str(args.fixture_copy),
        "--checkout", str(checkout), "--execute-regime", name,
        "--duration-seconds", str(args.duration_seconds),
        "--quiet-seconds", str(args.quiet_seconds),
        "--sample-seconds", str(args.sample_seconds),
        "--sync-interval", str(args.sync_interval),
        "--reuse-prepared-root",
    ]
    diagnostic_path = None
    if name == "idle":
        diagnostic_path = pathlib.Path(args.output).parent / "raw" / (
            "idle-initial-sync-diagnostics.json")
        command.extend(["--diagnostic-output", str(diagnostic_path)])
    result = _evidence_execution(command, cwd=checkout)
    if result["command"]["exitCode"] != 0:
        failure_path = pathlib.Path(args.output).parent / "raw" / (
            f"{name}-failed-probe.json")
        _write_raw_artifact(failure_path, {
            "schemaVersion": 1,
            "name": name,
            "replayProvenance": str(stamp_path) if stamp_path else None,
            "diagnostics": str(diagnostic_path) if diagnostic_path else None,
            "probe": result,
        })
        raise RuntimeError(
            f"{name} evidence probe failed: {result['execution']['stderr'][-2000:]}"
            f"; failed-probe record: {failure_path}"
            + (f"; initial-sync diagnostics: {diagnostic_path}"
               if diagnostic_path and diagnostic_path.exists() else ""))
    metrics = _strict_json(result["execution"]["stdout"].encode())
    metrics["coldPreparation"] = preparation
    result["provenance"] = {
        "host": receipt["measurementHost"],
        "fixtureSourceFingerprint": receipt["fixtureSourceFingerprint"],
        "startedAt": result.pop("startedAt"),
        "finishedAt": result.pop("finishedAt"),
        "command": result.pop("command"),
    }
    result["measurements"] = metrics
    return result


def _copy_sqlite_fixture(source: pathlib.Path, target: pathlib.Path) -> None:
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        sidecar = pathlib.Path(f"{source}{suffix}")
        if sidecar.is_file():
            shutil.copy2(sidecar, pathlib.Path(f"{target}{suffix}"))


def _sqlite_upgrade_state(path: pathlib.Path) -> tuple[int, int, bool]:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        has_migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='schema_migrations'"
        ).fetchone() is not None
        applied = int(conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]) if has_migrations else 0
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    return version, applied, integrity


def _run_upgrade_probe(args, name: str) -> dict:
    checkout = pathlib.Path(args.checkout or REPO).expanduser().resolve()
    fixture_name, database_name = {
        "cache-044": ("045_conversation_render_revision_columns", "cache.db"),
        "conversations-009": (
            "conversations_010_codex_conversation_source_file_identity",
            "conversations.db",
        ),
    }[name]
    source = (checkout / "tests" / "fixtures" / "migrations" /
              "per-migration" / fixture_name / "pre.sqlite")
    if not source.is_file():
        raise RuntimeError(f"{name} upgrade fixture is missing: {source}")
    temporary_parent = getattr(args, "upgrade_temp_parent", None)
    with tempfile.TemporaryDirectory(
        prefix=f"cctally-{name}-", dir=temporary_parent,
    ) as temporary:
        root = pathlib.Path(temporary)
        data_dir = root / "data"
        data_dir.mkdir()
        (root / "claude" / "projects").mkdir(parents=True)
        (root / "codex-evidence" / "sessions").mkdir(parents=True)
        (root / "home").mkdir()
        target = data_dir / database_name
        _copy_sqlite_fixture(source, target)
        before_version, before_applied, before_ok = _sqlite_upgrade_state(target)
        env = _fixture_env(root, production_shaped=True)
        command = [sys.executable, str(checkout / "bin" / "cctally"),
                   "cache-sync", "--source", "all"]
        result = _evidence_execution(command, cwd=checkout, env=env)
        after_version, after_applied, after_ok = _sqlite_upgrade_state(target)
    if result["command"]["exitCode"] != 0:
        raise RuntimeError(
            f"{name} upgrade probe failed: {result['execution']['stderr'][-2000:]}")
    result["provenance"] = {
        "host": socket.gethostname(),
        "fixtureSourceFingerprint": _fixture_source_fingerprint(
            pathlib.Path(args.fixture_copy)),
        "startedAt": result.pop("startedAt"),
        "finishedAt": result.pop("finishedAt"),
        "command": result.pop("command"),
    }
    result["measurements"] = {
        "schemaBefore": before_version,
        "schemaAfter": after_version,
        "migrationAppliedCount": after_applied - before_applied,
        "integrityCheckOk": before_ok and after_ok,
        "regressionCount": 0,
        "durationMs": result["execution"]["durationMs"],
    }
    return result


def _write_raw_artifact(path: pathlib.Path, artifact: dict) -> dict:
    payload = (json.dumps(artifact, indent=2, sort_keys=True,
                          allow_nan=False) + "\n").encode()
    with path.open("xb") as handle:
        handle.write(payload)
    return {
        "path": path.relative_to(path.parent.parent).as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _candidate_tree_identity_problem(
    checkout: pathlib.Path, candidate_sha: str,
) -> str | None:
    """Compare materialized candidate bytes to a named commit, not runner HEAD."""
    if not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
        return "candidate SHA must be a full 40-character commit ID"
    commit = subprocess.run(
        ["git", "cat-file", "-e", f"{candidate_sha}^{{commit}}"],
        cwd=checkout, capture_output=True,
    )
    if commit.returncode != 0:
        return "candidate commit is unavailable in the runner checkout"
    tracked = subprocess.run(
        ["git", "diff", "--quiet", "--no-ext-diff", candidate_sha,
         "--", "."],
        cwd=checkout, capture_output=True,
    )
    if tracked.returncode != 0:
        return "materialized candidate tree differs from the named commit"
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=checkout, capture_output=True,
    )
    if untracked.returncode != 0:
        return "candidate untracked-file check failed"
    if untracked.stdout:
        return "materialized candidate tree has untracked files"
    return None


def _prepare_retention_prepass_root(args, fingerprint: str) -> None:
    """Normalize the copied root before its single recorded due retention."""
    source = pathlib.Path(args.fixture_copy).expanduser().resolve()
    root = pathlib.Path(args.root).expanduser().resolve()
    if _fixture_source_fingerprint(source) != fingerprint:
        raise RuntimeError("operator fixture changed before clone preparation")
    _prepare_fixture(root, source, args.scale, args.seed)
    if _fixture_source_fingerprint(source) != fingerprint:
        raise RuntimeError("operator fixture changed during clone preparation")
    binary = pathlib.Path(args.checkout or REPO).expanduser().resolve() / "bin" / "cctally"
    env = _fixture_env(root, production_shaped=True)
    _set_offline_config(env, binary)
    _prepare_isolated_frontier_hooks(binary, root)
    _initialize_deferred_retention_clone(binary, env, root / "data", fingerprint)
    if not _prepared_clone_matches(root, fingerprint):
        raise RuntimeError("prepass clone normalization is incomplete")


def _produce_evidence(args, candidate_sha: str) -> pathlib.Path:
    if not args.fixture_copy and not getattr(args, "small_pipeline", False):
        raise ValueError("--produce-evidence requires --fixture-copy")
    if not args.output:
        raise ValueError("--produce-evidence requires --output manifest path")
    if args.baseline_ref != PRE_EPIC_BASELINE:
        raise ValueError(
            f"--produce-evidence requires baseline {PRE_EPIC_BASELINE}")
    output = pathlib.Path(args.output).expanduser().resolve()
    raw_dir = output.parent / "raw"
    if output.exists() or raw_dir.exists():
        raise ValueError("evidence output or raw directory already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir()

    if getattr(args, "small_pipeline", False):
        trial_source, _trial_root, _trial_fingerprint = _prepare_small_fixture(
            args, label="trial")
        args.fixture_copy = trial_source

    fingerprint = _fixture_source_fingerprint(args.fixture_copy)
    if (not getattr(args, "small_pipeline", False) and
            (pathlib.Path(args.fixture_copy).expanduser().resolve() / "data" /
             "conversations.db").stat().st_size < MIN_PRODUCTION_CONVERSATION_BYTES):
        raise ValueError("full-size evidence requires a multi-GB conversation copy")
    _prepare_retention_prepass_root(args, fingerprint)
    retention_probe = _run_retention_prepass(args, {
        "measurementHost": socket.gethostname(),
        "fixtureSourceFingerprint": fingerprint,
    })
    retention_metrics = retention_probe["measurements"]
    _validate_external_metrics("retention", "retention-when-due", retention_metrics)

    def envelope(kind: str, name: str, probe: dict, tier: str) -> dict:
        artifact = {
            "schemaVersion": 1,
            "kind": kind,
            "name": name,
            "fixtureTier": tier,
            "candidateSha": candidate_sha,
            "baselineSha": PRE_EPIC_BASELINE,
            "provenance": probe["provenance"],
            "execution": probe["execution"],
            "measurements": probe["measurements"],
        }
        _validate_external_metrics(kind, name, artifact["measurements"])
        return artifact

    # Persist the retention evidence before the direct soak, so a later
    # failure cannot discard the one recorded due retention.
    retention_ref = _write_raw_artifact(
        raw_dir / "retention-when-due.json",
        envelope("retention", "retention-when-due", retention_probe, "fullSize"))
    args.reuse_prepared_root = True
    direct = run_soak(args)
    if (direct.get("fixtureSourceFingerprint") != fingerprint or
            fingerprint != _fixture_source_fingerprint(args.fixture_copy)):
        raise RuntimeError("direct soak fixture fingerprint is missing or changed")
    if direct.get("measurementHost") != socket.gethostname():
        raise RuntimeError("direct soak host provenance is inconsistent")
    direct["retention"] = {
        "days": retention_metrics["days"],
        "dueAtStart": retention_metrics["dueAtStart"],
        "ran": retention_metrics["ran"],
    }

    regimes = {}
    regime_probes = {}
    _stage_regime_template_from_direct(args, direct, candidate_sha)

    def produce_tier(tier_args, receipt, names, tier):
        try:
            for name in names:
                probe = _run_evidence_probe(tier_args, name, receipt)
                regime_probes[name] = probe
                filename = re.sub(
                    r"(?<!^)(?=[A-Z])", "-", name).lower() + ".json"
                regimes[name] = _write_raw_artifact(
                    raw_dir / filename, envelope("regime", name, probe, tier))
        except Exception as exc:
            stamp = getattr(tier_args, "_prepared_regime_stamp", None)
            if stamp is not None:
                raise RuntimeError(
                    f"{exc}; exact replay template preserved at "
                    f"{tier_args._prepared_regime_template}; provenance: {stamp}"
                ) from exc
            raise

    produce_tier(args, direct, FULL_SIZE_REGIMES, "fullSize")
    small_source, small_root, small_fingerprint = _prepare_small_fixture(args)
    _validate_tier_fingerprints({
        "fullSize": fingerprint, "smallFixture": small_fingerprint,
    }, fingerprint)
    small_args = argparse.Namespace(**vars(args))
    small_args.root = str(small_root)
    small_args.fixture_copy = small_source
    small_receipt = {
        "measurementHost": direct["measurementHost"],
        "fixtureSourceFingerprint": small_fingerprint,
    }
    _stage_regime_template_from_direct(small_args, small_receipt, candidate_sha)
    produce_tier(small_args, small_receipt, SMALL_FIXTURE_REGIMES, "smallFixture")
    _validate_regime_execution_matrix(regime_probes)
    upgrades = {}
    for name in ("cache-044", "conversations-009"):
        upgrades[name] = _write_raw_artifact(
            raw_dir / f"{name}.json",
            envelope("upgrade", name, _run_upgrade_probe(args, name), "fullSize"))
    bundle = {
        "schemaVersion": 1,
        "candidateSha": candidate_sha,
        "baselineSha": PRE_EPIC_BASELINE,
        "fixtureSourceFingerprint": fingerprint,
        "fixtureTiers": {"fullSize": fingerprint,
                         "smallFixture": small_fingerprint},
        "pipelineMode": ("smallFixtureTrial" if getattr(args, "small_pipeline", False)
                         else "fullSizeCertification"),
        "retentionWhenDue": retention_ref,
        "regimes": regimes,
        "priorSchemaUpgrades": upgrades,
    }
    _merge_gate_evidence(direct, bundle, candidate_sha, output.parent)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    return output


def _evidence_number(metrics: dict, field: str, minimum: float,
                     maximum: float | None = None, *, integer=False) -> float:
    value = metrics.get(field)
    try:
        finite = math.isfinite(value) if isinstance(value, (int, float)) else False
    except OverflowError:
        finite = False
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not finite or value < minimum
            or (maximum is not None and value > maximum)
            or (integer and not isinstance(value, int))):
        raise ValueError(f"measurement {field} is missing, nonfinite or outside "
                         f"[{minimum}, {maximum if maximum is not None else '∞'}]")
    return value


def _validate_tier_fingerprints(tiers: dict, full_fingerprint: str) -> str:
    def valid(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
    if (not isinstance(tiers, dict) or
            set(tiers) != {"fullSize", "smallFixture"} or
            not valid(tiers.get("fullSize")) or
            tiers["fullSize"] != full_fingerprint):
        raise ValueError("full-size fixture fingerprint mismatch")
    small = tiers["smallFixture"]
    if not valid(small) or small == full_fingerprint:
        raise ValueError("small fixture fingerprint is missing or duplicates full size")
    return small


def _validate_retention_stages(name: str, metrics: dict) -> None:
    """Fail closed unless each retention stage is evidenced and consistent.

    The due deletion and its budgeted reclaim are the product behaviour being
    certified; a compaction and the settle that clears its stale record are
    setup, so each carries its own duration and bytes and must agree with the
    mechanism the evidence names.
    """
    before = _evidence_number(metrics, "familyBytesBefore", 0, integer=True)
    after = _evidence_number(metrics, "familyBytesAfter", 0, integer=True)
    if metrics.get("reclaimedBytes") != before - after:
        raise ValueError(f"{name} reclaimedBytes does not equal the family-byte change")
    phases = metrics.get("maintenancePhases")
    if not isinstance(phases, list):
        raise ValueError(f"{name} maintenancePhases is missing")
    deletes = [row["seq"] for row in phases
               if isinstance(row, dict) and row.get("phase") == "delete"
               and row.get("outcome") == "ok" and isinstance(row.get("seq"), int)]
    if not deletes:
        raise ValueError(f"{name} records no successful due deletion")
    if not any(isinstance(row, dict) and row.get("phase") == "reclaim"
               and isinstance(row.get("seq"), int) and row["seq"] > min(deletes)
               for row in phases):
        raise ValueError(f"{name} records no budgeted reclaim after its due deletion")
    mechanism = metrics.get("drainMechanism")
    if mechanism not in ("production", "production+compaction"):
        raise ValueError(f"{name} drainMechanism is not a known mechanism")
    compacted = mechanism == "production+compaction"
    production = metrics.get("production")
    if not isinstance(production, dict):
        raise ValueError(f"{name} production stage is missing")
    production_seconds = _evidence_number(production, "durationSeconds", 0.001)
    production_after = _evidence_number(
        production, "familyBytesAfter", 0, integer=True)
    backlog = _evidence_number(production, "backlogBytes", 0, integer=True)
    if (backlog > 0) is not compacted:
        raise ValueError(f"{name} production backlog does not match its drainMechanism")
    compaction = metrics.get("compaction")
    if not isinstance(compaction, dict) or compaction.get("ran") is not compacted:
        raise ValueError(f"{name} compaction does not match its drainMechanism")
    settle = metrics.get("settle")
    if not compacted:
        if settle is not None:
            raise ValueError(f"{name} settle ran without a compaction")
        if production_after != after:
            raise ValueError(f"{name} familyBytesAfter does not end the production stage")
        return
    compaction_seconds = _evidence_number(compaction, "durationSeconds", 0.001)
    compaction_before = _evidence_number(
        compaction, "familyBytesBefore", 0, integer=True)
    compaction_after = _evidence_number(
        compaction, "familyBytesAfter", 0, integer=True)
    if compaction_before != production_after:
        raise ValueError(
            f"{name} compaction familyBytesBefore does not continue the production stage")
    if compaction.get("reclaimedBytes") != compaction_before - compaction_after:
        raise ValueError(
            f"{name} compaction reclaimedBytes does not equal its family-byte change")
    made_due = compaction.get("retentionMadeDue")
    if not isinstance(made_due, bool):
        raise ValueError(f"{name} compaction retentionMadeDue is missing")
    if not isinstance(settle, dict):
        raise ValueError(f"{name} settle stage is missing")
    settle_seconds = _evidence_number(settle, "durationSeconds", 0)
    settle_phases = settle.get("phases")
    if not isinstance(settle_phases, list) or not any(
            isinstance(row, dict) and row.get("phase") == "reclaim"
            for row in settle_phases):
        raise ValueError(f"{name} settle has no clearing reclaim pass")
    deleted = any(isinstance(row, dict) and row.get("phase") == "delete"
                  for row in settle_phases)
    if settle.get("deletionRan") is not deleted or (deleted and not made_due):
        raise ValueError(f"{name} settle deletionRan is not the labelled dormant cleanup")
    # Stage durations are rounded to milliseconds; the total is not.
    if metrics["durationSeconds"] + 0.01 < (
            production_seconds + compaction_seconds + settle_seconds):
        raise ValueError(f"{name} durationSeconds is shorter than its stages")


@functools.lru_cache(maxsize=1)
def _current_schema_heads() -> dict[str, tuple[int, int]]:
    """(prior schema, current head) per upgrade artifact.

    The current heads are the registry lengths `_cctally_store._expected_head`
    uses, read from this soak's own tree: evidence production refuses any
    other checkout, so these are the heads its upgrade artifacts must reach.
    """
    db = _candidate_module(REPO, "_cctally_db")
    return {
        "cache-044": (44, len(db._CACHE_MIGRATIONS)),
        "conversations-009": (9, len(db._CONVERSATIONS_MIGRATIONS)),
    }


def _validate_external_metrics(kind: str, name: str, metrics: dict) -> None:
    """Bound measurements independently of any self-reported passed flag."""
    if not isinstance(metrics, dict):
        raise ValueError(f"{name} measurements are missing")

    def number(field, minimum=0, maximum=None, *, integer=False):
        return _evidence_number(metrics, field, minimum, maximum, integer=integer)

    def true(field):
        if metrics.get(field) is not True:
            raise ValueError(f"{name} measurement {field} is not true")

    if kind == "regime":
        run_id = metrics.get("measurementRunId")
        if (not isinstance(run_id, str) or len(run_id) != 64 or
                any(char not in "0123456789abcdef" for char in run_id)):
            raise ValueError(f"{name} measurementRunId is malformed")
        number("runtimeObservationCount", 1, integer=True)

    if kind == "retention":
        number("durationSeconds", 0.001)
        number("deletedPayloadBytes", 1, integer=True)
        number("reclaimedBytes", 0, integer=True)
        number("pendingBytes", 0, 0, integer=True)
        number("freelistBytes", 0, 0, integer=True)
        number("days", 1, integer=True)
        true("dueAtStart")
        true("ran")
        true("complete")
        number("inheritedPendingBytes", 0, integer=True)
        number("inheritedFreelistBytes", 0, integer=True)
        _validate_retention_stages(name, metrics)
        return

    if kind == "upgrade":
        before, current = _current_schema_heads()[name]
        number("schemaBefore", before, before, integer=True)
        number("schemaAfter", current, current, integer=True)
        number(
            "migrationAppliedCount", current - before, current - before,
            integer=True)
        number("regressionCount", 0, 0, integer=True)
        number("durationMs", 0.001)
        true("integrityCheckOk")
    elif name == "idle":
        number("durationSeconds", QUIET_MIN_SECONDS)
        number("sampleCount", QUIET_MIN_SAMPLES, integer=True)
        number("cpuPercent", 0, IDLE_CPU_PERCENT_CEILING)
        number("tickCount", 4, integer=True)
        number("conversationPassCount", 4, integer=True)
        true("quiet")
    elif name == "bothActive":
        # Operator decision (#857): full size gates that no appended event was
        # missed inside the unchanged probe window, plus RSS, retained owners
        # and API p95. The moved limits are #862 Task C's acceptance; here
        # each moved field is evidence, null or finite and non-negative.
        number("sampleCount", 4, integer=True)
        for field, limit in (
            ("rssBytes", PROCESS_CEILING_BYTES),
            ("retainedOwnerBytes", OWNER_TOTAL_CEILING_BYTES),
            ("apiP95Ms", API_P95_CEILING_MS),
        ):
            number(field, 0, limit)
        for provider in ("Claude", "Codex"):
            appended = number(f"appended{provider}Events", 1, integer=True)
            number(f"observed{provider}Events", appended, integer=True)
        if metrics.get("ungatedFields") != list(BOTH_ACTIVE_UNGATED_FIELDS):
            raise ValueError(
                f"{name} measurement ungatedFields is not the moved field list")
        for field in BOTH_ACTIVE_UNGATED_FIELDS:
            if field not in metrics:
                raise ValueError(f"{name} measurement {field} is missing")
            if metrics[field] is not None:
                number(field, 0)
        # Ungated, but never null: the producer raises instead of recording a
        # missing visibility latency, so a null here was not measured.
        number("mutationToRenderMs", 0)
    elif name in ("claudeActive", "codexActive"):
        number("sampleCount", 4, integer=True)
        for field, limit in (
            ("processCpuPercent", PROCESS_CPU_PERCENT_CEILING),
            ("combinedCpuDuty", COMBINED_CPU_DUTY_CEILING),
            ("rssBytes", PROCESS_CEILING_BYTES),
            ("retainedOwnerBytes", OWNER_TOTAL_CEILING_BYTES),
            ("fullBuildP50Ms", FULL_BUILD_P50_CEILING_MS),
            ("fullBuildP95Ms", FULL_BUILD_P95_CEILING_MS),
            ("apiP95Ms", API_P95_CEILING_MS),
            ("publishP95Ms", PUBLISH_P95_CEILING_MS),
            ("conversationPublishP95Ms", CONVERSATION_P95_CEILING_MS),
            ("publishMaxMs", PUBLICATION_GAP_CEILING_MS),
            ("conversationPublishMaxMs", PUBLICATION_GAP_CEILING_MS),
            ("mutationToRenderMs", FRESHNESS_CEILING_MS),
        ):
            number(field, 0, limit)
        if name == "claudeActive":
            number("observedClaudeEvents", 1, integer=True)
        if name == "codexActive":
            number("observedCodexEvents", 1, integer=True)
    elif name in ("missingHook", "ineffectiveHook"):
        number("frontierExpirySeconds", 0.001, 120)
        number("fallbackRefreshCount", 1, integer=True)
        number("trustedFreshFrontierCount", 1, integer=True)
        number("untrustedStaleFrontierCount", 1, integer=True)
        number("observedMutationCount", 1, integer=True)
        number("mutationToRenderMs", 0, FRESHNESS_CEILING_MS)
        true("invalidHookRejected")
    elif name == "mutationRace":
        observed = number("observedMutationCount", 2, integer=True)
        number("renderedMutationCount", observed, observed, integer=True)
        number("lostUpdateCount", 0, 0, integer=True)
        number("mutationToRenderMs", 0, FRESHNESS_CEILING_MS)
    elif name == "pricingSkew":
        observed = number("priceMismatchCount", 1, integer=True)
        number("recalculatedCount", observed, observed, integer=True)
        number("maxCostErrorUsd", 0, 1e-9)
    elif name == "degradedConversation":
        number("degradedCount", 1, integer=True)
        number("recoveredCount", 1, integer=True)
        number("unavailableContentLeaks", 0, 0, integer=True)
        number("conversationPublishP95Ms", 0, CONVERSATION_P95_CEILING_MS)
    elif name == "requestOverload":
        request_count = number("requestCount", 65, integer=True)
        number("maxConcurrentRequests", 65, request_count, integer=True)
        number("overloadCount", 1, request_count, integer=True)
        number("recoveredCount", 1, integer=True)
        number("unexpectedErrorCount", 0, 0, integer=True)
        number("peakRequestThreads", 1, 96, integer=True)
        number("peakRssBytes", 1, PROCESS_CEILING_BYTES, integer=True)
        number("apiP95Ms", 0, API_P95_CEILING_MS)
    else:
        raise ValueError(f"unknown evidence entry {kind}/{name}")


def _verified_artifact(reference: dict, directory: pathlib.Path, *, kind: str,
                       name: str, candidate_sha: str, fingerprint: str,
                       tier: str) -> tuple[dict, str]:
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ValueError(f"{name} reference requires path and sha256")
    relative = pathlib.PurePosixPath(reference["path"]) if isinstance(
        reference["path"], str) else pathlib.PurePosixPath("/")
    if (relative.is_absolute() or not relative.parts or
            any(part in (".", "..") for part in relative.parts) or
            str(relative) != reference["path"]):
        raise ValueError(f"{name} artifact path must be relative and contained")
    digest = reference["sha256"]
    if (not isinstance(digest, str) or len(digest) != 64 or
            any(char not in "0123456789abcdef" for char in digest)):
        raise ValueError(f"{name} artifact sha256 digest is malformed")
    base = directory.resolve(strict=True)
    path = base.joinpath(*relative.parts)
    if any(part.is_symlink() for part in (path, *path.parents) if part != base and
           base in part.parents):
        raise ValueError(f"{name} artifact path contains a symlink")
    if not path.is_file() or not path.resolve(strict=True).is_relative_to(base):
        raise ValueError(f"{name} artifact path is not a contained regular file")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"{name} artifact digest mismatch")
    artifact = _strict_json(raw)
    if (set(artifact) != {"schemaVersion", "kind", "name", "fixtureTier", "candidateSha",
                          "baselineSha", "provenance", "execution",
                          "measurements"} or
            type(artifact["schemaVersion"]) is not int or
            artifact["schemaVersion"] != 1 or artifact["kind"] != kind or
            artifact["name"] != name or artifact["fixtureTier"] != tier or
            artifact["candidateSha"] != candidate_sha or
            artifact["baselineSha"] != PRE_EPIC_BASELINE):
        raise ValueError(f"{name} artifact schema, identity or candidate SHA mismatch")
    provenance = artifact["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "host", "fixtureSourceFingerprint", "startedAt", "finishedAt", "command"
    } or provenance["fixtureSourceFingerprint"] != fingerprint:
        raise ValueError(f"{name} artifact provenance or fixture fingerprint mismatch")
    host = provenance["host"]
    if not isinstance(host, str) or not host.strip() or len(host) > 128:
        raise ValueError(f"{name} provenance host is missing")
    try:
        start = dt.datetime.fromisoformat(provenance["startedAt"].replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(provenance["finishedAt"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} provenance timestamps invalid") from exc
    if (start.tzinfo is None or end.tzinfo is None or end <= start):
        raise ValueError(f"{name} provenance timestamps invalid")
    command = provenance["command"]
    if (not isinstance(command, dict) or set(command) != {"argv", "exitCode"} or
            type(command["exitCode"]) is not int or command["exitCode"] != 0 or
            not isinstance(command["argv"], list) or
            len(command["argv"]) < 2 or len(command["argv"]) > 64 or
            any(not isinstance(arg, str) or not arg or "\x00" in arg
                for arg in command["argv"])):
        raise ValueError(f"{name} provenance command argv/exitCode invalid")
    executable = pathlib.Path(command["argv"][0]).name
    remote_wrapper = "bin/cctally-test-remote"  # mirror-private-ok: compared as text with a provenance argv, never opened or run
    if (command["argv"][0] != remote_wrapper
            and executable not in {"python", "python3"}
            and not executable.startswith("python3.")):
        raise ValueError(f"{name} provenance command is not an executable probe")
    execution = artifact["execution"]
    if not isinstance(execution, dict) or set(execution) != {
        "durationMs", "stdout", "stderr"
    }:
        raise ValueError(f"{name} execution receipt is malformed")
    _evidence_number(execution, "durationMs", 0.001)
    if any(not isinstance(execution[field], str) or len(execution[field]) > 200_000
           for field in ("stdout", "stderr")):
        raise ValueError(f"{name} execution output is malformed")
    _validate_external_metrics(kind, name, artifact["measurements"])
    return {"passed": True, "evidence": dict(reference), "fixtureTier": tier,
            "measurements": artifact["measurements"],
            "provenance": provenance, "execution": execution}, host


def _merge_gate_evidence(receipt: dict, bundle: dict, candidate_sha: str,
                         evidence_dir: pathlib.Path | None = None) -> dict:
    """Validate same-tree, digested raw artifacts; never rewrite direct-soak facts.

    Structural and numerical validation is not execution authenticity. An
    independent reviewer must inspect the raw artifacts before final closure.
    """
    if not isinstance(bundle, dict):
        raise ValueError("supplemental evidence must be an object")
    if bundle.get("candidateSha") != candidate_sha:
        raise ValueError("supplemental evidence candidate SHA does not match")
    if bundle.get("baselineSha") != PRE_EPIC_BASELINE:
        raise ValueError("supplemental evidence baseline SHA is not pre-epic")
    if (set(bundle) != {"schemaVersion", "candidateSha", "baselineSha",
                        "fixtureSourceFingerprint", "fixtureTiers", "pipelineMode",
                        "retentionWhenDue", "regimes", "priorSchemaUpgrades"}
            or type(bundle["schemaVersion"]) is not int or
            bundle["schemaVersion"] != 1):
        raise ValueError("supplemental evidence schemaVersion 1 contract invalid")
    fingerprint = receipt.get("fixtureSourceFingerprint")
    if (not isinstance(fingerprint, str) or len(fingerprint) != 64 or
            any(char not in "0123456789abcdef" for char in fingerprint) or
            bundle["fixtureSourceFingerprint"] != fingerprint):
        raise ValueError("supplemental evidence fixture fingerprint mismatch")
    small_fingerprint = _validate_tier_fingerprints(
        bundle["fixtureTiers"], fingerprint)
    mode = bundle["pipelineMode"]
    if (not isinstance(mode, str) or
            mode not in {"fullSizeCertification", "smallFixtureTrial"}):
        raise ValueError("supplemental evidence pipeline mode invalid")
    regimes = bundle["regimes"]
    upgrades = bundle["priorSchemaUpgrades"]
    if not isinstance(regimes, dict) or set(regimes) != set(REQUIRED_REGIMES):
        raise ValueError("supplemental evidence missing or unexpected regime")
    if not isinstance(upgrades, dict) or set(upgrades) != {
        "cache-044", "conversations-009"
    }:
        raise ValueError("supplemental evidence missing or unexpected prior-schema upgrade")
    if evidence_dir is None:
        raise ValueError("supplemental evidence artifact directory is missing")
    hosts = set()
    verified_regimes = {}
    verified_upgrades = []
    verified_retention, host = _verified_artifact(
        bundle["retentionWhenDue"], evidence_dir, kind="retention",
        name="retention-when-due", candidate_sha=candidate_sha,
        fingerprint=fingerprint, tier="fullSize")
    hosts.add(host)
    for name in REQUIRED_REGIMES:
        tier = "fullSize" if name in FULL_SIZE_REGIMES else "smallFixture"
        verified, host = _verified_artifact(
            regimes[name], evidence_dir, kind="regime", name=name,
            candidate_sha=candidate_sha,
            fingerprint=(fingerprint if tier == "fullSize" else small_fingerprint),
            tier=tier)
        hosts.add(host)
        verified_regimes[name] = verified
    for name in ("cache-044", "conversations-009"):
        verified, host = _verified_artifact(
            upgrades[name], evidence_dir, kind="upgrade", name=name,
            candidate_sha=candidate_sha, fingerprint=fingerprint,
            tier="fullSize")
        hosts.add(host)
        verified_upgrades.append({
            "from": name, "passed": True, "evidence": verified["evidence"],
            "measurements": verified["measurements"],
        })
    _validate_regime_execution_matrix(verified_regimes)
    if len(hosts) != 1:
        raise ValueError("supplemental evidence host provenance differs across artifacts")
    if hosts != {receipt.get("measurementHost")}:
        raise ValueError("supplemental evidence host does not match the direct soak")
    merged = dict(receipt)
    merged["regimes"] = {
        name: {key: value for key, value in verified.items()
               if key not in {"provenance", "execution"}}
        for name, verified in verified_regimes.items()
    }
    merged["priorSchemaUpgrades"] = verified_upgrades
    merged["retentionWhenDue"] = {
        "passed": True, "evidence": verified_retention["evidence"],
        "measurements": verified_retention["measurements"],
    }
    merged["retention"] = {
        "days": verified_retention["measurements"]["days"],
        "dueAtStart": True, "ran": True,
    }
    merged["fixtureTiers"] = dict(bundle["fixtureTiers"])
    merged["pipelineMode"] = mode
    merged["mutationToRenderMs"] = verified_regimes[
        "mutationRace"]["measurements"]["mutationToRenderMs"]
    merged["frontierPolicy"] = {
        **(receipt.get("frontierPolicy") or {}), "trustEnabled": True,
    }
    merged["externalEvidenceVerified"] = mode == "fullSizeCertification"
    merged["externalEvidenceHost"] = next(iter(hosts))
    merged["candidateSha"] = candidate_sha
    return merged


def _summary(receipt: dict) -> dict:
    samples = receipt.get("samples") or []
    return {
        "schemaVersion": receipt.get("schemaVersion"),
        "checkoutRef": receipt.get("checkoutRef"),
        "measurementHost": receipt.get("measurementHost"),
        "candidateSha": receipt.get("candidateSha"),
        "baselineRef": receipt.get("baselineRef"),
        "scale": receipt.get("scale"),
        "fixtureKind": receipt.get("fixtureKind"),
        "fixtureSourceFingerprint": receipt.get("fixtureSourceFingerprint"),
        "conversationStoreBytes": receipt.get("conversationStoreBytes"),
        "retention": receipt.get("retention"),
        "frontierPolicy": receipt.get("frontierPolicy"),
        "durationSeconds": receipt.get("durationSeconds"),
        "sampleCount": len(samples),
        "peakRssBytes": max(
            (int(row.get("rssBytes", 0)) for row in samples), default=0),
        "postWarmupRssSlopeBytesPerSecond": receipt.get(
            "postWarmupRssSlopeBytesPerSecond"),
        "postWarmupRssSlopeConfidence": receipt.get(
            "postWarmupRssSlopeConfidence"),
        "ownerEstimatedBytes": max(
            (int(row.get("ownerEstimatedBytes", 0)) for row in samples),
            default=0),
        "ownerCount": len(receipt.get("owners") or {}),
        "owners": receipt.get("owners"),
        "mainCpuDuty": receipt.get("mainCpuDuty"),
        "conversationCpuDuty": receipt.get("conversationCpuDuty"),
        "combinedCpuDuty": receipt.get("combinedCpuDuty"),
        "idleCpuPercent": receipt.get("idleCpuPercent"),
        "quietWindow": receipt.get("quietWindow"),
        "externalEvidenceVerified": receipt.get("externalEvidenceVerified"),
        "fullBuildP50Ms": percentile(receipt.get("fullBuildMs") or [], 0.50),
        "fullBuildP95Ms": percentile(receipt.get("fullBuildMs") or [], 0.95),
        "publishP95Ms": percentile(
            [float(value) / 1_000_000 for value in
             receipt.get("publishPeriodsNs") or []], 0.95),
        "publishMaxMs": max(
            (float(value) / 1_000_000 for value in
             receipt.get("publishPeriodsNs") or []), default=None),
        "conversationPublishP95Ms": percentile(
            [float(value) / 1_000_000 for value in
             receipt.get("conversationPeriodsNs") or []], 0.95),
        "conversationPublishMaxMs": max(
            (float(value) / 1_000_000 for value in
             receipt.get("conversationPeriodsNs") or []), default=None),
        "mutationToRenderMs": receipt.get("mutationToRenderMs"),
        "regimes": receipt.get("regimes"),
        "priorSchemaUpgrades": receipt.get("priorSchemaUpgrades"),
        "diskIoOpsPerSecond": receipt.get("diskIoOpsPerSecond"),
        "threadPeak": max(
            (int(row.get("threadCount", 0)) for row in samples), default=0),
        "apiP95Ms": receipt.get("apiP95Ms"),
        "graphs": receipt.get("graphs"),
        "stress": receipt.get("stress"),
        "sqliteSettingsStable": (
            receipt.get("sqliteBefore") == receipt.get("sqliteAfter")),
        "shutdown": receipt.get("shutdown"),
        "comparison": receipt.get("comparison"),
        "problems": receipt.get("problems"),
    }


# ── #901 §6.4: the full-size write-budget leg ─────────────────────────────
# A dedicated leg, NOT a change to #857's `bothActive` contract: idle and
# small-append arms on a compacted clone of the full-size fixture, and a
# backlog arm that KEEPS its free pages (no compaction) and admits due
# deletion only after warm admission by rewinding the retention stamp 25 h.
# Gated on the candidate's own `_lib_write_budget.LIMITS` (never above the I2
# caps) and the soak's existing process ceilings. `diskIoOpsPerSecond` stays a
# separate, non-discriminating gate: it counts I/O operations, not bytes, and
# it passed through the #901 defect.
WRITE_BUDGET_ARMS = ("idle", "append", "backlog")
WRITE_BUDGET_SECONDS = {"idle": 300, "append": 300, "backlog": 1800}
WRITE_BUDGET_SAMPLE_SECONDS = 15
WRITE_BUDGET_APPEND_INTERVAL_SECONDS = 0.5
WRITE_BUDGET_EXPIRING_GROUP_MIN_ROWS = 5_000
WRITE_BUDGET_MIN_RECLAIMS = 4
#: §6.4 (Q8): the backlog arm rewinds the retention stamp this far into its
#: 30 measured minutes, as §6.3 C does, so reclaim progresses first.
WRITE_BUDGET_REWIND_AFTER_SECONDS = 600
#: §6.4 (Q10): the soak certifies pacing, the limits, the ceilings and the
#: operation records - never the per-deletion I4 bound, which C-op, R-cov
#: and C certify with deletion receipts and G3s/G3v guard in every gate.
WRITE_BUDGET_I4_NOTE = "I4 not certified (Q10)"
#: §6.4 (Q11): each arm drains its fixture's ingest backlog before warm
#: admission with §6.3's certifiable-sync predicates, inside this deadline.
WRITE_BUDGET_ADMISSION_SECONDS = 900
_CATCHUP_TOOL = (pathlib.Path(__file__).resolve().parent / "write-attribution"
                 / "catchup.py")
_MIB = 1024 * 1024
# ── Q18 (dc15 F1): the leg's full-build gate is non-regression ─────────────
# Append is gated against append and backlog against backlog, each at p50
# and p95 on its own: candidate <= 1.2 x a recorded, qualified `56e66f07a`
# reference for that arm (equality passes). The absolute 5 s / 10 s ceilings
# are #881's and #857 Task B's: reported, never gated here. Idle has no full
# build to gate and reports it as not applicable on complete evidence.
#
# Capture the reference once per host and P0 fixture image (it runs the
# baseline's append for five minutes and its backlog for thirty, serially,
# once each, on clones of the baseline tree's P1 image; preparation v2,
# bench/README.md "Write-budget leg, preparation v2"):
#   dashboard-soak.py --write-budget-leg --capture-baseline-reference \
#       --fixture-image W/p1-BASE.sparsebundle --image-receipt W/p1-BASE.json \
#       --root W --output W/reference.json
# then reuse it for every candidate leg while it still qualifies:
#   dashboard-soak.py --write-budget-leg --fixture-image W/p1-CAND.sparsebundle \
#       --image-receipt W/p1-CAND.json --root W \
#       --baseline-reference W/reference.json --gate
# A missing, mismatched, empty, nonfinite or nonpositive reference makes the
# leg INVALID (exit 2) before any arm runs; a qualification mismatch needs a
# new capture, never another host's, baseline's or historical percentile.
# The one exception (#937 SD8, WRITE_BUDGET_REFERENCE_POLICY): a reference
# whose only defect is a demonstrated empty backlog population is usable, so
# every arm runs, but the leg stays INVALID (exit 2) and backlog full-build
# non-regression is uncertified.
WRITE_BUDGET_BASELINE_SHA = "56e66f07aacacc4b2d90b378190e99724064c1b4"
WRITE_BUDGET_GATED_ARMS = ("append", "backlog")
WRITE_BUDGET_FULL_BUILD_FACTOR = 1.2
_WRITE_BUDGET_FACTOR = fractions.Fraction(6, 5)
WRITE_BUDGET_REFERENCE_KIND = "writeBudgetBaselineReference"
#: The warm-build collector's identity; bump it when its selection changes.
WRITE_BUDGET_COLLECTOR = "warm-in-window/1"
#: #937 (Codex design thread 01a1c114, Q1-Q3): how a loaded reference is
#: adjudicated, arm by arm. A faithfully prepared backlog arm can be append-
#: free on `56e66f07a`: every tick then takes the idle decision and the arm
#: has no warm full build at all (measured on the #857 fixture: 348 of 348
#: window ticks idle, complete evidence). That demonstrated empty backlog
#: population - complete, gap-free tick evidence recorded AND recomputed from
#: its digest-bound raw diagnostics, n 0 - is the one defect this policy
#: recognizes. Every other reference arm and every shared qualification,
#: attestation, job and teardown requirement still binds. The capture's
#: bytes and `valid: false` are never rewritten. The leg then runs every
#: requested arm, gates append against append, keeps backlog's full-build
#: comparison INVALID ("uncertified") whatever the candidate's samples, and
#: still judges backlog's write, steady, RSS, API and maintenance gates. The
#: leg can never carry a verdict on such a reference (INVALID, exit 2). Builds
#: are never manufactured (dc21 D3).
WRITE_BUDGET_REFERENCE_POLICY = "per-arm/1"
WRITE_BUDGET_EMPTY_POPULATION = "empty warm full-build population"
WRITE_BUDGET_EMPTY_POPULATION_ARMS = ("backlog",)
WRITE_BUDGET_UNCERTIFIED = (
    "the 56e66f07a reference has no warm full build in this arm: "
    "full-build non-regression is uncertified (#937)")
WRITE_BUDGET_WARM_WINDOW = (
    "after the first warm final publication, the arm's measured window "
    "[start, end]: distinct tick seq, dispatch full, cold false, "
    "published_at inside the window")
#: The per-arm preparation contract a reference is bound to (version it
#: whenever `_run_write_budget_arm`'s setup changes). Version 2 (#937, spec
#: §5.6): the image replaces v1's per-arm clone of --fixture-copy.
WRITE_BUDGET_PREPARATION = {
    "version": 2,
    "image": ("P0: the fixture stream extracted into an APFS sparse-bundle "
              "image attached at its canonical root, normalized by its "
              "producer and sealed; P1: one clonefile(2) of P0 per tree, "
              "settled by that tree and sealed; P2: one clonefile(2) of the "
              "tree's P1 per arm, attached at the canonical root"),
    "identity": ("sealed (st_dev, st_ino, size, mtime_ns) equality at every "
                 "arm; no restamp"),
    "roots": ("data under the canonical root inside the image; logs, "
              "receipts and evidence under W/evidence/<step>/ and temp files "
              "under W/tmp/<step>/ on the backing volume"),
    "offline": "update.check.enabled=false",
    "idle": "db vacuum --db conversations; retention stamp now",
    "append": "db vacuum --db conversations; retention stamp now",
    "backlog": ("retention_days - 1 with its free pages kept; retention "
                "stamp now; rewound 25 h at 600 s"),
    "admission": ("catchup.py image-mode drain and verification "
                  "(WRITE_ATTRIBUTION_INPUTS=image:<p2-manifest>, the "
                  "manifest bound to the arm's identity receipt, exact "
                  "membership) inside 900 s, then the first warm final "
                  "publication"),
    "environment": ("HOME, CLAUDE_CONFIG_DIR, CODEX_HOME and TMPDIR under "
                    "the clone; PYTHONDONTWRITEBYTECODE=1"),
}
#: Teardown bounds for every process family the leg starts.
FAMILY_TERM_GRACE_SECONDS = 15.0
FAMILY_KILL_WAIT_SECONDS = 10.0


def _positive_finite(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _iso_epoch(value) -> "float | None":
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return None
    return stamp.timestamp()


def _warm_full_builds(diagnostics, start: float, end: float, *,
                      polled_at=None) -> dict:
    """The leg's full-build collector (Q18, dc15 F1): distinct ticks across
    the polled diagnostics, then only warm (`cold` false) full builds
    published inside [start, end]. Sequence deduplication alone does not
    establish warm-window membership, so cold and out-of-window records are
    excluded and counted. Complete only when every poll carried tick records,
    every record is stamped, the window's sequence numbers have no gap, and
    (Amendment 19 HR-20, the frozen-C coverage rule) the window's edges are
    covered: its first record's predecessor was published before the
    window, and its last record is within the window's longest publish
    period of the last poll (`polled_at`, else the window's end) - a
    sampling outage at an edge could hide a full build, so the idle arm's
    "not applicable" needs both."""
    records, problems, missing = {}, [], 0
    for diagnostic in diagnostics or ():
        tick = diagnostic.get("tick") if isinstance(diagnostic, dict) else None
        rows = tick.get("records") if isinstance(tick, dict) else None
        if not isinstance(rows, list):
            missing += 1
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("seq") is not None:
                records[int(row["seq"])] = row
    if missing:
        problems.append(f"{missing} diagnostic sample(s) carried no tick records")
    excluded = {"cold": 0, "outOfWindow": 0, "unstamped": 0}
    window_seqs, samples, seqs = [], [], []
    for seq, row in sorted(records.items()):
        at = _iso_epoch(row.get("published_at"))
        if at is None:
            excluded["unstamped"] += 1
            continue
        inside = start <= at <= end
        if inside:
            window_seqs.append(seq)
        if row.get("cold") is True:
            excluded["cold"] += 1
            continue
        if not inside:
            excluded["outOfWindow"] += 1
            continue
        if row.get("cold") is not False:
            excluded["unstamped"] += 1
            continue
        if row.get("dispatch") != "full":
            continue
        duration = row.get("duration_ns")
        if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
                or not math.isfinite(duration)):
            excluded["unstamped"] += 1
            continue
        samples.append(float(duration) / 1_000_000)
        seqs.append(seq)
    if excluded["unstamped"]:
        problems.append(f"{excluded['unstamped']} tick record(s) without a "
                        "publication time, cold flag or duration")
    if not window_seqs:
        problems.append("no tick records in the window")
    else:
        if window_seqs[-1] - window_seqs[0] + 1 != len(window_seqs):
            problems.append("the tick evidence has gaps (missing seq numbers "
                            "in the window)")
        before = records.get(window_seqs[0] - 1)
        before_at = _iso_epoch((before or {}).get("published_at"))
        if before_at is None or before_at >= start:
            problems.append("incomplete window coverage: no tick record "
                            "published before the window precedes its first")
        periods = [records[q]["period_ns"] / 1e9 for q in window_seqs
                   if isinstance(records[q].get("period_ns"), (int, float))]
        edge = max(polled_at) if polled_at else end
        tail = edge - (_iso_epoch(records[window_seqs[-1]].get("published_at"))
                       or start)
        if not periods or tail > max(periods):
            problems.append(f"incomplete window coverage: the last tick record "
                            f"is {tail:.1f} s before the last poll, longer "
                            "than the window's longest publish period")
    return {"samplesMs": samples, "seqs": seqs, "n": len(samples),
            "p50Ms": percentile(samples, 0.50),
            "p95Ms": percentile(samples, 0.95), "ticks": len(window_seqs),
            "excluded": excluded, "complete": not problems,
            "problems": problems}


def _reference_population_problems(full) -> "list[str]":
    """A reference population: complete, nonempty, every sample finite and
    positive, and n, p50 and p95 exactly what the samples give."""
    if not isinstance(full, dict):
        return ["no full-build population"]
    problems = []
    if full.get("complete") is not True:
        problems.append("incomplete tick evidence: "
                        + "; ".join(full.get("problems") or ["unknown"]))
    samples = full.get("samplesMs")
    if not isinstance(samples, list) or not samples:
        problems.append(WRITE_BUDGET_EMPTY_POPULATION)
        return problems
    if not all(_positive_finite(value) for value in samples):
        problems.append("a nonfinite or nonpositive warm sample")
        return problems
    if full.get("n") != len(samples):
        problems.append(f"n {full.get('n')!r} does not match {len(samples)} "
                        "samples")
    for q, rank in (("p50Ms", 0.50), ("p95Ms", 0.95)):
        value = full.get(q)
        if not _positive_finite(value):
            problems.append(f"{q} {value!r} is nonfinite or nonpositive")
        elif value != percentile(samples, rank):
            problems.append(f"{q} {value} is not the samples' nearest-rank "
                            f"percentile {percentile(samples, rank)}")
    return problems


def _harness_write_io():
    """The write counter is harness instrumentation: always this tree's,
    never the measured checkout's (the baseline has none)."""
    return _candidate_module(REPO, "_lib_write_io")


def _sysctl(name: str) -> "str | None":
    try:
        done = subprocess.run(["sysctl", "-n", name], capture_output=True,
                              text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def _host_identity() -> dict:
    memory = None
    if sys.platform == "darwin":
        model, raw = _sysctl("hw.model"), _sysctl("hw.memsize")
        memory = int(raw) if raw and raw.isdigit() else None
    else:
        model = platform.processor() or None
        try:
            memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (AttributeError, OSError, ValueError):
            memory = None
    return {"hostname": socket.gethostname(), "machine": platform.machine(),
            "model": model, "cpuCount": os.cpu_count(), "memoryBytes": memory}


def _runtime_identity() -> dict:
    return {"os": platform.system(), "osRelease": platform.release(),
            "osVersion": platform.mac_ver()[0] or platform.version(),
            "python": platform.python_version(),
            "pythonExecutable": os.path.realpath(sys.executable),
            "sqlite": sqlite3.sqlite_version}


def _storage_identity(path) -> dict:
    """{class: internal|external, mountPoint, fsType} of the volume `path`
    (or its nearest existing ancestor) lives on."""
    identity = {"class": None, "mountPoint": None, "fsType": None}
    if not path:
        return identity
    here = pathlib.Path(path).expanduser().resolve()
    while not here.exists() and here != here.parent:
        here = here.parent
    mount = here
    while not os.path.ismount(mount) and mount != mount.parent:
        mount = mount.parent
    identity["mountPoint"] = str(mount)
    if sys.platform == "darwin":
        try:
            info = plistlib.loads(subprocess.run(
                ["diskutil", "info", "-plist", str(mount)],
                capture_output=True, check=True, timeout=60).stdout)
        except (OSError, subprocess.SubprocessError, ValueError,
                plistlib.InvalidFileException):
            info = None
        if isinstance(info, dict) and "Internal" in info:
            identity["class"] = "internal" if info["Internal"] else "external"
            identity["fsType"] = info.get("FilesystemType")
            return identity
    internal = {os.stat("/").st_dev}
    try:
        internal.add(os.stat(pwd.getpwuid(os.getuid()).pw_dir).st_dev)
    except (KeyError, OSError):
        pass
    identity["class"] = ("internal" if here.stat().st_dev in internal
                         else "external")
    return identity


def _image_receipt_of(args) -> "dict | None":
    """The P1 receipt `--image-receipt` names, or None when absent or
    unreadable (the attestation then says why)."""
    path = getattr(args, "image_receipt", None)
    if not path:
        return None
    try:
        receipt = _strict_json(pathlib.Path(path).expanduser().read_bytes())
    except (OSError, ValueError):
        return None
    return receipt if isinstance(receipt, dict) else None


def _write_budget_qualification(args) -> dict:
    """The SHARED keys a reference must match to be reused (dc15 F1; v2,
    spec §5.6): the P0 seal and P0 receipt digest (from the P1 receipt) and
    the preparation contract, host and hardware, OS, Python and SQLite, the
    backing storage plus the image descriptor, instrumentation and the
    workload protocol with P0's restored retention. The tree SHAs are
    deliberately absent: each run's P1 attests on its own
    (`_p1_attestation_problems`), never compared between runs."""
    p0 = (_image_receipt_of(args) or {}).get("p0") or {}
    days = p0.get("retentionDays")
    days = days if isinstance(days, int) and not isinstance(days, bool) else None
    sync = getattr(args, "sync_interval", None)
    qualification = {
        "fixture": {"p0Seal": p0.get("seal"),
                    "p0ReceiptSha256": p0.get("sha256"),
                    "preparation": WRITE_BUDGET_PREPARATION},
        "host": _host_identity(),
        "runtime": _runtime_identity(),
        "storage": {**_storage_identity(getattr(args, "root", None)),
                    "image": V2_IMAGE_DESCRIPTOR},
        "instrumentation": {
            "collector": WRITE_BUDGET_COLLECTOR, "percentile": "nearest-rank",
            "diagnostic": "/api/debug/backend tick.records",
            "sampleSeconds": WRITE_BUDGET_SAMPLE_SECONDS,
            "writeCounter": _harness_write_io().ProcessWriteCounter().source},
        "workload": {
            "syncIntervalSeconds": None if sync is None else float(sync),
            "armSeconds": dict(WRITE_BUDGET_SECONDS),
            "appendIntervalSeconds": WRITE_BUDGET_APPEND_INTERVAL_SECONDS,
            "appendProviders": ["claude", "codex"],
            "rewindAfterSeconds": WRITE_BUDGET_REWIND_AFTER_SECONDS,
            "rewindHours": 25, "retentionDays": days,
            "backlogRetentionDays": None if days is None else max(1, days - 1),
            "expiringGroupMinRows": WRITE_BUDGET_EXPIRING_GROUP_MIN_ROWS,
            "admissionSeconds": WRITE_BUDGET_ADMISSION_SECONDS,
            "warmWindow": WRITE_BUDGET_WARM_WINDOW},
    }
    return json.loads(json.dumps(qualification))


def _p1_attestation_problems(p1_receipt, *, expected_tree, p0_seal,
                             bundle) -> "list[str]":
    """One run's per-tree attestation (spec §5.6): a passing P1 receipt of
    exactly this run's tree, built on the shared P0 seal, whose seal still
    matches the bundle the arms clone."""
    if not isinstance(p1_receipt, dict):
        return ["no readable P1 receipt (--image-receipt)"]
    problems = []
    if (p1_receipt.get("kind") != P1_RECEIPT_KIND
            or p1_receipt.get("passed") is not True):
        problems.append(f"the P1 receipt is not a passing {P1_RECEIPT_KIND}")
    tree = p1_receipt.get("treeSha")
    if not expected_tree or tree != expected_tree:
        problems.append(f"the P1 receipt's tree {tree} is not this run's tree "
                        f"{expected_tree}")
    seal = (p1_receipt.get("p0") or {}).get("seal")
    if not p0_seal or seal != p0_seal:
        problems.append(f"the P1 receipt's P0 seal {seal} is not the shared "
                        f"P0 seal {p0_seal}")
    recorded = p1_receipt.get("seal")
    if not bundle:
        problems.append("no P1 bundle (--fixture-image)")
    elif not isinstance(recorded, dict):
        problems.append("the P1 receipt records no seal")
    else:
        try:
            seal_problems = _soak_image().seal_metadata_problems(
                recorded, pathlib.Path(bundle).expanduser())
        except (OSError, ValueError) as exc:
            seal_problems = [f"unreadable ({exc})"]
        problems.extend(f"the P1 bundle {bundle} does not match its seal: {p}"
                        for p in seal_problems)
    return problems


def _qualification_mismatches(reference, run) -> "list[str]":
    """Every leaf of either qualification whose value differs or is absent
    on the other side, as `dotted.path: reference X, this run Y`."""
    if not isinstance(reference, dict) or not reference:
        return ["the reference records no qualification"]
    if not isinstance(run, dict) or not run:
        return ["this run has no qualification"]

    def flatten(node, prefix, out):
        if isinstance(node, dict) and node:
            for key, value in node.items():
                flatten(value, f"{prefix}.{key}" if prefix else str(key), out)
        else:
            out[prefix] = node

    left, right = {}, {}
    flatten(reference, "", left)
    flatten(json.loads(json.dumps(run)), "", right)
    absent = object()
    return [f"{key}: reference {left.get(key, '<absent>')!r}, this run "
            f"{right.get(key, '<absent>')!r}"
            for key in sorted(set(left) | set(right))
            if left.get(key, absent) != right.get(key, absent)]


# ── bounded, reaped process families (Q18: every exit reaps the family) ────

_PS_STATE_TIMEOUT_S = 5.0


def _exited_unreaped(pid: int) -> bool:
    """True once `pid` (our child) has exited, without reaping it."""
    if not hasattr(os, "waitid"):
        # Python added `os.waitid` on macOS only in 3.13, and the README
        # supports 3.11. An exited child we have not reaped is a zombie, so
        # ask `ps` for its state instead; no row means it is already gone.
        # A `ps` that hangs is bounded and read as "not exited yet", so the
        # caller's own deadline still decides.
        try:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(int(pid))],
                capture_output=True, text=True, timeout=_PS_STATE_TIMEOUT_S,
                env=dict(os.environ, LC_ALL="C")).stdout.strip()
        except subprocess.TimeoutExpired:
            return False
        return not state or state.startswith("Z")
    try:
        info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return True
    return info is not None and info.si_pid == pid


def _await_exit(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while not _exited_unreaped(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _signal_group(pgid: int, sig) -> bool:
    if pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _group_members(pgid: int) -> "list[int] | None":
    """Live (non-zombie) processes in group `pgid`; None when `ps` fails."""
    try:
        listing = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="],
                                 capture_output=True, text=True, timeout=30,
                                 check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    members = []
    for line in listing.splitlines():
        fields = line.split()
        if (len(fields) >= 3 and fields[1] == str(pgid)
                and not fields[2].startswith("Z")):
            members.append(int(fields[0]))
    return members


def _reap_group(pgid: int) -> dict:
    """Kill whatever is left of a child's own process group and prove it."""
    found = _group_members(pgid)
    if found:
        _signal_group(pgid, signal.SIGKILL)
    survivors = found
    deadline = time.monotonic() + FAMILY_KILL_WAIT_SECONDS
    while survivors:
        survivors = _group_members(pgid)
        if not survivors or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return {"pgid": pgid, "killed": found or [],
            "survivors": survivors, "verified": survivors is not None}


def _terminate_family(proc, *, grace: float, final_reader=None,
                      group_term: bool = False) -> dict:
    """Stop a child started in its own session and reap its whole family:
    SIGTERM (the child, or with `group_term` its group), SIGKILL to the group
    after `grace`, the child's terminal counter read while it is an unreaped
    zombie, then the reap and a verified kill of the group's remainder."""
    pid = proc.pid
    evidence = {"pid": pid, "signal": None, "exitCode": None,
                "finalWriteBytes": None, "finalWriteStatus": "not read"}
    if proc.returncode is not None:
        evidence.update(exitCode=proc.returncode,
                        finalWriteStatus="reaped before teardown")
    else:
        exited = _exited_unreaped(pid)
        if not exited:
            evidence["signal"] = "SIGTERM"
            if group_term:
                _signal_group(pid, signal.SIGTERM)
            else:
                with contextlib.suppress(ProcessLookupError):
                    proc.send_signal(signal.SIGTERM)
            exited = _await_exit(pid, grace)
        if not exited:
            evidence["signal"] = "SIGKILL"
            _signal_group(pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            exited = _await_exit(pid, FAMILY_KILL_WAIT_SECONDS)
        if exited and final_reader is not None:
            try:
                reading = final_reader(pid)
                evidence.update(finalWriteBytes=reading.value,
                                finalWriteStatus=reading.status)
            except Exception as exc:  # evidence, never a teardown failure
                evidence["finalWriteStatus"] = f"error: {exc}"
        try:
            evidence["exitCode"] = proc.wait(timeout=FAMILY_KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            evidence["exitCode"] = None
    evidence["group"] = _reap_group(pid)
    return evidence


def _child_label(cmd) -> str:
    parts = [str(part) for part in cmd]
    if (len(parts) > 1 and pathlib.Path(parts[0]).name.startswith("python")
            and not parts[1].startswith("-")):
        label = pathlib.Path(parts[1]).name
    else:
        label = "-".join([pathlib.Path(parts[0]).name] + parts[1:3])
    return re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:64] or "child"


class _MeasuredRun:
    """`subprocess.run`'s shape for one setup child of the leg: it runs in
    its own session with stdin closed and stdout/stderr in files under
    `log_dir`, under a finite deadline; on exit or timeout its final write
    counter is read before the reap and its whole group is killed. Each call
    leaves a record (label, exit, timeout, duration, write bytes, group)."""

    def __init__(self, *, log_dir, counter_factory=None, on_spawn=None,
                 on_exit=None):
        self.log_dir = pathlib.Path(log_dir)
        self.records: list[dict] = []
        self._counter_factory = counter_factory
        # Preparation v2: the job records every child it starts (pid and
        # start identity) so its teardown and its watchdog own them.
        self._on_spawn = on_spawn
        self._on_exit = on_exit

    def _read_final(self, pid):
        if self._counter_factory is not None:
            return self._counter_factory(pid).read()
        return _harness_write_io().ProcessWriteCounter(pid=pid).read()

    def __call__(self, cmd, *, env=None, cwd=None, stdout=None, stderr=None,
                 capture_output=False, text=False, timeout=None, check=False,
                 **_ignored):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        label = _child_label(cmd)
        stem = self.log_dir / f"{len(self.records):02d}-{label}"
        out_path, err_path = (stem.with_name(stem.name + ".stdout"),
                              stem.with_name(stem.name + ".stderr"))
        started = time.monotonic()
        with open(out_path, "wb") as out_fh, open(err_path, "wb") as err_fh:
            proc = subprocess.Popen(
                [str(part) for part in cmd], env=env, cwd=cwd,
                stdin=subprocess.DEVNULL, stdout=out_fh, stderr=err_fh,
                start_new_session=True)
        if self._on_spawn is not None:
            self._on_spawn(proc.pid)
        timed_out = False
        teardown = None
        try:
            deadline = None if timeout is None else started + float(timeout)
            while not _exited_unreaped(proc.pid):
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.05)
        finally:
            teardown = _terminate_family(
                proc, grace=FAMILY_TERM_GRACE_SECONDS,
                final_reader=self._read_final, group_term=True)
            if self._on_exit is not None:
                self._on_exit(proc.pid)
            self.records.append({
                "label": label, "returncode": teardown["exitCode"],
                "timedOut": timed_out,
                "durationS": round(time.monotonic() - started, 3),
                "writeBytes": teardown["finalWriteBytes"],
                "writeStatus": teardown["finalWriteStatus"],
                "group": teardown["group"],
                "logs": {"stdout": str(out_path), "stderr": str(err_path)}})

        def captured(path, wanted):
            if not wanted:
                return None
            data = path.read_bytes()
            return data.decode(errors="replace") if text else data
        out_value = captured(out_path, capture_output or stdout == subprocess.PIPE)
        err_value = captured(err_path, capture_output or stderr == subprocess.PIPE)
        if timed_out:
            raise subprocess.TimeoutExpired(cmd, timeout, output=out_value,
                                            stderr=err_value)
        code = teardown["exitCode"]
        if check and code:
            raise subprocess.CalledProcessError(code, cmd, out_value, err_value)
        return subprocess.CompletedProcess(cmd, code, out_value, err_value)


def _is_family_command(pid: int) -> "tuple[bool, str]":
    try:
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True,
                                 timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False, ""
    lowered = command.lower()
    return ("python" in lowered or "cctally" in lowered), command


def _sweep_clone_holders(data_dir) -> dict:
    """The last teardown check of an arm: any process still holding one of
    the clone's store files open. Detached product workers leave the
    dashboard's group, but not its stores. Python/cctally holders are the
    arm's family and are killed; anything else is recorded, never touched."""
    data_dir = pathlib.Path(data_dir)
    try:
        files = sorted(str(path) for path in data_dir.iterdir()
                       if path.is_file())
    except OSError as exc:
        return {"verified": False, "error": str(exc), "killed": [],
                "survivors": [], "otherHolders": []}

    def holders():
        if not files:
            return []
        try:
            done = subprocess.run(["lsof", "-t", "--", *files],
                                  capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode not in (0, 1):
            return None
        return sorted({int(x) for x in done.stdout.split() if x.isdigit()}
                      - {os.getpid()})

    found = holders()
    if found is None:
        return {"verified": False, "error": "lsof could not list the holders",
                "files": len(files), "killed": [], "survivors": [],
                "otherHolders": []}
    family, others = [], []
    for pid in found:
        member, command = _is_family_command(pid)
        (family if member else others).append(
            pid if member else {"pid": pid, "command": command[:200]})
    for pid in family:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    survivors = family
    deadline = time.monotonic() + FAMILY_KILL_WAIT_SECONDS
    while survivors:
        now = holders()
        if now is None:
            break
        survivors = [pid for pid in now if pid in family]
        if not survivors or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    return {"verified": True, "files": len(files), "killed": family,
            "survivors": survivors, "otherHolders": others}


def _teardown_problems(teardown: dict) -> "list[str]":
    problems = []
    groups = [child.get("group") for child in teardown.get("setupChildren") or ()]
    dashboard = teardown.get("dashboard")
    if dashboard:
        groups.append(dashboard.get("group"))
    for group in groups:
        if not isinstance(group, dict):
            continue
        if group.get("survivors") is None:
            problems.append(f"process group {group.get('pgid')} could not be "
                            "verified empty")
        elif group["survivors"]:
            problems.append(f"process group {group.get('pgid')} kept "
                            f"{group['survivors']} alive")
    holders = teardown.get("holders")
    if holders is not None:
        # v2: `ImageLifecycle.holders()` replaces the sweep; a holder this
        # run did not start is reported, never signalled, and invalidates.
        if holders.get("verified") is not True:
            problems.append("the image's holders could not be enumerated: "
                            f"{holders.get('error')}")
        elif holders.get("otherHolders"):
            problems.append("processes this run did not start hold files on "
                            f"the image: {holders['otherHolders'][:5]}")
        problems.extend(f"owned worker: {p}"
                        for p in holders.get("ownedProblems") or [])
    image = teardown.get("image")
    if isinstance(image, dict):
        problems.extend(f"image teardown: {p}"
                        for p in image.get("problems") or [])
    if "sweep" in teardown or holders is None:
        sweep = teardown.get("sweep") or {}
        if sweep.get("verified") is not True:
            problems.append("the clone's holder sweep could not run: "
                            f"{sweep.get('error')}")
        elif sweep.get("survivors"):
            problems.append(f"{sweep['survivors']} still hold the clone's "
                            "stores")
    return problems


def _candidate_module(checkout: pathlib.Path, name: str):
    """Import `name` from the CANDIDATE checkout's bin/, never this tree's."""
    bin_dir = str(pathlib.Path(checkout).resolve() / "bin")
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    return importlib.import_module(name)


def _clone_tree(source: pathlib.Path, target: pathlib.Path) -> None:
    """An APFS clone where the platform has one, else a plain copy."""
    if sys.platform == "darwin":
        result = subprocess.run(["cp", "-c", "-R", str(source), str(target)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return
    shutil.copytree(source, target)


def _retention_days_of(data_dir: pathlib.Path) -> int:
    try:
        config = json.loads((data_dir / "config.json").read_text())
    except (OSError, ValueError):
        config = {}
    block = config.get("conversation") if isinstance(config, dict) else None
    days = block.get("retention_days", 90) if isinstance(block, dict) else 90
    return int(days)


def _set_retention_days(data_dir: pathlib.Path, days: int) -> None:
    path = data_dir / "config.json"
    config = json.loads(path.read_text()) if path.exists() else {}
    config.setdefault("conversation", {})["retention_days"] = int(days)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")


def _set_retention_stamp(data_dir: pathlib.Path, when: dt.datetime) -> None:
    with contextlib.closing(sqlite3.connect(data_dir / "conversations.db")) as conn:
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("conversation_retention_last_prune_at", when.isoformat()))
        conn.commit()


def _expiry_batch(data_dir: pathlib.Path, days: int) -> dict:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    uri = f"file:{urllib.parse.quote(str(data_dir / 'conversations.db'))}?mode=ro"
    batch = {"cutoff": cutoff, "days": days}
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        for provider, table, key in (
                ("claude", "conversation_messages", "session_id"),
                ("codex", "codex_conversation_events", "conversation_key")):
            rows = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {key} IS NOT NULL "
                f"GROUP BY {key} HAVING MAX(timestamp_utc) < ?",
                (cutoff,)).fetchall()
            batch[provider] = {"groups": len(rows),
                               "rows": sum(r[0] for r in rows),
                               "largest": max((r[0] for r in rows), default=0)}
    return batch


def _expiring_group_problem(batch: dict) -> "str | None":
    largest = max(batch["claude"]["largest"], batch["codex"]["largest"])
    if largest < WRITE_BUDGET_EXPIRING_GROUP_MIN_ROWS:
        return (f"the fixture's due batch has no group of at least "
                f"{WRITE_BUDGET_EXPIRING_GROUP_MIN_ROWS} rows (largest {largest})")
    return None


_WA_WORKLOAD = None


def _wa_workload():
    """bench/write-attribution/workload.py, which owns the one I2 feed of
    the candidate kernel (Amendment 19 HR-8): `steady_evaluate` and
    `kernel_deletions` over `_lib_write_budget` from this tree's bin/."""
    global _WA_WORKLOAD
    if _WA_WORKLOAD is None:
        import importlib.util
        path = (pathlib.Path(__file__).resolve().parent / "write-attribution"
                / "workload.py")
        spec = importlib.util.spec_from_file_location("soak_wa_workload", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _WA_WORKLOAD = module
    return _WA_WORKLOAD


def _write_budget_steady(window: dict, ops: dict) -> dict:
    """The arm's I2 statistic from the candidate kernel (HR-8): every
    counter sample (a failed read is a sample without bytes, never dropped)
    with the publication count polled beside it, and the marked deletion
    operations bounded conservatively from the samples around them."""
    wa = _wa_workload()
    budget = wa._candidate("_lib_write_budget")
    samples = []
    for sample, debug in zip(window["samples"], window["diagnostics"]):
        ok = sample.get("status") == "ok" and sample.get("bytes") is not None
        samples.append(budget.CounterSample(
            int(round(float(sample["t"]) * 1e9)),
            int(sample["bytes"]) if ok else None,
            int(((debug or {}).get("tick") or {}).get("tick_seq") or 0)))
    deletion_ops = []
    for op in ops.values():
        if op.get("phase") != "delete" or op.get("outcome") != "ok":
            continue
        begun = _iso_epoch(op.get("started_at"))
        if begun is None:
            continue
        deletion_ops.append({
            "began": begun,
            "ended": begun + float(op.get("duration_ns") or 0) / 1e9,
            "process_write_bytes": op.get("process_write_bytes"),
            "rows": op.get("rows") or 0})
    deletions = wa.kernel_deletions(samples, deletion_ops)
    return wa.steady_evaluate(samples, deletions, start=window["start"],
                              end=window["end"],
                              warm=window.get("warm", window["start"]))


def _maintenance_completeness(ops: list, start_snap, end_snap) -> "list[str]":
    """Amendment 19 HR-18: the backlog arm's operations come from a
    16-record ring polled every 15 s, which can lose records. The #780
    record's op_seq and charged ledger, read at the window's start and end,
    must equal the committed operations the polls saw ending between them -
    a lost record would under-count the paced charges."""
    if not start_snap or not end_snap or start_snap.get("opSeq") is None \
            or end_snap.get("opSeq") is None:
        return ["no record snapshots bracket the window (op_seq)"]
    committed = []
    for op in ops:
        begun = _iso_epoch(op.get("started_at"))
        if op.get("outcome") != "ok" or begun is None:
            continue
        ended = begun + float(op.get("duration_ns") or 0) / 1e9
        if start_snap["t"] <= ended <= end_snap["t"]:
            committed.append(op)
    problems = []
    advance = int(end_snap["opSeq"]) - int(start_snap["opSeq"])
    if advance != len(committed):
        problems.append(f"op_seq advanced {advance}, {len(committed)} "
                        "operations seen in the ring (records were lost)")
    charged = sum(int(op.get("charged_bytes") or 0) for op in committed)
    if int(end_snap.get("charged") or 0) - int(start_snap.get("charged") or 0) \
            != charged:
        problems.append("the ledger's charged delta differs from the charges "
                        "the ring showed")
    return problems


def _record_seq_snapshot(data_dir: pathlib.Path) -> dict:
    """The #780 record's op_seq and charged ledger, read-only."""
    conn = sqlite3.connect(f"file:{data_dir / 'conversations.db'}?mode=ro",
                           uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key = "
            "'conversation_retention_reclaim_pending'").fetchone()
    finally:
        conn.close()
    record = json.loads(row[0]) if row and row[0] else {}
    return {"t": time.time(), "opSeq": record.get("op_seq"),
            "charged": sum(b.get("charged", 0)
                           for b in record.get("ledger") or [])}


def _maintenance_conditions(ops: list, start: float, end: float,
                            retention) -> dict:
    """§6.3 C conditions over the published operation records."""
    def epoch(iso):
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()

    inside = [op for op in ops if start <= epoch(op["started_at"]) <= end]
    deletions = [op for op in inside if op["phase"] == "delete"
                 and op["outcome"] == "ok"]
    reclaims = [op for op in inside if op["phase"] == "reclaim"
                and op["outcome"] == "ok" and op["pages_reclaimed"] > 0]
    if not deletions or len(reclaims) < WRITE_BUDGET_MIN_RECLAIMS:
        return {"invalid": (f"{len(deletions)} deletion and {len(reclaims)} "
                            "reclaim operations inside the interval")}
    charged = [op["charged_bytes"] for op in inside if op["outcome"] == "ok"]
    allowance = 4 * _MIB * ((end - start) / 60 + 1) + max(charged)
    problems = []
    if sum(charged) > allowance:
        problems.append(f"charged {sum(charged)} > allowance {int(allowance)}")
    for op in inside:
        if op["outcome"] != "ok":
            continue
        # Q8/Q9: the charge is recomputed from the record's own inputs.
        expected = retention.recompute_charge(op)
        if expected is None or op["charged_bytes"] != expected:
            problems.append(f"op {op['op_id']} charge != reservation")
        if (op.get("balance_before_bytes") or 0) < 0:
            problems.append(f"op {op['op_id']} started in debt")
    return {"deletions": len(deletions), "reclaims": len(reclaims),
            "chargedBytes": sum(charged), "allowanceBytes": int(allowance),
            "problems": problems}


def _measure_write_window(live, seconds: float, counter, *,
                          append_root: "pathlib.Path | None" = None,
                          midway=None, midway_after: float = 0.0) -> dict:
    samples, diagnostics, rss, latencies = [], [], [], []
    stop = threading.Event()
    appended = {"events": 0}

    def appender():
        try:
            while not stop.wait(WRITE_BUDGET_APPEND_INTERVAL_SECONDS):
                for provider in ("claude", "codex"):
                    _append_provider_event(append_root, provider,
                                           f"wb-{secrets.token_hex(6)}")
                    appended["events"] += 1
        except Exception as exc:  # noqa: BLE001 - a dead appender invalidates
            appended["error"] = f"{type(exc).__name__}: {exc}"

    thread = (threading.Thread(target=appender, daemon=True)
              if append_root is not None else None)
    started = time.time()
    began = time.monotonic()
    deadline = began + seconds
    midway_result = None
    if thread is not None:
        thread.start()
    try:
        while True:
            if live.proc.poll() is not None:
                raise RuntimeError("write-budget dashboard exited")
            if (midway is not None and midway_result is None
                    and time.monotonic() - began >= midway_after):
                midway_result = {"at": time.time(), "result": midway()}
            reading = counter.read()
            debug, latency = _live_debug(live.port)
            now = time.time()
            samples.append({"t": now, "bytes": reading.value,
                            "status": reading.status})
            diagnostics.append(debug)
            latencies.append(latency)
            rss.append({"t": now, **_process_sample(live.proc.pid)})
            if time.monotonic() >= deadline:
                break
            time.sleep(WRITE_BUDGET_SAMPLE_SECONDS)
    finally:
        stop.set()
        if thread is not None:
            thread.join(5)
    return {"start": started, "end": time.time(), "samples": samples,
            "diagnostics": diagnostics, "rss": rss,
            "apiLatenciesMs": latencies, "appendedEvents": appended["events"],
            "appenderError": appended.get("error"),
            "midway": midway_result}


def _drain_before_admission(checkout: pathlib.Path, env: dict,
                            root: pathlib.Path, *, run=None,
                            deadline_s: float = WRITE_BUDGET_ADMISSION_SECONDS,
                            image_manifest: "pathlib.Path | None" = None
                            ) -> "tuple[dict, str | None]":
    """§6.4 (Q11): the same drained admission as §6.3 - the candidate's
    unbudgeted `cache-sync --source all` plus a verification pass, admitted
    only on the certifiable-sync predicates (`catchup.py`). Returns the
    receipt and the reason the arm is invalid, if it is. `deadline_s` is
    what remains of the arm's one admission deadline (Amendment 19 HR-21:
    the drain and the warm admission share it). With `image_manifest`
    (preparation v2) catchup runs in its image mode,
    `WRITE_ATTRIBUTION_INPUTS=image:<manifest>`; `root` is then the arm's
    evidence root, never the data root."""
    out = root / "catchup.json"
    runner = subprocess.run if run is None else run
    if image_manifest is not None:
        env = {**env, "WRITE_ATTRIBUTION_INPUTS": f"image:{image_manifest}"}
    if deadline_s <= 0:
        return {}, "the admission deadline expired before the drain"
    try:
        child = runner(
            [sys.executable, str(_CATCHUP_TOOL), "--tree", str(checkout),
             "--out", str(out), "--deadline-s", str(int(deadline_s))],
            env=env, capture_output=True, text=True,
            timeout=deadline_s + 60)
    except subprocess.TimeoutExpired:
        return {}, "the drain overran its admission deadline"
    receipt = json.loads(out.read_text()) if out.exists() else {}
    if child.returncode != 0 or not receipt.get("admitted"):
        return receipt, ("the fixture's backlog was not drained: "
                         + "; ".join(receipt.get("problems") or
                                     [child.stderr.strip()[-200:]]))
    return receipt, None


def _maintenance_row_key(row: dict) -> tuple:
    """One maintenance ring record's identity across overlapping polls
    (#937 SD10): its ring `seq` with its phase, op_id, outcome and start. A
    skipped reclaim chunk carries the op_id (the record's op_seq) of the
    chunk committed just before it in the same visit, so (phase, op_id)
    alone collapses the two and drops the committed chunk and its charge."""
    return (row.get("seq"), row.get("phase"), row.get("op_id"),
            row.get("outcome"), row.get("started_at"))


def _write_budget_metrics(window: dict) -> dict:
    bad = [s["status"] for s in window["samples"] if s["status"] != "ok"]
    ops = {}
    for debug in window["diagnostics"]:
        for row in (debug.get("tick") or {}).get("maintenance") or ():
            if row.get("op_id"):
                ops[_maintenance_row_key(row)] = row
    # Amendment 19 HR-8: the candidate kernel's statistic and its rules.
    steady = _write_budget_steady(window, ops)
    # Q18: warm, in-window full builds only (cold and out-of-window records
    # excluded), with their raw samples, counts and completeness.
    full = _warm_full_builds(window["diagnostics"], window["start"],
                             window["end"],
                             polled_at=[s["t"] for s in window["samples"]])
    return {
        "counterStatus": bad[0] if bad else "ok",
        "steady": steady,
        "worstBytesPerMinute": steady["worstBytesPerMinute"],
        "worstBytesPerPublication": steady["worstBytesPerPublication"],
        "windows": steady["qualified"],
        "operations": sorted(ops.values(), key=lambda o: (
            o["op_id"], o.get("seq") or 0)),
        "fullBuild": full,
        "fullBuildP50Ms": full["p50Ms"],
        "fullBuildP95Ms": full["p95Ms"],
        "apiP95Ms": percentile(window["apiLatenciesMs"], 0.95),
        "rssMaxBytes": max(r["rssBytes"] for r in window["rss"]),
        "rssSlopeBytesPerSecond": linear_slope(window["rss"], "t", "rssBytes"),
        "appendedEvents": window["appendedEvents"],
        "start": window["start"], "end": window["end"],
    }


def _child_env(data_root: "pathlib.Path | None",
               tmp: pathlib.Path) -> dict:
    """The one base of every write-budget child's environment, v1 and v2
    (#937 F13): `_fixture_env(data_root)`'s isolated roots for a product
    child (the inherited environment for a tool), TMPDIR at `tmp` and
    PYTHONDONTWRITEBYTECODE=1. An inherited WRITE_ATTRIBUTION_INPUTS never
    selects catchup's input mode; the drain sets it explicitly."""
    env = (_fixture_env(pathlib.Path(data_root), production_shaped=True)
           if data_root is not None else dict(os.environ))
    env.pop("WRITE_ATTRIBUTION_INPUTS", None)
    env.pop(_soak_image().OWNER_TOKEN_ENV, None)
    tmp = pathlib.Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(tmp) + os.sep
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _write_budget_env(root: pathlib.Path) -> dict:
    """The v1 arm's environment: the fixture's isolated roots, and TMPDIR
    under the clone so temp files stay on the clone's (external) volume."""
    return _child_env(root, root / "tmp")


def _write_volumes(window: "dict | None", children: list,
                   dashboard: "dict | None") -> dict:
    """Setup, measured and terminal write volume, recorded separately: the
    setup children's own counters (read at exit) and the dashboard's bytes
    before the window; the window's bytes; the dashboard's bytes from the
    window's last sample to its exit."""
    ok = [s["bytes"] for s in (window or {}).get("samples") or ()
          if s.get("status") == "ok"]
    first, last = (ok[0], ok[-1]) if ok else (None, None)
    final = (dashboard or {}).get("finalWriteBytes")
    setup = [{"label": c["label"], "writeBytes": c["writeBytes"],
              "writeStatus": c["writeStatus"]} for c in children]
    return {"setup": {"children": setup,
                      "childrenBytes": (None if any(c["writeBytes"] is None
                                                    for c in setup)
                                        else sum(c["writeBytes"] for c in setup)),
                      "dashboardBeforeWindowBytes": first},
            "measuredBytes": None if first is None else last - first,
            "terminalBytes": (None if final is None or last is None
                              else final - last),
            "terminalStatus": (dashboard or {}).get("finalWriteStatus")}


def _run_write_budget_arm(args, name: str, *, p1_bundle, p1_receipt: dict,
                          data_root: pathlib.Path, evidence_root: pathlib.Path,
                          checkout: pathlib.Path, job,
                          capture: bool = False) -> dict:
    """One arm on its own clone of the tree's P1 image (spec §5.5, steps
    1-6): verify the P1 seal, clonefile and attach at the data root, require
    identity equality with the P1 seal before any product process starts,
    run v1's remaining per-arm preparation, admit in catchup's image mode,
    measure, then tear down through the job's lifecycle owner and remove the
    arm's bundle. Every log, receipt and the dashboard log go to
    `evidence_root` on the backing volume, never under `data_root`.
    `capture` measures the `56e66f07a` reference (Q18): no candidate module
    is imported from that checkout, the candidate-specific maintenance gates
    are not applied, and the raw diagnostics are returned for the
    reference's evidence."""
    si = _soak_image()
    checkout = pathlib.Path(checkout)
    data_root = pathlib.Path(data_root)
    evidence_root = pathlib.Path(evidence_root)
    working = pathlib.Path(job.working_root)
    mode = "reference" if capture else "candidate"
    if working not in evidence_root.parents:
        raise ValueError(f"the evidence root {evidence_root} is not under the "
                         f"working root {working}")
    evidence_root.mkdir(parents=True, exist_ok=False)
    binary = checkout / "bin" / "cctally"
    data_dir = data_root / "data"
    record = {"arm": name, "mode": mode, "root": str(data_root),
              "evidenceRoot": str(evidence_root),
              "preparationVersion": WRITE_BUDGET_PREPARATION["version"],
              "setup": {}, "admission": {}}
    try:
        seal_problems = si.seal_metadata_problems(
            (p1_receipt or {}).get("seal") or {}, pathlib.Path(p1_bundle))
    except (OSError, ValueError) as exc:
        seal_problems = [f"unreadable ({exc})"]
    if seal_problems:
        record["invalid"] = ("the P1 bundle does not match its seal: "
                             + "; ".join(seal_problems[:5]))
        return record
    arm_bundle = working / f"arm-{mode}-{name}-{_utc_stamp()}.sparsebundle"
    si.clonefile_bundle(pathlib.Path(p1_bundle), arm_bundle)
    record["bundle"] = str(arm_bundle)
    wio = _harness_write_io()
    attached = runner = live = window = None
    days = retention = None
    try:
        attached = job.lifecycle.attach(arm_bundle)
        record["image"] = attached.as_dict()
        # Step 2 (D1, D2): identity equality, before any product process.
        equality = si.identity_equality(
            attached, si.source_inventory(data_root), p1_receipt)
        manifest = si.write_admission_inputs(evidence_root / "admission",
                                             equality)
        record["identity"] = {
            "passed": equality["passed"], "problems": equality["problems"],
            "deviceMismatch": equality["deviceMismatch"],
            "manifestSha256": equality["manifestSha256"],
            "receipt": str(evidence_root / "admission" /
                           "identity-receipt.json")}
        if not equality["passed"]:
            record["invalid"] = ("identity equality with the P1 seal: "
                                 + "; ".join(equality["problems"][:5]))
            return record
        job.check()
        # Step 3: v1's remaining per-arm preparation, recorded as setup.
        env = _v2_env(_V2Step(f"arm-{mode}-{name}", working, data_root))
        runner = job.run(evidence_root / "logs")
        _checked(runner(_product(checkout, "config", "set",
                                 "update.check.enabled", "false"),
                        env=env, capture_output=True, text=True,
                        timeout=max(1.0, job.remaining())),
                 "config set update.check.enabled false")
        if name in ("idle", "append"):
            record["setup"]["compaction"] = _compact_reclaim_backlog(
                binary, env, data_dir,
                timeout_seconds=RECLAIM_COMPACTION_TIMEOUT_SECONDS, run=runner)
        else:
            days = max(1, _retention_days_of(data_dir) - 1)
            _set_retention_days(data_dir, days)
        _set_retention_stamp(data_dir, dt.datetime.now(dt.timezone.utc))
        retention = (None if capture else
                     _candidate_module(checkout, "_lib_conversation_retention"))
        job.check()
        # Step 4: image-mode admission. Amendment 19 HR-21: ONE 900 s
        # deadline covers the drain and the warm admission, as in §6.3.
        admit_deadline = time.monotonic() + WRITE_BUDGET_ADMISSION_SECONDS
        receipt, problem = _drain_before_admission(
            checkout, env, evidence_root, run=runner,
            deadline_s=admit_deadline - time.monotonic(),
            image_manifest=manifest)
        record["setup"]["catchUp"] = receipt
        record["admission"].update({
            "deadlineSeconds": WRITE_BUDGET_ADMISSION_SECONDS,
            "inputMode": receipt.get("inputMode"),
            "catchUpAdmitted": bool(receipt.get("admitted")),
            "catchUpElapsedS": receipt.get("elapsedS"),
            "catchUpProblems": receipt.get("problems") or []})
        if problem:
            record["invalid"] = problem
            return record
        job.check()
        # Step 5: measure, unchanged from v1; appends go to the data root,
        # whose glob and stored spelling agree by construction.
        with _live_regime_dashboard(
                data_root, binary, env, args.sync_interval, reap_family=True,
                final_write_bytes=lambda pid: wio.ProcessWriteCounter(
                    pid=pid).read(),
                log_path=evidence_root / "regime-dashboard.log",
                on_spawn=job.register) as live:
            try:
                _await_warm_publication(live.port, f"write-budget {name}",
                                        deadline=admit_deadline)
            except RuntimeError as exc:
                record["invalid"] = (f"no warm admission inside the "
                                     f"{WRITE_BUDGET_ADMISSION_SECONDS} s "
                                     f"admission deadline ({exc})")
                return record
            record["admission"]["warmAdmittedAt"] = time.time()
            midway = None
            if name == "backlog":
                # The batch the rewind will make due is checked now (an arm
                # without a large expiring group is invalid before it
                # measures) and recorded again at the rewind (Q8).
                problem = _expiring_group_problem(_expiry_batch(data_dir, days))
                if problem:
                    record["invalid"] = problem
                    return record

                def midway():
                    batch = _expiry_batch(data_dir, days)
                    _set_retention_stamp(
                        data_dir,
                        dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25))
                    return batch
            seq_start = (_record_seq_snapshot(data_dir)
                         if name == "backlog" else None)
            window = _measure_write_window(
                live, WRITE_BUDGET_SECONDS[name],
                wio.ProcessWriteCounter(pid=live.proc.pid),
                append_root=data_root if name == "append" else None,
                midway=midway, midway_after=WRITE_BUDGET_REWIND_AFTER_SECONDS)
            window["warm"] = record["admission"]["warmAdmittedAt"]
            if window.get("appenderError"):
                record["invalid"] = ("the appender failed "
                                     f"({window['appenderError']}); a dead "
                                     "appender invalidates the workload")
            if name == "backlog":
                window["recordSnapshots"] = {
                    "start": seq_start, "end": _record_seq_snapshot(data_dir)}
    finally:
        # Step 6: the lifecycle owner tears down; evidence is already under
        # the evidence root.
        children = list(getattr(runner, "records", None) or [])
        teardown = {
            "setupChildren": [{key: child.get(key) for key in
                               ("label", "returncode", "timedOut", "durationS",
                                "group")} for child in children],
            "dashboard": getattr(live, "teardown", None)}
        if attached is not None:
            try:
                # #937 SD7: this job's owned detached workers (a product
                # worker a recorded child started in its own session) are
                # reaped first; only a holder neither recorded nor owned is
                # an other holder, reported and never signalled.
                owned, owned_problems = job.lifecycle.reap_owned_workers()
                found = job.lifecycle.holders()
                teardown["holders"] = {
                    "verified": True,
                    "recorded": sorted(int(h["pid"]) for h in found
                                       if h.get("recorded")),
                    "ownedWorkers": owned,
                    "ownedProblems": owned_problems,
                    "otherHolders": [
                        {"pid": h.get("pid"), "command": h.get("command")}
                        for h in found
                        if not h.get("recorded") and not h.get("owned")]}
            except si.HolderEnumerationFailed as exc:
                teardown["holders"] = {"verified": False, "error": str(exc),
                                       "otherHolders": []}
            teardown["image"] = job.lifecycle.detach(attached).as_dict()
        record["teardown"] = teardown
        record["writes"] = _write_volumes(window, children,
                                          teardown["dashboard"])
        problems = _teardown_problems(teardown)
        if problems and not record.get("invalid"):
            record["invalid"] = "teardown: " + "; ".join(problems)
        stopped = _stop_problems(job)
        if stopped and not record.get("invalid"):
            # #937 F1: a stop during in-process work invalidates the arm.
            record["invalid"] = "; ".join(stopped)
        image = teardown.get("image") or {}
        if (attached is not None and not image.get("problems")
                and arm_bundle.is_dir()):
            # Session-created data: removed once the image is detached.
            shutil.rmtree(arm_bundle)
            record["bundleRemoved"] = True
    metrics = _write_budget_metrics(window)
    record["metrics"] = metrics
    record["window"] = {"start": window["start"], "end": window["end"]}
    if name == "backlog":
        record["setup"]["batch"] = (window.get("midway") or {}).get("result")
        record["setup"]["rewoundAt"] = (window.get("midway") or {}).get("at")
        if capture:
            record["maintenance"] = {
                "applies": False, "problems": [],
                "reason": "candidate-specific maintenance gates do not bind "
                          "the 56e66f07a reference (dc15)"}
        else:
            conditions = _maintenance_conditions(
                metrics["operations"], window["start"], window["end"],
                retention)
            if "invalid" in conditions and not record.get("invalid"):
                record["invalid"] = conditions["invalid"]
            snaps = window.get("recordSnapshots") or {}
            lost = _maintenance_completeness(
                metrics["operations"], snaps.get("start"), snaps.get("end"))
            conditions["completeness"] = {"complete": not lost,
                                          "problems": lost,
                                          "recordSnapshots": snaps}
            if lost and not record.get("invalid"):
                record["invalid"] = "operation records: " + "; ".join(lost)
            record["maintenance"] = conditions
        record["i4"] = WRITE_BUDGET_I4_NOTE
    if capture:
        record["diagnostics"] = window["diagnostics"]
        # #937 SD8: the poll times the collector's tail edge used, so the
        # loader recomputes completeness exactly as the capture judged it.
        record["polledAt"] = [s["t"] for s in window["samples"]]
    return record


def _requested_arms(leg: dict) -> list:
    requested = leg.get("requestedArms")
    return list(requested if requested is not None else (leg.get("arms") or {}))


def full_build_gate(leg: dict) -> dict:
    """Per arm, the Q18 full-build evaluation. Pure: `leg` is the receipt.
    Gated arms compare the candidate's warm p50/p95 with the qualified
    reference's for the same arm (exact rational compare: <= 6/5, equality
    passes); idle reports not applicable on complete evidence."""
    reference = leg.get("baselineReference")
    reference_ok = isinstance(reference, dict) and reference.get("valid") is True
    # #937 (01a1c114 Q1): a usable reference qualifies arm by arm.
    usable = isinstance(reference, dict) and reference.get("usable") is True
    out = {}
    for name, arm in (leg.get("arms") or {}).items():
        if not isinstance(arm, dict) or arm.get("invalid") or "metrics" not in arm:
            continue
        metrics = arm["metrics"]
        full = metrics.get("fullBuild")
        if name not in WRITE_BUDGET_GATED_ARMS:
            if not isinstance(full, dict) or full.get("complete") is not True:
                out[name] = {"gated": False, "valid": False, "problems": list(
                    (full or {}).get("problems")
                    or ["no full-build instrumentation"])}
            elif not full.get("n"):
                out[name] = {"gated": False, "valid": True,
                             "applicable": False, "n": 0, "p50Ms": None,
                             "p95Ms": None,
                             "reason": "no warm full build in the idle window "
                                       "(complete tick evidence)"}
            else:
                out[name] = {"gated": False, "valid": True, "applicable": True,
                             "n": full["n"],
                             "p50Ms": metrics.get("fullBuildP50Ms"),
                             "p95Ms": metrics.get("fullBuildP95Ms"),
                             "reason": "idle is not a gated arm; absolute "
                                       "values reported"}
            continue
        candidate = {"n": (full or {}).get("n"),
                     "p50Ms": metrics.get("fullBuildP50Ms"),
                     "p95Ms": metrics.get("fullBuildP95Ms")}
        problems = []
        if not isinstance(full, dict):
            problems.append("no full-build instrumentation")
        else:
            if full.get("complete") is not True:
                problems.extend(full.get("problems") or ["incomplete tick evidence"])
            if not full.get("n"):
                problems.append("no warm full build in the window")
        for q in ("p50Ms", "p95Ms"):
            if not _positive_finite(candidate[q]):
                problems.append(f"candidate {q} {candidate[q]!r} is missing, "
                                "nonfinite or nonpositive")
        ref_arm = ((reference.get("arms") or {}).get(name)
                   if reference_ok or usable else None)
        uncertified = (not reference_ok and isinstance(ref_arm, dict)
                       and ref_arm.get("population") == "empty")
        if (not reference_ok and isinstance(ref_arm, dict)
                and ref_arm.get("qualified") is not True):
            ref_arm = None
        matched = None
        if uncertified:
            # INVALID whatever the candidate's samples (01a1c114 Q3).
            problems.append(WRITE_BUDGET_UNCERTIFIED)
        elif not isinstance(ref_arm, dict):
            problems.append("no qualified 56e66f07a reference for this arm")
        else:
            matched = {key: ref_arm.get(key) for key in ("n", "p50Ms", "p95Ms")}
            for q in ("p50Ms", "p95Ms"):
                if not _positive_finite(matched[q]):
                    problems.append(f"reference {q} {matched[q]!r} is missing, "
                                    "nonfinite or nonpositive")
        entry = {"gated": True, "valid": not problems, "problems": problems,
                 "factor": WRITE_BUDGET_FULL_BUILD_FACTOR,
                 "candidate": candidate, "reference": matched,
                 "ratios": None, "failures": []}
        if uncertified:
            entry["uncertified"] = True
        if not problems:
            entry["ratios"] = {q: candidate[q] / matched[q]
                               for q in ("p50Ms", "p95Ms")}
            entry["failures"] = [
                q for q in ("p50Ms", "p95Ms")
                if fractions.Fraction(candidate[q])
                > _WRITE_BUDGET_FACTOR * fractions.Fraction(matched[q])]
        out[name] = entry
    return out


def write_budget_invalid_reasons(leg: dict) -> "list[str]":
    """Why the leg cannot carry a verdict ([] when it can): an invalid arm,
    a missing or unqualified reference while a gated arm is requested, or
    incomplete or empty full-build evidence. Pure."""
    reasons = [f"{name}: {arm['invalid']}"
               for name, arm in (leg.get("arms") or {}).items()
               if isinstance(arm, dict) and arm.get("invalid")]
    # Amendment 19 HR-21 / HR-8: a failed write counter, or a window the
    # candidate kernel cannot qualify, is missing evidence - INVALID, never
    # a FAIL.
    reasons.extend(_steady_invalid_reasons(leg))
    attestation = leg.get("imageAttestation")
    if isinstance(attestation, dict) and attestation.get("problems"):
        reasons.append("imageAttestation: "
                       + "; ".join(attestation["problems"]))
    reasons.extend(f"job: {problem}" for problem in leg.get("jobProblems") or [])
    if any(name in WRITE_BUDGET_GATED_ARMS for name in _requested_arms(leg)):
        reference = leg.get("baselineReference")
        if not isinstance(reference, dict):
            reasons.append("baselineReference: missing (capture one with "
                           "--capture-baseline-reference; dc15 F1)")
        elif reference.get("valid") is not True:
            reasons.append("baselineReference: " + "; ".join(
                reference.get("problems") or ["invalid"]))
    for name, gate in full_build_gate(leg).items():
        if not gate["valid"]:
            reasons.append(f"{name}: full build: "
                           + "; ".join(gate["problems"]))
    return reasons


def _steady_invalid_reasons(leg: dict) -> "list[str]":
    out = []
    for name, arm in (leg.get("arms") or {}).items():
        if not isinstance(arm, dict) or arm.get("invalid") or "metrics" not in arm:
            continue
        metrics = arm["metrics"]
        if metrics.get("counterStatus") != "ok":
            out.append(f"{name}: write counter {metrics.get('counterStatus')}")
            continue
        steady = metrics.get("steady")
        if not isinstance(steady, dict):
            out.append(f"{name}: no kernel steady statistic")
        elif not steady.get("valid"):
            out.append(f"{name}: " + "; ".join(steady.get("problems")
                                                 or ["the kernel's statistic "
                                                     "is invalid"]))
    return out


def evaluate_write_budget_leg(leg: dict) -> list:
    """Every gate the leg applies. Pure: `leg` is the receipt."""
    limits = leg["limits"]
    problems = []
    if any(name in WRITE_BUDGET_GATED_ARMS for name in _requested_arms(leg)):
        reference = leg.get("baselineReference")
        if not isinstance(reference, dict) or reference.get("valid") is not True:
            problems.append("baseline reference INVALID: " + "; ".join(
                (reference or {}).get("problems") or ["missing"]))
    gates = full_build_gate(leg)
    for name in WRITE_BUDGET_ARMS:
        arm = (leg.get("arms") or {}).get(name)
        if arm is None:
            problems.append(f"{name}: arm missing")
            continue
        if arm.get("invalid"):
            problems.append(f"{name}: INVALID: {arm['invalid']}")
            continue
        metrics = arm["metrics"]
        gate = gates.get(name)
        if gate is not None and not gate["valid"]:
            problems.append(f"{name}: INVALID: full build: "
                            + "; ".join(gate["problems"]))
        elif gate is not None and gate["gated"]:
            for q in gate["failures"]:
                field = "fullBuild" + q[0].upper() + q[1:]
                problems.append(
                    f"{name}: {field} {gate['candidate'][q]:.0f} over 1.2 x "
                    f"the 56e66f07a reference {gate['reference'][q]:.0f} "
                    f"(ratio {gate['ratios'][q]:.3f})")
        if metrics["counterStatus"] != "ok":
            problems.append(f"{name}: INVALID: write counter "
                            f"{metrics['counterStatus']}")
            continue
        steady = metrics.get("steady")
        if not isinstance(steady, dict) or not steady.get("valid"):
            problems.append(f"{name}: INVALID: " + "; ".join(
                (steady or {}).get("problems") or ["no kernel steady statistic"]))
            continue
        worst = metrics["worstBytesPerMinute"]
        if worst is None:
            problems.append(f"{name}: no five-minute window")
        elif worst > limits["bytesPerMinute"]:
            problems.append(f"{name}: {worst:.0f} B/min over the "
                            f"{limits['bytesPerMinute']} B/min limit")
        per_pub = metrics["worstBytesPerPublication"]
        if per_pub is not None and per_pub > limits["bytesPerPublication"]:
            problems.append(f"{name}: {per_pub:.0f} B/publication over the "
                            f"{limits['bytesPerPublication']} limit")
        if metrics["rssMaxBytes"] > PROCESS_CEILING_BYTES:
            problems.append(f"{name}: RSS {metrics['rssMaxBytes']} over ceiling")
        if metrics["rssSlopeBytesPerSecond"] > RSS_SLOPE_CEILING_BYTES_PER_SECOND:
            problems.append(f"{name}: RSS slope over ceiling")
        value = metrics.get("apiP95Ms")
        if value is not None and value > API_P95_CEILING_MS:
            problems.append(f"{name}: apiP95Ms {value:.0f} over "
                            f"{API_P95_CEILING_MS:.0f}")
        if name == "backlog":
            problems.extend(f"backlog: {p}"
                            for p in arm["maintenance"]["problems"])
    return problems


# ── the reference: capture once, reuse while it qualifies (dc15 F1) ───────

def _reference_evidence_dir(output: pathlib.Path) -> pathlib.Path:
    return output.with_name(output.name + ".evidence")


def _reference_arm(record: dict, evidence: pathlib.Path,
                   base: pathlib.Path) -> dict:
    """One captured arm as the reference keeps it: the warm population, the
    window, admission, teardown and write evidence, and its raw diagnostics
    (and dashboard log) as digest-bound artifacts beside the reference."""
    diagnostics = record.get("diagnostics")
    arm = {key: value for key, value in record.items()
           if key not in ("diagnostics", "polledAt")}
    arm["fullBuild"] = (record.get("metrics") or {}).get("fullBuild")
    # dc15 F1: the baseline is expected to fail the write budget (that is
    # the #901 defect); its write evidence is recorded, never judged, and
    # never invalidates otherwise valid latency evidence.
    arm["writeBudgetJudged"] = False
    artifacts = {}

    def keep(name, raw: bytes):
        path = evidence / record["arm"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {"path": path.relative_to(base).as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest()}
    if diagnostics is not None:
        body = {"window": record.get("window"), "diagnostics": diagnostics}
        if record.get("polledAt") is not None:
            body["polledAt"] = record["polledAt"]
        artifacts["diagnostics"] = keep("diagnostics.json", json.dumps(
            body, sort_keys=True, allow_nan=False).encode())
    # v2: the dashboard log lives under the arm's evidence root, which
    # outlives the detached image (spec §5.2).
    log_dir = record.get("evidenceRoot") or record.get("root")
    log = pathlib.Path(log_dir) / "regime-dashboard.log" if log_dir else None
    if log is not None and log.is_file():
        artifacts["dashboardLog"] = keep("regime-dashboard.log",
                                         log.read_bytes())
    arm["artifacts"] = artifacts
    if record.get("invalid"):
        problems = [str(record["invalid"])]
    else:
        problems = _reference_population_problems(arm["fullBuild"])
        if diagnostics is None:
            problems.append("no raw diagnostics were kept")
    arm["valid"] = not problems
    arm["problems"] = problems
    return arm


def capture_write_budget_reference(args, harness_sha: str) -> "tuple[dict, int]":
    """Capture the qualified `56e66f07a` reference (dc15 F1): one five-minute
    append and one thirty-minute backlog arm, serially, once each, with the
    leg's unchanged setup, admission deadline and retention rewind, on
    separately prepared clones under `--root` (never extra backlog). Refuses
    before any work unless `--output` is a new file inside `--root` and the
    root is on external storage. An invalid append stops the capture: the
    expensive backlog is never run for a reference that is already invalid,
    and nothing is ever repeated automatically. Exit 0 valid, 2 otherwise."""
    root = pathlib.Path(args.root).expanduser().resolve()
    output = (pathlib.Path(args.output).expanduser().resolve()
              if getattr(args, "output", None) else None)
    problems = []
    if output is None:
        problems.append("--output (the reference file, inside --root) is "
                        "required")
    else:
        if root not in output.parents:
            problems.append(f"the reference {output} is not inside --root "
                            f"{root}: the capture writes only under its root")
        if output.exists() or _reference_evidence_dir(output).exists():
            problems.append(f"{output} or its evidence directory exists; a "
                            "reference is never overwritten")
    storage = _storage_identity(root)
    if storage.get("class") != "external":
        problems.append(f"--root {root} is on {storage.get('class')} storage "
                        f"({storage.get('mountPoint')}); stores, temp files "
                        "and evidence go on the external drive")
    # Preparation v2: the reference measures clones of the baseline tree's
    # own sealed P1 image, which must attest before any work.
    p1_receipt = _image_receipt_of(args)
    bundle = getattr(args, "fixture_image", None)
    problems.extend(f"P1 attestation: {problem}" for problem in
                    _p1_attestation_problems(
                        p1_receipt, expected_tree=WRITE_BUDGET_BASELINE_SHA,
                        p0_seal=((p1_receipt or {}).get("p0") or {}).get("seal"),
                        bundle=bundle))
    if problems:
        return {"schemaVersion": 1, "kind": WRITE_BUDGET_REFERENCE_KIND,
                "status": "refused", "problems": problems}, 2
    root.mkdir(parents=True, exist_ok=True)
    sys.dont_write_bytecode = True
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    bundle = pathlib.Path(bundle).expanduser().resolve()
    canonical = pathlib.Path(getattr(args, "canonical_root", None)
                             or p1_receipt["canonicalRoot"])
    checkout = materialize_checkout_ref(
        WRITE_BUDGET_BASELINE_SHA, root / "baseline-checkout",
        env=_v2_env(_V2Step("reference-git", root)))
    reference = {
        "schemaVersion": 1, "kind": WRITE_BUDGET_REFERENCE_KIND,
        "baselineSha": WRITE_BUDGET_BASELINE_SHA,
        "capturedWithSha": harness_sha, "capturedAt": started,
        "command": {"argv": [str(arg) for arg in sys.argv]},
        "factor": WRITE_BUDGET_FULL_BUILD_FACTOR,
        "qualification": _write_budget_qualification(args),
        "attestation": {
            "treeSha": WRITE_BUDGET_BASELINE_SHA, "bundle": str(bundle),
            "p1ReceiptSha256": hashlib.sha256(pathlib.Path(
                args.image_receipt).expanduser().read_bytes()).hexdigest(),
            "p1Receipt": p1_receipt},
        "arms": {},
    }
    evidence = _reference_evidence_dir(output)
    stamp = _utc_stamp()
    job_problems = []
    job = _V2Job(working_root=root, canonical_root=canonical, kind="full",
                 deadline_s=_soak_image().BudgetWatchdog.KINDS["full"][
                     "seconds"], ledger=root / "budget.json")
    try:
        with job:
            for name in WRITE_BUDGET_GATED_ARMS:
                try:
                    record = _run_write_budget_arm(
                        args, name, p1_bundle=bundle, p1_receipt=p1_receipt,
                        data_root=canonical,
                        evidence_root=root / "evidence" /
                        f"reference-{name}-{stamp}",
                        checkout=checkout, job=job, capture=True)
                except (OSError, RuntimeError, ValueError, sqlite3.Error,
                        subprocess.SubprocessError) as exc:
                    record = {"arm": name,
                              "invalid": f"the reference arm failed: {exc}"}
                reference["arms"][name] = _reference_arm(record, evidence,
                                                         output.parent)
                if not reference["arms"][name]["valid"]:
                    break
    except (OSError, RuntimeError, ValueError, sqlite3.Error,
            subprocess.SubprocessError) as exc:
        job_problems.append(_refusal_text(exc))
    reference["job"] = job.records
    job_problems.extend(f"teardown: {p}" for p in
                        (job.records.get("teardown") or {}).get("problems")
                        or [])
    job_problems.extend(_stop_problems(job))
    reference["finishedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
    reference["problems"] = (
        [f"job: {problem}" for problem in job_problems]
        + [f"{name}: {problem}" for name, arm in reference["arms"].items()
           for problem in arm["problems"]]
        + [f"{name}: not captured (an earlier reference arm was invalid)"
           for name in WRITE_BUDGET_GATED_ARMS if name not in reference["arms"]])
    reference["valid"] = not reference["problems"]
    output.write_text(json.dumps(reference, indent=2, sort_keys=True,
                                 allow_nan=False) + "\n")
    return reference, 0 if reference["valid"] else 2


def _reference_artifact_problems(arm: dict, base: pathlib.Path) -> "list[str]":
    """The diagnostics artifact must be a contained regular file whose digest
    matches, and the collector must recompute the recorded samples from it."""
    artifact = (arm.get("artifacts") or {}).get("diagnostics")
    if not isinstance(artifact, dict):
        return ["no raw diagnostics artifact"]
    relative, digest = artifact.get("path"), artifact.get("sha256")
    parts = pathlib.PurePosixPath(relative).parts if isinstance(relative, str) else ()
    if (not parts or pathlib.PurePosixPath(relative).is_absolute()
            or any(part in (".", "..") for part in parts)):
        return ["the diagnostics artifact path must be relative and contained"]
    path = base.joinpath(*parts)
    if (any(node.is_symlink() for node in (path, *path.parents)
            if base in node.parents) or not path.is_file()
            or not path.resolve().is_relative_to(base)):
        return ["the diagnostics artifact is not a contained regular file"]
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        return ["the diagnostics artifact digest does not match"]
    try:
        data = _strict_json(raw)
    except ValueError as exc:
        return [f"the diagnostics artifact is not valid JSON: {exc}"]
    window = arm.get("window") or {}
    if not all(isinstance(window.get(k), (int, float)) for k in ("start", "end")):
        return ["the reference arm records no measured window"]
    # #937 SD8 (01a1c114 Q2): the window is consistent across the artifact,
    # the arm and its metrics.
    metrics = arm.get("metrics") or {}
    if data.get("window") != window or (metrics.get("start"), metrics.get(
            "end")) != (window["start"], window["end"]):
        return ["the measured window differs between the diagnostics "
                "artifact, the arm and its metrics"]
    polled = data.get("polledAt")
    polled = (polled if isinstance(polled, list) and polled and all(
        _positive_finite(t) for t in polled) else None)
    full = _warm_full_builds(data.get("diagnostics") or [], window["start"],
                             window["end"], polled_at=polled)
    recorded = arm.get("fullBuild") or {}
    if (full["samplesMs"] != recorded.get("samplesMs")
            or full["seqs"] != recorded.get("seqs")):
        return ["the recorded warm samples differ from those recomputed from "
                "the raw diagnostics"]
    # #937 (01a1c114 Q2): completeness is recomputed too, so a recorded
    # `complete` the raw evidence does not support never qualifies an arm.
    if not full["complete"] or full["problems"]:
        return ["the tick evidence recomputed from the raw diagnostics is "
                "incomplete: " + "; ".join(full["problems"])]
    if (full["ticks"] != recorded.get("ticks")
            or full["excluded"] != recorded.get("excluded")):
        return ["the recorded tick and exclusion counts differ from those "
                "recomputed from the raw diagnostics"]
    return []


def load_write_budget_reference(path, qualification) -> dict:
    """Read and qualify a reference for this run (dc15 F1). Valid only with
    the schema, kind and `56e66f07a` identity, a valid capture, every
    qualification field matched, and for append and backlog a complete,
    nonempty, finite and positive population recomputed from its
    digest-bound raw diagnostics. Usable (#937 SD8, per-arm/1) when it is
    valid, or when its sole defect is a demonstrated empty backlog
    population (`_empty_population_gaps`) with the capture's own
    verdict and job records consistent with exactly that; the summary's
    `adjudication` records the policy, the digest and each arm's status."""
    summary = {"path": None if path is None else str(path), "sha256": None,
               "valid": False, "usable": False, "problems": [], "arms": {},
               "qualification": None,
               "adjudication": {"policy": WRITE_BUDGET_REFERENCE_POLICY,
                                "collector": WRITE_BUDGET_COLLECTOR,
                                "referenceSha256": None, "captureValid": None,
                                "qualifiedArms": [], "uncertifiedArms": []}}
    problems = summary["problems"]
    if path is None:
        problems.append("no baseline reference (--baseline-reference); "
                        "capture one with --capture-baseline-reference on "
                        "this host and fixture")
        return summary
    path = pathlib.Path(path).expanduser()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        problems.append(f"cannot read the reference {path}: {exc}")
        return summary
    summary["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        reference = _strict_json(raw)
    except ValueError as exc:
        problems.append(f"the reference is not valid JSON: {exc}")
        return summary
    for key, expected in (("schemaVersion", 1),
                          ("kind", WRITE_BUDGET_REFERENCE_KIND),
                          ("baselineSha", WRITE_BUDGET_BASELINE_SHA)):
        if reference.get(key) != expected:
            problems.append(f"{key} {reference.get(key)!r} is not {expected!r}")
    summary.update(baselineSha=reference.get("baselineSha"),
                   capturedWithSha=reference.get("capturedWithSha"),
                   capturedAt=reference.get("capturedAt"))
    capture_problems = reference.get("problems")
    capture_problems = (list(capture_problems)
                        if isinstance(capture_problems, list) else None)
    mismatches = _qualification_mismatches(reference.get("qualification"),
                                           qualification)
    summary["qualification"] = {"matched": not mismatches,
                                "mismatches": mismatches}
    problems.extend(f"qualification mismatch (a new capture is required): {m}"
                    for m in mismatches)
    # Spec §5.6: the reference's P1 attests on its own (tree 56e66f07a, the
    # shared P0 seal, a seal that still matches the bundle its arms cloned).
    attestation = reference.get("attestation")
    attestation = attestation if isinstance(attestation, dict) else {}
    attested = _p1_attestation_problems(
        attestation.get("p1Receipt"), expected_tree=WRITE_BUDGET_BASELINE_SHA,
        p0_seal=((qualification or {}).get("fixture") or {}).get("p0Seal"),
        bundle=attestation.get("bundle"))
    summary["attestation"] = {"passed": not attested, "problems": attested}
    problems.extend(f"reference P1 attestation: {p}" for p in attested)
    arms = reference.get("arms") if isinstance(reference.get("arms"), dict) else {}
    base = path.resolve().parent
    empty_arms = []
    for name in WRITE_BUDGET_GATED_ARMS:
        arm = arms.get(name)
        if not isinstance(arm, dict):
            problems.append(f"{name}: no reference arm")
            continue
        full = arm.get("fullBuild")
        population = _reference_population_problems(full)
        artifact = _reference_artifact_problems(arm, base)
        gaps = _empty_population_gaps(name, arm, population, artifact)
        empty = gaps == []
        arm_problems = []
        if empty:
            empty_arms.append(name)
        else:
            if arm.get("valid") is not True:
                arm_problems.append("the reference arm is not valid")
            arm_problems.extend(population)
            arm_problems.extend(artifact)
            if gaps:
                arm_problems.append("not a demonstrated empty population: "
                                    + "; ".join(gaps))
        problems.extend(f"{name}: {problem}" for problem in arm_problems)
        if isinstance(full, dict):
            summary["arms"][name] = {key: full.get(key) for key in
                                     ("n", "p50Ms", "p95Ms", "samplesMs")}
            summary["arms"][name].update(
                qualified=not arm_problems and not empty,
                population=("empty" if empty else
                            "invalid" if arm_problems else "qualified"))
    # The capture's own verdict is superseded only when its sole problems
    # are exactly the demonstrated empty populations (no job, teardown or
    # other arm problem); its bytes and `valid: false` stay as recorded.
    if empty_arms:
        # The exception needs a capture that judged itself invalid for
        # exactly these populations, and job records that show no teardown
        # problem and no watchdog stop.
        if reference.get("valid") is not False:
            problems.append("the capture's valid flag is not false although "
                            "a reference population is empty")
        if capture_problems != [f"{name}: {WRITE_BUDGET_EMPTY_POPULATION}"
                                for name in empty_arms]:
            problems.append("the reference capture was not valid: "
                            + "; ".join(capture_problems or ["unknown"]))
        problems.extend(_capture_job_problems(reference.get("job")))
    elif reference.get("valid") is not True:
        problems.append("the reference capture was not valid: " + "; ".join(
            capture_problems or ["unknown"]))
    summary["usable"] = not problems
    problems.extend(f"{name}: {WRITE_BUDGET_UNCERTIFIED}" for name in empty_arms)
    summary["valid"] = not problems
    summary["adjudication"] = {
        "policy": WRITE_BUDGET_REFERENCE_POLICY,
        "collector": WRITE_BUDGET_COLLECTOR,
        "referenceSha256": summary["sha256"],
        "captureValid": reference.get("valid") is True,
        "qualifiedArms": [name for name, arm in summary["arms"].items()
                          if arm.get("qualified") and summary["usable"]],
        "uncertifiedArms": empty_arms}
    return summary


def _empty_population_gaps(name: str, arm: dict, population: list,
                           artifact: list) -> "list[str] | None":
    """The per-arm policy's one recognized defect (#937, 01a1c114 Q2). None
    when the arm is no candidate for it (not backlog, or a population
    problem other than emptiness); [] when it is a demonstrated empty
    population: a backlog arm whose capture judged it solely an empty
    population over its full measured duration, whose recorded evidence is
    complete with no sample, and whose digest-bound raw diagnostics
    recompute the same complete, empty evidence; otherwise why it is not."""
    if (name not in WRITE_BUDGET_EMPTY_POPULATION_ARMS
            or population != [WRITE_BUDGET_EMPTY_POPULATION]):
        return None
    full = arm.get("fullBuild")
    gaps = []
    if artifact:
        gaps.append("its raw diagnostics do not recompute it")
    if not (isinstance(full, dict) and full.get("complete") is True
            and full.get("problems") == [] and full.get("n") == 0
            and full.get("samplesMs") == [] and full.get("seqs") == []
            and full.get("p50Ms") is None and full.get("p95Ms") is None):
        gaps.append("its recorded evidence is not complete and empty")
    if (arm.get("invalid") or arm.get("valid") is not False
            or arm.get("problems") != [WRITE_BUDGET_EMPTY_POPULATION]):
        gaps.append("the capture did not judge it solely an empty population")
    seconds = _window_seconds(arm)
    if seconds < WRITE_BUDGET_SECONDS[name] - WRITE_BUDGET_SAMPLE_SECONDS:
        gaps.append(f"its window is {seconds:.0f} s, shorter than the arm's "
                    f"{WRITE_BUDGET_SECONDS[name]} s")
    return gaps


def _window_seconds(arm: dict) -> float:
    window = arm.get("window") or {}
    start, end = window.get("start"), window.get("end")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) for v in (start, end)):
        return 0.0
    return end - start


def _capture_job_problems(job) -> "list[str]":
    """A capture's job records cross-checked (#937 SD8): a teardown problem
    or a watchdog stop disqualifies the empty-population exception even when
    the capture's own problem list omits it."""
    if not isinstance(job, dict):
        return ["the capture records no job"]
    out = [f"the capture's job teardown recorded: {p}"
           for p in (job.get("teardown") or {}).get("problems") or []]
    stop = (job.get("watchdog") or {}).get("stopReason")
    if stop:
        out.append(f"the capture's budget watchdog stopped it: {stop}")
    return out


def run_write_budget_leg(args, candidate_sha: str) -> dict:
    checkout = pathlib.Path(args.checkout or REPO).expanduser().resolve()
    budget = _candidate_module(checkout, "_lib_write_budget")
    limits = budget.validate_limits(budget.LIMITS)
    arms = [a for a in args.write_budget_arms.split(",") if a]
    unknown = sorted(set(arms) - set(WRITE_BUDGET_ARMS))
    if unknown:
        raise ValueError(f"unknown write-budget arms: {unknown}")
    leg = {"schemaVersion": 1, "kind": "writeBudgetLeg",
           "candidateSha": candidate_sha, "i4": WRITE_BUDGET_I4_NOTE,
           "policyVersion": limits.policy_version,
           "limits": {"bytesPerMinute": limits.bytes_per_minute,
                      "bytesPerPublication": limits.bytes_per_publication},
           "fullBuildFactor": WRITE_BUDGET_FULL_BUILD_FACTOR,
           "requestedArms": arms, "baselineReference": None, "arms": {}}
    if any(name in WRITE_BUDGET_GATED_ARMS for name in arms):
        # Qualify the reference BEFORE any arm spends the fixture's writes:
        # a leg that cannot be judged is INVALID without running (dc15 F1),
        # except on a usable reference (#937 SD8), whose arms still run.
        leg["qualification"] = _write_budget_qualification(args)
        leg["baselineReference"] = load_write_budget_reference(
            getattr(args, "baseline_reference", None), leg["qualification"])
    # Spec §5.6: the candidate's own P1 attests (tree = this run's tree,
    # the shared P0 seal, a seal matching the bundle the arms clone).
    p1_receipt = _image_receipt_of(args)
    bundle = getattr(args, "fixture_image", None)
    shared = (leg.get("qualification") or {}).get("fixture") or {}
    p0_seal = shared.get("p0Seal") if "qualification" in leg else \
        ((p1_receipt or {}).get("p0") or {}).get("seal")
    leg["imageAttestation"] = {
        "treeSha": candidate_sha, "bundle": None if bundle is None
        else str(bundle), "problems": _p1_attestation_problems(
            p1_receipt, expected_tree=candidate_sha, p0_seal=p0_seal,
            bundle=bundle)}
    reference = leg["baselineReference"]
    # #937 (01a1c114 Q1): a usable reference (its only defect a demonstrated
    # empty backlog population) runs every requested arm; the leg still
    # cannot carry a verdict.
    if ((reference is None or reference["valid"]
         or reference.get("usable") is True)
            and not leg["imageAttestation"]["problems"] and arms):
        root = pathlib.Path(args.root).expanduser().resolve()
        canonical = pathlib.Path(getattr(args, "canonical_root", None)
                                 or p1_receipt["canonicalRoot"])
        stamp = _utc_stamp()
        job = _V2Job(working_root=root, canonical_root=canonical,
                     kind="full", deadline_s=_soak_image().BudgetWatchdog.KINDS[
                         "full"]["seconds"], ledger=root / "budget.json")
        leg["jobProblems"] = []
        try:
            with job:
                for name in arms:
                    leg["arms"][name] = _run_write_budget_arm(
                        args, name, p1_bundle=pathlib.Path(bundle).expanduser(),
                        p1_receipt=p1_receipt, data_root=canonical,
                        evidence_root=root / "evidence" /
                        f"candidate-{name}-{stamp}",
                        checkout=checkout, job=job)
        except (OSError, RuntimeError, ValueError, sqlite3.Error,
                subprocess.SubprocessError) as exc:
            leg["jobProblems"].append(_refusal_text(exc))
        leg["job"] = job.records
        leg["jobProblems"].extend(
            f"teardown: {p}" for p in
            (job.records.get("teardown") or {}).get("problems") or [])
        leg["jobProblems"].extend(_stop_problems(job))
    leg["fullBuildGate"] = full_build_gate(leg)
    leg["invalidReasons"] = write_budget_invalid_reasons(leg)
    leg["invalid"] = sorted({reason.split(":", 1)[0]
                             for reason in leg["invalidReasons"]})
    leg["problems"] = evaluate_write_budget_leg(leg)
    return leg


# ── #937: write-budget preparation v2 (spec §5.2-§5.7) ─────────────────────
# P0, once: the operator fixture arrives as a pax tar stream on stdin, is
# extracted into an APFS sparse-bundle image attached at its canonical root C
# (the fixture marker's root), normalized there by its producer with a pinned
# retention clock, compared with the retained stores and sealed. P1, once per
# tree: a clonefile(2) of P0 upgraded by that tree into its settled state and
# sealed on the device it presents. P2, per arm: a clonefile(2) of the tree's
# P1 attached with exactly the sealed identity and admitted by catchup's image
# mode. `bench/soak_image.py` owns the image lifecycle, seals, inventories,
# the budget watchdog, the host lock and P0's comparator; this section owns
# the flows. Data lives under C (inside the image); evidence lives under
# W/evidence/<step>/ on the backing volume and outlives every attach.

P0_PRODUCER_SHA = "4892abbec595a26cc47402671304fc1ac6339264"
P0_STATS_USER_VERSION = 1015
P0_SOURCE_FILES = 31_112
#: F17: the fixture's recorded `reclaim_pending.attempted_at`, the clock of
#: the one forced prune that produced its retained population.
P0_PRUNE_NOW_UTC = dt.datetime(2026, 9, 22, 16, 6, 30, 341988,
                               tzinfo=dt.timezone.utc)
P0_RETENTION_DAYS = 30
P0_LAST_PRUNE_AT = "2026-09-20T16:06:36.153568+00:00"
#: #857's recorded metadata fingerprint (2d3620b6...51b3): provenance only.
P0_857_FINGERPRINT_EDGES = ("2d3620b6", "51b3")
P1_DEADLINE_SECONDS = 7200
P1_STATS_REBUILD_SECONDS = 3600
P0_RECEIPT_KIND = "writeBudgetFixtureImage"
#: #937 I4: P0's sealed checkpoints, after the verified extraction and after
#: the producer's rebuild, so a later failure never re-streams or re-rebuilds.
P0_CHECKPOINT_KIND = "writeBudgetFixtureCheckpoint"
P0_CHECKPOINT_STAGES = ("extracted", "rebuilt")
P1_RECEIPT_KIND = "writeBudgetTreeImage"
#: The image every v2 step attaches (spec §5.2), as the qualification binds it.
V2_IMAGE_DESCRIPTOR = {"type": "SPARSEBUNDLE", "fs": "APFS",
                       "caseSensitive": False, "sizeGiB": 300,
                       "attach": "hdiutil attach -nobrowse -noautoopen "
                                 "-owners on -mountpoint C"}
_SIDECAR = ".segment-summaries"
_SOAK_IMAGE = None
#: The running `_V2Job`'s owner token, which `_v2_env` gives every child
#: (#937 SD7); None outside a job.
_V2_OWNER_TOKEN = None


def _soak_image():
    """bench/soak_image.py, loaded once by path."""
    global _SOAK_IMAGE
    if _SOAK_IMAGE is None:
        path = pathlib.Path(__file__).resolve().parent / "soak_image.py"
        loader = importlib.machinery.SourceFileLoader("soak_image", str(path))
        spec = importlib.util.spec_from_loader("soak_image", loader)
        module = importlib.util.module_from_spec(spec)
        # dataclasses resolve their module through sys.modules
        sys.modules["soak_image"] = module
        loader.exec_module(module)
        _SOAK_IMAGE = module
    return _SOAK_IMAGE


_CATCHUP_MODULE = None


def _catchup_module():
    """catchup.py's pure state readers (retained_state), loaded once."""
    global _CATCHUP_MODULE
    if _CATCHUP_MODULE is None:
        _CATCHUP_MODULE = _soak_image()._catchup()
    return _CATCHUP_MODULE


class _V2Step(tuple):
    """One v2 step's identity for its children: its name (W/tmp/<name>/), the
    working root W, and the data root C when the child is a product process.
    (A plain tuple: this script is loaded by path without a sys.modules entry,
    which a dataclass's annotation resolution needs.)"""
    __slots__ = ()

    def __new__(cls, name, working_root, data_root=None):
        return super().__new__(cls, (name, working_root, data_root))

    name = property(lambda self: self[0])
    working_root = property(lambda self: self[1])
    data_root = property(lambda self: self[2])


def _v2_env(step) -> dict:
    """The single constructor of a v2 child's environment (spec §5.2, Global
    Constraint 7): TMPDIR=W/tmp/<step>/ and PYTHONDONTWRITEBYTECODE=1 for every
    child (preparation, oracle, hdiutil, git, tar), plus `_fixture_env(C)`'s
    HOME, CLAUDE_CONFIG_DIR, CODEX_HOME and CCTALLY_DATA_DIR for a product
    child, through `_child_env` (shared with v1's `_write_budget_env`). Inside
    a `_V2Job` every child also inherits the job's owner token (#937 SD7),
    which the product's detached workers inherit in turn. The working root
    must be on external storage."""
    si = _soak_image()
    working = pathlib.Path(step.working_root)
    storage = _storage_identity(working)
    if storage.get("class") != "external":
        raise si.ImageRefusal(
            f"the working root {working} is on {storage.get('class')} storage "
            f"({storage.get('mountPoint')}), not external",
            "put the working root on an external volume: the image, temp "
            "files, bytecode and evidence never go to internal storage")
    env = _child_env(step.data_root, working / "tmp" / step.name)
    if _V2_OWNER_TOKEN is not None:
        env[si.OWNER_TOKEN_ENV] = _V2_OWNER_TOKEN
    return env


def _utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_new_json(path: pathlib.Path, payload: dict) -> None:
    """A receipt, written once: an existing path is never overwritten."""
    data = (json.dumps(payload, indent=2, sort_keys=True, default=str,
                       allow_nan=False) + "\n").encode()
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


# -- tree children: a separate interpreter bound to one tree's bin/ --------

#: Production runs every tree child isolated (`python3 -I`); the prelude also
#: sets `sys.path` to the tree's bin/ plus the standard library, so no other
#: tree's or site's module can answer an import.
_TREE_CHILD_FLAGS = ("-I",)
_TREE_CHILD_PRELUDE = r'''
import importlib, importlib.machinery, importlib.util, json, os, sys, sysconfig
_bin = os.path.realpath(sys.argv[1])
_std = [os.path.realpath(sysconfig.get_path(k)) for k in ("stdlib", "platstdlib")]
def _stdlib(entry):
    real = os.path.realpath(entry)
    return entry.endswith(".zip") or any(
        real == s or real.startswith(s + os.sep) for s in _std)
sys.path[:] = [_bin] + [p for p in sys.path
                        if p and "site-packages" not in p and _stdlib(p)]
def _tree():
    loader = importlib.machinery.SourceFileLoader(
        "cctally", os.path.join(_bin, "cctally"))
    spec = importlib.util.spec_from_loader("cctally", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = module
    loader.exec_module(module)
    # A tree child is its own process owner (#787 / #861); a producer tree
    # older than the store-authority services has no such entry to call.
    own = getattr(sys.modules.get("_cctally_core"), "establish_script_owner",
                  None)
    if own is not None:
        own()
    return module
def _emit(payload):
    payload["sysPath"] = list(sys.path)
    payload["modules"] = {
        name: module.__file__ for name, module in sorted(sys.modules.items())
        if getattr(module, "__file__", None)
        and os.path.realpath(module.__file__).startswith(_bin + os.sep)}
    print("\n" + json.dumps(payload, sort_keys=True, default=str), flush=True)
'''


def _tree_child(checkout, body: str, argv, env: dict, *, run=None,
                timeout=None) -> dict:
    """Run `body` in a separate interpreter bound to `checkout`'s bin/ only
    and return the JSON object it prints last."""
    runner = subprocess.run if run is None else run
    bindir = pathlib.Path(checkout).resolve() / "bin"
    cmd = [sys.executable, *_TREE_CHILD_FLAGS, "-c", _TREE_CHILD_PRELUDE + body,
           str(bindir), *(str(arg) for arg in argv)]
    done = runner(cmd, env=env, capture_output=True, text=True,
                  timeout=timeout)
    lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
    if done.returncode != 0 or not lines:
        raise RuntimeError(f"tree child of {bindir} exited {done.returncode}: "
                           f"{(done.stderr or '').strip()[-1500:]}")
    return json.loads(lines[-1])


_PINNED_PRUNE_BODY = r'''
import dataclasses, datetime
now = datetime.datetime.fromisoformat(sys.argv[2])
days = int(sys.argv[3])
c = _tree()
cache = c._load_sibling("_cctally_cache")
retention = importlib.import_module("_lib_conversation_retention")
conn = cache.open_conversations_db(attach_cache=False)
try:
    if conn.in_transaction:
        raise SystemExit("the dedicated connection has an open transaction")
    result = retention._maybe_prune_conversation_retention(
        conn, now_utc=now, retention_days=days, force=True)
finally:
    conn.close()
if result is not None:
    result = (dataclasses.asdict(result) if dataclasses.is_dataclass(result)
              else dict(vars(result)))
_emit({"result": result, "nowUtc": now.isoformat(), "retentionDays": days})
'''


def _pinned_prune_contract_problems(now_utc, retention_days) -> "list[str]":
    """P0 step 5's clock contract (F17): an aware datetime equal to the
    fixture's recorded prune instant, and the fixture's 30 days."""
    problems = []
    if (not isinstance(now_utc, dt.datetime) or now_utc.tzinfo is None
            or now_utc.utcoffset() is None):
        problems.append(f"the clock {now_utc!r} is not an aware datetime")
    elif now_utc != P0_PRUNE_NOW_UTC:
        problems.append(
            f"the clock {now_utc.isoformat()} is not the fixture's recorded "
            f"prune instant {P0_PRUNE_NOW_UTC.isoformat()}; another clock "
            "prunes another population")
    if retention_days != P0_RETENTION_DAYS:
        problems.append(f"retention_days {retention_days!r} is not the "
                        f"fixture's {P0_RETENTION_DAYS}")
    return problems


def _pinned_retention_prune(producer_checkout: pathlib.Path, env: dict, *,
                            now_utc: dt.datetime, retention_days: int,
                            run=None) -> dict:
    """Spec §5.3 step 5: in a separate process bound to the producer's bin/
    only, the producer's own `_maybe_prune_conversation_retention` on a
    dedicated `open_conversations_db(attach_cache=False)` connection, forced,
    on the pinned clock. A skipped prune (None: another holder had the
    maintenance or a provider lock) refuses, as does any other clock."""
    problems = _pinned_prune_contract_problems(now_utc, retention_days)
    if problems:
        raise ValueError("the pinned prune refuses: " + "; ".join(problems))
    out = _tree_child(producer_checkout, _PINNED_PRUNE_BODY,
                      [now_utc.isoformat(), retention_days], env, run=run)
    if out.get("result") is None:
        raise RuntimeError("the pinned prune was skipped: another holder had "
                           "the maintenance or a provider lock")
    out["checkout"] = str(producer_checkout)
    return out


_SEGMENT_SUMMARY_BODY = r'''
lss = importlib.import_module("_lib_segment_summary")
path = os.path.join(sys.argv[2], lss.SIDECAR_NAME)
present = os.path.lexists(path)
summaries = lss.read_sidecar(path) if present else None
lj = importlib.import_module("_lib_journal")
segments = sorted(
    name for name in os.listdir(sys.argv[2])
    if name.endswith(".jsonl")
    and (name.startswith(lj.BOOTSTRAP_PREFIX)
         or name.startswith(lj.SEGMENT_PREFIX))
    and os.path.isfile(os.path.join(sys.argv[2], name)))
_emit({"sidecar": path, "present": present, "readable": summaries is not None,
       "segments": segments,
       "summaries": {name: {"st_dev": s.st_dev, "st_ino": s.st_ino}
                     for name, s in (summaries or {}).items()}})
'''


def _segment_summary_identity_problems(tree_checkout: pathlib.Path,
                                       journal_dir: pathlib.Path, *,
                                       env: "dict | None" = None,
                                       run=None) -> "list[str]":
    """Spec §5.4 step 4: the sidecar decoded through the tree's own
    `_lib_segment_summary`; every persisted summary's (st_dev, st_ino) must
    equal its segment's current `os.stat`, and every journal segment (named
    by the tree's own `_lib_journal` prefixes) must have a summary (#937 F5:
    a segment without one passed). An unreadable sidecar is a problem too."""
    journal_dir = pathlib.Path(journal_dir)
    if env is None:
        env = {**os.environ, "CCTALLY_DATA_DIR": str(journal_dir.parent),
               "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
               "PYTHONDONTWRITEBYTECODE": "1"}
    decoded = _tree_child(tree_checkout, _SEGMENT_SUMMARY_BODY, [journal_dir],
                          env, run=run)
    problems = []
    if decoded.get("present") and not decoded.get("readable"):
        problems.append(f"the sidecar {decoded.get('sidecar')} is unreadable "
                        "by the tree's reader")
    summarized = decoded.get("summaries") or {}
    if "segments" not in decoded:
        problems.append("the tree child listed no journal segments")
    for name in decoded.get("segments") or ():
        if name not in summarized:
            problems.append(f"{name}: the journal segment has no summary")
    for name, identity in sorted(summarized.items()):
        try:
            st = os.stat(journal_dir / name)
        except FileNotFoundError:
            problems.append(f"{name}: a summary names a missing segment")
            continue
        current = [st.st_dev, st.st_ino]
        recorded = [identity.get("st_dev"), identity.get("st_ino")]
        if recorded != current:
            problems.append(f"{name}: summary identity (st_dev, st_ino) "
                            f"{recorded} differs from the segment's {current}")
    return problems


def _settle_segment_summaries(tree_checkout: pathlib.Path,
                              journal_dir: pathlib.Path, rebuild, *,
                              env: "dict | None" = None, run=None) -> dict:
    """After the stats rebuild: check the summaries' identities; when any is
    stale, remove the sidecar once (a pure re-derivable cache, inside the
    isolated image), call `rebuild()` and check again. Still stale refuses
    and names the segment (spec §5.4 step 4, D2)."""
    journal_dir = pathlib.Path(journal_dir)
    first = _segment_summary_identity_problems(tree_checkout, journal_dir,
                                               env=env, run=run)
    record = {"checked": True, "initialProblems": first, "removed": False,
              "recovered": False, "problems": []}
    if not first:
        return record
    sidecar = journal_dir / _SIDECAR
    if os.path.lexists(sidecar):
        os.unlink(sidecar)
        record["removed"] = True
    rebuild()
    second = _segment_summary_identity_problems(tree_checkout, journal_dir,
                                                env=env, run=run)
    record["problems"] = second
    if second:
        raise RuntimeError("segment summaries are still stale after one "
                           "remove-and-rebuild: " + "; ".join(second[:5]))
    record["recovered"] = True
    return record


_P1_CURSOR_FAMILIES = ("session_files", "conversation_source_files",
                       "codex_session_files", "codex_conversation_source_files")


#: The cursor families each pending-maintenance key re-reads (a reingest,
#: rebuild, rewalk or replay of that family's sources), by EXACT name (#937
#: F12). A key whose work is derived-only (aggregates, titles, search,
#: projections, the replay's blocked/deferred states) re-reads no cursor
#: family. A key missing here is unknown: P1 refuses and names it.
P1_PENDING_KEY_FAMILIES = {
    "cache_creation_split_rewalk_pending": ("session_files",),
    "conversation_rebuild_claude_pending": ("conversation_source_files",),
    "conversation_backfill_pending": ("conversation_source_files",),
    "conversation_reingest_pending": ("conversation_source_files",),
    "conversation_source_tool_use_reingest_pending": (
        "conversation_source_files",),
    "conversation_reingest_enrichment_pending": ("conversation_source_files",),
    "conversation_media_reingest_pending": ("conversation_source_files",),
    "conversation_queued_prompt_reingest_pending": (
        "conversation_source_files",),
    "conversation_reingest_nested_agent_pending": (
        "conversation_source_files",),
    "conversation_reingest_file_touches_pending": (
        "conversation_source_files",),
    "conversation_background_mcp_reingest_pending": (
        "conversation_source_files",),
    "codex_replay_from_zero_pending": ("codex_session_files",),
    "codex_conversation_replay_from_zero_pending": (
        "codex_conversation_source_files",),
    "conversation_rebuild_codex_pending": ("codex_conversation_source_files",),
    "conversation_sessions_backfill_pending": (),
    "ai_titles_backfill_pending": (),
    "conversation_search_split_pending": (),
    "conversation_promote_command_args_pending": (),
    "conversation_title_fts_backfill_pending": (),
    "codex_find_projection_backfill_pending": (),
    # #1072: the rollup's full re-derive and its deleted-session set rebuild
    # `conversation_sessions` from stored messages and accounting rows only.
    "conversation_sessions_rederive_cursor": (),
    "conversation_sessions_rederive_pending": (),
    "codex_replay_from_zero_blocked": (),
    "codex_replay_from_zero_deferred": (),
}


def _pending_keys_by_family(keys, registry) -> dict:
    """{cursor family: [pending keys re-reading it]} for the pending keys
    step 3's migrations set (spec §5.4 step 7), classified by exact name
    against the tested tree's own maintenance registry (`registry`: its
    `_PENDING_META_KEYS`, `_CONVERSATION_PENDING_META_KEYS` and
    `_TARGETED_DECLINE_FLAGS`) and `P1_PENDING_KEY_FAMILIES`. A key the tree
    does not register, or this table does not classify, refuses with its
    name (#937 F12): a guess could attribute a replay to the wrong family."""
    known = set(registry or ())
    unknown = sorted(key for key in keys
                     if key not in known or key not in P1_PENDING_KEY_FAMILIES)
    if unknown:
        raise RuntimeError(
            "pending key(s) the tested tree's maintenance registry or the "
            f"harness's family table does not name: {unknown}"
            + ("" if known else " (the tree exposed no registry)"))
    out = {}
    for key in sorted(keys):
        for family in P1_PENDING_KEY_FAMILIES[key]:
            out.setdefault(family, []).append(key)
    return out


def _p1_replay_attribution(before_state, after_state, pending_keys) -> dict:
    """Spec §5.4 step 7: every cursor row replay between the pre-migration
    state and the settled state (a new or vanished cursor, an offset below
    the earlier one, a changed inode or incarnation, a changed root key, an
    advanced Codex file incarnation), by family, attributed to the pending
    keys step 3 set for that family. An unattributed replay raises."""
    before_rows = (before_state or {}).get("rows") or {}
    after_rows = (after_state or {}).get("rows") or {}
    replayed = {}

    def note(table, kind, path):
        entry = replayed.setdefault(table, {"count": 0, "kinds": {},
                                            "first": []})
        entry["count"] += 1
        entry["kinds"][kind] = entry["kinds"].get(kind, 0) + 1
        if len(entry["first"]) < 20:
            entry["first"].append(f"{kind}: {path}")

    for key in sorted(set(before_rows) | set(after_rows)):
        a, b = before_rows.get(key), after_rows.get(key)
        row = a or b
        kind = None
        if a is None:
            kind = "new cursor"
        elif b is None:
            kind = "vanished cursor"
        elif int(b.get("last_byte_offset") or 0) < int(a.get("last_byte_offset")
                                                        or 0):
            kind = "reset"
        elif any(column in a and a.get(column) != b.get(column)
                 for column in ("inode", "source_incarnation_id")):
            kind = "new incarnation"
        elif "source_root_key" in a and a.get("source_root_key") != \
                b.get("source_root_key"):
            kind = "root-key change"
        if kind:
            note(row["table"], kind, row["path"])
    inc_a = (before_state or {}).get("incarnations") or {}
    inc_b = (after_state or {}).get("incarnations") or {}
    for identity in sorted(set(inc_a) | set(inc_b)):
        if identity not in inc_a or identity not in inc_b \
                or int(inc_b[identity]) != int(inc_a[identity]):
            note("codex_session_files", "codex file incarnation", identity)
    keys = pending_keys or {}
    unattributed = sorted(table for table in replayed if not keys.get(table))
    result = {"replayed": replayed,
              "attribution": {table: list(keys.get(table) or [])
                              for table in replayed},
              "unattributed": unattributed}
    if unattributed:
        raise RuntimeError(
            f"unattributed replay in {', '.join(unattributed)}: no migration "
            "of the migrating open set a pending key naming that family's "
            "reingest or rebuild ("
            + "; ".join(f"{t}: {replayed[t]['kinds']}" for t in unattributed)
            + ")")
    return result


# -- store helpers ---------------------------------------------------------

def _ro_db(path: pathlib.Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)


def _user_version(path: pathlib.Path) -> int:
    with contextlib.closing(_ro_db(path)) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _meta_value(db: pathlib.Path, key: str) -> "str | None":
    with contextlib.closing(_ro_db(db)) as conn:
        try:
            row = conn.execute("SELECT value FROM cache_meta WHERE key = ?",
                               (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
    return None if row is None else row[0]


def _set_meta(db: pathlib.Path, key: str, value: "str | None") -> None:
    with contextlib.closing(sqlite3.connect(db)) as conn:
        if value is None:
            conn.execute("DELETE FROM cache_meta WHERE key = ?", (key,))
        else:
            conn.execute("INSERT INTO cache_meta(key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, value))
        conn.commit()


def _store_meta(data_dir: pathlib.Path) -> dict:
    """{store: {"migrations": [...], "cacheMeta": {key: value}}}, read-only."""
    out = {}
    for store in ("cache.db", "conversations.db"):
        path = data_dir / store
        if not path.exists():
            continue
        entry = {"migrations": [], "cacheMeta": {}}
        with contextlib.closing(_ro_db(path)) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "schema_migrations" in tables:
                columns = [r[1] for r in conn.execute(
                    "PRAGMA table_info(schema_migrations)")]
                column = "name" if "name" in columns else columns[0]
                entry["migrations"] = sorted(
                    str(r[0]) for r in conn.execute(
                        f"SELECT {column} FROM schema_migrations"))
            if "cache_meta" in tables:
                entry["cacheMeta"] = {str(k): v for k, v in conn.execute(
                    "SELECT key, value FROM cache_meta")}
        out[store] = entry
    return out


def _pending_set_by(before: dict, after: dict) -> dict:
    """The pending keys (and the migrations) the migrating open applied."""
    keys, migrations = {}, {}
    for store, entry in after.items():
        prior = before.get(store) or {"migrations": [], "cacheMeta": {}}
        migrations[store] = sorted(set(entry["migrations"])
                                   - set(prior["migrations"]))
        for key, value in entry["cacheMeta"].items():
            if "pending" in key and prior["cacheMeta"].get(key) != value:
                keys[key] = value
    return {"pendingKeys": keys, "appliedMigrations": migrations}


def _checkpoint_stores(data_dir: pathlib.Path) -> dict:
    """`PRAGMA wal_checkpoint(TRUNCATE)` on each store (quiesce before seal)."""
    out = {}
    for store in ("cache.db", "conversations.db", "stats.db"):
        path = data_dir / store
        if not path.exists():
            continue
        with contextlib.closing(sqlite3.connect(path)) as conn:
            out[store] = list(conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    return out


def _defer_retention(data_dir: pathlib.Path) -> dict:
    """The deferred-retention configuration (as
    `_initialize_deferred_retention_clone` applies it): retention_days 0 and
    the retention stamp now; returns what `_restore_retention` puts back."""
    config_path = data_dir / "config.json"
    original = config_path.read_bytes()
    config = _strict_json(original)
    if not isinstance(config, dict):
        raise ValueError("the fixture's config.json is not an object")
    conversation = config.setdefault("conversation", {})
    if not isinstance(conversation, dict):
        raise ValueError("the fixture's conversation config is not an object")
    days = conversation.get("retention_days")
    conversations = data_dir / "conversations.db"
    stamp = _meta_value(conversations, "conversation_retention_last_prune_at")
    conversation["retention_days"] = 0
    _set_meta(conversations, "conversation_retention_last_prune_at",
              dt.datetime.now(dt.timezone.utc).isoformat())
    config_path.write_text(json.dumps(config, sort_keys=True) + "\n")
    return {"config": original, "stamp": stamp, "retentionDays": days,
            "configSha256": hashlib.sha256(original).hexdigest()}


def _restore_retention(data_dir: pathlib.Path, saved: dict) -> None:
    (data_dir / "config.json").write_bytes(saved["config"])
    _set_meta(data_dir / "conversations.db",
              "conversation_retention_last_prune_at", saved["stamp"])


def _clonefile(src: pathlib.Path, dst: pathlib.Path) -> None:
    """One clonefile(2) of a file (P0's retained originals); no fallback."""
    clone = _soak_image()._libc().clonefile
    clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    clone.restype = ctypes.c_int
    if clone(os.fsencode(str(src)), os.fsencode(str(dst)), 0) != 0:
        err = ctypes.get_errno() or 5
        raise OSError(err, f"clonefile({src}, {dst}) failed: {os.strerror(err)}")


def _image_fixture_fingerprint(root: pathlib.Path) -> str:
    """`_fixture_source_fingerprint` of the attached volume, its housekeeping
    excluded, so it equals the source host's pre-pass over the plain tree."""
    housekeeping = _soak_image().HOUSEKEEPING
    root = pathlib.Path(root)
    digest = hashlib.sha256()
    for walk_root, directories, files in os.walk(root, followlinks=False):
        if pathlib.Path(walk_root) == root:
            directories[:] = [d for d in directories if d not in housekeeping]
            files = [f for f in files if f not in housekeeping]
        directories.sort()
        for name in sorted(files):
            path = pathlib.Path(walk_root) / name
            st = path.stat()
            relative = path.relative_to(root).as_posix()
            digest.update(f"{relative}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def _journal_carriers(data_dir: pathlib.Path) -> dict:
    """F18's device-sensitive persistent carriers, present or absent, with
    their (st_ino, size, mtime_ns), plus the reclaim plan's record."""
    out = {}
    for name in _soak_image().JOURNAL_CARRIERS:
        try:
            st = os.lstat(data_dir / "journal" / name)
            out[name] = {"present": True,
                         "identity": [st.st_ino, st.st_size, st.st_mtime_ns]}
        except FileNotFoundError:
            out[name] = {"present": False}
    conversations = data_dir / "conversations.db"
    out["reclaimRecord"] = {"present": conversations.exists() and _meta_value(
        conversations, "conversation_retention_reclaim_pending") is not None}
    out["backupFiles"] = sorted(p.name for p in data_dir.glob("*.bak*"))
    return out


# -- one v2 job: host lock, budget watchdog, image lifecycle ----------------

def _v2_host_lock() -> int:
    return _soak_image().acquire_host_lock()


def _v2_meter(working_root: pathlib.Path):
    """The budget meter of the volume holding the working root. `diskutil
    info` resolves a volume only by its mount point (a subdirectory exits 1),
    so the meter is built from that mount point."""
    si = _soak_image()
    env = _v2_env(_V2Step("meter", working_root))
    mount = pathlib.Path(working_root).expanduser().resolve()
    while not os.path.ismount(mount) and mount != mount.parent:
        mount = mount.parent
    return si.disk_write_meter(
        mount, run=lambda argv, **kw: subprocess.run(argv, env=env, **kw))


class _V2Refusal(RuntimeError):
    """A v2 step refuses; the job unwinds through its teardown."""


class _V2Job:
    """One v2 job (spec §5.2, §5.7): the runner host lock, held until the
    teardown completes; the budget watchdog, started before the first
    attach; the image lifecycle owner; the processes the steps start. A
    watchdog stop sends SIGTERM to the active children's groups, SIGKILL after
    the kind's grace, and the step then unwinds to the teardown."""

    def __init__(self, *, working_root: pathlib.Path,
                 canonical_root: pathlib.Path, kind: str, deadline_s: float,
                 ledger: pathlib.Path):
        self.si = _soak_image()
        self.working_root = pathlib.Path(working_root)
        self.canonical_root = pathlib.Path(canonical_root)
        self.kind = kind
        self.deadline_s = float(deadline_s)
        self.ledger = pathlib.Path(ledger)
        self.started = None
        self.lock_fd = None
        self.watchdog = None
        self.lifecycle = None
        self.stop_reason = None
        self.records = {}
        self._active = set()
        self._mutex = threading.Lock()
        #: Inherited by every child (`_v2_env`) and so by the product's
        #: detached workers: what proves a volume holder is this job's own.
        self.owner_token = secrets.token_hex(16)

    def __enter__(self):
        global _V2_OWNER_TOKEN
        si = self.si
        self.started = time.monotonic()
        self.lock_fd = _v2_host_lock()
        _V2_OWNER_TOKEN = self.owner_token
        self.records["ownerToken"] = {"env": si.OWNER_TOKEN_ENV,
                                      "token": self.owner_token}
        try:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            self.watchdog = si.BudgetWatchdog(
                kind=self.kind, ledger=self.ledger,
                meter=_v2_meter(self.working_root),
                free_bytes=si.free_bytes_of(self.working_root),
                deadline_s=self.deadline_s, on_stop=self._on_stop)
            self.watchdog.start()
            self.lifecycle = si.ImageLifecycle(
                working_root=self.working_root,
                canonical_root=self.canonical_root,
                env=_v2_env(_V2Step("image", self.working_root)),
                grace_s=FAMILY_TERM_GRACE_SECONDS,
                kill_wait_s=FAMILY_KILL_WAIT_SECONDS,
                owner_token=self.owner_token)
            self.lifecycle.install_signal_handlers()
        except BaseException:
            self._finish()
            raise
        return self

    def __exit__(self, *exc):
        self._finish()
        return False

    def _finish(self) -> None:
        global _V2_OWNER_TOKEN
        try:
            if self.lifecycle is not None:
                self.records["teardown"] = self.lifecycle.close().as_dict()
        finally:
            try:
                if self.watchdog is not None and self.watchdog._started:
                    self.records["watchdog"] = self.watchdog.stop()
            finally:
                if self.lock_fd is not None:
                    os.close(self.lock_fd)
                    self.lock_fd = None
                if _V2_OWNER_TOKEN == self.owner_token:
                    _V2_OWNER_TOKEN = None
        self.records["elapsedS"] = round(time.monotonic() - self.started, 3)

    def remaining(self) -> float:
        return max(0.0, self.deadline_s - (time.monotonic() - self.started))

    def check(self) -> None:
        if self.stop_reason:
            raise _V2Refusal(f"the budget watchdog stopped the job: "
                             f"{self.stop_reason}")
        if self.lifecycle is not None and self.lifecycle.interrupted:
            raise _V2Refusal("the job was interrupted by a signal")

    def _spawned(self, pid: int) -> None:
        with self._mutex:
            self._active.add(pid)
        self.lifecycle.register_process(pid)

    def _exited(self, pid: int) -> None:
        with self._mutex:
            self._active.discard(pid)

    def register(self, pid: int) -> None:
        """A process a step started outside `run` (the dashboard, tar)."""
        self._spawned(pid)

    def _on_stop(self, reason: str) -> None:
        self.stop_reason = reason
        grace = self.watchdog.params()["g"] if self.watchdog else 10

        def signal_all(sig):
            with self._mutex:
                pids = sorted(self._active)
            for pid in pids:
                # #937 F4: only a process whose start identity is still the
                # recorded one; an exited or reused pid is pruned, never
                # signalled.
                recorded = self.lifecycle.recorded_identity(pid)
                if recorded is None or \
                        self.lifecycle.start_identity(pid) != recorded:
                    with self._mutex:
                        self._active.discard(pid)
                    continue
                with contextlib.suppress(OSError):
                    os.killpg(pid, sig)
        signal_all(signal.SIGTERM)
        timer = threading.Timer(grace, signal_all, args=(signal.SIGKILL,))
        timer.daemon = True
        timer.start()

    def run(self, log_dir: pathlib.Path) -> "_MeasuredRun":
        """A `_MeasuredRun` whose children this job records."""
        return _MeasuredRun(log_dir=log_dir, on_spawn=self._spawned,
                            on_exit=self._exited)


def _refusal_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _stop_problems(job) -> "list[str]":
    """#937 F1: a watchdog stop invalidates the job's receipt whenever it
    fired, including during in-process work no `job.check()` followed (an
    equivalence pass, the replay attribution, a checkpoint), so `passed`
    never rests on `job.check()` having raised."""
    reason = getattr(job, "stop_reason", None) or (
        (getattr(job, "records", None) or {}).get("watchdog") or {}).get(
            "stopReason")
    return [f"the budget watchdog stopped the job: {reason}"] if reason else []


def _require_quiet_volume(job, receipt: dict, what: str) -> None:
    """Before a seal or a checkpoint (spec §5.2 teardown steps 2-3): reap
    this job's owned detached workers (#937 SD7), then refuse on any holder
    that is neither recorded nor owned, or on an owned worker that could not
    be reaped. Unknown holders are never signalled."""
    owned, problems = job.lifecycle.reap_owned_workers()
    receipt.setdefault("ownedWorkers", []).extend(owned)
    if problems:
        raise _V2Refusal(f"owned detached workers before {what}: "
                         + "; ".join(problems))
    unknown = [h for h in job.lifecycle.holders() if not h.get("recorded")]
    if unknown:
        raise _V2Refusal(f"holders other than this job before {what}: "
                         f"{[(h['pid'], h.get('command')) for h in unknown]}")


_V2_ERRORS = (RuntimeError, OSError, ValueError, sqlite3.Error,
              subprocess.SubprocessError)


def _product(checkout: pathlib.Path, *argv) -> list:
    return [sys.executable, str(pathlib.Path(checkout) / "bin" / "cctally"),
            *argv]


def _checked(done, what: str):
    if done.returncode != 0:
        raise _V2Refusal(f"{what} exited {done.returncode}: "
                         f"{(done.stderr or done.stdout or '')[-1500:]}")
    return done


def _resolve_commit(ref: str) -> str:
    done = subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                          cwd=REPO, capture_output=True, text=True)
    if done.returncode != 0:
        raise ValueError(f"{ref} is not a commit in {REPO}")
    return done.stdout.strip()


def _new_output_problems(output, working_root: pathlib.Path) -> "list[str]":
    if output is None:
        return ["--output (a new file inside --root) is required"]
    output = pathlib.Path(output).expanduser().resolve()
    problems = []
    if working_root not in output.parents:
        problems.append(f"{output} is not inside --root {working_root}")
    if os.path.lexists(output):
        problems.append(f"{output} exists; a receipt is never overwritten")
    return problems


def _extract_stream(job: "_V2Job", step: "_V2Step", target: pathlib.Path,
                    log_dir: pathlib.Path, *, stdin=None) -> dict:
    """P0 step 2's extraction: `tar -x` of the pax stream on this process's
    stdin into `target`, a recorded child in its own session."""
    log_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with open(log_dir / "tar.stdout", "wb") as out, \
            open(log_dir / "tar.stderr", "wb") as err:
        proc = subprocess.Popen(
            ["tar", "-x", "--no-mac-metadata", "-f", "-", "-C", str(target)],
            stdin=sys.stdin.buffer if stdin is None else stdin, stdout=out,
            stderr=err, env=_v2_env(step), start_new_session=True)
    job.register(proc.pid)
    try:
        while proc.poll() is None:
            if job.stop_reason or job.remaining() <= 0:
                break
            time.sleep(0.5)
    finally:
        teardown = _terminate_family(proc, grace=FAMILY_TERM_GRACE_SECONDS,
                                     group_term=True)
        job._exited(proc.pid)
    job.check()
    if teardown["exitCode"] != 0:
        raise _V2Refusal(f"tar -x exited {teardown['exitCode']}: "
                         + (log_dir / "tar.stderr").read_text(
                             errors="replace")[-1500:])
    return {"exitCode": teardown["exitCode"],
            "durationS": round(time.monotonic() - started, 3),
            "group": teardown["group"]}


def _detach_and_seal(job: "_V2Job", attached, bundle: pathlib.Path,
                     receipt: dict, *, wal_key: str = "checkpoints") -> None:
    """Quiesce, require no holder, detach and seal (spec §5.3 step 8). The
    WAL checkpoint results go to `wal_key` (P0's `checkpoints` is its list
    of sealed stage checkpoints). A watchdog stop or a signal at any point
    before the seal refuses it (#937 F1)."""
    si = job.si
    job.check()
    # Quiet first (#937 SD7): a live owned worker could still be writing
    # C/data, so it is reaped before the WAL checkpoint and the inventory
    # record the state the seal then binds; re-checked after them.
    _require_quiet_volume(job, receipt, "the seal")
    receipt[wal_key] = _checkpoint_stores(job.canonical_root / "data")
    receipt["carriers"] = _journal_carriers(job.canonical_root / "data")
    receipt["sourceInventory"] = si.inventory_record(job.canonical_root)
    _require_quiet_volume(job, receipt, "the seal")
    teardown = job.lifecycle.detach(attached)
    receipt["detach"] = teardown.as_dict()
    if teardown.problems:
        raise _V2Refusal("detach before the seal: "
                         + "; ".join(teardown.problems))
    job.check()
    receipt["seal"] = si.bundle_seal(bundle, content=True)


def _p0_verify_extraction(job: "_V2Job", receipt: dict,
                          expected: dict) -> dict:
    """P0 step 2: census, fingerprint and the two-sided digest of the
    extracted volume, before anything is normalized. Returns the measured
    {"fingerprint", "digest"}; refuses unless all three verify."""
    si, canonical = job.si, job.canonical_root
    inventory = si.source_inventory(canonical)
    zst = sorted(rel for rel in inventory
                 if rel.startswith("codex-") and rel.endswith(".zst"))
    fingerprint = _image_fixture_fingerprint(canonical)
    digest = si._content_digest(canonical)
    receipt["census"] = {"regularFiles": len(inventory),
                         "expected": P0_SOURCE_FILES, "zst": zst,
                         "symlinks": 0}
    receipt["fingerprints"] = {
        "sourceHost": expected["fingerprint"], "image": fingerprint,
        "equal": fingerprint == expected["fingerprint"],
        "matches857": fingerprint.startswith(P0_857_FINGERPRINT_EDGES[0])
        and fingerprint.endswith(P0_857_FINGERPRINT_EDGES[1])}
    receipt["digests"] = {"sourceHost": expected["digest"], "image": digest,
                          "equal": digest["sha256"] == expected["digest"]}
    census = []
    if len(inventory) != P0_SOURCE_FILES:
        census.append(f"{len(inventory)} regular files, not "
                      f"{P0_SOURCE_FILES}")
    if zst:
        census.append(f"{len(zst)} *.zst file(s) under codex-*")
    if not receipt["fingerprints"]["equal"]:
        census.append("the image's fingerprint differs from the source "
                      "host's pre-pass")
    if not receipt["digests"]["equal"]:
        census.append("the image's content digest differs from the "
                      "source host's")
    if census:
        raise _V2Refusal("P0 extraction does not verify: "
                         + "; ".join(census))
    return {"fingerprint": fingerprint, "digest": digest}


def _p0_checkpoint(job: "_V2Job", attached, bundle: pathlib.Path,
                   receipt: dict, stage: str, stamp: str, *,
                   path: pathlib.Path, reattach: bool, facts: dict):
    """A sealed P0 checkpoint (#937 I4): with no unknown holder, detach the
    working bundle, clone it once to W/p0-<stage>-<stamp>.sparsebundle, seal
    the clone by metadata and write the checkpoint JSON at `path`; then
    reattach the working bundle (returned) when `reattach`. The checkpoint
    bundle itself is never attached: a resume works on a fresh clone."""
    si, canonical = job.si, job.canonical_root
    path = pathlib.Path(path)
    clone = job.working_root / f"p0-{stage}-{stamp}.sparsebundle"
    for target in (path, clone):
        if os.path.lexists(target):
            raise _V2Refusal(f"{target} exists; a checkpoint is never "
                             "overwritten")
    # Quiet first (#937 SD7): reap owned workers before the inventories
    # the checkpoint records, and re-check after them.
    _require_quiet_volume(job, receipt, f"the {stage} checkpoint")
    inventory = si.inventory_record(canonical)
    retained = canonical / ".p0-retained"
    retained_inventory = (si.source_inventory(retained)
                          if retained.is_dir() else None)
    _require_quiet_volume(job, receipt, f"the {stage} checkpoint")
    teardown = job.lifecycle.detach(attached)
    receipt.setdefault("checkpointDetaches", []).append(teardown.as_dict())
    if teardown.problems:
        raise _V2Refusal(f"detach before the {stage} checkpoint: "
                         + "; ".join(teardown.problems))
    si.clonefile_bundle(bundle, clone)
    seal = si.bundle_seal(clone, content=False)
    record = {"schemaVersion": 1, "kind": P0_CHECKPOINT_KIND, "passed": True,
              "stage": stage, "createdAt": _utc_now(), "bundle": str(clone),
              "workingBundle": str(bundle), "seal": seal,
              "sourceInventory": inventory,
              "retainedInventory": retained_inventory,
              "stDev": inventory["stDev"], "canonicalRoot": str(canonical),
              "imageDescriptor": dict(V2_IMAGE_DESCRIPTOR),
              "producerSha": P0_PRODUCER_SHA, **facts}
    _write_new_json(path, record)
    receipt["checkpoints"].append({
        "stage": stage, "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "seal": seal["sealSha256"]})
    if not reattach:
        return None
    attached = job.lifecycle.attach(bundle)
    receipt.setdefault("reattached", []).append(attached.as_dict())
    return attached


def _read_p0_checkpoint(path) -> "tuple[dict, str]":
    """A passing P0 checkpoint and the SHA-256 of its bytes; anything else
    raises ValueError."""
    raw = pathlib.Path(path).expanduser().read_bytes()
    checkpoint = _strict_json(raw)
    if (not isinstance(checkpoint, dict)
            or checkpoint.get("kind") != P0_CHECKPOINT_KIND
            or checkpoint.get("passed") is not True
            or checkpoint.get("stage") not in P0_CHECKPOINT_STAGES):
        raise ValueError(f"{path} is not a passing P0 checkpoint")
    if checkpoint.get("producerSha") != P0_PRODUCER_SHA:
        raise ValueError(f"{path} was taken for producer "
                         f"{checkpoint.get('producerSha')}, not "
                         f"{P0_PRODUCER_SHA}")
    pre = checkpoint.get("prePass") or {}
    for name in ("fingerprint", "digest"):
        if not isinstance(pre.get(name), str) or not _HEX64.fullmatch(
                pre[name]):
            raise ValueError(f"{path} records no pre-pass {name}")
    for key in ("extracted", "measured", "sourceInventory", "seal", "bundle"):
        if not checkpoint.get(key):
            raise ValueError(f"{path} records no {key}")
    if checkpoint["stage"] == "rebuilt" and not checkpoint.get(
            "normalization"):
        raise ValueError(f"{path} is a rebuilt checkpoint without its "
                         "normalization facts")
    return checkpoint, hashlib.sha256(raw).hexdigest()


def _p0_resume_verify(job: "_V2Job", checkpoint: dict, receipt: dict) -> None:
    """A resumed P0's attached clone against its checkpoint: the exact source
    inventory (relpath, st_ino, size, mtime_ns), the retained originals', and
    a re-run fingerprint and content digest equal to the checkpoint's, whose
    extraction equals the source host's pre-pass. Refuses otherwise."""
    si, canonical = job.si, job.canonical_root
    inventory = si.source_inventory(canonical)
    sealed = {rel: [int(v) for v in values[:3]] for rel, values in
              checkpoint["sourceInventory"]["files"].items()}
    differ = sorted(rel for rel in set(sealed) | set(inventory)
                    if sealed.get(rel) != inventory.get(rel))
    if differ:
        raise _V2Refusal(f"{len(differ)} file(s) differ from the checkpoint's "
                         f"source inventory; first {differ[:20]}")
    retained = canonical / ".p0-retained"
    now_retained = (si.source_inventory(retained)
                    if retained.is_dir() else None)
    recorded = checkpoint.get("retainedInventory")
    if recorded is not None:
        recorded = {rel: [int(v) for v in values[:3]]
                    for rel, values in recorded.items()}
    if recorded != now_retained:
        raise _V2Refusal("the retained originals differ from the "
                         "checkpoint's retained inventory")
    fingerprint = _image_fixture_fingerprint(canonical)
    digest = si._content_digest(canonical)
    measured, extracted = checkpoint["measured"], checkpoint["extracted"]
    pre = checkpoint["prePass"]
    receipt["resumeVerification"] = {
        "fingerprint": fingerprint, "digest": digest,
        "checkpointFingerprint": measured["fingerprint"],
        "checkpointDigest": measured["digest"]["sha256"]}
    problems = []
    if fingerprint != measured["fingerprint"]:
        problems.append("the clone's fingerprint differs from the "
                        "checkpoint's")
    if digest["sha256"] != measured["digest"]["sha256"]:
        problems.append("the clone's content digest differs from the "
                        "checkpoint's")
    if (extracted["fingerprint"] != pre["fingerprint"]
            or extracted["digest"]["sha256"] != pre["digest"]):
        problems.append("the checkpoint's extraction does not equal the "
                        "source host's pre-pass")
    receipt["resumeVerification"]["equal"] = not problems
    if problems:
        raise _V2Refusal("the resumed clone does not verify: "
                         + "; ".join(problems))


def _p0_saved_retention(checkpoint: dict, retained: pathlib.Path) -> dict:
    """What `_defer_retention` returned before a `rebuilt` checkpoint: the
    original config bytes from the retained copy (their digest must equal the
    checkpoint's) and the recorded original stamp."""
    facts = checkpoint["normalization"]
    original = (retained / "config.json").read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest != facts.get("configSha256"):
        raise _V2Refusal("the retained config.json differs from the bytes the "
                         "rebuilt checkpoint recorded")
    return {"config": original, "stamp": facts.get("originalStamp"),
            "retentionDays": facts.get("retentionDays"),
            "configSha256": digest}


def prepare_fixture_image(args) -> dict:
    """P0 (spec §5.3, steps 1-8): the fixture image, once. The stdin forms
    read the pax tar stream; every form seals an `extracted` checkpoint once
    the stream verifies and a `rebuilt` one once the producer's rebuild
    succeeds (#937 I4). `--extract-only` stops at the first, whose JSON is
    `--output`; `--resume-from` continues from a checkpoint's verified clone.
    Otherwise writes `--output` (W/p0-receipt.json)."""
    si = _soak_image()
    working = pathlib.Path(args.root).expanduser().resolve()
    output = getattr(args, "output", None)
    extract_only = bool(getattr(args, "extract_only", False))
    resume_from = getattr(args, "resume_from", None)
    problems = _new_output_problems(output, working)
    checkpoint = checkpoint_sha = None
    if resume_from:
        if extract_only:
            problems.append("--extract-only and --resume-from are exclusive")
        try:
            checkpoint, checkpoint_sha = _read_p0_checkpoint(resume_from)
        except (OSError, ValueError) as exc:
            problems.append(f"--resume-from: {exc}")
        expected = dict((checkpoint or {}).get("prePass") or {})
    else:
        expected = {"fingerprint": getattr(args, "source_fingerprint", None),
                    "digest": getattr(args, "source_digest", None)}
        for name, value in expected.items():
            if not isinstance(value, str) or not _HEX64.fullmatch(value):
                problems.append(f"--source-{name} (the source host's "
                                "read-only pre-pass value, 64 hex) is "
                                "required")
    if problems:
        raise ValueError("; ".join(problems))
    output = pathlib.Path(output).expanduser().resolve()
    canonical = pathlib.Path(
        getattr(args, "canonical_root", None)
        or (checkpoint or {}).get("canonicalRoot")
        or si.CANONICAL_ROOT_DEFAULT)
    source_bundle = None
    if checkpoint is not None:
        source_bundle = pathlib.Path(checkpoint["bundle"])
        remedy = ("remedy: resume from an earlier checkpoint whose bundle "
                  "still matches its seal, or re-run the stdin form; a "
                  "checkpoint bundle is never repaired or attached")
        if not source_bundle.is_dir():
            raise ValueError(f"the checkpoint bundle {source_bundle} is "
                             f"missing, so it cannot match its seal; {remedy}")
        seal_problems = si.seal_metadata_problems(checkpoint["seal"],
                                                  source_bundle)
        if seal_problems:
            raise ValueError(f"the checkpoint bundle {source_bundle} does not "
                             "match its seal: " + "; ".join(seal_problems)
                             + f"; {remedy}")
    stamp = _utc_stamp()
    bundle = working / f"p0-{stamp}.sparsebundle"
    evidence = working / "evidence" / f"p0-{stamp}"
    receipt_path = (working / f"p0-extract-{stamp}-receipt.json"
                    if extract_only else output)

    def checkpoint_path(stage: str) -> pathlib.Path:
        if extract_only and stage == "extracted":
            return output
        return working / f"p0-checkpoint-{stage}-{stamp}.json"

    receipt = {"schemaVersion": 1, "kind": P0_RECEIPT_KIND,
               "startedAt": _utc_now(), "workingRoot": str(working),
               "canonicalRoot": str(canonical), "bundle": str(bundle),
               "evidenceRoot": str(evidence), "producerSha": P0_PRODUCER_SHA,
               "imageDescriptor": dict(V2_IMAGE_DESCRIPTOR),
               "backing": _storage_identity(working),
               "mode": ("resume" if checkpoint else "extract-only"
                        if extract_only else "one-shot"),
               "checkpoints": [],
               "command": [str(a) for a in sys.argv], "problems": []}
    if checkpoint is not None:
        receipt["resumedFrom"] = {
            "stage": checkpoint["stage"],
            "path": str(pathlib.Path(resume_from).expanduser().resolve()),
            "sha256": checkpoint_sha, "bundle": str(source_bundle),
            "seal": checkpoint["seal"]["sealSha256"]}
    evidence.mkdir(parents=True, exist_ok=False)
    data = canonical / "data"
    job = _V2Job(working_root=working, canonical_root=canonical, kind="full",
                 deadline_s=si.BudgetWatchdog.KINDS["full"]["seconds"],
                 ledger=working / "budget.json")
    runner = None
    try:
        with job:
            if checkpoint is None:
                job.lifecycle.create(bundle)
                attached = job.lifecycle.attach(bundle)
                receipt["image"] = attached.as_dict()
                receipt["extraction"] = _extract_stream(
                    job, _V2Step("p0-extract", working), canonical,
                    evidence / "logs")
                extracted = _p0_verify_extraction(job, receipt, expected)
                job.check()
                attached = _p0_checkpoint(
                    job, attached, bundle, receipt, "extracted", stamp,
                    path=checkpoint_path("extracted"),
                    reattach=not extract_only,
                    facts={"prePass": expected, "extracted": extracted,
                           "measured": extracted,
                           "census": receipt["census"],
                           "receipt": str(receipt_path)})
                stage = "extracted"
            else:
                # Never the checkpoint bundle itself: a fresh clone, verified.
                si.clonefile_bundle(source_bundle, bundle)
                attached = job.lifecycle.attach(bundle)
                receipt["image"] = attached.as_dict()
                _p0_resume_verify(job, checkpoint, receipt)
                extracted = checkpoint["extracted"]
                receipt["census"] = checkpoint.get("census")
                receipt["fingerprints"] = {
                    "sourceHost": expected["fingerprint"],
                    "image": extracted["fingerprint"],
                    "equal": extracted["fingerprint"]
                    == expected["fingerprint"],
                    "matches857": extracted["fingerprint"].startswith(
                        P0_857_FINGERPRINT_EDGES[0])
                    and extracted["fingerprint"].endswith(
                        P0_857_FINGERPRINT_EDGES[1])}
                receipt["digests"] = {
                    "sourceHost": expected["digest"],
                    "image": extracted["digest"],
                    "equal": extracted["digest"]["sha256"]
                    == expected["digest"]}
                stage = checkpoint["stage"]
            job.check()
            if not extract_only:
                retained = canonical / ".p0-retained"
                producer = materialize_checkout_ref(
                    P0_PRODUCER_SHA,
                    working / "checkouts"
                    / f"producer-{P0_PRODUCER_SHA[:12]}-{stamp}",
                    env=_v2_env(_V2Step("p0-git", working)))
                receipt["producer"] = {"sha": P0_PRODUCER_SHA,
                                       "checkout": str(producer)}
                version = _user_version(data / "stats.db")
                receipt["producer"]["statsUserVersion"] = version
                if version != P0_STATS_USER_VERSION:
                    raise _V2Refusal(f"stats user_version {version} is not "
                                     f"the producer's {P0_STATS_USER_VERSION}")
                runner = job.run(evidence / "logs")
                if stage == "extracted":
                    # Step 3: retain the originals beside the corpus.
                    retained.mkdir()
                    for name in (*si.STORE_FILES, "config.json"):
                        if (data / name).exists():
                            _clonefile(data / name, retained / name)
                    # Step 4: normalize with the producer, retention
                    # disabled.
                    env = _v2_env(_V2Step("p0-normalize", working, canonical))
                    saved = _defer_retention(data)
                    receipt["normalization"] = {
                        "retentionDays": saved["retentionDays"],
                        "configSha256": saved["configSha256"],
                        "originalStamp": saved["stamp"]}
                    _checked(runner(_product(producer, "cache-sync",
                                             "--rebuild", "--source", "all"),
                                    env=env, capture_output=True, text=True,
                                    timeout=job.remaining()),
                             "the producer's cache-sync --rebuild --source all")
                    job.check()
                    attached = _p0_checkpoint(
                        job, attached, bundle, receipt, "rebuilt", stamp,
                        path=checkpoint_path("rebuilt"), reattach=True,
                        facts={"prePass": expected, "extracted": extracted,
                               "measured": {
                                   "fingerprint": _image_fixture_fingerprint(
                                       canonical),
                                   "digest": si._content_digest(canonical)},
                               "census": receipt["census"],
                               "normalization": {
                                   **receipt["normalization"],
                                   "statsUserVersion": version},
                               "receipt": str(receipt_path)})
                    job.check()
                else:
                    saved = _p0_saved_retention(checkpoint, retained)
                    receipt["normalization"] = {
                        key: checkpoint["normalization"].get(key)
                        for key in ("retentionDays", "configSha256",
                                    "originalStamp")}
                # Spec §5.3 step 5 restores the FIXTURE's stamp, which the
                # pinned constant names; a corpus whose own stamp differs is
                # refused, never rewritten to the constant (#937 F6).
                if saved["stamp"] != P0_LAST_PRUNE_AT:
                    raise _V2Refusal(
                        f"the corpus's conversation_retention_last_prune_at "
                        f"{saved['stamp']!r} is not the pinned "
                        f"{P0_LAST_PRUNE_AT}; another corpus or another "
                        "prune state")
                # Step 5: the pinned prune, in the producer's own modules.
                (data / "config.json").write_bytes(saved["config"])
                prune = _pinned_retention_prune(
                    producer, _v2_env(_V2Step("p0-prune", working, canonical)),
                    now_utc=P0_PRUNE_NOW_UTC, retention_days=P0_RETENTION_DAYS,
                    run=runner)
                _set_meta(data / "conversations.db",
                          "conversation_retention_last_prune_at",
                          saved["stamp"])
                meta = _store_meta(data)
                receipt["pinnedPrune"] = {
                    **prune, "freelistPages": _freelist_pages(
                        data / "conversations.db"),
                    "cacheMeta": (meta.get("conversations.db") or {}).get(
                        "cacheMeta")}
                restored = _retention_days_of(data)
                receipt["retentionDays"] = restored
                meta_problems = []
                if restored != P0_RETENTION_DAYS:
                    meta_problems.append(f"the restored retention_days is "
                                         f"{restored}, not "
                                         f"{P0_RETENTION_DAYS}")
                if _meta_value(data / "conversations.db",
                               "conversation_retention_last_prune_at") \
                        != P0_LAST_PRUNE_AT:
                    meta_problems.append("conversation_retention_last_prune_at"
                                         " was not restored")
                # Step 7's omitted cache_meta, key by key against the
                # retained stores (#937 F6).
                receipt["metaComparison"] = _p0_meta_comparison(retained,
                                                                data)
                meta_problems.extend(receipt["metaComparison"]["problems"])
                job.check()
                # Steps 6-7: cursor evidence and equivalence, run even when
                # the meta check failed, so one run records every finding.
                file_key, key_error = _producer_file_key(producer)
                receipt["equivalenceFileKey"] = {
                    "source": "producer _lib_source_identity.codex_file_key",
                    "loaded": file_key is not None, "error": key_error}
                equivalence = si.StoreEquivalence(
                    canonical_root=canonical,
                    inventory=si.source_inventory(canonical),
                    file_key=file_key).compare(retained, data)
                receipt["equivalence"] = equivalence
                if meta_problems:
                    raise _V2Refusal("P0 meta restore refuses: "
                                     + "; ".join(meta_problems[:10]))
                if equivalence["problems"]:
                    raise _V2Refusal("P0 equivalence refuses: "
                                     + "; ".join(equivalence["problems"][:10]))
                # Step 8: quiesce, detach and seal.
                shutil.rmtree(retained)
                _detach_and_seal(job, attached, bundle, receipt,
                                 wal_key="walCheckpoints")
    except si.ImageInterrupted as exc:
        receipt["problems"].append(f"interrupted: {exc}")
    except (si.ImageRefusal, _V2Refusal, *_V2_ERRORS) as exc:
        receipt["problems"].append(_refusal_text(exc))
    receipt.update(job.records)
    receipt["problems"].extend(_stop_problems(job))
    teardown = receipt.get("teardown") or {}
    receipt["problems"].extend(f"teardown: {p}"
                               for p in teardown.get("problems") or [])
    receipt["processWrites"] = _process_writes(runner)
    receipt["finishedAt"] = _utc_now()
    if extract_only:
        receipt["passed"] = not receipt["problems"] and any(
            c["stage"] == "extracted" for c in receipt["checkpoints"])
    else:
        receipt["passed"] = not receipt["problems"] and "seal" in receipt
    _write_new_json(receipt_path, receipt)
    return receipt


def _process_writes(runner) -> list:
    """Each recorded child's own write counter (P0 and P1 alike, #937 F8)."""
    return [{key: record.get(key) for key in ("label", "writeBytes",
                                              "writeStatus", "durationS",
                                              "returncode")}
            for record in getattr(runner, "records", None) or []]


#: P0 step 5's cache_meta expectation (#937 F6), key by key. The classes
#: come from the producer's (4892abbec) own writers and the retained corpus's
#: keys: a pinned key equals its constant on both sides; a key the pinned
#: prune writes is recorded; a key the producer stamps with its own constant
#: must hold that constant; a contract or version key is present in both
#: stores and equal; a key the producer's `cache-sync --rebuild` regenerates
#: (clocks, mutation counters, generation ids, cursors, parse health,
#: completion markers) is recorded with both values, and one marked required
#: must survive the rebuild. Any other key, on either side, is a problem that
#: names it: an unclassified key (a pending flag, say) fails closed.
P0_META_PINNED = {"conversations.db": {
    "conversation_retention_last_prune_at": P0_LAST_PRUNE_AT}}
P0_META_WRITTEN = {"conversations.db": (
    "conversation_retention_reclaim_pending",)}
P0_META_EQUAL = {
    "conversations.db": ("codex_conversation_contract_version",
                         "codex_find_projection_complete_version",
                         "conversation_account_dimension",
                         "conversation_schema_version")}
#: Keys the producer stamps with its own constant on the rebuild: the
#: normalized value must be that constant, the retained one is recorded. The
#: rollup pricing fingerprint is the producer's PRICING_SNAPSHOT_DATE
#: (4892abbec:bin/_lib_pricing.py:71); the corpus was last priced by an
#: older binary (2026-09-05), so equality with the retained store refused a
#: correct P0 (full-size run, 2026-11-09).
P0_PRODUCER_PRICING_FP = "2026-09-23"
P0_META_PRODUCER = {
    "cache.db": {"conversation_sessions_pricing_fp": P0_PRODUCER_PRICING_FP},
    "conversations.db": {
        "conversation_sessions_pricing_fp": P0_PRODUCER_PRICING_FP}}
P0_META_REGENERATED = {
    "cache.db": ("claude_ingest_walk_complete",
                 "codex_accounting_mutation_seq",
                 "codex_attribution_rehydrated_hw",
                 "codex_physical_mutation_seq",
                 "codex_quota_projection_certificate",
                 "codex_window_attribution_cursor",
                 "conversation_render_revision",
                 "dashboard_codex_full_walk_complete",
                 "dashboard_frontier_generation_claude",
                 "dashboard_frontier_generation_codex",
                 "parse_health_claude", "parse_health_codex",
                 "session_entries_mutation_seq"),
    "conversations.db": ("claude_account_stamp_coverage_complete",
                         "codex_find_projection_generation",
                         "conversation_render_revision",
                         "dashboard_frontier_generation_claude",
                         "dashboard_frontier_generation_codex")}
#: Regenerated keys a clean rebuild must re-establish: the walk-complete
#: marker certifies that the rebuild's exhaustive walk ended cleanly.
P0_META_REQUIRED = {"cache.db": ("claude_ingest_walk_complete",)}


def _p0_meta_comparison(retained: pathlib.Path, data: pathlib.Path) -> dict:
    """Every cache_meta key of both stores, retained against normalized, key
    by key against P0's expectation (spec §5.3 step 7's omitted family
    "compared key by key against the recorded expectation in step 5"): a
    contract key missing, extra or changed, a required regenerated key
    missing, a pinned key off its constant, or any unclassified key is a
    problem that names it; regenerated and prune-written keys are recorded
    with both values."""
    before, after = _store_meta(retained), _store_meta(data)
    out = {"stores": {}, "problems": []}
    for store in ("cache.db", "conversations.db"):
        a = (before.get(store) or {}).get("cacheMeta") or {}
        b = (after.get(store) or {}).get("cacheMeta") or {}
        pinned = P0_META_PINNED.get(store, {})
        written = P0_META_WRITTEN.get(store, ())
        equal_keys = P0_META_EQUAL.get(store, ())
        regenerated = P0_META_REGENERATED.get(store, ())
        required = P0_META_REQUIRED.get(store, ())
        producer = P0_META_PRODUCER.get(store, {})
        entry = {"retainedKeys": len(a), "normalizedKeys": len(b),
                 "equal": 0, "missing": [], "extra": [], "changed": [],
                 "unclassified": [], "pinned": {}, "written": {},
                 "regenerated": {}, "producer": {}}
        for key in sorted(set(a) | set(b)):
            if key in pinned:
                ok = a.get(key) == pinned[key] == b.get(key)
                entry["pinned"][key] = {"retained": a.get(key),
                                        "normalized": b.get(key),
                                        "expected": pinned[key], "equal": ok}
                if not ok:
                    out["problems"].append(
                        f"{store} cache_meta {key}: retained {a.get(key)!r}, "
                        f"normalized {b.get(key)!r}, pinned {pinned[key]!r}")
            elif key in written:
                entry["written"][key] = {"retained": a.get(key),
                                         "normalized": b.get(key)}
            elif key in producer:
                ok = b.get(key) == producer[key]
                entry["producer"][key] = {"retained": a.get(key),
                                          "normalized": b.get(key),
                                          "expected": producer[key],
                                          "equal": ok}
                if not ok:
                    out["problems"].append(
                        f"{store} cache_meta {key}: normalized "
                        f"{b.get(key)!r}, the producer's {producer[key]!r}")
            elif key in regenerated:
                entry["regenerated"][key] = {
                    "retained": None if key not in a else str(a[key])[:200],
                    "normalized": None if key not in b else str(b[key])[:200]}
                if key in required and key in a and key not in b:
                    entry["missing"].append(key)
            elif key not in equal_keys:
                entry["unclassified"].append(key)
            elif key not in b:
                entry["missing"].append(key)
            elif key not in a:
                entry["extra"].append(key)
            elif a[key] != b[key]:
                entry["changed"].append(key)
            else:
                entry["equal"] += 1
        for kind, label in (("missing", "missing from the normalized store"),
                            ("extra", "extra in the normalized store"),
                            ("changed", "changed"),
                            ("unclassified", "outside P0's expectation")):
            if entry[kind]:
                out["problems"].append(
                    f"{store} cache_meta: {len(entry[kind])} key(s) {label}: "
                    f"{entry[kind][:20]}")
        entry["changedValues"] = {
            key: [str(a.get(key))[:200], str(b.get(key))[:200]]
            for key in entry["changed"][:20]}
        out["stores"][store] = entry
    return out


def _producer_file_key(producer: pathlib.Path):
    """(codex_file_key, error): the producer's own stdlib-only
    `_lib_source_identity.codex_file_key`, so P0's equivalence names the path
    of every advanced Codex incarnation (#937 F7). Evidence only: a module
    that cannot be loaded leaves the paths unnamed and says why."""
    path = pathlib.Path(producer) / "bin" / "_lib_source_identity.py"
    try:
        loader = importlib.machinery.SourceFileLoader(
            "p0_producer_source_identity", str(path))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module.codex_file_key, None
    except (OSError, ImportError, AttributeError, SyntaxError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _freelist_pages(db: pathlib.Path) -> "int | None":
    try:
        with contextlib.closing(_ro_db(db)) as conn:
            return int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    except sqlite3.Error:
        return None


_MIGRATE_BODY = r'''
c = _tree()
cache = c._load_sibling("_cctally_cache")
cache.open_cache_db().close()
cache.open_conversations_db().close()
db = c._load_sibling("_cctally_db")
frontier = c._load_sibling("_lib_ingest_frontier")
keys = set()
for module, name in ((frontier, "_PENDING_META_KEYS"),
                     (frontier, "_CONVERSATION_PENDING_META_KEYS"),
                     (cache, "_TARGETED_DECLINE_FLAGS")):
    keys.update(getattr(module, name, ()) or ())
_emit({"opened": ["cache.db", "conversations.db"],
       "registries": {
           "maintenanceKeys": sorted(keys),
           "migrations": {
               "cache.db": [m.name for m in db._CACHE_MIGRATIONS],
               "conversations.db": [m.name
                                    for m in db._CONVERSATIONS_MIGRATIONS]}}})
'''

_QUOTA_INDEX_PROBE_BODY = r'''
c = _tree()
journal = c._load_sibling("_cctally_journal")
out = {"defined": hasattr(journal, "quota_index_valid")}
if out["defined"]:
    out["valid"] = bool(journal.quota_index_valid())
_emit(out)
'''

_QUOTA_INDEX_BODY = r'''
c = _tree()
journal = c._load_sibling("_cctally_journal")
out = {"defined": hasattr(journal, "quota_index_valid")}
if out["defined"]:
    out["validBefore"] = bool(journal.quota_index_valid())
    if not out["validBefore"]:
        journal.ensure_quota_index(budgeted=False)
        out["ensured"] = True
    out["valid"] = bool(journal.quota_index_valid())
_emit(out)
'''


def _read_receipt(path, kind: str) -> "tuple[dict, bytes]":
    raw = pathlib.Path(path).expanduser().read_bytes()
    receipt = _strict_json(raw)
    if receipt.get("kind") != kind or receipt.get("passed") is not True:
        raise ValueError(f"{path} is not a passing {kind} receipt")
    return receipt, raw


def prepare_tree_image(args) -> dict:
    """P1 (spec §5.4, steps 1-8): one tree image, in the tree's settled
    state on the device it seals. Writes `--output`
    (W/p1-<sha>-receipt.json)."""
    si = _soak_image()
    working = pathlib.Path(args.root).expanduser().resolve()
    problems = _new_output_problems(getattr(args, "output", None), working)
    if problems:
        raise ValueError("; ".join(problems))
    p0, p0_raw = _read_receipt(args.p0_receipt, P0_RECEIPT_KIND)
    tree_sha = _resolve_commit(args.tree)
    canonical = pathlib.Path(getattr(args, "canonical_root", None)
                             or p0["canonicalRoot"])
    output = pathlib.Path(args.output).expanduser().resolve()
    p0_bundle = pathlib.Path(p0["bundle"])
    stamp = _utc_stamp()
    bundle = working / f"p1-{tree_sha[:12]}-{stamp}.sparsebundle"
    evidence = working / "evidence" / f"p1-{tree_sha[:12]}-{stamp}"
    receipt = {"schemaVersion": 1, "kind": P1_RECEIPT_KIND,
               "startedAt": _utc_now(), "treeSha": tree_sha,
               "workingRoot": str(working), "canonicalRoot": str(canonical),
               "bundle": str(bundle), "evidenceRoot": str(evidence),
               "p0": {"path": str(pathlib.Path(args.p0_receipt).resolve()),
                      "sha256": hashlib.sha256(p0_raw).hexdigest(),
                      "seal": p0["seal"]["sealSha256"],
                      "bundle": str(p0_bundle),
                      "retentionDays": p0.get("retentionDays"),
                      "canonicalRoot": p0["canonicalRoot"],
                      "imageDescriptor": p0.get("imageDescriptor")},
               "deadlineSeconds": P1_DEADLINE_SECONDS,
               "command": [str(a) for a in sys.argv], "problems": []}
    seal_problems = si.seal_metadata_problems(p0["seal"], p0_bundle)
    if seal_problems:
        raise ValueError("the P0 bundle does not match its seal: "
                         + "; ".join(seal_problems))
    evidence.mkdir(parents=True, exist_ok=False)
    data = canonical / "data"
    job = _V2Job(working_root=working, canonical_root=canonical, kind="full",
                 deadline_s=P1_DEADLINE_SECONDS, ledger=working / "budget.json")
    runner = None
    try:
        with job:
            # Step 1: clone and attach; the P0 inventory, exactly.
            si.clonefile_bundle(p0_bundle, bundle)
            attached = job.lifecycle.attach(bundle)
            receipt["image"] = attached.as_dict()
            receipt["deviceEqualsP0"] = (attached.st_dev
                                         == p0["sourceInventory"]["stDev"])
            inventory = si.source_inventory(canonical)
            sealed = {rel: [int(v) for v in values[:3]] for rel, values in
                      p0["sourceInventory"]["files"].items()}
            differ = sorted(rel for rel in set(sealed) | set(inventory)
                            if sealed.get(rel) != inventory.get(rel))
            if differ:
                raise _V2Refusal(f"{len(differ)} file(s) differ from the P0 "
                                 f"inventory; first {differ[:20]}")
            checkout = materialize_checkout_ref(
                tree_sha, working / "checkouts" / f"tree-{tree_sha[:12]}-{stamp}",
                env=_v2_env(_V2Step("p1-git", working)))
            receipt["checkout"] = str(checkout)
            env = _v2_env(_V2Step(f"p1-{tree_sha[:12]}", working, canonical))
            runner = job.run(evidence / "logs")
            # Step 2: defer retention.
            saved = _defer_retention(data)
            receipt["retention"] = {"retentionDays": saved["retentionDays"],
                                    "configSha256": saved["configSha256"],
                                    "stamp": saved["stamp"]}
            before_state = _catchup_module().retained_state(data)
            # Step 3: the tree's ordinary migrating openers, nothing ingested.
            meta_before = _store_meta(data)
            receipt["migrate"] = _tree_child(
                checkout, _MIGRATE_BODY, [], env, run=runner,
                timeout=job.remaining())
            meta_after = _store_meta(data)
            receipt["migrations"] = {
                "before": {s: e["migrations"] for s, e in meta_before.items()},
                **_pending_set_by(meta_before, meta_after)}
            registries = receipt["migrate"].get("registries") or {}
            receipt["migrations"]["registries"] = registries
            pending = _pending_keys_by_family(
                receipt["migrations"]["pendingKeys"],
                registries.get("maintenanceKeys"))
            receipt["migrations"]["byFamily"] = pending
            job.check()
            # Step 4: the stats rebuild and the summary identity check.
            rebuilds = []

            def rebuild():
                done = _checked(runner(
                    _product(checkout, "db", "rebuild", "--db", "stats",
                             "--json"),
                    env=env, capture_output=True, text=True,
                    timeout=min(P1_STATS_REBUILD_SECONDS, job.remaining())),
                    "db rebuild --db stats")
                try:
                    rebuilds.append(json.loads(done.stdout))
                except ValueError:
                    rebuilds.append({"stdout": done.stdout[-2000:]})
                job.check()
            rebuild()
            receipt["segmentSummaries"] = _settle_segment_summaries(
                checkout, data / "journal", rebuild, env=env, run=runner)
            receipt["statsRebuilds"] = rebuilds
            # Step 5: the ordinary sync of the pending work.
            _checked(runner(_product(checkout, "cache-sync", "--source", "all"),
                            env=env, capture_output=True, text=True,
                            timeout=job.remaining()),
                     "cache-sync --source all")
            job.check()
            # R1's bearing: the quota index's state before the drain, read
            # only (never ensured here).
            quota_before = _tree_child(checkout, _QUOTA_INDEX_PROBE_BODY, [],
                                       env, run=runner,
                                       timeout=job.remaining())
            # Step 6: the default-mode drain to the settled judgement.
            drained, problem = _drain_before_admission(
                checkout, env, evidence, run=runner,
                deadline_s=job.remaining())
            receipt["drain"] = {"admitted": drained.get("admitted"),
                                "problems": drained.get("problems"),
                                "elapsedS": drained.get("elapsedS"),
                                "pendingState": drained.get("pendingState")}
            if problem:
                raise _V2Refusal(f"P1's drain did not settle: {problem}")
            quota = _tree_child(checkout, _QUOTA_INDEX_BODY, [], env,
                                run=runner, timeout=job.remaining())
            quota.update(_quota_index_bearing(quota_before, quota))
            receipt["quotaIndex"] = quota
            if quota.get("defined") and not quota.get("valid"):
                raise _V2Refusal("the tree's quota index is not valid after "
                                 "one ensure_quota_index(budgeted=False)")
            # Step 7: attribute every replay to a pending key.
            receipt["replayAttribution"] = _p1_replay_attribution(
                before_state, _catchup_module().retained_state(data), pending)
            receipt["statsEpoch"] = (drained.get("pendingState") or {}).get(
                "stats")
            # Step 8: restore, quiesce, detach and seal.
            _restore_retention(data, saved)
            _detach_and_seal(job, attached, bundle, receipt)
            receipt["deviceAtSeal"] = receipt["sourceInventory"]["stDev"]
    except si.ImageInterrupted as exc:
        receipt["problems"].append(f"interrupted: {exc}")
    except (si.ImageRefusal, _V2Refusal, *_V2_ERRORS) as exc:
        receipt["problems"].append(_refusal_text(exc))
    receipt.update(job.records)
    receipt["problems"].extend(_stop_problems(job))
    teardown = receipt.get("teardown") or {}
    receipt["problems"].extend(f"teardown: {p}"
                               for p in teardown.get("problems") or [])
    receipt["processWrites"] = _process_writes(runner)
    receipt["finishedAt"] = _utc_now()
    receipt["passed"] = not receipt["problems"] and "seal" in receipt
    _write_new_json(output, receipt)
    return receipt


def _quota_index_bearing(before: dict, after: dict) -> dict:
    """P1's quota-index state for review follow-up R1: valid before and after
    the drain, and whether it was rebuilt (by the drain itself, or by the one
    `ensure_quota_index(budgeted=False)`). A tree without
    `quota_index_valid` (56e66f07a) records null with the reason."""
    if not (before or {}).get("defined") or not (after or {}).get("defined"):
        return {"validBeforeDrain": None, "validAfterDrain": None,
                "rebuilt": None, "rebuiltBy": None,
                "reason": "the tree defines no _cctally_journal."
                          "quota_index_valid"}
    valid_before = bool(before.get("valid"))
    valid_after = bool(after.get("validBefore"))
    by = ("ensure" if after.get("ensured") else
          "drain" if not valid_before and valid_after else None)
    return {"validBeforeDrain": valid_before, "validAfterDrain": valid_after,
            "rebuilt": by is not None, "rebuiltBy": by, "reason": None}



# -- the two-tree relocation oracle (spec §6.2; off the merge path, lan) ----

#: Cursor families and the derived families each one feeds, with the
#: column that carries a derived row's source offset.
_ORACLE_CURSORS = (
    ("cache.db", "session_files"),
    ("conversations.db", "conversation_source_files"),
    ("cache.db", "codex_session_files"),
    ("conversations.db", "codex_conversation_source_files"),
)
_ORACLE_DERIVED = (
    ("cache.db", "session_entries", "line_offset", "session_files"),
    ("conversations.db", "conversation_messages", "byte_offset",
     "conversation_source_files"),
    ("cache.db", "codex_session_entries", "line_offset",
     "codex_session_files"),
    ("conversations.db", "codex_conversation_events", "line_offset",
     "codex_conversation_source_files"),
    ("conversations.db", "codex_conversation_messages", "line_offset",
     "codex_conversation_source_files"),
)
#: Cursor columns the evidence records (and GREEN compares with Control):
#: catchup's retained columns plus the logical identities.
_ORACLE_CURSOR_COLUMNS = (
    "last_byte_offset", "size_bytes", "inode", "device_id",
    "source_incarnation_id", "committed_prefix_sha256", "source_root_key",
    "account_key", "conversation_key", "last_conversation_key")
#: Derived-row columns that are not content: the surrogate id (judged on its
#: own), the source spelling (relocated in RED) and ingest clocks.
_ORACLE_NON_CONTENT = frozenset({"id", "source_path", "ingested_at",
                                 "created_at", "updated_at"})
ORACLE_SEED = 937
ORACLE_CASES = ("control", "red-v1", "green-v2", "clean-verification",
                "replacement-codex", "replacement-claude", "append-discovery")
ORACLE_KIND = "relocationOracle"


def _ro_rows(db: pathlib.Path, table: str, wanted=None):
    """(columns, rows) of `table`, read-only; ([], []) when absent."""
    if not db.exists():
        return [], []
    with contextlib.closing(_ro_db(db)) as conn:
        have = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if not have:
            return [], []
        columns = have if wanted is None else [c for c in wanted if c in have]
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM {table}").fetchall()
    return columns, rows


def _store_replay_evidence(before: pathlib.Path, after: pathlib.Path, *,
                           relocate=None) -> dict:
    """Store-based replay evidence between two snapshots of a data dir (spec
    §6.2): every cursor row before and after; replays (a new incarnation, an
    inode or root-key change, an offset below the pre-drain cursor, a
    vanished cursor, a derived row below the pre-drain cursor whose
    surrogate id or content changed, a deleted or newly inserted derived row
    below it); and the consumed input, the derived rows added at offsets in
    [pre-drain cursor, post-drain cursor). `relocate` maps a before-path to
    its after spelling (RED's v1 copy under another root)."""
    move = relocate or (lambda path: path)
    before, after = pathlib.Path(before), pathlib.Path(after)
    cursors, replays, floors = {}, [], {}

    def replay(kind, table, path, **detail):
        replays.append({"kind": kind, "table": table, "path": path, **detail})

    for store, table in _ORACLE_CURSORS:
        cols_a, rows_a = _ro_rows(before / store, table,
                                  ("path", *_ORACLE_CURSOR_COLUMNS))
        cols_b, rows_b = _ro_rows(after / store, table,
                                  ("path", *_ORACLE_CURSOR_COLUMNS))
        a = {move(r[0]): dict(zip(cols_a[1:], r[1:])) for r in rows_a}
        b = {r[0]: dict(zip(cols_b[1:], r[1:])) for r in rows_b}
        family = cursors[table] = {}
        floors[table] = {path: int(row.get("last_byte_offset") or 0)
                         for path, row in a.items()}
        for path in sorted(set(a) | set(b)):
            old, new = a.get(path), b.get(path)
            family[path] = {"before": old, "after": new}
            if new is None:
                replay("vanished cursor", table, path)
                continue
            if old is None:
                continue
            if int(new.get("last_byte_offset") or 0) < \
                    int(old.get("last_byte_offset") or 0):
                replay("reset", table, path,
                       offsets=[old.get("last_byte_offset"),
                                new.get("last_byte_offset")])
            for column in ("inode", "source_incarnation_id"):
                if column in old and old.get(column) != new.get(column):
                    replay("new incarnation", table, path, column=column,
                           values=[old.get(column), new.get(column)])
            if "source_root_key" in old and \
                    old.get("source_root_key") != new.get("source_root_key"):
                replay("root-key change", table, path)
    incarnations = {}
    for side, root in (("before", before), ("after", after)):
        _cols, rows = _ro_rows(root / "cache.db", "codex_file_incarnations",
                               ("file_identity", "incarnation"))
        incarnations[side] = {str(k): v for k, v in rows}
    for identity in sorted(set(incarnations["before"])
                           & set(incarnations["after"])):
        if incarnations["after"][identity] != incarnations["before"][identity]:
            replay("codex file incarnation advanced", "codex_file_incarnations",
                   identity, values=[incarnations["before"][identity],
                                     incarnations["after"][identity]])
    derived, consumed = {}, {}
    for store, table, offset_column, cursor_table in _ORACLE_DERIVED:
        def keyed(root, mover):
            columns, rows = _ro_rows(root / store, table)
            if not columns:
                return {}
            out = {}
            for row in rows:
                values = dict(zip(columns, row))
                content = {k: v for k, v in values.items()
                           if k not in _ORACLE_NON_CONTENT}
                out[(mover(values["source_path"]), int(values[offset_column]))] = (
                    values.get("id"), hashlib.sha256(json.dumps(
                        content, sort_keys=True, default=str).encode()
                    ).hexdigest())
            return out
        a, b = keyed(before, move), keyed(after, lambda path: path)
        floor = floors.get(cursor_table) or {}
        added = consumed[table] = {}
        stats = {"before": len(a), "after": len(b), "rewritten": 0,
                 "deleted": 0, "consumed": 0, "reinsertedBelowCursor": 0}
        for key in sorted(set(a) | set(b)):
            path, offset = key
            below = offset < floor.get(path, 0)
            old, new = a.get(key), b.get(key)
            if old is not None and new is not None:
                if old != new and below:
                    stats["rewritten"] += 1
                    replay("surrogate id changed" if old[0] != new[0]
                           else "content changed", table, path, offset=offset,
                           ids=[old[0], new[0]])
            elif old is not None:
                if below:
                    stats["deleted"] += 1
                    replay("deleted row", table, path, offset=offset)
            elif below:
                stats["reinsertedBelowCursor"] += 1
                replay("inserted below the cursor", table, path,
                       offset=offset, id=new[0])
            else:
                stats["consumed"] += 1
                added.setdefault(path, []).append(offset)
        stats["sha256"] = {side: hashlib.sha256(json.dumps(
            sorted([list(k), list(v)] for k, v in rows.items()),
            default=str).encode()).hexdigest()
            for side, rows in (("before", a), ("after", b))}
        derived[table] = stats
    kinds = {}
    for item in replays:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    return {"replayed": bool(replays), "replayKinds": kinds,
            "replays": replays[:500], "replayCount": len(replays),
            "cursors": cursors, "consumed": consumed, "derived": derived,
            "incarnations": incarnations}


def _snapshot_stores(data_dir: pathlib.Path, dest: pathlib.Path) -> dict:
    """A consistent copy (SQLite online backup) of the two cursor stores."""
    dest.mkdir(parents=True, exist_ok=False)
    out = {}
    for store in ("cache.db", "conversations.db"):
        source = data_dir / store
        if not source.exists():
            continue
        with contextlib.closing(sqlite3.connect(
                f"file:{urllib.parse.quote(str(source))}?mode=ro",
                uri=True)) as src, \
                contextlib.closing(sqlite3.connect(dest / store)) as dst:
            src.backup(dst)
        out[store] = (dest / store).stat().st_size
    return out


def _derived_content(snapshot: pathlib.Path) -> dict:
    """{table: {(path, offset): content digest}} of a snapshot's derived
    families, ids excluded (GREEN's post-drain store against Control's)."""
    out = {}
    for store, table, offset_column, _cursor in _ORACLE_DERIVED:
        columns, rows = _ro_rows(snapshot / store, table)
        entries = {}
        for row in rows:
            values = dict(zip(columns, row))
            content = {k: v for k, v in values.items()
                       if k not in _ORACLE_NON_CONTENT}
            entries[f"{values['source_path']}\t{values[offset_column]}"] = \
                hashlib.sha256(json.dumps(content, sort_keys=True,
                                          default=str).encode()).hexdigest()
        out[table] = entries
    return out


def _provider_event_row(root: pathlib.Path, provider: str, marker: str, *,
                        target: "pathlib.Path | None" = None):
    """(target, row): one complete provider record and the file it goes to,
    as `_append_provider_event` writes it (shared with the oracle's owed
    input, which names its own `target`)."""
    now = _utc_now()
    if provider == "claude":
        if target is None:
            paths = sorted((root / "claude" / "projects").rglob("*.jsonl"))
            if not paths:
                raise RuntimeError("production clone has no Claude source")
            target = paths[0]
        row = {
            "type": "assistant", "uuid": marker, "parentUuid": None,
            "sessionId": f"soak-{marker}", "timestamp": now,
            "cwd": "/bench/dashboard-soak", "gitBranch": "main",
            "requestId": f"request-{marker}",
            "message": {
                "id": f"message-{marker}", "role": "assistant",
                "model": "claude-sonnet-4-5-20250929", "content": marker,
                "usage": {"input_tokens": 100, "output_tokens": 50,
                          "cache_read_input_tokens": 0,
                          "cache_creation_input_tokens": 0},
            },
        }
        return target, row
    if provider != "codex":
        raise ValueError(f"unknown provider {provider}")
    paths = sorted(root.glob("codex-*/sessions/**/*.jsonl"))
    if not paths:
        raise RuntimeError("production clone has no Codex source")
    # The event must qualify as live Codex activity. A rollout whose
    # cursor names no conversation (one written before `session_meta`)
    # turns it into an incomplete accounting row, and the dashboard then
    # rebuilds the whole Codex source on every tick (#857 Task B).
    with contextlib.closing(sqlite3.connect(root / "data" / "cache.db")) as conn:
        cursors = {
            str(path): int(total or 0)
            for path, total in conn.execute(
                "SELECT path, last_total_tokens FROM codex_session_files "
                "WHERE last_conversation_key IS NOT NULL")
        }
    if target is None:
        target = next((path for path in paths if str(path) in cursors), None)
    if target is None or str(target) not in cursors:
        raise RuntimeError(
            "production clone has no Codex source with a conversation identity")
    prior_total = cursors[str(target)]
    row = {
        "timestamp": now, "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "last_token_usage": {
                "input_tokens": 200, "cached_input_tokens": 25,
                "output_tokens": 100, "reasoning_output_tokens": 25,
                "total_tokens": 300},
            "total_token_usage": {"total_tokens": prior_total + 300},
            "marker": marker,
        }},
    }
    return target, row


def _codex_identity_paths(canonical: pathlib.Path) -> "list[pathlib.Path]":
    """Codex rollouts whose accounting cursor names a conversation."""
    with contextlib.closing(_ro_db(canonical / "data" / "cache.db")) as conn:
        named = {row[0] for row in conn.execute(
            "SELECT path FROM codex_session_files "
            "WHERE last_conversation_key IS NOT NULL")}
    return [path for path in sorted(canonical.glob("codex-*/sessions/**/*.jsonl"))
            if str(path) in named]


def _oracle_targets(canonical: pathlib.Path) -> dict:
    """Disjoint files for each stimulus: the append case's own first files
    (as `_append_provider_event` picks them) stay out of every other role."""
    claude = sorted((canonical / "claude" / "projects").rglob("*.jsonl"))
    codex = _codex_identity_paths(canonical)
    if len(claude) < 5 or len(codex) < 4:
        raise _V2Refusal(f"the oracle corpus is too small ({len(claude)} "
                         f"Claude, {len(codex)} named Codex files)")
    return {"owedClaude": claude[1:3], "owedCodex": codex[1:3],
            "replaceClaudeLarger": claude[3], "replaceClaudeSameSize": claude[4],
            "replaceCodex": codex[3]}


def _append_owed_input(canonical: pathlib.Path, targets: dict) -> list:
    """O1's owed input: one complete record on each of two Claude
    transcripts and two Codex rollouts, and an incomplete trailing record
    (no newline) after it on the second of each."""
    owed = []
    for provider, paths in (("claude", targets["owedClaude"]),
                            ("codex", targets["owedCodex"])):
        for index, path in enumerate(paths):
            _target, row = _provider_event_row(
                canonical, provider, f"owed-{provider}-{index}", target=path)
            complete = (json.dumps(row, separators=(",", ":")) + "\n").encode()
            size = path.stat().st_size
            tail = b""
            if index == 1:
                _t, partial = _provider_event_row(
                    canonical, provider, f"partial-{provider}", target=path)
                text = json.dumps(partial, separators=(",", ":")).encode()
                tail = text[: len(text) // 2]
            with path.open("ab") as handle:
                handle.write(complete + tail)
            owed.append({"provider": provider, "path": str(path),
                         "beforeSize": size, "completeEnd": size + len(complete),
                         "incompleteBytes": len(tail)})
    return owed


_ORACLE_PROVIDER_FAMILIES = {
    "claude": (("session_files", "session_entries"),
               ("conversation_source_files", "conversation_messages")),
    "codex": (("codex_session_files", "codex_session_entries"),
              ("codex_conversation_source_files", None)),
}


def _consumption_problems(evidence: dict, owed: list) -> "list[str]":
    """Control's and GREEN's consumption rule: each owed target's cursors end
    at its last complete record (never past the incomplete tail), its
    accounting family committed rows only inside the owed complete suffix,
    and no other file consumed anything."""
    problems = []
    targets = {item["path"]: item for item in owed}
    for item in owed:
        for cursor_table, derived_table in _ORACLE_PROVIDER_FAMILIES[
                item["provider"]]:
            row = (evidence["cursors"].get(cursor_table) or {}).get(
                item["path"]) or {}
            after = (row.get("after") or {}).get("last_byte_offset")
            if after != item["completeEnd"]:
                problems.append(
                    f"{cursor_table} {item['path']}: cursor {after}, owed "
                    f"complete end {item['completeEnd']}")
            if derived_table is None:
                continue
            offsets = (evidence["consumed"].get(derived_table) or {}).get(
                item["path"]) or []
            if not offsets:
                problems.append(f"{derived_table} {item['path']}: the owed "
                                "complete record was not consumed")
            outside = [o for o in offsets if not
                       item["beforeSize"] <= o < item["completeEnd"]]
            if outside:
                problems.append(f"{derived_table} {item['path']}: rows "
                                f"outside the owed suffix at {outside[:5]}")
    for table, paths in evidence["consumed"].items():
        foreign = sorted(set(paths) - set(targets))
        if foreign:
            problems.append(f"{table}: consumed rows on files with no owed "
                            f"input, first {foreign[:3]}")
    return problems


def _replace_file(path: pathlib.Path, *, grow: bool) -> dict:
    """A replacement with a new inode and different content: the lines after
    the first reversed (the same size), plus one more complete record when
    `grow` (a size change)."""
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    if len(lines) < 3 or not raw.endswith(b"\n"):
        raise _V2Refusal(f"{path} is too short to replace")
    body = lines[0] + b"".join(reversed(lines[1:]))
    if grow:
        body += lines[-1]
    if body == raw:
        raise _V2Refusal(f"replacing {path} would not change its content")
    before = os.stat(path)
    staged = path.with_name(path.name + ".oracle-replacement")
    staged.write_bytes(body)
    os.utime(staged, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(staged, path)
    after = os.stat(path)
    return {"path": str(path), "sizes": [before.st_size, after.st_size],
            "inodes": [before.st_ino, after.st_ino]}


def _new_rollout(canonical: pathlib.Path, template: pathlib.Path) -> dict:
    """Discovery: one new rollout file, a copy of `template` under a new
    session id, beside it."""
    first = json.loads(template.read_bytes().splitlines()[0])
    payload = first.get("payload") or {}
    old = payload.get("id") or payload.get("session_id")
    if not old:
        raise _V2Refusal(f"{template} has no session id in its session_meta")
    new = "-".join(secrets.token_hex(n) for n in (4, 2, 2, 2, 6))
    name = template.name.replace(old, new) if old in template.name else \
        f"rollout-oracle-{new}.jsonl"
    target = template.with_name(name)
    target.write_bytes(template.read_bytes().replace(old.encode(), new.encode()))
    return {"path": str(target), "template": str(template), "sessionId": new,
            "size": target.stat().st_size}


def _oracle_catchup(checkout, env, root: pathlib.Path, runner, job, *,
                    manifest=None) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    receipt, problem = _drain_before_admission(
        checkout, env, root, run=runner,
        deadline_s=min(WRITE_BUDGET_ADMISSION_SECONDS, job.remaining()),
        image_manifest=manifest)
    stability = receipt.get("stability") or {}
    rounds = stability.get("receipts") or [] if isinstance(stability,
                                                             dict) else []
    last = rounds[-1] if rounds and isinstance(rounds[-1], dict) else {}
    return {"inputMode": receipt.get("inputMode"),
            "admitted": receipt.get("admitted"),
            "problems": receipt.get("problems") or [],
            "invalid": problem, "elapsedS": receipt.get("elapsedS"),
            # The final round's unconsumed frontier entries, so a case can
            # tell an expected not-drained cause from any other (#937).
            "unconsumed": [{key: entry.get(key) for key in
                            ("logical", "provider", "reason", "cursor",
                             "completeEnd", "generation")}
                           for entry in last.get("unconsumed") or ()
                           if isinstance(entry, dict)]}


#: The drain verdict a not-drained backlog carries (`catchup.py` D6).
_NOT_DRAINED_PREFIX = "the fixture's backlog was not drained"
_UNCONSUMED_PROBLEM = re.compile(r"^round \d+: \d+ unconsumed frontier entr")


def _same_path(a, b) -> bool:
    if a is None or b is None:
        return False
    return str(a) == str(b) or os.path.realpath(str(a)) == os.path.realpath(
        str(b))


def _expected_not_drained(catchup: dict, same_size_path) -> "tuple":
    """(record or None, problems) for one oracle case's catch-up (#937).

    The same-size Claude replacement stimulus is observed only (spec §6.2):
    neither tree detects it, so its file stays "not consumed" and catch-up
    reports the backlog not drained. Exactly that cause, the one unconsumed
    entry being that file with reason "not consumed", is `expectedNotDrained`
    with its evidence. Any other not-drained cause, another file, the same
    file with another reason, or any other catch-up problem, is a failure,
    so a different not-drained cause cannot hide behind the stimulus."""
    invalid = catchup.get("invalid")
    if not invalid:
        return None, []
    entries = catchup.get("unconsumed") or []
    expected = (str(invalid).startswith(_NOT_DRAINED_PREFIX)
                and len(entries) == 1
                and _same_path(entries[0].get("logical"), same_size_path)
                and entries[0].get("reason") == "not consumed"
                and all(_UNCONSUMED_PROBLEM.match(str(p))
                        for p in catchup.get("problems") or ()))
    if not expected:
        return None, [f"catchup: {invalid}"]
    return {"path": str(same_size_path), "reason": "not consumed",
            "cause": "the same-size Claude replacement stimulus, observed "
                     "only (spec §6.2)",
            "invalid": invalid, "evidence": entries[0]}, []


def _image_admission(si, attached, canonical: pathlib.Path, sealed: dict,
                     directory: pathlib.Path) -> "tuple[dict, pathlib.Path]":
    equality = si.identity_equality(attached, si.source_inventory(canonical),
                                    {"sourceInventory": sealed})
    manifest = si.write_admission_inputs(directory, equality)
    return ({key: equality[key] for key in ("passed", "problems",
                                            "deviceMismatch",
                                            "manifestSha256")}, manifest)


def _control_identity(si, attached, canonical: pathlib.Path, sealed: dict,
                      directory: pathlib.Path) -> dict:
    """The oracle control's P2 identity equality (#937 F11): the same
    `identity_equality` the image cases run, recorded as measured. A failed
    or unrunnable check is the control's problem, never a skipped check."""
    try:
        identity, _manifest = _image_admission(si, attached, canonical,
                                               sealed, directory)
    except (OSError, RuntimeError, ValueError) as exc:
        return {"passed": False, "problems": [
            f"identity equality could not run: {exc}"],
            "deviceMismatch": None, "manifestSha256": None}
    identity = dict(identity)
    identity["problems"] = list(identity.get("problems") or [])
    if not identity.get("passed") and not identity["problems"]:
        identity["problems"].append("identity equality did not pass")
    return identity


def _cursor_identities(evidence: dict) -> dict:
    return {table: {path: (row.get("after") or {}) for path, row in
                    rows.items()}
            for table, rows in evidence["cursors"].items()}


def run_relocation_oracle(args) -> dict:
    """Spec §6.2 for one tree: build the small corpus in an image at the
    oracle's own canonical root, normalized by the tree, append the owed
    input, seal O1, then run Control, RED (v1), GREEN (v2), Clean
    verification, Replacement (Codex, Claude) and Append and discovery,
    each writing `W/oracle/<tree>/<case>.json` with store-based evidence.
    RED must show a replay (v1 fails) and every other case must pass."""
    si = _soak_image()
    working = pathlib.Path(args.root).expanduser().resolve()
    tree_sha = _resolve_commit(args.tree)
    stamp = _utc_stamp()
    canonical = pathlib.Path(getattr(args, "canonical_root", None) or (
        si.CANONICAL_ROOT_DEFAULT.parent / f"cctally-937-oracle-{stamp}"))
    out = working / "oracle" / tree_sha
    if os.path.lexists(out):
        raise ValueError(f"{out} exists; the oracle never overwrites a tree's "
                         "evidence")
    out.mkdir(parents=True)
    summary = {"schemaVersion": 1, "kind": ORACLE_KIND, "treeSha": tree_sha,
               "startedAt": _utc_now(), "workingRoot": str(working),
               "canonicalRoot": str(canonical), "cases": {}, "problems": [],
               "command": [str(a) for a in sys.argv]}
    cases = summary["cases"]

    def write_case(name: str, case: dict) -> None:
        case["case"] = name
        cases[name] = {"passed": case.get("passed"),
                       "expected": case.get("expected"),
                       "problems": case.get("problems") or []}
        _write_new_json(out / f"{name}.json", case)

    job = _V2Job(working_root=working, canonical_root=canonical, kind="oracle",
                 deadline_s=si.BudgetWatchdog.KINDS["oracle"]["seconds"],
                 ledger=out / "budget.json")
    o1 = working / f"oracle-{tree_sha[:12]}-o1-{stamp}.sparsebundle"
    try:
        with job:
            lifecycle = job.lifecycle
            checkout = materialize_checkout_ref(
                tree_sha, working / "checkouts" / f"oracle-{tree_sha[:12]}-{stamp}",
                env=_v2_env(_V2Step("oracle-git", working)))
            runner = job.run(out / "logs")
            # O1: the small corpus, normalized by the tree, owed input.
            staging = out / "staging"
            _build_fixture(staging, "small", ORACLE_SEED,
                           env=_v2_env(_V2Step("oracle-build", working)))
            lifecycle.create(o1)
            attached = lifecycle.attach(o1)
            build = summary["o1"] = {"bundle": str(o1),
                                     "image": attached.as_dict()}

            def ignore(parent, names):
                relative = pathlib.Path(parent).relative_to(staging)
                if relative == pathlib.Path("home/.claude"):
                    return {"settings.json"} & set(names)
                if relative.parent == pathlib.Path(".") and \
                        relative.name.startswith("codex-"):
                    return {"hooks.json", "config.toml"} & set(names)
                return set()
            for entry in sorted(staging.iterdir()):
                if entry.is_dir() and (entry.name in ("claude", "home")
                                       or entry.name.startswith("codex-")):
                    shutil.copytree(entry, canonical / entry.name,
                                    ignore=ignore)
            (canonical / "data").mkdir()
            shutil.copy2(staging / "data" / "config.json",
                         canonical / "data" / "config.json")
            env = _v2_env(_V2Step("oracle-normalize", working, canonical))
            _checked(runner(_product(checkout, "config", "set",
                                     "update.check.enabled", "false"),
                            env=env, capture_output=True, text=True,
                            timeout=job.remaining()), "config set")
            _checked(runner(_product(checkout, "db", "rebuild", "--db",
                                     "stats", "--json"),
                            env=env, capture_output=True, text=True,
                            timeout=job.remaining()), "db rebuild --db stats")
            _checked(runner(_product(checkout, "cache-sync", "--rebuild",
                                     "--source", "all"),
                            env=env, capture_output=True, text=True,
                            timeout=job.remaining()),
                     "cache-sync --rebuild --source all")
            build["settle"] = _oracle_catchup(checkout, env, out / "o1-settle",
                                              runner, job)
            if build["settle"]["invalid"]:
                raise _V2Refusal(f"O1's settling drain: "
                                 f"{build['settle']['invalid']}")
            targets = _oracle_targets(canonical)
            build["owed"] = owed = _append_owed_input(canonical, targets)
            build["checkpoints"] = _checkpoint_stores(canonical / "data")
            sealed = si.inventory_record(canonical)
            teardown = lifecycle.detach(attached)
            build["detach"] = teardown.as_dict()
            if teardown.problems:
                raise _V2Refusal("O1 detach: " + "; ".join(teardown.problems))
            build["seal"] = si.bundle_seal(o1, content=False)
            job.check()

            def clone(label: str):
                bundle = working / f"oracle-{tree_sha[:12]}-{label}-{stamp}.sparsebundle"
                si.clonefile_bundle(o1, bundle)
                return bundle, lifecycle.attach(bundle)

            def release(bundle, attached_record, record):
                """Detach a case's clone; remove its bundle once clean."""
                td = lifecycle.detach(attached_record)
                record["detach"] = td.as_dict()
                record.setdefault("problems", []).extend(
                    f"detach: {p}" for p in td.problems)
                if not td.problems:
                    shutil.rmtree(bundle)

            # Control: P2 rules, default-mode admission drain.
            case = {"expected": "no replay; the owed complete records "
                                "consumed exactly", "problems": []}
            bundle, attached = clone("control")
            try:
                work = out / "control"
                # #937 F11: the control clone is held to the same P2
                # identity equality the image cases use, and the measured
                # result is recorded; it still drains in default mode.
                case["identity"] = _control_identity(
                    si, attached, canonical, sealed, work / "admission")
                summary["controlIdentity"] = case["identity"]
                case["problems"].extend(
                    f"identity: {p}" for p in case["identity"]["problems"])
                _snapshot_stores(canonical / "data", work / "before")
                case["catchup"] = _oracle_catchup(checkout, env, work, runner,
                                                  job)
                _snapshot_stores(canonical / "data", work / "after")
            finally:
                release(bundle, attached, case)
            control = _store_replay_evidence(work / "before", work / "after")
            case["evidence"] = control
            if control["replayed"]:
                case["problems"].append(f"replay: {control['replayKinds']}")
            case["problems"].extend(_consumption_problems(control, owed))
            if case["catchup"]["invalid"]:
                case["problems"].append(f"catchup: {case['catchup']['invalid']}")
            case["passed"] = not case["problems"]
            write_case("control", case)
            job.check()

            # RED (v1): `_clone_tree` of the canonical tree to another root,
            # the v1 environment, the admission drain.
            case = {"expected": "an observed replay, or catchup INVALID for "
                                "reset, replay or prune (v1 fails)",
                    "problems": []}
            bundle, attached = clone("red")
            arm_root = out / "write-budget-arm"
            try:
                # v1's `_clone_tree`, entry by entry: the volume's own
                # housekeeping is not part of the fixture.
                arm_root.mkdir()
                for entry in sorted(canonical.iterdir()):
                    if entry.name not in si.HOUSEKEEPING:
                        _clone_tree(entry, arm_root / entry.name)
            finally:
                release(bundle, attached, case)
            work = out / "red-v1"
            _snapshot_stores(arm_root / "data", work / "before")
            red_env = _write_budget_env(arm_root)
            red_env["PYTHONDONTWRITEBYTECODE"] = "1"
            case["catchup"] = _oracle_catchup(checkout, red_env, work, runner,
                                              job)
            _snapshot_stores(arm_root / "data", work / "after")
            prefix = str(canonical) + "/"
            red = _store_replay_evidence(
                work / "before", work / "after",
                relocate=lambda p: (str(arm_root) + "/" + p[len(prefix):])
                if p.startswith(prefix) else p)
            case["evidence"] = red
            catchup_problems = " ".join(case["catchup"]["problems"]).lower()
            judged = any(word in catchup_problems
                         for word in ("reset", "replay", "prune"))
            case["observed"] = {"replay": red["replayed"],
                                "catchupJudgedInvalid": judged,
                                "timedOut": "deadline" in str(
                                    case["catchup"]["invalid"] or "")}
            case["v1Failed"] = red["replayed"] or judged
            if not case["v1Failed"]:
                case["problems"].append("v1 showed no replay and catchup did "
                                        "not judge it INVALID")
            case["passed"] = case["v1Failed"]
            write_case("red-v1", case)
            shutil.rmtree(arm_root)
            job.check()

            # GREEN (v2) and the cases that follow it on the same clone.
            case = {"expected": "equals Control, no replay", "problems": []}
            bundle, attached = clone("green")
            green_teardown = {}
            try:
                work = out / "green-v2"
                case["identity"], manifest = _image_admission(
                    si, attached, canonical, sealed, work / "admission")
                if not case["identity"]["passed"]:
                    raise _V2Refusal("GREEN identity equality: " + "; ".join(
                        case["identity"]["problems"][:5]))
                _snapshot_stores(canonical / "data", work / "before")
                case["catchup"] = _oracle_catchup(checkout, env, work, runner,
                                                  job, manifest=manifest)
                _snapshot_stores(canonical / "data", work / "after")
                green = _store_replay_evidence(work / "before", work / "after")
                case["evidence"] = green
                if green["replayed"]:
                    case["problems"].append(f"replay: {green['replayKinds']}")
                if case["catchup"]["invalid"]:
                    case["problems"].append(
                        f"catchup: {case['catchup']['invalid']}")
                if case["catchup"]["inputMode"] != "image":
                    case["problems"].append(
                        f"catchup ran in {case['catchup']['inputMode']} mode")
                if green["consumed"] != control["consumed"]:
                    case["problems"].append("GREEN consumed other rows than "
                                            "Control")
                if _derived_content(out / "control" / "after") != \
                        _derived_content(work / "after"):
                    case["problems"].append("GREEN's post-drain derived rows "
                                            "differ from Control's")
                if _cursor_identities(green) != _cursor_identities(control):
                    case["problems"].append("GREEN's cursors or logical "
                                            "identities differ from Control's")
                case["passed"] = not case["problems"]
                write_case("green-v2", case)
                job.check()

                # Clean verification: a second image-mode drain is empty.
                clean = {
                    "expected": "no cursor or derived-row change",
                    "problems": []}
                work = out / "clean-verification"
                clean["identity"], manifest = _image_admission(
                    si, attached, canonical, si.inventory_record(canonical),
                    work / "admission")
                _snapshot_stores(canonical / "data", work / "before")
                clean["catchup"] = _oracle_catchup(
                    checkout, env, work, runner, job, manifest=manifest)
                _snapshot_stores(canonical / "data", work / "after")
                evidence = clean["evidence"] = _store_replay_evidence(
                    work / "before", work / "after")
                changed = [t for t, rows in evidence["cursors"].items()
                           for row in rows.values()
                           if row["before"] != row["after"]]
                if evidence["replayed"] or changed or any(
                        evidence["consumed"].values()):
                    clean["problems"].append(
                        f"the second drain changed the stores (replays "
                        f"{evidence['replayKinds']}, cursor tables {changed})")
                if clean["catchup"]["invalid"]:
                    clean["problems"].append(
                        f"catchup: {clean['catchup']['invalid']}")
                clean["passed"] = not clean["problems"]
                write_case("clean-verification", clean)
                job.check()

                # Replacement (Codex same size; Claude larger, judged; Claude
                # same size, observed only).
                work = out / "replacements"
                _snapshot_stores(canonical / "data", work / "before")
                stimuli = {
                    "codex": _replace_file(targets["replaceCodex"], grow=False),
                    "claudeLarger": _replace_file(
                        targets["replaceClaudeLarger"], grow=True),
                    "claudeSameSize": _replace_file(
                        targets["replaceClaudeSameSize"], grow=False)}
                catchup = _oracle_catchup(checkout, env, work, runner, job)
                _snapshot_stores(canonical / "data", work / "after")
                evidence = _store_replay_evidence(work / "before",
                                                  work / "after")

                def reset_problems(path, families):
                    problems = []
                    for table in families:
                        row = evidence["cursors"].get(table, {}).get(path) or {}
                        old, new = row.get("before") or {}, row.get("after") or {}
                        fresh = any(k in old and old.get(k) != new.get(k)
                                    for k in ("inode", "source_incarnation_id"))
                        if not fresh:
                            problems.append(f"{table}: no new incarnation "
                                            f"for {path}")
                        if new.get("last_byte_offset") != os.stat(path).st_size:
                            problems.append(f"{table}: {path} not re-read to "
                                            f"its end ({new.get('last_byte_offset')})")
                    return problems
                codex_case = {
                    "expected": "codex_session_files and "
                                "codex_conversation_source_files reset the "
                                "path: a new incarnation, re-read from 0",
                    "stimulus": stimuli["codex"], "catchup": catchup,
                    "problems": reset_problems(
                        stimuli["codex"]["path"],
                        ("codex_session_files",
                         "codex_conversation_source_files"))}
                codex_case["evidence"] = evidence
                same = stimuli["claudeSameSize"]["path"]
                expected_nd, nd_problems = _expected_not_drained(catchup, same)
                codex_case["expectedNotDrained"] = expected_nd
                codex_case["problems"].extend(nd_problems)
                codex_case["passed"] = not codex_case["problems"]
                write_case("replacement-codex", codex_case)
                claude_case = {
                    "expected": "conversation_source_files mints a new "
                                "source_incarnation_id and re-reads from 0",
                    "stimulus": stimuli["claudeLarger"],
                    "problems": reset_problems(
                        stimuli["claudeLarger"]["path"],
                        ("conversation_source_files",)),
                    "sameSizeObservedOnly": {
                        "stimulus": stimuli["claudeSameSize"],
                        "cursors": {t: evidence["cursors"].get(t, {}).get(same)
                                    for t in ("session_files",
                                              "conversation_source_files")},
                        "note": "neither tree detects a same-size Claude "
                                "replacement (spec §6.2); recorded, not "
                                "judged"},
                    "catchup": catchup, "evidence": evidence,
                    "expectedNotDrained": expected_nd}
                claude_case["problems"].extend(nd_problems)
                claude_case["passed"] = not claude_case["problems"]
                write_case("replacement-claude", claude_case)
                job.check()

                # Append and discovery through the real arm path.
                append = {"expected": "both appends ingested into every "
                                      "relevant family; the new rollout gets "
                                      "fresh cursors", "problems": []}
                work = out / "append-discovery"
                _snapshot_stores(canonical / "data", work / "before")
                appended = {
                    provider: str(_append_provider_event(
                        canonical, provider, f"oracle-{provider}"))
                    for provider in ("claude", "codex")}
                discovered = _new_rollout(canonical,
                                          _codex_identity_paths(canonical)[0])
                append["stimulus"] = {"appended": appended,
                                      "newRollout": discovered}
                append["catchup"] = _oracle_catchup(checkout, env, work,
                                                    runner, job)
                _snapshot_stores(canonical / "data", work / "after")
                evidence = append["evidence"] = _store_replay_evidence(
                    work / "before", work / "after")
                for provider, path in appended.items():
                    for cursor_table, derived_table in \
                            _ORACLE_PROVIDER_FAMILIES[provider]:
                        row = evidence["cursors"].get(cursor_table, {}).get(
                            path) or {}
                        if (row.get("after") or {}).get("last_byte_offset") \
                                != os.stat(path).st_size:
                            append["problems"].append(
                                f"{cursor_table}: the {provider} append to "
                                f"{path} was not ingested")
                        if derived_table and not evidence["consumed"].get(
                                derived_table, {}).get(path):
                            append["problems"].append(
                                f"{derived_table}: no row for the {provider} "
                                "append")
                for table in ("codex_session_files",
                              "codex_conversation_source_files"):
                    row = evidence["cursors"].get(table, {}).get(
                        discovered["path"]) or {}
                    if row.get("before") is not None or (
                            row.get("after") or {}).get("last_byte_offset") \
                            != discovered["size"]:
                        append["problems"].append(
                            f"{table}: the new rollout has no fresh, complete "
                            "cursor")
                expected_nd, nd_problems = _expected_not_drained(
                    append["catchup"], same)
                append["expectedNotDrained"] = expected_nd
                append["problems"].extend(nd_problems)
                append["passed"] = not append["problems"]
                write_case("append-discovery", append)
            finally:
                # The GREEN clone serves every case after it; its teardown
                # is recorded on the summary.
                release(bundle, attached, green_teardown)
                summary["greenTeardown"] = green_teardown
                summary["problems"].extend(green_teardown["problems"])
    except si.ImageInterrupted as exc:
        summary["problems"].append(f"interrupted: {exc}")
    except (si.ImageRefusal, _V2Refusal, *_V2_ERRORS) as exc:
        summary["problems"].append(_refusal_text(exc))
    summary.update(job.records)
    teardown = summary.get("teardown") or {}
    summary["problems"].extend(f"teardown: {p}"
                               for p in teardown.get("problems") or [])
    summary["problems"].extend(_stop_problems(job))
    missing = [name for name in ORACLE_CASES if name not in cases]
    summary["problems"].extend(f"{name}: not run" for name in missing)
    summary["problems"].extend(
        f"{name}: " + "; ".join(case["problems"][:3])
        for name, case in cases.items() if not case.get("passed"))
    if not summary["problems"] and o1.is_dir():
        shutil.rmtree(o1)
        summary["o1Removed"] = True
    summary["finishedAt"] = _utc_now()
    summary["redFailed"] = bool((cases.get("red-v1") or {}).get("passed"))
    summary["passed"] = not summary["problems"]
    _write_new_json(out / "oracle.json", summary)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True,
                        help="new isolated measurement root (never a live store)")
    checkout = parser.add_mutually_exclusive_group()
    checkout.add_argument(
        "--checkout", help="checkout whose bin/cctally the harness launches")
    checkout.add_argument(
        "--checkout-ref",
        help="committed git ref to materialize as a read-only baseline checkout")
    parser.add_argument(
        "--baseline-ref",
        help="run this committed ref first, then gate the current checkout against it")
    parser.add_argument(
        "--candidate-sha",
        help="full commit ID of the materialized candidate tree")
    parser.add_argument("--scale", choices=("small", "large"), default="large")
    parser.add_argument(
        "--fixture-copy", type=pathlib.Path,
        help="operator-prepared isolated production-shaped root to clone per arm")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--duration-seconds", type=float, default=600)
    parser.add_argument("--quiet-seconds", type=float, default=180)
    parser.add_argument("--sample-seconds", type=float, default=5)
    parser.add_argument("--sync-interval", type=float, default=5)
    parser.add_argument(
        "--fixture-image", type=pathlib.Path,
        help="#937 preparation v2: the tree's sealed P1 bundle "
             "(--write-budget-leg, --capture-baseline-reference)")
    parser.add_argument(
        "--image-receipt", type=pathlib.Path,
        help="#937 preparation v2: the P1 receipt of --fixture-image")
    parser.add_argument(
        "--canonical-root", type=pathlib.Path,
        help="#937 preparation v2: where images attach (default: the fixture "
             "marker's root for P0, the receipt's root thereafter)")
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--compare", type=pathlib.Path)
    parser.add_argument(
        "--evidence", type=pathlib.Path,
        help="same-SHA adversarial regime, upgrade and frontier evidence JSON")
    parser.add_argument(
        "--produce-evidence", action="store_true",
        help="execute and emit the required raw evidence bundle")
    parser.add_argument(
        "--small-pipeline", action="store_true",
        help="exercise both evidence tiers on small fixtures; never certifies full size")
    parser.add_argument(
        "--execute-regime", choices=REQUIRED_REGIMES,
        help=argparse.SUPPRESS)
    parser.add_argument(
        "--reuse-prepared-root", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--diagnostic-output", type=pathlib.Path, help=argparse.SUPPRESS)
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument(
        "--write-budget-leg", action="store_true",
        help="#901 §6.4: run the full-size write-budget leg on --fixture-copy")
    parser.add_argument(
        "--write-budget-arms", default=",".join(WRITE_BUDGET_ARMS),
        help="comma-separated subset of idle,append,backlog (all by default)")
    parser.add_argument(
        "--baseline-reference", type=pathlib.Path,
        help="#901 Q18: the qualified 56e66f07a reference the leg's append "
             "and backlog full builds are gated against (<= 1.2 x)")
    parser.add_argument(
        "--capture-baseline-reference", action="store_true",
        help="#901 Q18: with --write-budget-leg, measure the 56e66f07a "
             "append and backlog reference once on this host and fixture "
             "and write it to --output, a new file inside --root")
    parser.add_argument("--gate", action="store_true")
    parser.add_argument(
        "--prepare-fixture-image", action="store_true",
        help="#937 P0: extract the fixture's pax tar stream (stdin) into an "
             "image at the canonical root, normalize, compare and seal, "
             "sealing checkpoints after the extraction and the rebuild; "
             "writes --output (W/p0-receipt.json)")
    parser.add_argument(
        "--source-fingerprint",
        help="#937 P0: the source host's read-only metadata fingerprint")
    parser.add_argument(
        "--source-digest",
        help="#937 P0: the source host's two-sided content digest "
             "(soak_image.py digest)")
    parser.add_argument(
        "--extract-only", action="store_true",
        help="#937 P0: stop after the sealed `extracted` checkpoint; --output "
             "names the checkpoint JSON")
    parser.add_argument(
        "--resume-from", type=pathlib.Path,
        help="#937 P0: continue from this checkpoint JSON's verified clone "
             "instead of stdin (the pre-pass values come from it)")
    parser.add_argument(
        "--prepare-tree-image", action="store_true",
        help="#937 P1: one tree's settled image from --p0-receipt; writes "
             "--output (W/p1-<sha>-receipt.json)")
    parser.add_argument("--p0-receipt", type=pathlib.Path,
                        help="#937 P1: the P0 receipt")
    parser.add_argument("--tree", help="#937 P1 and the oracle: the tree")
    parser.add_argument(
        "--relocation-oracle", action="store_true",
        help="#937 §6.2: the two-tree relocation oracle for --tree")
    args = parser.parse_args(argv)
    v2_modes = [flag for flag, on in (
        ("--prepare-fixture-image", args.prepare_fixture_image),
        ("--prepare-tree-image", args.prepare_tree_image),
        ("--relocation-oracle", args.relocation_oracle)) if on]
    if (args.extract_only or args.resume_from) and \
            not args.prepare_fixture_image:
        parser.error("--extract-only and --resume-from apply only to "
                     "--prepare-fixture-image")
    if v2_modes:
        if len(v2_modes) > 1 or args.write_budget_leg:
            parser.error("choose one of --prepare-fixture-image, "
                         "--prepare-tree-image, --relocation-oracle and "
                         "--write-budget-leg")
        if args.fixture_copy:
            parser.error(f"{v2_modes[0]} is preparation v2 and refuses "
                         "--fixture-copy; the fixture arrives as P0's stdin "
                         "stream and each tree's P1 image")
        p0_inputs = ("output",) if args.resume_from else (
            "output", "source_fingerprint", "source_digest")
        need = {"--prepare-fixture-image": p0_inputs,
                "--prepare-tree-image": ("output", "p0_receipt", "tree"),
                "--relocation-oracle": ("tree",)}[v2_modes[0]]
        if args.resume_from and (args.extract_only or args.source_fingerprint
                                 or args.source_digest):
            parser.error("--resume-from continues from a checkpoint and takes "
                         "the pre-pass values from it; drop --extract-only, "
                         "--source-fingerprint and --source-digest")
        missing = ["--" + name.replace("_", "-") for name in need
                   if not getattr(args, name)]
        if missing:
            parser.error(f"{v2_modes[0]} requires {', '.join(missing)}")
        flow = {"--prepare-fixture-image": prepare_fixture_image,
                "--prepare-tree-image": prepare_tree_image,
                "--relocation-oracle": run_relocation_oracle}[v2_modes[0]]
        try:
            result = flow(args)
        except (OSError, RuntimeError, ValueError, sqlite3.Error,
                subprocess.SubprocessError) as exc:
            print(json.dumps({"schemaVersion": 1, "status": "failed",
                              "mode": v2_modes[0], "error": str(exc)},
                             sort_keys=True))
            return 2
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0 if result.get("passed") else 2
    if args.small_pipeline and not args.produce_evidence:
        parser.error("--small-pipeline requires --produce-evidence")
    if args.duration_seconds < 20 or args.sample_seconds <= 0:
        parser.error("duration must be >=20 seconds and sample interval >0")
    if args.capture_baseline_reference and not args.write_budget_leg:
        parser.error("--capture-baseline-reference requires --write-budget-leg")
    if args.write_budget_leg:
        if args.fixture_copy:
            parser.error("--write-budget-leg uses preparation v2 and refuses "
                         "--fixture-copy; pass --fixture-image <the tree's P1 "
                         "bundle> --image-receipt <its P1 receipt>")
        if not (args.fixture_image and args.image_receipt):
            parser.error("--write-budget-leg requires --fixture-image and "
                         "--image-receipt (preparation v2)")
        if args.capture_baseline_reference:
            if not args.output:
                parser.error("--capture-baseline-reference requires --output "
                             "(the reference file, inside --root)")
            if args.checkout or args.checkout_ref or args.baseline_reference:
                parser.error("--capture-baseline-reference always measures "
                             f"{WRITE_BUDGET_BASELINE_SHA}; drop --checkout, "
                             "--checkout-ref and --baseline-reference")
        sha = args.candidate_sha or subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
            text=True, check=True).stdout.strip()
        if args.capture_baseline_reference:
            try:
                reference, code = capture_write_budget_reference(args, sha)
            except (OSError, RuntimeError, ValueError, sqlite3.Error,
                    subprocess.SubprocessError) as exc:
                print(json.dumps({"schemaVersion": 1,
                                  "kind": WRITE_BUDGET_REFERENCE_KIND,
                                  "status": "failed", "error": str(exc)},
                                 sort_keys=True))
                return 2
            print(json.dumps({
                "schemaVersion": 1, "kind": WRITE_BUDGET_REFERENCE_KIND,
                "status": reference.get("status") or (
                    "complete" if reference.get("valid") else "invalid"),
                "output": str(args.output), "problems": reference["problems"],
                "arms": {name: {key: (arm.get("fullBuild") or {}).get(key)
                                for key in ("n", "p50Ms", "p95Ms")}
                         for name, arm in (reference.get("arms") or {}).items()},
            }, indent=2, sort_keys=True))
            return code
        try:
            leg = run_write_budget_leg(args, sha)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            print(json.dumps({"schemaVersion": 1, "kind": "writeBudgetLeg",
                              "status": "failed", "error": str(exc)},
                             sort_keys=True))
            return 2
        rendered = json.dumps(leg, indent=2, sort_keys=True, default=str) + "\n"
        if args.output:
            args.output.write_text(rendered)
        print(rendered, end="")
        if leg["invalid"]:
            return 2
        return 1 if args.gate and leg["problems"] else 0
    if args.execute_regime:
        try:
            metrics = _execute_regime(args, args.execute_regime)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
            return 2
        print(json.dumps(metrics, sort_keys=True, allow_nan=False))
        return 0

    baseline_ref = args.baseline_ref
    comparison_receipt = None
    if args.compare:
        comparison_receipt = json.loads(args.compare.read_text())
        baseline_ref = comparison_receipt.get("checkoutRef")
    candidate_sha = args.candidate_sha or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
        text=True, check=True,
    ).stdout.strip()
    if args.produce_evidence:
        problems = []
        if args.checkout_ref or (
            args.checkout and pathlib.Path(args.checkout).resolve() != REPO
        ):
            problems.append("candidate checkout is not the committed current tree")
        if not args.candidate_sha:
            problems.append("--candidate-sha is required for exact-tree evidence")
        else:
            identity_problem = _candidate_tree_identity_problem(
                REPO, candidate_sha)
            if identity_problem:
                problems.append(identity_problem)
        try:
            resolved_baseline = subprocess.run(
                ["git", "rev-parse", "--verify",
                 f"{args.baseline_ref}^{{commit}}"],
                cwd=REPO, capture_output=True, text=True, check=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, TypeError):
            resolved_baseline = ""
        if resolved_baseline != PRE_EPIC_BASELINE:
            problems.append(
                f"baseline {args.baseline_ref} is not true pre-epic "
                f"{PRE_EPIC_BASELINE}")
        if problems:
            print(json.dumps({
                "schemaVersion": 1, "status": "refused",
                "candidateSha": candidate_sha, "problems": problems,
            }, indent=2, sort_keys=True))
            return 2
        try:
            manifest = _produce_evidence(args, candidate_sha)
        except (OSError, RuntimeError, ValueError) as exc:
            print(json.dumps({
                "schemaVersion": 1, "status": "failed",
                "candidateSha": candidate_sha, "error": str(exc),
            }, indent=2, sort_keys=True))
            return 2
        print(json.dumps({
            "schemaVersion": 1, "status": "complete",
            "candidateSha": candidate_sha, "manifest": str(manifest),
        }, indent=2, sort_keys=True))
        return 0
    if args.gate:
        identity_problems = []
        if args.checkout_ref or (
            args.checkout and pathlib.Path(args.checkout).resolve() != REPO
        ):
            identity_problems.append(
                "candidate checkout is not the committed current tree")
        if args.candidate_sha:
            identity_problem = _candidate_tree_identity_problem(
                REPO, candidate_sha)
            if identity_problem:
                identity_problems.append(identity_problem)
        else:
            dirty = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=REPO, capture_output=True, text=True, check=True,
            ).stdout.strip()
            if dirty:
                identity_problems.append("candidate tree differs from its committed SHA")
            if args.evidence:
                identity_problems.append(
                    "--candidate-sha is required for exact-tree evidence")
        if not baseline_ref:
            identity_problems.append("exact pre-epic baseline is missing")
        else:
            try:
                resolved_baseline = subprocess.run(
                    ["git", "rev-parse", "--verify", f"{baseline_ref}^{{commit}}"],
                    cwd=REPO, capture_output=True, text=True, check=True,
                ).stdout.strip()
            except subprocess.CalledProcessError:
                resolved_baseline = ""
            if resolved_baseline != PRE_EPIC_BASELINE:
                identity_problems.append(
                    f"baseline {baseline_ref} is not true pre-epic "
                    f"{PRE_EPIC_BASELINE}")
        if identity_problems:
            refused = {
                "schemaVersion": 1, "candidateSha": candidate_sha,
                "baselineRef": baseline_ref, "problems": identity_problems,
            }
            rendered = json.dumps(
                _summary(refused) if args.summary_only else refused,
                indent=2, sort_keys=True) + "\n"
            if args.output:
                args.output.write_text(rendered)
            print(rendered, end="")
            return 1
    comparison_problems: list[str] = []
    with contextlib.ExitStack() as stack:
        if args.checkout_ref:
            args.checkout_label = args.checkout_ref
            scratch = pathlib.Path(stack.enter_context(
                tempfile.TemporaryDirectory(prefix="cctally-dashboard-soak-ref-")))
            args.checkout = str(materialize_checkout_ref(
                args.checkout_ref, scratch / "checkout"))
        if args.baseline_ref:
            baseline_scratch = pathlib.Path(stack.enter_context(
                tempfile.TemporaryDirectory(
                    prefix="cctally-dashboard-soak-baseline-")))
            baseline_args = argparse.Namespace(**vars(args))
            baseline_args.checkout = str(materialize_checkout_ref(
                args.baseline_ref, baseline_scratch / "checkout"))
            baseline_args.checkout_label = args.baseline_ref
            baseline_args.root = str(pathlib.Path(args.root) / "before")
            before = run_soak(baseline_args)
            args.root = str(pathlib.Path(args.root) / "after")
            receipt = run_soak(args)
            receipt["comparison"] = _comparison(before, receipt)
            comparison_problems.extend(
                _comparison_problems(receipt["comparison"]))
            if args.gate:
                comparison_problems.extend(
                    _fixture_identity_problems(before, receipt))
        else:
            receipt = run_soak(args)
    if args.compare:
        receipt["comparison"] = _comparison(
            comparison_receipt, receipt)
        comparison_problems.extend(
            _comparison_problems(receipt["comparison"]))
        if args.gate:
            comparison_problems.extend(
                _fixture_identity_problems(comparison_receipt, receipt))
    receipt["baselineRef"] = baseline_ref
    receipt["candidateSha"] = candidate_sha
    evidence_problems: list[str] = []
    if args.evidence:
        try:
            receipt = _merge_gate_evidence(
                receipt, _strict_json(args.evidence.read_bytes()), candidate_sha,
                args.evidence.parent)
        except (OSError, ValueError) as exc:
            evidence_problems.append(f"supplemental evidence invalid: {exc}")
    # Re-evaluate after supplemental evidence; otherwise stale "missing regime"
    # problems from the direct soak remain even when the exact-SHA matrix is
    # present. Comparison and evidence errors are independent and retained.
    receipt["problems"] = (
        evaluate_receipt(receipt) + comparison_problems + evidence_problems)
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    if args.summary_only:
        print(json.dumps(_summary(receipt), indent=2, sort_keys=True))
    else:
        print(rendered, end="")
    return 1 if args.gate and receipt["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
