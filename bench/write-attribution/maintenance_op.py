"""#901 spec §6.3 C-op / R-cov: run product maintenance operations exactly as
the dashboard visit does, in ONE process under the write interposer.

usage:
  maintenance_op.py --tree TREE --db DB --out OUT.json --start-utc ISO \
      (--delete PROVIDER:KEY [--delete PROVIDER:KEY ...] | --reclaim-chunks N --pages P) \
      [--final-checkpoint {self,none}] [--counter-unavailable] [--fallback-threshold N] \
      [--pin] [--control-unbounded]
  maintenance_op.py --tree TREE --db DB --normalize-only

The baseline `wal_checkpoint(TRUNCATE)` is NOT done here: the runner does it in
a separate measured process, so this process's interposer totals hold only the
operations, their reservation writes, their closes and (with `self`) the forced
final checkpoint. With `none`, the runner performs the final checkpoint in a
second process (the foreign copier). Group selection is also done elsewhere —
a selection query could spill a temp file and pollute this process's totals.
`--normalize-only` writes the fixed-size #780 record (spec §5.4 "State
write") in the runner's baseline process, so that uncharged write is never
inside a measured run.

The `--start-utc` clock advances between operations by the minutes the durable
debt needs, so every operation starts from a non-negative balance exactly as a
paced visit would; nothing bypasses the gate.

Every operation records its wall-clock transaction boundaries (`began`,
`ended`; the interposer's frame log uses the same clock), the geometry this
tool observed independently before it (page size and reserved bytes from the
closed file header, page count from a read-only connection), the candidate's
reservation coefficients and the run's identities. A reclaim chunk also
records its full plan (the ID set, UNID, steps and reservation), captured from
the product's own plan request.

`--pin` (the classification runs, spec §6.3 R-cov): a reader pins the WAL
before the first operation, so no frame is copied or reset until the end;
this tool APFS-clones the start-of-run database (`db.start`) and, after the
last operation and before the reader releases, the WAL (`wal.pinned`), and
records each operation's WAL byte range. The analyzer reconstructs every
chunk's starting snapshot from those two files and recomputes its plan
independently. `--control-unbounded` (R-cov (vi)'s validity control) deletes
the groups with the shipped statements on a connection with an unbounded
cache and an in-memory temp store, as the fallback's working-set witness.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pathlib
import shutil
import sqlite3
import struct
import subprocess
import sys
import time

UTC = dt.timezone.utc
HERE = pathlib.Path(__file__).resolve().parent


def footprint_peak_bytes():
    """`ri_lifetime_max_phys_footprint` (RUSAGE_INFO_V4) of this process."""
    if sys.platform != "darwin":
        return None
    import ctypes

    names = [f"f{i}" for i in range(27)] + ["logical_writes",
                                             "lifetime_max_footprint"] + [
        f"g{i}" for i in range(6)]

    class V4(ctypes.Structure):
        _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
            (n, ctypes.c_uint64) for n in names]

    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    lib.proc_pid_rusage.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    lib.proc_pid_rusage.restype = ctypes.c_int
    info = V4()
    if lib.proc_pid_rusage(os.getpid(), 4, ctypes.byref(info)) != 0:
        return None
    return int(info.lifetime_max_footprint)


def ledger_total(retention, db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        record = retention.read_reclaim_pending(conn)
    finally:
        conn.close()
    return sum(b["charged"] for b in (record or {}).get("ledger", [])), record


def next_start(retention, db, now):
    """Advance past any debt so the next operation starts funded.

    The durable `as_of` never moves backwards (`PacingState.charge` keeps
    `max(now, as_of)`), so a record stamped ahead of this run's clock accrues
    nothing until the clock passes it: the advance starts from the later of
    the two."""
    _total, record = ledger_total(retention, db)
    state = retention.PacingState.from_record(record, now)
    if state.available(now) >= 0:
        return now
    base = max(now, state.as_of)
    available = state.available(base)
    if available >= 0:
        return base
    minutes = math.ceil(-available / (4 * 1024 * 1024)) + 1
    return base + dt.timedelta(minutes=minutes)


def read_header(db) -> dict:
    """Page size and reserved bytes from the database header. Called once,
    before this process opens any connection: closing a raw descriptor of
    the file later would drop the POSIX locks its connections hold. Neither
    value can change while the file exists."""
    with open(db, "rb") as fh:
        header = fh.read(100)
    page_size = struct.unpack(">H", header[16:18])[0]
    page_size = 65536 if page_size == 1 else page_size
    return {"pageSize": page_size, "usableSize": page_size - header[20]}


def observed_geometry(db, header: dict) -> dict:
    """The independent pre-operation geometry: the header's sizes and the
    page count from a read-only connection."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    finally:
        conn.close()
    return {**header, "pageCount": page_count}


