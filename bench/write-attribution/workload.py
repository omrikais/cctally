"""#901 spec §6.3 workloads A–D: setup helpers and verdicts for a clone.

Setup (each writes ONLY the clone named by --root / --db):
  groups --db DB --provider {claude,codex} [--largest N | --median N | --smallest N | --dense N]
  compact --tree TREE --root ROOT          A/B: `cctally db vacuum --db conversations`,
                                           skipped when reclaim is already not eligible
  stamp --root ROOT --at {now,rewind-25h}  the retention stamp
  retention-days --root ROOT --days N      C: one day shorter than live
  batch --root ROOT --days N               C: the expiry batch the rewind makes due
  append --root ROOT --seconds S --kib-per-min K [--codex-only] [--scratch]   B/D appender (bounded)
  seed-scratch --root ROOT                 B/D: synthetic seed transcripts in ROOT/scratch, the extra
                                           root read beside the real (read-only) transcript roots
  synth-group --root ROOT --source-key K --new-key N --copies C
                                           R-cov (vi): a >60,000-row Codex group copied on the clone
  record-snapshot --root ROOT              C: the #780 record's op_seq and charged total
Verdicts (read a run directory; exit 0 PASS, 1 FAIL, 2 INVALID):
  ab-verdict RUN_DIR --tree TREE           A/B (and P2/P3), with the terminal drain as its own section;
                                           FAIL on a keeper-yield line in dash.log (revision 14, Q15)
  c-verdict RUN_DIR --tree TREE            C, with the terminal drain as its own section
  d-verdict RUN_DIR                        D (revision 11: ingested/deferred/error classes)
  drain-verdict RUN_DIR                    the terminal drain alone (revision 12, Q13)
  lifecycle-verdict RUN_DIR                W9's lifecycle from the WAL frame log (revision 13, Q14);
                                           FAIL on a keeper-yield line in dash.log (revision 14, Q15)
  b-pair CANDIDATE_RUN BASELINE_RUN        B full builds compared B against B (idle-A exception)
  c-pair CANDIDATE_RUN BASELINE_RUN        one candidate C against the 56e66f07a C (Q18/Q19)
  perf-pair CANDIDATE_RUN BASELINE_RUN     the performance table's other limits (Amendment 19
                                           HR-6): footprint peak against peak, doctor.gather,
                                           ingest phases, D's hook wall time
Every pair requires the baseline run's tree to be 56e66f07a and the candidate's
not to be (HR-9).
Reports:
  p3-report W9_OFF_RUN SHIPPED_RUN         P3's per-store mechanism isolation (revision 12),
                                           with WAL generations, completed checkpoints and the
                                           per-generation rusage fit (revision 13)
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import fractions
import glob
import json
import math
import os
import pathlib
import re
import sqlite3
import statistics
import subprocess
import sys
import time

UTC = dt.timezone.utc
KiB = 1024
MiB = 1024 * KiB
HERE = pathlib.Path(__file__).resolve().parent
_GROUP = {"claude": ("conversation_messages", "session_id"),
          "codex": ("codex_conversation_events", "conversation_key")}


def _ro(db) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def reclaim_eligible(page_size: int, page_count: int, freelist_count: int) -> bool:
    """Spec §5.4's reclaim start rule: free pages above 2 GiB AND 20% of the
    file. Hard-coded here (not imported) because the runners also drive the
    56e66f07a baseline tree, which has no such constant."""
    free = freelist_count * page_size
    return free > 2 * 1024 ** 3 and free > 0.2 * page_count * page_size


def conversations_reclaim_eligible(root) -> bool:
    db = pathlib.Path(root) / "data" / "conversations.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        page_size, page_count, freelist = (
            conn.execute(f"PRAGMA {p}").fetchone()[0]
            for p in ("page_size", "page_count", "freelist_count"))
    finally:
        conn.close()
    return reclaim_eligible(page_size, page_count, freelist)


#: R-cov (v): the stored bytes a row of each provider's charged table holds.
_STORED_BYTES = {
    "claude": "COALESCE(LENGTH(text), 0) + COALESCE(LENGTH(blocks_json), 0)",
    "codex": "COALESCE(LENGTH(payload_json), 0)",
}


def groups(db, provider, *, largest=0, median=0, smallest=0, dense=0) -> list:
    """R-cov's group populations: the `largest` (ii), the `median`-nearest
    (iii), the `smallest` (iv), and the `dense` groups of at most median size
    with the most stored bytes per row (v)."""
    table, key = _GROUP[provider]
    conn = _ro(db)
    try:
        rows = conn.execute(
            f"SELECT {key}, COUNT(*), SUM({_STORED_BYTES[provider]}) FROM "
            f"{table} WHERE {key} IS NOT NULL GROUP BY {key}").fetchall()
    finally:
        conn.close()
    rows.sort(key=lambda r: (-r[1], r[0]))
    if largest:
        return [{"key": k, "rows": n} for k, n, _b in rows[:largest]]
    middle = statistics.median(n for _, n, _b in rows)
    if smallest:
        return [{"key": k, "rows": n}
                for k, n, _b in sorted(rows, key=lambda r: (r[1], r[0]))[:smallest]]
    if dense:
        small = [r for r in rows if r[1] <= middle]
        small.sort(key=lambda r: (-(r[2] or 0) / r[1], r[0]))
        return [{"key": k, "rows": n, "bytesPerRow": round((b or 0) / n, 1)}
                for k, n, b in small[:dense]]
    rows.sort(key=lambda r: (abs(r[1] - middle), r[0]))
    return [{"key": k, "rows": n} for k, n, _b in rows[:median]]


def stamp(root, at) -> str:
    now = dt.datetime.now(UTC)
    value = now if at == "now" else now - dt.timedelta(hours=25)
    db = _inside(root, pathlib.Path(root) / "data" / "conversations.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("conversation_retention_last_prune_at", value.isoformat()))
        conn.commit()
    finally:
        conn.close()
    return value.isoformat()


def retention_days(root, days) -> None:
    path = _inside(root, pathlib.Path(root) / "data" / "config.json")
    config = json.loads(path.read_text()) if path.exists() else {}
    config.setdefault("conversation", {})["retention_days"] = int(days)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")


def batch(root, days) -> dict:
    cutoff = (dt.datetime.now(UTC) - dt.timedelta(days=int(days))).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    conn = _ro(pathlib.Path(root) / "data" / "conversations.db")
    report = {"cutoff": cutoff}
    try:
        for provider, (table, key) in _GROUP.items():
            rows = conn.execute(
                f"SELECT {key}, COUNT(*) FROM {table} WHERE {key} IS NOT NULL "
                f"GROUP BY {key} HAVING MAX(timestamp_utc) < ?",
                (cutoff,)).fetchall()
            report[provider] = {
                "groups": len(rows), "rows": sum(n for _, n in rows),
                "largest": max((n for _, n in rows), default=0)}
    finally:
        conn.close()
    return report


def synth_group(root, source_key, new_key, copies) -> dict:
    """R-cov (vi) (901-SR-015): a synthetic Codex conversation of more than
    60,000 rows built ON THE CLONE by copying the largest conversation's
    events and derived rows (messages, file touches, rollup) `copies` times
    under `new_key`, each copy's `source_path` suffixed so the unique keys
    hold. The FTS triggers fire as for any insert, so the deletion exercises
    the real derived-row and FTS maintenance. Writes only the clone."""
    db = _inside(root, pathlib.Path(root) / "data" / "conversations.db")
    conn = sqlite3.connect(db)

    def columns(table):
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]

    counts = {"events": 0, "messages": 0, "touches": 0, "rollups": 0}
    try:
        conn.execute("BEGIN IMMEDIATE")
        for n in range(int(copies)):
            suffix = f"#synthetic-{new_key}-{n}"
            cols = [c for c in columns("codex_conversation_events") if c != "id"]
            exprs = ["?" if c == "conversation_key" else
                     "source_path || ?" if c == "source_path" else c
                     for c in cols]
            params = [new_key if c == "conversation_key" else suffix
                      for c in cols if c in ("conversation_key", "source_path")]
            cur = conn.execute(
                f"INSERT INTO codex_conversation_events ({', '.join(cols)}) "
                f"SELECT {', '.join(exprs)} FROM codex_conversation_events "
                f"WHERE conversation_key = ? ORDER BY id",
                params + [source_key])
            counts["events"] += cur.rowcount
            mcols = [c for c in columns("codex_conversation_messages")
                     if c != "id"]
            remap = {}
            for row in conn.execute(
                    f"SELECT id, {', '.join(mcols)} FROM "
                    f"codex_conversation_messages WHERE conversation_key = ? "
                    f"ORDER BY id", (source_key,)).fetchall():
                values = dict(zip(mcols, row[1:]))
                values["conversation_key"] = new_key
                values["source_path"] = values["source_path"] + suffix
                cur = conn.execute(
                    f"INSERT INTO codex_conversation_messages "
                    f"({', '.join(mcols)}) VALUES "
                    f"({', '.join('?' for _ in mcols)})",
                    [values[c] for c in mcols])
                remap[row[0]] = cur.lastrowid
                counts["messages"] += 1
            tcols = columns("codex_conversation_file_touches")
            for row in conn.execute(
                    f"SELECT {', '.join(tcols)} FROM "
                    f"codex_conversation_file_touches WHERE conversation_key = ?",
                    (source_key,)).fetchall():
                values = dict(zip(tcols, row))
                if values.get("message_id") not in remap:
                    continue
                values.update(message_id=remap[values["message_id"]],
                              conversation_key=new_key,
                              source_path=values["source_path"] + suffix)
                values.pop("id", None)
                keys = list(values)
                conn.execute(
                    f"INSERT INTO codex_conversation_file_touches "
                    f"({', '.join(keys)}) VALUES "
                    f"({', '.join('?' for _ in keys)})",
                    [values[k] for k in keys])
                counts["touches"] += 1
        rcols = columns("codex_conversation_rollups")
        row = conn.execute(
            f"SELECT {', '.join(rcols)} FROM codex_conversation_rollups "
            f"WHERE conversation_key = ?", (source_key,)).fetchone()
        if row is not None:
            values = dict(zip(rcols, row), conversation_key=new_key)
            conn.execute(
                f"INSERT OR REPLACE INTO codex_conversation_rollups "
                f"({', '.join(rcols)}) VALUES ({', '.join('?' for _ in rcols)})",
                [values[c] for c in rcols])
            counts["rollups"] = 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return counts


def db_geometry(db) -> dict:
    """The database's geometry observed independently of the product
    (Amendment 19 HR-16): page size and the reserved bytes per page from
    the main file's 100-byte header, and the page count SQLite reports
    through a read-only connection (the WAL's latest commit included)."""
    with open(db, "rb") as fh:
        header = fh.read(100)
    raw = int.from_bytes(header[16:18], "big")
    page_size = 65536 if raw == 1 else raw
    reserved = header[20]
    conn = _ro(db)
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    finally:
        conn.close()
    return {"pageSize": page_size, "reservedBytes": reserved,
            "usableSize": page_size - reserved, "pageCount": int(page_count),
            "source": "observed: main-file header + read-only PRAGMA page_count"}


def record_snapshot(root) -> dict:
    """The clone's #780 record (op_seq and ledger), read-only: C binds its
    receipts to the operations committed between two snapshots. Amendment
    19: it also carries the record's continuation state (HR-15) and the
    database geometry observed independently of the product (HR-16)."""
    db = pathlib.Path(root) / "data" / "conversations.db"
    conn = _ro(db)
    try:
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key = "
            "'conversation_retention_reclaim_pending'").fetchone()
    finally:
        conn.close()
    record = json.loads(row[0]) if row and row[0] else {}
    return {"t": time.time(), "opSeq": record.get("op_seq"),
            "charged": sum(b.get("charged", 0)
                           for b in record.get("ledger") or []),
            "continuation": {k: record.get(k) for k in CONTINUATION_FIELDS},
            "geometry": db_geometry(db)}


def _claude_dir(root, scratch=False) -> pathlib.Path:
    root = pathlib.Path(root)
    return (root / "scratch" / "claude" / "projects") if scratch else (
        root / "home" / ".claude" / "projects")


def _codex_dir(root, scratch=False) -> pathlib.Path:
    root = pathlib.Path(root)
    return (root / "scratch" / "codex" / "sessions") if scratch else (
        root / "codex" / "sessions")


def seed_scratch(root) -> dict:
    """Operator decision esc-0db0e25cdfa4: the real transcript roots stay
    read-only, so B/D append into an extra scratch root that the run adds via
    CLAUDE_CONFIG_DIR / CODEX_HOME. Seed one valid Claude session and one Codex
    rollout with a conversation identity and a token total, so `append` has
    something to continue. Writes only inside ROOT/scratch."""
    now = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    claude = _claude_dir(root, True) / "-bench-write-attribution"
    codex = _codex_dir(root, True) / now[:4] / now[5:7] / now[8:10]
    claude.mkdir(parents=True, exist_ok=True)
    codex.mkdir(parents=True, exist_ok=True)
    session = "bench901-seed"
    row = {"type": "assistant", "uuid": f"{session}-0", "parentUuid": None,
           "sessionId": session, "timestamp": now,
           "cwd": "/bench/write-attribution", "gitBranch": "main",
           "requestId": f"req-{session}-0",
           "message": {"id": f"msg-{session}-0", "role": "assistant",
                       "model": "claude-sonnet-4-5-20250929", "content": "seed",
                       "usage": {"input_tokens": 1, "output_tokens": 1,
                                 "cache_read_input_tokens": 0,
                                 "cache_creation_input_tokens": 0}}}
    _inside(root, claude / f"{session}.jsonl").write_text(
        json.dumps(row, separators=(",", ":")) + "\n", encoding="utf-8")
    thread = "01a19010-0000-7000-8000-000000000901"
    lines = [
        {"timestamp": now, "type": "session_meta", "payload": {
            "id": thread, "session_id": thread, "timestamp": now,
            "cwd": "/bench/write-attribution", "originator": "codex_exec",
            "cli_version": "0.160.0", "source": "exec", "thread_source": "user",
            "model_provider": "openai"}},
        {"timestamp": now, "type": "turn_context", "payload": {
            "cwd": "/bench/write-attribution", "model": "gpt-5"}},
        {"timestamp": now, "type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 1, "cached_input_tokens": 0,
                                     "output_tokens": 1,
                                     "reasoning_output_tokens": 0,
                                     "total_tokens": 2},
                "total_token_usage": {"total_tokens": 2}}}},
    ]
    _inside(root, codex / f"rollout-bench901-{thread}.jsonl").write_text(
        "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines),
        encoding="utf-8")
    return {"claude": str(_claude_dir(root, True)), "codex": str(_codex_dir(root, True))}


def _claude_target(root, scratch=False) -> pathlib.Path:
    paths = sorted(_claude_dir(root, scratch)
                   .rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not paths:
        raise SystemExit("clone has no Claude transcript to append to")
    return _inside(root, paths[-1])


def _inside(root, path) -> pathlib.Path:
    """Refuse any write outside the clone: this tool never touches live roots."""
    resolved = pathlib.Path(path).resolve()
    if pathlib.Path(root).resolve() not in resolved.parents:
        raise SystemExit(f"refusing to write outside the clone: {resolved}")
    return resolved


def _last_total_tokens(path) -> "int | None":
    total = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"total_token_usage"' not in line:
                continue
            try:
                info = json.loads(line)["payload"]["info"]
                total = int(info["total_token_usage"]["total_tokens"])
            except (ValueError, KeyError, TypeError):
                continue
    return total


def _codex_target(root, scratch=False) -> "tuple[pathlib.Path, int]":
    """The clone's newest rollout that already has a conversation identity
    (a `session_meta` first line) and a token total to continue from."""
    paths = sorted(_codex_dir(root, scratch).rglob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
        if '"session_meta"' not in first:
            continue
        total = _last_total_tokens(path)
        if total is not None:
            return _inside(root, path), total
    raise SystemExit("clone has no Codex rollout with a conversation identity")


def append(root, seconds, kib_per_min, *, codex_only=False, scratch=False) -> dict:
    """Append valid records at the given total rate for `seconds`, bounded.
    `scratch` targets the seeded ROOT/scratch root instead of the clone's
    frozen roots (operator decision esc-0db0e25cdfa4)."""
    period = 6.0
    per_tick = max(1, int(kib_per_min * KiB * period / 60))
    claude = None if codex_only else _claude_target(root, scratch)
    codex, total = _codex_target(root, scratch)
    written = 0
    deadline = time.monotonic() + seconds
    n = 0
    while time.monotonic() < deadline:
        n += 1
        now = dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        share = per_tick if claude is None else per_tick // 2
        if claude is not None:
            marker = f"bench901-{os.getpid()}-{n}"
            row = {"type": "assistant", "uuid": marker, "parentUuid": None,
                   "sessionId": "bench901-append", "timestamp": now,
                   "cwd": "/bench/write-attribution", "gitBranch": "main",
                   "requestId": f"req-{marker}",
                   "message": {"id": f"msg-{marker}", "role": "assistant",
                               "model": "claude-sonnet-4-5-20250929",
                               "content": "x" * max(1, share - 400),
                               "usage": {"input_tokens": 100,
                                         "output_tokens": 50,
                                         "cache_read_input_tokens": 0,
                                         "cache_creation_input_tokens": 0}}}
            line = json.dumps(row, separators=(",", ":")) + "\n"
            with claude.open("a", encoding="utf-8") as fh:
                fh.write(line)
            written += len(line)
        total += 300
        event = {"timestamp": now, "type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 200,
                                     "cached_input_tokens": 25,
                                     "output_tokens": 100,
                                     "reasoning_output_tokens": 25,
                                     "total_tokens": 300},
                "total_token_usage": {"total_tokens": total},
                "pad": "y" * max(1, share - 400)}}}
        line = json.dumps(event, separators=(",", ":")) + "\n"
        with codex.open("a", encoding="utf-8") as fh:
            fh.write(line)
        written += len(line)
        if deadline - time.monotonic() <= period:
            break
        time.sleep(period)
    return {"bytes": written, "ticks": n}


