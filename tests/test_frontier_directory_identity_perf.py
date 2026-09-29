"""The frontier's idle directory restat and walk-time directory derivation (#880).

Both dashboard frontiers restat their saved directory set on every idle tick,
and re-derive that set from every source row each time a certificate expires.
On a large store those two steps dominated the idle dashboard's CPU: building
one ``pathlib.Path`` per saved key per tick cost roughly three times the
``stat`` itself, and the derivation built a ``pathlib.Path`` per source row and
walked every source's ancestor chain again even when a sibling had already
collected it.

These tests pin two things. First, EQUIVALENCE: the cheaper implementations
return exactly what the pathlib implementations they replaced returned. Those
implementations are kept verbatim below as the oracle, so the comparison does
not depend on the code under test. Second, the COST SHAPE: the restat builds no
``pathlib.Path`` per key, and the derivation builds pathlib objects in
proportion to the directories it returns rather than to the source rows it
reads.
"""
from __future__ import annotations

import itertools
import os
import pathlib
import random
import sqlite3
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_ingest_frontier as frontier  # noqa: E402


# ── the oracle: the pre-#880 implementations, verbatim ─────────────────────
#
# Only the table-name lookups are routed through the module, because those two
# functions are unchanged and are what decides which table a provider reads.

def _stat_identity(path: pathlib.Path) -> tuple[int, int, int, int]:
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)


def _stat_identity_or_missing(path: pathlib.Path) -> tuple[int, int, int, int]:
    """Represent an already-absent directory as a stable observed state."""
    try:
        return _stat_identity(path)
    except OSError:
        return (0, 0, 0, 0)


def _source_paths(conn, provider: str) -> tuple[pathlib.Path, ...]:
    table = frontier._ingest_source_table(provider)
    return tuple(
        pathlib.Path(str(row[0]))
        for row in conn.execute(f"SELECT path FROM {table}")
        if row[0] and os.path.isabs(str(row[0]))
    )


def _directory_paths(conn, provider: str, roots) -> tuple[pathlib.Path, ...]:
    normalized_roots = tuple(pathlib.Path(root).absolute() for root in roots)
    directories = set(normalized_roots)
    for source in _source_paths(conn, provider):
        parent = source.parent
        for root in normalized_roots:
            try:
                parent.relative_to(root)
            except ValueError:
                continue
            cur = parent
            while True:
                directories.add(cur)
                if cur == root:
                    break
                cur = cur.parent
            break
    return tuple(sorted(directories, key=str))


def _restat_directory_identity(saved):
    return {
        raw: _stat_identity_or_missing(pathlib.Path(raw))
        for raw in saved
    }


def _conversation_source_paths(conn, provider: str) -> tuple[pathlib.Path, ...]:
    table = frontier._conversation_source_table(provider)
    return tuple(
        pathlib.Path(str(row[0]))
        for row in conn.execute(f"SELECT path FROM {table}")
        if row[0] and os.path.isabs(str(row[0]))
    )


def _conversation_directory_paths(conn, provider: str, roots):
    normalized_roots = tuple(pathlib.Path(root).absolute() for root in roots)
    directories = set(normalized_roots)
    for source in _conversation_source_paths(conn, provider):
        parent = source.parent
        for root in normalized_roots:
            try:
                parent.relative_to(root)
            except ValueError:
                continue
            cur = parent
            while True:
                directories.add(cur)
                if cur == root:
                    break
                cur = cur.parent
            break
    return tuple(sorted(directories, key=str))


# ── fixtures ────────────────────────────────────────────────────────────────

_TABLES = (
    "session_files",
    "codex_session_files",
    "conversation_source_files",
    "codex_conversation_source_files",
)

#: (family, provider) -> (new function, oracle function)
_FAMILIES = {
    ("accounting", "claude"): (
        lambda c, r: frontier._directory_paths(c, "claude", r),
        lambda c, r: _directory_paths(c, "claude", r)),
    ("accounting", "codex"): (
        lambda c, r: frontier._directory_paths(c, "codex", r),
        lambda c, r: _directory_paths(c, "codex", r)),
    ("conversation", "claude"): (
        lambda c, r: frontier._conversation_directory_paths(c, "claude", r),
        lambda c, r: _conversation_directory_paths(c, "claude", r)),
    ("conversation", "codex"): (
        lambda c, r: frontier._conversation_directory_paths(c, "codex", r),
        lambda c, r: _conversation_directory_paths(c, "codex", r)),
}


