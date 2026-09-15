"""Eight fixture builders: `--out`, and the committed set is current.

Six harnesses used to rebuild their fixtures IN PLACE on every run — 676 tracked
files rewritten by a test run, and nothing asserting the tree came back
unchanged. That is both a source of interference for anything else reading the
tree concurrently and a way for a fixture to drift from its builder without
anybody noticing.

Two things are asserted here, and they are complements:

- `--out DIR` exists on all eight and redirects everything the builder writes,
  so a harness can build into scratch and leave the tracked tree alone.
- Rebuilding into scratch reproduces the COMMITTED tree, so the committed
  fixtures are provably what the builders currently produce.

The second of those runs under two policies, because the eight builders divide
into two families that mean different things by "the committed tree".

`HOLDOUTS` — the six — compare SEMANTICALLY over the INTERSECTION of the
git-tracked paths and the produced paths. The intersection is what these six
need: a committed file the builder does not produce is a golden or a
hand-maintained input, and a produced file that is not tracked is gitignored
working state, so demanding either would redden permanently. The comparison is
semantic because these builders generate SQLite. `bin/_fixture_cache.py` hashes
the SQLite version, its compile options, FTS5 availability and the Python
identity into its cache key precisely because builder output is
toolchain-sensitive; a raw byte comparison of a generated SQLite file would
therefore fail on any runner whose SQLite differs and turn a detector into an
outage.

`EXACT_TREE_BUILDERS` — `release` and `mirror-public` — compare BYTE FOR BYTE
over the UNION. Their builders own every committed path, so neither exemption
above applies, and the union is what reports a stale committed golden the
builder no longer declares. The intersection drops exactly that case, which is
one of the two drifts issue #646 was filed for. These two builders write text
rather than SQLite, so a byte comparison has nothing toolchain-sensitive to
trip over, and `exact_manifest()` therefore carries no ignored name and no
ignored suffix. The one committed root either builder does not produce is named
in `EXACT_TREE_EXCLUDED_ROOTS` and filtered by the CALLER, which is what stops
the helper acquiring a hidden exemption of its own.

Both manifests carry the executable bit, because `bin/build-doctor-fixtures.py`
deliberately `chmod(0o755)`s one of its outputs and a comparison ignoring mode
would miss that regression.

Two specific things make the SQLite comparison portable, and it is worth being
exact about them because they are the only two. Sorting the dump removes page
layout. Dropping each virtual table's shadow tables removes FTS5's on-disk
index format, which the generated `cache.db` files carry and which is chosen by
the FTS5 implementation rather than by the data. Nothing else is removed, so a
toolchain that changed a stored value or a schema still fails the comparison.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
import contextlib
import difflib
import fcntl
import hashlib
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import types

import pytest
from _fts5_gate import requires_fts5  # the ONE FTS5 capability gate (#630 S6)

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
FIXTURES = REPO / "tests" / "fixtures"

# The six that built in place. Each name is both the builder's infix and its
# fixture directory under tests/fixtures/.
HOLDOUTS = (
    "conversation",
    "dashboard",
    "doctor",
    "pricing-check",
    "share",
    "share-v2",
)

# The two families whose builders own EVERY committed path under their fixture
# tree. They compare byte for byte over the union of tracked and generated
# paths, where the six above compare semantically over the intersection. The
# intersection's two exemptions — a committed file the builder does not produce
# is a hand-maintained golden, a produced file that is not tracked is gitignored
# working state — are both true of the six and neither is true of these.
EXACT_TREE_BUILDERS = ("mirror-public", "release")

CONTRACT_BUILDERS = HOLDOUTS + EXACT_TREE_BUILDERS

#: Committed roots an exact-tree builder does not produce, by exact name.
#: `tests/fixtures/release/_assets/` holds the shared fake archive the harness
#: exports as CCTALLY_RELEASE_BREW_ARCHIVE_URL — a harness input, not builder
#: output. Never derive this from `tracked - produced`: that computation would
#: bless the drift this comparison exists to detect.
EXACT_TREE_EXCLUDED_ROOTS = {
    "mirror-public": frozenset(),
    "release": frozenset({"_assets"}),
}

# Written by the builder's own environment rather than by its logic, so they are
# not part of what "the committed set" means.
_IGNORED_SUFFIXES = (".db-wal", ".db-shm")
_IGNORED_NAMES = frozenset({".DS_Store"})

# SQLite compares through a canonical dump; everything else by content.
_SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def builder_for(name: str) -> pathlib.Path:
    return BIN / f"build-{name}-fixtures.py"


def _skip_if_absent(name: str) -> None:
    if not builder_for(name).exists():
        pytest.skip(f"builder for {name} is not present in this checkout")


def _git_tracks(root: pathlib.Path, relpath: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relpath],
        capture_output=True, text=True,
    )
    return proc.returncode == 0


def missing_builders(root: pathlib.Path, names=CONTRACT_BUILDERS) -> list[str]:
    """The holdouts this checkout is supposed to carry but does not.

    "Supposed to" is answered by git, not by `HOLDOUTS`. This file is published
    to the public mirror and `bin/build-share-v2-fixtures.py` is not, so in the
    public repository that builder is legitimately absent; a flat existence
    assertion would fail a lane that is behaving correctly. A builder git tracks
    and the working tree lacks is a real defect and is still reported.
    """
    return [
        name
        for name in names
        if not (root / "bin" / f"build-{name}-fixtures.py").exists()
        and _git_tracks(root, f"bin/build-{name}-fixtures.py")
    ]


# Isolation knobs, which change where a builder looks rather than what it
# writes. Everything else beginning with `CCTALLY_` is dropped.
_KEEP_CCTALLY_ENV = ("CCTALLY_DISABLE_DEV_AUTODETECT",)


def builder_env() -> dict[str, str]:
    """A builder environment that does not depend on the developer's shell.

    `bin/build-doctor-fixtures.py` reads `CCTALLY_AS_OF` at five places and
    falls back to a fixed date, so a maintainer who exports it — and it is the
    documented hook `bin/cctally-project-test` depends on — rebuilds different
    fixtures than CI does, and this contract reddens on their machine with
    nothing wrong in the repository. Every `CCTALLY_*` name is dropped rather
    than that one specifically, because the next builder to read a knob would
    reintroduce the same failure silently.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CCTALLY_") or key in _KEEP_CCTALLY_ENV
    }
    env["TZ"] = "Etc/UTC"
    env["LC_ALL"] = "C"
    env["CCTALLY_FIXTURE_CACHE"] = "0"
    return env


def _run_builder(name: str, out: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(builder_for(name)), "--out", str(out)],
        cwd=str(REPO), env=builder_env(), capture_output=True, text=True,
    )


def _tracked_status(paths: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain", "--", *paths],
        capture_output=True, text=True,
    )
    return proc.stdout


# --------------------------------------------------------------- the manifest


_STATEMENT_TARGET = re.compile(
    r"""^\s*(?:CREATE\s+TABLE|INSERT\s+INTO)\s+(?:"([^"]+)"|'([^']+)'|(\w+))""",
    re.IGNORECASE,
)


def _statement_target(statement: str) -> str | None:
    match = _STATEMENT_TARGET.match(statement)
    if match is None:
        return None
    return match.group(1) or match.group(2) or match.group(3)


#: The suffixes SQLite's full-text extensions give a virtual table's shadow
#: tables. Enumerated rather than inferred from the prefix alone: a real table
#: sharing a virtual table's name as its prefix — `conversation_messages`
#: beside a `conversation` virtual table — was dropped from the comparison
#: entirely, and nothing reported that it had been.
_SHADOW_SUFFIXES = frozenset({
    "data", "idx", "content", "docsize", "config",   # FTS5
    "segments", "segdir", "stat",                    # FTS3/4
})


def _shadow_tables(rows: list[tuple[str, str | None]]) -> set[str]:
    """The tables SQLite maintains for a virtual table, keyed off ROWS.

    A virtual table's shadow tables are named `<vtab>_<suffix>` for a known set
    of suffixes, so the virtual tables in `sqlite_master` and that set together
    identify them; the `_fts_` infix is not relied on, because it is FTS5's
    naming convention rather than a rule. A `<vtab>_`-prefixed table with any
    other suffix stays in the comparison, so a real table is never silently
    excluded and a suffix a future SQLite adds fails loudly instead.
    """
    virtual = {
        name
        for name, sql in rows
        if sql and sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")
    }
    return {
        name
        for name, _ in rows
        for vtab in virtual
        if name.startswith(f"{vtab}_")
        and name[len(vtab) + 1:] in _SHADOW_SUFFIXES
    }


