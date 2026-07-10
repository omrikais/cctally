"""Deliberate-hang canary for the pytest-timeout wiring (#279 S7 W4).

Proves that `--timeout` actually bounds a hung test, so a regression in the
flag plumbing (bin/cctally-test-all Phase 3 or the CI pip install) is caught
instead of silently letting a hang wedge the suite / a CI job for GitHub's
default 6h.

Mechanics: launch a NESTED pytest (``sys.executable -m pytest`` — NOT bare
``python3``, which could resolve to a different interpreter than the one where
the plugin was detected, gate finding #6) over a tmp test file that sleeps 600s,
with ``--timeout=2``, all wrapped in ``subprocess.run(timeout=<small bound>)``.

  * GREEN: the child exits NONZERO within ~2s carrying the timeout marker — the
    plugin bounded the hang.
  * RED (the stash→RED equivalent): if ``--timeout`` were dropped/regressed the
    child would sleep 600s, so the OUTER ``subprocess.run`` bound trips
    ``TimeoutExpired`` — a fast, loud failure, not a 10-minute hang. The child is
    terminated by ``subprocess.run`` on that path (and again in ``finally``).

Skipped entirely when pytest-timeout is not importable (the stdlib-only local
default), so a plugin-less `bin/cctally-test-all` run stays green.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


pytest.importorskip("pytest_timeout")

# Outer bound: well under the child's 600s sleep, generous over the 2s timeout.
_OUTER_BOUND_SECONDS = 60
_CHILD_TIMEOUT_SECONDS = 2


def test_pytest_timeout_bounds_a_hang(tmp_path):
    hang_file = tmp_path / "test_hang_.py"
    hang_file.write_text(
        textwrap.dedent(
            """
            import time

            def test_deliberate_hang():
                time.sleep(600)
            """
        )
    )

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "pytest",
            str(hang_file),
            f"--timeout={_CHILD_TIMEOUT_SECONDS}",
            "--timeout-method=thread",
            "-p", "no:cacheprovider",
            "-q",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(tmp_path),
    )
    try:
        out, _ = proc.communicate(timeout=_OUTER_BOUND_SECONDS)
    except subprocess.TimeoutExpired:
        # The plugin did NOT bound the hang — the flag plumbing regressed. Fail
        # fast and loud instead of hanging (the child is reaped in `finally`).
        pytest.fail(
            f"pytest-timeout did not bound the hang: the nested pytest ran past "
            f"{_OUTER_BOUND_SECONDS}s with --timeout={_CHILD_TIMEOUT_SECONDS}. "
            f"The --timeout flag is not taking effect."
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert proc.returncode != 0, (
        f"nested pytest should FAIL the hung test (nonzero exit); "
        f"got {proc.returncode}. Output:\n{out}"
    )
    assert "timeout" in out.lower(), (
        f"nested pytest output should carry the timeout marker; got:\n{out}"
    )
