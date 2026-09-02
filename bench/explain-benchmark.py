#!/usr/bin/env python3
"""Measure `cctally explain` against a production-scale corpus (#620 S2 B13).

Builds (or reuses) the `large` bench corpus — the ~300K-entry scale the
committed backend baseline uses — then times `cctally explain` for
`--source claude` and `--source all`, reporting the warm median and p95 over
at least twenty timed runs each.

Warm means what it says: the first runs are discarded so the page cache and
the SQLite index pages are resident, which is the state a user's second
invocation is always in. A cold first run measures the operating system's
disk, not the diagnosis.

Run through the remote wrapper, never on the development machine:

    bin/cctally-test-remote python3 bench/explain-benchmark.py
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import http.client
import json
import os
import pathlib
import re
import selectors
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse

REPO = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO / "bin" / "cctally"
BUILDER = REPO / "bin" / "build-bench-fixtures.py"

CLAUDE_MEDIAN_BUDGET_SECONDS = 1.0
CLAUDE_P95_BUDGET_SECONDS = 2.0
ALL_MEDIAN_BUDGET_SECONDS = 2.0
ALL_P95_BUDGET_SECONDS = 4.0
# A fresh dashboard process's first on-demand All-provider request. Startup is
# outside the span: the clock starts after the server has bound and ends after
# the complete JSON body arrives. This is the user-facing cold-route ceiling.
COLD_DASHBOARD_CEILING_SECONDS = 3.0
# Aggregate resident bytes across the CLI parent and its isolated Codex child.
PEAK_RSS_CEILING_BYTES = 2 * 1024 * 1024 * 1024
# Four simultaneous tabs/clients exercise the process-wide route admission.
# One All-provider build uses at most a resource tracker, a forkserver and the
# isolated Codex provider worker.  The concurrent aggregate may carry four
# response encodings, so allow 256 MiB above a separately measured one-request
# DASHBOARD process tree while retaining the existing absolute 2 GiB ceiling.
# The CLI uses a direct fork on platforms that support it; comparing the
# dashboard's forkserver/spawn tree to that different process shape is invalid.
CONCURRENT_DASHBOARD_REQUESTS = 4
CONCURRENT_DESCENDANT_PROCESS_CEILING = 3
CONCURRENT_RSS_OVER_SINGLE_ALLOWANCE_BYTES = 256 * 1024 * 1024
# Two complete provider reports (current + baseline), including all seven
# classes, execute 81 adapter statements on the over-cap adversarial corpus.
# The fixed 96 ceiling leaves bounded schema-evolution headroom while the
# dedicated width tests continue to prove the count does not scale with
# sessions, threads or source files.
ADVERSARIAL_QUERY_BUDGET = 96


def _build_corpus(scale: str, seed: int, root: pathlib.Path) -> pathlib.Path:
    completed = subprocess.run(
        [sys.executable, str(BUILDER), "--scale", scale, "--seed", str(seed),
         "--out", str(root)],
        capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"corpus build failed ({completed.returncode}):\n{completed.stderr}"
        )
    for line in completed.stdout.splitlines():
        if line.startswith("fixture: "):
            return pathlib.Path(line.split(" ", 1)[1].strip())
    raise SystemExit(f"corpus build printed no fixture path:\n{completed.stdout}")


def _time_run(argv: list[str], env: dict[str, str], *,
              bin_path: pathlib.Path = BIN) -> tuple[float, int, str]:
    start = time.perf_counter()
    completed = subprocess.run([sys.executable, str(bin_path), *argv],
                               capture_output=True, text=True, env=env)
    return time.perf_counter() - start, completed.returncode, completed.stderr


def _table_bounds(data_dir: pathlib.Path, table: str):
    """The MIN/MAX timestamp of one accounting table, as dates."""
    import datetime as dt
    import sqlite3

    conn = sqlite3.connect(f"file:{data_dir / 'cache.db'}?mode=ro", uri=True)
    try:
        low, high = conn.execute(
            f"SELECT MIN(timestamp_utc), MAX(timestamp_utc) FROM {table}"
        ).fetchone()
    except sqlite3.Error:
        return None, None
    finally:
        conn.close()
    if not high:
        return None, None
    parse = (lambda v:
             dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).date())
    return parse(low), parse(high)


def _corpus_window(data_dir: pathlib.Path, days: int) -> str:
    """Derive a date-range window that BOTH providers actually populate.

    The default `this-week` token resolves through the subscription-week
    anchor, and the synthetic corpus records no usage snapshots, so the token
    fails to resolve and the command exits 2. Timing that is timing an error
    path: the earlier run of this benchmark reported a confident 0.84s median
    over twenty invocations that had produced no report at all.

    Deriving the window from the Claude table alone has the same failure one
    layer down. The Claude and Codex corpora are emitted over different date
    spans, so a Claude-derived window can contain zero Codex entries — and
    `--source all` then reads an empty Codex population and measures one
    provider while claiming to measure two. The window is therefore anchored
    on the OVERLAP when the two tables overlap at all.
    """
    import datetime as dt

    claude_low, claude_high = _table_bounds(data_dir, "session_entries")
    if claude_high is None:
        raise SystemExit(f"corpus at {data_dir} holds no session entries")
    codex_low, codex_high = _table_bounds(data_dir, "codex_session_entries")

    low, high = claude_low, claude_high
    if codex_high is not None:
        overlap_low = max(claude_low, codex_low)
        overlap_high = min(claude_high, codex_high)
        if overlap_high >= overlap_low:
            low, high = overlap_low, overlap_high
        else:
            print(f"warning: Claude {claude_low}..{claude_high} and Codex "
                  f"{codex_low}..{codex_high} do not overlap, so no window "
                  f"can load both providers", file=sys.stderr)

    start = high - dt.timedelta(days=days - 1)
    if start < low:
        start = low
    return f"{start.isoformat()}..{high.isoformat()}"


def _provider_populations(argv: list[str], env: dict[str, str]) -> dict:
    """What each provider's report actually loaded, from one real run.

    A `--source all` timing that costs the same as one provider is evidence
    that the second provider read an empty population, not evidence that two
    providers are free. This states each provider's denominator and support so
    the recorded figure can be read against what it measured.
    """
    completed = subprocess.run([sys.executable, str(BIN), *argv],
                               capture_output=True, text=True, env=env)
    if completed.returncode != 0:
        return {"error": f"exit {completed.returncode}: {completed.stderr}"}
    payload = json.loads(completed.stdout)
    out = {}
    for result in payload["results"]:
        usd = result["denominator"]["usd"]
        coverage = result.get("coverage") or {}
        out[result["source"]] = {
            "denominatorState": usd["state"],
            "denominatorUsd": usd["value"],
            "denominatorCode": usd.get("code"),
            "supportUnits": coverage.get("supportUnits"),
            "baselineSupportUnits": max([
                int(row.get("baseline", {}).get("population", {}).get(
                    "supportUnits") or 0)
                for class_result in result.get("classes", [])
                for row in class_result.get("rows", [])
            ] or [0]),
            "baselineAvailableRows": sum(
                row.get("baseline", {}).get("state") == "available"
                for class_result in result.get("classes", [])
                for row in class_result.get("rows", [])
            ),
            "withheldFieldCount": _count_state(result, "withheld"),
        }
    return out


def _count_state(value, state: str) -> int:
    if isinstance(value, dict):
        return (int(value.get("state") == state)
                + sum(_count_state(item, state) for item in value.values()))
    if isinstance(value, list):
        return sum(_count_state(item, state) for item in value)
    return 0


def _count_withheld_classes(result: dict) -> int:
    return sum(
        class_result.get("verdict") == "withheld"
        for class_result in result.get("classes", []))


def _conversation_populations(data_dir: pathlib.Path) -> dict[str, int]:
    """The two S3 populations whose absence made the old benchmark vacuous."""
    import sqlite3

    conversations = data_dir / "conversations.db"
    cache = data_dir / "cache.db"
    conn = sqlite3.connect(f"file:{conversations}?mode=ro", uri=True)
    try:
        conn.execute(
            "ATTACH DATABASE ? AS cache_db", (f"file:{cache}?mode=ro",))
        return {
            "claudeSidechainMessages": conn.execute(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE is_sidechain=1").fetchone()[0],
            "codexConversationEvents": conn.execute(
                "SELECT COUNT(*) FROM codex_conversation_events").fetchone()[0],
            "codexConversationMessages": conn.execute(
                "SELECT COUNT(*) FROM codex_conversation_messages").fetchone()[0],
        }
    finally:
        conn.close()


def _identity_populations(data_dir: pathlib.Path) -> dict[str, int]:
    """Account, attribution and hard-history discriminators for the receipt."""
    import sqlite3

    conversations = data_dir / "conversations.db"
    cache = data_dir / "cache.db"
    conn = sqlite3.connect(f"file:{conversations}?mode=ro", uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS cache_db", (f"file:{cache}?mode=ro",))
        one = lambda sql: int(conn.execute(sql).fetchone()[0])
        return {
            "claudeMetaMessages": one(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE entry_type='meta'"),
            "claudeToolResultMessages": one(
                "SELECT COUNT(*) FROM conversation_messages "
                "WHERE entry_type='tool_result'"),
            "claudeUnattributedEntries": one(
                "SELECT COUNT(*) FROM cache_db.session_entries "
                "WHERE account_key IS NULL OR account_key='unattributed'"),
            "codexRealAccounts": one(
                "SELECT COUNT(DISTINCT account_key) "
                "FROM cache_db.codex_session_entries "
                "WHERE account_key IS NOT NULL AND account_key!='unattributed'"),
            "codexUnattributedEntries": one(
                "SELECT COUNT(*) FROM cache_db.codex_session_entries "
                "WHERE account_key IS NULL OR account_key='unattributed'"),
            "codexSubagentThreads": one(
                "SELECT COUNT(*) FROM cache_db.codex_conversation_threads "
                "WHERE root_thread_id='subagent'"),
        }
    finally:
        conn.close()


def _canonical_projection(payload: dict) -> dict:
    """The public serializer's documented parity projection."""
    projected = copy.deepcopy(payload)
    projected.pop("measuredAt", None)
    projected.pop("notes", None)
    if isinstance(projected.get("window"), dict):
        projected["window"].pop("label", None)
    for result in projected.get("results", []):
        for row in result.get("contributors", []):
            row.pop("subjectLabel", None)
        for class_result in result.get("classes", []):
            for row in class_result.get("rows", []):
                row.pop("subjectLabel", None)
    return projected