def _sqlite_canonical(path: pathlib.Path) -> str:
    """Schema and rows as text, ordered, minus each virtual table's index state.

    Two things are removed, and only two. Ordering the dump removes page
    layout, which the SQLite version, its compile options and its page size all
    move without any of the DATA changing. Dropping the shadow tables removes
    FTS5's on-disk index format — a format version in `<vtab>_config` and packed
    blobs in `<vtab>_data`, `<vtab>_idx` and `<vtab>_docsize`, whose bytes FTS5
    chooses and which two builds can write differently for identical searchable
    content. Every FTS5 table in this repository is external-content
    (`content='<base table>'`), so the indexed rows themselves live in an
    ordinary table that the dump still covers in full.

    What is NOT removed: everything else. A different SQLite build that changed
    a stored value, a schema, a row count or a column type still fails the
    comparison, which is the point of comparing at all.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = list(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
        ))
        shadow = _shadow_tables(rows)
        return "\n".join(sorted(
            statement for statement in conn.iterdump()
            if _statement_target(statement) not in shadow
        ))
    finally:
        conn.close()


def _entry_digest(path: pathlib.Path) -> str:
    if path.suffix in _SQLITE_SUFFIXES:
        try:
            return "sqlite:" + hashlib.sha256(
                _sqlite_canonical(path).encode("utf-8")
            ).hexdigest()
        except sqlite3.DatabaseError:
            pass  # Not a database after all; fall through to the byte digest.
    return "bytes:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _walk_manifest(root: pathlib.Path, digest, ignored_names, ignored_suffixes):
    """`{relative path: type + mode + content}` for one fixture tree.

    Never follows a symlink, and records the target rather than what it points
    at. The executable bit is carried, and nothing else of the mode is, so a
    difference in umask between two runners is not reported as drift while a
    deliberate `chmod(0o755)` still is.

    The two wrappers below differ only in DIGEST and in what they ignore. The
    traversal, the type encoding and the executable bit are shared, so a family
    that compares bytes and a family that compares semantically still disagree
    about the same set of things.
    """
    entries: dict[str, str] = {}
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        here = pathlib.Path(current)
        for name in sorted(dirnames + filenames):
            path = here / name
            rel = str(path.relative_to(root))
            if name in ignored_names or (
                ignored_suffixes and rel.endswith(ignored_suffixes)
            ):
                continue
            if path.is_symlink():
                entries[rel] = "l:%s" % os.readlink(path)
                continue
            if path.is_dir():
                entries[rel] = "d:"
                continue
            executable = "x" if os.access(path, os.X_OK) else "-"
            entries[rel] = "f:%s:%s" % (executable, digest(path))
    return entries


def manifest(root: pathlib.Path) -> dict[str, str]:
    """The six holdouts' policy: semantic for SQLite, sidecars ignored."""
    return _walk_manifest(root, _entry_digest, _IGNORED_NAMES, _IGNORED_SUFFIXES)


def _exact_digest(path: pathlib.Path) -> str:
    return "bytes:" + hashlib.sha256(path.read_bytes()).hexdigest()


def exact_manifest(root: pathlib.Path) -> dict[str, str]:
    """Byte equality, with nothing exempt.

    No ignored name and no ignored suffix, because an exemption here is a path
    the comparison stops seeing, and the two families that use this helper have
    builders that own every committed path. Exclusions are applied by the
    CALLER against a named constant, never in here, so this helper cannot
    acquire a hidden one.
    """
    return _walk_manifest(root, _exact_digest, frozenset(), ())


