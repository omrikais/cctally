"""Tests for bin/cctally-mirror-public helpers.

Focused on the contract that the mirror tool classifies each commit's
paths under the allowlist that lived in THAT commit's tree (commit-time
semantics), matching the commit-msg hook (`.githooks/_public_trailer.py`).

The bug this guards against: a commit that adds an unmatched file
followed by a LATER commit that adds the file to `.mirror-allowlist`.
The hook (commit-time) accepts the first commit without a public
trailer; pre-fix, the mirror tool re-evaluated under HEAD's allowlist
and retroactively flagged the first commit as "touches public files
but has no trailer." Post-fix both surfaces use the same snapshot.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


# Load bin/cctally-mirror-public as a module (no .py extension).
_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "bin" / "cctally-mirror-public"
_LOADER = importlib.machinery.SourceFileLoader(
    "cctally_mirror_public", str(_SCRIPT),
)
_SPEC = importlib.util.spec_from_loader("cctally_mirror_public", _LOADER)
mirror = importlib.util.module_from_spec(_SPEC)
sys.modules["cctally_mirror_public"] = mirror
_LOADER.exec_module(mirror)

# Module-level sys.path guard for `.githooks/_match.py` siblings. The
# mirror tool itself does the same insertion at import time, but pytest
# may collect this file in any order so we mirror that side here. Guard
# against duplicate insertions across test-class boundaries (a previous
# revision called `sys.path.insert` from inside test methods, which
# leaked entries for the rest of the pytest session); the `not in`
# check is idempotent and avoids cleanup-ordering bugs of a fixture
# alternative.
_HOOKS_PATH = str(_REPO / ".githooks")
if _HOOKS_PATH not in sys.path:
    sys.path.insert(0, _HOOKS_PATH)
import _match  # noqa: E402  (sys.path injection above)


# ---------------------------------------------------------------------------
# Test git-repo helpers (mirroring .githooks/test_skip_chain_metrics.py
# style so the two test suites stay legible side-by-side).
# ---------------------------------------------------------------------------
def _git(args: list[str], cwd: Path,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "t@e.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    })
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=env,
        capture_output=True, text=True, check=check, timeout=30,
    )


def _init(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], p)
    _git(["config", "commit.gpgsign", "false"], p)


def _commit(p: Path, files: dict[str, str], msg: str) -> str:
    """Write each (path, content) under `p`, stage, and commit. Returns SHA."""
    for rel, content in files.items():
        target = p / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _git(["add", rel], p)
    _git(["commit", "-q", "--no-verify", "-m", msg], p)
    return _git(["rev-parse", "HEAD"], p).stdout.strip()


# ---------------------------------------------------------------------------
# _read_allowlist_at_sha: returns the allowlist body as it lived at the
# given SHA, or "" when absent at that SHA.
# ---------------------------------------------------------------------------
class TestReadAllowlistAtSha:
    def test_returns_empty_when_absent(self, tmp_path):
        _init(tmp_path)
        sha = _commit(tmp_path, {"a.txt": "x"}, "chore: a")
        assert mirror._read_allowlist_at_sha(sha, tmp_path) == ""

    def test_returns_blob_text_when_present(self, tmp_path):
        _init(tmp_path)
        sha = _commit(
            tmp_path,
            {".mirror-allowlist": "foo.txt\n", "a.txt": "x"},
            "chore: seed",
        )
        assert mirror._read_allowlist_at_sha(sha, tmp_path) == "foo.txt\n"

    def test_returns_snapshot_for_each_sha(self, tmp_path):
        """A later commit growing the allowlist must NOT change the body
        returned for the earlier SHA — the helper reads each commit's
        own tree, not HEAD."""
        _init(tmp_path)
        sha_a = _commit(
            tmp_path, {".mirror-allowlist": "a.txt\n"}, "chore: allow a",
        )
        sha_b = _commit(
            tmp_path,
            {".mirror-allowlist": "a.txt\nb.txt\n"},
            "chore: allow b too",
        )
        assert mirror._read_allowlist_at_sha(sha_a, tmp_path) == "a.txt\n"
        assert (
            mirror._read_allowlist_at_sha(sha_b, tmp_path)
            == "a.txt\nb.txt\n"
        )


# ---------------------------------------------------------------------------
# _classify_commit_paths: end-to-end of the bug fix. Verifies that a
# commit which added a file BEFORE the allowlist matched it classifies
# the file as `unmatched` (NOT `public`) — even when HEAD's allowlist
# has since been grown to match that file.
# ---------------------------------------------------------------------------
class TestClassifyCommitPaths:
    def test_commit_before_allowlist_match_is_unmatched(self, tmp_path):
        """The bug-fix scenario, distilled.

        Layout:
          - Commit A: seed an empty `.mirror-allowlist`.
          - Commit B: add `package.json` (NOT yet in allowlist).
          - Commit C: extend allowlist to include `package.json`.

        Pre-fix: classifying B against HEAD's allowlist (which now
        matches package.json) returned `public: ['package.json']`,
        triggering the trailer-required refusal.
        Post-fix: classifying B against B's-tree allowlist returns
        `unmatched: ['package.json']`, matching the commit-msg hook's
        verdict at commit time.
        """
        _init(tmp_path)
        _commit(
            tmp_path,
            {".mirror-allowlist": "# starts empty\n"},
            "chore: seed allowlist",
        )
        sha_b = _commit(
            tmp_path, {"package.json": "{}\n"}, "feat(npm): add package.json",
        )
        _commit(
            tmp_path,
            {".mirror-allowlist": "# starts empty\npackage.json\n"},
            "chore: promote package.json",
        )

        cls = mirror._classify_commit_paths(sha_b, tmp_path)
        assert cls["public"] == []
        assert cls["unmatched"] == ["package.json"]

    def test_commit_after_allowlist_match_is_public(self, tmp_path):
        """Symmetric guard: once a file IS in the allowlist at the
        commit's tree, a later edit to that file classifies as public."""
        _init(tmp_path)
        _commit(
            tmp_path,
            {".mirror-allowlist": "package.json\n"},
            "chore: allow package.json",
        )
        sha_b = _commit(
            tmp_path, {"package.json": "{}\n"}, "feat(npm): add package.json",
        )

        cls = mirror._classify_commit_paths(sha_b, tmp_path)
        assert cls["public"] == ["package.json"]
        assert cls["unmatched"] == []

    def test_classification_independent_of_head_state(self, tmp_path):
        """Defense-in-depth: mutate HEAD's allowlist AFTER capturing the
        commit's classification — the SHA's own classification must not
        shift."""
        _init(tmp_path)
        _commit(
            tmp_path,
            {".mirror-allowlist": "# empty\n"},
            "chore: seed",
        )
        sha_b = _commit(
            tmp_path, {"package.json": "{}\n"}, "feat: add package.json",
        )

        # First call: B's tree's allowlist is the seed (empty rules).
        cls1 = mirror._classify_commit_paths(sha_b, tmp_path)
        assert cls1["unmatched"] == ["package.json"]

        # Now grow HEAD's allowlist. B's classification must NOT change.
        _commit(
            tmp_path,
            {".mirror-allowlist": "# empty\npackage.json\n"},
            "chore: grow allowlist",
        )

        cls2 = mirror._classify_commit_paths(sha_b, tmp_path)
        assert cls2 == cls1


