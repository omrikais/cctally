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
import contextlib
import http.client
import io
import json
import math
import os
import pathlib
import re
import selectors
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse

REPO = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO / "bin" / "cctally"
FIXTURE_BUILDER = REPO / "bin" / "build-bench-fixtures.py"
PROCESS_CEILING_BYTES = 1536 * 1024 * 1024
RSS_SLOPE_CEILING_BYTES_PER_SECOND = 4 * 1024 * 1024 / 60
COMBINED_CPU_DUTY_CEILING = 0.75
API_P95_CEILING_MS = 8000
THREAD_COUNT_CEILING = 64
DISK_IO_OPS_PER_SECOND_CEILING = 500
PROCESS_CPU_PERCENT_CEILING = 100.0
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


def evaluate_receipt(receipt: dict) -> list[str]:
    """Return every numeric gate breach; empty means the receipt passes."""
    ceilings = receipt.get("ceilings") or {}
    problems: list[str] = []
    samples = receipt.get("samples") or []
    if len(samples) < 4:
        problems.append("fewer than four process samples; plateau is unmeasured")
    else:
        peak = max(int(row.get("rssBytes", 0)) for row in samples)
        maximum = int(ceilings.get("processRssBytes", PROCESS_CEILING_BYTES))
        if peak > maximum:
            problems.append(f"process RSS {peak} exceeds {maximum}")
        warm = samples[len(samples) // 2:]
        slope_bound = linear_slope_confidence_bound(
            warm, "elapsedSeconds", "rssBytes")
        slope_max = float(ceilings.get(
            "rssSlopeBytesPerSecond", RSS_SLOPE_CEILING_BYTES_PER_SECOND))
        if slope_bound is None:
            problems.append(
                "post-warmup RSS slope confidence bound is unmeasured")
        elif float(slope_bound["upper"]) > slope_max:
            problems.append(
                "post-warmup RSS slope 95% upper bound "
                f"{float(slope_bound['upper']):.1f} exceeds {slope_max:.1f} B/s "
                f"(estimate {float(slope_bound['estimate']):.1f})")
        cpu_peak = max(float(row.get("cpuPercent", 0) or 0) for row in samples)
        cpu_max = float(ceilings.get(
            "processCpuPercent", PROCESS_CPU_PERCENT_CEILING))
        if cpu_peak > cpu_max:
            problems.append(
                f"whole-process CPU {cpu_peak:.3f}% exceeds {cpu_max:.3f}%")
    owners = receipt.get("owners") or {}
    if not owners:
        problems.append("no retained-memory owners were reported")
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
    combined_max = float(ceilings.get(
        "combinedCpuDuty", COMBINED_CPU_DUTY_CEILING))
    if combined is None:
        problems.append("combined main-plus-conversation CPU duty is unmeasured")
    elif float(combined) > combined_max:
        problems.append(f"combined CPU duty {float(combined):.3f} exceeds {combined_max:.3f}")
    api_latencies = receipt.get("apiLatencyMs") or []
    p95 = percentile(api_latencies, 0.95)
    api_max = float(ceilings.get("apiP95Ms", API_P95_CEILING_MS))
    if p95 is None:
        problems.append("API latency is unmeasured")
    elif p95 > api_max:
        problems.append(f"API p95 {p95:.1f}ms exceeds {api_max:.1f}ms")
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
    initial_tick_seq: int,
) -> float:
    """Return request-to-settlement latency for synchronous or queued refresh."""
    if status == 204:
        return initial_latency_ms
    if status != 202:
        raise RuntimeError(f"manual refresh answered {status}: {body[:400]!r}")
    request_id = int(json.loads(body)["request_id"])
    started = time.perf_counter() - initial_latency_ms / 1000
    deadline = time.monotonic() + 180
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


def _fixture_env(root: pathlib.Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "CCTALLY_DATA_DIR": str(root / "data"),
        "CLAUDE_CONFIG_DIR": str(root / "claude"),
        "CODEX_HOME": ",".join(str(path) for path in sorted(root.glob("codex-*"))),
        "HOME": str(root / "home"),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "CCTALLY_AS_OF": "2026-01-07T00:00:00Z",
    })
    return env


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
        try:
            path.parent.rmdir()
        except OSError:
            pass


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


