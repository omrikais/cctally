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
import json
import os
import pathlib
import statistics
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO / "bin" / "cctally"
BUILDER = REPO / "bin" / "build-bench-fixtures.py"


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
        }
    return out


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


def _profile_phases(data_dir: pathlib.Path, root: pathlib.Path,
                    window: str, runs: int) -> dict[str, float]:
    """Time the diagnosis's own phases in-process, without the CLI startup.

    The subprocess timings above are dominated by interpreter start and the
    eager module loading every subcommand pays. This measures what the
    diagnosis itself spends, split by the store it spends it on and by the
    per-class shaping, so "which class dominates" is answered by measurement
    rather than by reading the source.
    """
    import datetime as dt
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
    kernel = sys.modules["_lib_diagnosis"]

    start_date, end_date = window.split("..")
    scope = sources.DiagnosisScope(
        source="claude", account_key=None,
        window_start=dt.datetime.fromisoformat(start_date).replace(
            tzinfo=dt.timezone.utc),
        window_end=(dt.datetime.fromisoformat(end_date).replace(
            tzinfo=dt.timezone.utc) + dt.timedelta(days=1)),
        display_tz="UTC",
    )

    phases: dict[str, list[float]] = {"generation": [], "classes": [],
                                      "wire": []}
    for _ in range(runs):
        bundle = sources.StoreBundle(
            scope, kernel.resolve_policy_plan(
                scope.source, transcripts_visible=True))
        try:
            marker = time.perf_counter()
            sources._establish(scope, bundle)
            phases["generation"].append(time.perf_counter() - marker)
            marker = time.perf_counter()
            for spec in kernel.CONTRIBUTOR_REGISTRY:
                sources.load_class_facts(bundle, scope, spec)
            phases["classes"].append(time.perf_counter() - marker)
        finally:
            bundle.close()
        marker = time.perf_counter()
        report = sources.build_diagnosis(scope, measured_at=scope.window_end,
                                         transcripts_visible=True)
        module.diagnosis_to_wire(report)
        phases["wire"].append(time.perf_counter() - marker)

    return {name: round(statistics.median(values), 4)
            for name, values in phases.items()}


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
        rows = real_execute(conn, sql, params)
        # Counted PER SUBJECT, because that is the bound the design states:
        # every budget is allocated per session, per conversation or per source
        # file so no subject can starve another. A per-statement total would
        # scale with the number of subjects in the chunk and would either fail
        # on a wide window or pass on a narrow one for the wrong reason.
        for label, template, key in (
            ("seed_scan", sources._CLAUDE_SEED_PREFIX_SQL, "session_id"),
            ("normalize", sources._CLAUDE_TURN_CANDIDATE_SQL, "session_id"),
            ("codex_events", sources._CODEX_EVENTS_SQL, "source_path"),
        ):
            head = template.split("{placeholders}")[0]
            if sql.startswith(head):
                per_subject: dict = {}
                for row in rows:
                    per_subject[row[key]] = per_subject.get(row[key], 0) + 1
                observed[label] = max(
                    [observed.get(label, 0), *per_subject.values()])
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
    parser.add_argument("--window-days", type=int, default=7)
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
    # `startup` is not a diagnosis case. It times `cctally --version`, which
    # does the same interpreter start and the same eager module loading every
    # subcommand pays, and reads no store at all. Without it a median of
    # roughly a second reads as a second of diagnosis work, when most of it is
    # a fixed cost the diagnosis neither causes nor can remove.
    for label, source in (("startup", None), ("claude", "claude"),
                          ("all", "all")):
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
