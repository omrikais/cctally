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


def _stage(
    destination: str,
    name: str = "doctor",
    *,
    temp_root: pathlib.Path | str | None = None,
) -> subprocess.CompletedProcess:
    # ``temp_root`` reaches TMPDIR spelled exactly as given: a str keeps a
    # trailing slash that a pathlib.Path would drop (spec 5.8).
    script = (
        'set -uo pipefail\n'
        '. "$1"\n'
        'stage_fixtures_out_of_tree "$2" "$3"\n'
    )
    return subprocess.run(
        ["bash", "-c", script, "_", str(HELPER), name, destination],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=110,
        env=dict(
            os.environ,
            **({"TMPDIR": str(temp_root)} if temp_root is not None else {}),
        ),
    )


def test_an_empty_destination_is_refused():
    proc = _stage("")
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    # Reworded under #769 S5 #808 so the diagnostic survives the evidence
    # scrub; the fail-loud intent is unchanged.
    assert "fixture tree: empty path, refuses to run" in proc.stderr


def test_a_destination_in_the_repository_is_refused(tmp_path):
    """The in-place rebuild, refused at the one place that would perform it.

    A sentinel copy outside a deliberately narrower TMPDIR stands in for
    `tests/fixtures/doctor`. It is outside the helper's allowed root but remains
    wholly under pytest scratch, so neither the passing test nor a regression in
    the guard creates or removes an untracked path in the live repository.
    """
    committed = REPO / "tests" / "fixtures" / "doctor"
    assert committed.is_dir(), "this test needs a committed tree to copy"
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    sentinel = tmp_path / "outside" / "doctor"
    shutil.copytree(committed, sentinel)
    before = sorted(path.name for path in sentinel.iterdir())
    assert before, "the sentinel copy is empty, so surviving proves nothing"

    proc = _stage(str(sentinel), temp_root=allowed_root)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "fixture path" in proc.stderr and "not under temp" in proc.stderr
    assert sorted(path.name for path in sentinel.iterdir()) == before


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
        'd=$(mktemp -d "${TMPDIR:-/tmp}/cctally-staging-guard.XXXXXX") || exit 1\n'
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


# --- #966(c), spec 5.8 (T1): a TMPDIR reached through a symlink -------------
#
# The CI layout gives the suite a TMPDIR whose spelling is not its realpath.
# Allocation goes through that spelling (a real `mktemp -d`), and the guard
# must still accept a destination inside it and refuse one that resolves
# outside it. Baseline-green coverage: the helper already resolves both sides
# (spec section 9). The discrimination run against the two named wrong
# variants of the helper is what proves these tests can fail.

_T1_ALLOCATE_AND_STAGE = (
    'set -uo pipefail\n'
    '. "$1"\n'
    'd=$(mktemp -d "${TMPDIR%/}/cctally-t1.XXXXXX") || exit 9\n'
    'printf "%s\\n" "$d" > "$2"\n'
    'stage_fixtures_out_of_tree pricing-check "$d/pricing-check"\n'
)


def _t1_tree(root):
    """Every regular file under ``root``, by relative path, with its bytes;
    SQLite sidecars are ignored (the fixture's own .gitignore ignores them)."""
    root = pathlib.Path(root)
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and not p.is_symlink()
            and not p.name.endswith(("-wal", "-shm"))}


def _t1_symlinked_tmpdir(tmp_path, trailing):
    real = tmp_path / "real-tmp"
    real.mkdir()
    link = tmp_path / "link-tmp"
    link.symlink_to(real, target_is_directory=True)
    return real, link, str(link) + trailing


@pytest.mark.parametrize("trailing", ["", "/"], ids=["bare", "trailing-slash"])
def test_staging_through_a_symlinked_tmpdir_allocates_through_the_link(tmp_path, trailing):
    real, link, tmpdir = _t1_symlinked_tmpdir(tmp_path, trailing)
    where = tmp_path / "allocated.txt"
    proc = subprocess.run(
        ["bash", "-c", _T1_ALLOCATE_AND_STAGE, "_", str(HELPER), str(where)],
        capture_output=True, text=True, cwd=str(REPO), timeout=110,
        env=dict(os.environ, TMPDIR=tmpdir))
    allocated = where.read_text(encoding="utf-8").strip()
    # The allocation went through the link: its spelling is not its realpath.
    assert allocated.startswith(str(link) + "/"), allocated
    resolved = os.path.realpath(allocated)
    assert resolved != allocated and resolved.startswith(os.path.realpath(real) + os.sep)
    assert proc.returncode == 0, (
        "staging through a symlinked TMPDIR was refused: " + proc.stderr[-3000:])
    staged = pathlib.Path(allocated) / "pricing-check"
    # The content verifies: the committed file set, and byte for byte what the
    # same helper stages through a plain TMPDIR in this same process environment.
    reference_root = tmp_path / "reference-tmp"
    reference_root.mkdir()
    ref = _stage(str(reference_root / "pricing-check"), name="pricing-check",
                 temp_root=reference_root)
    assert ref.returncode == 0, ref.stderr[-3000:]
    committed = _t1_tree(REPO / "tests" / "fixtures" / "pricing-check")
    assert committed, "the committed pricing-check tree is empty, so matching it proves nothing"
    assert sorted(_t1_tree(staged)) == sorted(committed), "the staged tree is not the fixture tree"
    assert _t1_tree(staged) == _t1_tree(reference_root / "pricing-check"), (
        "staging through the link produced different content")


# The bare cases keep their original node ids; the trailing-slash twins are added.
@pytest.mark.parametrize("escape,trailing", [
    ("symlink", ""), ("dotdot", ""), ("symlink", "/"), ("dotdot", "/"),
], ids=["symlink", "dotdot", "symlink-trailing-slash", "dotdot-trailing-slash"])
def test_an_escaping_destination_under_a_symlinked_tmpdir_is_refused(tmp_path, escape, trailing):
    real, link, tmpdir = _t1_symlinked_tmpdir(tmp_path, trailing)
    sentinel = tmp_path / "outside" / "pricing-check"
    shutil.copytree(REPO / "tests" / "fixtures" / "pricing-check", sentinel)
    before = _t1_tree(sentinel)
    assert before, "the sentinel copy is empty, so surviving proves nothing"
    if escape == "symlink":
        (real / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
        destination = os.path.join(tmpdir, "escape/pricing-check")
    else:
        destination = os.path.join(tmpdir, "../outside/pricing-check")
    # The raw spelling, so a trailing slash reaches the guard's TMPDIR intact.
    proc = _stage(destination, name="pricing-check", temp_root=tmpdir)
    assert proc.returncode == 2, "the escaping destination was not refused: rc=%d %s" % (
        proc.returncode, proc.stderr[-2000:])
    assert "fixture path" in proc.stderr and "not under temp" in proc.stderr
    assert _t1_tree(sentinel) == before, "the refused staging changed the outside sentinel"
