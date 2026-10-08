"""#901 spec §6.3 "Terminal drain" (Q13, 901-SR-026/-029/-030, `dc10` H3).

usage: terminal_drain.py --data CLONE_DATA_DIR --terminal RUN/terminal.json
                         [--stores conversations.db cache.db]

W9 defers checkpoint copies, so copies of frames committed near a window's end
can land after it. `run-workload.sh` stops the dashboard with SIGKILL (so no
close-time checkpoint runs), then starts this copier under the interposer
(`WTRACE_OUT=RUN/drain`) with no other process on the clone. Per store, in this
order:

1. Before any SQLite connection opens, it reads the stopped dashboard's `-shm`
   wal-index with the validation the reclaim planner's helper applies
   (`_lib_reclaim_planner.parse_wal_index_header`: both 48-byte header copies
   identical, initialized, the supported version, a valid checksum) and the
   `-wal` header's salt, which must equal the wal-index's (one WAL generation).
   It records the committed frontier `mxFrame` and the checkpointed frontier
   `nBackfill` (the first word of the checkpoint info after the two copies). An
   absent or empty `-wal` is a valid empty backlog; anything else that cannot
   be read is recorded as invalid, never estimated.
2. It runs `PRAGMA wal_checkpoint(TRUNCATE)` and records the row, whether
   `busy = 0`, and the `-wal` length right after it (empty is required).
3. Last, it samples its own kernel write counter (`proc_pid_rusage`,
   RUSAGE_INFO_V4 `ri_diskio_bytes_written`) and adds everything to the run's
   `terminal.json` under "drain", beside the runner's `t`/`alive` record.

The analyzer (`workload.py drain-verdict`) judges bounded deferral from step
1's readings and the window's interposer record, never from this copier's own
output (a successful TRUNCATE reports zero counts). Imports only the bench
checkout's own `bin/_lib_reclaim_planner.py`, so it serves both trees.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import pathlib
import sqlite3
import struct
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
BIN = HERE.parent.parent / "bin"
STORES = ("conversations.db", "cache.db")
#: The 32-byte WAL header: magic, version, page size, checkpoint sequence,
#: salt-1, salt-2, checksum-1, checksum-2 (big-endian).
WAL_HEADER_BYTES = 32
#: The wal-index's two 48-byte header copies; the checkpoint info follows.
WAL_INDEX_HEADERS = 96


def _planner():
    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    import _lib_reclaim_planner

    return _lib_reclaim_planner


def read_wal_index(db_path: pathlib.Path) -> dict:
    """Step 1 for one store, from the files alone (no SQLite connection)."""
    wal = pathlib.Path(f"{db_path}-wal")
    shm = pathlib.Path(f"{db_path}-shm")
    wal_size = wal.stat().st_size if wal.exists() else 0
    if wal_size == 0:
        return {"valid": True, "walAbsent": True, "walBytes": 0,
                "mxFrame": 0, "nBackfill": 0, "pageSize": None}
    if not shm.exists():
        return {"valid": False, "reason": "no -shm beside a non-empty -wal",
                "walBytes": wal_size}
    planner = _planner()
    raw = shm.read_bytes()[:WAL_INDEX_HEADERS + 4]
    try:
        header = planner.parse_wal_index_header(raw[:WAL_INDEX_HEADERS])
    except planner.ReclaimPlanRefused as exc:
        return {"valid": False, "reason": f"wal-index: {exc}",
                "walBytes": wal_size}
    if len(raw) < WAL_INDEX_HEADERS + 4:
        return {"valid": False, "reason": "short -shm checkpoint info",
                "walBytes": wal_size}
    order = "<" if sys.byteorder == "little" else ">"
    n_backfill = struct.unpack(order + "I", raw[WAL_INDEX_HEADERS:
                                                WAL_INDEX_HEADERS + 4])[0]
    with open(wal, "rb") as fh:
        wal_header = fh.read(WAL_HEADER_BYTES)
    if len(wal_header) < WAL_HEADER_BYTES:
        return {"valid": False, "reason": "short -wal header",
                "walBytes": wal_size}
    if header.max_frame and wal_header[16:24] != header.salt:
        return {"valid": False, "walBytes": wal_size,
                "reason": "the -wal salt differs from the wal-index's"}
    if n_backfill > header.max_frame:
        return {"valid": False, "walBytes": wal_size,
                "reason": f"nBackfill {n_backfill} > mxFrame {header.max_frame}"}
    page_size = header.page_size or struct.unpack(">I", wal_header[8:12])[0]
    return {"valid": True, "walAbsent": False, "walBytes": wal_size,
            "mxFrame": int(header.max_frame), "nBackfill": int(n_backfill),
            "pageSize": int(page_size)}


def truncate(db_path: pathlib.Path) -> dict:
    """Step 2: the copier's own TRUNCATE checkpoint and the empty WAL."""
    wal = pathlib.Path(f"{db_path}-wal")
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        after = wal.stat().st_size if wal.exists() else 0
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()
    busy = int(row[0]) if row else 1
    return {"ok": busy == 0 and after == 0, "row": list(row) if row else None,
            "busy": busy, "walBytesAfter": after}


