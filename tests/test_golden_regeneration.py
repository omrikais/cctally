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

import os
import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


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
    return _staged_golden_writes(text)


def _staged_golden_writes(text: str) -> list[str]:
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
FIXTURE_STAGE=$(mktemp -d "${TMPDIR:-/tmp}/cctally-doctor-fixstage.XXXXXX")
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
FIXTURE_STAGE=$(mktemp -d "${TMPDIR:-/tmp}/cctally-doctor-fixstage.XXXXXX")
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
        offenders = goldens_addressed_at_staging(
            harness.read_text(encoding="utf-8"))
        if offenders:
            reported[harness.name] = offenders
    assert not reported, (
        "these harnesses build a golden path out of a directory their own EXIT "
        "trap removes, so regenerating a golden reports success and writes "
        "nothing that survives the run: %r. Address goldens at the committed "
        "root instead, as bin/cctally-share-v2-test does with GOLDEN_ROOT."
        % reported
    )


# #940 F2 fix round 2 (G4): the case-tree binding check over-approximates.
# A bare-word `d`: not part of a longer word, not a parameter expansion (`$d`,
# `${d…}`), an option (`-d`), a printf conversion (`%d`), or part of a path or
# extension (`/d`, `.d`, `d/`, `d.`).
_BARE_D = re.compile(r"(?<![A-Za-z0-9_$%/.\-])(?<!\$\{)d(?![A-Za-z0-9_/.])")
# `${d:=…}`, `${d=…}`, `${d:?…}` and `${d?…}` assign (or abort on) `d`.
_ASSIGNING_D = re.compile(r"\$\{d:?[=?]")
# What may follow an accepted binding's value: the end of the word.
_WORD_END = r"(?=$|[\s;&|)])"
# The three accepted bindings, matched at the bare `d`.
_NEW_TREE_D = re.compile(r"d=\$\(new_tree\s[^()]*\)" + _WORD_END)
_LOCAL_PREFIX = re.compile(r"(?:^|[\s;&|({!])local\s+$")
_LOCAL_POSITIONAL_D = re.compile(r"d=\$1" + _WORD_END)
_SCRATCH_DIR_D = re.compile(r'd="\$SCRATCH_DIR/[^"`]*"' + _WORD_END)
# A here-document operator (not a `<<<` here-string) and its delimiter word:
# `'TAG'`, `"TAG"`, `\TAG` (quoted, so the body is literal) or `TAG`.
_HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(?:'([^']*)'|\"([^\"]*)\"|\\(\w+)|(\w+))")