def _table_for(family: str, provider: str) -> str:
    if family == "accounting":
        return frontier._ingest_source_table(provider)
    return frontier._conversation_source_table(provider)


def _store(rows_by_table):
    """An in-memory store carrying the four real cursor tables' `path` column.

    The column is declared without a type, so a row can carry NULL, an empty
    string, an integer or a blob exactly as a damaged store could.
    """
    conn = sqlite3.connect(":memory:")
    for table in _TABLES:
        conn.execute(f"CREATE TABLE {table} (path)")
    for table, rows in rows_by_table.items():
        conn.executemany(
            f"INSERT INTO {table}(path) VALUES (?)", [(row,) for row in rows])
    return conn


def _assert_same_paths(new, old):
    assert isinstance(new, tuple)
    assert [str(path) for path in new] == [str(path) for path in old]
    assert new == old
    assert [type(path) for path in new] == [type(path) for path in old]


def _compare_all_families(sources, roots):
    for (family, provider), (new_fn, old_fn) in _FAMILIES.items():
        conn = _store({_table_for(family, provider): sources})
        try:
            _assert_same_paths(new_fn(conn, roots), old_fn(conn, roots))
        finally:
            conn.close()


@pytest.fixture
def tree(tmp_path):
    """Two provider roots, one with a nested inner root, plus an outsider."""
    outer = tmp_path / "claude" / "projects"
    inner = outer / "proj-a"
    codex = tmp_path / "codex-main" / "sessions"
    outside = tmp_path / "elsewhere"
    for directory in (
        inner / "sess-1" / "subagents",
        inner / "sess-2",
        outer / "proj-b" / "deep" / "deeper",
        codex / "2026" / "09" / "26",
        codex / "2026" / "09" / "25",
        outside / "x",
    ):
        directory.mkdir(parents=True)
    return types.SimpleNamespace(
        base=tmp_path, outer=outer, inner=inner, codex=codex, outside=outside)


def _tree_sources(t):
    outer, inner, codex, outside = (
        str(t.outer), str(t.inner), str(t.codex), str(t.outside))
    return [
        # Ordinary canonical sources, several sharing a parent.
        f"{inner}/sess-1/a.jsonl",
        f"{inner}/sess-1/b.jsonl",
        f"{inner}/sess-1/subagents/agent-1.jsonl",
        f"{inner}/sess-1/subagents/agent-2.jsonl",
        f"{inner}/sess-2/c.jsonl",
        f"{outer}/proj-b/deep/deeper/d.jsonl",
        f"{outer}/proj-b/e.jsonl",
        f"{codex}/2026/09/26/rollout-1.jsonl",
        f"{codex}/2026/09/25/rollout-2.jsonl",
        # A source whose parent IS a root, and a source that IS a root.
        f"{outer}/top.jsonl",
        f"{inner}/top.jsonl",
        f"{codex}/top.jsonl",
        outer,
        inner,
        # Duplicates.
        f"{inner}/sess-1/a.jsonl",
        f"{codex}/2026/09/26/rollout-1.jsonl",
        # Outside every root.
        f"{outside}/x/f.jsonl",
        "/definitely/not/under/any/root.jsonl",
        "/top-level.jsonl",
        # Directories that were never created: the derivation is lexical.
        f"{outer}/proj-ghost/sess/ghost.jsonl",
        f"{codex}/1999/01/01/ghost.jsonl",
        # Non-canonical spellings pathlib normalizes.
        f"{outer}//proj-c//sess//g.jsonl",
        f"{outer}/./proj-d/./h.jsonl",
        f"{outer}/proj-e/i.jsonl/",
        f"{outer}/proj-f/sess/.",
        f"{outer}/proj-g/sess/./",
        f"{inner}/sess-1//j.jsonl",
        f"/{outer}/leading-double.jsonl",
        f"//{outer}/leading-triple.jsonl",
        # `..` is kept by pathlib, so containment stays lexical.
        f"{inner}/../proj-h/k.jsonl",
        f"{outer}/proj-b/deep/../l.jsonl",
        f"{codex}/../escape/m.jsonl",
        # Rows the row source must skip.
        None,
        "",
        "relative/n.jsonl",
        "./relative/o.jsonl",
        0,
        12345,
        b"/bytes/are/not/absolute.jsonl",
        # A bare root and bare dots.
        "/",
        "//",
        "/.",
    ]


