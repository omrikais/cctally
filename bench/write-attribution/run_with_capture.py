"""#901 spec §6.3 C: run `TREE/bin/cctally <args>` in-process with every
maintenance operation the product reports captured COMPLETELY.

usage: run_with_capture.py TREE -- <cctally args>
  env: OPS_CAPTURE=<jsonl path>   one JSON line per reported operation

The dashboard publishes maintenance operations into a 16-record ring that a
poll every 15 s can miss; C's receipts are bound to every committed
operation instead (spec §6.3 "Deletion receipts": completeness is checked
against the committed operation ids and ledger deltas, never against a polled
telemetry ring). This launcher wraps `_lib_tick_stats.record_maintenance_phase`
before the product imports it, appends each payload with the wall-clock time
of the report (the operation's end; its start is that minus `duration_s`)
and the reporting process's pid, and then runs the product exactly as
`bin/cctally` does. Harness evidence only: the product's own outputs are
unchanged.
"""
from __future__ import annotations

import json
import os
import runpy
import sys
import threading
import time


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[1] != "--":
        print(__doc__, file=sys.stderr)
        return 2
    tree, args = argv[0], argv[2:]
    capture = os.environ["OPS_CAPTURE"]
    bin_dir = os.path.join(os.path.abspath(tree), "bin")
    sys.path.insert(0, bin_dir)
    import _lib_tick_stats

    real = _lib_tick_stats.record_maintenance_phase
    lock = threading.Lock()

    def record(phase, payload=None):
        try:
            line = json.dumps({"reportedAt": time.time(), "pid": os.getpid(),
                               "phase": phase, **(payload or {})},
                              default=str)
            with lock, open(capture, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception as exc:  # noqa: BLE001 — never break the product
            print(f"run_with_capture: {exc}", file=sys.stderr)
        return real(phase, payload)

    _lib_tick_stats.record_maintenance_phase = record
    sys.argv = [os.path.join(bin_dir, "cctally")] + args
    runpy.run_path(os.path.join(bin_dir, "cctally"), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