def coefficients(retention) -> dict:
    return {"fixed": retention.DELETION_FIXED_BYTES,
            "perRow": retention.DELETION_PER_ROW_BYTES,
            "perRowFallback": retention.DELETION_PER_ROW_FALLBACK_BYTES,
            "reclaimFixed": retention.RECLAIM_FIXED_BYTES,
            "reservationVersion": retention.RESERVATION_VERSION}


def identities(tree, db) -> dict:
    rev = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    conn = sqlite3.connect(":memory:")
    try:
        source = conn.execute("SELECT sqlite_source_id()").fetchone()[0]
    finally:
        conn.close()
    st = os.stat(db)
    return {"tree": str(tree), "gitRev": rev or None,
            "sqliteVersion": sqlite3.sqlite_version, "sqliteSourceId": source,
            "db": str(db), "dbDevice": st.st_dev, "dbInode": st.st_ino}


def wal_size(db) -> int:
    try:
        return os.stat(f"{db}-wal").st_size
    except FileNotFoundError:
        return 0


def apfs_clone(src, dst) -> None:
    if subprocess.run(["cp", "-c", str(src), str(dst)]).returncode != 0:
        shutil.copyfile(src, dst)


def run_baseline(retention, args) -> int:
    """The C-op RED proof: what 56e66f07a's visit runs for ONE group — its
    per-group DELETE statements in one `BEGIN IMMEDIATE` transaction on a
    connection with SQLite's default cache and temp store."""
    db = args.db.resolve()
    ops = []
    for spec in args.delete:
        provider, key = spec.split(":", 1)
        retention._prunable_groups = (
            lambda conn, table, key_col, cutoff, _p=provider, _k=key:
            [_k] if (table == "conversation_messages") == (_p == "claude")
            else [])
        retention._prunable_null_identity_paths = lambda *a, **k: []
        began = time.time()
        conn = sqlite3.connect(db)
        if args.control_unbounded:
            conn.execute("PRAGMA cache_size = -16777216")
            conn.execute("PRAGMA temp_store = MEMORY")
        try:
            table = ("conversation_messages" if provider == "claude"
                     else "codex_conversation_events")
            column = "session_id" if provider == "claude" else "conversation_key"
            rows = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
                (key,)).fetchone()[0]
            conn.execute("BEGIN IMMEDIATE")
            prune = (retention._prune_claude if provider == "claude"
                     else retention._prune_codex)
            prune(conn, "9999-12-31T00:00:00Z")
            conn.commit()
        finally:
            conn.close()
        ops.append({"kind": "delete", "provider": provider, "rows": rows,
                    "pages_reclaimed": 0, "charged_bytes": 0,
                    "outcome": "ok",
                    "mode": "control" if args.control_unbounded else "shipped",
                    "began": began, "ended": time.time()})
    if args.final_checkpoint == "self":
        conn = sqlite3.connect(db)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
    args.out.write_text(json.dumps({
        "pid": os.getpid(), "tree": str(args.tree.resolve()), "db": str(db),
        # Amendment 19 HR-9: a pair verifies the baseline's tree revision.
        "identities": identities(args.tree.resolve(), db),
        "ops": ops, "chargedBytes": 0, "rows": sum(o["rows"] for o in ops),
        "pages": 0, "baseline": not args.control_unbounded,
        "control": bool(args.control_unbounded),
        "footprintPeakBytes": footprint_peak_bytes()},
        indent=2, sort_keys=True) + "\n")
    return 0


