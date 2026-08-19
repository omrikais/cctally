#!/usr/bin/env python3
"""Sample one Gate 0.25 lane's resource use from inside its container (#621).

Started only by acceptance mode. It copies the cgroup files out verbatim and
records ``/tmp`` usage, writing one JSON object per line and flushing each one,
so a lane that is killed still leaves every sample it took. Interpretation
belongs to ``bin/cctally-test-linux-matrix-acceptance.py``, which is
host-side and
testable; nothing here decides anything.

Timestamps are wall clock rather than monotonic on purpose: three containers
share the host clock, and aligning three lanes on a common timebase is the whole
point of the simultaneous-memory threshold.

Usage: cctally-test-linux-matrix-sampler.py <output.jsonl> [interval-seconds]
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time


CGROUP_ROOT = "/sys/fs/cgroup"
# cgroup v2 first, then the v1 equivalents. Reading a name that does not exist
# on this host records null for it, which the analyzer treats as absent rather
# than as zero.
CGROUP_FILES = (
    "memory.current",
    "memory.peak",
    "memory.stat",
    "memory.events",
    "memory.usage_in_bytes",
    "memory.max_usage_in_bytes",
    "memory.oom_control",
)
DEFAULT_INTERVAL_SECONDS = 0.25


def _read(name: str) -> str | None:
    for candidate in (
        os.path.join(CGROUP_ROOT, name),
        os.path.join(CGROUP_ROOT, "memory", name),
    ):
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            continue
    return None


def _tmp_used_bytes(path: str = "/tmp") -> int | None:
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    return (stats.f_blocks - stats.f_bfree) * stats.f_frsize


def _container_fs_available_bytes() -> int | None:
    """Free space on the filesystem the suite's scratch actually lands on.

    `/tmp` is a 4 GiB tmpfs the suite barely touches; pytest is pointed at
    ``TMPDIR`` on the container filesystem, which every concurrent lane shares
    with the image store. That is the resource observed to bind, so it is
    recorded rather than inferred.
    """
    try:
        stats = os.statvfs(os.environ.get("TMPDIR") or "/")
    except OSError:
        return None
    return stats.f_bavail * stats.f_frsize


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: sampler.py <output.jsonl> [interval-seconds]", file=sys.stderr)
        return 2
    destination = argv[0]
    interval = float(argv[1]) if len(argv) > 1 else DEFAULT_INTERVAL_SECONDS

    running = {"go": True}

    def _stop(_signum, _frame):
        running["go"] = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    with open(destination, "w", encoding="utf-8") as handle:
        while running["go"]:
            record = {
                "t": time.time(),
                "cgroup": {name: _read(name) for name in CGROUP_FILES},
                "tmpUsedBytes": _tmp_used_bytes(),
                "containerFsAvailableBytes": _container_fs_available_bytes(),
            }
            handle.write(json.dumps(record, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            time.sleep(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