# ── _restat_directory_identity ─────────────────────────────────────────────

def _restat_keys(t):
    link = t.base / "link-to-inner"
    link.symlink_to(t.inner, target_is_directory=True)
    dangling = t.base / "dangling"
    dangling.symlink_to(t.base / "never-created")
    regular = t.inner / "sess-2" / "c.jsonl"
    regular.write_text("{}\n")
    locked = t.base / "locked"
    (locked / "child").mkdir(parents=True)
    locked.chmod(0)
    keys = [
        str(t.outer), str(t.inner), str(t.codex), str(t.outside),
        str(t.inner / "sess-1" / "subagents"),
        str(t.codex / "2026" / "09" / "26"),
        str(link), str(link / "sess-1"),
        str(dangling),
        str(regular),
        str(t.base / "missing"),
        str(t.base / "missing" / "deeper"),
        str(locked), str(locked / "child"),
    ]
    return keys, locked


def test_restat_matches_the_pathlib_restat_over_a_real_tree(tree):
    keys, locked = _restat_keys(tree)
    try:
        saved = {key: (9, 9, 9, 9) for key in keys}
        new = frontier._restat_directory_identity(saved)
        old = _restat_directory_identity(saved)
        assert new == old
        assert list(new) == list(old) == keys
        for value in new.values():
            assert type(value) is tuple and len(value) == 4
            assert all(type(part) is int for part in value)
        assert new[str(tree.base / "missing")] == (0, 0, 0, 0)
        assert new[str(tree.base / "dangling")] == (0, 0, 0, 0)
        assert new[str(tree.base / "link-to-inner")] == _stat_identity(tree.inner)
    finally:
        locked.chmod(0o755)


def test_restat_of_an_empty_saved_set_is_empty():
    assert frontier._restat_directory_identity({}) == {}


@pytest.mark.parametrize("family", ["accounting", "conversation"])
def test_restat_round_trips_the_identity_the_walk_recorded(tree, family):
    """Keys come from the real identity writer; an unchanged tree compares equal."""
    sources = [s for s in _tree_sources(tree) if isinstance(s, str)]
    roots = (tree.outer, tree.codex)
    conn = _store({_table_for(family, "claude"): sources})
    try:
        if family == "accounting":
            saved = frontier._directory_identity(conn, "claude", roots)
        else:
            saved = frontier._conversation_directory_identity(
                conn, "claude", roots)
    finally:
        conn.close()
    assert saved, "the walk recorded no directories"
    assert frontier._restat_directory_identity(saved) == saved
    assert _restat_directory_identity(saved) == saved

    # A new session directory moves its parent's identity. The timestamp is
    # pinned as well, because a coarse-clock filesystem can give the mkdir the
    # same mtime tick the fixture's own creation already recorded.
    sess_1 = tree.inner / "sess-1"
    (sess_1 / "new-session").mkdir()
    os.utime(sess_1, ns=(1_000_000_000, 1_000_000_000))
    new = frontier._restat_directory_identity(saved)
    assert new == _restat_directory_identity(saved)
    assert new != saved
    assert new[str(tree.inner / "sess-1")] != saved[str(tree.inner / "sess-1")]


def test_restat_builds_no_path_object_per_key(tree, monkeypatch):
    """The idle restat stats the saved key strings directly.

    Building a ``pathlib.Path`` per saved key per idle tick cost about three
    times the ``stat`` it wrapped, on thousands of keys, on two threads.
    """
    keys, locked = _restat_keys(tree)
    try:
        saved = {key: (0, 0, 0, 0) for key in keys}
        expected = _restat_directory_identity(saved)

        def no_path(*_args, **_kwargs):
            raise AssertionError("the idle restat built a pathlib.Path per key")

        monkeypatch.setattr(
            frontier, "pathlib", types.SimpleNamespace(Path=no_path))
        assert frontier._restat_directory_identity(saved) == expected
    finally:
        locked.chmod(0o755)