def _case_tree_scan_lines(text: str):
    """The text the binding check reads: ``(number, line)`` for every line,
    with full-line comments, trailing comments and quoted here-document
    bodies removed.

    Quote state is carried across lines (in single quotes only ``'`` ends
    the string; in double quotes ``\\`` escapes and ``"`` ends it), so a
    ``#`` or ``<<'TAG'`` inside a string is neither a comment nor a
    here-document. Each misreading this lexer can make errs toward reading
    more: a trailing comment whose text holds a quote, a backtick or a
    ``)`` is kept, since it may be a string the lexer mis-tracked; a quoted
    here-document whose delimiter line never comes is not skipped; and an
    unquoted body is read in full, comments included, because it expands
    ``${d:=…}``."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out = []
    state = None          # None (code), "'" or '"'
    pending = []          # [(tag, quoted, strip_tabs)] in operator order
    k = 0
    while k < len(lines):
        line = lines[k]
        number = k + 1
        k += 1
        if pending:
            tag, quoted, strip_tabs = pending[0]
            closing = line.lstrip("\t") if strip_tabs else line
            if closing == tag:
                pending.pop(0)
                continue
            if not quoted:
                out.append((number, line))
            continue
        if state is None and line.lstrip().startswith("#"):
            continue
        kept = line
        j = 0
        while j < len(line):
            ch = line[j]
            if state == "'":
                if ch == "'":
                    state = None
            elif state == '"':
                if ch == "\\":
                    j += 1
                elif ch == '"':
                    state = None
            elif ch == "\\":
                j += 1
            elif ch in "'\"":
                state = ch
            elif ch == "#" and (j == 0 or line[j - 1] in " \t"):
                comment = line[j:]
                if not any(c in comment for c in "'\"`)"):
                    kept = line[:j]
                break
            elif ch == "<":
                m = _HEREDOC.match(line, j)
                if m:
                    quoted_tag = m.group(2) if m.group(2) is not None else (
                        m.group(3) if m.group(3) is not None else m.group(4))
                    if quoted_tag is not None:
                        rest = lines[k:]
                        strip = bool(m.group(1))
                        if any((r.lstrip("\t") if strip else r) == quoted_tag for r in rest):
                            pending.append((quoted_tag, True, strip))
                        # An unclosed quoted body is read: nothing is skipped.
                    else:
                        pending.append((m.group(5), False, bool(m.group(1))))
                    j = m.end()
                    continue
            j += 1
        out.append((number, kept))
    return out


def _bare_d_is_accepted(line: str, at: int) -> bool:
    """Whether the bare `d` at ``at`` is the binding of exactly one accepted
    form: ``d=$(new_tree …)``, ``local d=$1`` or ``[local] d="$SCRATCH_DIR/…"``."""
    forms = 0
    if _NEW_TREE_D.match(line, at):
        forms += 1
    if _LOCAL_PREFIX.search(line[:at]) and _LOCAL_POSITIONAL_D.match(line, at):
        forms += 1
    if _SCRATCH_DIR_D.match(line, at):
        forms += 1
    return forms == 1


def _case_tree_binding_offenders(text: str) -> list[str]:
    """#940 F2-f, made total in fix round 2 (G4): every binding of a case's
    tree variable `d` must be a scratch tree the case allocated. The check
    does not list binding forms. It over-approximates, failing closed: a
    binding it missed is the harmful direction, and a false alarm is cheap.

    1. It skips full-line comments, trailing comments (a ``#`` after
       whitespace, outside quotes) and quoted here-document bodies
       (``<<'TAG'``, ``<<"TAG"``, ``<<\\TAG``, with or without ``-``), up to
       the closing line (see ``_case_tree_scan_lines``). An unquoted body is
       read, because it expands ``${d:=…}``.
    2. On every remaining line it finds each bare-word `d` (``_BARE_D``) and
       each assigning expansion ``${d:=…}``, ``${d=…}``, ``${d:?…}`` or
       ``${d?…}``. An assigning expansion is an offender whatever the line.
    3. A line is accepted only when each bare `d` in it is the binding of
       exactly one accepted form: ``d=$(new_tree …)``, ``local d=$1`` or
       ``d="$SCRATCH_DIR/…"`` (with or without ``local``).
    4. Every other line with a bare `d` is an offender. That includes a `d`
       in a quoted string, a false alarm on purpose: a fixture that needs
       `d` as a word uses another name."""
    offenders = []
    source = text.split("\n")
    for number, line in _case_tree_scan_lines(text):
        offending = _ASSIGNING_D.search(line) is not None or any(
            not _bare_d_is_accepted(line, m.start()) for m in _BARE_D.finditer(line))
        if offending:
            offenders.append("%d: %s" % (number, source[number - 1].strip()))
    return offenders


def test_every_case_tree_binding_is_a_scratch_tree():
    """#940 F2-f: every pre-flight case writes its fixtures into `$d`, so `$d`
    must be the case's own scratch tree; a binding of `d` to anything else
    would make a fixture write land outside it."""
    text = (REPO / "bin" / "cctally-preflight-test").read_text(encoding="utf-8")
    assert _case_tree_binding_offenders(text) == []
    assert text.count("d=$(new_tree ") > 20  # the scan reads the real bindings
    # The rule bites: other bindings are reported.
    assert _case_tree_binding_offenders('d="$REPO_ROOT/x"\nlocal d=/tmp/y\n') == [
        '1: d="$REPO_ROOT/x"', "2: local d=/tmp/y"]
    assert _case_tree_binding_offenders("local d=$1 mode=$2\n") == []
    # #940 F2 fix round 2 (G4): a bare `d` in quoted fixture text is reported
    # on purpose (the rule over-approximates); such a fixture uses another name.
    assert _case_tree_binding_offenders('printf x "      - run: d=$(pwd)"\n') == [
        '1: printf x "      - run: d=$(pwd)"']


@pytest.mark.parametrize("line", [
    pytest.param("for d in /tmp/a /tmp/b; do", id="for-loop"),
    pytest.param("    for d in \"$SCRATCH_DIR\"/*; do", id="for-loop-indented"),
    pytest.param("true; for d in x; do :; done", id="for-loop-mid-line"),
    pytest.param("read d < /tmp/f", id="read"),
    pytest.param("read -r d", id="read-r"),
    pytest.param("IFS= read -r name d", id="read-second-name"),
    pytest.param("while read -r d; do", id="while-read"),
    pytest.param("x=1; d=/tmp/y", id="semicolon"),
    pytest.param("[ -n x ] && d=$(pwd)", id="and"),
    pytest.param("[ -n x ] || d=\"$REPO_ROOT/x\"", id="or"),
])
def test_each_other_binding_form_of_the_case_tree_is_an_offender(line):
    """#940 F2 fix round (F2X-5): a `for d in`, a `read … d` and a mid-line
    `; d=`, `&& d=` or `|| d=` also bind `d`; each one that is not one of the
    three accepted forms is reported."""
    assert _case_tree_binding_offenders(line + "\n") == ["1: %s" % line.strip()]


@pytest.mark.parametrize("line", [
    pytest.param("x=1; d=$(new_tree y)", id="semicolon-new-tree"),
    pytest.param("[ -n x ] && d=\"$SCRATCH_DIR/z\"", id="and-scratch"),
    pytest.param("read -r dd", id="read-another-name"),
    pytest.param("for dir in a b; do", id="for-another-name"),
    pytest.param("# for d in a b; read d; x; d=/tmp", id="comment"),
])
def test_the_other_binding_forms_accept_the_scratch_forms(line):
    assert _case_tree_binding_offenders(line + "\n") == []


@pytest.mark.parametrize("text", [
    pytest.param("if read -r d; then", id="if-read"),
    pytest.param("elif read d; then", id="elif-read"),
    pytest.param("command read d", id="command-read"),
    pytest.param("x; local d=/tmp", id="mid-line-local"),
    pytest.param("then d=/tmp/a", id="then-assign"),
    pytest.param("printf -v d %s /tmp", id="printf-v"),
    pytest.param("mapfile d < f", id="mapfile"),
    pytest.param("readarray -t d < f", id="readarray"),
    pytest.param("select d in a b; do", id="select"),
    pytest.param("(( d++ ))", id="arithmetic"),
    pytest.param(": ${d:=/tmp/a}", id="assign-default-colon"),
    pytest.param(": ${d=/tmp/a}", id="assign-default"),
    pytest.param(': "${d:?unset}"', id="error-if-unset-colon"),
    pytest.param(": ${d?unset}", id="error-if-unset"),
    pytest.param("d+=/x", id="append"),
    pytest.param("export d=/tmp/a", id="export"),
    pytest.param("declare d=/tmp/a", id="declare"),
    pytest.param("x=1 d=/tmp/a", id="second-prefix-assignment"),
    pytest.param("d=$(new_tree x); : ${d:=/tmp}", id="assigning-expansion-beside-a-binding"),
    pytest.param("local d=$1x", id="local-positional-with-suffix"),
    pytest.param("d=$(new_tree x)/sub", id="new-tree-with-suffix"),
    pytest.param("foo  # it's (read d)", id="trailing-comment-with-a-quote"),
    pytest.param('x="$(cat "a # b")"; read d', id="hash-inside-a-nested-quote"),
])
def test_every_binding_form_of_the_case_tree_is_an_offender(text):
    """#940 F2 fix round 2 (G4): the rule over-approximates rather than
    listing binding forms, so each of these is reported."""
    assert _case_tree_binding_offenders(text + "\n") == ["1: %s" % text.strip()]


def test_an_unquoted_here_document_body_is_read():
    """#940 F2 fix round 2 (G4): an unquoted here-document expands its body,
    so ``${d:=…}`` there assigns ``d``; only a quoted body is skipped."""
    text = "cat <<EOF\n${d:=/tmp/a}\nEOF\n"
    assert _case_tree_binding_offenders(text) == ["2: ${d:=/tmp/a}"]


@pytest.mark.parametrize("text", [
    pytest.param("d=$(new_tree x)\n", id="new-tree"),
    pytest.param("    local d=$1\n", id="local-positional"),
    pytest.param('    local d="$SCRATCH_DIR/$1-tool"\n', id="local-scratch"),
    pytest.param('printf "%s\\n" "$d" "${d}" "${d%/}"\n', id="parameter-expansions"),
    pytest.param('cp a "$d/bin/x"\n', id="path-use"),
    pytest.param("[ -d x ] && ls -d y\n", id="option"),
    pytest.param("printf '%d\\n' 3\n", id="printf-conversion"),
    pytest.param("ls a/d d/b x.d d.x\n", id="path-and-extension"),
    pytest.param("foo  # see read d\n", id="trailing-comment"),
    pytest.param("  # for d in a b; read d\n", id="full-line-comment"),
    pytest.param("cat <<'EOF'\nfor d in a b:\nEOF\n", id="single-quoted-here-document"),
    pytest.param('cat <<"EOF" >x\nread d\nEOF\n', id="double-quoted-here-document"),
    pytest.param("cat <<\\EOF\nd=/tmp\nEOF\n", id="escaped-here-document"),
    pytest.param("cat <<-'EOF'\n\tfor d in a b:\n\tEOF\n", id="tab-stripped-here-document"),
])
def test_the_scratch_forms_and_non_bindings_stay_clean(text):
    """#940 F2 fix round 2 (G4): the three accepted bindings, expansions,
    options, conversions, paths, comments and quoted here-document bodies
    are not offenders."""
    assert _case_tree_binding_offenders(text) == []


def test_a_quoted_here_document_skip_ends_at_its_delimiter():
    """#940 F2 fix round 2 (G4): the skip covers the body only; the line
    after the closing delimiter is read again."""
    text = "cat <<'EOF'\nfor d in a b:\nEOF\nread d\n"
    assert _case_tree_binding_offenders(text) == ["4: read d"]


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
def test_regenerating_rewrites_the_committed_goldens(tmp_path):
    """The check the static scan cannot make: regenerated bytes come back.

    `bin/cctally-pricing-check-test` is the smallest of the affected harnesses,
    and it exercises both write shapes — a golden held in a variable and an exit
    code redirected straight onto a path. The test copies the committed golden
    tree to scratch, corrupts every copied golden, points the harness's explicit
    regeneration seam at that independent tree, and requires the original bytes
    to come back. The live checkout is read-only throughout, so an xdist peer or
    whole-tree reader can never observe this test's deliberate corruption.
    """
    assert "CCTALLY_PRICING_CHECK_GOLDEN_ROOT" in _REGEN_HARNESS.read_text(), (
        "the pricing-check harness needs a scratch-golden override before this "
        "test can stop mutating the live repository tree"
    )

    original = _committed_goldens()
    assert original, "no committed golden to regenerate"
    scratch_goldens = tmp_path / "pricing-check"
    shutil.copytree(_REGEN_FIXTURES, scratch_goldens)
    copied = {
        scratch_goldens / path.relative_to(_REGEN_FIXTURES): data
        for path, data in original.items()
    }
    for path in copied:
        path.write_bytes(b"corrupted by test_regenerating_rewrites\n")

    proc = subprocess.run(
        [str(_REGEN_HARNESS)],
        cwd=str(REPO),
        env=dict(
            os.environ,
            CCTALLY_PRICING_CHECK_REGENERATE="1",
            CCTALLY_PRICING_CHECK_GOLDEN_ROOT=str(scratch_goldens),
            TZ="Etc/UTC",
        ),
        capture_output=True,
        text=True,
        timeout=110,
    )
    rewritten = {path: path.read_bytes() for path in copied}
    unchanged = sorted(
        str(path.relative_to(scratch_goldens))
        for path, data in rewritten.items()
        if data == b"corrupted by test_regenerating_rewrites\n"
    )
    assert not unchanged, (
        "regenerate mode reported success but never wrote these scratch "
        "goldens: %s\n--- harness stdout ---\n%s"
        % (unchanged, proc.stdout[-3000:])
    )
    differing = sorted(
        str(path.relative_to(scratch_goldens))
        for path, data in rewritten.items()
        if data != copied[path]
    )
    assert not differing, (
        "regeneration did not reproduce the committed bytes for %s, so "
        "the committed goldens are stale relative to the code" % differing
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert _committed_goldens() == original, (
        "scratch regeneration changed the live committed fixture tree"
    )
