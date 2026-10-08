"""#901 spec §6.3 D (Q11, 901-PA-004): D's own hook runner.

usage: hook_runner.py --tree TREE --root ROOT --out OUT --dylib DYLIB
                      [--hooks 20] [--max-invocations 25] [--spacing-s 35]
                      [--settle-s 120] [--reproduce]
  (the clone's environment comes from run-workload.sh: CCTALLY_DATA_DIR on the
   clone, the real roots read-only plus ROOT/scratch, TMPDIR external)

Foreground `hook-tick --foreground --source codex` runs, `--spacing-s` apart
(D's 35-second post-hook spacing), continue until `--hooks` (20) invocations have
ingested their own append, stopping at `--max-invocations` (25) (spec §6.3 D,
revision 11, `dc9` G4). For each:
1. Before: the lifecycle throttle markers' ages, the scratch rollout's cursors
   and its token and quota rows, and the hook-tick log's end.
2. Append ~2 KiB of uniquely marked Codex records to the scratch rollout - token
   counts carrying `rate_limits`, so the ingest must write token AND quota rows.
3. Run the hook under the interposer AND sqlattr.py, recording its start, end and
   exit code.
4. After: the hook's own log lines (its lock and throttle outcome, due roots,
   sync result and projection deferral - absent when no lock was due), the
   cursors and rows again, every process the interposer traced under this hook
   (the hook and any worker it launched, each with its lifetime peak footprint),
   and sqlattr's statement attribution.
5. Proof of ingest: the cursor reached the end of the appended range and the
   expected token and quota rows exist. A hook that did not ingest its own
   append is marked `ingested: false`; `workload.d_classify` then admits it as
   `deferred` only for a successful budgeted continuation whose walk consumed
   other owed data, and anything else is `error` (D is then INVALID). A
   before-walk budget deferral (Q22) needs the NEXT invocation's proof of
   ingest, so the `class` written live here is provisional for a hook that
   consumed nothing: `workload.py d-verdict` judges each receipt with its
   successor (`d_classify_all`).
   Consumed bytes: every Codex source file's cursor (`codex_session_files`
   path, root and offset) is read, read-only, from the clone's cache.db before
   and after the invocation; C is the sum of the advances across all roots,
   ambient and previously deferred data included (`consumedBytes`), and
   `consumedOtherBytes` excludes the scratch rollout.
6. Attribution completeness: sqlattr's attributed + unattributed temp bytes
   equal the interposer's total for the hook process; each worker's temp bytes
   are listed on their own.
7. Settling (Amendment 19 HR-4): a worker the hook detached may still be
   running when the hook returns, so before reading the traces the runner
   waits, up to `--settle-s` (120 s), until every process traced under the
   hook (`hook-{i}.<pid>`) has written its exit snapshot. A trace still open
   at the bound, or one whose process died without its snapshot, is recorded
   (`settled: false`, `unsettled`) and makes D INVALID. `endToEndS` runs
   from the hook's start to the last exit snapshot, so a worker's work counts
   in its invocation's wall time.

Receipts go to OUT/hooks.jsonl (one line per hook), harness evidence only: the
hook's stdout and stderr contracts are unchanged. Before it stops the runner
proves every appended marker ingested, whichever invocation consumed it (the
scratch cursor through the last appended end, and the token and quota rows of
every append), into OUT/hooks-summary.json. `--reproduce` is the
pre-attempt reproduction mode (§6.3 D): the same runner on a given
(pre-revision-9) tree, whose summary must show at least one ingesting,
temp-writing hook with its statements named. Exit 0 when every hook ran and its
evidence is complete, 2 otherwise; `workload.py d-verdict` judges the receipts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
EVENT = '{"hook_event_name":"Stop","session_id":"bench-901","transcript_path":"","cwd":""}'
UTC = dt.timezone.utc


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _scalar(path, sql, args=()):
    if not path.exists():
        return None
    conn = _ro(path)
    try:
        row = conn.execute(sql, args).fetchone()
        return None if row is None else row[0]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def observe(data_dir: pathlib.Path, rollout: str) -> dict:
    cache, conv = data_dir / "cache.db", data_dir / "conversations.db"
    markers = {}
    for path in glob.glob(str(data_dir / "codex-hook-tick" / "*.last-success")):
        markers[os.path.basename(path)] = round(time.time()
                                                - os.stat(path).st_mtime, 3)
    log = data_dir / "logs" / "hook-tick.log"
    return {
        "markerAgesS": markers,
        "cacheCursor": _scalar(cache, "SELECT last_byte_offset FROM "
                               "codex_session_files WHERE path = ?", (rollout,)),
        "conversationCursor": _scalar(
            conv, "SELECT last_byte_offset FROM codex_conversation_source_files "
            "WHERE path = ?", (rollout,)),
        "tokenRows": _scalar(cache, "SELECT COUNT(*) FROM codex_session_entries "
                             "WHERE source_path = ?", (rollout,)),
        "quotaRows": _scalar(cache, "SELECT COUNT(*) FROM quota_window_snapshots "
                             "WHERE source_path = ?", (rollout,)),
        "logBytes": log.stat().st_size if log.exists() else 0,
    }


def cursors(data_dir: pathlib.Path) -> "dict | None":
    """Every Codex source file's cursor in the clone's cache.db, read-only:
    {"<root>\\x00<path>": offset}; None when the store cannot be read."""
    cache = data_dir / "cache.db"
    if not cache.exists():
        return None
    conn = _ro(cache)
    try:
        return {f"{root or ''}\x00{path}": int(offset or 0)
                for path, root, offset in conn.execute(
                    "SELECT path, source_root_key, last_byte_offset "
                    "FROM codex_session_files")}
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def consumed(before: "dict | None", after: "dict | None",
             own_path: str) -> dict:
    """C = the sum of cursor advances across all roots (a new file counts its
    whole offset; a cursor that moved back re-read from zero), and the part
    outside the hook's own scratch rollout."""
    if before is None or after is None:
        return {"consumedBytes": None, "consumedOtherBytes": None,
                "advances": []}
    advances = []
    for key, offset in after.items():
        prior = before.get(key, 0)
        advance = offset - prior if offset >= prior else offset
        if advance:
            advances.append((key.split("\x00", 1)[1], advance))
    total = sum(a for _p, a in advances)
    other = sum(a for p, a in advances if p != own_path)
    advances.sort(key=lambda item: -item[1])
    return {"consumedBytes": total, "consumedOtherBytes": other,
            "advances": [{"path": p, "bytes": a} for p, a in advances[:20]]}


