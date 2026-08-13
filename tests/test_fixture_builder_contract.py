"""The six holdout fixture builders: `--out`, and the committed set is current.

Six harnesses used to rebuild their fixtures IN PLACE on every run — 676 tracked
files rewritten by a test run, and nothing asserting the tree came back
unchanged. That is both a source of interference for anything else reading the
tree concurrently and a way for a fixture to drift from its builder without
anybody noticing.

Two things are asserted here, and they are complements:

- `--out DIR` exists on all six and redirects everything the builder writes, so
  a harness can build into scratch and leave the tracked tree alone.
- Rebuilding into scratch reproduces the COMMITTED tree, so the committed
  fixtures are provably what the builders currently produce.

The comparison is semantic, not byte-for-byte. `bin/_fixture_cache.py` hashes
the SQLite version, its compile options, FTS5 availability and the Python
identity into its cache key precisely because builder output is
toolchain-sensitive; a raw byte comparison of a generated SQLite file would
therefore fail on any runner whose SQLite differs and turn a detector into an
outage. SQLite files are compared through a canonical dump, everything else by
content digest, and the manifest carries the executable bit because
`bin/build-doctor-fixtures.py` deliberately `chmod(0o755)`s one of its outputs
and a comparison ignoring mode would miss that regression.

Two specific things make the SQLite comparison portable, and it is worth being
exact about them because they are the only two. Sorting the dump removes page
layout. Dropping each virtual table's shadow tables removes FTS5's on-disk
index format, which the generated `cache.db` files carry and which is chosen by
the FTS5 implementation rather than by the data. Nothing else is removed, so a
toolchain that changed a stored value or a schema still fails the comparison.
"""

from __future__ import annotations

import ast
import contextlib
import fcntl
import hashlib
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile

import pytest

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


def missing_builders(root: pathlib.Path, names=HOLDOUTS) -> list[str]:
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


def manifest(root: pathlib.Path) -> dict[str, str]:
    """`{relative path: type + mode + content}` for one fixture tree.

    Never follows a symlink, and records the target rather than what it points
    at. The executable bit is carried, and nothing else of the mode is, so a
    difference in umask between two runners is not reported as drift while a
    deliberate `chmod(0o755)` still is.
    """
    entries: dict[str, str] = {}
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        here = pathlib.Path(current)
        for name in sorted(dirnames + filenames):
            path = here / name
            rel = str(path.relative_to(root))
            if name in _IGNORED_NAMES or rel.endswith(_IGNORED_SUFFIXES):
                continue
            if path.is_symlink():
                entries[rel] = "l:%s" % os.readlink(path)
                continue
            if path.is_dir():
                entries[rel] = "d:"
                continue
            executable = "x" if os.access(path, os.X_OK) else "-"
            entries[rel] = "f:%s:%s" % (executable, _entry_digest(path))
    return entries


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


@pytest.mark.parametrize("name", HOLDOUTS)
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


@pytest.mark.parametrize("name", HOLDOUTS)
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


@pytest.mark.parametrize("name", HOLDOUTS)
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


def _fts5_available() -> bool:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


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


@pytest.mark.skipif(not _fts5_available(), reason="this SQLite has no FTS5")
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


@pytest.mark.skipif(not _fts5_available(), reason="this SQLite has no FTS5")
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


def test_the_holdout_list_is_not_empty_and_every_builder_exists():
    """A parameterized suite over an empty list passes without running."""
    assert len(HOLDOUTS) == 6
    assert [name for name in HOLDOUTS if builder_for(name).exists()], (
        "no holdout builder is present, so every parameterized case above "
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


def test_a_mirror_private_builder_absent_from_the_checkout_is_not_reported(tmp_path):
    """The public mirror carries this file but not `build-share-v2-fixtures.py`.

    `.githooks/_match.py` classifies this test as public and that builder as
    private, and `.github/workflows/ci-linux-matrix.yml` runs the whole suite
    against the public repository, so a flat existence assertion reddens a lane
    that is behaving exactly as designed. Absence is only a defect when the
    checkout is supposed to carry the file, and git is what says so.
    """
    root = tmp_path / "public"
    public = tuple(n for n in HOLDOUTS if n != "share-v2")
    _scaffold_checkout(root, present=public, tracked=public)
    assert missing_builders(root) == []


def test_a_tracked_builder_deleted_from_the_working_tree_is_reported(tmp_path):
    """The guard still has to fail, or it is a comment."""
    root = tmp_path / "private"
    _scaffold_checkout(root, present=tuple(n for n in HOLDOUTS if n != "doctor"),
                       tracked=HOLDOUTS)
    assert missing_builders(root) == ["doctor"]


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