# ---------------------------------------------------------------------------
# _match.classify accepting allowlist_text directly: the underlying
# kwarg added to support commit-time classification without round-
# tripping through the working tree.
# ---------------------------------------------------------------------------
class TestMatchAllowlistText:
    def test_text_kwarg_classifies_without_disk_file(self, tmp_path):
        """The kwarg routes content directly without reading from disk —
        the use case `bin/cctally-mirror-public` exercises when feeding
        `git show <sha>:.mirror-allowlist` output into the classifier
        for historical commits whose tree-time allowlist isn't on the
        working-tree disk anymore."""
        # Even with NO file on disk, the text kwarg classifies cleanly.
        result = _match.classify(
            ["a.txt", "b.txt"],
            allowlist_text="a.txt\n",
        )
        assert result["public"] == ["a.txt"]
        assert result["unmatched"] == ["b.txt"]

    def test_text_kwarg_overrides_disk_path(self, tmp_path):
        """Precedence: when both `allowlist_path` and `allowlist_text`
        are supplied, the text wins. Documented contract of the kwarg
        — it must override the path even when the path resolves to a
        readable file with contradicting content. Without this
        guarantee, the mirror tool's commit-time classifier would
        leak HEAD's allowlist back in if a caller passed both."""
        # Disk allowlist matches `b.txt` (would classify b as public).
        disk = tmp_path / ".mirror-allowlist"
        disk.write_text("b.txt\n")

        # Text kwarg matches `a.txt` only. Text wins → a is public, b
        # falls into `unmatched`.
        result = _match.classify(
            ["a.txt", "b.txt"],
            allowlist_path=str(disk),
            allowlist_text="a.txt\n",
        )
        assert result["public"] == ["a.txt"]
        assert result["unmatched"] == ["b.txt"]
        assert result["private"] == []

    def test_empty_text_yields_all_unmatched(self, tmp_path):
        result = _match.classify(
            ["x", "y"], allowlist_text="",
        )
        assert result["public"] == []
        assert result["private"] == []
        assert result["unmatched"] == ["x", "y"]