def diff_manifests(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    lines = []
    for rel in sorted(set(expected) | set(actual)):
        if rel not in actual:
            lines.append(f"missing from the rebuild: {rel}")
        elif rel not in expected:
            lines.append(f"present only in the rebuild: {rel}")
        elif expected[rel] != actual[rel]:
            lines.append(f"differs: {rel} ({expected[rel]} vs {actual[rel]})")
    return lines


_EXACT_CATEGORIES = (
    "missing",
    "unexpected",
    "file-type-different",
    "content-different",
    "executable-bit-different",
)


def _entry_kind(value: str) -> str:
    """`d`, `l` or `f` — the first field of a manifest value."""
    return value.split(":", 1)[0]


def classify_exact_differences(committed, produced):
    """Sorted `(category, relpath)` for every path the two trees disagree on.

    The type check comes first because a path that changed from a file to a
    directory has no content and no executable bit to compare, so asking about
    either would report a difference in the wrong terms.
    """
    found = []
    for rel in sorted(set(committed) | set(produced)):
        if rel not in produced:
            found.append(("missing", rel))
            continue
        if rel not in committed:
            found.append(("unexpected", rel))
            continue
        want, have = committed[rel], produced[rel]
        if want == have:
            continue
        if _entry_kind(want) != _entry_kind(have):
            found.append(("file-type-different", rel))
            continue
        if _entry_kind(want) != "f":
            found.append(("content-different", rel))
            continue
        _, want_mode, want_digest = want.split(":", 2)
        _, have_mode, have_digest = have.split(":", 2)
        if want_digest != have_digest:
            found.append(("content-different", rel))
        if want_mode != have_mode:
            found.append(("executable-bit-different", rel))
    return sorted(found)


#: How many content mismatches get a unified diff before the rest are counted.
#: The live release drift is 82 files at once, so rendering all of them would
#: bury the inventory that says which paths are involved.
_EXACT_DIFF_LIMIT = 5
#: Lines per rendered diff. A fixture `setup.sh` is ~280 lines and two of them
#: side by side is not a diagnostic.
_EXACT_DIFF_MAX_LINES = 60


def _readable_lines(path: pathlib.Path) -> list[str] | None:
    """The file's lines with terminators intact, or None when not UTF-8 text.

    Decoded from bytes rather than read as text, because `read_text` applies
    universal-newline translation: a CRLF file and its LF twin would produce
    equal line lists, and their unified diff would be empty.
    """
    try:
        return path.read_bytes().decode("utf-8").splitlines(keepends=True)
    except (UnicodeDecodeError, OSError):
        return None


#: Longest first, so a CRLF line is not reported as an LF one.
_LINE_TERMINATORS = (("CRLF", "\r\n"), ("CR", "\r"), ("LF", "\n"))


def _terminator_names(lines: Sequence[str]) -> str:
    """The line terminators present, in first-seen order; `none` for the rest."""
    seen: list[str] = []
    for line in lines:
        name = next(
            (name for name, end in _LINE_TERMINATORS if line.endswith(end)),
            "none",
        )
        if name not in seen:
            seen.append(name)
    return "+".join(seen) or "none"


def _differs_only_in_terminators(
    want: Sequence[str], have: Sequence[str],
) -> bool:
    """True when stripping every line's terminator makes the two lists equal.

    Each element of a `splitlines(keepends=True)` list is its content followed
    by a line boundary, so the content itself can never end in CR or LF.
    """
    return [line.rstrip("\r\n") for line in want] == [
        line.rstrip("\r\n") for line in have
    ]


def _normalized_diff_lines(lines: Sequence[str]) -> list[str]:
    """Lines safe for a terminal diff while preserving logical boundaries."""
    normalized = []
    for line in lines:
        if line.endswith(("\r", "\n")):
            normalized.append(line.rstrip("\r\n") + "\n")
        else:
            normalized.append(line)
    return normalized


def _short_digest(path: pathlib.Path) -> str:
    """Twelve hex digits of the file's SHA-256, or why there are none.

    This runs only inside a failure diagnostic, so it must not raise: an
    exception here would replace the report the guard exists to print.
    """
    try:
        return _exact_digest(path)[6:18]
    except OSError as exc:
        return f"<unreadable: {exc.__class__.__name__}>"


def _link_target(value: str) -> str:
    """The target an `l:` manifest value records, or the value unchanged."""
    kind, _, rest = value.partition(":")
    return rest if kind == "l" else value


def render_exact_mismatch(name, committed_root, produced_root, differences,
                          committed, produced):
    path_count = len({rel for _, rel in differences})
    difference_count = len(differences)
    count_suffix = (
        f" ({difference_count} actionable differences)"
        if difference_count != path_count else ""
    )
    parts = [
        f"tests/fixtures/{name} is not what bin/build-{name}-fixtures.py "
        f"produces today: {path_count} path(s) differ{count_suffix}.",
        "",
        "every differing path:",
    ]
    for category, rel in differences:
        entry = f"  {category} {rel}"
        if category == "executable-bit-different":
            _, have_mode, _ = produced[rel].split(":", 2)
            direction = "+x" if have_mode == "x" else "-x"
            entry += (
                f" — run chmod {direction} tests/fixtures/{name}/{rel}"
            )
        parts.append(entry)

    content = [rel for category, rel in differences if category == "content-different"]
    shown = 0
    for rel in content:
        if shown >= _EXACT_DIFF_LIMIT:
            break
        want_value, have_value = committed.get(rel, ""), produced.get(rel, "")
        parts.append("")
        shown += 1
        if "l" in (_entry_kind(want_value), _entry_kind(have_value)):
            # Read from the manifest, not the filesystem: reading through the
            # link reports a difference between two files nothing compared, and
            # a dangling link has no bytes to read at all.
            parts.append(
                f"  {rel}: symlink target {_link_target(want_value)} "
                f"vs {_link_target(have_value)}"
            )
            continue
        want = _readable_lines(committed_root / rel)
        have = _readable_lines(produced_root / rel)
        if want is None or have is None:
            parts.append(
                f"  {rel}: not UTF-8 text; "
                f"committed sha256 {_short_digest(committed_root / rel)} "
                f"vs rebuilt sha256 {_short_digest(produced_root / rel)}"
            )
            continue
        if _differs_only_in_terminators(want, have):
            parts.append(
                f"  {rel}: differs only in line terminators; "
                f"committed {_terminator_names(want)} "
                f"vs rebuilt {_terminator_names(have)}"
            )
            continue
        diff = list(difflib.unified_diff(
            _normalized_diff_lines(want),
            _normalized_diff_lines(have),
            fromfile=f"committed/{rel}",
            tofile=f"rebuilt/{rel}",
        ))
        capped = diff[:_EXACT_DIFF_MAX_LINES]
        parts.append("".join(capped).rstrip("\n"))
        if len(diff) > _EXACT_DIFF_MAX_LINES:
            parts.append(
                f"  ... diff truncated at {_EXACT_DIFF_MAX_LINES} lines "
                f"({len(diff)} total)")

    remaining = len(content) - shown
    if remaining > 0:
        parts.append("")
        parts.append(
            f"  ... {remaining} further content diff(s) not rendered; every "
            f"path is listed in the inventory above."
        )

    blocking_directories = [
        rel for category, rel in differences
        if category == "file-type-different"
        and _entry_kind(committed.get(rel, "")) == "d"
    ]
    if blocking_directories:
        parts += [
            "",
            "the committed side is a directory at these paths, so the rebuild "
            "would stop with IsADirectoryError:",
        ]
        parts += [
            f"  remove tests/fixtures/{name}/{rel} by hand before regenerating"
            for rel in blocking_directories
        ]

    parts += [
        "",
        f"to fix, from the repository root: python3 bin/build-{name}-fixtures.py",
        "then review the result and commit it.",
        "",
        "the builders do not prune unowned paths recursively. they do perform "
        "targeted unlinks for optional goldens they still name, and they chmod "
        "setup.sh and run.sh and no other path:",
        "  content-different: content is repaired, in a golden as much as in a "
        "script. a separately listed mode difference still follows its own "
        "remedy below.",
        "  unexpected: repaired — commit the regenerated result.",
        "  missing: repaired when it is an optional golden the builder still "
        "names, which the rebuild unlinks. a path the builder no longer names "
        "at all — an obsolete scenario directory, say — survives, and must be "
        "reviewed and removed by hand.",
        "  executable-bit-different: repaired on setup.sh and run.sh only. for "
        "every other path, run the path-specific chmod command above.",
        "  file-type-different: never repaired. replace the committed path by "
        "hand; a committed directory would raise IsADirectoryError, so those "
        "blockers are listed before the rebuild command.",
    ]
    return "\n".join(parts)


# ------------------------------------------------------------------ Task 14


def _out_argument(name: str) -> ast.keyword | None:
    tree = ast.parse(builder_for(name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == "--out":
            return node
    return None


@pytest.mark.parametrize("name", CONTRACT_BUILDERS)
def test_the_builder_declares_out_and_defaults_to_the_committed_root(name):
    """`default=None` is what makes omitting the flag keep the committed root.

    Asserted on the declaration rather than by running the builder without the
    flag, because running it without the flag is exactly the in-place rebuild
    this work exists to stop.
    """
    _skip_if_absent(name)
    call = _out_argument(name)
    assert call is not None, f"build-{name}-fixtures.py does not accept --out"
    defaults = [kw for kw in call.keywords if kw.arg == "default"]
    assert defaults, "--out must declare an explicit default"
    assert isinstance(defaults[0].value, ast.Constant)
    assert defaults[0].value.value is None, (
        "--out must default to None, which is what means 'the committed root'"
    )


@contextlib.contextmanager
def _fixture_tree_lock(name: str):
    """Held while this test's `git status` readings must mean what they say.

    `tests/test_golden_regeneration.py` corrupts every committed golden under
    `tests/fixtures/pricing-check` for the length of a harness run, and the
    pytest phase runs under xdist, so the two tests can be in different
    processes at the same moment. Without this the before/after comparison below
    reports that comparison's corruption as a write by the builder. Both sides
    derive the same path from the repository root.
    """
    digest = hashlib.sha1(str(REPO).encode("utf-8")).hexdigest()[:12]
    path = pathlib.Path(tempfile.gettempdir()) / (
        "cctally-fixture-tree.%s.%s.lock" % (name, digest)
    )
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


@pytest.mark.parametrize("name", CONTRACT_BUILDERS)
def test_out_redirects_every_write_and_leaves_the_tracked_tree_alone(name, tmp_path):
    _skip_if_absent(name)
    tracked = f"tests/fixtures/{name}"
    with _fixture_tree_lock(name):
        before = _tracked_status([tracked])
        out = tmp_path / name
        proc = _run_builder(name, out)
        assert proc.returncode == 0, proc.stderr[-4000:]
        assert out.is_dir(), (
            f"--out directory was not created by build-{name}-fixtures.py"
        )
        produced = manifest(out)
        assert produced, f"build-{name}-fixtures.py wrote nothing under --out"
        assert _tracked_status([tracked]) == before, (
            f"build-{name}-fixtures.py --out still wrote into {tracked}"
        )


# ------------------------------------------------------------------ Task 16


def _tracked_relpaths(name: str) -> set[str]:
    proc = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--", f"tests/fixtures/{name}"],
        capture_output=True, text=True,
    )
    prefix = f"tests/fixtures/{name}/"
    return {
        rel[len(prefix):]
        for rel in proc.stdout.split("\0")
        if rel.startswith(prefix)
    }


@pytest.mark.parametrize("name", HOLDOUTS)
def test_the_committed_fixtures_are_what_the_builder_produces_today(name, tmp_path):
    """A rebuild into scratch must reproduce every committed file it owns.

    This is the check that makes building out of tree safe: nothing rewrites the
    committed fixtures any more, so something has to prove they are still
    current. It found the drift it was written for — a schema column the
    builders had gained and the committed databases had not.

    Scoped to the intersection of "git-tracked" and "the builder produces it",
    and both halves are needed. A committed file the builder does not produce is
    a golden or a hand-maintained input, and demanding the builder reproduce it
    would redden permanently. A produced file that is not tracked is gitignored
    working state, absent from a fresh clone, and demanding it be committed
    would redden there instead. The intersection is asserted non-empty, because
    a comparison over nothing passes without checking anything.
    """
    _skip_if_absent(name)
    committed = FIXTURES / name
    if not committed.is_dir():
        pytest.skip(f"no committed fixture tree at tests/fixtures/{name}")
    tracked = _tracked_relpaths(name)
    if not tracked:
        pytest.skip("git could not list the committed fixture files")
    out = tmp_path / name
    proc = _run_builder(name, out)
    assert proc.returncode == 0, proc.stderr[-4000:]

    produced = manifest(out)
    have = manifest(committed)
    shared = sorted(tracked & set(produced))
    assert shared, (
        f"no committed file under tests/fixtures/{name} is produced by "
        f"bin/build-{name}-fixtures.py, so this comparison would check nothing"
    )
    differences = diff_manifests(
        {rel: have.get(rel, "<absent>") for rel in shared},
        {rel: produced[rel] for rel in shared},
    )
    assert not differences, (
        f"tests/fixtures/{name} is not what bin/build-{name}-fixtures.py produces "
        f"today ({len(shared)} committed files compared). Rebuild it and commit "
        f"the result:\n  " + "\n  ".join(differences)
    )


def _excluded(name: str, rel: str) -> bool:
    roots = EXACT_TREE_EXCLUDED_ROOTS[name]
    return any(rel == root or rel.startswith(f"{root}/") for root in roots)


@pytest.mark.parametrize("name", EXACT_TREE_BUILDERS)
def test_the_exact_tree_is_what_the_builder_produces_today(name, tmp_path):
    """Byte equality over the UNION, not the intersection.

    The intersection is what hides the second of the two drifts issue #646 was
    filed for: a stale committed optional golden the builder no longer declares
    is committed-but-not-produced, which the intersection drops. The union
    reports it as `missing`, which is what it is.
    """
    _skip_if_absent(name)
    committed_root = FIXTURES / name
    assert committed_root.is_dir(), (
        f"bin/build-{name}-fixtures.py is present but tests/fixtures/{name} is "
        f"not; the comparison has nothing to compare against"
    )
    tracked = {rel for rel in _tracked_relpaths(name) if not _excluded(name, rel)}
    assert tracked, (
        f"git tracks no file under tests/fixtures/{name} outside "
        f"{sorted(EXACT_TREE_EXCLUDED_ROOTS[name])}, so this would check nothing"
    )

    out = tmp_path / name
    proc = _run_builder(name, out)
    assert proc.returncode == 0, proc.stderr[-4000:]
    produced = exact_manifest(out)
    assert [rel for rel, value in produced.items() if value != "d:"], (
        f"bin/build-{name}-fixtures.py wrote no file under --out"
    )

    committed = {
        rel: value
        for rel, value in exact_manifest(committed_root).items()
        if not _excluded(name, rel)
    }
    differences = classify_exact_differences(committed, produced)
    assert not differences, render_exact_mismatch(
        name, committed_root, out, differences, committed, produced)


# Each of the three assertions above is driven to failure below, through the
# test itself rather than through a copy: a copy would prove that the copy fails.


def _self() -> types.ModuleType:
    return sys.modules[__name__]


@pytest.mark.parametrize("name", EXACT_TREE_BUILDERS)
def test_a_missing_committed_root_fails_the_exact_comparison(
    name, tmp_path, monkeypatch,
):
    """Guard one. A builder on disk whose committed tree has been deleted."""
    _skip_if_absent(name)
    monkeypatch.setattr(_self(), "FIXTURES", tmp_path / "no-fixtures")
    with pytest.raises(AssertionError, match="nothing to compare against"):
        test_the_exact_tree_is_what_the_builder_produces_today(
            name=name, tmp_path=tmp_path / "run")


@pytest.mark.parametrize("name", EXACT_TREE_BUILDERS)
def test_an_empty_tracked_side_fails_rather_than_skipping(
    name, tmp_path, monkeypatch,
):
    """Guard two. Skipping here would pass a checkout that tracks nothing."""
    _skip_if_absent(name)
    monkeypatch.setattr(_self(), "_tracked_relpaths", lambda _name: set())
    with pytest.raises(AssertionError, match="would check nothing"):
        test_the_exact_tree_is_what_the_builder_produces_today(
            name=name, tmp_path=tmp_path / "run")


@pytest.mark.parametrize("name", EXACT_TREE_BUILDERS)
def test_an_empty_generated_side_fails_independently(
    name, tmp_path, monkeypatch,
):
    """Guard three. A builder that exits 0 having written only directories."""
    _skip_if_absent(name)

    def _writes_only_directories(_name, out):
        (out / "scenario").mkdir(parents=True)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(_self(), "_run_builder", _writes_only_directories)
    with pytest.raises(AssertionError, match="wrote no file under --out"):
        test_the_exact_tree_is_what_the_builder_produces_today(
            name=name, tmp_path=tmp_path / "run")


def test_the_exclusion_policy_is_what_was_reviewed():
    """A tripwire, not a tautology.

    The comparison reads this constant; this test carries an independently
    written copy of what it is supposed to say. It cannot stop a deliberate
    edit to both, and is not meant to — it stops a one-line addition from
    quietly widening the only hole in a byte-exact comparison.
    """
    assert EXACT_TREE_EXCLUDED_ROOTS == {
        "mirror-public": frozenset(),
        "release": frozenset({"_assets"}),
    }


@pytest.mark.parametrize("name", EXACT_TREE_BUILDERS)
def test_every_exclusion_is_a_real_committed_root_the_builder_omits(name, tmp_path):
    """One case per family, so one absent builder cannot skip the other.

    A family whose exclusion set is empty returns before the builder runs: there
    is no root to locate in either tree, and the structural checks above it are
    the whole of what it has to prove.
    """
    assert set(EXACT_TREE_EXCLUDED_ROOTS) == set(EXACT_TREE_BUILDERS)
    roots = EXACT_TREE_EXCLUDED_ROOTS[name]
    for root in roots:
        assert "/" not in root, f"{root} is not a single path component"
    if not roots:
        return

    _skip_if_absent(name)
    tracked = _tracked_relpaths(name)
    out = tmp_path / name
    assert _run_builder(name, out).returncode == 0
    produced = exact_manifest(out)
    for root in roots:
        assert any(rel.startswith(f"{root}/") or rel == root for rel in tracked), (
            f"{name}: nothing tracked under the excluded root {root}"
        )
        assert root not in produced, (
            f"{name}: the builder produces {root}, so excluding it hides "
            f"real output"
        )


@pytest.mark.parametrize("name", CONTRACT_BUILDERS)
def test_the_builder_produces_the_same_tree_twice(name, tmp_path):
    """Determinism, over EVERYTHING the builder writes.

    The comparison above deliberately ignores the builder's gitignored outputs,
    which for one of these six is most of what it writes. This one covers them:
    two rebuilds must agree, so a builder that stirred a timestamp or a uuid
    into a fixture is caught even where nothing is committed to compare against.
    """
    _skip_if_absent(name)
    first, second = tmp_path / "one", tmp_path / "two"
    for out in (first, second):
        proc = _run_builder(name, out)
        assert proc.returncode == 0, proc.stderr[-4000:]
    produced = manifest(first)
    assert produced, f"build-{name}-fixtures.py wrote nothing"
    differences = diff_manifests(produced, manifest(second))
    assert not differences, (
        f"bin/build-{name}-fixtures.py is not deterministic:\n  "
        + "\n  ".join(differences)
    )


def test_regenerating_release_repairs_a_tampered_gitignore(tmp_path):
    """The advertised remedy has to actually converge the tree.

    `build()` used to write a scenario's `.gitignore` only when it was absent,
    so a committed one that had been altered survived every rebuild while the
    diagnostic told the maintainer that rebuilding was the fix.
    """
    _skip_if_absent("release")
    out = tmp_path / "release"
    assert _run_builder("release", out).returncode == 0
    victim = next(iter(sorted(out.glob("*/.gitignore"))))
    victim.write_text("TAMPERED\n", encoding="utf-8")
    assert _run_builder("release", out).returncode == 0
    assert victim.read_text(encoding="utf-8") == "_artifacts/\n"


# ----------------------------------------------- the comparison can actually fail
# Each of these mutates a rebuilt tree in one of the ways the contract claims to
# catch, and asserts the comparison reports it. Without them a manifest that
# silently compared nothing would pass forever.


def _sample_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "sample"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "data.json").write_text('{"a": 1}\n', encoding="utf-8")
    (root / "setup.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (root / "setup.sh").chmod(0o644)
    conn = sqlite3.connect(root / "cache.db")
    try:
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'x')")
        conn.commit()
    finally:
        conn.close()
    return root


def test_a_changed_non_golden_file_fails_the_comparison(tmp_path):
    """The case that makes the contract non-vacuous.

    A builder-owned file that is not a golden is exactly what a silent builder
    change moves, and a contract that only compared goldens would not see it.
    """
    root = _sample_tree(tmp_path)
    before = manifest(root)
    (root / "nested" / "data.json").write_text('{"a": 2}\n', encoding="utf-8")
    assert any("data.json" in line for line in diff_manifests(before, manifest(root)))


def test_a_changed_executable_bit_fails_the_comparison(tmp_path):
    """`bin/build-doctor-fixtures.py` chmods a fixture 0755 on purpose."""
    root = _sample_tree(tmp_path)
    before = manifest(root)
    (root / "setup.sh").chmod(0o755)
    assert any("setup.sh" in line for line in diff_manifests(before, manifest(root)))


def test_a_missing_or_extra_path_fails_the_comparison(tmp_path):
    root = _sample_tree(tmp_path)
    before = manifest(root)
    (root / "nested" / "data.json").unlink()
    (root / "surprise.txt").write_text("x", encoding="utf-8")
    lines = diff_manifests(before, manifest(root))
    assert any("missing from the rebuild" in line for line in lines)
    assert any("present only in the rebuild" in line for line in lines)


# ---------------------------------------------- the exact manifest is exact
# `manifest()` earns three exemptions for the six holdouts: two sidecar
# suffixes and `.DS_Store` are environment-created working state, and a
# generated SQLite file compares through a canonical dump because its bytes
# are chosen by the runner's SQLite rather than by the fixture. None of the
# three is true of a family whose contract is byte equality, so `exact_manifest`
# has to report what `manifest` is right to ignore. Both directions are
# asserted in each test, because a helper that reported everything and a
# helper that reported nothing would each pass a one-sided assertion.


def test_the_exact_manifest_reports_a_ds_store(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "kept.txt").write_text("x", encoding="utf-8")
    (root / ".DS_Store").write_bytes(b"\x00\x01")
    assert ".DS_Store" not in manifest(root)
    assert ".DS_Store" in exact_manifest(root)


def test_the_exact_manifest_reports_a_sqlite_sidecar(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "cache.db-wal").write_bytes(b"\x00")
    (root / "cache.db-shm").write_bytes(b"\x00")
    assert not manifest(root)
    assert set(exact_manifest(root)) == {"cache.db-wal", "cache.db-shm"}


def test_the_exact_manifest_separates_byte_different_databases(tmp_path):
    """Same logical rows, different bytes — the canonical dump cannot see it."""
    root = tmp_path / "tree"
    root.mkdir()
    for name, page_size in (("a.db", 4096), ("b.db", 16384)):
        conn = sqlite3.connect(root / name)
        try:
            conn.execute(f"PRAGMA page_size = {page_size}")
            conn.execute("VACUUM")
            conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
            conn.execute("INSERT INTO t VALUES (1, 'x')")
            conn.commit()
        finally:
            conn.close()
    assert (root / "a.db").read_bytes() != (root / "b.db").read_bytes(), (
        "the two databases must differ byte for byte, or this test proves nothing"
    )
    entries = manifest(root)
    assert entries["a.db"] == entries["b.db"]
    exact = exact_manifest(root)
    assert exact["a.db"] != exact["b.db"]


# ------------------------------------------- classifying and rendering drift


def test_the_classifier_names_all_five_categories():
    committed = {
        "gone.txt": "f:-:bytes:aa",
        "same.txt": "f:-:bytes:bb",
        "swapped": "f:-:bytes:cc",
        "linked": "f:-:bytes:dd",
        "edited.txt": "f:-:bytes:ee",
        "script.sh": "f:-:bytes:ff",
    }
    produced = {
        "same.txt": "f:-:bytes:bb",
        "swapped": "d:",
        "linked": "l:elsewhere",
        "edited.txt": "f:-:bytes:99",
        "script.sh": "f:x:bytes:ff",
        "extra.txt": "f:-:bytes:11",
    }
    assert classify_exact_differences(committed, produced) == [
        ("content-different", "edited.txt"),
        ("executable-bit-different", "script.sh"),
        ("file-type-different", "linked"),
        ("file-type-different", "swapped"),
        ("missing", "gone.txt"),
        ("unexpected", "extra.txt"),
    ]


def test_the_classifier_reports_content_and_mode_drift_on_the_same_file():
    """One repair must not hide the second mismatch until the next run."""
    assert classify_exact_differences(
        {"script.sh": "f:x:bytes:aa"},
        {"script.sh": "f:-:bytes:bb"},
    ) == [
        ("content-different", "script.sh"),
        ("executable-bit-different", "script.sh"),
    ]


def test_the_classifier_reports_a_changed_symlink_target_as_content():
    """A symlink's target IS its content; the type did not change."""
    assert classify_exact_differences(
        {"link": "l:one"}, {"link": "l:two"}
    ) == [("content-different", "link")]


def test_a_file_that_became_a_directory_is_reported_from_a_real_tree(tmp_path):
    """The same category, derived from `exact_manifest` instead of typed out.

    The test above feeds the classifier hand-written manifest values, so it
    proves the classifier and nothing about the encoding it will really be
    handed. This one walks two directories, so a change to how `_walk_manifest`
    encodes a type cannot leave the classifier passing over an encoding it no
    longer receives.
    """
    committed, produced = tmp_path / "c", tmp_path / "p"
    committed.mkdir()
    produced.mkdir()
    (committed / "thing").write_text("payload\n", encoding="utf-8")
    (produced / "thing").mkdir()
    assert classify_exact_differences(
        exact_manifest(committed), exact_manifest(produced)
    ) == [("file-type-different", "thing")]


def test_a_file_that_became_a_symlink_is_reported_from_a_real_tree(tmp_path):
    """The second half of the file-type category, also over real trees."""
    committed, produced = tmp_path / "c", tmp_path / "p"
    committed.mkdir()
    produced.mkdir()
    (committed / "thing").write_text("payload\n", encoding="utf-8")
    (produced / "thing").symlink_to("elsewhere")
    assert classify_exact_differences(
        exact_manifest(committed), exact_manifest(produced)
    ) == [("file-type-different", "thing")]


def test_the_exact_comparison_covers_goldens_not_only_the_scripts(tmp_path):
    """Goldens are inside the comparison, and both drifts #646 cites are theirs.

    `public-clone-tag-already-on-public` drifted in a golden's CONTENT, and the
    intersection the six holdouts use drops a golden the builder has stopped
    producing altogether. A comparison narrowed to `setup.sh` and `run.sh` would
    report neither.
    """
    committed, produced = tmp_path / "committed", tmp_path / "rebuilt"
    for root in (committed, produced):
        (root / "scenario").mkdir(parents=True)
        (root / "scenario" / "setup.sh").write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8")
    (committed / "scenario" / "golden-stderr-substr.txt").write_text(
        "refusing to push\n", encoding="utf-8")
    (produced / "scenario" / "golden-stderr-substr.txt").write_text(
        "refusing to publish\n", encoding="utf-8")
    (committed / "scenario" / "golden-retired.txt").write_text(
        "no longer declared\n", encoding="utf-8")

    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)
    assert differences == [
        ("content-different", "scenario/golden-stderr-substr.txt"),
        ("missing", "scenario/golden-retired.txt"),
    ]
    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)
    assert "missing scenario/golden-retired.txt" in text
    assert "refusing to publish" in text


