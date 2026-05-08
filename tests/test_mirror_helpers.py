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
    def test_text_kwarg_overrides_path(self, tmp_path):
        sys.path.insert(0, str(_REPO / ".githooks"))
        import _match  # noqa: WPS433 — sibling-module import, mirror style

        # Even with NO file on disk, the text kwarg classifies cleanly.
        result = _match.classify(
            ["a.txt", "b.txt"],
            allowlist_text="a.txt\n",
        )
        assert result["public"] == ["a.txt"]
        assert result["unmatched"] == ["b.txt"]

    def test_empty_text_yields_all_unmatched(self, tmp_path):
        sys.path.insert(0, str(_REPO / ".githooks"))
        import _match  # noqa: WPS433

        result = _match.classify(
            ["x", "y"], allowlist_text="",
        )
        assert result["public"] == []
        assert result["private"] == []
        assert result["unmatched"] == ["x", "y"]
