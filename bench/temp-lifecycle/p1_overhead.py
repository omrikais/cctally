#!/usr/bin/env python3
"""P1 (#963 spec 9.1, J9): the test-temp kernel's cost on a production-shaped tree.

Builds, under ``--root`` (which must lie inside TMPDIR), a basetemp-scale tree
of ``--entries`` empty files in directories of 1,000, plus the freeze-capture
population: ``--freeze-trees`` sealed trees, each three nested 0o500
directories holding ``--freeze-files`` files, two of which carry UF_IMMUTABLE on
Darwin. It then times, separately, the guard's inspection (``inspect``), the
release (``release``) and the generation removal (``remove``) over the whole
tree, and prints one JSON line. The sum must fit the 289 s headroom of spec
section 7; the exit status says whether it did (0) or not (1).
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin"))
import _lib_test_temp as T  # noqa: E402

HEADROOM_SECONDS = 289.0


def build(root, entries, freeze_trees, freeze_files):
    os.makedirs(root)
    made = 0
    d = 0
    while made < entries:
        sub = os.path.join(root, "bt", "d%05d" % d)
        os.makedirs(sub)
        for i in range(min(1000, entries - made)):
            os.close(os.open(os.path.join(sub, "f%04d" % i), os.O_CREAT | os.O_WRONLY, 0o600))
        made += min(1000, entries - made)
        d += 1
    flagged = 0
    for t in range(freeze_trees):
        deep = os.path.join(root, "freeze", "t%03d" % t, "a", "b", "c")
        os.makedirs(deep)
        for i in range(freeze_files):
            path = os.path.join(deep, "f%02d" % i)
            with open(path, "w") as fh:
                fh.write("x")
            if i < 2 and hasattr(os, "chflags"):
                os.chflags(path, stat.UF_IMMUTABLE)
                flagged += 1
        for level in ("a/b/c", "a/b", "a"):
            os.chmod(os.path.join(root, "freeze", "t%03d" % t, level), 0o500)
    return made, flagged


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True)
    ap.add_argument("--entries", type=int, default=880000)
    ap.add_argument("--freeze-trees", type=int, default=45)
    ap.add_argument("--freeze-files", type=int, default=20)
    args = ap.parse_args(argv)
    tmp = os.path.realpath(os.environ.get("TMPDIR", "/tmp"))
    root = os.path.realpath(args.root)
    if not root.startswith(tmp + os.sep) or os.path.lexists(root):
        sys.stderr.write("p1_overhead: --root must be a new path inside TMPDIR (%s)\n" % tmp)
        return 2
    # From here the flagged tree exists: every exit removes it through the
    # kernel, so an early refusal never leaves an undeletable fixture behind.
    try:
        t0 = time.perf_counter()
        made, flagged = build(root, args.entries, args.freeze_trees, args.freeze_files)
        build_s = time.perf_counter() - t0
        clocks = {}
        for name, op in (("inspect", T.inspect), ("release", T.release), ("remove", T.remove)):
            t0 = time.perf_counter()
            res = op(root)
            clocks[name] = time.perf_counter() - t0
            if res.refusals:
                sys.stderr.write("p1_overhead: %s refused: %r\n" % (name, res.refusals[:5]))
                return 2
        total = clocks["inspect"] + clocks["release"] + clocks["remove"]
        record = {
            "p1": "963", "entries": made, "freezeTrees": args.freeze_trees,
            "flaggedFiles": flagged, "buildSeconds": round(build_s, 2),
            "inspectSeconds": round(clocks["inspect"], 2),
            "releaseSeconds": round(clocks["release"], 2),
            "removeSeconds": round(clocks["remove"], 2),
            "sumSeconds": round(total, 2), "headroomSeconds": HEADROOM_SECONDS,
            "removeEntriesPerSecond": round(made / clocks["remove"]) if clocks["remove"] else None,
            "rootGone": not os.path.lexists(root),
        }
        print(json.dumps(record, sort_keys=True))
        return 0 if total <= HEADROOM_SECONDS and record["rootGone"] else 1
    finally:
        if os.path.lexists(root):
            T.remove(root)


if __name__ == "__main__":
    sys.exit(main())
