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
import datetime as dt
import fcntl
import hashlib
import http.client
import io
import json
import math
import os
import pathlib
import re
import selectors
import secrets
import shlex
import shutil
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
# Keep this benchmark-side copy pinned to the production retention policy with
# ``test_dashboard_soak_idle_readiness_accepts_dormant_reclaim_backlog``.
RETENTION_RECLAIM_ESCALATION_BYTES = 256 * 1024 * 1024
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


def materialize_checkout_ref(ref: str, destination: pathlib.Path) -> pathlib.Path:
    """Materialize one committed baseline without mutating repository state."""
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=False)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref],
        cwd=REPO, capture_output=True, check=True,
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


def _build_fixture(root: pathlib.Path, scale: str, seed: int) -> None:
    subprocess.run(
        [sys.executable, str(FIXTURE_BUILDER), "--scale", scale,
         "--seed", str(seed), "--out", str(root)],
        check=True,
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
    for walk_root, directories, files in os.walk(source, followlinks=False):
        for name in (*directories, *files):
            if (pathlib.Path(walk_root) / name).is_symlink():
                raise ValueError("fixture copy contains a symlink")
    shutil.copytree(source, root)
    return "isolatedProductionCopy"


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


def _require_no_reclaim_backlog(data_dir: pathlib.Path) -> dict:
    """Fail closed unless the copied conversation store has fully reclaimed."""
    path = data_dir / "conversations.db"
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            ("conversation_retention_reclaim_pending",),
        ).fetchone()
        if row is not None and row[0]:
            try:
                state = json.loads(row[0])
                pending = int(state["unreclaimed_bytes"])
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                raise ValueError("malformed reclaim backlog") from exc
            if pending < 0:
                raise ValueError("malformed reclaim backlog")
            raise ValueError(f"reclaim backlog remains: {pending} bytes")
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    if freelist:
        raise ValueError(f"reclaim backlog remains: {freelist * page_size} freelist bytes")
    return {"pendingBytes": 0, "freelistBytes": 0}


def _reclaim_backlog_snapshot(data_dir: pathlib.Path) -> dict:
    """Record the copied store's reclaim state without judging it.

    ``pendingBytes`` is the product's durable record (None when absent) and
    ``freelistBytes`` the physical freelist; the two differ once a compaction
    has emptied the freelist behind a record the product has not yet cleared.
    """
    path = data_dir / "conversations.db"
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            ("conversation_retention_reclaim_pending",),
        ).fetchone()
        pending = None
        if row is not None and row[0]:
            try:
                pending = int(json.loads(row[0])["unreclaimed_bytes"])
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                raise ValueError("malformed reclaim backlog") from exc
            if pending < 0:
                raise ValueError("malformed reclaim backlog")
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    return {"pendingBytes": pending, "freelistBytes": freelist * page_size}


def _compact_reclaim_backlog(
    binary: pathlib.Path, env: dict[str, str], data_dir: pathlib.Path, *,
    timeout_seconds: float,
) -> dict:
    """Drain a copied store's reclaim backlog with the product's compaction.

    Production reclaim is budgeted to two seconds a pass and goes dormant below
    ``RETENTION_RECLAIM_ESCALATION_BYTES`` until the next daily retention run,
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
    try:
        result = subprocess.run(
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
    made_due = (after["pendingBytes"] is not None and
                after["pendingBytes"] < RETENTION_RECLAIM_ESCALATION_BYTES)
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
    now = _utc_now()
    if provider == "claude":
        paths = sorted((root / "claude" / "projects").rglob("*.jsonl"))
        if not paths:
            raise RuntimeError("production clone has no Claude source")
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
        target = paths[0]
    elif provider == "codex":
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
        target = next((path for path in paths if str(path) in cursors), None)
        if target is None:
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
    else:
        raise ValueError(f"unknown provider {provider}")
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
):
    """Keep one real isolated dashboard alive for a named regime measurement."""
    log_path = root / "regime-dashboard.log"
    log = log_path.open("w+", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(binary), "dashboard", "--port", "0",
         "--host", "127.0.0.1", "--no-browser", "--sync-interval",
         str(sync_interval), "--tz", "Etc/UTC"],
        stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1, env=env,
    )
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
        yield types.SimpleNamespace(proc=proc, port=port)
    finally:
        try:
            selector.close()
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


def _await_warm_publication(port: int, name: str) -> None:
    """Admit an active probe only once a warm tick has published.

    The live dashboard's first tick is a cold build, and the product then
    cools down for as long as that build took (#313). On the full-size source
    the cold build took 21.6 s, so a mutation made just after the first
    publication waited 21.6 s for any tick at all (#857 Task B, 2026-09-26):
    that measures the startup build, not the cadence the probe is for. The
    measured interval still starts at the mutation; this wait is setup.
    """
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
    """Whether durable reclaim is intentionally waiting for its daily pass.

    Production continues a backlog on every conversation cycle only while it
    is at or above the escalation threshold.  A smaller positive backlog that
    made progress and exhausted its per-pass deadline remains durably pending
    until the next daily retention pass; requiring that record to disappear
    makes same-day idle readiness impossible by construction.  Malformed,
    no-progress, checkpoint-only, locked, and escalated states all fail closed.
    """
    path = data_dir / "conversations.db"
    try:
        uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
        with contextlib.closing(
            sqlite3.connect(uri, uri=True, timeout=0.1)
        ) as conn:
            row = conn.execute(
                "SELECT value FROM cache_meta WHERE key=?",
                ("conversation_retention_reclaim_pending",),
            ).fetchone()
    except sqlite3.Error:
        return False
    if row is None or not row[0]:
        return True
    try:
        state = json.loads(row[0])
        backlog = int(state.get("unreclaimed_bytes") or 0)
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        0 < backlog < RETENTION_RECLAIM_ESCALATION_BYTES
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
        before, current = {
            "cache-044": (44, 46),
            "conversations-009": (9, 10),
        }[name]
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
    if (command["argv"][0] != "bin/cctally-test-remote"
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
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args(argv)
    if args.small_pipeline and not args.produce_evidence:
        parser.error("--small-pipeline requires --produce-evidence")
    if args.duration_seconds < 20 or args.sample_seconds <= 0:
        parser.error("duration must be >=20 seconds and sample interval >0")
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
