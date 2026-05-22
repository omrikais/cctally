"""§9.4 — top-level ``-v`` short alias for ``--version``.

Spec §7.5 (issue #86 Session A): ``cctally -v`` is a 2-character
ccusage-paste-friendly alias for the existing ``cctally --version``.
Both must print the same string and exit 0.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CCTALLY = Path(__file__).resolve().parent.parent / "bin" / "cctally"


def _run(*args):
    return subprocess.run(
        [sys.executable, str(CCTALLY), *args],
        capture_output=True, text=True,
    )


def test_v_prints_same_as_version():
    r1 = _run("-v")
    r2 = _run("--version")
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    assert r1.stdout == r2.stdout, (r1.stdout, r2.stdout)
    assert r1.stdout.strip() != "", "version output should not be empty"


def test_v_exits_zero():
    r = _run("-v")
    assert r.returncode == 0, r.stderr


def test_v_version_string_starts_with_cctally():
    # Defensive: any future change to the version-string format must
    # still emit a "cctally " prefix (the rest is the semver / "unknown").
    r = _run("-v")
    assert r.returncode == 0
    assert r.stdout.startswith("cctally"), r.stdout