def test_the_classifier_says_nothing_about_matching_trees():
    entries = {"a.txt": "f:-:bytes:aa", "dir": "d:"}
    assert classify_exact_differences(entries, dict(entries)) == []


def _mismatch_pair(tmp_path, content_mismatches: int):
    """A committed/rebuilt pair with N content diffs plus one mode diff."""
    committed, produced = tmp_path / "committed", tmp_path / "rebuilt"
    for root in (committed, produced):
        root.mkdir()
    for index in range(content_mismatches):
        (committed / f"s{index:02d}.sh").write_text(
            f"#!/bin/sh\noriginal {index}\n", encoding="utf-8")
        (produced / f"s{index:02d}.sh").write_text(
            f"#!/bin/sh\nrebuilt {index}\n", encoding="utf-8")
    (committed / "mode.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (produced / "mode.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (produced / "mode.sh").chmod(0o755)
    return committed, produced


def test_the_diagnostic_lists_every_path_and_truncates_the_diffs(tmp_path):
    """Six content diffs: five rendered, one counted, all seven inventoried."""
    committed, produced = _mismatch_pair(tmp_path, content_mismatches=6)
    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)
    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)

    for index in range(6):
        assert f"s{index:02d}.sh" in text, f"s{index:02d}.sh missing from inventory"
    assert "executable-bit-different mode.sh" in text
    assert "7 path(s) differ" in text
    assert text.count("--- committed/") == 5
    assert text.count("+++ rebuilt/") == 5
    assert "1 further content diff" in text
    # `test_public_test_dep_closure.py` scope A2 cannot tell a substring
    # assertion over rendered text from a read of the file it names, and this
    # test reads no builder — every tree it touches is under `tmp_path`.
    assert "bin/build-release-fixtures.py" in text  # mirror-private-ok
    assert "builders do not prune" in text