# ---------------------------------------------------------------------------
# _read_allowlist_at_sha: error narrowing + per-call cache. Today's
# callers always feed `git rev-list`-validated SHAs, but a future caller
# passing an unverified SHA should get a loud error rather than silently
# treating an unreadable SHA as "no allowlist."
# ---------------------------------------------------------------------------
class TestReadAllowlistAtShaErrors:
    def test_invalid_sha_raises(self, tmp_path):
        """An invalid object name is a real git failure (corrupt index,
        bogus sha, ref-lock contention all surface the same way) — not
        an "absent allowlist." The helper raises so the caller can
        decide how to handle it."""
        _init(tmp_path)
        _commit(tmp_path, {"a.txt": "x"}, "chore: a")
        with pytest.raises(subprocess.CalledProcessError):
            mirror._read_allowlist_at_sha("deadbeefdeadbeef", tmp_path)

    def test_cache_memoizes_per_sha(self, tmp_path):
        """The optional `cache` dict memoizes the body per SHA so a
        long rev-list doesn't re-fork `git show` per visit. The
        validation pass, merge guard, apply pass, and tag-fingerprint
        pass all read the same allowlist body for the same SHA — the
        cache turns 4× forks into 1× per historical commit. Per-call
        scope (NOT module-level) — a long-running daemon would
        otherwise leak entries indefinitely."""
        _init(tmp_path)
        sha = _commit(
            tmp_path,
            {".mirror-allowlist": "foo.txt\n"},
            "chore: seed",
        )
        cache: dict[str, str] = {}

        # First call: populates the cache.
        body1 = mirror._read_allowlist_at_sha(sha, tmp_path, cache=cache)
        assert body1 == "foo.txt\n"
        assert cache == {sha: "foo.txt\n"}

        # Second call: hits the cache. Mutate disk underneath to prove
        # we're not re-reading. (Even though `git show` reads from the
        # tree object, not disk, the assertion proves the cache short-
        # circuits before any subprocess fork.)
        cache[sha] = "DIFFERENT\n"
        body2 = mirror._read_allowlist_at_sha(sha, tmp_path, cache=cache)
        assert body2 == "DIFFERENT\n"


# ---------------------------------------------------------------------------
# Allowlist-modifying boundary commit: a single commit that adds a file
# AND promotes it to the allowlist in the same change. The implementation
# is correct (`git show <sha>:.mirror-allowlist` returns the
# post-modification body, matching the hook's `git diff --cached` view),
# but pin the contract for future readers.
# ---------------------------------------------------------------------------
class TestAllowlistModifyingBoundaryCommit:
    def test_same_commit_adds_file_and_promotes(self, tmp_path):
        """One commit that simultaneously adds `widget.js` AND adds it
        to `.mirror-allowlist`. Tree-time semantics return the
        POST-modification allowlist (which includes `widget.js`), so
        `widget.js` classifies as `public` — the symmetric behavior to
        the commit-msg hook reading `.mirror-allowlist` from the
        working tree at commit time."""
        _init(tmp_path)
        # Seed an unrelated allowlist so the boundary commit isn't the
        # first allowlist commit (which would be a degenerate case).
        _commit(
            tmp_path,
            {".mirror-allowlist": "README.md\n", "README.md": "init\n"},
            "chore: seed",
        )
        # Boundary commit: add widget.js AND grow the allowlist to
        # include it, in a single commit.
        sha = _commit(
            tmp_path,
            {
                ".mirror-allowlist": "README.md\nwidget.js\n",
                "widget.js": "console.log('hi');\n",
            },
            "feat: add widget + promote to public",
        )

        cls = mirror._classify_commit_paths(sha, tmp_path)
        # widget.js classifies as public — the allowlist's own
        # post-modification body (which now lists widget.js) governs
        # THIS commit's classification, matching the commit-msg hook's
        # `git diff --cached` view. The point of the test is the
        # per-commit snapshot semantics: a commit can simultaneously
        # add a file AND promote it, and the tree-time allowlist read
        # is the post-modification body.
        #
        # (Aside on `.mirror-allowlist` itself: it falls into
        # `unmatched` because the allowlist doesn't list itself by
        # convention. That's a separate cross-cutting concern from
        # what this test is pinning.)
        assert "widget.js" in cls["public"]