# ── verdict helpers (pure) ────────────────────────────────────────────────

def ops_from_perf(paths) -> dict:
    """Maintenance operations from successive `dashboard-perf --json`
    snapshots, de-duplicated by (phase, op_id)."""
    ops = {}
    for path in paths:
        try:
            diag = json.load(open(path))["diagnostic"] or {}
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for row in (diag.get("tick") or {}).get("maintenance") or ():
            if row.get("op_id"):
                ops[(row["phase"], row["op_id"])] = row
    return ops


def _epoch(iso) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


#: §6.3 C condition 1 (via §5.5): the plan every reclaim chunk's record
#: carries; a chunk without it is not certifiable evidence.
RECLAIM_PLAN_FIELDS = ("planner_version", "plan_digest", "steps",
                       "identified_pages", "unidentified_pages", "fixed_bytes")
#: §6.3 C condition 1: "the continuation state at the end" - the #780
#: record's pacing state the end snapshot must carry.
CONTINUATION_FIELDS = ("balance_bytes", "as_of", "continuation_cutoff",
                       "eligible", "next_attempt_at", "progress")


def c_record_problems(ops, start, end) -> "list[str]":
    """Amendment 19 HR-15: C condition 1's completeness over the records of
    the operations inside the interval - each with its timestamps, its rows
    (a deletion) or pages and plan (a reclaim chunk), its charge, and its
    balance before and after (a missing balance is no evidence, never 0); a
    committed operation's balance after is its balance before less its
    charge, a gated one's is unchanged."""
    problems = []
    for op in ops:
        if not start <= _epoch(op["started_at"]) <= end:
            continue
        name = f"op {op.get('op_id')} ({op.get('phase')})"
        missing = [k for k in ("started_at", "duration_s", "charged_bytes",
                               "balance_before_bytes", "balance_after_bytes")
                   if op.get(k) is None]
        if op.get("outcome") == "ok":
            missing += [k for k in (("rows",) if op.get("phase") == "delete"
                                    else ("pages_reclaimed",)
                                    + RECLAIM_PLAN_FIELDS)
                        if op.get(k) is None]
        if missing:
            problems.append(f"{name}: its record lacks {sorted(set(missing))}")
            continue
        before, after = op["balance_before_bytes"], op["balance_after_bytes"]
        expect = (before - int(op["charged_bytes"])
                  if op.get("outcome") == "ok" else before)
        if after != expect:
            problems.append(f"{name}: balance after {after} is not its balance "
                            f"before {before} less its charge "
                            f"{op['charged_bytes']}")
    return problems


def c_continuation_problems(end_snap) -> "list[str]":
    """HR-15: the record snapshot at the interval's end must carry the
    continuation state (`record-snapshot` writes it under `continuation`)."""
    state = (end_snap or {}).get("continuation")
    if not isinstance(state, dict):
        return ["no continuation state at the end (record-end.json has no "
                "`continuation`)"]
    missing = [k for k in CONTINUATION_FIELDS if k not in state]
    return [f"the continuation state at the end lacks {missing}"] if missing else []


def c_conditions(ops, start, end, recompute) -> dict:
    """Spec §6.3 C conditions 1, 4 and 5 over op records (condition 2 is
    the deletion receipts').
    `recompute(op)` is the candidate's `recompute_charge`: the reservation
    the record's own inputs imply (None when they are missing)."""
    inside = [op for op in ops if start <= _epoch(op["started_at"]) <= end]
    before = [op for op in ops if _epoch(op["started_at"]) < start]
    deletions = [op for op in inside if op["phase"] == "delete"
                 and op["outcome"] == "ok"]
    reclaims = [op for op in inside if op["phase"] == "reclaim"
                and op["outcome"] == "ok" and op["pages_reclaimed"] > 0]
    problems = []
    if not deletions or len(reclaims) < 4:
        return {"valid": False, "problems": [
            f"{len(deletions)} deletion and {len(reclaims)} reclaim "
            "operations inside the interval (need >= 1 and >= 4)"]}
    incomplete = c_record_problems(ops, start, end)
    if incomplete:
        return {"valid": False, "problems": incomplete}
    charged = [op["charged_bytes"] for op in inside if op["outcome"] == "ok"]
    minutes = (end - start) / 60
    allowance = 4 * MiB * (minutes + 1) + max(charged)
    if sum(charged) > allowance:
        problems.append(f"charged {sum(charged)} > allowance {int(allowance)}")
    for op in inside:
        if op["outcome"] != "ok":
            continue
        expected = recompute(op)
        if expected is None or op["charged_bytes"] != expected:
            problems.append(f"op {op['op_id']} charged {op['charged_bytes']} "
                            f"!= its reservation")
        if op["balance_before_bytes"] < 0:
            problems.append(f"op {op['op_id']} started in debt")
    # Revision 6 (dc4 T6): a deletion's write bound is its receipt's I4
    # verdict (c_deletion_receipts), never a process counter per row.
    return {"valid": True, "problems": problems,
            "deletions": len(deletions), "reclaims": len(reclaims),
            "chargedBytes": sum(charged), "allowanceBytes": int(allowance),
            "beforeWindow": len(before)}


# ── the I2 statistic: always the candidate kernel's (Amendment 19 HR-8) ──────

_NS = 1_000_000_000


def counter_samples(run) -> list:
    """The dashboard's rusage samples as the candidate kernel's
    CounterSamples, in time order: a failed read is a sample with no bytes
    (the kernel's `invalid_samples` rule), never dropped; each carries the
    publication count of the latest dashboard-perf sample at or before it."""
    budget = _candidate("_lib_write_budget")
    pid = int(open(os.path.join(run, "pid")).read())
    rows = []
    path = os.path.join(run, "rusage.jsonl")
    if os.path.exists(path):
        rows = [json.loads(l) for l in open(path) if l.strip()]
    rows = sorted((r for r in rows if r.get("pid") == pid), key=lambda r: r["t"])
    pubs = sorted(_publications(run))
    times = [t for t, _n in pubs]
    out = []
    for row in rows:
        ok = row.get("rc") == 0 and row.get("dwrite") is not None
        at = bisect.bisect_right(times, row["t"]) - 1
        out.append(budget.CounterSample(
            int(round(float(row["t"]) * _NS)),
            int(row["dwrite"]) if ok else None,
            pubs[at][1] if at >= 0 else 0))
    return out


def kernel_deletions(samples, ops) -> list:
    """The deletion operations as the kernel's DeletionIntervals. The kernel
    takes the process counter at each operation's BEGIN and COMMIT; the
    harness samples that counter only every few seconds, so each interval
    is bounded conservatively from the samples around it: its start reading
    no lower than the counter can have been at BEGIN and its end reading no
    higher than it can have been at COMMIT, so the bytes excluded from a
    window never exceed what the operation can have written inside it. An
    operation without a counter delta excludes its time and no bytes."""
    budget = _candidate("_lib_write_budget")
    valid = [s for s in samples if s.bytes is not None]
    times = [s.t_ns for s in valid]

    def before(t_ns):
        i = bisect.bisect_right(times, t_ns) - 1
        return valid[i] if i >= 0 else None

    def after(t_ns):
        i = bisect.bisect_left(times, t_ns)
        return valid[i] if i < len(valid) else None

    out = []
    for op in ops:
        b0 = int(round(float(op["began"]) * _NS))
        b1 = int(round(float(op["ended"]) * _NS))
        pwb = op.get("process_write_bytes")
        start_bytes = end_bytes = None
        lo_b, hi_b, lo_e, hi_e = before(b0), after(b0), before(b1), after(b1)
        if pwb is not None and None not in (lo_b, hi_b, lo_e, hi_e):
            start_bytes = min(hi_b.bytes, hi_e.bytes - int(pwb))
            end_bytes = max(lo_e.bytes, lo_b.bytes + int(pwb))
        out.append(budget.DeletionInterval(b0, b1, start_bytes, end_bytes,
                                           int(op.get("rows") or 0)))
    return out


def kernel_steady(run, start: float, end: float, warm: float,
                  deletions=()) -> dict:
    """`steady_evaluate` over the run's own rusage samples."""
    return steady_evaluate(counter_samples(run), deletions, start=start,
                           end=end, warm=warm)


def steady_evaluate(samples, deletions=(), *, start: float, end: float,
                    warm: float) -> dict:
    """The I2 verdict input over every trailing five-minute window ending at
    a successful sample in [start + 300, end + 15], each computed by the
    candidate kernel's `steady_statistic` with its own rules: a failed sample
    or a gap over 60 s (insufficient coverage), fewer than six samples, a
    counter reset (unavailable) and excluded deletion time over 20%
    (insufficient). Any evaluation the kernel cannot qualify for an evidence
    reason makes the window INVALID; one it declines for excluded time is
    reported, not judged; no qualified evaluation at all is INVALID. Pure
    over kernel CounterSamples and DeletionIntervals (the soak's write
    budget leg shares it)."""
    budget = _candidate("_lib_write_budget")
    lo, hi = int((start + 300) * _NS), int((end + 15) * _NS)
    evaluations, invalid = [], []
    for sample in samples:
        if sample.bytes is None or not lo <= sample.t_ns <= hi:
            continue
        stat = budget.steady_statistic(
            samples, deletions, now_ns=sample.t_ns,
            warm_admitted_ns=int(round(warm * _NS)))
        row = {"t": sample.t_ns / _NS, "status": stat.status,
               "reasons": list(stat.reasons),
               "bytesPerMinute": stat.bytes_per_minute,
               "publications": stat.publications,
               "bytesPerPublication": (stat.mean_bytes_per_publication
                                       if stat.publication_qualified else None),
               "excludedBytes": stat.excluded_bytes,
               "excludedSeconds": stat.excluded_seconds,
               "samples": stat.sample_count}
        evaluations.append(row)
        if stat.status != "qualified" and \
                list(stat.reasons) != ["excluded_time_over_limit"]:
            invalid.append(f"the kernel's statistic at {row['t'] - start:.0f} s "
                           f"is {stat.status} ({', '.join(stat.reasons)})")
    qualified = [e for e in evaluations if e["status"] == "qualified"]
    if not qualified and not invalid:
        invalid.append("no qualified five-minute window (the kernel's "
                       "statistic qualified no evaluation)")
    per_pub = [e["bytesPerPublication"] for e in qualified
               if e["bytesPerPublication"] is not None]
    return {"valid": not invalid, "problems": invalid[:20],
            "invalidEvaluations": len(invalid),
            "evaluations": len(evaluations), "qualified": len(qualified),
            "excludedTimeOverLimit": sum(
                1 for e in evaluations
                if e["reasons"] == ["excluded_time_over_limit"]),
            "worstBytesPerMinute": max((e["bytesPerMinute"] for e in qualified),
                                       default=None),
            "worstBytesPerPublication": max(per_pub, default=None),
            "kernel": "bin/_lib_write_budget.steady_statistic"}


def steady_limit_problems(steady: dict, limits) -> "list[str]":
    problems = []
    worst = steady["worstBytesPerMinute"]
    if worst is not None and worst > limits.bytes_per_minute:
        problems.append(f"{worst:.0f} B/min > {limits.bytes_per_minute}")
    per_pub = steady["worstBytesPerPublication"]
    if per_pub is not None and per_pub > limits.bytes_per_publication:
        problems.append(f"{per_pub:.0f} B/publication > "
                        f"{limits.bytes_per_publication}")
    return problems


def _rusage(run) -> list:
    pid = int(open(os.path.join(run, "pid")).read())
    samples = []
    for line in open(os.path.join(run, "rusage.jsonl")):
        row = json.loads(line)
        if row.get("pid") == pid and row.get("rc") == 0 and row.get("dwrite") is not None:
            samples.append((row["t"], row["dwrite"]))
    return sorted(samples)