def test_the_diagnostic_caps_a_long_diff(tmp_path):
    committed, produced = tmp_path / "c", tmp_path / "p"
    for root, word in ((committed, "old"), (produced, "new")):
        root.mkdir()
        (root / "big.txt").write_text(
            "\n".join(f"{word} {n}" for n in range(400)) + "\n", encoding="utf-8")
    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)
    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)
    assert "diff truncated" in text
    assert _EXACT_DIFF_MAX_LINES == 60, (
        "60 is the cap the design fixed; the bound below reads the constant, so "
        "widening the constant would keep that assertion green"
    )
    # The cap governs the diff; the remedy footer that follows it does not.
    rendered = text.split(
        "--- committed/big.txt", 1)[1].split("... diff truncated", 1)[0]
    assert rendered.count("\n") <= _EXACT_DIFF_MAX_LINES, (
        "the per-file diff cap was not applied"
    )


def test_the_diagnostic_falls_back_to_digests_for_a_binary_file(tmp_path):
    committed, produced = tmp_path / "c", tmp_path / "p"
    for root, byte in ((committed, b"\xff\xfe\x00"), (produced, b"\xff\xfe\x01")):
        root.mkdir()
        (root / "blob.bin").write_bytes(byte)
    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)
    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)
    assert "--- committed/blob.bin" not in text
    assert "sha256" in text and "blob.bin" in text


