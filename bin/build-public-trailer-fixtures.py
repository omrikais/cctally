#!/usr/bin/env python3
"""Build fixtures for bin/cctally-public-trailer-test.

Each scenario is a directory under tests/fixtures/public-trailer/ with:
  - input.txt        : commit message to feed `parse --message-file`
  - golden-parse.txt : expected JSON output (one line, with trailing \\n)

Builder is idempotent: re-running overwrites both files in place.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "public-trailer"

SCENARIOS: list[tuple[str, str, object]] = [
    ("none-empty", "fix: thing\n",
     {"kind": "none", "subject": "", "body": ""}),
    ("none-with-body", "fix: thing\n\nDetail paragraph.\n",
     {"kind": "none", "subject": "", "body": ""}),
    ("skip-basic", "fix: thing\n\nPublic-Skip: true\n",
     {"kind": "skip", "subject": "", "body": ""}),
    ("skip-with-body", "fix: thing\n\nDetail.\n\nPublic-Skip: true\n",
     {"kind": "skip", "subject": "", "body": ""}),
    ("skip-case-insensitive", "fix: thing\n\nPublic-Skip: TRUE\n",
     {"kind": "skip", "subject": "", "body": ""}),
    ("publish-subject-only", "fix: thing\n\n--- public ---\ndocs: refresh\n",
     {"kind": "publish", "subject": "docs: refresh", "body": ""}),
    ("publish-subject-and-body",
     "fix: thing\n\n--- public ---\ndocs: refresh\n\nMore detail here.\nSecond para.\n",
     {"kind": "publish", "subject": "docs: refresh",
      "body": "More detail here.\nSecond para."}),
    ("publish-multiparagraph-body",
     "fix: thing\n\n--- public ---\ndocs: refresh\n\nPara one.\n\nPara two.\n",
     {"kind": "publish", "subject": "docs: refresh",
      "body": "Para one.\n\nPara two."}),
    ("crlf-line-endings",
     "fix: thing\r\n\r\n--- public ---\r\ndocs: refresh\r\n",
     {"kind": "publish", "subject": "docs: refresh", "body": ""}),
    ("bom-prefix",
     "﻿fix: thing\n\n--- public ---\ndocs: refresh\n",
     {"kind": "publish", "subject": "docs: refresh", "body": ""}),
    ("publish-very-long-subject",
     "fix: thing\n\n--- public ---\n" + "x" * 200 + "\n",
     {"kind": "publish", "subject": "x" * 200, "body": ""}),
    ("err-both",
     "fix: thing\n\nPublic-Skip: true\n\n--- public ---\ndocs: x\n",
     ("ERROR", "E_BOTH")),
    ("err-duplicate-delimiter",
     "fix: thing\n\n--- public ---\ndocs: x\n--- public ---\ndocs: y\n",
     ("ERROR", "E_DUPLICATE_DELIMITER")),
    ("err-empty-public",
     "fix: thing\n\n--- public ---\n   \n",
     ("ERROR", "E_EMPTY_PUBLIC")),
    ("err-invalid-skip-value",
     "fix: thing\n\nPublic-Skip: false\n",
     ("ERROR", "E_INVALID_SKIP_VALUE")),
    ("err-blank-subject",
     "fix: thing\n\n--- public ---\n\ndocs: refresh\n",
     ("ERROR", "E_BLANK_SUBJECT")),
]


def build(out_root: Path) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    for name, message, expected in SCENARIOS:
        d = out_root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "input.txt").write_text(message, encoding="utf-8")
        if isinstance(expected, dict):
            golden = json.dumps(expected) + "\n"
        else:
            assert expected[0] == "ERROR"
            details = {
                "E_BOTH": "Found both --- public --- and Public-Skip: true. Pick one.",
                "E_DUPLICATE_DELIMITER": "Multiple `--- public ---` lines (at indices [2, 4]).",
                "E_EMPTY_PUBLIC": "`--- public ---` present but no subject follows.",
                "E_INVALID_SKIP_VALUE": "Public-Skip: only 'true' is accepted; got 'false'",
                "E_BLANK_SUBJECT": "`--- public ---` followed by a blank line; subject must be on the line immediately after the delimiter.",
            }
            code = expected[1]
            golden = json.dumps({"error": code, "detail": details[code]}) + "\n"
        (d / "golden-parse.txt").write_text(golden, encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(FIXTURES_DIR),
                   help="Output root (default: tests/fixtures/public-trailer/).")
    args = p.parse_args()
    build(Path(args.out))
    print(f"public-trailer fixtures: built {len(SCENARIOS)} scenarios → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