def marker_proof(first: dict, last: dict, appends: "list[dict]") -> "list[str]":
    """Every appended marker ingested, whichever invocation consumed it."""
    if not appends:
        return ["nothing was appended"]
    problems = []
    end = appends[-1]["size"]
    if (last["cacheCursor"] or 0) < end:
        problems.append(f"cache cursor {last['cacheCursor']} short of the last "
                        f"appended end {end}")
    events = sum(a["events"] for a in appends)
    if (last["tokenRows"] or 0) - (first["tokenRows"] or 0) < events:
        problems.append(f"{(last['tokenRows'] or 0) - (first['tokenRows'] or 0)}"
                        f" token rows for {events} appended token counts")
    if (last["quotaRows"] or 0) - (first["quotaRows"] or 0) < len(appends):
        problems.append("fewer quota rows than appends")
    return problems


def append_marked(rollout: pathlib.Path, marker: str, *, events: int = 4,
                  pad: int = 300) -> dict:
    """Append `events` token counts with rate limits, uniquely marked."""
    total = 0
    with open(rollout, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"total_token_usage"' in line:
                try:
                    total = int(json.loads(line)["payload"]["info"][
                        "total_token_usage"]["total_tokens"])
                except (ValueError, KeyError, TypeError):
                    continue
    now = dt.datetime.now(UTC)
    written = 0
    lines = []
    for n in range(events):
        total += 300
        lines.append(json.dumps({
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "type": "event_msg", "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {
                    "input_tokens": 200, "cached_input_tokens": 25,
                    "output_tokens": 100, "reasoning_output_tokens": 25,
                    "total_tokens": 300},
                    "total_token_usage": {"total_tokens": total},
                    "marker": f"{marker}-{n}", "pad": "y" * pad},
                "rate_limits": {
                    "limit_id": "bench901", "limit_name": "Bench 901",
                    "plan_type": "bench",
                    "primary": {"used_percent": 1.0 + n, "window_minutes": 300,
                                "resets_at": int(now.timestamp()) + 4 * 3600},
                    "secondary": {"used_percent": 2.0 + n,
                                  "window_minutes": 10080,
                                  "resets_at": int(now.timestamp()) + 6 * 86400}},
            }}, separators=(",", ":")))
    blob = "".join(line + "\n" for line in lines)
    with open(rollout, "a", encoding="utf-8") as fh:
        fh.write(blob)
    written = len(blob.encode("utf-8"))
    return {"bytes": written, "events": events,
            "size": os.stat(rollout).st_size}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def open_traces(out: pathlib.Path, prefix: str) -> "list[int]":
    """The pids traced under `prefix` (`prefix.<pid>`) without an exit
    snapshot (`prefix.<pid>.exit`)."""
    pending = []
    for path in glob.glob(str(out / f"{prefix}.*")):
        tail = os.path.basename(path)[len(prefix) + 1:]
        if tail.isdigit() and not os.path.exists(f"{path}.exit"):
            pending.append(int(tail))
    return sorted(pending)