def _json_run(argv: list[str], env: dict[str, str]) -> dict:
    completed = subprocess.run(
        [sys.executable, str(BIN), *argv], capture_output=True, text=True,
        env=env,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"JSON comparison command exited {completed.returncode}:\n"
            f"{completed.stderr}")
    return json.loads(completed.stdout)


def _cold_dashboard_rounds(env: dict[str, str], window: str, rounds: int,
                           cli_payload: dict) -> dict:
    """First diagnosis request from fresh dashboard processes."""
    samples: list[float] = []
    statuses: list[int] = []
    parity: list[bool] = []
    query = urllib.parse.urlencode({"source": "all", "window": window})
    for _ in range(max(1, rounds)):
        proc = subprocess.Popen(
            [sys.executable, str(BIN), "dashboard", "--port", "0",
             "--host", "127.0.0.1", "--no-browser", "--no-sync",
             "--tz", "Etc/UTC"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            bufsize=1, env=env,
        )
        selector = selectors.DefaultSelector()
        assert proc.stdout is not None
        selector.register(proc.stdout, selectors.EVENT_READ)
        port = None
        deadline = time.monotonic() + 120.0
        startup_lines = []
        try:
            while port is None and time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                for key, _mask in selector.select(timeout=0.2):
                    line = key.fileobj.readline()
                    if not line:
                        continue
                    startup_lines.append(line.rstrip())
                    match = re.search(r"localhost:(\d+)", line)
                    if match:
                        port = int(match.group(1))
                        break
            if port is None:
                stderr = proc.stderr.read() if proc.stderr is not None else ""
                raise SystemExit(
                    "dashboard did not bind for cold-route measurement: "
                    f"stdout={startup_lines!r} stderr={stderr}")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                started = time.perf_counter()
                conn.request("GET", f"/api/diagnosis?{query}")
                response = conn.getresponse()
                body = response.read()
                samples.append(time.perf_counter() - started)
                statuses.append(response.status)
                payload = json.loads(body)
                parity.append(
                    _canonical_projection(payload)
                    == _canonical_projection(cli_payload))
            finally:
                conn.close()
        finally:
            selector.close()
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
    return {
        "ceilingSeconds": COLD_DASHBOARD_CEILING_SECONDS,
        "roundSeconds": [round(value, 4) for value in samples],
        "medianSeconds": round(statistics.median(samples), 4),
        "p95Seconds": round(_percentile(samples, 0.95), 4),
        "statuses": statuses,
        "canonicalParity": all(parity),
    }


def _peak_rss_run(argv: list[str], env: dict[str, str]) -> int:
    """Sample aggregate RSS for the command's whole live process tree."""
    proc = subprocess.Popen(
        [sys.executable, str(BIN), *argv], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env,
    )
    peak_bytes = 0
    while proc.poll() is None:
        rss_bytes, _descendants = _process_tree_sample(proc.pid)
        peak_bytes = max(peak_bytes, rss_bytes)
        time.sleep(0.02)
    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        raise SystemExit(
            f"RSS command exited {proc.returncode}:\n{stderr}\n{stdout}")
    return peak_bytes


def _process_tree_sample(root_pid: int) -> tuple[int, int]:
    """Aggregate RSS bytes and descendant count for one live process tree."""
    snapshot = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss="], capture_output=True,
        text=True, check=True,
    ).stdout
    children: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for line in snapshot.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        pid, ppid, kib = map(int, parts)
        children.setdefault(ppid, []).append(pid)
        rss[pid] = kib
    pending = [root_pid]
    tree: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in tree:
            continue
        tree.add(pid)
        pending.extend(children.get(pid, ()))
    return sum(rss.get(pid, 0) for pid in tree) * 1024, max(0, len(tree) - 1)


