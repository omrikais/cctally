#!/usr/bin/env python3
"""One catch-up pass of a product TREE, in-process, for write attribution (#939).

  conversation_catchup.py prep --tree TREE [--out OUT.json] [--max-opens N]
  conversation_catchup.py run  --tree TREE --out OUT.json
                               [--mode conversations|cache-cold]
                               [--providers claude,codex]
                               [--passes N] [--cache-sync]
                               [--ownership LOG]

Run it with CCTALLY_DATA_DIR on a CLONE of a closed store copy (never the live
data directory: refused) and TMPDIR / SQLITE_TMPDIR on the external drive.

prep    opens the stores the measured pass opens, repeatedly, until the tree's
        cache and conversations migrations are complete (a gated migration such
        as cache 050 needs several opens), then closes them. It reads no
        transcript. Run it on a store copy BEFORE the freeze is captured, so the
        measured pass starts from the tree's own schema.

run     mode `conversations` (default) is the dashboard's first full
        conversation-sync pass, exactly as `_conversation_sync_pass` runs it
        when no frontier plan exists (`modes = full`): open_conversations_db(),
        sync_claude_conversations(conn), sync_codex_conversations(conn), close.
        The same three calls exist in v1.113.0, v1.114.0 and main, so one
        driver measures every tree. --passes N repeats the pass until it is
        drained (nothing processed, deferred or pending) or N passes ran;
        --cache-sync runs the accounting ingests (sync_cache,
        sync_codex_cache) before each pass, as the dashboard's main tick does,
        which a tree with the #968 publication barrier needs to drain. The retention prune that follows a
        dashboard pass is throttled maintenance and is not part of it.

        mode `cache-cold` is a cold first Claude accounting sync (#944's
        residual): the clone's cache.db and its sidecars are removed first,
        then open_cache_db() and sync_cache(conn).

        --ownership LOG (#939 spec 2.2) follows the measured stores'
        page ownership per write occurrence (`ownership.py`): a tracker
        seeded from the files before anything opens them observes whenever
        a traced connection starts a statement outside a transaction (the
        first statement after each COMMIT or ROLLBACK), after each phase,
        and at exit, and appends to LOG what `catchup_attrib.py` needs to
        charge every WAL and checkpoint page write to its owner at its
        commit. It reads files only and opens no connection.

The receipt records the tree, the wall clock of each phase, every primitive
IngestStats field per provider, and the migration state before and after.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import pwd
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ownership  # noqa: E402


_V2 = ("user", "sys", "idle_wk", "int_wk", "pageins", "wired", "resident",
       "footprint", "start", "exit", "c_user", "c_sys", "c_idle", "c_int",
       "c_pageins", "c_elapsed", "dread", "dwrite")
_V3 = ("qos_default", "qos_maintenance", "qos_background", "qos_utility",
       "qos_legacy", "qos_user_initiated", "qos_user_interactive",
       "billed_system", "serviced_system")
_V4 = ("logical_writes", "lifetime_max_footprint", "instructions", "cycles",
       "billed_energy", "serviced_energy", "interval_max_footprint",
       "runnable_time")


class _RU(ctypes.Structure):
    _fields_ = ([("uuid", ctypes.c_uint8 * 16)]
                + [(n, ctypes.c_uint64) for n in _V2 + _V3 + _V4])


def self_rusage() -> "dict | None":
    """This process's proc_pid_rusage(RUSAGE_INFO_V4) counters (rusage.py's
    fields): dwrite is ri_diskio_byteswritten, the physical-write measure."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    libc.proc_pid_rusage.restype = ctypes.c_int
    ru = _RU()
    if libc.proc_pid_rusage(os.getpid(), 4, ctypes.byref(ru)) != 0:
        return None
    return {"t": time.time(), "dwrite": ru.dwrite, "dread": ru.dread,
            "logical_writes": ru.logical_writes,
            "footprint_peak": ru.lifetime_max_footprint,
            "cpu_abs": ru.user + ru.sys}