# ---------------------------------------------------------------------------
# _merge_has_evil_public_content: regression guard for the merge branch
# of the bug fix. Today only `_classify_commit_paths` has commit-time
# coverage in unit tests; the merge guard was exercised only
# transitively through the harness.
# ---------------------------------------------------------------------------
class TestMergeHasEvilPublicContent:
    def test_evil_merge_classified_at_merge_time(self, tmp_path):
        """A merge that introduces conflict-resolution content on a path
        that is NOT yet in the merge-time allowlist must classify as
        unmatched (not as evil-merge public content). Setup:
          - Commit M0: seed allowlist (excludes foo.txt).
          - Branch B from M0: add foo.txt with content "B".
          - Branch A from M0: add bar.txt (something to diverge).
          - Merge B into A with a conflict-resolution edit on foo.txt
            ("M" content, differing from both parents).
          - LATER commit on the merged branch: grow the allowlist to
            include foo.txt.

        At the merge SHA's tree, foo.txt is NOT in `.mirror-allowlist`
        — so the merge introduces NO public-classified content.
        Pre-fix (HEAD-allowlist): the merge guard saw foo.txt as
        public via HEAD's grown allowlist and refused the merge.
        Post-fix (commit-time): the merge guard reads merge-time
        allowlist and correctly classifies foo.txt as `unmatched`.
        """
        _init(tmp_path)
        # M0: seed minimal allowlist excluding foo.txt.
        _commit(
            tmp_path,
            {".mirror-allowlist": "README.md\n", "README.md": "init\n"},
            "chore: seed allowlist",
        )

        # Branch B: add foo.txt with content "B".
        _git(["checkout", "-q", "-b", "B"], tmp_path)
        _commit(tmp_path, {"foo.txt": "B\n"}, "feat(B): add foo")

        # Switch back to main, branch A: add bar.txt to diverge.
        _git(["checkout", "-q", "main"], tmp_path)
        _commit(tmp_path, {"bar.txt": "A\n"}, "feat(A): add bar")

        # Merge B into main with a conflict-resolution edit on foo.txt.
        # We do this manually (no actual conflict here since main
        # doesn't have foo.txt) by completing the merge and then
        # amending in an "evil" content for foo.txt.
        _git(["merge", "--no-ff", "--no-edit", "B"], tmp_path)
        # Mutate foo.txt to a third value (differing from both parents'
        # contributions) and amend into the merge commit.
        (tmp_path / "foo.txt").write_text("M (evil)\n")
        _git(["add", "foo.txt"], tmp_path)
        _git(["commit", "-q", "--amend", "--no-edit", "--no-verify"], tmp_path)
        merge_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

        # AT THIS POINT: under the merge's tree-time allowlist, foo.txt
        # is unmatched.
        evil = mirror._merge_has_evil_public_content(merge_sha, tmp_path)
        assert evil == [], (
            f"merge-time allowlist excludes foo.txt → evil should be "
            f"empty, got {evil!r}"
        )

        # Now grow HEAD's allowlist to include foo.txt — this is the
        # scenario that pre-fix would have re-classified foo.txt as
        # public retroactively at the merge SHA.
        _commit(
            tmp_path,
            {".mirror-allowlist": "README.md\nfoo.txt\n"},
            "chore: promote foo.txt",
        )
        # The merge SHA's classification must NOT shift: still empty.
        evil2 = mirror._merge_has_evil_public_content(merge_sha, tmp_path)
        assert evil2 == [], (
            f"after HEAD allowlist grew, merge-time classification must "
            f"remain unchanged, got {evil2!r}"
        )


