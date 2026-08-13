"""No harness rebuilds a committed fixture tree in place (#529 S3, Task 17).

This file used to record the opposite. It listed the six harnesses that DID
rebuild `tests/fixtures/<cmd>` in place and, for each, the pytest files that
read those directories and would have raced the rebuild under the
pytest/shell-pool overlap #296 investigated. That list was an inventory of a
hazard, kept because the hazard was real.

Task 17 removed the hazard: all six harnesses now stage the committed tree into
scratch and build their generated inputs there, so a test run writes nothing
under `tests/fixtures/`. The assertion inverts with it. Two things are checked,
and the second is what keeps the first from being satisfied by a harness that
simply stopped building anything:

  1. No harness in this tree invokes a fixture builder without redirecting its
     output. The derivation is the same parser as before, so a regression to an
     in-place invocation is caught exactly as a new one used to be.
  2. Each of the six former holdouts still stages its own fixture tree out of
     tree, named explicitly.

The reader and deselect assertions are deleted rather than left. With no
in-place directory left to read, `_pytest_readers_of` returns nothing, and an
assertion over nothing passes without checking anything — which is worse than
no assertion, because it reads like coverage.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]

# The six that used to rebuild in place. Each maps 1:1 to bin/cctally-<name>-test
# and to tests/fixtures/<name>.
FORMER_INPLACE_HOLDOUTS = {
    "conversation", "dashboard", "doctor", "pricing-check", "share", "share-v2",
}

# The helper each of them now calls, from bin/_lib-harness-env.sh.
STAGING_HELPER = "stage_fixtures_out_of_tree"

# builder stem -> output roots when NOT tests/fixtures/<stem>.
BUILDER_ROOT_EXCEPTIONS = {
    "codex": ["codex-daily", "codex-monthly", "codex-weekly", "codex-session"],
}
OUTDIR_MARKERS = ("--out", "--out-dir", "build_fixtures_cached", "SCRATCH")


def _logical_lines(text):
    """Join backslash line-continuations into single logical lines."""
    out, buf = [], ""
    for raw in text.splitlines():
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        out.append(buf + raw)
        buf = ""
    if buf:
        out.append(buf)
    return out


# A builder invocation in COMMAND position: optional leading whitespace, an
# optional `python3 ` / `env VAR=val ` prefix, then the builder path token.
# Excludes `#`-comments, `FAIL: build-… crashed` echoes (builder mid-string),
# and `B=…`/`SB=…` assignments (token not in command position).
_CMD = re.compile(
    r'^\s*(?:python3\s+|env(?:\s+\w+=\S+)*\s+)*'
    r'"?\$(?:REPO_ROOT|REPO)"?/bin/build-([a-z0-9-]+)-fixtures\.py\b(.*)$'
)


def _inplace_dirs_in(text):
    """The fixture dirs one harness's TEXT rebuilds in place.

    Split out from the tree walk so the parser can be exercised against
    scaffolds: an assertion that a derived set is empty is otherwise satisfied
    by a derivation that finds nothing under any circumstances.
    """
    dirs = set()
    for line in _logical_lines(text):
        if line.lstrip().startswith("#"):
            continue
        m = _CMD.match(line)
        if not m:
            continue
        if any(mark in line for mark in OUTDIR_MARKERS):
            continue  # redirected to scratch -> not in-place
        dirs.update(BUILDER_ROOT_EXCEPTIONS.get(m.group(1), [m.group(1)]))
    return dirs


def _derive_inplace_dirs():
    dirs = set()
    for harness in sorted((REPO / "bin").glob("cctally-*-test")):
        dirs |= _inplace_dirs_in(harness.read_text())
    return dirs


def test_no_harness_rebuilds_a_committed_fixture_tree_in_place():
    """The inverted assertion: the derived in-place set must be EMPTY."""
    derived = _derive_inplace_dirs()
    assert derived == set(), (
        "these harnesses invoke a fixture builder without redirecting its "
        "output, so a test run rewrites tests/fixtures/%s and leaves the tracked "
        "tree dirty: %s. Stage the committed tree into scratch with %s and build "
        "there." % (sorted(derived), sorted(derived), STAGING_HELPER)
    )


_ASSIGN = re.compile(r"^\s*(?:local\s+|export\s+)?([A-Za-z_]\w*)=(.*)$")
_REF = re.compile(r"\$\{?([A-Za-z_]\w*)")


def _bindings(text):
    found = {}
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            continue
        m = _ASSIGN.match(raw)
        if m:
            found.setdefault(m.group(1), []).append(m.group(2))
    return found


def _reaches_mktemp(name, bindings, seen=None):
    """Whether NAME's value is built on a `mktemp` result, following references."""
    seen = seen or set()
    if name in seen:
        return False
    seen.add(name)
    for value in bindings.get(name, ()):
        if "mktemp" in value:
            return True
        if any(_reaches_mktemp(ref, bindings, seen) for ref in _REF.findall(value)):
            return True
    return False