def load_tree(tree: pathlib.Path):
    """Load TREE/bin/cctally as `cctally`, exactly as the product runs."""
    sys.path.insert(0, str(tree / "bin"))
    loader = importlib.machinery.SourceFileLoader(
        "cctally", str(tree / "bin" / "cctally"))
    spec = importlib.util.spec_from_loader("cctally", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = module
    loader.exec_module(module)
    return module


def refuse_live_store() -> pathlib.Path:
    """The data directory must be an explicit clone, never the live store."""
    raw = os.environ.get("CCTALLY_DATA_DIR")
    if not raw:
        raise SystemExit("refusing: set CCTALLY_DATA_DIR to a clone of a store copy")
    data = pathlib.Path(raw).resolve()
    live = (pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
            / ".local" / "share" / "cctally").resolve()
    if data == live or live in data.parents or data in live.parents:
        raise SystemExit(f"refusing: {data} is the live data directory")
    return data


def _versions(data: pathlib.Path) -> dict:
    out = {}
    for name in ("cache.db", "conversations.db", "stats.db"):
        path = data / name
        if not path.exists():
            out[name] = None
            continue
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            out[name] = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    return out


def _meta_manifest(data: pathlib.Path) -> dict:
    """Per store, every `cache_meta` key with its value (truncated): the
    replay, reclaim and backfill markers a version arms or consumes, so two
    versions' runs can be compared for the work they were asked to do."""
    out = {}
    for name in ("cache.db", "conversations.db"):
        path = data / name
        if not path.exists():
            continue
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            out[name] = {k: (v if v is None or len(str(v)) <= 160 else str(v)[:160] + "...")
                         for k, v in conn.execute("SELECT key, value FROM cache_meta")}
        except sqlite3.Error as exc:
            out[name] = {"(error)": str(exc)}
        finally:
            conn.close()
    return out


def _heads(c) -> dict:
    db = sys.modules.get("_cctally_db") or c._load_sibling("_cctally_db")
    return {"cache.db": len(db._CACHE_MIGRATIONS),
            "conversations.db": len(db._CONVERSATIONS_MIGRATIONS)}


def _stats_dict(stats) -> dict:
    if stats is None:
        return None
    return {k: v for k, v in vars(stats).items()
            if isinstance(v, (int, float, str, bool)) or v is None}


def _codex_accounting(c):
    """sync_codex_cache without the quota projection where the tree has the
    hook's `quota_reconcile="defer"` (public #5): the projection writes
    stats.db, which is not the conversation catch-up, and its stats.db open
    would make a store copy at an older stats epoch than the tree start that
    epoch's background rebuild."""
    import inspect
    fn = c.sync_codex_cache
    try:
        src = inspect.getsource(sys.modules["_cctally_cache"]._sync_codex_cache_locked)
    except (KeyError, AttributeError, OSError, TypeError):
        src = ""
    if "quota_reconcile" in src:
        return lambda conn: fn(conn, quota_reconcile="defer")
    return fn


def _cursor_bytes(conn) -> dict:
    """Per provider, the sum of the conversation cursors (`last_byte_offset`)
    and the row count: the input a pass consumed is the after-minus-before
    difference (a truncation reset can only lower it)."""
    out = {}
    for provider, table in (("claude", "conversation_source_files"),
                            ("codex", "codex_conversation_source_files")):
        try:
            n, total = conn.execute(
                f"SELECT count(*), coalesce(sum(last_byte_offset), 0) FROM {table}"
            ).fetchone()
        except sqlite3.Error:
            continue
        out[provider] = {"rows": int(n), "bytes": int(total)}
    return out


def _drained(record, providers) -> bool:
    """A conversation pass with nothing left: no file processed, and on a tree
    with the #968 barrier and container work nothing deferred or pending."""
    for provider in providers:
        st = record.get(provider) or {}
        if st.get("files_processed") or st.get("barrier_files_deferred") \
                or st.get("barrier_retry") or st.get("container_pending"):
            return False
    return True



class TransactionTrace:
    """Write-transaction spans on one connection, from its statement trace.

    A span opens at a top-level ``BEGIN`` (explicit, or the one Python's
    sqlite3 issues before the first DML) and closes at ``COMMIT``/``END`` or a
    whole ``ROLLBACK`` (never ``ROLLBACK TO``). With a deferred BEGIN the span
    starts at the first statement of the transaction, so it bounds how long
    the connection may hold the database's write lock. Spans are attributed
    to the driver phase they close in.
    """

    def __init__(self):
        self.phase = None
        self.open_at = None
        self.spans = {}

    def __call__(self, statement):
        head = statement.lstrip()[:12].upper()
        if head.startswith("BEGIN"):
            if self.open_at is None:
                self.open_at = time.monotonic()
        elif head.startswith(("COMMIT", "END")) or (
                head.startswith("ROLLBACK") and not head.startswith("ROLLBACK TO")):
            if self.open_at is not None:
                self.spans.setdefault(self.phase or "between", []).append(
                    time.monotonic() - self.open_at)
                self.open_at = None

    def summary(self) -> dict:
        out = {}
        for phase, spans in sorted(self.spans.items()):
            ordered = sorted(spans)
            out[phase] = {
                "count": len(ordered),
                "maxS": round(ordered[-1], 4),
                "p95S": round(ordered[min(len(ordered) - 1,
                                          int(0.95 * len(ordered)))], 4),
                "p50S": round(ordered[len(ordered) // 2], 4),
                "totalS": round(sum(ordered), 3),
            }
        return out


def prep(args) -> int:
    data = refuse_live_store()
    c = load_tree(pathlib.Path(args.tree).resolve())
    heads = _heads(c)
    before = _versions(data)
    opens = 0
    while opens < args.max_opens:
        opens += 1
        conn = c.open_conversations_db()  # opens (and migrates) cache.db first
        conn.close()
        now = _versions(data)
        if all(now[k] == heads[k] for k in heads):
            break
    after = _versions(data)
    ok = all(after[k] == heads[k] for k in heads)
    receipt = {"tree": str(args.tree), "heads": heads, "before": before,
               "after": after, "opens": opens, "complete": ok}
    text = json.dumps(receipt, indent=2, sort_keys=True)
    if args.out:
        pathlib.Path(args.out).write_text(text + "\n")
    print(text)
    return 0 if ok else 3


def run(args) -> int:
    data = refuse_live_store()
    tree = pathlib.Path(args.tree).resolve()
    receipt = {"tree": str(tree), "mode": args.mode, "dataDir": str(data),
               "tmpdir": os.environ.get("TMPDIR"),
               "sqliteTmpdir": os.environ.get("SQLITE_TMPDIR"),
               "pid": os.getpid()}
    if args.mode == "cache-cold":
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = data / f"cache.db{suffix}"
            if path.exists():
                path.unlink()
        receipt["cacheRemoved"] = True
    tracker = None
    if args.ownership:
        if args.mode == "conversations":
            names = ["conversations.db"] + (["cache.db"] if args.cache_sync else [])
        else:
            names = ["cache.db"]
        tracker = ownership.Tracker({n: str(data / n) for n in names},
                                    args.ownership)
        receipt["ownership"] = {"log": args.ownership, "stores": names}

    def observe_after(conn) -> None:
        if tracker is not None and not conn.in_transaction:
            tracker.observe()

    def traced(conn, then=None):
        return then if tracker is None else ownership.observer(conn, tracker, then)

    t_load = time.time()
    c = load_tree(tree)
    receipt["heads"] = _heads(c)
    receipt["versionsBefore"] = _versions(data)
    receipt["metaBefore"] = _meta_manifest(data)
    phases = {}
    windows = {}   # provider -> [start, end] epoch seconds (the frame log's clock)
    receipt["rusageStart"] = self_rusage()
    t0 = time.time()
    phases["load"] = round(t0 - t_load, 3)
    receipt["start"] = t0
    if args.mode == "conversations":
        providers = [p for p in args.providers.split(",") if p]
        conn = c.open_conversations_db()
        trace = TransactionTrace()
        conn.set_trace_callback(traced(conn, trace))
        observe_after(conn)
        t1 = time.time()
        phases["open"] = round(t1 - t0, 3)
        passes = []
        receipt["cursorBytesBefore"] = _cursor_bytes(conn)
        try:
            for n in range(1, args.passes + 1):
                record = {"pass": n}
                if args.cache_sync:
                    # The dashboard's main tick runs both accounting ingests
                    # between conversation passes; on a tree with the #968
                    # publication barrier a Codex conversation is published
                    # only after accounting sealed its target.
                    cache = c.open_cache_db()
                    if tracker is not None:
                        cache.set_trace_callback(traced(cache))
                    try:
                        for label, fn in (("cache-claude", c.sync_cache),
                                          ("cache-codex", _codex_accounting(c))):
                            ts = time.time()
                            stats = fn(cache)
                            observe_after(cache)
                            te = time.time()
                            key = f"{label}-{n}"
                            phases[key] = round(te - ts, 3)
                            windows[key] = [ts, te]
                            record[label] = _stats_dict(stats)
                    finally:
                        cache.close()
                for provider in providers:
                    fn = {"claude": c.sync_claude_conversations,
                          "codex": c.sync_codex_conversations}[provider]
                    key = provider if args.passes == 1 else f"{provider}-{n}"
                    trace.phase = key
                    ts = time.time()
                    stats = fn(conn)
                    observe_after(conn)
                    te = time.time()
                    trace.phase = None
                    phases[key] = round(te - ts, 3)
                    windows[key] = [ts, te]
                    record[provider] = _stats_dict(stats)
                    if n == 1:
                        receipt[provider] = record[provider]
                record["rusage"] = self_rusage()
                passes.append(record)
                if _drained(record, providers):
                    break
        finally:
            try:
                receipt["cursorBytesAfter"] = _cursor_bytes(conn)
            except sqlite3.Error:
                pass
            tc = time.time()
            conn.close()
            if tracker is not None:
                tracker.observe()
            phases["close"] = round(time.time() - tc, 3)
        receipt["transactions"] = trace.summary()
        receipt["passes"] = passes
        receipt["drained"] = bool(passes) and _drained(passes[-1], providers)
    else:
        conn = c.open_cache_db()
        trace = TransactionTrace()
        conn.set_trace_callback(traced(conn, trace))
        observe_after(conn)
        t1 = time.time()
        phases["open"] = round(t1 - t0, 3)
        try:
            trace.phase = "sync_cache"
            ts = time.time()
            stats = c.sync_cache(conn)
            observe_after(conn)
            te = time.time()
            phases["sync_cache"] = round(te - ts, 3)
            windows["sync_cache"] = [ts, te]
            receipt["claude"] = _stats_dict(stats)
            trace.phase = None
            receipt["transactions"] = trace.summary()
        finally:
            tc = time.time()
            conn.close()
            if tracker is not None:
                tracker.observe()
            phases["close"] = round(time.time() - tc, 3)
    if tracker is not None:
        receipt["ownership"]["observations"] = tracker.observations
    receipt["end"] = time.time()
    receipt["rusageEnd"] = self_rusage()
    receipt["phases"] = phases
    receipt["windows"] = windows
    receipt["versionsAfter"] = _versions(data)
    receipt["metaAfter"] = _meta_manifest(data)
    pathlib.Path(args.out).write_text(json.dumps(receipt, indent=2,
                                                 sort_keys=True) + "\n")
    print(json.dumps(phases, sort_keys=True))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--tree", required=True)
    p.add_argument("--out")
    p.add_argument("--max-opens", type=int, default=60)
    r = sub.add_parser("run")
    r.add_argument("--tree", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--mode", choices=("conversations", "cache-cold"),
                   default="conversations")
    r.add_argument("--providers", default="claude,codex")
    r.add_argument("--passes", type=int, default=1,
                   help="most conversation passes; stop early once drained")
    r.add_argument("--cache-sync", action="store_true",
                   help="run sync_cache and sync_codex_cache before each pass")
    r.add_argument("--ownership", metavar="LOG",
                   help="log per-occurrence page ownership (ownership.py)")
    args = ap.parse_args(argv)
    return prep(args) if args.cmd == "prep" else run(args)


if __name__ == "__main__":
    sys.exit(main())
