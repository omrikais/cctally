"""Regenerating a golden must write the committed file, not a staging copy.

#529 S3 Task 15 moved six harnesses to build their generated fixture inputs into
a `mktemp -d` staging root, so a test run stops writing under `tests/fixtures/`.
Three of them addressed their GOLDENS through that same staging root, and each
one also removes the staging root in an `EXIT` trap. Their documented
regenerate workflow therefore printed `REGEN <case>` for every case, wrote the
new goldens into a directory that was then deleted, and left the committed files
untouched — a failure that reads exactly like success, and one no test could see
because nothing exercised regenerate mode at all.

Two checks here, and they answer different questions. The first reads the
harnesses and reports any golden path that expands from a staging root, so the
defect cannot come back in a harness this file never ran. The second runs a real
harness in regenerate mode against a deliberately corrupted committed golden and
requires the committed bytes to come back, so the first check is not the only
thing standing between the workflow and silence.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def fixture_tree_lock_path(name: str) -> pathlib.Path:
    """The lock a test holds while `tests/fixtures/NAME` is not its committed self.

    The pytest phase runs under xdist, so two tests over one fixture tree land
    in different processes. `test_regenerating_rewrites_the_committed_goldens`
    corrupts every committed golden under `tests/fixtures/pricing-check` for
    about four seconds, and
    `tests/test_fixture_builder_contract.py` compares `git status` over that
    same subtree before and after a builder run — concurrently, that comparison
    blames the builder for this file's corruption. Keyed by the repository path
    so two checkouts of cctally do not serialise against each other.
    """
    digest = hashlib.sha1(str(REPO).encode("utf-8")).hexdigest()[:12]
    return pathlib.Path(tempfile.gettempdir()) / (
        "cctally-fixture-tree.%s.%s.lock" % (name, digest)
    )


@contextlib.contextmanager
def fixture_tree_lock(name: str):
    handle = open(fixture_tree_lock_path(name), "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        handle.close()

# The roots a harness stages generated fixture inputs into. Each is a `mktemp -d`
# directory removed by the harness's own EXIT trap.
_STAGING_ROOTS = ("FIXTURE_STAGE", "SCRATCH_ROOT", "SCRATCH_DIR", "SCRATCH")

# An assignment whose left-hand side names a golden file or an expected exit
# code — the two things a regenerate mode writes.
_GOLDEN_LHS = re.compile(r"(?i)^(?:\w*golden\w*|\w*exit_file)$")

# The directory `bin/_lib-golden-diff.sh` materializes the ACTUAL output into so
# it can run one `diff` over two real files. It has to be scratch, and naming it
# is not addressing a golden.
_NOT_A_GOLDEN_PATH = frozenset({"GOLDEN_DIFF_TMPDIR"})

_ASSIGN = re.compile(r"^\s*(?:local\s+|export\s+)?([A-Za-z_]\w*)=(.*)$")
_FOR_IN = re.compile(r"^\s*for\s+([A-Za-z_]\w*)\s+in\s+(.*?)(?:;\s*do)?\s*$")
_REFERENCE = re.compile(r"\$\{?([A-Za-z_]\w*)")


def assignments(text: str) -> dict[str, list[str]]:
    """`{name: [every value bound to it]}`, in source order.

    Every value is kept rather than only the last, because a harness may address
    the committed root in one branch and a staging root in another and only the
    second is the defect. A `for NAME in <words>` header binds NAME just as an
    assignment does, and `bin/cctally-share-test` reaches its goldens exactly
    that way, so leaving loop headers out would have missed a real offender.
    """
    found: dict[str, list[str]] = {}
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            continue
        match = _ASSIGN.match(raw) or _FOR_IN.match(raw)
        if match is None:
            continue
        found.setdefault(match.group(1), []).append(match.group(2))
    return found


# `basename` and `dirname` take a path apart; what comes out is a component,
# not a location. `name=$(basename "${d%/}")` yields the same scenario name
# whether `d` walked the staging copy or the committed root, so treating the
# result as a staging path would report every fixed harness as still broken.
_NAME_EXTRACTION = re.compile(r"\$\((?:basename|dirname)\b[^()]*\)")


def _path_references(value: str) -> list[str]:
    previous = None
    while previous != value:
        previous, value = value, _NAME_EXTRACTION.sub("", value)
    return _REFERENCE.findall(value)


def staged_names(text: str, roots: tuple[str, ...] = _STAGING_ROOTS) -> set[str]:
    """Variables whose value reaches a staging root, following references."""
    values = assignments(text)
    staged = {name for name in roots if name in values}
    changed = True
    while changed:
        changed = False
        for name, assigned in values.items():
            if name in staged:
                continue
            for value in assigned:
                if any(ref in staged for ref in _path_references(value)):
                    staged.add(name)
                    changed = True
                    break
    return staged


# A redirection straight onto a path, with no variable holding it first —
# `echo "$rc" > "$dir/expected.exit"` is how `bin/cctally-pricing-check-test`
# wrote one of its two goldens, so the assignment scan alone would have missed
# it.
_REDIRECT = re.compile(r'>\s*"([^"]*(?:golden|expected)[^"]*)"', re.IGNORECASE)


def goldens_addressed_at_staging(text: str) -> list[str]:
    """Golden paths this harness builds out of a staging root."""
    staged = staged_names(text)
    offenders = []
    for name, assigned in assignments(text).items():
        if name in _NOT_A_GOLDEN_PATH or not _GOLDEN_LHS.match(name):
            continue
        for value in assigned:
            if any(ref in staged for ref in _path_references(value)):
                offenders.append(f"{name}={value.strip()}")
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            continue
        for target in _REDIRECT.findall(raw):
            if "/" not in target:
                continue  # `> "$golden"` — that variable's own binding is checked above.
            if any(ref in staged for ref in _path_references(target)):
                offenders.append(f'> "{target}"')
    return offenders


# ------------------------------------------------------- the resolver can fail


_DEFECTIVE = """\
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
FIXTURE_STAGE=$(mktemp -d -t cctally-doctor-fixstage.XXXXXX)
FIXTURES="$FIXTURE_STAGE/doctor"
trap 'rm -rf "$FIXTURE_STAGE"' EXIT
run_mode () {
    local name="$1" suffix="$2"
    local dir="$FIXTURES/$name"
    local golden="$dir/expected.$suffix"
    printf '%s' "$actual" > "$golden"
    echo "$rc" > "$dir/expected.exit"
}
"""

_FIXED = """\
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
FIXTURE_STAGE=$(mktemp -d -t cctally-doctor-fixstage.XXXXXX)
FIXTURES="$FIXTURE_STAGE/doctor"
GOLDEN_ROOT="$REPO_ROOT/tests/fixtures/doctor"
trap 'rm -rf "$FIXTURE_STAGE"' EXIT
run_mode () {
    local name="$1" suffix="$2"
    local dir="$FIXTURES/$name"
    local golden="$GOLDEN_ROOT/$name/expected.$suffix"
    printf '%s' "$actual" > "$golden"
}
"""


def test_the_resolver_reports_the_defective_shape():
    """The shape three harnesses actually had, kept as the thing being detected.

    An assertion that a derived list is empty is satisfied by a derivation that
    finds nothing under any circumstances, so the derivation is exercised
    against the real defect here rather than only against the fixed tree.
    """
    assert "FIXTURES" in staged_names(_DEFECTIVE)
    offenders = goldens_addressed_at_staging(_DEFECTIVE)
    assert offenders == [
        'golden="$dir/expected.$suffix"',
        '> "$dir/expected.exit"',
    ], offenders


def test_the_resolver_accepts_the_repository_rooted_shape():
    assert goldens_addressed_at_staging(_FIXED) == []
    assert "GOLDEN_ROOT" not in staged_names(_FIXED)


# ------------------------------------------------------------- the real estate


def _harnesses() -> list[pathlib.Path]:
    return sorted((REPO / "bin").glob("cctally-*-test"))


def test_no_harness_addresses_a_golden_through_its_staging_root():
    reported = {}
    for harness in _harnesses():
        offenders = goldens_addressed_at_staging(harness.read_text(encoding="utf-8"))
        if offenders:
            reported[harness.name] = offenders
    assert not reported, (
        "these harnesses build a golden path out of a directory their own EXIT "
        "trap removes, so regenerating a golden reports success and writes "
        "nothing that survives the run: %r. Address goldens at the committed "
        "root instead, as bin/cctally-share-v2-test does with GOLDEN_ROOT."
        % reported
    )


def test_at_least_one_harness_is_scanned():
    """The estate assertion above is empty-set-shaped; this is its floor."""
    assert len(_harnesses()) > 10


# ------------------------------------------------- regenerate mode, end to end


_REGEN_HARNESS = REPO / "bin" / "cctally-pricing-check-test"
_REGEN_FIXTURES = REPO / "tests" / "fixtures" / "pricing-check"


def _committed_goldens() -> dict[pathlib.Path, bytes]:
    return {
        path: path.read_bytes()
        for path in sorted(_REGEN_FIXTURES.rglob("expected.*"))
        if path.is_file()
    }


@pytest.mark.skipif(
    not _REGEN_HARNESS.exists() or not _REGEN_FIXTURES.is_dir(),
    reason="the pricing-check harness is not present in this checkout",
)
def test_regenerating_rewrites_the_committed_goldens():
    """The check the static scan cannot make: the committed bytes come back.

    `bin/cctally-pricing-check-test` is the smallest of the affected harnesses,
    and it exercises both write shapes — a golden held in a variable and an exit
    code redirected straight onto a path. Each committed golden is corrupted,
    the harness is run in regenerate mode, and the original bytes must be back
    afterwards. Against the defect this fails on the first assertion, because
    regeneration reported `REGEN` for every scenario and wrote into a directory
    the harness then deleted.

    Every file is restored in `finally`, and the subtree is asserted clean, so a
    failure here does not leave the tracked tree dirty. The corruption is held
    under `fixture_tree_lock`, because while it is in place any other test that
    reads `git status` over this subtree sees a tree this test dirtied.
    """
    original = _committed_goldens()
    assert original, "no committed golden to regenerate"

    with fixture_tree_lock("pricing-check"):
        try:
            for path in original:
                path.write_bytes(b"corrupted by test_regenerating_rewrites\n")
            proc = subprocess.run(
                [str(_REGEN_HARNESS)],
                cwd=str(REPO),
                env=dict(os.environ, CCTALLY_PRICING_CHECK_REGENERATE="1",
                         TZ="Etc/UTC"),
                capture_output=True, text=True, timeout=110,
            )
            rewritten = {path: path.read_bytes() for path in original}
            unchanged = sorted(
                str(path.relative_to(REPO))
                for path, data in rewritten.items()
                if data == b"corrupted by test_regenerating_rewrites\n"
            )
            assert not unchanged, (
                "regenerate mode reported success but never wrote these "
                "committed goldens: %s\n--- harness stdout ---\n%s"
                % (unchanged, proc.stdout[-3000:])
            )
            differing = sorted(
                str(path.relative_to(REPO))
                for path, data in rewritten.items()
                if data != original[path]
            )
            assert not differing, (
                "regeneration did not reproduce the committed bytes for %s, so "
                "the committed goldens are stale relative to the code" % differing
            )
        finally:
            for path, data in original.items():
                path.write_bytes(data)

        status = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain", "--",
             "tests/fixtures/pricing-check"],
            capture_output=True, text=True,
        )
        assert status.stdout == "", status.stdout