# ── _directory_paths / _conversation_directory_paths ───────────────────────

@pytest.mark.parametrize("order", ["outer-first", "inner-first"])
def test_derivation_matches_the_pathlib_derivation_with_nested_roots(
    tree, order,
):
    if order == "outer-first":
        roots = (tree.outer, tree.inner, tree.codex)
    else:
        roots = (tree.inner, tree.outer, tree.codex)
    _compare_all_families(_tree_sources(tree), roots)


def test_nested_roots_order_decides_the_intermediate_directories(tree):
    """The first root in the caller's order that contains a parent owns it.

    With the outer root first, a source under the inner root contributes every
    directory up to the outer root; with the inner root first it stops at the
    inner root. Pinned against the oracle so the per-root early exit cannot
    stop at an inner root that merely happens to be in the set already.
    """
    deeper = tree.inner.parent / "mid" / "proj-z"
    deeper.mkdir(parents=True)
    nested = tree.outer / "mid"
    sources = [f"{deeper}/s.jsonl", f"{deeper}/sub/t.jsonl"]
    outer_first = (tree.outer, nested)
    inner_first = (nested, tree.outer)
    _compare_all_families(sources, outer_first)
    _compare_all_families(sources, inner_first)
    conn = _store({"session_files": sources})
    try:
        by_outer = {str(p) for p in frontier._directory_paths(
            conn, "claude", outer_first)}
        by_inner = {str(p) for p in frontier._directory_paths(
            conn, "claude", inner_first)}
    finally:
        conn.close()
    assert str(nested) in by_outer and str(nested) in by_inner
    assert str(deeper) in by_outer and str(deeper) in by_inner


def test_nested_roots_with_an_inner_root_already_collected(tree):
    """An inner root collected first must not stop an outer root's walk."""
    inner_first_source = f"{tree.inner}/sess-1/a.jsonl"
    through_inner = f"{tree.outer}/proj-a/sess-2/b.jsonl"
    _compare_all_families(
        [inner_first_source, through_inner], (tree.inner, tree.outer))
    _compare_all_families(
        [inner_first_source, through_inner], (tree.outer, tree.inner))
    # Three levels deep, in every order.
    middle = tree.outer / "proj-a" / "sess-1"
    levels = (tree.outer, tree.inner, middle)
    sources = [
        f"{middle}/subagents/x.jsonl", f"{tree.inner}/sess-2/y.jsonl",
        f"{tree.outer}/proj-b/deep/z.jsonl", f"{middle}/w.jsonl",
    ]
    for order in itertools.permutations(levels):
        _compare_all_families(sources, order)
        _compare_all_families(list(reversed(sources)), order)


def test_derivation_with_no_roots_and_no_rows(tree):
    _compare_all_families([], ())
    _compare_all_families([], (tree.outer,))
    _compare_all_families(_tree_sources(tree), ())


def test_derivation_with_the_filesystem_root_as_a_root(tree):
    _compare_all_families(_tree_sources(tree), ("/",))
    _compare_all_families(_tree_sources(tree), (tree.codex, "/"))
    _compare_all_families(_tree_sources(tree), ("/", tree.codex))


def test_derivation_with_unnormalized_and_relative_roots(tree, monkeypatch):
    monkeypatch.chdir(tree.base)
    roots = (
        "claude/projects",
        f"{tree.codex}/",
        f"{tree.outer}//proj-a/./",
        f"{tree.base}/codex-main/../claude/projects/proj-b",
        pathlib.Path("elsewhere"),
    )
    _compare_all_families(_tree_sources(tree), roots)
    _compare_all_families(_tree_sources(tree), tuple(reversed(roots)))


def test_derivation_with_duplicate_roots(tree):
    roots = (tree.outer, tree.codex, tree.outer, str(tree.codex))
    _compare_all_families(_tree_sources(tree), roots)


_SEGMENTS = ("a", "b", "sess", "proj", "..", ".", "", "x.y", " sp", "é")


