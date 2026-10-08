"""#901 spec §6.3 "Frozen namespace" / "Stores and method" (Q16, `dc13` L1):
the one launch contract of a frozen run's process family.

usage (the runners' `wa_exec` in _prep.sh builds this line):
  sandbox-exec -f FREEZE/frozen.sb PYTHON frozen_launch.py --freeze FREEZE \
      --rootmap ROOTMAP_DYLIB --receipts DIR [--libs LIB[:LIB...]] -- TARGET ARGV...

`sandbox-exec` strips DYLD_INSERT_LIBRARIES from the process it starts, so the
launcher - already inside the inherited sandbox that denies the live roots -
composes the libraries itself: DYLD_INSERT_LIBRARIES = ROOTMAP_DYLIB followed
by --libs (the write interposer, when the caller had set it), with
ROOTMAP_MANIFEST = FREEZE/rootmap.tsv and ROOTMAP_RECEIPTS = DIR. Nothing else
in the environment changes (TMPDIR stays exactly as given). It refuses unless
the sandbox really denies every live root, closes every inherited descriptor
open on a live root or metadata file, appends {pid, t, argv} to
DIR/launches.jsonl (the target keeps this pid across execve, so
`frozen_roots.py family` requires an activation receipt for it), and
`os.execve`s the target. Exit 2 on a refusal.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time

F_GETPATH = 50


def _sandbox_denies(path: str) -> bool:
    lib = ctypes.CDLL(None)
    check = lib.sandbox_check
    check.restype = ctypes.c_int
    try:
        no_report = ctypes.c_int.in_dll(lib, "SANDBOX_CHECK_NO_REPORT").value
    except ValueError:
        no_report = 0x40000000
    # sandbox_check(pid, operation, type, ...): the path is variadic, so call
    # it through a prototype whose fourth argument is passed on the stack the
    # way arm64 variadics are (ctypes has no variadic marker; a fixed
    # prototype with eight dummy register arguments pushes the path).
    proto = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                             ctypes.c_int, *([ctypes.c_long] * 5),
                             ctypes.c_char_p)
    fn = proto(ctypes.cast(check, ctypes.c_void_p).value)
    return fn(os.getpid(), b"file-read-data", 1 | no_report,
              0, 0, 0, 0, 0, path.encode()) > 0


def _live_paths(freeze: str) -> list:
    """Every live path the freeze protects: each logical root (`R`, an
    out-of-root link target included) and each non-traversed directory
    link's live target (`X`, Amendment 10)."""
    out = []
    with open(os.path.join(freeze, "rootmap.tsv"), encoding="utf-8") as fh:
        for line in fh:
            fields = line.rstrip("\n").split("\t")
            if fields[0] in ("R", "X"):
                out.append(fields[1])
    return out


def _close_live_descriptors(live: list) -> list:
    closed = []
    import fcntl
    try:
        limit = min(os.sysconf("SC_OPEN_MAX"), 65536)
    except (ValueError, OSError):
        limit = 4096
    for fd in range(3, limit):
        try:
            buf = fcntl.fcntl(fd, F_GETPATH, b"\0" * 1024)
        except OSError:
            continue
        path = buf.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
        if any(path == p or path.startswith(p.rstrip("/") + "/") for p in live):
            os.close(fd)
            closed.append(fd)
    return closed


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("frozen_launch: usage: ... -- TARGET ARGV...", file=sys.stderr)
        return 2
    cut = argv.index("--")
    opts, target = argv[:cut], argv[cut + 1:]
    import argparse
    parser = argparse.ArgumentParser(prog="frozen_launch.py")
    parser.add_argument("--freeze", required=True)
    parser.add_argument("--rootmap", required=True)
    parser.add_argument("--receipts", required=True)
    parser.add_argument("--libs", default="")
    args = parser.parse_args(opts)
    if not target:
        print("frozen_launch: no target", file=sys.stderr)
        return 2
    freeze = os.path.realpath(args.freeze)
    live = _live_paths(freeze)
    undenied = [p for p in live if not _sandbox_denies(p)]
    if undenied:
        print(f"frozen_launch: refusing: the sandbox does not deny {undenied} "
              "(start through sandbox-exec -f FREEZE/frozen.sb)", file=sys.stderr)
        return 2
    closed = _close_live_descriptors(live)
    os.makedirs(args.receipts, exist_ok=True)
    libs = [os.path.realpath(args.rootmap)] + [
        lib for lib in args.libs.split(":") if lib and "rootmap" not in
        os.path.basename(lib)]
    env = dict(os.environ)
    env.update({"DYLD_INSERT_LIBRARIES": ":".join(libs),
                "ROOTMAP_MANIFEST": os.path.join(freeze, "rootmap.tsv"),
                "ROOTMAP_RECEIPTS": os.path.realpath(args.receipts),
                "WRITE_ATTRIBUTION_INPUTS": f"frozen:{freeze}"})
    with open(os.path.join(args.receipts, "launches.jsonl"), "a") as fh:
        fh.write(json.dumps({"pid": os.getpid(), "t": time.time(),
                             "argv": target, "closedFds": closed,
                             "libs": libs}) + "\n")
    os.execve(target[0], target, env)
    return 2  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
