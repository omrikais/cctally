"""The committed runtime budget, written into a scratch estate (#630 S7, #650).

A real estate carries `tests/authoritative-runtime-budget.json`, and an
authoritative full run loads it at admission, so every scratch estate that can
reach an eligible run carries one too.

The DEFAULT maximum is deliberately enormous rather than the committed 120: a
scratch estate passes about ten cases in a few seconds, which is a per-case cost
three orders of magnitude worse than the real estate's, and a fixture that
breached its own budget by construction would fail every unrelated authoritative
case in the file that built it.
"""
from __future__ import annotations

import json
import pathlib

DEFAULT_MAX = 1000000


def write_budget(tests_dir, budget=None) -> None:
    """Write the budget file into `tests_dir`.

    `budget` is a number to pin the maximum, a string to write the file
    verbatim, or `False` to omit the file entirely.
    """
    if budget is False:
        return
    if isinstance(budget, str):
        text = budget
    else:
        text = json.dumps(
            {
                "schemaVersion": 1,
                "metric": "secondsPerThousandCases",
                "maxSecondsPerThousandCases": (
                    DEFAULT_MAX if budget is None else budget
                ),
            },
            indent=2,
        ) + "\n"
    pathlib.Path(tests_dir).joinpath(
        "authoritative-runtime-budget.json"
    ).write_text(text, encoding="utf-8")