def main(argv=None) -> int:
    sys.path.insert(0, str(HERE))
    import frozen_roots
    frozen_roots.require_namespace("maintenance_op.py")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", required=True, type=pathlib.Path)
    parser.add_argument("--db", required=True, type=pathlib.Path)
    parser.add_argument("--out", type=pathlib.Path)
    parser.add_argument("--start-utc")
    parser.add_argument("--delete", action="append", default=[])
    parser.add_argument("--reclaim-chunks", type=int, default=0)
    parser.add_argument("--pages", type=int, default=16)
    parser.add_argument("--final-checkpoint", choices=("self", "none"),
                        default="self")
    parser.add_argument("--counter-unavailable", action="store_true")
    parser.add_argument("--fallback-threshold", type=int)
    parser.add_argument("--pin", action="store_true")
    parser.add_argument("--normalize-only", action="store_true")
    parser.add_argument(
        "--baseline", action="store_true",
        help="56e66f07a: the shipped per-group statements on a default "
             "connection (that tree has no operation function)")
    parser.add_argument("--control-unbounded", action="store_true",
                        help="R-cov (vi): the shipped statements on an "
                             "unbounded-cache, in-memory-temp connection")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.tree.resolve() / "bin"))
    import _lib_conversation_retention as retention

    if args.normalize_only:
        if not hasattr(retention, "normalize_reclaim_record"):
            print(json.dumps({"normalized": None,
                              "reason": "the tree has no fixed-size record"}))
            return 0
        conn = sqlite3.connect(args.db)
        try:
            wrote = retention.normalize_reclaim_record(
                conn, dt.datetime.now(UTC))
        finally:
            conn.close()
        print(json.dumps({"normalized": wrote}))
        return 0
    if args.out is None or args.start_utc is None:
        parser.error("--out and --start-utc are required")
    if bool(args.delete) == bool(args.reclaim_chunks):
        parser.error("give --delete or --reclaim-chunks, not both")
    if args.baseline or args.control_unbounded:
        return run_baseline(retention, args)
    import _lib_write_io as wio

    if args.counter_unavailable:
        wio.reset_for_tests(counter=wio.ProcessWriteCounter(platform="unsupported"))
    if args.fallback_threshold is not None:
        retention.DELETION_FALLBACK_ROW_THRESHOLD = args.fallback_threshold
    db = args.db.resolve()
    header = read_header(db)
    plans = []
    if hasattr(retention, "_request_plan"):
        real_plan = retention._request_plan

        def capture(*a, **k):
            plan = real_plan(*a, **k)
            plans.append(plan)
            return plan
        retention._request_plan = capture
    run_identities = identities(args.tree.resolve(), db)
    run_coefficients = coefficients(retention)
    charged_before, _ = ledger_total(retention, db)
    now = dt.datetime.fromisoformat(args.start_utc.replace("Z", "+00:00"))
    reader = None
    if args.pin:
        apfs_clone(db, f"{db}.start")
        reader = sqlite3.connect(db)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM cache_meta").fetchone()
    ops = []
    cache_bytes = None
    if args.delete:
        for spec in args.delete:
            provider, key = spec.split(":", 1)
            now = next_start(retention, db, now)
            geometry = observed_geometry(db, header)
            wal_from = wal_size(db)
            began = time.time()
            conn = retention.open_deletion_connection(db)
            if cache_bytes is None:
                # R-cov (vi)'s validity: the production cap, as the product
                # opened it (a negative cache_size is KiB).
                size = int(conn.execute("PRAGMA cache_size").fetchone()[0])
                cache_bytes = -size * 1024 if size < 0 else None
            try:
                result = retention.delete_group(
                    conn, retention.ExpiredGroup(provider, False, key),
                    now_utc=now, cutoff_iso="9999-12-31T00:00:00Z")
            finally:
                conn.close()
            ended = time.time()
            ops.append({"kind": "delete", "provider": provider, "key": key,
                        "began": began, "ended": ended, "geometry": geometry,
                        "walFrom": wal_from, "walTo": wal_size(db),
                        **result.as_phase_payload()})
    else:
        for _ in range(args.reclaim_chunks):
            now = next_start(retention, db, now)
            wal_from = wal_size(db)
            began = time.time()
            before_plans = len(plans)
            geometry = observed_geometry(db, header)
            conn = retention.open_reclaim_connection(db)
            try:
                # The record was normalized in the baseline process
                # (`--normalize-only`); an unnormalized one refuses the chunk
                # and the run is incomplete, never silently measured.
                result = retention.reclaim_chunk(conn, now_utc=now,
                                                 pages=args.pages)
            finally:
                conn.close()
            plan = plans[before_plans] if len(plans) > before_plans else None
            ops.append({"kind": "reclaim", "began": began, "ended": time.time(),
                        "geometry": geometry,
                        "walFrom": wal_from, "walTo": wal_size(db),
                        "plan": None if plan is None else plan.as_wire(),
                        **result.as_phase_payload()})
            if result.outcome != "ok":
                break
    if reader is not None:
        apfs_clone(f"{db}-wal", f"{db}.wal.pinned")
        reader.rollback()
        reader.close()
    if args.final_checkpoint == "self":
        conn = sqlite3.connect(db)
        try:
            final = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
    else:
        final = None
    charged_after, record = ledger_total(retention, db)
    report = {
        "pid": os.getpid(), "tree": str(args.tree.resolve()),
        "db": str(db), "ops": ops, "identities": run_identities,
        "coefficients": run_coefficients,
        "chargedBytes": charged_after - charged_before,
        "opSeq": (record or {}).get("op_seq"),
        "rows": sum(op["rows"] for op in ops),
        "pages": sum(op["pages_reclaimed"] for op in ops),
        "finalCheckpoint": None if final is None else list(final),
        "counterUnavailable": bool(args.counter_unavailable),
        "pinned": bool(args.pin),
        "deletionCacheBytes": cache_bytes,
        "footprintPeakBytes": footprint_peak_bytes(),
    }
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    bad = [op for op in ops if op["outcome"] != "ok"]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
