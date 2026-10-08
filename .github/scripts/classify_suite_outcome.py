#!/usr/bin/env python3
"""Decide whether a failed full suite failed ONLY on its runtime budget.

The scheduled `test-macos` run is the repository's background full
verification. A run whose every test passed, but whose wall time exceeded the
runtime budget, is not a failed verification (operator decision D1(b) of
#899). This script reads the suite's own outcome record and exits 0 exactly
when that is the whole story:

* the record has the expected schema (`schemaVersion` 1, a reasons list, a
  totals object);
* coverage is the whole estate: mode `full`, pytest `full`, at least one
  selected harness and none omitted;
* `totals.failed` is 0 and `pytestPassed` is a positive count;
* the reasons are exactly one `runtime-budget-exceeded` in phase `budget`.

Anything else, including a missing or unreadable file, exits 1. It prints one
line naming the first condition that does not hold. `ci.yml` runs it only on a
failed scheduled run, and runs its marker step only on exit 0; the workflow
itself still concludes `failure`. Readers confirm the marker against the
completed job's final step conclusions, because steps after the marker can
still fail.

PUBLIC by mirror classification and stdlib only.
"""
from __future__ import annotations

import json
import sys

BUDGET_CODE = "runtime-budget-exceeded"
BUDGET_PHASE = "budget"


def _count(value) -> bool:
    """A real integer count (a bool is not one)."""
    return isinstance(value, int) and not isinstance(value, bool)


def classify(doc) -> str | None:
    """None when the outcome is a budget-only failure, else the first condition that fails."""
    if not isinstance(doc, dict):
        return "the outcome record is not a JSON object"
    if doc.get("schemaVersion") != 1:
        return "the outcome record's schemaVersion is %r, not 1" % (doc.get("schemaVersion"),)
    reasons, totals, coverage = doc.get("reasons"), doc.get("totals"), doc.get("coverage")
    if not isinstance(reasons, list) or not isinstance(totals, dict):
        return "the outcome record has no reasons list or totals object"
    if not isinstance(coverage, dict):
        return "the outcome record states no coverage"
    if coverage.get("mode") != "full" or coverage.get("pytest") != "full":
        return "coverage is %r / pytest %r, not the whole estate" % (coverage.get("mode"), coverage.get("pytest"))
    selected, omitted = coverage.get("selectedHarnesses"), coverage.get("omittedHarnesses")
    if not isinstance(selected, list) or not selected or omitted != []:
        return "coverage selected no harness or omitted some"
    if not _count(totals.get("failed")) or totals["failed"] != 0:
        return "totals.failed is %r, not 0" % (totals.get("failed"),)
    if not _count(doc.get("pytestPassed")) or doc["pytestPassed"] <= 0:
        return "pytestPassed is %r, not a positive count" % (doc.get("pytestPassed"),)
    if len(reasons) != 1 or not isinstance(reasons[0], dict):
        return "the run recorded %d reasons, not exactly one" % len(reasons)
    if reasons[0].get("code") != BUDGET_CODE or reasons[0].get("phase") != BUDGET_PHASE:
        return "the one reason is %r in phase %r, not %s in phase %s" % (
            reasons[0].get("code"), reasons[0].get("phase"), BUDGET_CODE, BUDGET_PHASE)
    return None


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: classify_suite_outcome.py <outcome.json>")
        return 1
    try:
        with open(argv[0], encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError) as exc:
        print("the outcome record is unreadable: %s" % (exc,))
        return 1
    reason = classify(doc)
    if reason is not None:
        print("not budget-only: " + reason)
        return 1
    print("runtime budget is the only failure")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
