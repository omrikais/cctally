"""The reader of tests/harness-duration-hints.tsv (#630 S3; test regime reset).

Imported by `bin/cctally-test-all`'s dispatch scheduler. The table is a set of
HINTS (Codex Q10): one `harness<TAB>seconds` row per timed harness, committed
as a record (its generator, which read the retired wrapper's invocation ledger,
is gone; edit a row by hand from a measured run). A missing, stale or malformed row orders
nothing and never suppresses or refuses a harness: every discovered harness is
dispatched whatever the table says.

Pure and stdlib-only: every function either returns a value or returns an error
string. Nothing here writes, prints, or exits.
"""
from __future__ import annotations

import os

TABLE_REL = os.path.join("tests", "harness-duration-hints.tsv")


def read_table(repo_root):
    """-> (text, error). A missing file is an error, never an empty table."""
    path = os.path.join(repo_root, TABLE_REL)
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read(), None
    except OSError:
        return "", "no duration hints at %s" % path


def parse_hints(text):
    """-> (hints, problems). hints maps a harness name to a positive integer;
    each problem names one row that was skipped. A duplicate keeps its first
    row."""
    hints, problems = {}, []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            problems.append("row %d is not 'name<TAB>seconds'" % lineno)
            continue
        name, raw = parts[0].strip(), parts[1].strip()
        try:
            seconds = int(raw)
        except ValueError:
            problems.append("row %d: %r is not an integer" % (lineno, raw))
            continue
        if seconds <= 0 or not name:
            problems.append("row %d is not a positive duration for a name" % lineno)
            continue
        if name in hints:
            problems.append("row %d repeats %r" % (lineno, name))
            continue
        hints[name] = seconds
    return hints, problems


def dispatch_order(report, hints):
    """A permutation of ``report``: the harnesses without a hint first, in
    report order (a harness nobody has timed may be long), then the hinted ones
    longest-processing-time first, ties broken by report position so the sort
    is total and the printed row order never depends on it. A hint naming a
    harness that is not in ``report`` is ignored."""
    position = {name: index for index, name in enumerate(report)}
    unhinted = [name for name in report if name not in hints]
    hinted = sorted(
        (name for name in report if name in hints),
        key=lambda name: (-hints[name], position[name]),
    )
    return unhinted + hinted
