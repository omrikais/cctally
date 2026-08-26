"""The one validator for tests/authoritative-harness-durations.tsv (#630 S3).

Imported by `bin/cctally-test-all`'s dispatch scheduler and by
`bin/cctally-preflight`. One spelling on purpose. The two callers must disagree
about the CONSEQUENCE of a bad table — the scheduler falls back to report order
and stays authoritative, pre-flight refuses the round trip at the keyboard —
and they must never disagree about the CONTRACT, because two spellings of one
contract is how they drift apart.

The contract: one `harness<TAB>seconds` row per harness, `seconds` a positive
integer, no duplicate names, and an exact cover of the FULL manifest in both
directions. The current profile's rows are selected only afterwards, the way
`contract_manifest_harness_names` selects names, so the committed table is one
union file rather than a public base plus a private overlay.

Pure and stdlib-only: every function either returns a value or returns an error
string. Nothing here writes, prints, or exits.
"""
from __future__ import annotations

import json
import os

MANIFEST_REL = os.path.join("tests", "authoritative-test-manifest.json")
TABLE_REL = os.path.join("tests", "authoritative-harness-durations.tsv")


def read_manifest(repo_root):
    """-> (rows, error). rows is a list of (name, visibility) in file order."""
    path = os.path.join(repo_root, MANIFEST_REL)
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError) as exc:
        return [], "the estate manifest at %s is unreadable: %s" % (path, exc)
    try:
        rows = [
            (str(row.get("name", "")), str(row.get("visibility", "public")))
            for row in doc.get("harnesses", [])
            if str(row.get("name", ""))
        ]
    except AttributeError:
        return [], "the estate manifest at %s has no harness rows" % path
    if not rows:
        return [], "the estate manifest at %s declares no harness" % path
    return rows, None


def read_table(repo_root):
    """-> (text, error). A missing file is an error, never an empty table."""
    path = os.path.join(repo_root, TABLE_REL)
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read(), None
    except OSError:
        return "", "no duration table at %s" % path


def validate(text, manifest_rows):
    """-> (table, error). table maps harness name to a positive integer."""
    manifest_names = {name for name, _ in manifest_rows}
    table = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            return {}, (
                "malformed row %d in the duration table (want 'name<TAB>seconds')"
                % lineno
            )
        name, raw = parts[0].strip(), parts[1].strip()
        try:
            seconds = int(raw)
        except ValueError:
            return {}, (
                "malformed row %d in the duration table: %r is not an integer"
                % (lineno, raw)
            )
        if seconds <= 0:
            return {}, (
                "malformed row %d in the duration table: %d is not a positive duration"
                % (lineno, seconds)
            )
        if name in table:
            return {}, "duplicate harness %r in the duration table" % name
        table[name] = seconds

    unknown = sorted(set(table) - manifest_names)
    if unknown:
        return {}, "unknown harness %s in the duration table" % ", ".join(unknown)
    missing = sorted(manifest_names - set(table))
    if missing:
        return {}, "the duration table does not cover %s" % ", ".join(missing)
    return table, None


def select_for_profile(manifest_rows, profile):
    """The names this profile requires, the way contract_manifest_harness_names
    selects them: the private checkout takes the union, the public tree takes
    only the public rows."""
    return {
        name
        for name, visibility in manifest_rows
        if profile == "private" or visibility == "public"
    }


def dispatch_order(report, table, selected):
    """-> (order, error). Longest-processing-time first over ``report``.

    Ties break by the harness's index in ``report``, ascending, so the sort is
    total and deterministic and the printed row order can never depend on it.

    The result must be a PERMUTATION of ``report``, and that is checked rather
    than assumed. The filter below drops any discovered harness the profile
    does not select or the table does not price, and the caller feeds this list
    straight to the pool — so a filtered name is a harness that is reported,
    counted against the manifest floor, and never actually run. Returning an
    error instead makes the caller dispatch the full report order, which is
    slower and complete rather than faster and short.
    """
    position = {name: index for index, name in enumerate(report)}
    order = sorted(
        (name for name in report if name in selected and name in table),
        key=lambda name: (-table[name], position[name]),
    )
    if len(order) != len(report):
        dropped = sorted(set(report) - set(order))
        return [], (
            "the dispatch order covers %d of the %d discovered harnesses and "
            "would drop %s" % (len(order), len(report), ", ".join(dropped))
        )
    return order, None