def settle(out: pathlib.Path, prefix: str, timeout_s: float, *,
           clock=time.monotonic, sleep=time.sleep) -> "list[int]":
    """HR-4: wait (bounded) until every process traced under `prefix` has
    written its exit snapshot; return the pids still without one - alive at
    the bound, or dead without it (their last bytes are unrecorded)."""
    deadline = clock() + timeout_s
    while True:
        pending = open_traces(out, prefix)
        if not pending or not any(_alive(p) for p in pending) \
                or clock() >= deadline:
            return pending
        sleep(0.2)


def traced_processes(out: pathlib.Path, prefix: str) -> list:
    """Every process the interposer traced under `prefix` (the hook and its
    workers): pid, temp and other bytes, drops, lifetime peak footprint."""
    procs = []
    for path in sorted(glob.glob(str(out / f"{prefix}.*.exit"))):
        lines = [json.loads(l) for l in open(path) if l.strip()]
        if not lines:
            continue
        snap = lines[-1]
        temp = sum(v[0] for k, v in snap["paths"].items()
                   if os.path.basename(k).startswith("etilqs_"))
        procs.append({"pid": snap["pid"], "temp": temp,
                      "other": sum(v[0] for v in snap["paths"].values()) - temp,
                      "dropped": snap.get("dropped", 0),
                      "framesDropped": snap.get("framesDropped", 0),
                      "footprintPeak": snap.get("footprintPeak")})
    return procs


def new_log_lines(data_dir: pathlib.Path, offset: int) -> list:
    log = data_dir / "logs" / "hook-tick.log"
    if not log.exists():
        return []
    with open(log, "rb") as fh:
        fh.seek(offset)
        text = fh.read().decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if "provider=codex" in line
            or "source_root_key=" in line]


def proof_of_ingest(before: dict, after: dict, appended: dict) -> "list[str]":
    problems = []
    if (after["cacheCursor"] or 0) < appended["size"]:
        problems.append(f"cache cursor {after['cacheCursor']} short of the "
                        f"appended end {appended['size']}")
    if (after["tokenRows"] or 0) - (before["tokenRows"] or 0) < appended["events"]:
        problems.append("the appended token counts were not ingested")
    if (after["quotaRows"] or 0) - (before["quotaRows"] or 0) < 1:
        problems.append("the appended rate limits wrote no quota row")
    return problems


def reconcile(sqlattr: "dict | None", main: "dict | None") -> "list[str]":
    if sqlattr is None or main is None:
        return ["no sqlattr output or no interposer trace for the hook"]
    if sqlattr.get("unattributed") is None:
        return ["sqlattr recorded no unattributed bucket"]
    total = sqlattr["attributed"]["temp"] + sqlattr["unattributed"]["temp"]
    if total != main["temp"]:
        return [f"attributed + unattributed temp {total} != interposer "
                f"{main['temp']}"]
    return []