def _concurrent_dashboard_round(
    env: dict[str, str], window: str, cli_payload: dict,
    request_count: int = CONCURRENT_DASHBOARD_REQUESTS,
) -> dict:
    """Issue identical simultaneous route requests and bound their process tree."""
    proc = subprocess.Popen(
        [sys.executable, str(BIN), "dashboard", "--port", "0",
         "--host", "127.0.0.1", "--no-browser", "--no-sync",
         "--tz", "Etc/UTC"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        bufsize=1, env=env,
    )
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None
    selector.register(proc.stdout, selectors.EVENT_READ)
    port = None
    deadline = time.monotonic() + 120.0
    startup_lines = []
    try:
        while port is None and time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            for key, _mask in selector.select(timeout=0.2):
                line = key.fileobj.readline()
                if not line:
                    continue
                startup_lines.append(line.rstrip())
                match = re.search(r"localhost:(\d+)", line)
                if match:
                    port = int(match.group(1))
                    break
        if port is None:
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            raise SystemExit(
                "dashboard did not bind for concurrent-route measurement: "
                f"stdout={startup_lines!r} stderr={stderr}")

        query = urllib.parse.urlencode({"source": "all", "window": window})
        gate = threading.Barrier(request_count + 1)

        def _request():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:
                gate.wait(timeout=10)
                started = time.perf_counter()
                conn.request("GET", f"/api/diagnosis?{query}")
                response = conn.getresponse()
                body = response.read()
                return response.status, body, time.perf_counter() - started
            finally:
                conn.close()

        peak_rss = 0
        peak_descendants = 0
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=request_count) as executor:
            futures = [executor.submit(_request) for _ in range(request_count)]
            gate.wait(timeout=10)
            while not all(future.done() for future in futures):
                rss_bytes, descendants = _process_tree_sample(proc.pid)
                peak_rss = max(peak_rss, rss_bytes)
                peak_descendants = max(peak_descendants, descendants)
                time.sleep(0.01)
            results = [future.result() for future in futures]
            rss_bytes, descendants = _process_tree_sample(proc.pid)
            peak_rss = max(peak_rss, rss_bytes)
            peak_descendants = max(peak_descendants, descendants)

        statuses = [status for status, _body, _elapsed in results]
        bodies = [body for _status, body, _elapsed in results]
        parity = []
        for body in bodies:
            try:
                payload = json.loads(body)
            except (TypeError, ValueError):
                parity.append(False)
            else:
                parity.append(
                    _canonical_projection(payload)
                    == _canonical_projection(cli_payload)
                )
        return {
            "requestCount": request_count,
            "statuses": statuses,
            "elapsedSeconds": [
                round(elapsed, 4) for _status, _body, elapsed in results
            ],
            "uniqueBodyHashes": len({
                hashlib.sha256(body).hexdigest() for body in bodies
            }),
            "canonicalParity": all(parity),
            "peakDescendantProcesses": peak_descendants,
            "descendantProcessCeiling": CONCURRENT_DESCENDANT_PROCESS_CEILING,
            "peakRssBytes": peak_rss,
        }
    finally:
        selector.close()
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


