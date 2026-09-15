#!/usr/bin/env python3
"""Validate the TUI fixture manifest before any render runs (#769 S4 #796).

`bin/build-tui-fixtures.py --manifest` is the single owner of the TUI
combination list and `bin/cctally-tui-test` reads it. Checking only the
builder's exit status would leave that harness VACUOUSLY GREEN: it starts
`fail=0` and passes while that stays zero, so a manifest that exited 0 and
carried nothing would run no case and report success.

So the manifest is validated in full first, and the checks live here rather
than in a heredoc inside the harness — a pure function over text is something
`tests/test_tui_fixture_manifest.py` can drive with each malformed shape,
which a heredoc is not.

`validate_manifest` returns a list of human-readable problems for any manifest
TEXT, however malformed, and never raises on that text; an empty list means the
manifest is usable. `expected_records` is a count rather than text: a
non-numeric value raises `ValueError` from `int()`, which is why `main`
converts the command-line argument itself and reports a bad one as a problem
instead of letting a traceback reach the caller.
"""
from __future__ import annotations

import pathlib
import sys

#: Columns of one record, in order.
FIELDS = ("state", "variant", "size", "tz")


def validate_manifest(
    text: str,
    *,
    fixtures_dir: "pathlib.Path | str",
    golden_dir: "pathlib.Path | str",
    expected_records: int,
) -> list:
    """Every reason this manifest cannot drive a run, worst case first."""
    fixtures = pathlib.Path(fixtures_dir)
    golden = pathlib.Path(golden_dir)
    problems: list = []

    if text and not text.endswith("\n"):
        problems.append("the last record is unterminated")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    seen = set()
    for number, line in enumerate(lines, start=1):
        fields = line.split("\t")
        if len(fields) != len(FIELDS):
            problems.append(
                f"record {number} has {len(fields)} fields, "
                f"expected {len(FIELDS)}"
            )
            continue
        record = dict(zip(FIELDS, fields))
        blank = [name for name in FIELDS if not record[name].strip()]
        if blank:
            problems.append(
                f"record {number} has a blank {', '.join(blank)}")
            continue
        combination = (record["state"], record["variant"], record["size"])
        if combination in seen:
            problems.append(
                f"record {number} duplicates "
                f"{record['state']}/{record['variant']}/{record['size']}"
            )
        seen.add(combination)
        module = fixtures / f"snapshot_{record['state']}.py"
        if not module.is_file():
            problems.append(
                f"record {number} names a missing snapshot module {module}")
        want = golden / (
            f"{record['state']}_{record['variant']}_{record['size']}.txt")
        if not want.is_file():
            problems.append(
                f"record {number} names a missing golden {want}")

    if len(lines) != int(expected_records):
        problems.append(
            f"{len(lines)} records, expected {expected_records}")
    return problems


def main(argv: list) -> int:
    if len(argv) != 4:
        print("usage: _lib_tui_manifest.py <manifest> <fixtures> <golden> "
              "<expected>", file=sys.stderr)
        return 2
    manifest, fixtures, golden = argv[0], argv[1], argv[2]
    try:
        text = pathlib.Path(manifest).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"the manifest could not be read: {exc}")
        return 1
    try:
        expected = int(argv[3])
    except ValueError:
        print(f"the expected record count {argv[3]!r} is not a number")
        return 1
    problems = validate_manifest(
        text, fixtures_dir=fixtures, golden_dir=golden,
        expected_records=expected,
    )
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
