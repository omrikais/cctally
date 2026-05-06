#!/usr/bin/env python3
"""Build fixtures for bin/cctally-public-trailer-hook-test.

Each scenario is a directory under tests/fixtures/public-trailer-hook/
containing:
  - setup.sh                : bash script that produces a tiny git repo at
                              $SCRATCH/<scenario>/repo, stages files, and
                              writes ../msg.txt with the test message
  - golden-exit.txt         : single-line expected exit code
  - golden-stderr-substr.txt: expected stderr substring (the E_* code)

The harness invokes setup.sh then runs the hook subcommand from inside
the scratch repo.
"""

from __future__ import annotations

import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "public-trailer-hook"

# Each setup.sh runs in $SCRATCH/<scenario>/, creates `repo/`, copies
# .mirror-allowlist + .githooks/_match.py + .githooks/_public_trailer.py
# from REPO_ROOT (passed as $1), then runs scenario-specific staging.
_SCAFFOLD = '''#!/bin/bash
set -euo pipefail
SCRATCH="$(pwd)"
REPO_ROOT="$1"
mkdir -p repo && cd repo
git init -q
git config user.email "test@example.com"
git config user.name "Test"
mkdir -p .githooks
cp "$REPO_ROOT/.mirror-allowlist" .
cp "$REPO_ROOT/.githooks/_match.py" .githooks/
cp "$REPO_ROOT/.githooks/_public_trailer.py" .githooks/
'''

# (name, staging_snippet, message, expected_exit, stderr_substr)
SCENARIOS: list[tuple[str, str, str, int, str]] = [
    ("public-with-publish-block",
     'echo "x" > README.md\ngit add README.md\n',
     "fix: thing\n\n--- public ---\ndocs: tweak\n",
     0, ""),
    ("public-with-skip",
     'echo "x" > README.md\ngit add README.md\n',
     "fix: thing\n\nPublic-Skip: true\n",
     0, ""),
    ("public-missing-trailer",
     'echo "x" > README.md\ngit add README.md\n',
     "fix: tweak readme\n",
     1, "E_MISSING"),
    ("public-both-surfaces",
     'echo "x" > README.md\ngit add README.md\n',
     "fix: thing\n\nPublic-Skip: true\n\n--- public ---\ndocs: x\n",
     1, "E_BOTH"),
    ("private-no-trailer",
     'mkdir -p .githooks\ncp "$REPO_ROOT/.githooks/_match.py" .githooks/_match.py\necho "x" >> .githooks/_match.py\ngit add .githooks/_match.py\n',
     "wip: private\n",
     0, ""),
    ("private-malformed-trailer",
     'mkdir -p .githooks\ncp "$REPO_ROOT/.githooks/_match.py" .githooks/_match.py\necho "x" >> .githooks/_match.py\ngit add .githooks/_match.py\n',
     "wip\n\nPublic-Skip: false\n",
     1, "E_INVALID_SKIP_VALUE"),
    ("public-blank-subject",
     'echo "x" > README.md\ngit add README.md\n',
     "fix: thing\n\n--- public ---\n\ndocs: x\n",
     1, "E_BLANK_SUBJECT"),
    ("public-duplicate-delimiter",
     'echo "x" > README.md\ngit add README.md\n',
     "fix: thing\n\n--- public ---\ndocs: x\n--- public ---\ndocs: y\n",
     1, "E_DUPLICATE_DELIMITER"),
    ("public-empty-block",
     'echo "x" > README.md\ngit add README.md\n',
     "fix: thing\n\n--- public ---\n   \n",
     1, "E_EMPTY_PUBLIC"),
]


def build(out_root: Path) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    for name, staging, message, exit_code, stderr_substr in SCENARIOS:
        d = out_root / name
        d.mkdir(parents=True, exist_ok=True)
        # Use a heredoc with a unique sentinel so the message body is
        # written byte-for-byte without shell interpretation. printf-
        # based interpolation is fragile across host shells (BSD vs.
        # GNU printf differ on backslash escapes); a heredoc avoids
        # both `'` quoting and `\n` interpretation entirely.
        setup = (
            _SCAFFOLD
            + staging
            + f"cat > ../msg.txt <<'CCTALLY_EOF_MSG_DELIM'\n{message}CCTALLY_EOF_MSG_DELIM\n"
        )
        (d / "setup.sh").write_text(setup, encoding="utf-8")
        (d / "setup.sh").chmod(0o755)
        (d / "golden-exit.txt").write_text(f"{exit_code}\n", encoding="utf-8")
        (d / "golden-stderr-substr.txt").write_text(stderr_substr + "\n", encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(FIXTURES_DIR))
    args = p.parse_args()
    build(Path(args.out))
    print(f"public-trailer-hook fixtures: built {len(SCENARIOS)} scenarios → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