def _profile_phases(data_dir: pathlib.Path, root: pathlib.Path,
                    window: str, runs: int) -> dict[str, dict[str, float]]:
    """Nested provider phases, outside the CLI's fixed startup cost."""
    import datetime as dt
    import collections
    import types

    os.environ["CCTALLY_DATA_DIR"] = str(data_dir)
    os.environ["CLAUDE_CONFIG_DIR"] = str(root / "claude")
    os.environ["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    os.environ["HOME"] = str(root / "home")
    sys.path.insert(0, str(REPO / "bin"))
    module = types.ModuleType("cctally")
    module.__file__ = str(BIN)
    sys.modules["cctally"] = module
    exec(compile(BIN.read_text(), str(BIN), "exec"), module.__dict__)

    import _cctally_core
    _cctally_core._init_paths_from_env()
    sources = module._load_sibling("_cctally_diagnosis_sources")
    diagnosis = module._load_sibling("_cctally_diagnosis")
    kernel = sys.modules["_lib_diagnosis"]

    start_date, end_date = window.split("..")
    low = dt.datetime.fromisoformat(start_date).replace(tzinfo=dt.timezone.utc)
    high = (dt.datetime.fromisoformat(end_date).replace(
        tzinfo=dt.timezone.utc) + dt.timedelta(days=1))
    profiles = {}
    for provider in ("claude", "codex"):
        scope = sources.DiagnosisScope(
            source=provider, account_key=None, window_start=low,
            window_end=high, display_tz="UTC",
        )
        samples: dict[str, list[float]] = collections.defaultdict(list)
        for _ in range(runs):
            elapsed: dict[str, float] = collections.defaultdict(float)
            real_open = sources.open_read_only
            real_probe = sources._probe_component
            real_read = sources._read_component
            real_class = sources.load_class_facts

            def _open(kind):
                marker = time.perf_counter()
                try:
                    return real_open(kind)
                finally:
                    elapsed[f"storeOpen.{kind}"] += (
                        time.perf_counter() - marker)

            def _probe(component, bundle):
                marker = time.perf_counter()
                try:
                    return real_probe(component, bundle)
                finally:
                    elapsed["generationProbes"] += (
                        time.perf_counter() - marker)

            def _read(component, read_scope, bundle):
                half = ("current" if read_scope.window_start == scope.window_start
                        else "baseline")
                marker = time.perf_counter()
                try:
                    return real_read(component, read_scope, bundle)
                finally:
                    spent = time.perf_counter() - marker
                    elapsed[f"{half}Evidence"] += spent
                    elapsed[f"{half}Components.{component}"] += spent

            def _class(bundle, read_scope, spec):
                marker = time.perf_counter()
                try:
                    return real_class(bundle, read_scope, spec)
                finally:
                    spent = time.perf_counter() - marker
                    elapsed["classShaping"] += spent
                    elapsed[f"classes.{spec.kind}"] += spent

            sources.open_read_only = _open
            sources._probe_component = _probe
            sources._read_component = _read
            sources.load_class_facts = _class
            marker = time.perf_counter()
            try:
                provider_result = sources.build_provider_diagnosis(
                    scope, transcripts_visible=True)
            finally:
                elapsed["requestTotal"] = time.perf_counter() - marker
                sources.open_read_only = real_open
                sources._probe_component = real_probe
                sources._read_component = real_read
                sources.load_class_facts = real_class

            report = kernel.build_report(
                sources._iso_z(scope.window_end), scope.window(),
                [provider_result])
            marker = time.perf_counter()
            diagnosis.diagnosis_to_wire(
                report, scopes={
                    provider: diagnosis._scope_for(
                        sources, scope, provider)})
            elapsed["serialization"] = time.perf_counter() - marker
            accounted = sum(elapsed[name] for name in (
                "generationProbes", "currentEvidence", "baselineEvidence",
                "classShaping"))
            elapsed["requestOverhead"] = max(
                0.0, elapsed["requestTotal"] - accounted)
            for name, value in elapsed.items():
                samples[name].append(value)

        profiles[provider] = {
            name: round(statistics.median(values), 4)
            for name, values in sorted(samples.items())
        }
    return profiles


def _withheld_probe(data_dir: pathlib.Path, root: pathlib.Path,
                    window: str) -> dict[str, dict[str, int]]:
    """Privacy-denied applicability and typed empty-scope withholding."""
    import datetime as dt

    os.environ["CCTALLY_DATA_DIR"] = str(data_dir)
    os.environ["CLAUDE_CONFIG_DIR"] = str(root / "claude")
    os.environ["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    os.environ["HOME"] = str(root / "home")
    module = sys.modules.get("cctally")
    if module is None or not hasattr(module, "_load_sibling"):
        import types
        module = types.ModuleType("cctally")
        module.__file__ = str(BIN)
        sys.modules["cctally"] = module
        exec(compile(BIN.read_text(), str(BIN), "exec"), module.__dict__)
    import _cctally_core
    _cctally_core._init_paths_from_env()
    sources = module._load_sibling("_cctally_diagnosis_sources")
    diagnosis = module._load_sibling("_cctally_diagnosis")
    start_date, end_date = window.split("..")
    scope = sources.DiagnosisScope(
        source="all", account_key=None,
        window_start=dt.datetime.fromisoformat(start_date).replace(
            tzinfo=dt.timezone.utc),
        window_end=(dt.datetime.fromisoformat(end_date).replace(
            tzinfo=dt.timezone.utc) + dt.timedelta(days=1)),
        display_tz="UTC",
    )
    privacy_report = sources.build_diagnosis(
        scope, measured_at=scope.window_end, transcripts_visible=False)
    scopes = {
        result.source: diagnosis._scope_for(sources, scope, result.source)
        for result in privacy_report.results
    }
    privacy_payload = diagnosis.diagnosis_to_wire(
        privacy_report, scopes=scopes)
    empty_scope = sources.DiagnosisScope(
        source="all", account_key="benchmark-empty-account",
        window_start=scope.window_start, window_end=scope.window_end,
        display_tz="UTC",
    )
    empty_report = sources.build_diagnosis(
        empty_scope, measured_at=scope.window_end, transcripts_visible=True)
    empty_scopes = {
        result.source: diagnosis._scope_for(
            sources, empty_scope, result.source)
        for result in empty_report.results
    }
    empty_payload = diagnosis.diagnosis_to_wire(
        empty_report, scopes=empty_scopes)
    return {
        "privacyWithheldClasses": {
            result["source"]: _count_withheld_classes(result)
            for result in privacy_payload["results"]
        },
        "emptyAccountWithheldClasses": {
            result["source"]: _count_withheld_classes(result)
            for result in empty_payload["results"]
        },
    }


# --- the three budget bounds (#620 S3 §6.8, C15) ------------------------
#
# Three read paths in this design are CAPPED rather than structurally bounded —
# the compaction seed scan, the per-conversation normalization and the Codex
# event inference — and the structural alternative (persisted turn attribution)
# was deliberately scoped to its own session. Whether a cap ever binds is an
# empirical question about real stores, so the caps are asserted here against
# corpora built to exceed them, and the real-store gate reports the headroom.
#
# Each case must render UNEVALUABLE with `scan_budget_exhausted` — never short,
# never long, never mis-attributed. A bound that held while the subject was
# silently classified would be the worse outcome.


def _bench_module(root: pathlib.Path):
    """Load `cctally` against a scratch data dir, the way `_profile_phases`
    does. Kept separate so a bounds run needs no bench corpus at all."""
    import types

    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["CCTALLY_DATA_DIR"] = str(data_dir)
    os.environ["CLAUDE_CONFIG_DIR"] = str(root / "claude")
    os.environ["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    os.environ["CCTALLY_DISABLE_UPDATE_CHECK"] = "1"
    os.environ["HOME"] = str(root / "home")
    sys.path.insert(0, str(REPO / "bin"))
    module = types.ModuleType("cctally")
    module.__file__ = str(BIN)
    sys.modules["cctally"] = module
    exec(compile(BIN.read_text(), str(BIN), "exec"), module.__dict__)
    import _cctally_core

    _cctally_core._init_paths_from_env()
    return module, data_dir


_WINDOW_START = "2026-08-10T00:00:00+00:00"
_WINDOW_END = "2026-08-17T00:00:00+00:00"


def _seed_adversarial(module, kernel) -> None:
    """Three corpora, each built to exceed exactly one cap.

    Seeded through the real openers, so the column set is production's.
    """
    import datetime as dt

    start = dt.datetime(2026, 8, 10, tzinfo=dt.timezone.utc)
    seed_rows = kernel.DIAGNOSIS_SEED_SCAN_BUDGET_ROWS + 10_000
    normalize_rows = kernel.DIAGNOSIS_CONVERSATION_NORMALIZE_BUDGET_ROWS + 1
    event_rows = kernel.DIAGNOSIS_CODEX_EVENT_SCAN_PER_FILE_ROWS + 1

    # stats.db must EXIST, or the provider is withheld as
    # `provider_unavailable` before any class shapes a population and the
    # bounds below are never exercised at all.
    module.open_db().close()

    cache = module.open_cache_db()
    try:
        cache.execute(
            "INSERT OR IGNORE INTO session_files (path, size_bytes, mtime_ns,"
            " last_byte_offset, last_ingested_at, session_id, project_path)"
            " VALUES (?,?,?,?,?,?,?)",
            ("/bench/long.jsonl", 0, 0, 0, "2026-08-10T00:00:00Z",
             "sess-long", "/repo/bench"))
        cache.execute(
            "INSERT OR IGNORE INTO session_files (path, size_bytes, mtime_ns,"
            " last_byte_offset, last_ingested_at, session_id, project_path)"
            " VALUES (?,?,?,?,?,?,?)",
            ("/bench/normalize.jsonl", 0, 0, 0,
             "2026-08-10T00:00:00Z", "sess-normalize", "/repo/bench"))
        cache.executemany(
            "INSERT INTO session_entries (source_path, line_offset,"
            " timestamp_utc, model, input_tokens, output_tokens,"
            " cache_create_tokens, cache_read_tokens, cache_create_1h_tokens,"
            " account_key, msg_id, req_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [("/bench/long.jsonl", index,
              (start + dt.timedelta(hours=1, minutes=index)).isoformat(),
              "claude-opus-4-20250514", 1000, 500, 40_000, 100, 0,
              "unattributed", f"msg-w{index}", f"req-w{index}")
             for index in range(30)])
        # The normalize-over-cap conversation is a spending candidate. The
        # optimized reader derives its candidate session set from the already
        # established accounting population, so a transcript-only session is
        # intentionally irrelevant to a cost diagnosis and would make this
        # adversarial leg vacuous.
        cache.execute(
            "INSERT INTO session_entries (source_path, line_offset,"
            " timestamp_utc, model, input_tokens, output_tokens,"
            " cache_create_tokens, cache_read_tokens, cache_create_1h_tokens,"
            " account_key, msg_id, req_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("/bench/normalize.jsonl", 1,
             (start + dt.timedelta(hours=2)).isoformat(),
             "claude-opus-4-20250514", 1000, 500, 100, 100, 0,
             "unattributed", "norm-msg", "norm-req"))
        # The Codex half: in-window spend on one conversation whose source file
        # carries the long pre-window event history seeded below.
        cache.execute(
            "INSERT OR IGNORE INTO codex_session_files (path, size_bytes,"
            " mtime_ns, last_byte_offset, last_ingested_at, last_session_id,"
            " last_model, source_root_key) VALUES (?,?,?,?,?,?,?,?)",
            ("/bench/codex.jsonl", 0, 0, 0, "2026-08-10T00:00:00Z", "sess-0",
             "gpt-5.3-codex", "root-bench"))
        cache.execute(
            "INSERT INTO codex_conversation_threads (conversation_key,"
            " source_root_key, native_thread_id, root_thread_id,"
            " parent_thread_id, source_path, cwd, git_json, context_window)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("v1.root-bench.0", "root-bench", "t1", "user", None,
             "/bench/codex.jsonl", "/repo/bench", None, 400_000))
        cache.executemany(
            "INSERT INTO codex_session_entries (source_path, line_offset,"
            " timestamp_utc, session_id, model, input_tokens,"
            " cached_input_tokens, output_tokens, reasoning_output_tokens,"
            " total_tokens, source_root_key, conversation_key, account_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [("/bench/codex.jsonl", 900_000 + index,
              (start + dt.timedelta(hours=1, minutes=index)).isoformat()
              .replace("+00:00", "Z"),
              "sess-0", "gpt-5.3-codex", 360_000, 100, 500, 100, 360_600,
              "root-bench", "v1.root-bench.0", "unattributed")
             for index in range(30)])
        cache.commit()
    finally:
        cache.close()

    conv = module.open_conversations_db()
    try:
        # Case A: a very long pre-window history with no compaction anywhere,
        # so the seed can never be established within the session's share.
        conv.executemany(
            "INSERT INTO conversation_messages (session_id, uuid, parent_uuid,"
            " source_path, byte_offset, timestamp_utc, entry_type, text,"
            " blocks_json, model, msg_id, req_id, cwd, git_branch,"
            " is_sidechain) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [("sess-long", f"long-{index}", None, "/bench/long.jsonl", index,
              (start - dt.timedelta(minutes=seed_rows - index)).isoformat(),
              "assistant", "a reply",
              json.dumps([{"kind": "text", "text": "a reply"}]),
              "claude-opus-4-20250514", None, None, "/repo/bench", "main", 0)
             for index in range(seed_rows)])
        conv.executemany(
            "INSERT INTO conversation_messages (session_id, uuid, parent_uuid,"
            " source_path, byte_offset, timestamp_utc, entry_type, text,"
            " blocks_json, model, msg_id, req_id, cwd, git_branch,"
            " is_sidechain) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [("sess-long", f"win-{index}", None, "/bench/long.jsonl",
              seed_rows + index,
              (start + dt.timedelta(hours=1, minutes=index)).isoformat(),
              "assistant", "a reply",
              json.dumps([{"kind": "text", "text": "a reply"}]),
              "claude-opus-4-20250514", f"msg-w{index}", f"req-w{index}",
              "/repo/bench", "main", 0)
             for index in range(30)])
        # Case B: three human turns behind a very long tool and meta history.
        conv.executemany(
            "INSERT INTO conversation_messages (session_id, uuid, parent_uuid,"
            " source_path, byte_offset, timestamp_utc, entry_type, text,"
            " blocks_json, model, msg_id, req_id, cwd, git_branch,"
            " is_sidechain) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [("sess-normalize", f"norm-{index}", None,
              "/bench/normalize.jsonl", index,
              (start + dt.timedelta(hours=2, seconds=index)).isoformat(),
              "human" if index < 3 else "meta",
              "please do the thing" if index < 3 else "tool result",
              json.dumps([{"kind": "text", "text": "body"}]),
              None, None, None, "/repo/bench", "main", 0)
             for index in range(normalize_rows)])
        # Case C: in-window Codex entries behind a very long pre-window event
        # history in ONE source file.
        conv.executemany(
            "INSERT INTO codex_conversation_events (source_path, line_offset,"
            " source_root_key, conversation_key, native_thread_id,"
            " root_thread_id, parent_thread_id, timestamp_utc, record_type,"
            " event_type, turn_id, call_id, payload_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [("/bench/codex.jsonl", index, "root-bench", "v1.root-bench.0",
              "t1", "user", None,
              (start - dt.timedelta(minutes=event_rows - index)).isoformat(),
              "event_msg", "token_count", None, None,
              json.dumps({"payload": {"type": "token_count"}}))
             for index in range(event_rows)])
        # And a canonical prompt, so the Codex predicate has a conversation to
        # decide rather than returning on an empty candidate set.
        conv.executemany(
            "INSERT INTO codex_conversation_messages (conversation_key,"
            " source_root_key, source_path, line_offset, timestamp_utc,"
            " turn_id, kind, record_family, content_digest, content_len, text)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [("v1.root-bench.0", "root-bench", "/bench/codex.jsonl",
              800_000 + index,
              (start + dt.timedelta(hours=1, minutes=index)).isoformat(),
              "turn-a", "user", "response_item", f"seed-{index}", 5, "hello")
             for index in range(2)])
        conv.commit()
    finally:
        conv.close()