def staging_destinations(text, helper=STAGING_HELPER):
    """`{fixture name: destination expression}` for each staging call."""
    pattern = re.compile(
        r'^\s*%s\s+([A-Za-z0-9_-]+)\s+"?([^"\s]+)"?' % re.escape(helper)
    )
    found = {}
    for raw in text.splitlines():
        if raw.strip().startswith("#"):
            continue
        m = pattern.match(raw)
        if m:
            found[m.group(1)] = m.group(2)
    return found


def staged_into_the_tree(text, helper=STAGING_HELPER):
    """Staging calls whose destination is not a `mktemp` directory.

    Calling the helper is not the property; staging OUT OF TREE is.
    `stage_fixtures_out_of_tree doctor "$REPO_ROOT/tests/fixtures/doctor"`
    rebuilds the committed tree in place and satisfies a check that only looks
    for the call. `bin/_lib-harness-env.sh` refuses that destination at run
    time; this is the same rule read off the source, so a harness that would be
    refused is reported before anybody runs it.
    """
    bindings = _bindings(text)
    offenders = {}
    for name, destination in staging_destinations(text, helper).items():
        refs = _REF.findall(destination)
        if not any(_reaches_mktemp(ref, bindings) for ref in refs):
            offenders[name] = destination
    return offenders


def test_the_six_former_holdouts_stage_their_fixtures_out_of_tree():
    """Emptiness alone is satisfied by a harness that stopped building at all."""
    missing = []
    for name in sorted(FORMER_INPLACE_HOLDOUTS):
        harness = REPO / "bin" / ("cctally-%s-test" % name)
        if not harness.exists():
            continue  # mirror-private in a public clone
        text = harness.read_text()
        if ("%s %s " % (STAGING_HELPER, name)) not in text:
            missing.append(name)
    assert not missing, (
        "these harnesses no longer stage their fixture tree out of tree: %s" % missing
    )


def test_every_staging_destination_is_a_temporary_directory():
    """The other direction, which naming the helper does not establish."""
    reported = {}
    for harness in sorted((REPO / "bin").glob("cctally-*-test")):
        offenders = staged_into_the_tree(harness.read_text())
        if offenders:
            reported[harness.name] = offenders
    assert not reported, (
        "these harnesses call %s with a destination that is not a mktemp "
        "directory, so the rebuild lands wherever that path points — including, "
        "if it is the committed root, in place: %r" % (STAGING_HELPER, reported)
    )


def test_the_destination_check_reports_a_committed_root():
    """Proven against the exact call that would defeat the check above."""
    in_tree = (
        'REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)\n'
        '%s doctor "$REPO_ROOT/tests/fixtures/doctor"\n' % STAGING_HELPER
    )
    assert staged_into_the_tree(in_tree) == {
        "doctor": "$REPO_ROOT/tests/fixtures/doctor"
    }

    staged = (
        'FIXTURE_STAGE=$(mktemp -d -t cctally-doctor-fixstage.XXXXXX)\n'
        'FIXTURES="$FIXTURE_STAGE/doctor"\n'
        '%s doctor "$FIXTURES"\n' % STAGING_HELPER
    )
    assert staged_into_the_tree(staged) == {}


def test_the_inversion_can_fail():
    """Proven against scaffolds, so the empty derivation is not vacuous.

    An assertion that something is empty is exactly the assertion that passes
    when the thing that produces it is broken, so the producer is exercised
    directly here.
    """
    inplace = '"$REPO_ROOT/bin/build-dashboard-fixtures.py" >/dev/null\n'
    assert _inplace_dirs_in(inplace) == {"dashboard"}

    redirected = (
        '"$REPO_ROOT/bin/build-dashboard-fixtures.py" --out "$SCRATCH/x"\n'
    )
    assert _inplace_dirs_in(redirected) == set()

    cached = 'build_fixtures_cached "$REPO_ROOT/bin/build-share-fixtures.py" "$D"\n'
    assert _inplace_dirs_in(cached) == set()

    commented = '#  "$REPO_ROOT/bin/build-doctor-fixtures.py" >/dev/null\n'
    assert _inplace_dirs_in(commented) == set()

    staged = '%s doctor "$FIXTURES"\n' % STAGING_HELPER
    assert _inplace_dirs_in(staged) == set()