def run_soak(args) -> dict:
    checkout = pathlib.Path(args.checkout or REPO).expanduser().resolve()
    binary = checkout / "bin" / "cctally"
    if not binary.is_file():
        raise ValueError(f"checkout has no bin/cctally: {checkout}")
    root = pathlib.Path(args.root).expanduser().resolve()
    _build_fixture(root, args.scale, args.seed)
    env = _fixture_env(root)
    _set_offline_config(env, binary)
    data_dir = root / "data"
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

    diagnosis_503_count, initial_diagnosis_latency = _wait_for_diagnosis(port)
    injected_503_count, recovery_latency = _exercise_diagnosis_recovery(
        port, data_dir)
    diagnosis_503_count += injected_503_count

    samples = []
    api_latency = [*initial_diagnosis_latency, *recovery_latency]
    reconnect_latency = []
    rail_latency = []
    live_tail_latency = []
    reader_latency = []
    diagnostics = []
    created: list[pathlib.Path] = []
    auth_originals: dict[pathlib.Path, bytes] = {}
    added = rebuilt = False
    memory_admission_measured = False
    memory_admission_targets: dict[str, int] = {}
    memory_admission_observed: dict[str, int] = {}
    initial_debug_status, initial_debug_raw, _ = _request(
        port, "/api/debug/backend")
    if initial_debug_status != 200:
        raise RuntimeError("debug endpoint unavailable before manual refresh")
    initial_tick_seq = int(
        (json.loads(initial_debug_raw).get("tick") or {}).get("tick_seq", 0))
    queued_status, queued_body, queued_response_latency = _post(
        port, "/api/sync?refresh=0&queue=1")
    manual_queued_latency = _settle_manual_refresh(
        port, queued_status, queued_body, queued_response_latency,
        initial_tick_seq)
    manual_status = None
    manual_refresh_latency: list[float] = []
    for _attempt in range(30):
        debug_status, debug_raw, _ = _request(port, "/api/debug/backend")
        if debug_status != 200:
            raise RuntimeError("debug endpoint unavailable before manual refresh")
        tick_seq = int((json.loads(debug_raw).get("tick") or {}).get("tick_seq", 0))
        status, body, response_latency = _post(port, "/api/sync?refresh=0")
        settled_latency = _settle_manual_refresh(
            port, status, body, response_latency, tick_seq)
        manual_status = status
        if status == 204:
            manual_refresh_latency.append(settled_latency)
            if len(manual_refresh_latency) == 21:
                break
    if len(manual_refresh_latency) < 21:
        raise RuntimeError("manual refresh did not produce 21 synchronous samples")
    large_reader_exercised = False
    started = time.monotonic()
    try:
        while time.monotonic() - started < args.duration_seconds:
            elapsed = time.monotonic() - started
            if not added and elapsed >= args.duration_seconds / 3:
                created = _copy_stress_sources(root)
                auth_originals = _rotate_codex_accounts(root)
                added = True
            if not rebuilt and elapsed >= args.duration_seconds * 2 / 3:
                _cleanup_stress_sources(created)
                created = []
                _restore_codex_accounts(auth_originals)
                auth_originals = {}
                subprocess.run(
                    [str(binary), "cache-sync", "--rebuild", "--source", "all"],
                    env=env, stdout=subprocess.DEVNULL, check=True,
                )
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
            samples.append({
                "elapsedSeconds": round(elapsed, 3),
                **process_sample,
                "diskBytes": _disk_bytes(data_dir),
                "ownerEstimatedBytes": int(memory.get("ownerEstimatedBytes", 0)),
                "ownerCeilingBytes": int(memory.get("ownerCeilingBytes", 0)),
                "tickSeq": int((diagnostic.get("tick") or {}).get("tick_seq", 0)),
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

    latest = diagnostics[-1] if diagnostics else {}
    tick = latest.get("tick") or {}
    main_duty = _cpu_duty(tick.get("records"))
    conversation_duty = _cpu_duty(tick.get("conversation_sync"))
    combined_samples = [
        duty for duty in (_combined_cpu_duty(row) for row in diagnostics)
        if duty is not None
    ]
    combined = max(combined_samples) if combined_samples else None
    owners = _peak_memory_owners(diagnostics)
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
        "checkoutRef": getattr(args, "checkout_label", None),
        "scale": args.scale,
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
            "cacheRebuild": rebuilt,
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


def _summary(receipt: dict) -> dict:
    samples = receipt.get("samples") or []
    return {
        "schemaVersion": receipt.get("schemaVersion"),
        "checkoutRef": receipt.get("checkoutRef"),
        "scale": receipt.get("scale"),
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
                        help="generator-owned isolated fixture root")
    checkout = parser.add_mutually_exclusive_group()
    checkout.add_argument(
        "--checkout", help="checkout whose bin/cctally the harness launches")
    checkout.add_argument(
        "--checkout-ref",
        help="committed git ref to materialize as a read-only baseline checkout")
    parser.add_argument(
        "--baseline-ref",
        help="run this committed ref first, then gate the current checkout against it")
    parser.add_argument("--scale", choices=("small", "large"), default="large")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--duration-seconds", type=float, default=600)
    parser.add_argument("--sample-seconds", type=float, default=5)
    parser.add_argument("--sync-interval", type=float, default=5)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--compare", type=pathlib.Path)
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args(argv)
    if args.duration_seconds < 20 or args.sample_seconds <= 0:
        parser.error("duration must be >=20 seconds and sample interval >0")

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
            receipt["problems"].extend(
                _comparison_problems(receipt["comparison"]))
        else:
            receipt = run_soak(args)
    if args.compare:
        receipt["comparison"] = _comparison(
            json.loads(args.compare.read_text()), receipt)
        receipt["problems"].extend(
            _comparison_problems(receipt["comparison"]))
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