def _run_bounds(root: pathlib.Path) -> dict:
    import datetime as dt

    module, _data_dir = _bench_module(root)
    sources = module._load_sibling("_cctally_diagnosis_sources")
    kernel = sys.modules["_lib_diagnosis"]
    _seed_adversarial(module, kernel)

    # Instrument the ONE query chokepoint every read in the adapter goes
    # through, so the counts describe what the store actually returned rather
    # than what the source is believed to ask for.
    observed: dict[str, int] = {}
    query_count = 0
    # The ALLOCATED share per session, captured from the allocator itself. The
    # seed budget is split across the sessions in the window, so "the seed scan
    # never parses beyond its allocated share" is a claim about that share and
    # not about the whole budget — asserting the whole budget would pass on a
    # one-session window for the wrong reason.
    #
    # The current and the baseline window receive SEPARATE budgets, so this
    # merges two allocations and the reported share is the largest of them.
    # That is the right bound to assert here: it is the widest share any one
    # session was given, and no session may parse past its own.
    allocations: dict = {}
    real_allocate = sources.allocate_scan_budget

    def _recording_allocate(keys, budget):
        result = real_allocate(keys, budget)
        if budget == kernel.DIAGNOSIS_SEED_SCAN_BUDGET_ROWS:
            allocations.update(result)
        return result

    real_execute = sources._execute

    def _counting(conn, sql, params=()):
        nonlocal query_count
        query_count += 1
        rows = real_execute(conn, sql, params)
        # Counted PER SUBJECT, because that is the bound the design states:
        # every budget is allocated per session, per conversation or per source
        # file so no subject can starve another. A per-statement total would
        # scale with the number of subjects in the chunk and would either fail
        # on a wide window or pass on a narrow one for the wrong reason.
        for label, template, key in (
            ("seed_scan", sources._CLAUDE_SEED_PREFIX_SQL, "session_id"),
            ("normalize", sources._CLAUDE_TURN_CANDIDATE_SQL, "session_id"),
        ):
            head = template.split("{placeholders}")[0]
            if sql.startswith(head):
                per_subject: dict = {}
                for row in rows:
                    per_subject[row[key]] = per_subject.get(row[key], 0) + 1
                observed[label] = max(
                    [observed.get(label, 0), *per_subject.values()])
        if sql.startswith(sources._CODEX_EVENT_BUDGET_TERM_SQL):
            # One compound statement carries a bounded arm per physical file;
            # every returned count is capped at allocated-share + 1.
            observed["codex_events"] = max(
                [observed.get("codex_events", 0),
                 *(int(row["rows_read"]) for row in rows)])
        return rows

    sources._execute = _counting
    sources.allocate_scan_budget = _recording_allocate
    try:
        scope = sources.DiagnosisScope(
            source="claude", account_key=None,
            window_start=dt.datetime.fromisoformat(_WINDOW_START),
            window_end=dt.datetime.fromisoformat(_WINDOW_END),
            display_tz="Etc/UTC",
        )
        claude = sources.build_diagnosis(scope, transcripts_visible=True)
        codex_scope = sources.DiagnosisScope(
            source="codex", account_key=None,
            window_start=dt.datetime.fromisoformat(_WINDOW_START),
            window_end=dt.datetime.fromisoformat(_WINDOW_END),
            display_tz="Etc/UTC",
        )
        sources.build_diagnosis(codex_scope, transcripts_visible=True)
    finally:
        sources._execute = real_execute
        sources.allocate_scan_budget = real_allocate

    # `+ 1` on each: every statement fetches ONE row past its share, which is
    # how a subject that holds more history than its budget is visible as
    # exhausted without a second query per subject.
    seed_share = max(allocations.values(), default=0)
    bounds = {
        "adapterQueries": query_count,
        "adapterQueryBudget": ADVERSARIAL_QUERY_BUDGET,
        "seedSessions": len(allocations),
        "seedScanAllocatedShare": seed_share,
        "seedScanRowsParsed": observed.get("seed_scan", 0),
        "seedScanBudget": seed_share + 1,
        "normalizeRowsRead": observed.get("normalize", 0),
        "normalizeBudget":
            kernel.DIAGNOSIS_CONVERSATION_NORMALIZE_BUDGET_ROWS + 1,
        "codexEventRowsRead": observed.get("codex_events", 0),
        "codexEventBudget":
            kernel.DIAGNOSIS_CODEX_EVENT_SCAN_PER_FILE_ROWS + 1,
    }
    problems = []
    for measured, budget in (("seedScanRowsParsed", "seedScanBudget"),
                             ("normalizeRowsRead", "normalizeBudget"),
                             ("codexEventRowsRead", "codexEventBudget")):
        if bounds[measured] == 0:
            problems.append(f"{measured}: the path never ran, so the bound "
                            f"was never exercised")
        elif bounds[measured] > bounds[budget]:
            problems.append(f"{measured} {bounds[measured]} exceeds "
                            f"{bounds[budget]}")
    if query_count > ADVERSARIAL_QUERY_BUDGET:
        problems.append(
            f"adapterQueries {query_count} exceeds "
            f"{ADVERSARIAL_QUERY_BUDGET}")

    # And the verdict each case must render. A bound that held while the
    # subject was silently classified short or long would be worse than one
    # that did not.
    classes = {c.contributor_class: c for c in claude.results[0].classes}
    for kind in ("cache_churn", "short_high_context"):
        gaps = tuple(classes[kind].population.gap_codes or ())
        if kernel.GAP_SCAN_BUDGET_EXHAUSTED not in gaps:
            problems.append(f"{kind}: no scan_budget_exhausted gap, got {gaps}")
    bounds["problems"] = problems
    return bounds