def test_a_binary_mismatch_consumes_a_rendered_slot(tmp_path):
    """The cap counts every rendered content mismatch, diff or digest pair.

    Seven binary mismatches against a limit of five: five digest lines, and the
    two that were skipped are the ones the omission count names.
    """
    committed, produced = tmp_path / "c", tmp_path / "p"
    committed.mkdir()
    produced.mkdir()
    for index in range(7):
        (committed / f"b{index}.bin").write_bytes(b"\xff\xfe" + bytes([index]))
        (produced / f"b{index}.bin").write_bytes(b"\xff\xfd" + bytes([index]))
    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)
    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)
    assert text.count("not UTF-8 text") == 5
    assert "2 further content diff" in text


def test_the_diagnostic_renders_a_changed_symlink_as_its_target(tmp_path):
    """A symlink's content is its target string, not the target file's bytes.

    Reading through the link reports a difference between two files the
    comparison never looked at, which is a different claim from the true one.
    """
    committed, produced = tmp_path / "c", tmp_path / "p"
    for root, target in ((committed, "a.txt"), (produced, "b.txt")):
        root.mkdir()
        (root / "a.txt").write_text("AAA\n", encoding="utf-8")
        (root / "b.txt").write_text("BBB\n", encoding="utf-8")
        (root / "link").symlink_to(target)
    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)
    assert differences == [("content-different", "link")]
    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)
    assert "a.txt" in text and "b.txt" in text
    assert "AAA" not in text and "BBB" not in text
    assert "--- committed/link" not in text


def test_the_diagnostic_survives_a_dangling_symlink(tmp_path):
    """A diagnostic that raises replaces the failure it exists to explain."""
    committed, produced = tmp_path / "c", tmp_path / "p"
    for root, target in ((committed, "gone-one"), (produced, "gone-two")):
        root.mkdir()
        (root / "link").symlink_to(target)
    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)
    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)
    assert "gone-one" in text and "gone-two" in text


def test_the_digest_fallback_reports_an_unreadable_file_rather_than_raising(
    tmp_path,
):
    """The fallback runs inside the diagnostic, where raising loses the report."""
    assert "unreadable" in _short_digest(tmp_path / "never-written")


def test_the_remedy_distinguishes_targeted_unlink_from_recursive_pruning(
    tmp_path,
):
    """A targeted optional-golden unlink is not recursive pruning.

    Both builders unlink an optional golden they still name and no longer
    declare a value for, so telling the maintainer that every `missing` path
    survives a rebuild sends them to remove by hand a file the rebuild removes.
    """
    committed, produced = _mismatch_pair(tmp_path, content_mismatches=1)
    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)
    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)
    assert "do not prune unowned paths recursively" in text
    assert "targeted unlinks for optional goldens" in text
    assert "still reported after regenerating" not in text
    assert "an optional golden the builder still names" in text
    assert "must be reviewed and removed by hand" in text
    # Either builder chmods `setup.sh` and `run.sh` and no other path, and
    # every other file is written with `write_text`, which preserves the mode
    # the file already has. A blanket claim that the rebuild repairs mode drift
    # therefore sends the maintainer to a command that leaves an
    # `executable-bit-different` on a golden exactly where it was.
    assert "are all repaired by the rebuild" not in text
    assert "setup.sh and run.sh only" in text
    assert "chmod" in text


def test_the_remedy_names_a_resolution_for_every_reportable_category(tmp_path):
    """A category the remedy omits is one the maintainer gets no answer for.

    `file-type-different` is the omission that costs most: where the committed
    side is a directory, the rebuild the remedy advertises raises
    `IsADirectoryError` rather than converging, and nothing said so.
    """
    committed, produced = _mismatch_pair(tmp_path, content_mismatches=1)
    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)
    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)
    for category in _EXACT_CATEGORIES:
        assert f"  {category}:" in text, (
            f"the remedy states no resolution for {category}"
        )
    assert "IsADirectoryError" in text


def test_the_diagnostic_gives_each_mode_mismatch_its_chmod_direction(tmp_path):
    committed, produced = tmp_path / "c", tmp_path / "p"
    committed.mkdir()
    produced.mkdir()
    for root in (committed, produced):
        (root / "setup.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "golden.txt").write_text("expected\n", encoding="utf-8")
    (produced / "setup.sh").chmod(0o755)
    (committed / "golden.txt").chmod(0o755)

    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)
    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)

    assert "chmod +x tests/fixtures/release/setup.sh" in text
    assert "chmod -x tests/fixtures/release/golden.txt" in text


def test_the_diagnostic_puts_directory_removal_before_regeneration(tmp_path):
    committed, produced = tmp_path / "c", tmp_path / "p"
    committed.mkdir()
    produced.mkdir()
    (committed / "blocked").mkdir()
    (produced / "blocked").write_text("rebuilt\n", encoding="utf-8")

    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)
    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)

    removal = "remove tests/fixtures/release/blocked by hand before regenerating"
    rebuild = "python3 bin/build-release-fixtures.py"
    assert removal in text
    assert text.index(removal) < text.index(rebuild)