def run_hook(i, args, env, data_dir, rollout) -> dict:
    out = pathlib.Path(args.out)
    before = observe(data_dir, str(rollout))
    appended = append_marked(rollout, f"bench901-hook{i}-{os.getpid()}")
    prefix = f"hook-{i}"
    henv = {**env, "WTRACE_OUT": str(out / prefix), "WTRACE_PERIOD": "1",
            # Revision 15: after the namespace the runner was launched with.
            "DYLD_INSERT_LIBRARIES": ":".join(
                [x for x in env.get("DYLD_INSERT_LIBRARIES", "").split(":")
                 if x and x != args.dylib] + [args.dylib]),
            "SQLATTR_OUT": str(out / f"{prefix}.sqlattr.json"),
            "SQLATTR_TREE": str(args.tree), "SQLATTR_DYLIB": args.dylib}
    cursors_before = cursors(data_dir)
    start = time.time()
    child = subprocess.Popen(
        [sys.executable, str(HERE / "sqlattr.py"), "hook-tick",
         "--foreground", "--source", "codex"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=henv, cwd=str(args.tree))
    try:
        child.communicate(EVENT, timeout=120)
        rc = child.returncode
    except subprocess.TimeoutExpired:
        child.kill()                     # a family kill: recorded by the namespace
        child.communicate()
        rc = None
    # Amendment 13 O3: how the hook ended, beside the runners' records (the
    # namespace's own wait record covers it too; this one names the hook).
    if env.get("ROOTMAP_RECEIPTS"):
        sys.path.insert(0, str(HERE))
        import frozen_roots
        frozen_roots.record_toplevel(env["ROOTMAP_RECEIPTS"], child.pid,
                                     returncode=child.returncode,
                                     runner="hook_runner.py", step=f"hook-{i}")
    end = time.time()
    unsettled = settle(out, prefix, getattr(args, "settle_s", 120.0))
    settled_at = time.time()
    cursors_after = cursors(data_dir)
    after = observe(data_dir, str(rollout))
    procs = traced_processes(out, prefix)
    exit_times = []
    for path in glob.glob(str(out / f"{prefix}.*.exit")):
        lines = [json.loads(l) for l in open(path) if l.strip()]
        if lines:
            exit_times.append(float(lines[-1]["t"]))
    sqlattr_path = out / f"{prefix}.sqlattr.json"
    sqlattr = (json.load(open(sqlattr_path)) if sqlattr_path.exists() else None)
    main = next((p for p in procs if sqlattr and p["pid"] == sqlattr.get("pid")),
                None)
    ingest = proof_of_ingest(before, after, appended)
    completeness = reconcile(sqlattr, main)
    temp_statements = ([s for s in sqlattr["stmts"] if s[2] > 0][:20]
                       if sqlattr else [])
    return {
        "i": i, "rc": rc, "start": start, "end": end, "wallS": end - start,
        "settled": not unsettled, "unsettled": unsettled,
        "settledAt": settled_at,
        "endToEndS": max([end] + exit_times) - start,
        "appended": appended, "before": before, "after": after,
        **consumed(cursors_before, cursors_after, str(rollout)),
        "hookLog": new_log_lines(data_dir, before["logBytes"]),
        "processes": procs,
        "workers": [p for p in procs if main is None or p["pid"] != main["pid"]],
        "ingested": not ingest, "ingestProblems": ingest,
        "attributionComplete": not completeness,
        "attributionProblems": completeness,
        "tempBytes": sum(p["temp"] for p in procs),
        "tempStatements": temp_statements,
        "footprintPeakBytes": None if main is None else main["footprintPeak"],
    }


def main(argv=None) -> int:
    sys.path.insert(0, str(HERE))
    import frozen_roots
    frozen_roots.require_namespace("hook_runner.py")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", required=True, type=pathlib.Path)
    parser.add_argument("--root", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--dylib", required=True)
    parser.add_argument("--hooks", type=int, default=20,
                        help="invocations that must ingest their own append")
    parser.add_argument("--max-invocations", type=int, default=25)
    parser.add_argument("--spacing-s", type=float, default=35.0)
    parser.add_argument("--settle-s", type=float, default=120.0,
                        help="bound on waiting for the hook's workers' exit "
                        "snapshots (HR-4)")
    parser.add_argument("--reproduce", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(HERE))
    import workload

    env = dict(os.environ)
    data_dir = pathlib.Path(env["CCTALLY_DATA_DIR"])
    rollout, _total = workload._codex_target(args.root, scratch=True)
    first = observe(data_dir, str(rollout))
    receipts = []
    with open(args.out / "hooks.jsonl", "a", encoding="utf-8") as fh:
        i = 0
        while True:
            i += 1
            receipt = run_hook(i, args, env, data_dir, rollout)
            receipt["class"] = workload.d_classify(receipt)
            receipts.append(receipt)
            fh.write(json.dumps(receipt, default=str) + "\n")
            fh.flush()
            done = sum(r["class"] == "ingested" for r in receipts)
            if done >= args.hooks or i >= args.max_invocations:
                break
            time.sleep(args.spacing_s)
    markers = marker_proof(first, observe(data_dir, str(rollout)),
                           [r["appended"] for r in receipts])
    summary = {
        "mode": "reproduce" if args.reproduce else "measure",
        "hooks": len(receipts),
        "ingesting": sum(r["ingested"] for r in receipts),
        "classes": {c: sum(r["class"] == c for r in receipts)
                    for c in ("ingested", "deferred", "error")},
        "tempWriting": sum(1 for r in receipts if r["tempBytes"]),
        "markersProven": not markers, "markerProblems": markers,
        "complete": all(r["rc"] == 0 and r["attributionComplete"]
                        and r["processes"] and r["settled"] for r in receipts),
    }
    if args.reproduce:
        hits = [r for r in receipts if r["ingested"] and r["tempBytes"]
                and r["tempStatements"]]
        summary["reproduced"] = bool(hits)
        summary["statements"] = hits[0]["tempStatements"] if hits else []
    (args.out / "hooks-summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n")
    print(json.dumps(summary, default=str))
    return 0 if summary["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