# ---------------------------------------------------------------------------
# _build_priv_to_pub_map: regression guard for the tag-mapping pass.
# The fix changed `_build_priv_to_pub_map` from HEAD's allowlist to
# commit-time. Pre-fix, after an allowlist grow, the private-side
# fingerprint at an earlier publish would expand to include paths that
# weren't on the public side at that publish — and the fingerprint would
# never match → tags hold back even when properly published.
# ---------------------------------------------------------------------------
class TestBuildPrivToPubMapPostAllowlistGrow:
    def test_tag_propagates_after_later_allowlist_grow(self, tmp_path):
        """Three-commit private repo:
          - C1: seed allowlist with `core/**`.
          - C2 (publish): add `core/main.py` → mirrors as public commit.
          - C3 (publish): grow allowlist to include `extras/**` AND
            add `extras/util.py` (allowed under post-grow rules).

        Mirror C1+C2 first, tag v1.0.0 on C2. The bootstrap commit on
        the public side is the seed; C2 is the first publish on the
        public side.

        Now apply C3 (a publish, mirrors as a second public commit).
        Tag v1.0.0 should still propagate to the public commit
        corresponding to C2. Pre-fix, after C3 grew the allowlist,
        `_build_priv_to_pub_map` would build C2's fingerprint under
        HEAD's (post-C3) allowlist — including `extras/**` paths that
        weren't even present at C2's tree — and the fingerprint
        wouldn't match the public C2 commit. Tag would hold back.
        """
        # Build the private repo.
        priv = tmp_path / "priv"
        _init(priv)
        _git(["config", "tag.gpgsign", "false"], priv)

        # We need the `_public_trailer` parser inside the test for
        # _build_priv_to_pub_map. Mirror's helper handles it.
        parser = mirror._import_trailer_parser(_REPO)

        # C1: seed allowlist + a private file (so C1 itself is private,
        # not a publish).
        _commit(
            priv,
            {".mirror-allowlist": "core/**\n", "private.txt": "p\n"},
            "chore: seed allowlist (private)",
        )

        # C2: add core/main.py with a publish trailer.
        c2_msg = (
            "feat: core main\n\n"
            "--- public ---\n"
            "feat: core main\n"
        )
        (priv / "core").mkdir()
        (priv / "core" / "main.py").write_text("print('hi')\n")
        _git(["add", "core/main.py"], priv)
        _git(["commit", "-q", "--no-verify", "-m", c2_msg], priv)
        c2_sha = _git(["rev-parse", "HEAD"], priv).stdout.strip()

        # Build the public clone by hand to mirror C2 (avoids spinning
        # up the full mirror tool inside this unit test). The public
        # clone has a SINGLE commit reflecting C2's public-classified
        # tree: just `core/main.py`. We can't have an additional init
        # commit because the priv-side fingerprint at C2 is built from
        # the public-classified subset of C2's full tree (just
        # core/main.py), and a pub-side init commit with a README
        # would shift its public fingerprint to (README.md,
        # core/main.py) — never matching. The single-commit shape here
        # mirrors what a real `--bootstrap` produces.
        pub = tmp_path / "pub"
        pub.mkdir()
        _git(["init", "-q", "-b", "main"], pub)
        _git(["config", "commit.gpgsign", "false"], pub)
        (pub / "core").mkdir()
        (pub / "core" / "main.py").write_text("print('hi')\n")
        _git(["add", "core/main.py"], pub)
        _git(["commit", "-q", "--no-verify", "-m", "feat: core main"], pub)
        c2_pub_sha = _git(["rev-parse", "HEAD"], pub).stdout.strip()

        # Capture the priv→pub map AS-OF C2 (before C3 grows the
        # allowlist). Should map C2 → c2_pub_sha.
        m_before = mirror._build_priv_to_pub_map(priv, pub, parser)
        assert m_before.get(c2_sha) == c2_pub_sha, (
            f"baseline: c2 must map to c2_pub_sha, got {m_before!r}"
        )

        # C3: grow the allowlist + add extras/util.py with a publish.
        c3_msg = (
            "feat: extras util + grow allowlist\n\n"
            "--- public ---\n"
            "feat: extras util\n"
        )
        (priv / ".mirror-allowlist").write_text("core/**\nextras/**\n")
        (priv / "extras").mkdir()
        (priv / "extras" / "util.py").write_text("# util\n")
        _git(["add", ".mirror-allowlist", "extras/util.py"], priv)
        _git(["commit", "-q", "--no-verify", "-m", c3_msg], priv)

        # NOW build the priv→pub map. Public side STILL only has C2
        # (we haven't applied C3 yet, mirroring the realistic state
        # between mirror runs). The fix's invariant: C2 must STILL
        # map to c2_pub_sha despite HEAD's allowlist having grown.
        m_after = mirror._build_priv_to_pub_map(priv, pub, parser)
        assert m_after.get(c2_sha) == c2_pub_sha, (
            f"post-grow: c2 must map to c2_pub_sha, got {m_after!r}. "
            f"Pre-fix this would be missing because C2's fingerprint "
            f"under HEAD's allowlist would include extras/** paths "
            f"that don't exist at C2's tree."
        )