class _RusageInfoV4(ctypes.Structure):
    _fields_ = ([("uuid", ctypes.c_uint8 * 16)]
                + [(name, ctypes.c_uint64) for name in (
                    "user", "sys", "idle_wk", "int_wk", "pageins", "wired",
                    "resident", "footprint", "start", "exit", "c_user", "c_sys",
                    "c_idle", "c_int", "c_pageins", "c_elapsed", "dread",
                    "dwrite", "qos_default", "qos_maintenance",
                    "qos_background", "qos_utility", "qos_legacy",
                    "qos_user_initiated", "qos_user_interactive",
                    "billed_system", "serviced_system", "logical_writes",
                    "lifetime_max_footprint", "instructions", "cycles",
                    "billed_energy", "serviced_energy",
                    "interval_max_footprint", "runnable_time")])


def self_rusage() -> dict:
    """Step 3: this process's kernel write counter (macOS proc_pid_rusage)."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        libc.proc_pid_rusage.argtypes = (ctypes.c_int, ctypes.c_int,
                                         ctypes.c_void_p)
        libc.proc_pid_rusage.restype = ctypes.c_int
    except OSError as exc:
        return {"rc": None, "error": str(exc), "dwrite": None}
    info = _RusageInfoV4()
    rc = libc.proc_pid_rusage(os.getpid(), 4, ctypes.byref(info))
    return {"t": time.time(), "pid": os.getpid(), "rc": rc,
            "dwrite": int(info.dwrite) if rc == 0 else None,
            "logicalWrites": int(info.logical_writes) if rc == 0 else None}


def drain(data: pathlib.Path, stores=STORES, *, rusage=self_rusage) -> dict:
    """Every step for every store; the readings precede the first open."""
    readings = {name: read_wal_index(data / name) for name in stores
                if (data / name).exists()}
    checkpoints = {name: truncate(data / name) for name in readings}
    # Amendment 19 HR-13: a store this drain was asked for and did not find
    # is recorded as missing, never silently left out (the verdict refuses).
    out = {name: {"missing": True} for name in stores if name not in readings}
    out.update({name: {"walIndex": readings[name],
                       "checkpoint": checkpoints[name]} for name in readings})
    return {"pid": os.getpid(), "startedAt": time.time(), "stores": out,
            "rusage": rusage()}


def main(argv=None) -> int:
    sys.path.insert(0, str(HERE))
    import frozen_roots
    frozen_roots.require_namespace("terminal_drain.py")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--terminal", required=True)
    parser.add_argument("--stores", nargs="+", default=list(STORES))
    args = parser.parse_args(argv)
    data = pathlib.Path(args.data)
    result = drain(data, tuple(args.stores))
    terminal = pathlib.Path(args.terminal)
    record = {}
    if terminal.exists():
        try:
            record = json.loads(terminal.read_text())
        except ValueError:
            record = {}
    record["drain"] = result
    terminal.write_text(json.dumps(record, sort_keys=True) + "\n")
    ok = all(not store.get("missing") and store["walIndex"].get("valid")
             and store["checkpoint"].get("ok")
             for store in result["stores"].values())
    print(json.dumps({"ok": ok, "stores": sorted(result["stores"])}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
