"""Synthetic checkouts that run a copy of bin/cctally-test-all.

Three test families copy the aggregator into a scratch tree and run it
(tests/test_test_all_scheduler.py, tests/test_test_all_observability.py and
tests/test_authoritative_test_contract.py). They share this copier so the set
of files the aggregator needs is stated once.

The capability table is the one thing the copy changes. It lives in
bin/_lib-test-contract.sh as an explicit assignment (test regime reset, Task
3.4), and a scratch tree that probed node, rich and FTS5 for real would make
every case depend on what the runner has installed. `capabilities` replaces the
table's rows; the default is no rows, which probes nothing.
"""
from __future__ import annotations

import pathlib
import re
import shutil

REPO = pathlib.Path(__file__).resolve().parents[2]
BIN = REPO / "bin"
MATCHER = pathlib.Path(".githooks") / "_match.py"

_TABLE = re.compile(r"^CONTRACT_CAPABILITY_TABLE=\$'[^']*'$", re.MULTILINE)


def capability_table(rows=()):
    """The assignment for ``rows`` of (name, probe, hard, visibility)."""
    body = "".join("\\t".join(row) + "\\n" for row in rows)
    return "CONTRACT_CAPABILITY_TABLE=$'%s'" % body


def set_capabilities(contract_lib, rows=()):
    """Rewrite the copied library's capability table in place."""
    path = pathlib.Path(contract_lib)
    text = path.read_text(encoding="utf-8")
    new, count = _TABLE.subn(lambda _m: capability_table(rows), text)
    assert count == 1, "bin/_lib-test-contract.sh declares no capability table"
    path.write_text(new, encoding="utf-8")


def copy_aggregator(bindir, *, capabilities=(), durations_validator=None):
    """Copy bin/cctally-test-all and everything it loads into ``bindir``.

    The shell libraries and the `_lib_test_*.py` kernels are matched as a
    CLASS, because the aggregator loads whichever of them a tree carries and a
    copy by name breaks the moment one more is added. ``durations_validator``
    writes bin/_lib_harness_durations.py's text instead of copying it, which is
    how the scheduler's ImportError branch is reached: deleting the file does
    not reach it, because tests/isolation_bootstrap/sitecustomize.py appends
    the real bin/ to every child's sys.path.
    """
    bindir = pathlib.Path(bindir)
    bindir.mkdir(parents=True, exist_ok=True)
    runner = bindir / "cctally-test-all"
    shutil.copy2(BIN / "cctally-test-all", runner)
    for lib in sorted(BIN.glob("_lib-*.sh")):
        shutil.copy2(lib, bindir / lib.name)
    set_capabilities(bindir / "_lib-test-contract.sh", capabilities)
    for kernel in sorted(BIN.glob("_lib_test_*.py")):
        shutil.copy2(kernel, bindir / kernel.name)
    shutil.copy2(BIN / "_lib_quarantine.py", bindir / "_lib_quarantine.py")
    # The mirror matcher is what marks the private tree to the aggregator's
    # disclosure facts: without it the published tree vouches for no word and
    # redacts every detail. A copy taken from the private tree keeps it, so the
    # copy scrubs as its source does.
    if (REPO / MATCHER).is_file():
        (bindir.parent / MATCHER).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / MATCHER, bindir.parent / MATCHER)
    target = bindir / "_lib_harness_durations.py"
    if durations_validator is None:
        shutil.copy2(BIN / "_lib_harness_durations.py", target)
    else:
        target.write_text(durations_validator, encoding="utf-8")
    return runner


def write_harness(bindir, name, body="#!/usr/bin/env bash\nexit 0\n", mode=0o755):
    path = pathlib.Path(bindir) / f"cctally-{name}-test"
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path