def _publications(run) -> list:
    out = []
    for path in sorted(glob.glob(os.path.join(run, "perf-*.json"))):
        try:
            tick = json.load(open(path))["diagnostic"]["tick"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        out.append((os.path.getmtime(path), int(tick.get("tick_seq") or 0)))
    return out


#: The analyzer split (spec §6.3, 901-PA-002): every verdict imports the
#: CANDIDATE's kernel - this tree's `bin/` - for both populations. The tree
#: under test (`--tree`) is analysis data only, never an import path, so a
#: baseline run is judged by the same budget kernel and reservation formula.
CANDIDATE_BIN = HERE.parent.parent / "bin"


def _candidate(name):
    if str(CANDIDATE_BIN) not in sys.path:
        sys.path.insert(0, str(CANDIDATE_BIN))
    import importlib
    return importlib.import_module(name)


def _load_limits(tree=None):
    """The candidate's limits; `tree` is accepted for the record only."""
    return _candidate("_lib_write_budget").LIMITS


def _temp_bytes(snap) -> int:
    return sum(v[0] for k, v in (snap.get("paths") or {}).items()
               if os.path.basename(k).startswith("etilqs_"))


def family_temp_bytes(run, began: float, ended: float,
                      prefix: str = "wtrace") -> int:
    """Amendment 19 HR-11: `etilqs_*` bytes written in [began, ended] by
    EVERY process the interposer traced under `prefix` (the dashboard and
    every child or detached worker it started), each from its periodic and
    exit snapshots. Conservative: a process's base is its last snapshot at
    or before `began` (zero when it started later), its top its first
    snapshot at or after `ended` (else its last, its exit included)."""
    snaps = _snapshots(run, prefix)
    exits = _exit_snapshots(run, prefix)
    total = 0
    for pid in set(snaps) | set(exits):
        rows = sorted(snaps.get(pid, []) + exits.get(pid, []),
                      key=lambda r: r["t"])
        if not rows:
            continue
        lo = [r for r in rows if r["t"] <= began]
        hi = [r for r in rows if r["t"] >= ended] or rows[-1:]
        if hi[0]["t"] < began:
            continue                      # the process ended before the window
        total += max(0, _temp_bytes(hi[0]) - (_temp_bytes(lo[-1]) if lo else 0))
    return total


def _etilqs_in_window(run, start, end) -> int:
    return family_temp_bytes(run, start, end)


def _window(run) -> "tuple[float, float]":
    window = json.load(open(os.path.join(run, "window.json")))
    return float(window["start"]), float(window["end"])


def _frozen_roots():
    sys.path.insert(0, str(HERE))
    import frozen_roots
    return frozen_roots


def input_evidence(run) -> dict:
    """The label every verdict carries (revision 15): {mode, freeze seal}."""
    return _frozen_roots().evidence_label(run)


def input_problems(run) -> "list[str]":
    """Spec §6.3 revision 15 (Q16): a verdict needs the input mode and, for a
    frozen run, the freeze's seal before and after and complete enforced
    activation receipts for the whole family (frozen_roots.check_inputs); for
    a live run with a drained admission, the finite-frontier receipt. Frozen
    inputs cannot grow, so frozen growth or a discontinuity in the window is a
    namespace leak: INVALID, never a labelled ambient run."""
    problems = _frozen_roots().check_inputs(run)
    if input_evidence(run)["mode"] == "frozen" and os.path.exists(
            os.path.join(run, "window.json")):
        start, end = _window(run)
        leak = frozen_changes(run, start, end)
        if not leak.get("measured"):
            # Amendment 19 HR-14: no samples is no evidence, never "no leak".
            problems.append("the frozen leak tripwire has no samples in the "
                            "window (jsonl.jsonl missing or fewer than two "
                            "samples), so the frozen inputs are unverified")
        elif leak.get("leaked"):
            problems.append("the frozen inputs grew or changed in the window "
                            f"({leak.get('bytes')}, "
                            f"{leak.get('discontinuities', 0)} "
                            "discontinuities"
                            + (f"; {sorted(leak['paths'])[:5]}"
                               if leak.get("paths") else "")
                            + "): the namespace leaked")
    return problems


def _freeze_roots(run) -> "list[str] | None":
    """The logical roots of the run's freeze (`manifest.json` `roots`, the
    single-file link-target roots included), or None when they cannot be
    read."""
    try:
        with open(os.path.join(run, "inputs.json")) as fh:
            freeze = (json.load(fh).get("freeze") or {}).get("freeze")
        with open(os.path.join(freeze, "manifest.json")) as fh:
            manifest = json.load(fh)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    roots = [r["logical"] for r in manifest.get("roots") or []
             if isinstance(r, dict) and r.get("logical")]
    return roots or None


def frozen_changes(run, start: float, end: float) -> dict:
    """Amendment 15 Q1: changes to FROZEN inputs in a frozen run's window.
    The sampler walks every root the tree reads, which for B and D includes
    the controlled scratch root (ROOT/scratch); only a change at or under a
    logical root of the freeze is frozen input, and any one of them (grown,
    new, shrunk, replaced or deleted; the last in-window sample's per-file
    `changes` are cumulative from the baseline) means the namespace leaked.
    Fail closed: without the freeze's roots, or for aggregate-only samples,
    every change and all growth count."""
    ambient = ambient_growth(run, start, end)
    if not ambient.get("measured"):
        return {"leaked": False, "measured": False}
    rows = []
    with open(os.path.join(run, "jsonl.jsonl")) as fh:
        rows = [json.loads(l) for l in fh if l.strip()]
    rows = [r for r in rows if start <= r["t"] <= end + 15]
    roots = _freeze_roots(run)
    last = rows[-1]
    if roots is None or "changes" not in last:
        return {"leaked": ambient.get("label") == "ambient", "measured": True,
                "bytes": ambient.get("bytes"),
                "discontinuities": ambient.get("discontinuities", 0),
                "scoped": False}

    def frozen(path):
        return any(path == r or path.startswith(r.rstrip("/") + "/")
                   for r in roots)
    leaked = {p: c for p, c in (last.get("changes") or {}).items() if frozen(p)}
    controlled = {p: c for p, c in (last.get("changes") or {}).items()
                  if not frozen(p)}
    grown, broken = {"claude": 0, "codex": 0}, 0
    for change in leaked.values():
        if change.get("kind") in ("grown", "new"):
            grown[change.get("provider", "claude")] += max(
                0, int(change.get("size") or 0) - int(change.get("base") or 0))
        else:
            broken += 1
    return {"leaked": bool(leaked), "measured": True, "bytes": grown,
            "discontinuities": broken, "paths": list(leaked),
            "controlled": controlled, "scoped": True}


#: A run its runner did not finish is never evidence (Amendment 19 HR-1/HR-2:
#: `_inputs.sh` wa_guard records these).
RUNNER_MARKERS = {"interrupted.jsonl": "the runner was interrupted",
                  "deadline.json": "the runner hit its hard deadline",
                  "orphaned.json": "the runner died and its watchdog cleaned up"}


def runner_problems(run) -> "list[str]":
    return [f"{what} ({name})" for name, what in RUNNER_MARKERS.items()
            if os.path.exists(os.path.join(run, name))]


def admission_problems(run) -> "list[str]":
    """Drained admission (§6.3, Q11): the runner's admission.json must say
    valid, after a catch-up receipt that admitted the clone; and (revision
    15) the run's input-mode evidence must hold (`input_problems`).
    Amendment 19: a run its runner did not finish is INVALID; a frozen
    catch-up must carry its verification comparison (HR-12); the
    post-release `run-live.sh` run (workload L-post) has no catch-up and is
    admitted on its warm admission and its live frontier (HR-17)."""
    unfinished = runner_problems(run)
    path = os.path.join(run, "admission.json")
    if not os.path.exists(path):
        return unfinished + ["no admission record (admission.json)"]
    admission = json.load(open(path))
    if not admission.get("valid"):
        return unfinished + [f"admission refused: {admission.get('reason')}"]
    if workload_kind(run)[0] == "L-post":
        frontier = _read_json(os.path.join(run, "frontier.json")) or {}
        if frontier.get("mode") != "live" or not isinstance(
                frontier.get("files"), dict):
            unfinished.append("no live finite-frontier receipt (frontier.json)")
        return unfinished + input_problems(run)
    catchup = os.path.join(run, "catchup.json")
    receipt = _read_json(catchup) or {}
    if not receipt.get("admitted"):
        return unfinished + [
            "no catch-up receipt that admitted the clone (catchup.json)"]
    frontier = receipt.get("frontier") or {}
    if frontier.get("mode") == "frozen" and not (
            frontier.get("verification") or {}).get("checked"):
        unfinished.append("the frozen catch-up never compared its verification "
                          "pass with its catch-up (catchup.json frontier has no "
                          "verification; Amendment 19 HR-12)")
    return unfinished + input_problems(run)


def terminal_problems(run, end: float) -> "list[str]":
    """The terminal rusage, interposer and dashboard-perf samples must have
    been taken at or after the window's end while the process was alive."""
    problems = []
    terminal = os.path.join(run, "terminal.json")
    if not os.path.exists(terminal) or not json.load(open(terminal)).get("alive"):
        problems.append("the dashboard was not alive for its terminal samples")
    if not any(t >= end for t, _b in _rusage(run)):
        problems.append("no successful rusage sample at or after the end")
    pid = int(open(os.path.join(run, "pid")).read())
    trace = os.path.join(run, f"wtrace.{pid}")
    snaps = ([json.loads(l) for l in open(trace) if l.strip()]
             if os.path.exists(trace) else [])
    if not any(s["t"] >= end for s in snaps):
        problems.append("no interposer snapshot at or after the end")
    perf = [p for p in glob.glob(os.path.join(run, "perf-*.json"))
            if os.path.getmtime(p) >= end]
    if not perf:
        problems.append("no dashboard-perf sample at or after the end")
    return problems


def ambient_growth(run, start: float, end: float) -> dict:
    """Growth of the transcript roots inside the window, recorded separately
    from controlled appends (§6.3); an A window with growth is a labelled
    ambient run, not an append-free idle one. Frozen roots cannot grow, so a
    frozen run's growth must be zero (revision 15: the verdicts refuse it)."""
    rows = []
    path = os.path.join(run, "jsonl.jsonl")
    if os.path.exists(path):
        rows = [json.loads(l) for l in open(path) if l.strip()]
    rows = [r for r in rows if start <= r["t"] <= end + 15]
    if len(rows) < 2:
        return {"measured": False}
    growth = {k: rows[-1][k] - rows[0][k] for k in ("claude", "codex")}
    first, last = rows[0], rows[-1]
    if "growth" in first and "growth" in last:
        # Revision 15 (Q16): per-file evidence (jsonl_bytes.py --baseline).
        # Aggregate totals can hide a deletion or a replacement behind growth
        # elsewhere; a discontinuity in the window is ambient activity too.
        growth = {k: last["growth"][k] - first["growth"][k]
                  for k in ("claude", "codex")}
        broken = max(0, int(last.get("discontinuities") or 0)
                     - int(first.get("discontinuities") or 0))
        return {"measured": True, "perFile": True, "bytes": growth,
                "discontinuities": broken,
                "label": "ambient" if any(growth.values()) or broken
                else "append-free"}
    return {"measured": True, "bytes": growth,
            "label": "ambient" if any(growth.values()) else "append-free"}


# ── the terminal drain (spec §6.3 revision 12, Q13; 901-SR-026/-029/-030) ───

#: (i) (Q19): every frame appended in the window is followed by an observed
#: main-file checkpoint write within this many seconds plus the sampling
#: period; a frame appended this close to the window's end with no later
#: write is left to (ii).
DRAIN_GAP_S = 120.0
#: (ii): the committed-but-unbackfilled frames a stopped dashboard may leave,
#: before one minute of the window's mean frame rate is added (W9's trigger).
DRAIN_BACKLOG_BYTES = 16 * MiB
WAL_FRAME_HEADER_BYTES = 24
_DRAIN_STORES = ("conversations.db", "cache.db")
#: wtrace.c's WAL-path table (NWAL): a WAL past it has no frame records.
WTRACE_WAL_PATHS = 32


def interposer_drop_problems(run, prefix: str, snaps: dict) -> "list[str]":
    """Records the interposer could not keep (Amendment 19 HR-13): dropped
    bytes or frame records, and WAL paths past its 32-path table (wtrace.c
    NWAL, per process), whose frames it silently never logs. A WAL path
    takes a slot at its first write, and its first write is a logged header
    (a 32-byte WAL header or a 24-byte frame header), so with no dropped
    frame record a process's frame log names every slot its table assigned:
    32 distinct WAL paths in one process's log means its table was full and
    a later WAL may be missing. The check reads only the trace, so it judges
    retained runs and new ones alike, with the instrumentation unchanged."""
    rows = [r for rows in snaps.values() for r in rows]
    problems = []
    if any(r.get("dropped") or r.get("framesDropped") for r in rows):
        problems.append("the interposer dropped bytes or frame records")
    for path in sorted(glob.glob(os.path.join(run, f"{prefix}.*.frames"))):
        with open(path) as fh:
            wals = {json.loads(l).get("path") for l in fh if l.strip()}
        if len(wals) >= WTRACE_WAL_PATHS:
            problems.append(f"{os.path.basename(path)}: {len(wals)} WAL paths "
                            "fill the interposer's 32-path table, so frames "
                            "of a further WAL would never have been logged")
    return problems


def _snapshots(run, prefix: str) -> "dict[int, list]":
    """Every periodic interposer snapshot file under `prefix`, by pid."""
    out = {}
    base = os.path.join(run, prefix) + "."
    for path in glob.glob(base + "*"):
        tail = path[len(base):]
        if tail.isdigit():
            out[int(tail)] = [json.loads(l) for l in open(path) if l.strip()]
    return out


def _frame_records(run, prefix: str) -> "list | None":
    paths = glob.glob(os.path.join(run, f"{prefix}.*.frames"))
    if not paths:
        return None
    rows = []
    for path in paths:
        rows += [json.loads(l) for l in open(path) if l.strip()]
    return rows


def _main_write_times(snaps: dict, name: str) -> "list[float]":
    """Snapshot times at which a traced process's cumulative bytes to the main
    database file `name` had grown since its previous snapshot."""
    times = []
    for rows in snaps.values():
        previous = 0
        for row in sorted(rows, key=lambda r: r["t"]):
            total = sum(v[0] for k, v in row.get("paths", {}).items()
                        if os.path.basename(k) == name)
            if total > previous:
                times.append(float(row["t"]))
            previous = max(previous, total)
    return sorted(times)


def _exit_snapshots(run, prefix: str) -> "dict[int, list]":
    """Every exit snapshot `prefix.<pid>.exit`, by pid: the same cumulative
    counters as the periodic snapshots, taken as the process ended."""
    out = {}
    base = os.path.join(run, prefix) + "."
    for path in glob.glob(base + "*.exit"):
        tail = path[len(base):-len(".exit")]
        if tail.isdigit():
            out[int(tail)] = [json.loads(l) for l in open(path) if l.strip()]
    return out


def _snapshot_period(snaps: dict) -> float:
    gaps = []
    for rows in snaps.values():
        ts = sorted(r["t"] for r in rows)
        gaps += [b - a for a, b in zip(ts, ts[1:]) if b > a]
    return statistics.median(gaps) if gaps else 5.0


def drain_check(run) -> dict:
    """The terminal drain's verdict, judged from evidence recorded BEFORE the
    copier ran (the window's interposer record and the wal-index reading),
    never from the copier's own output. Status PASS, FAIL or INVALID."""
    start, end = _window(run)
    invalid, failed, stores = [], [], {}
    terminal_path = os.path.join(run, "terminal.json")
    terminal = (json.load(open(terminal_path))
                if os.path.exists(terminal_path) else {})
    drain = terminal.get("drain")
    if not drain:
        return {"status": "INVALID", "problems": [
            "no terminal drain record (terminal.json has no copier)"]}
    pid = drain.get("pid")
    usage = drain.get("rusage") or {}
    if usage.get("rc") != 0 or usage.get("dwrite") is None:
        invalid.append("no kernel write sample of the copier")
    copier = _snapshots(run, "drain").get(pid)
    copier_exit = os.path.join(run, f"drain.{pid}.exit")
    if not copier and not os.path.exists(copier_exit):
        invalid.append("no interposer record of the copier")
    snaps = _snapshots(run, "wtrace")
    frames = _frame_records(run, "wtrace")
    if not snaps:
        invalid.append("no interposer timeline of the window")
    if frames is None:
        invalid.append("no WAL frame log of the window")
    invalid.extend(interposer_drop_problems(run, "wtrace", snaps))
    if invalid:
        return {"status": "INVALID", "problems": invalid}
    period = _snapshot_period(snaps)
    exits = _exit_snapshots(run, "wtrace")
    observed = {pid: snaps.get(pid, []) + exits.get(pid, [])
                for pid in set(snaps) | set(exits)}
    window_bytes = _window_bytes(observed, start, end)
    minutes = max((end - start) / 60.0, 1e-9)
    readings = drain.get("stores") or {}
    for name in _DRAIN_STORES:
        # HR-13: a store with no reading is missing evidence, never skipped.
        if name not in readings or readings[name].get("missing"):
            invalid.append(f"{name}: no drain reading (the store is missing "
                           "from the copier's record)")
            continue
        wal_index = readings[name].get("walIndex") or {}
        checkpoint = readings[name].get("checkpoint") or {}
        if not wal_index.get("valid"):
            invalid.append(f"{name}: invalid wal-index reading "
                           f"({wal_index.get('reason')})")
            continue
        if not checkpoint.get("ok"):
            invalid.append(f"{name}: the copier's TRUNCATE did not succeed "
                           f"with busy = 0 and an empty -wal ({checkpoint})")
            continue
        appended = sorted(float(f["t"]) for f in frames
                          if os.path.basename(f["path"]) == f"{name}-wal"
                          and f["frame"] >= 1 and start <= f["t"] <= end)
        headers = sum(1 for f in frames
                      if os.path.basename(f["path"]) == f"{name}-wal"
                      and f["frame"] == 0 and start <= f["t"] <= end)
        wal_bytes = window_bytes.get(f"{name}-wal", 0)
        if wal_bytes > 32 * headers and not appended:
            # HR-13: frames written to -wal in the window with no frame
            # record would pass rule (i) vacuously.
            invalid.append(f"{name}: {wal_bytes} -wal bytes written in the "
                           "window but no frame records")
            continue
        # Rule (i) (Q19): append-relative checkpoint progress. Window
        # boundaries are not write events; the copier's writes are not in
        # `observed`; each process's counters run from its first snapshot,
        # pre-window ones included, through its exit snapshot.
        writes = sorted(t for t in _main_write_times(observed, name)
                        if start <= t <= end)
        overdue, censored, worst = [], 0, 0.0
        for at in appended:
            following = bisect.bisect_right(writes, at)
            if following < len(writes):
                delay = writes[following] - at
                worst = max(worst, delay)
                if delay > DRAIN_GAP_S + period:
                    overdue.append((delay, at, True))
            elif at >= end - DRAIN_GAP_S:
                censored += 1
            else:
                overdue.append((end - at, at, False))
        if overdue:
            delay, at, written = max(overdue, key=lambda o: o[0])
            failed.append(
                f"{name}: a frame appended at {at - start:.0f} s had no "
                f"main-file write for {delay:.0f} s (> {DRAIN_GAP_S:.0f} s)"
                + ("" if written else " before the window's end")
                + f"; {len(overdue)} frame(s) overdue")
        page = wal_index.get("pageSize") or 4096
        frame_bytes = int(page) + WAL_FRAME_HEADER_BYTES
        backlog_frames = int(wal_index["mxFrame"]) - int(wal_index["nBackfill"])
        backlog = backlog_frames * frame_bytes
        minute = len(appended) / minutes * frame_bytes
        limit = DRAIN_BACKLOG_BYTES + minute
        if backlog > limit:
            failed.append(
                f"{name}: {backlog} B of committed, unbackfilled frames at the "
                f"stop (mxFrame {wal_index['mxFrame']} - nBackfill "
                f"{wal_index['nBackfill']}) > {limit:.0f} B")
        stores[name] = {
            "appendedFrames": len(appended), "mainWrites": len(writes),
            "worstDeferralS": worst, "overdueFrames": len(overdue),
            "censoredFrames": censored,
            "backlogBytes": backlog, "backlogLimitBytes": limit,
            "mxFrame": wal_index["mxFrame"], "nBackfill": wal_index["nBackfill"],
            "copierRow": checkpoint.get("row")}
    if invalid:
        return {"status": "INVALID", "problems": invalid, "stores": stores}
    return {"status": "FAIL" if failed else "PASS", "problems": failed,
            "stores": stores, "snapshotPeriodS": period,
            "copierWriteBytes": usage.get("dwrite")}


def _with_drain(run, result: dict, code: int) -> int:
    """The drain as its own section of a window verdict: never added to the
    window's I2 numbers; a failure fails the run, an invalid drain makes it
    invalid."""
    drain = drain_check(run)
    result["drain"] = drain
    if drain["status"] == "INVALID":
        result["valid"] = False
        return _emit(result, 2)
    if drain["status"] == "FAIL":
        code = max(code, 1)
    return _emit(result, code)


#: The marker of the one stderr line `bin/_lib_wal_checkpoint.py` writes at
#: every keeper yield (its `YIELD_DIAGNOSTIC`; spec §6.3, P after revision
#: 14). A dashboard in ordinary operation never yields, so its log carries
#: none.
KEEPER_YIELD_MARKER = "[w9] keeper-yield"


def keeper_yield_lines(run) -> "list[str] | None":
    """The keeper-yield lines of the run's dashboard log (`dash.log`), or None
    when the run has no dashboard log to check."""
    path = os.path.join(run, "dash.log")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as fh:
        return [line.rstrip("\n") for line in fh if KEEPER_YIELD_MARKER in line]


def keeper_yield_problem(lines: "list[str]") -> "str | None":
    """A verdict problem for keeper-yield lines, or None for none."""
    if not lines:
        return None
    return (f"{len(lines)} keeper-yield line(s) in dash.log: the yield path "
            f"fired in ordinary operation (first: {lines[0].strip()})")


def run_analyzer(run, start: float, end: float) -> "str | None":
    """analyze.py over the window, its offsets measured from the window's
    own trace start (`--origin`, Amendment 19 HR-21: analyze.py's default
    origin is its first snapshot minus 5 s, not `traceStart`). Returns the
    analyzer's INVALID reason, or None; its report is printed."""
    t0 = float(json.load(open(os.path.join(run, "window.json")))["traceStart"])
    analyzed = subprocess.run(
        [sys.executable, str(HERE / "analyze.py"), run, "--origin", str(t0),
         "--skip", str(start - t0), "--until", str(end - t0)],
        capture_output=True, text=True)
    print(analyzed.stdout, end="")
    if analyzed.returncode != 0:
        return ("analyze.py: " + (analyzed.stdout.strip().splitlines() or
                                  [analyzed.stderr.strip()[-200:]])[-1])
    return None


def ab_verdict(run, tree) -> int:
    start, end = _window(run)
    invalid = admission_problems(run) + terminal_problems(run, end)
    yields = keeper_yield_lines(run)
    if yields is None:
        invalid.append("no dashboard log (dash.log) to check for keeper-yield "
                       "lines")
    if invalid:
        return _emit({"valid": False, "problems": invalid}, 2)
    analyzer = run_analyzer(run, start, end)
    if analyzer:
        return _emit({"valid": False, "problems": [analyzer]}, 2)
    limits = _load_limits(tree)
    warm = float(json.load(open(os.path.join(run, "window.json"))).get(
        "warm", start))
    steady = kernel_steady(run, start, end, warm)
    if not steady["valid"]:
        return _emit({"valid": False, "problems": steady["problems"],
                      "steady": steady}, 2)
    temp = _etilqs_in_window(run, start, end)
    problems = steady_limit_problems(steady, limits)
    if temp:
        problems.append(f"{temp} etilqs bytes in the window (whole family)")
    yield_problem = keeper_yield_problem(yields)
    if yield_problem:
        problems.append(yield_problem)
    return _with_drain(run, {
        "valid": True, "problems": problems,
        "worstBytesPerMinute": steady["worstBytesPerMinute"],
        "worstBytesPerPublication": steady["worstBytesPerPublication"],
        "steady": steady, "etilqsBytes": temp,
        "ambient": ambient_growth(run, start, end),
        "keeperYieldLines": len(yields), "inputs": input_evidence(run)},
        1 if problems else 0)


def captured_ops(run) -> "list | None":
    """Every operation the dashboard reported (`run_with_capture.py`), with
    precise wall-clock bounds; None without a capture."""
    path = os.path.join(run, "ops.jsonl")
    if not os.path.exists(path):
        return None
    ops = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            op = json.loads(line)
            if not op.get("op_id"):
                continue
            op["ended"] = float(op["reportedAt"])
            op["began"] = op["ended"] - float(op.get("duration_s") or 0.0)
            op["duration_ns"] = int(float(op.get("duration_s") or 0) * 1e9)
            ops.append(op)
    return ops


def c_completeness(ops, start_snap, end_snap) -> "list[str]":
    """The captured operations committed between the two record snapshots
    must account for the op_seq advance and the ledger's charged delta."""
    if not start_snap or not end_snap or start_snap.get("opSeq") is None:
        return ["no record snapshots bracket the window"]
    committed = [op for op in ops if op.get("outcome") == "ok"
                 and start_snap["t"] <= op["ended"] <= end_snap["t"]]
    problems = []
    advance = int(end_snap["opSeq"] or 0) - int(start_snap["opSeq"] or 0)
    if advance != len(committed):
        problems.append(f"op_seq advanced {advance}, {len(committed)} "
                        "operations captured")
    charged = sum(int(op.get("charged_bytes") or 0) for op in committed)
    if int(end_snap["charged"]) - int(start_snap["charged"]) != charged:
        problems.append("the ledger's charged delta differs from the "
                        "captured charges")
    return problems


def _etilqs_between(run, began, ended) -> "int | None":
    """Temp bytes the whole traced family wrote over the snapshots bracketing
    one operation (a conservative upper bound at the trace's period; HR-11);
    None without the dashboard's own trace."""
    pid = int(open(os.path.join(run, "pid")).read())
    if not os.path.exists(os.path.join(run, f"wtrace.{pid}")):
        return None
    return family_temp_bytes(run, began, ended)


def independent_geometry(run, began: float) -> "dict | None":
    """Amendment 19 HR-16: a deletion's pre-operation geometry observed
    independently of the product's operation record: page size and usable
    size from the database header the window's start snapshot read
    (`record-start.json` `geometry`), and the page count the database had
    when the operation began - the size field of the last commit frame the
    traced family logged for conversations.db before `began`, else the
    start snapshot's page count when no commit happened since it. None when
    the run never captured the start snapshot's geometry."""
    start = _read_json(os.path.join(run, "record-start.json")) or {}
    base = start.get("geometry")
    if not isinstance(base, dict) or any(
            base.get(k) is None for k in ("pageSize", "usableSize",
                                          "pageCount")):
        return None
    commits = [f for f in (_frame_records(run, "wtrace") or [])
               if os.path.basename(f["path"]) == "conversations.db-wal"
               and f.get("frame", 0) >= 1 and f.get("commit")
               and float(start.get("t") or 0) <= f["t"] < began]
    page_count, source = base["pageCount"], "record-start PRAGMA page_count"
    if commits:
        last = max(commits, key=lambda f: (f["t"], f["frame"]))
        page_count, source = int(last["commit"]), "last WAL commit frame"
    return {"pageSize": base["pageSize"], "usableSize": base["usableSize"],
            "pageCount": page_count,
            "source": f"observed: database header + {source}"}


def c_deletion_receipts(run, ops, retention) -> "list[dict]":
    """§6.3 C condition 2: a receipt for every deletion in the window, its
    frame identities from the dashboard's interposer frame log (C does not
    pin the WAL). Amendment 19 HR-16: its geometry is observed independently
    (`independent_geometry`), never taken from the product's own record,
    which the receipt keeps beside it for comparison; where the run never
    captured it the receipt says so and is INVALID. The checkpoint copy is
    derived, not measured: one page-size copy per distinct page the
    operation's committed frames hold (`copySource`)."""
    sys.path.insert(0, str(HERE))
    import analyze_op

    pid = int(open(os.path.join(run, "pid")).read())
    frames_log = os.path.join(run, f"wtrace.{pid}.frames")
    coefficients = {"fixed": retention.DELETION_FIXED_BYTES,
                    "perRow": retention.DELETION_PER_ROW_BYTES,
                    "perRowFallback": retention.DELETION_PER_ROW_FALLBACK_BYTES}
    receipts = []
    for op in ops:
        frames, header = analyze_op.frames_from_log(
            frames_log, wal_name="conversations.db-wal", began=op["began"],
            ended=op["ended"])
        geometry = independent_geometry(run, op["began"])
        page_size = (geometry or {}).get("pageSize")
        receipt = analyze_op.deletion_receipt(
            op=op, frames=frames, frame_source="dashboard frame log",
            geometry=geometry, coefficients=coefficients,
            wal_header_bytes=header,
            temp_bytes=_etilqs_between(run, op["began"], op["ended"]),
            copy_bytes=(None if frames is None or page_size is None
                        else len(set(frames)) * page_size),
            identities={"run": run}, committed=True)
        receipt["geometrySource"] = ((geometry or {}).get("source")
                                     or "not captured: no independent "
                                        "geometry in record-start.json")
        receipt["recordedGeometry"] = {"pageSize": op.get("page_size"),
                                       "usableSize": op.get("usable_size"),
                                       "pageCount": op.get("page_count")}
        receipt["copySource"] = "derived: distinct pages x page size"
        receipts.append(receipt)
    return receipts


def c_verdict(run, tree) -> int:
    start, end = _window(run)
    invalid = admission_problems(run) + terminal_problems(run, end)
    if invalid:
        return _emit({"valid": False, "problems": invalid}, 2)
    retention = _candidate("_lib_conversation_retention")
    ops = captured_ops(run)
    if ops is None:
        return _emit({"valid": False, "problems": [
            "no complete operation capture (ops.jsonl)"]}, 2)
    for op in ops:
        op.setdefault("started_at", dt.datetime.fromtimestamp(
            op["began"], UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    result = c_conditions(ops, start, end, retention.recompute_charge)
    if not result["valid"]:
        return _emit(result, 2)
    snaps = {}
    for name in ("record-start", "record-end"):
        path = os.path.join(run, f"{name}.json")
        snaps[name] = json.load(open(path)) if os.path.exists(path) else None
    incomplete = (c_completeness(ops, snaps["record-start"], snaps["record-end"])
                  + c_continuation_problems(snaps["record-end"]))
    if incomplete:
        return _emit({**result, "valid": False, "problems": incomplete}, 2)
    analyzer = run_analyzer(run, start, end)
    if analyzer:
        return _emit({**result, "valid": False, "problems": [analyzer]}, 2)
    in_window = [op for op in ops if op["phase"] == "delete"
                 and op.get("outcome") == "ok" and start <= op["began"] <= end]
    receipts = c_deletion_receipts(run, in_window, retention)
    result["receipts"] = receipts
    if any(r["verdict"] == "INVALID" for r in receipts):
        return _emit({**result, "valid": False, "problems": [
            f"deletion {r['opId']}: {'; '.join(r['validity']['reasons'])}"
            for r in receipts if r["verdict"] == "INVALID"]}, 2)
    for r in receipts:
        result["problems"].extend(f"deletion {r['opId']}: {p}"
                                  for p in r["i4"]["reasons"])
    limits = _load_limits(tree)
    # HR-8: the candidate kernel's statistic with the marked deletion
    # intervals; HR-5: its per-publication cap too (ten publications).
    warm = float(json.load(open(os.path.join(run, "window.json"))).get(
        "warm", start))
    deletions = kernel_deletions(
        counter_samples(run), [op for op in ops if op["phase"] == "delete"
                               and op.get("outcome") == "ok"])
    steady = kernel_steady(run, start, end, warm, deletions)
    if not steady["valid"]:
        return _emit({**result, "valid": False, "problems": steady["problems"],
                      "steady": steady}, 2)
    result["problems"].extend(f"steady {p}"
                              for p in steady_limit_problems(steady, limits))
    temp = _etilqs_in_window(run, start, end)
    if temp:
        result["problems"].append(f"{temp} etilqs bytes in the interval "
                                  "(whole family)")
    result.update({"worstBytesPerMinute": steady["worstBytesPerMinute"],
                   "worstBytesPerPublication":
                       steady["worstBytesPerPublication"],
                   "steady": steady, "etilqsBytes": temp,
                   "inputs": input_evidence(run)})
    return _with_drain(run, result, 1 if result["problems"] else 0)


#: Spec §6.3 D (Q12, `dc9` G4): the envelope and the sampling limits.
D_FIXED_BYTES = 4 * MiB
D_PER_CONSUMED_BYTE = 8
D_INGESTED_REQUIRED = 20
D_MAX_INVOCATIONS = 25
D_MAX_DEFERRED_SHARE = 0.20
_LIFECYCLE_FIELD = re.compile(r"\b(sync|backlog|result|dur_ms)=(\S+)")


def d_lifecycle(lines) -> "list[dict]":
    """The hook's Codex lifecycle lines (one per due root): sync, backlog and
    result, as `hook-tick` logs them."""
    out = []
    for line in lines or ():
        fields = dict(_LIFECYCLE_FIELD.findall(line))
        if "result" not in fields:
            continue
        try:
            backlog = int(fields.get("backlog", "0"))
        except ValueError:
            backlog = None
        out.append({"sync": fields.get("sync"), "backlog": backlog,
                    "result": fields["result"]})
    return out


def d_lifecycle_dur_ms(lines) -> "list[int | None]":
    """The `dur_ms` of each of `d_lifecycle`'s lines, in the same order: the
    hook's own timer for that root, None where the line carries no integer
    `dur_ms`."""
    out = []
    for line in lines or ():
        fields = dict(_LIFECYCLE_FIELD.findall(line))
        if "result" not in fields:
            continue
        try:
            out.append(int(fields["dur_ms"]))
        except (KeyError, ValueError):
            out.append(None)
    return out


def d_judge(row, next_row=None, budget_s=None) -> dict:
    """{"class", "rule", "problems"} for one D invocation. `class` is
    `ingested` (rule `own-append`: its own append consumed, with the cursor,
    token-row and quota-row proof), `deferred` or `error`. Two rules admit a
    deferral, and `rule` names the one that did:
    - `Q12`: a successful budgeted continuation - every lifecycle line
      `result=success`, a positive backlog on one of them, and consumed-range
      evidence that its walk consumed OTHER owed data. A positive backlog
      alone is not a deferral.
    - `Q22` (`d_before_walk_deferral`): the ingest budget expired before the
      walk consumed anything, and the immediately following invocation
      `next_row` proved ingest of this invocation's append.
    An `error` carries the reasons the before-walk proof failed."""
    if row.get("rc") != 0:
        return {"class": "error", "rule": None,
                "problems": [f"exit {row.get('rc')}"]}
    if row.get("ingested"):
        return {"class": "ingested", "rule": "own-append", "problems": []}
    lifecycle = d_lifecycle(row.get("hookLog"))
    if (lifecycle and all(x["result"] == "success" for x in lifecycle)
            and any((x["backlog"] or 0) > 0 for x in lifecycle)
            and (row.get("consumedOtherBytes") or 0) > 0):
        return {"class": "deferred", "rule": "Q12", "problems": []}
    problems = d_before_walk_deferral(row, next_row, budget_s)
    if not problems:
        return {"class": "deferred", "rule": "Q22", "problems": []}
    return {"class": "error", "rule": None, "problems": problems}


def d_classify(row, next_row=None, budget_s=None) -> str:
    """`ingested`, `deferred` or `error` (`d_judge`). Without the
    immediately following invocation and the hook's ingest budget, only the
    per-row rules (own append, Q12) can admit an invocation."""
    return d_judge(row, next_row, budget_s)["class"]


def d_classify_all(rows, budget_s) -> "dict[int, dict]":
    """`d_judge` over a run's receipts in invocation order, each judged with
    its immediate successor (the receipt whose `i` is one more)."""
    ordered = sorted(rows, key=lambda r: r["i"])
    out = {}
    for n, row in enumerate(ordered):
        successor = ordered[n + 1] if n + 1 < len(ordered) else None
        out[row["i"]] = d_judge(row, successor, budget_s)
    return out


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def d_before_walk_deferral(row, next_row, budget_s) -> "list[str]":
    """Spec §6.3 D, before-walk budget deferral (Q22): [] when the receipts
    prove every condition, else the reasons they do not. Missing evidence
    for any condition is a reason.
    1. Exit 0, every lifecycle line `result=success`, a positive backlog on
       at least one line.
    2. Consumed bytes C zero on every root (`consumedBytes`, the sum of the
       cursor advances across all roots, and `consumedOtherBytes`), and both
       the recorded duration (`end - start`) and the hook's own lifecycle
       timer (the largest `dur_ms` across its lifecycle lines; a line without
       one is a reason) at least the ingest budget.
    3. The immediately following invocation (`i` one more) consumed this
       invocation's appended range [size - bytes, size): its cache cursor
       started at or below the range and ended at or beyond it, and its token
       and quota rows grew by at least this append's expected rows (its token
       counts, and one quota row) plus those of its own append when its
       cursor also passed that one."""
    problems = []
    if row.get("rc") != 0:
        problems.append(f"exit {row.get('rc')}")
    lifecycle = d_lifecycle(row.get("hookLog"))
    if not lifecycle:
        problems.append("no lifecycle line")
    elif not all(x["result"] == "success" for x in lifecycle):
        problems.append("a lifecycle result other than success")
    elif not any((x["backlog"] or 0) > 0 for x in lifecycle):
        problems.append("no positive backlog")
    for key in ("consumedBytes", "consumedOtherBytes"):
        if not _number(row.get(key)):
            problems.append(f"no {key}")
        elif row[key] != 0:
            problems.append(f"{key} {row[key]} (the walk consumed data)")
    start, end = row.get("start"), row.get("end")
    if not _number(budget_s):
        problems.append("no ingest budget")
    elif not (_number(start) and _number(end)):
        problems.append("no recorded start and end")
    elif end - start < budget_s:
        problems.append(f"duration {end - start:.2f} s below the "
                        f"{budget_s:g} s ingest budget")
    durations = d_lifecycle_dur_ms(row.get("hookLog"))
    if lifecycle and any(d is None for d in durations):
        problems.append("a lifecycle line without dur_ms")
    elif lifecycle and _number(budget_s) and max(durations) < budget_s * 1000:
        problems.append(f"lifecycle dur_ms {max(durations)} below the "
                        f"{budget_s:g} s ingest budget")
    appended = row.get("appended") or {}
    if not all(_number(appended.get(k)) for k in ("size", "bytes", "events")):
        problems.append("no appended range")
        return problems
    if next_row is None or next_row.get("i") != row.get("i", 0) + 1:
        problems.append("no next invocation to prove the append ingested")
        return problems
    before, after = next_row.get("before") or {}, next_row.get("after") or {}
    own = next_row.get("appended") or {}
    fields = ("cacheCursor", "tokenRows", "quotaRows")
    if not (all(_number(before.get(k)) for k in fields)
            and all(_number(after.get(k)) for k in fields)
            and all(_number(own.get(k)) for k in ("size", "events"))):
        problems.append(f"the next invocation (hook {next_row.get('i')}) "
                        "has no cursor, row or append evidence")
        return problems
    first = appended["size"] - appended["bytes"]
    if before["cacheCursor"] > first:
        problems.append(f"the next invocation's cursor started at "
                        f"{before['cacheCursor']}, past this append's start "
                        f"{first}")
    if after["cacheCursor"] < appended["size"]:
        problems.append(f"the next invocation's cursor {after['cacheCursor']} "
                        f"is short of this append's end {appended['size']}")
    covered_own = after["cacheCursor"] >= own["size"]
    tokens = appended["events"] + (own["events"] if covered_own else 0)
    quota = 1 + (1 if covered_own else 0)
    if after["tokenRows"] - before["tokenRows"] < tokens:
        problems.append(f"the next invocation wrote "
                        f"{after['tokenRows'] - before['tokenRows']} token "
                        f"rows (expected {tokens})")
    if after["quotaRows"] - before["quotaRows"] < quota:
        problems.append(f"the next invocation wrote "
                        f"{after['quotaRows'] - before['quotaRows']} quota "
                        f"rows (expected at least {quota})")
    return problems


#: The product constant that sets the hook's ingest budget when the clone
#: configures none (Q22). D's runner keeps D's budget but does not record it.
D_BUDGET_CONSTANT = "CODEX_HOOK_INGEST_BUDGET_DEFAULT_SECONDS"
D_BUDGET_MODULE = "bin/_cctally_config.py"
#: The config key that overrides the constant
#: (`resolve_codex_hook_ingest_budget(load_config())`).
D_BUDGET_OVERRIDE = ("codex", "hook", "ingest_budget_seconds")
_GIT_REV = re.compile(r"[0-9a-f]{7,64}")


def _d_budget_config(receipt) -> "tuple[str | None, str]":
    """(problem, path) for the config the hook read: the source data dir's
    config.json, which the run's clone is a copy of (run-workload.sh clones
    `$SRC/data`; catchup.json records `$SRC` as `identity.sourceRoot`).
    `problem` is None only when that config exists, parses to an object and
    does not touch the budget's override key; the harness does not
    re-implement the product's resolution of an override."""
    source = (receipt.get("identity") or {}).get("sourceRoot")
    if not isinstance(source, str) or not source:
        return ("no source root (identity.sourceRoot) in catchup.json to "
                "locate the config the hook read", "")
    path = os.path.join(source, "data", "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError) as exc:
        return f"cannot read the config the hook read, {path}: {exc}", path
    if not isinstance(config, dict):
        return f"{path} is not a JSON object", path
    key = ".".join(D_BUDGET_OVERRIDE)
    node = config
    for part in D_BUDGET_OVERRIDE[:-1]:
        if part not in node:
            return None, path
        node = node[part]
        if not isinstance(node, dict):
            return (f"{path} has a malformed {part} block where {key} may be "
                    "set; the harness does not re-implement the product's "
                    "resolution"), path
    if D_BUDGET_OVERRIDE[-1] in node:
        return (f"{path} sets {key} (an override of {D_BUDGET_CONSTANT}); the "
                "harness does not re-implement the product's resolution"), path
    return None, path


def d_ingest_budget(run) -> dict:
    """{"seconds", "basis"}: the hook ingest budget a Q22 before-walk
    deferral must reach. The runner does not record it, so it is the
    product default read, as data, from `bin/_cctally_config.py` at the
    commit the run's catch-up receipt records for the tree under test
    (`git show`, not the mutable working tree), provided the config the
    hook read sets no override. `seconds` is None - and then no before-walk
    deferral is admitted - when the tree, its commit, that config or exactly
    one plain numeric definition of the constant cannot be established."""
    receipt = _read_json(os.path.join(run, "catchup.json")) or {}
    tree = receipt.get("tree")
    if not tree:
        return {"seconds": None,
                "basis": "no tree under test in catchup.json"}
    rev = receipt.get("gitRev")
    recorded = (receipt.get("identity") or {}).get("gitRev")
    if not isinstance(rev, str) or not _GIT_REV.fullmatch(rev):
        return {"seconds": None,
                "basis": f"no commit (gitRev) of the tree {tree} in "
                         "catchup.json"}
    if recorded is not None and recorded != rev:
        return {"seconds": None,
                "basis": f"catchup.json records two commits, {rev} and "
                         f"{recorded}"}
    problem, config = _d_budget_config(receipt)
    if problem:
        return {"seconds": None, "basis": problem}
    source = f"{rev}:{D_BUDGET_MODULE} in {tree}"
    try:
        shown = subprocess.run(
            ["git", "-C", str(tree), "show", f"{rev}:{D_BUDGET_MODULE}"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"seconds": None, "basis": f"cannot read {source}: {exc}"}
    if shown.returncode != 0:
        return {"seconds": None,
                "basis": f"cannot read {source}: "
                         f"{shown.stderr.strip() or shown.returncode}"}
    name = re.escape(D_BUDGET_CONSTANT)
    definitions = re.findall(
        rf"^[ \t]*{name}\b[ \t]*(?::|=|[-+*/%@&|^]=|//=|\*\*=|<<=|>>=).*$",
        shown.stdout, re.M)
    if len(definitions) != 1:
        return {"seconds": None,
                "basis": f"{len(definitions)} definitions of "
                         f"{D_BUDGET_CONSTANT} in {source} (need exactly one)"}
    match = re.fullmatch(rf"{name}\s*=\s*([0-9]+(?:\.[0-9]*)?)\s*",
                         definitions[0])
    if not match:
        return {"seconds": None,
                "basis": f"{D_BUDGET_CONSTANT} in {source} is not a plain "
                         f"number: {definitions[0].strip()!r}"}
    return {"seconds": float(match.group(1)),
            "basis": f"product default {D_BUDGET_CONSTANT} in {source} (the "
                     "budget is not recorded by the runner); the config the "
                     f"hook read, {config}, sets no "
                     f"{'.'.join(D_BUDGET_OVERRIDE)}"}


def d_envelope(row) -> "tuple[int, int] | None":
    """(W, limit): the interposer's bytes for the hook and its causal workers
    against 4 MiB + 8 x the bytes it consumed across every root; None without
    consumed-range evidence."""
    consumed = row.get("consumedBytes")
    if consumed is None or not row.get("processes"):
        return None
    written = sum(p["temp"] + p["other"] for p in row["processes"])
    return written, D_FIXED_BYTES + D_PER_CONSUMED_BYTE * int(consumed)


def d_verdict(run) -> int:
    """§6.3 D over hook_runner.py's receipts (revision 11, `dc9` G4). Every
    invocation is classified with its immediate successor (`d_classify_all`:
    its own append, a Q12 continuation, or a Q22 before-walk deferral against
    `d_ingest_budget`, each deferral tagged with its rule in `invocations` and
    counted in `deferredByRule`); INVALID without drained admission,
    with an `error` invocation, incomplete evidence (no trace, dropped records,
    attribution that does not reconcile, no terminal footprint, no
    consumed-range evidence), fewer than 20 invocations that ingested their own
    append, more than 25 invocations, more than 20% deferred (starvation is
    reported separately), or an appended marker not proven ingested. FAIL when
    any invocation - ingested or deferred - or a worker it launched wrote temp
    bytes, or wrote more than 4 MiB + 8 x its consumed bytes; a violation is a
    FAIL even when the run is incomplete."""
    invalid = admission_problems(run)
    path = os.path.join(run, "hooks.jsonl")
    if not os.path.exists(path):
        return _emit({"valid": False, "problems": invalid + [
            "no hook receipts (hooks.jsonl)"]}, 2)
    rows = [json.loads(l) for l in open(path) if l.strip()]
    budget = d_ingest_budget(run)
    judged = d_classify_all(rows, budget["seconds"])
    classes = {i: j["class"] for i, j in judged.items()}
    ingested = [r for r in rows if classes[r["i"]] == "ingested"]
    deferred = [r for r in rows if classes[r["i"]] == "deferred"]
    for row in rows:
        if classes[row["i"]] == "error":
            if row.get("rc") != 0:
                invalid.append(f"hook {row['i']}: exit {row.get('rc')}")
            else:
                invalid.append(
                    f"hook {row['i']} ingested nothing and is not an admitted "
                    "deferral: " + "; ".join(row.get("ingestProblems") or [])
                    + "; before-walk deferral (Q22) not proven: "
                    + "; ".join(judged[row["i"]]["problems"]))
    if len(rows) > D_MAX_INVOCATIONS:
        invalid.append(f"{len(rows)} invocations (at most {D_MAX_INVOCATIONS})")
    if len(ingested) < D_INGESTED_REQUIRED:
        invalid.append(f"{len(ingested)} invocations ingested their own append "
                       f"(need {D_INGESTED_REQUIRED})")
    share = len(deferred) / len(rows) if rows else 0.0
    if share > D_MAX_DEFERRED_SHARE:
        invalid.append(f"{len(deferred)} of {len(rows)} invocations deferred "
                       f"({share:.0%} > {D_MAX_DEFERRED_SHARE:.0%})")
    summary_path = os.path.join(run, "hooks-summary.json")
    summary = (json.load(open(summary_path))
               if os.path.exists(summary_path) else {})
    if not summary.get("markersProven"):
        invalid.append("not every appended marker is proven ingested: "
                       + "; ".join(summary.get("markerProblems")
                                   or ["no marker proof (hooks-summary.json)"]))
    violations = []
    per = []
    sys.path.insert(0, str(HERE))
    import hook_runner
    for row in rows:
        # Amendment 19 HR-4: the run's traces, read now, are the evidence -
        # a worker still running when the hook returned was missing from
        # the receipt's own list. A trace without its exit snapshot means
        # an invocation whose work is not fully on record.
        prefix = f"hook-{row['i']}"
        open_ = hook_runner.open_traces(pathlib.Path(run), prefix)
        if open_ or row.get("settled") is False:
            invalid.append(f"hook {row['i']}: processes traced without an exit "
                           f"snapshot {open_ or row.get('unsettled')} (the "
                           "hook's workers had not settled)")
        traced = hook_runner.traced_processes(pathlib.Path(run), prefix)
        if traced:
            row = dict(row, processes=traced,
                       tempBytes=sum(p["temp"] for p in traced))
        if not row.get("processes"):
            invalid.append(f"hook {row['i']}: no interposer trace")
        if any(p.get("dropped") or p.get("framesDropped")
               for p in row.get("processes") or ()):
            invalid.append(f"hook {row['i']}: dropped records")
        if not row.get("attributionComplete"):
            invalid.append(f"hook {row['i']}: "
                           + "; ".join(row.get("attributionProblems") or []))
        if any(p.get("footprintPeak") in (None, -1)
               for p in row.get("processes") or ()):
            invalid.append(f"hook {row['i']}: no terminal footprint")
        envelope = d_envelope(row)
        if envelope is None:
            invalid.append(f"hook {row['i']}: no consumed-range evidence")
        elif envelope[0] > envelope[1]:
            violations.append(
                f"hook {row['i']} ({classes[row['i']]}) wrote {envelope[0]} "
                f"bytes > 4 MiB + 8 x {row['consumedBytes']} consumed "
                f"= {envelope[1]}")
        if row.get("tempBytes"):
            violations.append(f"hook {row['i']} ({classes[row['i']]}) wrote "
                              f"{row['tempBytes']} etilqs bytes")
        per.append({"i": row["i"], "class": classes[row["i"]],
                    "rule": judged[row["i"]]["rule"],
                    "bytes": None if envelope is None else envelope[0],
                    "envelopeBytes": None if envelope is None else envelope[1],
                    "consumedBytes": row.get("consumedBytes"),
                    "consumedOtherBytes": row.get("consumedOtherBytes"),
                    "etilqs": row.get("tempBytes"), "wallS": row.get("wallS"),
                    "appendedBytes": (row.get("appended") or {}).get("bytes"),
                    "footprintPeakBytes": row.get("footprintPeakBytes"),
                    "workers": len(row.get("workers") or ())})
    starvation = {"deferred": len(deferred), "invocations": len(rows),
                  "share": share, "ingested": len(ingested)}
    stats = {"invocations": per, "starvation": starvation,
             "deferredByRule": {
                 rule: sum(1 for j in judged.values()
                           if j["class"] == "deferred" and j["rule"] == rule)
                 for rule in ("Q12", "Q22")},
             "ingestBudget": budget}
    if rows and rows[-1].get("end") and rows[0].get("start"):
        span_min = (rows[-1]["end"] - rows[0]["start"]) / 60
        stats["invocationsPerMinute"] = len(rows) / span_min if span_min else None
    measured = [p for p in per if p["bytes"] is not None]
    if measured:
        stats.update({
            "meanBytes": statistics.mean(p["bytes"] for p in measured),
            "maxBytes": max(p["bytes"] for p in measured),
            "meanWallS": statistics.mean(p["wallS"] or 0 for p in measured),
            "footprintPeakMaxBytes": max(
                (p["footprintPeakBytes"] or 0) for p in measured)})
    stats["inputs"] = input_evidence(run)
    if violations:
        return _emit({"valid": not invalid, "problems": violations,
                      "incomplete": invalid, **stats}, 1)
    if invalid:
        return _emit({"valid": False, "problems": invalid, **stats}, 2)
    return _emit({"valid": True, "problems": [], **stats}, 0)


def _percentile(values, q):
    """bench/dashboard-soak.py's nearest-rank percentile, so the §6.3 numbers
    compare with the soak ceilings they are judged against."""
    values = sorted(float(v) for v in values)
    if not values:
        return None
    return values[max(0, min(len(values) - 1, math.ceil(q * len(values)) - 1))]


def _flatten(node, prefix, out):
    path = f"{prefix}/{node['name']}" if prefix else node["name"]
    out.setdefault(path, []).append(float(node["elapsed_ms"]))
    for child in node.get("children") or ():
        _flatten(child, path, out)


def workload_kind(run) -> "tuple[str, str]":
    """(workload, basis): the runner's `window.json` label, else inferred from
    the run's own artifacts (B appends, C captures operations, D runs hooks)."""
    path = os.path.join(run, "window.json")
    window = json.load(open(path)) if os.path.exists(path) else {}
    if window.get("workload"):
        return str(window["workload"]), "window.json"
    for name, kind in (("append.json", "B"), ("ops.jsonl", "C"),
                       ("hooks.jsonl", "D")):
        if os.path.exists(os.path.join(run, name)):
            return kind, "inferred"
    return "A", "inferred"


def window_coverage_problems(records: dict, window_rows: list, warm: float,
                             end: float) -> "list[str]":
    """Complete tick evidence over [warm, end] (Q19, shared with idle-A by
    Amendment 19 HR-20): a nonempty, gap-free run of records whose
    predecessor was published before the window (the start edge) and whose
    last record is within the window's longest publish period of its end
    (the end edge). A sampling outage at either edge could hide a full
    build, so either gap refuses the exception."""
    if not window_rows:
        return ["no tick records in the warm window"]
    reasons = []
    seqs = [int(row["seq"]) for row in window_rows]
    if seqs[-1] - seqs[0] + 1 != len(seqs):
        reasons.append("the tick evidence has gaps (missing seq numbers)")
    before = records.get(seqs[0] - 1)
    if not (before and before.get("published_at")
            and _epoch(before["published_at"]) < warm):
        reasons.append("incomplete window coverage: no tick record "
                       "published before the window precedes its first")
    periods = [row["period_ns"] / 1e9 for row in window_rows
               if row.get("period_ns") is not None]
    tail = end - _epoch(window_rows[-1]["published_at"])
    if not periods or tail > max(periods):
        reasons.append(f"incomplete window coverage: the last tick record "
                       f"is {tail:.1f} s before the end, longer than the "
                       "window's longest publish period")
    return reasons


def idle_a_exception(run, records: dict, warm: float, end: float) -> dict:
    """Spec §6.3 (Q12, `dc9` G5): the idle-A exception applies only to an
    append-free, drained A whose complete tick evidence over the warm window
    shows idle decisions only - a nonempty, gap-free run of tick records, every
    one `dispatch == idle`, covering both edges of the window (Amendment 19
    HR-20, `window_coverage_problems`). It never covers B or C."""
    kind, basis = workload_kind(run)
    reasons = []
    if kind != "A":
        reasons.append(f"workload {kind} ({basis}), not A")
    reasons += admission_problems(run)
    start, _end = _window(run)
    ambient = ambient_growth(run, start, end)
    if ambient.get("label") != "append-free":
        reasons.append(f"not an append-free window ({ambient})")
    window_rows = [row for seq, row in sorted(records.items())
                   if row.get("published_at")
                   and warm <= _epoch(row["published_at"]) <= end]
    reasons += window_coverage_problems(records, window_rows, warm, end)
    other = sorted({row.get("dispatch") for row in window_rows} - {"idle"},
                   key=str)
    if other:
        reasons.append(f"non-idle decisions in the window: {other}")
    return {"applies": not reasons, "reasons": reasons, "workload": kind,
            "basis": basis, "ticks": len(window_rows)}


#: The frozen-C exception's name in a receipt (spec §6.3, Q19).
FROZEN_C_EXCEPTION = "frozen-C (Q19)"


def frozen_c_qualification(run, records: dict, warm: float,
                           end: float) -> dict:
    """Spec §6.3 (Q19, `dc16` G2): ONE side of the paired frozen-C exception.
    A C run qualifies when it read a sealed freeze, its admission was valid
    and drained, its measured window is append-free, and complete tick
    evidence covers the whole window with exclusively non-cold idle
    decisions: a nonempty, gap-free run of records whose predecessor was
    published before the window (the start edge) and whose last record is
    within the window's longest publish period of its end (the end edge).
    The comparison applies the exception only when BOTH sides qualify
    (`full_build_pair`); the idle-A exception never covers C."""
    kind, basis = workload_kind(run)
    reasons = []
    if kind != "C":
        reasons.append(f"workload {kind} ({basis}), not C")
    if input_evidence(run).get("mode") != "frozen":
        reasons.append("not a frozen run (revision 15: a sealed freeze)")
    reasons += admission_problems(run)
    start, _end = _window(run)
    ambient = ambient_growth(run, start, end)
    if ambient.get("label") != "append-free":
        reasons.append(f"not an append-free window ({ambient})")
    window_rows = [row for _seq, row in sorted(records.items())
                   if row.get("published_at")
                   and warm <= _epoch(row["published_at"]) <= end]
    reasons += window_coverage_problems(records, window_rows, warm, end)
    if window_rows:
        other = sorted({row.get("dispatch") for row in window_rows} - {"idle"},
                       key=str)
        if other:
            reasons.append(f"non-idle decisions in the window: {other}")
        if any(row.get("cold") for row in window_rows):
            reasons.append("cold ticks in the window")
    return {"applies": not reasons, "reasons": reasons, "workload": kind,
            "basis": basis, "ticks": len(window_rows)}


def perf_evidence(run) -> dict:
    """Spec §6.3 performance evidence for one dashboard run: distinct warm full
    builds (p50/p95, as the soak counts them), the publish period, the peak
    physical footprint (a lifetime high-water mark) and sampled resident
    maximum, and per-phase durations from the deep phase trace, one value per
    distinct stored tree built after warm admission. Invalid without a tree.

    Revision 11's idle-A exception (`dc9` G5): an append-free, drained A whose
    complete tick evidence shows only idle decisions reports `fullBuild` as
    not applicable with zero samples - never as zero latency - and waives only
    the full-build phase-tree requirement; publish periods and every other
    requirement stay. Q19 (`dc16` G2): a frozen, drained, append-free C whose
    complete tick evidence covers its window with non-cold idle decisions
    only reports `fullBuild` as not applicable on its side, with zero
    samples and null percentiles; the C comparison applies that only when
    both sides qualify. Any other run without a warm full build is
    invalid."""
    window = json.load(open(os.path.join(run, "window.json")))
    warm, end = float(window["warm"]), float(window["end"])
    pid = int(open(os.path.join(run, "pid")).read())
    records, builds, ingests = {}, {}, {}
    for path in sorted(glob.glob(os.path.join(run, "perf-*.json"))):
        try:
            diag = json.load(open(path))["diagnostic"] or {}
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for row in (diag.get("tick") or {}).get("records") or ():
            if row.get("seq") is not None:
                records[int(row["seq"])] = row
        for slot, key, tree in ((builds, "generated_at", "phases"),
                                (ingests, "ingest_generated_at", "ingest_phases")):
            if diag.get(tree) and diag.get(key) and warm <= _epoch(diag[key]) <= end:
                slot[diag[key]] = diag[tree]
    warm_rows = [row for _seq, row in sorted(records.items())
                 if not row.get("cold") and row.get("published_at")
                 and warm <= _epoch(row["published_at"]) <= end]
    full = [row["duration_ns"] / 1e6 for row in warm_rows
            if row.get("dispatch") == "full" and row.get("duration_ns") is not None]
    periods = [row["period_ns"] / 1e6 for row in warm_rows
               if row.get("period_ns") is not None]
    usage = [json.loads(line) for line in open(os.path.join(run, "rusage.jsonl"))]
    usage = [row for row in usage if row.get("pid") == pid and row.get("rc") == 0]

    def phases(trees):
        flat = {}
        for tree in trees.values():
            _flatten(tree, "", flat)
        return {name: {"n": len(v), "medianMs": statistics.median(v), "maxMs": max(v)}
                for name, v in sorted(flat.items())}
    problems = []
    idle = idle_a_exception(run, records, warm, end) if not full else None
    frozen_c = (frozen_c_qualification(run, records, warm, end)
                if not full and workload_kind(run)[0] == "C" else None)
    exempt = bool(idle and idle["applies"])
    c_exempt = bool(frozen_c and frozen_c["applies"])
    if not builds and not exempt and not c_exempt:
        problems.append("no phase tree stored after warm admission (trace not armed?)")
    if not full and not exempt and not c_exempt:
        problems.append("no warm full build in the window")
    footprints = terminal_footprints(run, pid)
    if footprints["dashboard"] is None:
        problems.append("no terminal lifetime peak footprint for the dashboard")
    full_build = ({"applicable": False, "n": 0, "idleTicks": idle["ticks"],
                   "reason": "idle-A exception: append-free, drained, idle "
                             "decisions only"}
                  if exempt else
                  {"applicable": False, "n": 0, "p50Ms": None, "p95Ms": None,
                   "exception": FROZEN_C_EXCEPTION, "pairRequired": True,
                   "idleTicks": frozen_c["ticks"]}
                  if c_exempt else
                  {"applicable": True, "n": len(full),
                   "p50Ms": _percentile(full, 0.50),
                   "p95Ms": _percentile(full, 0.95)})
    return {"valid": not problems, "problems": problems,
            "fullBuild": full_build, "idleA": idle, "frozenC": frozen_c,
            "terminalFootprintPeakBytes": footprints["dashboard"],
            "workerFootprintPeakBytes": footprints["workers"],
            "catchUp": footprints["catchUp"],
            "fullBuildMs": full, "fullBuildP50Ms": _percentile(full, 0.50),
            "fullBuildP95Ms": _percentile(full, 0.95),
            "publishPeriodP50Ms": _percentile(periods, 0.50),
            "publishPeriodP95Ms": _percentile(periods, 0.95),
            "publishPeriodMaxMs": max(periods, default=None),
            "footprintPeakBytes": max((r["footprint_peak"] for r in usage
                                       if r.get("footprint_peak") is not None), default=None),
            "residentMaxBytes": max((r["resident"] for r in usage
                                     if r.get("resident") is not None), default=None),
            "phases": phases(builds), "ingestPhases": phases(ingests)}


#: Spec §6.3 (Q18, dc15 F2): #901's full-build gate is non-regression only -
#: candidate p50 and p95 each <= this factor x the matched baseline's. The
#: absolute 5 s / 10 s ceilings are #881's and #857 Task B's: reported, never
#: gated here.
FULL_BUILD_BASELINE_FACTOR = 1.2
_FACTOR = fractions.Fraction(6, 5)
#: C's measured interval (run-workload.sh `MEASURE=1800` for C).
C_MEASURE_SECONDS = 1800.0


def _population_problems(name: str, full: dict) -> "list[str]":
    """A comparable warm full-build population: nonempty, every percentile
    finite and positive (a zero or nonfinite denominator is no evidence)."""
    if not full.get("applicable") or not full.get("n"):
        return [f"{name}: no warm full-build population"]
    problems = []
    for q in ("p50Ms", "p95Ms"):
        value = full.get(q)
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value <= 0):
            problems.append(f"{name}: {q} {value!r} is nonfinite or "
                            "nonpositive")
    return problems


def _read_json(path) -> "dict | None":
    try:
        with open(path) as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


#: C's ten-minute rewind (§6.3 C, Q8): the runner rewinds at its first
#: sample at or after 600 s into the interval; one sampling iteration of
#: rusage, dashboard-perf and the jsonl walk may run past it.
C_REWIND_AFTER_S = 600.0
C_REWIND_SLACK_S = 180.0


def c_settings_problems(run, start) -> "tuple[dict, list[str]]":
    """Amendment 19 HR-21: C's workload settings checked against the
    receipts' contents and times, not their presence: batch.json is the
    expiry batch (a cutoff and each provider's groups, rows and largest
    group), rewind.txt names an instant 25 h before it was written, and both
    were written 600 s into the interval (within one sampling iteration)."""
    problems, settings = [], {}
    batch_path = os.path.join(run, "batch.json")
    rewind_path = os.path.join(run, "rewind.txt")
    batch = _read_json(batch_path)
    settings["batch"] = batch is not None
    if batch is None:
        problems.append("workload settings: no ten-minute batch receipt "
                        "(batch.json)")
    else:
        ok = isinstance(batch.get("cutoff"), str) and all(
            isinstance(batch.get(p), dict) and all(
                isinstance(batch[p].get(k), int) for k in
                ("groups", "rows", "largest")) for p in ("claude", "codex"))
        if not ok:
            problems.append("workload settings: batch.json is not an expiry "
                            "batch (cutoff, groups, rows, largest per provider)")
    if not os.path.exists(rewind_path):
        settings["rewound"] = False
        problems.append("workload settings: no retention rewind (rewind.txt)")
        return settings, problems
    settings["rewound"] = True
    written = os.path.getmtime(rewind_path)
    try:
        value = dt.datetime.fromisoformat(
            open(rewind_path).read().strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        problems.append("workload settings: rewind.txt names no instant")
        return settings, problems
    settings["rewindAgoS"] = written - value
    settings["rewoundAtS"] = written - start
    if abs((written - value) - 25 * 3600) > 120:
        problems.append(f"workload settings: the stamp was rewound "
                        f"{(written - value) / 3600:.2f} h, not 25 h")
    if not (C_REWIND_AFTER_S <= written - start
            <= C_REWIND_AFTER_S + C_REWIND_SLACK_S):
        problems.append(f"workload settings: the rewind happened "
                        f"{written - start:.0f} s into the interval, not 600 s")
    if batch is not None and not (0 <= written - os.path.getmtime(batch_path)
                                  <= C_REWIND_SLACK_S):
        problems.append("workload settings: the batch was not recorded at the "
                        "rewind")
    return settings, problems


def c_identity(run) -> "tuple[dict, list[str]]":
    """What a C comparison must match across its two runs (dc15 F2): the
    runtime (Python from the input receipt, SQLite from the catch-up), the
    instrumentation (the write interposer and, when frozen, the namespace
    library, by digest) and C's workload settings (the ten-minute batch and
    retention rewind happened at 600 s, checked by content and time, and the
    thirty-minute interval was measured; Amendment 19 HR-21). The host is
    `inputs.json` `host` (recorded since Amendment 19): compared when both
    runs carry it, and otherwise bound through the one freeze seal (checked
    by the input label), the runtime and the library builds, as before."""
    inputs = _read_json(os.path.join(run, "inputs.json")) or {}
    catchup = _read_json(os.path.join(run, "catchup.json")) or {}
    libraries = inputs.get("libraries") or {}
    identity = {
        "runtime": {"python": (inputs.get("runtime") or {}).get("version"),
                    "sqlite": catchup.get("sqlite")},
        "instrumentation": {
            "wtrace": (libraries.get("wtrace") or {}).get("sha256"),
            "rootmap": (libraries.get("rootmap") or {}).get("sha256")},
        "host": inputs.get("host")}
    problems = []
    for field, where in (("python", "inputs.json runtime.version"),
                         ("sqlite", "catchup.json sqlite")):
        if not identity["runtime"][field]:
            problems.append(f"runtime {field} not recorded ({where})")
    if not identity["instrumentation"]["wtrace"]:
        problems.append("instrumentation: the write interposer's digest is "
                        "not recorded (inputs.json libraries.wtrace)")
    try:
        start, end = _window(run)
        measured = end - start
    except (OSError, ValueError, KeyError, TypeError):
        start, measured = None, None
    if start is None:
        settings = {"batch": os.path.exists(os.path.join(run, "batch.json")),
                    "rewound": os.path.exists(os.path.join(run, "rewind.txt"))}
        problems.append("workload settings: no window (window.json)")
    else:
        settings, wrong = c_settings_problems(run, start)
        problems.extend(wrong)
    settings["measuredSeconds"] = measured
    if measured is None or measured < C_MEASURE_SECONDS:
        problems.append(f"workload settings: measured window {measured} s is "
                        f"shorter than C's {C_MEASURE_SECONDS:.0f} s")
    identity["settings"] = settings
    return identity, problems


def full_build_pair(candidate_run, baseline_run, *, workload: str) -> int:
    """The shared §6.3 full-build comparison kernel (Q18, dc15 F2): one
    candidate run against its matched `56e66f07a` run of the same workload,
    on one freeze (or both live). PASS when the candidate's warm full-build
    p50 and p95 are each <= 1.2 x the baseline's (equality passes); the
    absolute percentiles, sample counts and ratios are reported, never gated.
    INVALID on a run of another workload, invalid input or perf evidence, a
    missing, empty or nonpositive warm population, mixed input evidence, or
    (C) a mismatched runtime, instrumentation or workload identity.

    Q19 (`dc16` G2): a C pair whose two sides BOTH qualify for the frozen-C
    exception (`frozen_c_qualification`) is NOT APPLICABLE, exit 0, with zero
    samples and null percentiles and ratios; one qualifying side against a
    population (a one-sided population) is INVALID. B has no exception."""
    accepted = ("B", "P2") if workload == "B" else (workload,)
    sides, invalid, labels, identities = {}, [], {}, {}
    fulls, excepted = {}, set()
    invalid.extend(pair_identity_problems(candidate_run, baseline_run))
    for name, run in (("candidate", candidate_run), ("baseline", baseline_run)):
        labels[name] = input_evidence(run)
        # Amendment 19 HR-9: B, like C, needs drained admission on both sides.
        invalid.extend(f"{name}: {p}" for p in admission_problems(run))
        kind, basis = workload_kind(run)
        evidence = perf_evidence(run)
        full = evidence.get("fullBuild") or {}
        sides[name] = {"run": run, "workload": kind, "basis": basis,
                       "n": full.get("n"), "p50Ms": full.get("p50Ms"),
                       "p95Ms": full.get("p95Ms"),
                       "publishPeriodP95Ms": evidence.get("publishPeriodP95Ms")}
        if kind not in accepted:
            invalid.append(f"{name}: workload {kind} ({basis}) is not "
                           f"{workload}")
        if not evidence["valid"]:
            invalid.append(f"{name}: " + "; ".join(evidence["problems"]))
        fulls[name] = full
        if workload == "C" and full.get("exception") == FROZEN_C_EXCEPTION:
            excepted.add(name)
        if workload == "C":
            identities[name], missing = c_identity(run)
            invalid.extend(f"{name}: {p}" for p in missing)
    if len(excepted) != 2:
        for name, full in fulls.items():
            if name in excepted:
                invalid.append(
                    f"{name}: a one-sided full-build population: this side is "
                    f"{FROZEN_C_EXCEPTION} idle-only and the other is not "
                    "(the exception needs both sides)")
            else:
                invalid.extend(_population_problems(name, full))
    if labels["candidate"] != labels["baseline"]:
        invalid.append("mixed input evidence in one comparison: candidate "
                       f"{labels['candidate']}, baseline {labels['baseline']} "
                       "(revision 15: one freeze, or both live)")
    if identities:
        for part in ("runtime", "instrumentation"):
            cand, base = (identities["candidate"][part],
                          identities["baseline"][part])
            if cand != base:
                invalid.append(f"mismatched {part}: candidate {cand}, "
                               f"baseline {base}")
        hosts = (identities["candidate"]["host"], identities["baseline"]["host"])
        if None not in hosts and hosts[0] != hosts[1]:
            invalid.append(f"mismatched host: candidate {hosts[0]}, "
                           f"baseline {hosts[1]}")
    for name in sides:
        sides[name]["inputs"] = labels[name]
    result = {"workload": workload, "factor": FULL_BUILD_BASELINE_FACTOR,
              "gate": "Q18: full-build p50 and p95 each <= 1.2 x the matched "
                      "baseline; absolute values reported, not gated "
                      "(the 5 s / 10 s ceilings are #881's)",
              **sides}
    if identities:
        result["identity"] = {
            "runtime": identities["candidate"]["runtime"],
            "instrumentation": identities["candidate"]["instrumentation"],
            "host": {name: identities[name]["host"] for name in identities},
            "settings": {name: identities[name]["settings"]
                         for name in identities}}
    result["trees"] = {"candidate": run_tree_rev(candidate_run),
                       "baseline": run_tree_rev(baseline_run)}
    if invalid:
        return _emit({**result, "valid": False, "problems": invalid}, 2)
    if len(excepted) == 2:
        return _emit({**result, "valid": True, "applicable": False,
                      "exception": FROZEN_C_EXCEPTION, "problems": [],
                      "ratios": {"p50Ms": None, "p95Ms": None}}, 0,
                     label="NOT APPLICABLE")
    cand, base = sides["candidate"], sides["baseline"]
    problems = []
    for q in ("p50Ms", "p95Ms"):
        if fractions.Fraction(cand[q]) > _FACTOR * fractions.Fraction(base[q]):
            problems.append(f"candidate {q} {cand[q]} > 1.2 x baseline "
                            f"{base[q]} = {FULL_BUILD_BASELINE_FACTOR * base[q]:.1f}")
    ratios = {q: cand[q] / base[q] for q in ("p50Ms", "p95Ms")}
    return _emit({**result, "valid": True, "problems": problems,
                  "ratios": ratios}, 1 if problems else 0)


#: The baseline every §6.3 pair compares against.
BASELINE_REV = "56e66f07a"


def run_tree_rev(run) -> "str | None":
    """The git revision of the tree a run measured, from its own receipts:
    the catch-up's (dashboard workloads and D), the operation's (C-op and
    R-cov), or the latency harness's."""
    catchup = _read_json(os.path.join(run, "catchup.json")) or {}
    rev = catchup.get("gitRev") or (catchup.get("identity") or {}).get("gitRev")
    if not rev:
        op = _read_json(os.path.join(run, "op.json")) or {}
        rev = (op.get("identities") or {}).get("gitRev")
    if not rev:
        rev = (_read_json(os.path.join(run, "latency.json")) or {}).get("gitRev")
    return rev or None


def tree_identity_problems(candidate_rev, baseline_rev) -> "list[str]":
    """Amendment 19 HR-9: a pair compares a candidate against the
    `56e66f07a` baseline - never a swapped pair or one tree twice."""
    problems = []
    if not baseline_rev:
        problems.append("baseline: no tree revision recorded")
    elif not str(baseline_rev).startswith(BASELINE_REV):
        problems.append(f"baseline: tree {baseline_rev} is not {BASELINE_REV}")
    if not candidate_rev:
        problems.append("candidate: no tree revision recorded")
    elif str(candidate_rev).startswith(BASELINE_REV):
        problems.append(f"candidate: tree {candidate_rev} is the baseline "
                        f"{BASELINE_REV}")
    return problems


def pair_identity_problems(candidate_run, baseline_run) -> "list[str]":
    return tree_identity_problems(run_tree_rev(candidate_run),
                                  run_tree_rev(baseline_run))


def b_pair(candidate_run, baseline_run) -> int:
    """Spec §6.3 (Q12, `dc9` G5; Q18): B supplies matched, nonempty warm
    full-build populations for both trees, compared B against B with sample
    counts; the idle-A exception never covers B. See `full_build_pair`."""
    return full_build_pair(candidate_run, baseline_run, workload="B")


def c_pair(candidate_run, baseline_run) -> int:
    """Spec §6.3 (Q18, dc15 F2): one candidate C repetition against the
    recipe's one `56e66f07a` C, never pooled with the other repetitions and
    never against B. C's acceptance needs both this and its `c-verdict`; the
    baseline needs only valid admission and perf evidence, never the
    candidate-specific maintenance gates. A pair of frozen, idle-only C runs
    is NOT APPLICABLE (Q19). See `full_build_pair`."""
    return full_build_pair(candidate_run, baseline_run, workload="C")


# ── the performance table's other limits (Amendment 19 HR-6, sr5) ───────────

#: §6.3 performance table: every process within the soak's process ceiling.
PROCESS_CEILING_BYTES = 1536 * MiB
#: ... and each workload's measured process within its baseline plus this.
FOOTPRINT_DELTA_BYTES = {"A": 64 * MiB, "B": 64 * MiB, "P2": 64 * MiB,
                         "C": 512 * MiB, "C-op": 512 * MiB}
#: Ingest phases (§6.3 "Ingest cost"): candidate median <= 1.2 x baseline.
INGEST_FACTOR = fractions.Fraction(6, 5)
_DASHBOARD_KINDS = ("A", "B", "P2", "C")


def perf_kind(run) -> str:
    """The workload a perf pair judges: the runner's label, else the run's
    artifacts (an operation run's op.json is C-op)."""
    kind, basis = workload_kind(run)
    if basis == "inferred" and os.path.exists(os.path.join(run, "op.json")):
        return "C-op"
    return kind


def _phase_population(phases: dict, keep) -> dict:
    return {path: v for path, v in (phases or {}).items()
            if keep(path.rsplit("/", 1)[-1])}


def phase_comparison(name: str, cand: dict, base: dict, *,
                     factor) -> "tuple[list, list, dict]":
    """(invalid, problems, detail) for one phase family: every phase path
    measured on either side must be measured on both (a phase or regime on
    one side only is missing evidence), and each candidate median must be
    <= factor x its matched baseline median (exact rational; equality
    passes; no exemption for a small phase)."""
    invalid, problems, detail = [], [], {}
    paths = sorted(set(cand) | set(base))
    if not paths:
        return [f"{name}: no measured phase on either side"], [], {}
    for path in paths:
        c, b = cand.get(path), base.get(path)
        if c is None or b is None:
            invalid.append(f"{name}: {path} measured on the "
                           f"{'baseline' if c is None else 'candidate'} only")
            continue
        cm, bm = c.get("medianMs"), b.get("medianMs")
        if not all(isinstance(v, (int, float)) and math.isfinite(v)
                   for v in (cm, bm)) or bm <= 0:
            invalid.append(f"{name}: {path} has no positive median on both "
                           "sides")
            continue
        detail[path] = {"candidateMedianMs": cm, "baselineMedianMs": bm,
                        "candidateN": c.get("n"), "baselineN": b.get("n"),
                        "ratio": cm / bm}
        if fractions.Fraction(cm) > factor * fractions.Fraction(bm):
            problems.append(f"{name}: {path} candidate median {cm} ms > "
                            f"{float(factor):g} x baseline median {bm} ms")
    return invalid, problems, detail


def footprint_comparison(kind: str, cand: dict, base: dict) -> "tuple[list, list, dict]":
    """Peak against peak (sr5 HR-6): the measured process's terminal
    lifetime peak, candidate against baseline, with the per-process ceiling
    and the workload's baseline delta each enforced on its own; every other
    candidate process (workers, catch-up) within the ceiling. Missing
    evidence is INVALID."""
    invalid, problems = [], []
    cp, bp = cand.get("peak"), base.get("peak")
    if cp is None:
        invalid.append("candidate: no terminal lifetime peak footprint")
    if bp is None:
        invalid.append("baseline: no terminal lifetime peak footprint")
    detail = {"candidatePeakBytes": cp, "baselinePeakBytes": bp,
              "ceilingBytes": PROCESS_CEILING_BYTES,
              "deltaBytes": FOOTPRINT_DELTA_BYTES.get(kind)}
    if invalid:
        return invalid, problems, detail
    for label, value in [("measured process", cp)] + sorted(
            (cand.get("others") or {}).items()):
        if value is None:
            invalid.append(f"candidate {label}: no terminal footprint")
        elif value > PROCESS_CEILING_BYTES:
            problems.append(f"candidate {label}: peak footprint {value} > the "
                            f"{PROCESS_CEILING_BYTES} process ceiling")
    delta = FOOTPRINT_DELTA_BYTES.get(kind)
    if delta is not None and cp > bp + delta:
        problems.append(f"peak footprint {cp} > baseline {bp} + {delta}")
    return invalid, problems, detail


def _catchup_peak(run) -> "int | None":
    receipt = _read_json(os.path.join(run, "catchup.json")) or {}
    peaks = [receipt.get("footprintPeakBytes") or 0] + [
        rows[-1].get("footprintPeak") or 0
        for rows in _exit_snapshots(run, "catchup").values() if rows]
    return max(peaks) or None


def dashboard_perf_side(run) -> "tuple[dict, list[str]]":
    evidence = perf_evidence(run)
    others = {f"worker {pid}": v
              for pid, v in (evidence.get("workerFootprintPeakBytes") or {}).items()}
    if os.path.exists(os.path.join(run, "catchup.json")):
        others["catch-up"] = _catchup_peak(run)
    side = {"peak": evidence.get("terminalFootprintPeakBytes"), "others": others,
            "phases": evidence.get("phases") or {},
            "ingestPhases": evidence.get("ingestPhases") or {}}
    return side, ([] if evidence["valid"] else list(evidence["problems"]))


def hook_end_to_end(run) -> "tuple[list, list[str]]":
    """D's hook wall time per scheduled invocation, end to end (sr5 HR-6):
    from the hook's start to the end of the last process traced under it -
    an asynchronous worker cannot disappear when its launcher returns. Every
    `hook-{i}.<pid>` trace must have its exit snapshot (HR-4), else the
    invocation's end is unknown and the evidence is incomplete."""
    path = os.path.join(run, "hooks.jsonl")
    if not os.path.exists(path):
        return [], ["no hook receipts (hooks.jsonl)"]
    rows = [json.loads(l) for l in open(path) if l.strip()]
    out, problems = [], []
    for row in rows:
        prefix = f"hook-{row['i']}"
        open_traces = sorted(set(_snapshots(run, prefix))
                             - set(_exit_snapshots(run, prefix)))
        if open_traces:
            problems.append(f"{prefix}: traces without an exit snapshot "
                            f"{open_traces[:5]} (a worker outlived the run)")
            continue
        ends = [rows_[-1]["t"] for rows_ in _exit_snapshots(run, prefix).values()
                if rows_]
        if row.get("start") is None or row.get("end") is None:
            problems.append(f"{prefix}: no start or end recorded")
            continue
        out.append(max([float(row["end"])] + [float(t) for t in ends])
                   - float(row["start"]))
    if not rows:
        problems.append("no scheduled hook invocations")
    return out, problems


def hook_footprints(run) -> dict:
    out = {}
    for path in glob.glob(os.path.join(run, "hook-*.*.exit")):
        lines = [json.loads(l) for l in open(path) if l.strip()]
        peak = lines[-1].get("footprintPeak") if lines else None
        out[os.path.basename(path)[:-len(".exit")]] = (
            None if peak in (None, -1) else peak)
    return out


def op_footprint_side(run) -> "tuple[dict, list[str]]":
    peaks = {pid: rows[-1].get("footprintPeak")
             for pid, rows in _exit_snapshots(run, "wtrace").items() if rows}
    peaks = {pid: (None if v in (None, -1) else v) for pid, v in peaks.items()}
    op = _read_json(os.path.join(run, "op.json")) or {}
    known = [v for v in peaks.values() if v is not None]
    if op.get("footprintPeakBytes"):
        known.append(op["footprintPeakBytes"])
    side = {"peak": max(known, default=None),
            "others": {f"operation process {pid}": v
                       for pid, v in peaks.items()}}
    return side, ([] if peaks else ["no operation process exit snapshot"])


def perf_pair(candidate_run, baseline_run) -> int:
    """Spec §6.3's performance table beyond Q18's full-build gate, with the
    comparators sr5 fixed before any result was read (Amendment 19 HR-6):
    - footprint: peak against peak, the 1,536 MiB ceiling and the A/B
      baseline + 64 MiB or C/C-op baseline + 512 MiB delta each on its own;
    - doctor.gather: candidate median <= matched baseline median, within
      each regime (the full build's and the idle decision's);
    - ingest: `ingest` and every measured `ingest.*` phase (ingest.claude,
      ingest.codex, ingest.store_open included), parent and child compared
      separately, candidate median <= 1.2 x matched baseline median, with no
      small-millisecond exemption;
    - D: the arithmetic mean end-to-end hook wall time per scheduled
      invocation <= the matched baseline mean (median and maximum are
      diagnostics), and every hook process within the ceiling.
    Both runs must be the same workload on one freeze (or both live), the
    baseline `56e66f07a` and the candidate not (HR-9), with drained
    admission (C-op: valid input evidence). Missing required evidence is
    INVALID (exit 2); a violated limit FAIL (1); else PASS (0)."""
    invalid, problems, result = [], [], {"comparators": "sr5 HR-6"}
    invalid.extend(pair_identity_problems(candidate_run, baseline_run))
    kinds = {name: perf_kind(run) for name, run in
             (("candidate", candidate_run), ("baseline", baseline_run))}
    norm = {k: ("B" if v == "P2" else v) for k, v in kinds.items()}
    result["workload"] = kinds
    if norm["candidate"] != norm["baseline"]:
        invalid.append(f"workloads differ: candidate {kinds['candidate']}, "
                       f"baseline {kinds['baseline']}")
    kind = kinds["candidate"]
    if kind not in _DASHBOARD_KINDS + ("D", "C-op"):
        invalid.append(f"perf-pair judges A, B, C, D and C-op, not {kind}")
    labels = {"candidate": input_evidence(candidate_run),
              "baseline": input_evidence(baseline_run)}
    if labels["candidate"] != labels["baseline"]:
        invalid.append("mixed input evidence in one comparison: candidate "
                       f"{labels['candidate']}, baseline {labels['baseline']}")
    result["inputs"] = labels
    result["trees"] = {"candidate": run_tree_rev(candidate_run),
                       "baseline": run_tree_rev(baseline_run)}
    for name, run in (("candidate", candidate_run), ("baseline", baseline_run)):
        checks = (input_problems(run) if kind == "C-op"
                  else admission_problems(run))
        invalid.extend(f"{name}: {p}" for p in checks)
    if kind in _DASHBOARD_KINDS:
        sides = {}
        for name, run in (("candidate", candidate_run),
                          ("baseline", baseline_run)):
            sides[name], bad = dashboard_perf_side(run)
            invalid.extend(f"{name}: perf evidence: {p}" for p in bad)
        bad, fails, detail = footprint_comparison(
            norm["candidate"], sides["candidate"], sides["baseline"])
        invalid += bad
        problems += fails
        result["footprint"] = detail
        for key, label, pick, factor in (
                ("doctorGather", "doctor.gather", "phases",
                 fractions.Fraction(1)),
                ("ingest", "ingest", "ingestPhases", INGEST_FACTOR)):
            keep = ((lambda leaf: leaf == "doctor.gather") if key == "doctorGather"
                    else (lambda leaf: leaf == "ingest"
                          or leaf.startswith("ingest.")))
            bad, fails, detail = phase_comparison(
                label, _phase_population(sides["candidate"][pick], keep),
                _phase_population(sides["baseline"][pick], keep), factor=factor)
            invalid += bad
            problems += fails
            result[key] = detail
    elif kind == "D":
        walls = {}
        for name, run in (("candidate", candidate_run),
                          ("baseline", baseline_run)):
            walls[name], bad = hook_end_to_end(run)
            invalid.extend(f"{name}: {p}" for p in bad)
        result["hookWall"] = {name: {
            "n": len(v), "meanS": statistics.mean(v) if v else None,
            "medianS": statistics.median(v) if v else None,
            "maxS": max(v, default=None)} for name, v in walls.items()}
        if walls["candidate"] and walls["baseline"]:
            cm, bm = (result["hookWall"][n]["meanS"]
                      for n in ("candidate", "baseline"))
            if fractions.Fraction(cm) > fractions.Fraction(bm):
                problems.append(f"hook wall time: candidate mean {cm:.3f} s > "
                                f"baseline mean {bm:.3f} s")
        peaks = hook_footprints(candidate_run)
        result["hookFootprints"] = {"processes": len(peaks),
                                    "maxBytes": max((v for v in peaks.values()
                                                     if v is not None),
                                                    default=None)}
        if not peaks:
            invalid.append("candidate: no hook process exit snapshot")
        for label, value in sorted(peaks.items()):
            if value is None:
                invalid.append(f"candidate {label}: no terminal footprint")
            elif value > PROCESS_CEILING_BYTES:
                problems.append(f"candidate {label}: peak footprint {value} > "
                                f"the {PROCESS_CEILING_BYTES} process ceiling")
        catchup = _catchup_peak(candidate_run)
        if catchup is not None and catchup > PROCESS_CEILING_BYTES:
            problems.append(f"candidate catch-up: peak footprint {catchup} > "
                            f"the {PROCESS_CEILING_BYTES} process ceiling")
    elif kind == "C-op":
        sides = {}
        for name, run in (("candidate", candidate_run),
                          ("baseline", baseline_run)):
            sides[name], bad = op_footprint_side(run)
            invalid.extend(f"{name}: {p}" for p in bad)
        bad, fails, detail = footprint_comparison("C-op", sides["candidate"],
                                                  sides["baseline"])
        invalid += bad
        problems += fails
        result["footprint"] = detail
    if invalid:
        return _emit({**result, "valid": False, "problems": invalid}, 2)
    return _emit({**result, "valid": True, "problems": problems},
                 1 if problems else 0)


def terminal_footprints(run, pid: int) -> dict:
    """Revision-9 memory evidence (§6.3, `dc7` D5): the TERMINAL lifetime
    peak physical footprint of the dashboard (its exit snapshot, else its
    terminal rusage sample taken alive), of every other process the
    interposer traced under the dashboard's prefix (workers, separately),
    and of the catch-up - setup work, reported apart from steady state with
    the migrations it applied."""
    def exits(prefix):
        out = {}
        for path in glob.glob(os.path.join(run, f"{prefix}.*.exit")):
            lines = [json.loads(l) for l in open(path) if l.strip()]
            if lines and lines[-1].get("footprintPeak", -1) >= 0:
                out[lines[-1]["pid"]] = lines[-1]["footprintPeak"]
        return out
    dashboard_exits = exits("wtrace")
    peak = dashboard_exits.get(pid)
    usage = os.path.join(run, "rusage.jsonl")
    if peak is None and os.path.exists(usage):
        rows = [json.loads(l) for l in open(usage) if l.strip()]
        terminal = os.path.join(run, "terminal.json")
        alive_at = (json.load(open(terminal)).get("t")
                    if os.path.exists(terminal) else None)
        alive = [r for r in rows if r.get("pid") == pid and r.get("rc") == 0
                 and r.get("footprint_peak") is not None
                 and (alive_at is None or r["t"] <= alive_at)]
        peak = max((r["footprint_peak"] for r in alive), default=None)
    catchup = None
    path = os.path.join(run, "catchup.json")
    if os.path.exists(path):
        receipt = json.load(open(path))
        catchup = {"footprintPeakBytes": max(
            [receipt.get("footprintPeakBytes") or 0]
            + list(exits("catchup").values())) or None,
            "setupMigrations": receipt.get("setupMigrations"),
            "label": "setup"}
    return {"dashboard": peak,
            "workers": {str(p): v for p, v in dashboard_exits.items()
                        if p != pid},
            "catchUp": catchup}


# ── the lifecycle proof (spec §6.3 revision 13, Q14) ────────────────────────

#: W9's timer: a completed checkpoint at most once per this many seconds...
LIFECYCLE_TIMER_S = 60.0
#: ...plus once per this many bytes of appended WAL frames (its size trigger).
LIFECYCLE_SIZE_BYTES = 16 * MiB


def _store_frames(frames, name: str, start: float, end: float) -> list:
    """One store's WAL frame records inside the window, in log order (time,
    then frame number); frame 0 is a WAL header write."""
    rows = [f for f in frames
            if os.path.basename(f["path"]) == f"{name}-wal"
            and start <= f["t"] <= end]
    return sorted(rows, key=lambda f: (f["t"], f["frame"]))


def _generations(rows) -> "list[dict]":
    """The WAL generations the records show, in order: a generation is a run
    of records under one salt. Each carries its first and last time and its
    page frames (frame >= 1)."""
    out: list = []
    for record in rows:
        if not out or record.get("salt") != out[-1]["salt"]:
            out.append({"salt": record.get("salt"), "first": record["t"],
                        "last": record["t"], "frames": []})
        out[-1]["last"] = record["t"]
        if record["frame"] >= 1:
            out[-1]["frames"].append(record)
    return out


def _commits(frames) -> "list[list[dict]]":
    """Page frames grouped into transactions: a commit ends at a frame whose
    `commit` field (the database size after the commit) is non-zero."""
    out, current = [], []
    for record in frames:
        current.append(record)
        if record.get("commit"):
            out.append(current)
            current = []
    return out


def _store_lifecycle(frames, snaps, name: str, start: float, end: float,
                     period: float) -> dict:
    rows = _store_frames(frames, name, start, end)
    generations = _generations(rows)
    page_frames = [r for g in generations for r in g["frames"]]
    page1 = [c for c in _commits(page_frames)
             if {r["pgno"] for r in c} == {1}]
    writes = _main_write_times(snaps, name)
    transitions, unchecked = 0, []
    for old, new in zip(generations, generations[1:]):
        transitions += 1
        # A WAL restarts only once every frame of the old generation reached
        # the main file: that copy is a main-file write after the old
        # generation's last frame and before the new generation's first.
        if not any(old["last"] - period <= w <= new["first"] + period
                   for w in writes):
            unchecked.append((old["last"] - start, new["first"] - start))
    appended = len(page_frames) * (4096 + WAL_FRAME_HEADER_BYTES)
    allowed = (int((end - start) // LIFECYCLE_TIMER_S) + 1
               + int(appended // LIFECYCLE_SIZE_BYTES))
    return {"walGenerations": len(generations),
            "completedCheckpoints": transitions,
            "allowedCheckpoints": allowed,
            "appendedFrames": len(page_frames),
            "commits": len(_commits(page_frames)),
            "page1OnlyCommits": len(page1),
            "generationsWithoutCheckpoint": unchecked,
            "generationStarts": [g["first"] for g in generations]}


def lifecycle_check(run) -> dict:
    """W9's lifecycle on both stores, from the run's WAL frame log (spec §6.3
    "P after revision 13"): no commit writes page 1 alone; completed
    checkpoints at most once per 60 s plus once per 16 MiB of appended
    frames; and a new WAL generation only after a completed checkpoint. Since
    revision 14 (Q15), also no keeper-yield line in the dashboard log: the
    yield path must not fire in ordinary operation. Status PASS, FAIL or
    INVALID (no frame log, no interposer timeline, or no dashboard log)."""
    start, end = _window(run)
    snaps = _snapshots(run, "wtrace")
    frames = _frame_records(run, "wtrace")
    yields = keeper_yield_lines(run)
    invalid = []
    if frames is None:
        invalid.append("no WAL frame log of the window")
    if yields is None:
        invalid.append("no dashboard log (dash.log) to check for keeper-yield "
                       "lines")
    if not snaps:
        invalid.append("no interposer timeline of the window")
    invalid.extend(interposer_drop_problems(run, "wtrace", snaps))
    if invalid:
        return {"status": "INVALID", "problems": invalid}
    period = _snapshot_period(snaps)
    stores, problems = {}, []
    for name in _DRAIN_STORES:
        store = _store_lifecycle(frames, snaps, name, start, end, period)
        stores[name] = store
        if store["page1OnlyCommits"]:
            problems.append(
                f"{name}: {store['page1OnlyCommits']} commit(s) wrote page 1 "
                "alone")
        if store["completedCheckpoints"] > store["allowedCheckpoints"]:
            problems.append(
                f"{name}: {store['completedCheckpoints']} completed checkpoints "
                f"in {end - start:.0f} s, above the "
                f"{store['allowedCheckpoints']} allowed (one per "
                f"{LIFECYCLE_TIMER_S:.0f} s plus one per 16 MiB of appended "
                "frames)")
        for left, right in store["generationsWithoutCheckpoint"]:
            problems.append(
                f"{name}: a new WAL generation at {right:.0f} s without a "
                f"completed checkpoint after the old one's last frame at "
                f"{left:.0f} s")
    yield_problem = keeper_yield_problem(yields)
    if yield_problem:
        problems.append(yield_problem)
    return {"status": "FAIL" if problems else "PASS", "problems": problems,
            "stores": stores, "seconds": end - start,
            "snapshotPeriodS": period, "keeperYieldLines": len(yields)}


def _interposer_total_at(rows, t: float) -> "int | None":
    """A traced process's cumulative interposer bytes at time `t` (its last
    snapshot at or before `t`)."""
    before = [r for r in rows if r["t"] <= t]
    if not before:
        return None
    return sum(v[0] for v in before[-1]["paths"].values())


def per_generation_fit(run, start: float, end: float) -> dict:
    """§1.8's fit: the dashboard's rusage per sample interval regressed on its
    interposer bytes and on the WAL generations both stores began in that
    interval (least squares through the origin). Indicative only: the two
    regressors are collinear in a steady window."""
    pid = int(open(os.path.join(run, "pid")).read())
    rows = sorted(_snapshots(run, "wtrace").get(pid) or [],
                  key=lambda r: r["t"])
    frames = _frame_records(run, "wtrace") or []
    starts = []
    for name in _DRAIN_STORES:
        starts += [g["first"] for g in _generations(
            _store_frames(frames, name, start, end))]
    samples = [(t, b) for t, b in _rusage(run) if start <= t <= end]
    points = []
    for (t0, b0), (t1, b1) in zip(samples, samples[1:]):
        i0, i1 = _interposer_total_at(rows, t0), _interposer_total_at(rows, t1)
        if i0 is None or i1 is None:
            continue
        points.append((b1 - b0, i1 - i0,
                       sum(1 for s in starts if t0 < s <= t1)))
    sxx = sum(x * x for _y, x, _g in points)
    sgg = sum(g * g for _y, _x, g in points)
    sxg = sum(x * g for _y, x, g in points)
    sxy = sum(x * y for y, x, _g in points)
    sgy = sum(g * y for y, _x, g in points)
    det = sxx * sgg - sxg * sxg
    if len(points) < 3 or det == 0:
        return {"intervals": len(points), "bytesPerInterposerByte": None,
                "bytesPerGeneration": None}
    return {"intervals": len(points),
            "bytesPerInterposerByte": (sxy * sgg - sgy * sxg) / det,
            "bytesPerGeneration": (sgy * sxx - sxy * sxg) / det}


# ── P3: mechanism isolation (spec §6.3 revision 12, `dc10` H3) ──────────────


def _window_bytes(snaps: dict, start: float, end: float) -> "dict[str, int]":
    """Interposer bytes per path basename written inside [start, end], summed
    over every traced process (cumulative at the end minus at the start)."""
    out: dict = {}
    for rows in snaps.values():
        rows = sorted(rows, key=lambda r: r["t"])
        lo = [r for r in rows if r["t"] <= start]
        hi = [r for r in rows if r["t"] <= end] or rows[-1:]
        base = lo[-1]["paths"] if lo else {}
        for path, (written, _calls) in (hi[-1]["paths"] if hi else {}).items():
            name = os.path.basename(path)
            delta = written - (base.get(path) or [0])[0]
            out[name] = out.get(name, 0) + max(0, delta)
    return out


def _exit_bytes(run, prefix: str, pid) -> "dict[str, int]":
    path = os.path.join(run, f"{prefix}.{pid}.exit")
    if not os.path.exists(path):
        rows = _snapshots(run, prefix).get(pid) or []
    else:
        rows = [json.loads(l) for l in open(path) if l.strip()]
    if not rows:
        return {}
    out: dict = {}
    for path_, (written, _calls) in rows[-1]["paths"].items():
        name = os.path.basename(path_)
        out[name] = out.get(name, 0) + written
    return out


def _rusage_window(run, start: float, end: float) -> "int | None":
    samples = _rusage(run)
    lo = [s for s in samples if s[0] <= start]
    hi = [s for s in samples if s[0] >= end]
    if not lo or not hi:
        return None
    return hi[0][1] - lo[-1][1]


def p3_run(run) -> dict:
    """One P3 run's per-store mechanism evidence over its window, plus the
    measured drain (spec §6.3: the comparison uses window plus drain)."""
    start, end = _window(run)
    snaps = _snapshots(run, "wtrace")
    frames = _frame_records(run, "wtrace") or []
    window = _window_bytes(snaps, start, end)
    terminal_path = os.path.join(run, "terminal.json")
    drain = (json.load(open(terminal_path)).get("drain")
             if os.path.exists(terminal_path) else None) or {}
    copier = _exit_bytes(run, "drain", drain.get("pid")) if drain else {}
    stores = {}
    period = _snapshot_period(snaps) if snaps else 5.0
    for name in _DRAIN_STORES:
        records = [f for f in frames
                   if os.path.basename(f["path"]) == f"{name}-wal"
                   and f["frame"] >= 1 and start <= f["t"] <= end]
        lifecycle = _store_lifecycle(frames, snaps, name, start, end, period)
        names = (name, f"{name}-wal", f"{name}-shm")
        interposer = sum(window.get(n, 0) for n in names)
        drained = sum(copier.get(n, 0) for n in names)
        stores[name] = {
            "interposerBytes": interposer,
            "commits": sum(1 for f in records if f.get("commit")),
            "walFrames": len(records),
            "distinctPages": len({f["pgno"] for f in records}),
            "checkpointCopyBytes": window.get(name, 0),
            "drainInterposerBytes": drained,
            "windowPlusDrainBytes": interposer + drained,
            "walGenerations": lifecycle["walGenerations"],
            "completedCheckpoints": lifecycle["completedCheckpoints"]}
    interposer_total = sum(window.values())
    rusage = _rusage_window(run, start, end)
    copier_rusage = (drain.get("rusage") or {}).get("dwrite")
    total_drain = sum(copier.values())
    return {"run": run, "seconds": end - start, "stores": stores,
            "interposerBytes": interposer_total, "rusageBytes": rusage,
            "rusageToInterposer": (rusage / interposer_total
                                   if rusage is not None and interposer_total
                                   else None),
            "drainInterposerBytes": total_drain,
            "drainRusageBytes": copier_rusage,
            "windowPlusDrainInterposerBytes": interposer_total + total_drain,
            "windowPlusDrainRusageBytes": (
                rusage + copier_rusage
                if rusage is not None and copier_rusage is not None else None),
            "perGenerationFit": per_generation_fit(run, start, end)}


def p3_report(off_run, shipped_run) -> dict:
    off, shipped = p3_run(off_run), p3_run(shipped_run)
    lines = [f"{'store':18} {'metric':22} {'w9-off':>14} {'shipped':>14}"]
    for name in _DRAIN_STORES:
        for metric in ("interposerBytes", "commits", "walFrames",
                       "distinctPages", "checkpointCopyBytes",
                       "walGenerations", "completedCheckpoints",
                       "drainInterposerBytes", "windowPlusDrainBytes"):
            lines.append(f"{name:18} {metric:22} "
                         f"{off['stores'][name][metric]:>14} "
                         f"{shipped['stores'][name][metric]:>14}")
    for metric in ("interposerBytes", "rusageBytes", "rusageToInterposer",
                   "drainInterposerBytes", "drainRusageBytes",
                   "windowPlusDrainInterposerBytes",
                   "windowPlusDrainRusageBytes"):
        lines.append(f"{'process':18} {metric:22} {off[metric]!s:>14} "
                     f"{shipped[metric]!s:>14}")
    for metric in ("bytesPerInterposerByte", "bytesPerGeneration",
                   "intervals"):
        lines.append(f"{'fit':18} {metric:22} "
                     f"{off['perGenerationFit'][metric]!s:>14} "
                     f"{shipped['perGenerationFit'][metric]!s:>14}")
    labels = {"w9Off": input_evidence(off_run), "shipped": input_evidence(shipped_run)}
    return {"w9Off": off, "shipped": shipped, "table": lines,
            "inputs": labels,
            "comparable": labels["w9Off"] == labels["shipped"]}


def _emit(result, code, label=None) -> int:
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    print(label or {0: "PASS", 1: "FAIL", 2: "INVALID"}[code])
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("groups")
    p.add_argument("--db", required=True)
    p.add_argument("--provider", choices=tuple(_GROUP), required=True)
    p.add_argument("--largest", type=int, default=0)
    p.add_argument("--median", type=int, default=0)
    p.add_argument("--smallest", type=int, default=0)
    p.add_argument("--dense", type=int, default=0)
    p = sub.add_parser("compact")
    p.add_argument("--tree", required=True)
    p.add_argument("--root", required=True)
    p = sub.add_parser("stamp")
    p.add_argument("--root", required=True)
    p.add_argument("--at", choices=("now", "rewind-25h"), required=True)
    p = sub.add_parser("retention-days")
    p.add_argument("--root", required=True)
    p.add_argument("--days", type=int, required=True)
    p = sub.add_parser("batch")
    p.add_argument("--root", required=True)
    p.add_argument("--days", type=int, required=True)
    p = sub.add_parser("append")
    p.add_argument("--root", required=True)
    p.add_argument("--seconds", type=float, required=True)
    p.add_argument("--kib-per-min", type=float, required=True)
    p.add_argument("--codex-only", action="store_true")
    p.add_argument("--scratch", action="store_true")
    p = sub.add_parser("seed-scratch")
    p.add_argument("--root", required=True)
    p = sub.add_parser("synth-group")
    p.add_argument("--root", required=True)
    p.add_argument("--source-key", required=True)
    p.add_argument("--new-key", required=True)
    p.add_argument("--copies", type=int, required=True)
    p = sub.add_parser("record-snapshot")
    p.add_argument("--root", required=True)
    for name in ("ab-verdict", "c-verdict"):
        p = sub.add_parser(name)
        p.add_argument("run")
        p.add_argument("--tree", required=True)
    p = sub.add_parser("d-verdict")
    p.add_argument("run")
    p = sub.add_parser("drain-verdict")
    p.add_argument("run")
    p = sub.add_parser("lifecycle-verdict")
    p.add_argument("run")
    p = sub.add_parser("p3-report")
    p.add_argument("w9_off")
    p.add_argument("shipped")
    p = sub.add_parser("perf-evidence")
    p.add_argument("run")
    for name in ("b-pair", "c-pair", "perf-pair"):
        p = sub.add_parser(name)
        p.add_argument("candidate")
        p.add_argument("baseline")
    args = parser.parse_args(argv)
    if args.cmd == "groups":
        print(json.dumps(groups(args.db, args.provider, largest=args.largest,
                                median=args.median, smallest=args.smallest,
                                dense=args.dense)))
    elif args.cmd == "compact":
        # A/B need only that reclaim is not eligible; a store already below
        # the start rule (a compacted source root) is not rewritten again.
        if not conversations_reclaim_eligible(args.root):
            print(json.dumps({"skipped": "reclaim not eligible"}))
            return 0
        env = dict(os.environ)
        return subprocess.run(
            [sys.executable, str(pathlib.Path(args.tree) / "bin" / "cctally"),
             "db", "vacuum", "--db", "conversations"], env=env).returncode
    elif args.cmd == "stamp":
        print(stamp(args.root, args.at))
    elif args.cmd == "retention-days":
        retention_days(args.root, args.days)
    elif args.cmd == "batch":
        print(json.dumps(batch(args.root, args.days), sort_keys=True))
    elif args.cmd == "append":
        print(json.dumps(append(args.root, args.seconds, args.kib_per_min,
                                codex_only=args.codex_only, scratch=args.scratch)))
    elif args.cmd == "seed-scratch":
        print(json.dumps(seed_scratch(args.root)))
    elif args.cmd == "synth-group":
        print(json.dumps(synth_group(args.root, args.source_key, args.new_key,
                                     args.copies)))
    elif args.cmd == "record-snapshot":
        print(json.dumps(record_snapshot(args.root)))
    elif args.cmd == "ab-verdict":
        return ab_verdict(args.run, args.tree)
    elif args.cmd == "c-verdict":
        return c_verdict(args.run, args.tree)
    elif args.cmd == "perf-evidence":
        result = perf_evidence(args.run)
        return _emit(result, 0 if result["valid"] else 2)
    elif args.cmd == "b-pair":
        return b_pair(args.candidate, args.baseline)
    elif args.cmd == "c-pair":
        return c_pair(args.candidate, args.baseline)
    elif args.cmd == "perf-pair":
        return perf_pair(args.candidate, args.baseline)
    elif args.cmd == "drain-verdict":
        drain = drain_check(args.run)
        drain["inputs"] = input_evidence(args.run)
        inputs = input_problems(args.run)
        if inputs:
            drain.update(status="INVALID", problems=inputs + drain.get("problems", []))
        return _emit(drain, {"PASS": 0, "FAIL": 1, "INVALID": 2}[drain["status"]])
    elif args.cmd == "lifecycle-verdict":
        lifecycle = lifecycle_check(args.run)
        lifecycle["inputs"] = input_evidence(args.run)
        inputs = input_problems(args.run)
        if inputs:
            lifecycle.update(status="INVALID",
                             problems=inputs + lifecycle.get("problems", []))
        return _emit(lifecycle, {"PASS": 0, "FAIL": 1,
                                 "INVALID": 2}[lifecycle["status"]])
    elif args.cmd == "p3-report":
        report = p3_report(args.w9_off, args.shipped)
        print("\n".join(report.pop("table")))
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    else:
        return d_verdict(args.run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