def test_the_diagnostic_explains_a_line_ending_only_difference(tmp_path):
    """Universal-newline translation makes this pair diff to nothing.

    Read through `read_text`, a CRLF file and its LF twin produce equal line
    lists, `difflib.unified_diff` returns no lines at all, and the entry renders
    as two blank lines while still consuming one of the five rendered slots.
    """
    committed, produced = tmp_path / "c", tmp_path / "p"
    committed.mkdir()
    produced.mkdir()
    (committed / "run.sh").write_bytes(b"#!/bin/sh\r\nexit 0\r\n")
    (produced / "run.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)
    assert differences == [("content-different", "run.sh")]
    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)
    entry = text.split("content-different run.sh", 1)[1]
    entry = entry.split("to fix, from the repository root", 1)[0].strip()
    assert entry, "the only reported mismatch rendered nothing at all"
    assert "line terminators" in entry
    assert "committed CRLF" in entry
    assert "rebuilt LF" in entry


def test_a_mixed_content_and_lone_cr_diff_renders_as_separate_lines(tmp_path):
    committed, produced = tmp_path / "c", tmp_path / "p"
    committed.mkdir()
    produced.mkdir()
    (committed / "run.sh").write_bytes(b"one\rtwo\r")
    (produced / "run.sh").write_bytes(b"one\rTWO\r")
    committed_entries = exact_manifest(committed)
    produced_entries = exact_manifest(produced)
    differences = classify_exact_differences(committed_entries, produced_entries)

    text = render_exact_mismatch(
        "release", committed, produced, differences,
        committed_entries, produced_entries)

    assert "\r" not in text
    assert "\n-two\n+TWO\n" in text


def test_changed_sqlite_data_fails_the_comparison(tmp_path):
    root = _sample_tree(tmp_path)
    before = manifest(root)
    conn = sqlite3.connect(root / "cache.db")
    try:
        conn.execute("INSERT INTO t VALUES (2, 'y')")
        conn.commit()
    finally:
        conn.close()
    assert any("cache.db" in line for line in diff_manifests(before, manifest(root)))