def _random_source(rng, anchors):
    kind = rng.random()
    if kind < 0.04:
        return rng.choice([None, "", "rel/x.jsonl", 7, "./x.jsonl"])
    base = rng.choice(anchors)
    parts = [rng.choice(_SEGMENTS[:4] + _SEGMENTS[7:]) for _ in range(
        rng.randint(0, 5))]
    path = base + "".join("/" + part for part in parts) + "/f.jsonl"
    if rng.random() < 0.15:
        # Non-canonical mutation: an empty, dot or dot-dot segment, a trailing
        # slash or dot, or a doubled leading slash.
        mutation = rng.choice(["//", "/./", "/../", "trail/", "trail/.", "lead"])
        if mutation == "trail/":
            path += "/"
        elif mutation == "trail/.":
            path += "/."
        elif mutation == "lead":
            path = "/" + path
        else:
            cut = rng.randint(1, len(path) - 1)
            path = path[:cut] + mutation + path[cut:]
    return path


@pytest.mark.parametrize("seed", [880, 1, 2, 3, 4, 5, 6, 7])
def test_derivation_matches_the_pathlib_derivation_on_random_paths(
    tmp_path, seed,
):
    rng = random.Random(seed)
    base = str(tmp_path)
    candidate_roots = [
        f"{base}/claude/projects",
        f"{base}/claude/projects/p1",
        f"{base}/claude/projects/p1/s",
        f"{base}/claude",
        f"{base}/codex/sessions",
        f"{base}/codex/sessions/2026",
        f"{base}/other",
    ]
    anchors = candidate_roots + [base, f"{base}/unrelated", "/"]
    sources = [_random_source(rng, anchors) for _ in range(600)]
    sources += rng.sample(sources, 60)
    for _ in range(4):
        roots = rng.sample(candidate_roots, rng.randint(1, len(candidate_roots)))
        _compare_all_families(sources, tuple(roots))


def test_derivation_builds_pathlib_objects_per_directory_not_per_source(
    tmp_path, monkeypatch,
):
    """A walk re-seeds the directory set from every source row.

    With thousands of rows spread over far fewer directories, the derivation
    must not build a ``pathlib.Path`` per row; it builds one per configured
    root and one per directory it returns.
    """
    root = tmp_path / "projects"
    sources = [
        f"{root}/proj-{p}/sess-{s}/file-{f}.jsonl"
        for p in range(4) for s in range(5) for f in range(100)
    ]
    for family, provider in _FAMILIES:
        conn = _store({_table_for(family, provider): sources})
        try:
            new_fn, old_fn = _FAMILIES[(family, provider)]
            expected = old_fn(conn, (root,))
            calls = {"count": 0}

            def counting_path(*args):
                calls["count"] += 1
                return pathlib.Path(*args)

            monkeypatch.setattr(
                frontier, "pathlib", types.SimpleNamespace(Path=counting_path))
            try:
                result = new_fn(conn, (root,))
            finally:
                monkeypatch.undo()
            _assert_same_paths(result, expected)
            assert len(result) == 1 + 4 + 4 * 5
            assert calls["count"] <= 1 + len(result), (
                family, provider, calls["count"], len(sources))
        finally:
            conn.close()


# ── failure order ───────────────────────────────────────────────────────────

class _UnresolvableRoot(os.PathLike):
    """A configured root that cannot be turned into a path at walk time."""

    def __fspath__(self):
        raise OSError("root unavailable")


class _UnreadableStore:
    def execute(self, *_args, **_kwargs):
        raise sqlite3.OperationalError("store unavailable")


@pytest.mark.parametrize("derivation", [
    frontier._directory_paths, frontier._conversation_directory_paths,
])
def test_derivation_resolves_its_roots_before_reading_the_store(derivation):
    # The pre-#880 derivation resolved the roots first, so a root it could not
    # resolve surfaced as the OSError the seed path treats as a non-certifiable
    # walk, even when the store would also have failed. Reading the sources
    # first let the store's sqlite3 error escape instead.
    with pytest.raises(OSError, match="root unavailable"):
        derivation(_UnreadableStore(), "claude", (_UnresolvableRoot(),))
