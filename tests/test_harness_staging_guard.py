"""`stage_fixtures_out_of_tree` refuses two destinations before deleting one.

The helper's third statement is `rm -rf "$dest"`, and `$dest` arrives from the
caller unvalidated. Two callers can reach it with a destination that must never
be deleted.

The first is a failed `mktemp -d`. Harnesses run `set -uo pipefail` without
`-e`, so `FIXTURE_STAGE=$(mktemp -d …)` failing leaves the variable set to the
empty string. `set -u` does not fire, because the variable is set, and
`"$FIXTURE_STAGE/doctor"` expands to `/doctor`.

The second is a destination in the repository. `stage_fixtures_out_of_tree
doctor "$REPO_ROOT/tests/fixtures/doctor"` rebuilds the committed tree in place
while satisfying every static check that the harness calls the staging helper —
the in-place rebuild #529 S3 Task 15 exists to end, reintroduced through the
helper that ended it.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
HELPER = REPO / "bin" / "_lib-harness-env.sh"


def _stage(destination: str, name: str = "doctor") -> subprocess.CompletedProcess:
    script = (
        'set -uo pipefail\n'
        '. "$1"\n'
        'stage_fixtures_out_of_tree "$2" "$3"\n'
    )
    return subprocess.run(
        ["bash", "-c", script, "_", str(HELPER), name, destination],
        capture_output=True, text=True, cwd=str(REPO), timeout=110,
    )


def test_an_empty_destination_is_refused():
    proc = _stage("")
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "refusing an empty destination" in proc.stderr


def test_a_destination_in_the_repository_is_refused():
    """The in-place rebuild, refused at the one place that would perform it.

    A sentinel copy stands in for `tests/fixtures/doctor`, and the committed
    tree itself is never handed to the helper. The helper's third statement is
    `rm -rf "$dest"`: passing the real path meant that removing the guard this
    test protects would delete the committed tree, and the run that reported the
    regression would also be the run that caused it.
    """
    committed = REPO / "tests" / "fixtures" / "doctor"
    assert committed.is_dir(), "this test needs a committed tree to copy"
    sentinel = REPO / ".staging-guard-sentinel" / "doctor"
    shutil.rmtree(sentinel.parent, ignore_errors=True)
    try:
        shutil.copytree(committed, sentinel)
        before = sorted(path.name for path in sentinel.iterdir())
        assert before, "the sentinel copy is empty, so surviving proves nothing"

        proc = _stage(str(sentinel))
        assert proc.returncode == 2, (proc.returncode, proc.stderr)
        assert "refusing a destination outside" in proc.stderr
        assert sorted(path.name for path in sentinel.iterdir()) == before
    finally:
        shutil.rmtree(sentinel.parent, ignore_errors=True)


def test_a_destination_under_the_temp_root_is_accepted(tmp_path):
    """The positive control, so the two refusals are not refusing everything."""
    if not (REPO / "bin" / "build-pricing-check-fixtures.py").exists():
        pytest.skip("the pricing-check builder is not present in this checkout")
    destination = tmp_path / "pricing-check"
    proc = _stage(str(destination), name="pricing-check")
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert destination.is_dir()
    assert any(destination.iterdir())


def test_the_temp_root_the_guard_uses_is_the_one_mktemp_writes_into(tmp_path):
    """A guard keyed to a different root than `mktemp -d` would refuse everything.

    macOS resolves `$TMPDIR` through the `/private` symlink inconsistently —
    `mktemp` reports `/var/folders/…` while a resolved path reads
    `/private/var/folders/…` — so the accepted forms are asserted against a
    real `mktemp -d` rather than assumed.
    """
    script = (
        'set -uo pipefail\n'
        '. "$1"\n'
        'd=$(mktemp -d -t cctally-staging-guard.XXXXXX) || exit 1\n'
        'stage_fixtures_out_of_tree pricing-check "$d/pricing-check"\n'
        'rc=$?\n'
        'rm -rf "$d"\n'
        'exit $rc\n'
    )
    if not (REPO / "bin" / "build-pricing-check-fixtures.py").exists():
        pytest.skip("the pricing-check builder is not present in this checkout")
    proc = subprocess.run(
        ["bash", "-c", script, "_", str(HELPER)],
        capture_output=True, text=True, cwd=str(REPO), timeout=110,
        env=dict(os.environ),
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