def _percentile(values: list[float], fraction: float) -> float:
    """The nearest-rank percentile.

    Deliberately not an interpolating estimator: with twenty samples an
    interpolated p95 is a weighted average of the two slowest runs, which
    reads lower than any run actually observed.
    """
    ordered = sorted(values)
    rank = max(1, int(round(fraction * len(ordered))))
    return ordered[min(rank, len(ordered)) - 1]


def _performance_problems(report: dict) -> list[str]:
    """Fail-closed acceptance predicates for the published receipt."""
    problems = []
    budgets = {
        "claude": (CLAUDE_MEDIAN_BUDGET_SECONDS,
                   CLAUDE_P95_BUDGET_SECONDS),
        "all": (ALL_MEDIAN_BUDGET_SECONDS, ALL_P95_BUDGET_SECONDS),
    }
    for case_name, (median_budget, p95_budget) in budgets.items():
        case = report["cases"][case_name]
        if case["medianSeconds"] > median_budget:
            problems.append(
                f"{case_name} median {case['medianSeconds']}s exceeds "
                f"{median_budget}s")
        if case["p95Seconds"] > p95_budget:
            problems.append(
                f"{case_name} p95 {case['p95Seconds']}s exceeds "
                f"{p95_budget}s")
    cold = report["coldDashboard"]
    if cold["p95Seconds"] > COLD_DASHBOARD_CEILING_SECONDS:
        problems.append(
            f"cold dashboard p95 {cold['p95Seconds']}s exceeds "
            f"{COLD_DASHBOARD_CEILING_SECONDS}s")
    if any(status != 200 for status in cold["statuses"]):
        problems.append(f"cold dashboard statuses were {cold['statuses']}")
    if not cold["canonicalParity"]:
        problems.append("CLI/dashboard canonical projection differs")
    if report["peakRssBytes"] > PEAK_RSS_CEILING_BYTES:
        problems.append(
            f"peak RSS {report['peakRssBytes']} exceeds "
            f"{PEAK_RSS_CEILING_BYTES}")
    concurrent = report["concurrentDashboard"]
    single_dashboard = report["singleDashboardProcessTree"]
    if single_dashboard["peakRssBytes"] > PEAK_RSS_CEILING_BYTES:
        problems.append(
            "single dashboard process-tree RSS "
            f"{single_dashboard['peakRssBytes']} exceeds "
            f"{PEAK_RSS_CEILING_BYTES}")
    if any(status != 200 for status in concurrent["statuses"]):
        problems.append(
            "concurrent dashboard statuses were "
            f"{concurrent['statuses']}")
    if not concurrent["canonicalParity"]:
        problems.append("concurrent dashboard canonical projection differs")
    if concurrent["uniqueBodyHashes"] != 1:
        problems.append(
            "identical concurrent dashboard requests did not share one body")
    if (concurrent["peakDescendantProcesses"]
            > concurrent["descendantProcessCeiling"]):
        problems.append(
            "concurrent dashboard descendants "
            f"{concurrent['peakDescendantProcesses']} exceed "
            f"{concurrent['descendantProcessCeiling']}")
    if concurrent["peakRssBytes"] > concurrent["rssCeilingBytes"]:
        problems.append(
            f"concurrent dashboard RSS {concurrent['peakRssBytes']} exceeds "
            f"{concurrent['rssCeilingBytes']}")
    for provider in ("claude", "codex"):
        population = report["providerPopulations"].get(provider, {})
        if int(population.get("supportUnits") or 0) <= 0:
            problems.append(f"{provider} current population is empty")
        if int(population.get("baselineSupportUnits") or 0) <= 0:
            problems.append(f"{provider} baseline population is empty")
        withheld = report.get("withheldProbe", {})
        if int(withheld.get("emptyAccountWithheldClasses", {}).get(
                provider) or 0) <= 0:
            problems.append(
                f"{provider} empty-account corpus exercises no withheld class")
        if int(withheld.get("privacyWithheldClasses", {}).get(
                provider) or 0) <= 0:
            problems.append(
                f"{provider} privacy denial withholds no class")
    conversation = report.get("conversationPopulations", {})
    for key in ("claudeSidechainMessages", "codexConversationEvents",
                "codexConversationMessages"):
        if int(conversation.get(key) or 0) <= 0:
            problems.append(f"{key} is empty")
    identity = report.get("identityPopulations", {})
    for key in ("claudeMetaMessages", "claudeToolResultMessages",
                "claudeUnattributedEntries", "codexSubagentThreads"):
        if int(identity.get(key) or 0) <= 0:
            problems.append(f"{key} is empty")
    if int(identity.get("codexRealAccounts") or 0) < 2:
        problems.append("Codex corpus has fewer than two real accounts")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", default="large")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument(
        "--repeat", type=int, default=3,
        help="How many independent timing rounds to run per case. The "
             "recorded figure is the DISTRIBUTION across rounds, not the "
             "best of them: run-to-run variation here is dominated by fixed "
             "process startup, and reporting one round hides that.",
    )
    parser.add_argument(
        "--window", default=None,
        help="Window token. Default: a date range derived from the corpus, "
             "because the synthetic corpus records no usage snapshots and so "
             "cannot resolve a subscription-week token.",
    )
    parser.add_argument("--window-days", type=int, default=3)
    parser.add_argument("--cold-rounds", type=int, default=3)
    parser.add_argument("--root", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--compare-bin", type=pathlib.Path, default=None,
        help="Reference cctally executable to time immediately before the "
             "candidate for every warmup and timed sample. Both binaries read "
             "the same corpus in one pinned-runner invocation.",
    )
    parser.add_argument(
        "--bounds", action="store_true",
        help="Assert the three scan bounds over adversarial corpora instead "
             "of timing the large corpus. Fast, and it needs no bench "
             "fixture: each corpus is built to exceed exactly one cap.",
    )
    args = parser.parse_args(argv)
    if args.compare_bin is not None and not args.compare_bin.is_file():
        parser.error(f"--compare-bin is not a file: {args.compare_bin}")

    if args.bounds:
        import tempfile

        root = pathlib.Path(args.root) if args.root else pathlib.Path(
            tempfile.mkdtemp(prefix="cctally-explain-bounds-"))
        bounds = _run_bounds(root)
        if args.json:
            print(json.dumps(bounds, indent=2))
        else:
            for key, value in bounds.items():
                if key != "problems":
                    print(f"{key}: {value}")
        for problem in bounds["problems"]:
            print(f"BOUND FAILED: {problem}", file=sys.stderr)
        return 1 if bounds["problems"] else 0

    root = pathlib.Path(args.root) if args.root else (
        pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "cctally-bench"
        / f"{args.scale}-seed{args.seed}"
    )
    data_dir = _build_corpus(args.scale, args.seed, root)
    window = args.window or _corpus_window(data_dir, args.window_days)

    env = dict(os.environ)
    env["CCTALLY_DATA_DIR"] = str(data_dir)
    env["CLAUDE_CONFIG_DIR"] = str(root / "claude")
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    env["HOME"] = str(root / "home")
    env["TZ"] = "Etc/UTC"
    env["NO_COLOR"] = "1"
    # Pin the read instant on both surfaces. The canonical parity projection
    # excludes it, but an exact shared instant also prevents current-window
    # liveness from changing between the CLI and dashboard reads.
    end_date = window.split("..", 1)[1]
    env["CCTALLY_AS_OF"] = f"{end_date}T23:59:59Z"

    report: dict[str, object] = {"scale": args.scale, "seed": args.seed,
                                 "runs": args.runs, "warmup": args.warmup,
                                 "repeat": args.repeat,
                                 "window": window, "cases": {}}
    implementations = [("candidate", BIN)]
    if args.compare_bin is not None:
        implementations.insert(0, ("reference", args.compare_bin))
        report["comparison"] = {
            "referenceBin": str(args.compare_bin),
            "candidateBin": str(BIN),
            "cases": {},
        }
    report["providerPopulations"] = _provider_populations(
        ["explain", "--source", "all", "--window", window, "--json"], env,
    )
    report["conversationPopulations"] = _conversation_populations(data_dir)
    report["identityPopulations"] = _identity_populations(data_dir)
    # `startup` is not a diagnosis case. It times `cctally --version`, which
    # does the same interpreter start and the same eager module loading every
    # subcommand pays, and reads no store at all. Without it a median of
    # roughly a second reads as a second of diagnosis work, when most of it is
    # a fixed cost the diagnosis neither causes nor can remove.
    for label, source in (("startup", None), ("claude", "claude"),
                          ("codex", "codex"), ("all", "all")):
        argv_case = (["--version"] if source is None else
                     ["explain", "--source", source, "--window", window,
                      "--json"])
        measurements = {
            name: {"roundMedians": [], "roundP95s": [], "samples": [],
                   "codes": set()}
            for name, _path in implementations
        }

        def _timed(name, bin_path):
            elapsed, code, stderr = _time_run(
                argv_case, env, bin_path=bin_path)
            # A benchmark that measures a failing invocation reports a
            # confident number for work that never happened, so a non-zero
            # exit ends the run rather than being folded into the sample.
            if code != 0:
                raise SystemExit(
                    f"--source {label} exited {code}, so these timings would "
                    f"measure an error path:\n{stderr}"
                )
            measurements[name]["codes"].add(code)
            return elapsed
        for _ in range(max(args.repeat, 1)):
            for _ in range(args.warmup):
                for name, bin_path in implementations:
                    _timed(name, bin_path)
            round_samples = {name: [] for name, _path in implementations}
            for _ in range(args.runs):
                for name, bin_path in implementations:
                    round_samples[name].append(_timed(name, bin_path))
            for name, _path in implementations:
                samples = round_samples[name]
                measurements[name]["roundMedians"].append(
                    statistics.median(samples))
                measurements[name]["roundP95s"].append(
                    _percentile(samples, 0.95))
                measurements[name]["samples"].extend(samples)

        def _summary(name):
            measured = measurements[name]
            round_medians = measured["roundMedians"]
            round_p95s = measured["roundP95s"]
            every_sample = measured["samples"]
            return {
                # The distribution ACROSS rounds is the headline. A single
                # round's median straddles the budget line on this case, and
                # quoting the fastest round would report a figure two of
                # three rounds did not reproduce.
                "roundMedianSeconds": [round(v, 4) for v in round_medians],
                "roundP95Seconds": [round(v, 4) for v in round_p95s],
                "medianOfRoundMediansSeconds": round(
                    statistics.median(round_medians), 4),
                "worstRoundMedianSeconds": round(max(round_medians), 4),
                "medianSeconds": round(statistics.median(every_sample), 4),
                "p95Seconds": round(_percentile(every_sample, 0.95), 4),
                "minSeconds": round(min(every_sample), 4),
                "maxSeconds": round(max(every_sample), 4),
                "exitCodes": sorted(measured["codes"]),
            }

        candidate = _summary("candidate")
        report["cases"][label] = candidate
        if args.compare_bin is not None:
            reference = _summary("reference")
            report["comparison"]["cases"][label] = {
                "reference": reference,
                "candidate": candidate,
                "medianDeltaSeconds": round(
                    candidate["medianSeconds"] - reference["medianSeconds"], 4),
            }

    report["phaseProfileSeconds"] = _profile_phases(
        data_dir, root, window, min(args.runs, 10)
    )
    report["withheldProbe"] = _withheld_probe(
        data_dir, root, window)

    all_argv = ["explain", "--source", "all", "--window", window, "--json"]
    cli_payload = _json_run(all_argv, env)
    report["coldDashboard"] = _cold_dashboard_rounds(
        env, window, args.cold_rounds, cli_payload)
    report["peakRssBytes"] = _peak_rss_run(all_argv, env)
    report["peakRssCeilingBytes"] = PEAK_RSS_CEILING_BYTES
    report["singleDashboardProcessTree"] = _concurrent_dashboard_round(
        env, window, cli_payload, request_count=1)
    report["concurrentDashboard"] = _concurrent_dashboard_round(
        env, window, cli_payload)
    report["concurrentDashboard"]["rssCeilingBytes"] = min(
        PEAK_RSS_CEILING_BYTES,
        report["singleDashboardProcessTree"]["peakRssBytes"]
        + CONCURRENT_RSS_OVER_SINGLE_ALLOWANCE_BYTES,
    )
    report["concurrentDashboard"]["peakRssDeltaBytes"] = (
        report["concurrentDashboard"]["peakRssBytes"]
        - report["singleDashboardProcessTree"]["peakRssBytes"]
    )
    report["budgetProblems"] = _performance_problems(report)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"corpus: {data_dir} (scale={args.scale} seed={args.seed})")
        print(f"window: {window}")
        print(f"runs: {args.runs} timed, {args.warmup} discarded as warmup")
        print(f"provider populations: {report['providerPopulations']}")
        for label, case in report["cases"].items():
            rounds = ", ".join(f"{v:.3f}" for v in case["roundMedianSeconds"])
            print(f"--source {label}: round medians [{rounds}]  "
                  f"worst round median {case['worstRoundMedianSeconds']:.3f}s  "
                  f"pooled median {case['medianSeconds']:.3f}s  "
                  f"pooled p95 {case['p95Seconds']:.3f}s  "
                  f"min {case['minSeconds']:.3f}s  "
                  f"max {case['maxSeconds']:.3f}s  "
                  f"exit {case['exitCodes']}")
        print(f"phase profile (in-process medians): "
              f"{report['phaseProfileSeconds']}")
        print(f"cold dashboard: {report['coldDashboard']}")
        print(f"peak RSS: {report['peakRssBytes']} bytes "
              f"(ceiling {PEAK_RSS_CEILING_BYTES})")
        print("single dashboard process tree: "
              f"{report['singleDashboardProcessTree']}")
        print(f"concurrent dashboard: {report['concurrentDashboard']}")
    for problem in report["budgetProblems"]:
        print(f"BUDGET FAILED: {problem}", file=sys.stderr)
    return 1 if report["budgetProblems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