def _raw_dump(path: pathlib.Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return "\n".join(sorted(conn.iterdump()))
    finally:
        conn.close()


def _external_content_fts_db(path: pathlib.Path, extra: tuple[str, ...] = ()) -> None:
    """A base table plus an external-content FTS5 index, the repo's shape."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute(
            "CREATE VIRTUAL TABLE messages_fts USING fts5"
            "(body, content='messages', content_rowid='id')"
        )
        for index, body in enumerate(("alpha one", "beta two"), start=1):
            conn.execute("INSERT INTO messages VALUES (?, ?)", (index, body))
            conn.execute(
                "INSERT INTO messages_fts (rowid, body) VALUES (?, ?)", (index, body)
            )
        # Index churn the base table does not record: written, then withdrawn.
        for index, body in enumerate(extra, start=900):
            conn.execute(
                "INSERT INTO messages_fts (rowid, body) VALUES (?, ?)", (index, body)
            )
            conn.execute(
                "INSERT INTO messages_fts (messages_fts, rowid, body) "
                "VALUES ('delete', ?, ?)", (index, body)
            )
        conn.commit()
    finally:
        conn.close()


@requires_fts5
def test_the_canonical_form_drops_fts5_index_state(tmp_path):
    """The claim E3 corrected: sorting removes page layout, not FTS5 format.

    `INSERT INTO "…_fts_config" VALUES('version',4)` and the packed blobs in
    `…_fts_data` are FTS5's own on-disk representation. Two databases whose
    indexed content is identical can hold different bytes there, so leaving them
    in the canonical form makes the contract fail on a differing SQLite build
    while nothing about the fixture has changed.
    """
    plain, churned = tmp_path / "plain.db", tmp_path / "churned.db"
    _external_content_fts_db(plain)
    _external_content_fts_db(churned, extra=("gamma three", "delta four"))

    raw_plain, raw_churned = _raw_dump(plain), _raw_dump(churned)
    assert raw_plain != raw_churned, (
        "the two databases must differ in their FTS5 index state, or this "
        "test proves nothing about excluding it"
    )
    assert "_fts_data" in raw_plain, "the raw dump must carry the shadow tables"

    canonical = _sqlite_canonical(plain)
    assert canonical == _sqlite_canonical(churned)
    for shadow in ("_fts_config", "_fts_data", "_fts_idx", "_fts_docsize"):
        assert shadow not in canonical, shadow
    assert "alpha one" in canonical, (
        "the indexed rows live in the base table and must survive the exclusion"
    )


@requires_fts5
def test_dropping_the_shadow_tables_still_sees_a_content_change(tmp_path):
    """Excluding index state must not excuse a change to what is indexed."""
    path = tmp_path / "one.db"
    _external_content_fts_db(path)
    before = _sqlite_canonical(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE messages SET body = 'alpha changed' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    assert _sqlite_canonical(path) != before


def test_the_sqlite_comparison_survives_a_different_page_layout(tmp_path):
    """The portability claim, which is why this is not a byte comparison.

    Two databases holding identical data can differ byte for byte — a different
    page size is enough, and so is a different SQLite build. Comparing bytes
    would redden on any runner whose SQLite differs from the one that committed
    the fixture.
    """
    root = _sample_tree(tmp_path)
    original = root / "cache.db"
    canonical = _sqlite_canonical(original)
    raw = original.read_bytes()

    rebuilt = tmp_path / "rebuilt.db"
    conn = sqlite3.connect(rebuilt)
    try:
        conn.execute("PRAGMA page_size = 16384")
        conn.execute("VACUUM")
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'x')")
        conn.commit()
    finally:
        conn.close()
    assert rebuilt.read_bytes() != raw, "the two files must differ byte for byte"
    assert _sqlite_canonical(rebuilt) == canonical
    assert _entry_digest(rebuilt) == _entry_digest(original)


def test_a_nondeterministic_builder_fails_the_comparison(tmp_path):
    """Two runs of the same builder must produce the same tree."""
    script = tmp_path / "build-flaky-fixtures.py"
    script.write_text(
        "import argparse, pathlib, uuid\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--out', type=pathlib.Path, default=None)\n"
        "a = p.parse_args()\n"
        "a.out.mkdir(parents=True, exist_ok=True)\n"
        "(a.out / 'x.txt').write_text(uuid.uuid4().hex)\n",
        encoding="utf-8",
    )
    first, second = tmp_path / "one", tmp_path / "two"
    for out in (first, second):
        proc = subprocess.run(
            [sys.executable, str(script), "--out", str(out)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
    assert diff_manifests(manifest(first), manifest(second))


def test_the_contract_list_is_not_empty_and_every_builder_exists():
    """A parameterized suite over an empty list passes without running."""
    assert len(HOLDOUTS) == 6
    assert len(EXACT_TREE_BUILDERS) == 2
    assert len(CONTRACT_BUILDERS) == 8
    assert len(set(CONTRACT_BUILDERS)) == 8, "a name is in both families"
    assert [name for name in CONTRACT_BUILDERS if builder_for(name).exists()], (
        "no contract builder is present, so every parameterized case above "
        "skipped and this file checked nothing"
    )
    missing = missing_builders(REPO)
    assert not missing, missing


def _scaffold_checkout(root: pathlib.Path, present: tuple[str, ...],
                       tracked: tuple[str, ...]) -> None:
    """A repository carrying PRESENT builders on disk and TRACKED ones in git."""
    (root / "bin").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    for name in tracked:
        path = root / "bin" / f"build-{name}-fixtures.py"
        path.write_text("# stub\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                   capture_output=True)
    for name in tracked:
        if name not in present:
            (root / "bin" / f"build-{name}-fixtures.py").unlink()
    for name in present:
        path = root / "bin" / f"build-{name}-fixtures.py"
        if not path.exists():
            path.write_text("# stub\n", encoding="utf-8")


def test_mirror_private_builders_absent_from_the_checkout_are_not_reported(tmp_path):
    """The public mirror carries this file but not every builder it names.

    `.githooks/_match.py` classifies this test as public and both
    `bin/build-share-v2-fixtures.py` and `bin/build-release-fixtures.py` as
    private, and `.github/workflows/ci-linux-matrix.yml` runs the whole suite
    against the public repository, so a flat existence assertion reddens a lane
    that is behaving exactly as designed.
    """
    root = tmp_path / "public"
    public = tuple(n for n in CONTRACT_BUILDERS if n not in ("share-v2", "release"))
    _scaffold_checkout(root, present=public, tracked=public)
    assert missing_builders(root) == []


@pytest.mark.parametrize("victim", CONTRACT_BUILDERS)
def test_a_tracked_builder_deleted_from_the_working_tree_is_reported(victim, tmp_path):
    """The guard still has to fail, or it is a comment."""
    root = tmp_path / "private"
    _scaffold_checkout(
        root,
        present=tuple(n for n in CONTRACT_BUILDERS if n != victim),
        tracked=CONTRACT_BUILDERS,
    )
    assert missing_builders(root) == [victim]


def test_the_builder_environment_drops_an_inherited_as_of(monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", "1999-01-01T00:00:00+00:00")
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    env = builder_env()
    assert "CCTALLY_AS_OF" not in env
    assert env["CCTALLY_DISABLE_DEV_AUTODETECT"] == "1"
    assert env["TZ"] == "Etc/UTC"
    assert env["CCTALLY_FIXTURE_CACHE"] == "0"


@pytest.mark.parametrize("name", ["doctor"])
def test_an_exported_as_of_does_not_change_what_the_builder_produces(
    name, tmp_path, monkeypatch,
):
    """The pinning, proven by running the builder rather than by reading it.

    `bin/build-doctor-fixtures.py` dates five of its scaffolds relative to
    `CCTALLY_AS_OF`, so an inherited value moves real bytes. This exports one
    and requires the committed tree back anyway.
    """
    _skip_if_absent(name)
    monkeypatch.setenv("CCTALLY_AS_OF", "1999-01-01T00:00:00+00:00")
    out = tmp_path / name
    proc = _run_builder(name, out)
    assert proc.returncode == 0, proc.stderr[-4000:]

    tracked = _tracked_relpaths(name)
    produced, have = manifest(out), manifest(FIXTURES / name)
    shared = sorted(tracked & set(produced))
    assert shared
    assert not diff_manifests(
        {rel: have.get(rel, "<absent>") for rel in shared},
        {rel: produced[rel] for rel in shared},
    )


# ── The journal-stamp boundary (#769 S4 #795) ────────────────────────────────
#
# `_cctally_core` adds a `journal_id` column to eleven stats tables through an
# open-time ALTER that runs after the migration dispatcher. `create_stats_db`
# declares that column for the tables a fixture actually seeds a stamp into
# and deliberately omits the rest. Before #795 the omitted set included
# `five_hour_blocks`, and the consequence was invisible: a scenario that
# opened its store through the real modules gained the column, one that did
# not stayed without it, and `_retained_block_facts_many` reads the column
# directly — so the reader silently recomputed instead of failing.
#
# The two sets below are therefore checked against each other rather than
# transcribed. Adding a seeded stamp to a third table fails here until the
# table is declared, and declaring a table production does not stamp fails
# here too.
#
# WHICH OF THESE THREE CASES THE #795 FIX ACTUALLY TURNED. `fb93d5b37`'s commit
# body says all three fail against the pre-fix `bin/_fixture_builders.py`. Two
# do: `test_the_fixture_ddl_declares_exactly_the_stamped_tables` fails because
# the pre-fix DDL declares `journal_id` on `weekly_usage_snapshots` alone, and
# `test_the_retained_block_read_path_finds_its_column_and_indexes` fails
# because neither the column nor the two indexes exist on `five_hour_blocks`.
# `test_every_fixture_stamped_table_is_one_production_stamps` does NOT: it
# reads `bin/_cctally_core.py` and the constant below and never opens a built
# store, so no state of `_fixture_builders.py` can turn it. It is a
# direction-of-drift guard against declaring a column production does not add,
# which is a different property and worth keeping — it is simply not evidence
# that the #795 defect was reproduced. The record is corrected here because the
# commit body cannot be.

#: Stats tables whose `journal_id` `create_stats_db` declares in its own DDL,
#: each because some committed fixture seeds a value into it.
_FIXTURE_STAMPED_TABLES = frozenset({
    "weekly_usage_snapshots",   # week-reset origin identity (#750 S3)
    "five_hour_blocks",         # retained closed-block facts (#769 S4 #795)
})


def _production_journal_tables() -> frozenset:
    """The tables `_cctally_core`'s open-time ALTER stamps, read from source.

    Read by AST rather than by importing and running the opener, because
    running it would need a real stats.db and would apply migrations. The
    loop is a single `for _jtable in (...)` over a literal tuple, so the
    literal is what this reads; a rewrite into another form fails loudly here
    rather than returning a smaller set that looks live.
    """
    import ast

    source = (BIN / "_cctally_core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        if not isinstance(node.target, ast.Name) or node.target.id != "_jtable":
            continue
        assert isinstance(node.iter, ast.Tuple), (
            "the journal-identity ALTER loop no longer iterates a literal "
            "tuple, so this guard cannot read it"
        )
        for element in node.iter.elts:
            assert isinstance(element, ast.Constant) and isinstance(
                element.value, str), (
                "the journal-identity ALTER loop carries a non-literal entry"
            )
            found.append(element.value)
    assert found, (
        "no `for _jtable in (...)` loop found in bin/_cctally_core.py; the "
        "journal-identity ALTER moved and this guard is reading nothing"
    )
    return frozenset(found)


def test_the_fixture_ddl_declares_exactly_the_stamped_tables(tmp_path):
    """The fixture DDL's `journal_id` set is the declared seeded subset."""
    sys.path.insert(0, str(BIN))
    try:
        from _fixture_builders import create_stats_db
    finally:
        sys.path.pop(0)
    db = tmp_path / "stats.db"
    create_stats_db(db)
    with sqlite3.connect(db) as conn:
        tables = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        declared = {
            name for name in tables
            if any(
                row[1] == "journal_id"
                for row in conn.execute(f"PRAGMA table_info({name})")
            )
        }
    assert declared == set(_FIXTURE_STAMPED_TABLES), (
        "bin/_fixture_builders.py's create_stats_db declares journal_id on "
        f"{sorted(declared)}, but _FIXTURE_STAMPED_TABLES names "
        f"{sorted(_FIXTURE_STAMPED_TABLES)}. A fixture that seeds a stamp "
        "into a new table must declare the column here and name the table "
        "in that set; nothing else may declare it."
    )


def test_every_fixture_stamped_table_is_one_production_stamps():
    """A fixture may only declare a column production actually adds."""
    production = _production_journal_tables()
    assert _FIXTURE_STAMPED_TABLES <= production, (
        "these fixture-declared tables are not in _cctally_core's "
        "journal-identity ALTER loop: "
        f"{sorted(_FIXTURE_STAMPED_TABLES - production)}"
    )


def test_the_retained_block_read_path_finds_its_column_and_indexes(tmp_path):
    """Every column `_retained_block_facts_many` SELECTs, and its two indexes.

    The SELECT is inside a `try` that returns `{}` on `sqlite3.DatabaseError`,
    so a missing column costs the caller its retained facts without raising.
    The indexes matter for a different reason: production creates both with
    `IF NOT EXISTS` at open, so a fixture that omits them has its bytes
    rewritten the first time a real command opens it in tree.
    """
    sys.path.insert(0, str(BIN))
    try:
        from _fixture_builders import create_stats_db
    finally:
        sys.path.pop(0)
    db = tmp_path / "stats.db"
    create_stats_db(db)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        # Every expression `_retained_block_facts_many`'s parent query selects,
        # and its whole predicate. An earlier form selected two of the eight
        # and called itself "the literal query shape the dashboard reader
        # issues", which is the kind of label that survives a schema change the
        # reader would not.
        conn.execute(
            """
            SELECT id,
                   unixepoch(block_start_at)      AS bs_epoch,
                   unixepoch(five_hour_resets_at) AS rs_epoch,
                   total_input_tokens, total_output_tokens,
                   total_cache_create_tokens, total_cache_read_tokens,
                   total_cost_usd
              FROM five_hour_blocks
             WHERE is_closed = 1
               AND journal_id IS NOT NULL
               AND unixepoch(block_start_at) IN (?)
            """,
            (0,),
        ).fetchall()
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(five_hour_blocks)")
        }
    assert {"idx_five_hour_blocks_journal_id",
            "idx_five_hour_blocks_journal_id_null"} <= indexes, (
        "five_hour_blocks is missing the journal-identity indexes production "
        f"creates at open; present: {sorted(indexes)}"
    )
