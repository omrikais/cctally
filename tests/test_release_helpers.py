"""Tests for bin/cctally release-automation helpers (issue #24)."""
import argparse
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Load bin/cctally as a module. The script has no .py extension, so we
# supply an explicit SourceFileLoader (otherwise spec_from_file_location
# returns None for unrecognized suffixes).
_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "bin" / "cctally"
_LOADER = importlib.machinery.SourceFileLoader("cctally", str(_SCRIPT))
_SPEC = importlib.util.spec_from_loader("cctally", _LOADER)
cctally = importlib.util.module_from_spec(_SPEC)
sys.modules["cctally"] = cctally
_LOADER.exec_module(cctally)


class TestParseSemver:
    def test_stable(self):
        assert cctally._release_parse_semver("1.0.0") == (1, 0, 0, None, None)

    def test_prerelease_rc(self):
        assert cctally._release_parse_semver("1.1.0-rc.1") == (1, 1, 0, "rc", 1)

    def test_prerelease_alpha(self):
        assert cctally._release_parse_semver("2.0.0-alpha.5") == (2, 0, 0, "alpha", 5)

    def test_zero_components(self):
        assert cctally._release_parse_semver("0.0.0") == (0, 0, 0, None, None)

    @pytest.mark.parametrize("bad", [
        "v1.0.0",                # leading v
        "1.0",                   # missing patch
        "01.0.0",                # leading zero major
        "1.0.0-",                # empty prerelease
        "1.0.0-rc",              # missing prerelease counter
        "1.0.0-rc.01",           # leading zero in counter
        "1.0.0-1rc.1",           # prerelease id starts with digit
    ])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            cctally._release_parse_semver(bad)


class TestFormatSemver:
    def test_stable(self):
        assert cctally._release_format_semver(1, 0, 0) == "1.0.0"

    def test_prerelease(self):
        assert cctally._release_format_semver(1, 1, 0, "rc", 2) == "1.1.0-rc.2"


class TestComputeNextVersion:
    @pytest.mark.parametrize("current,kind,bump,expected", [
        ("1.0.0", "patch", None, "1.0.1"),
        ("1.0.0", "minor", None, "1.1.0"),
        ("1.0.0", "major", None, "2.0.0"),
        ("1.0.0", "prerelease", "minor", "1.1.0-rc.1"),
        ("1.0.0", "prerelease", "major", "2.0.0-rc.1"),
        ("1.0.0", "prerelease", "patch", "1.0.1-rc.1"),
        ("1.1.0-rc.1", "prerelease", None, "1.1.0-rc.2"),
        ("1.1.0-rc.5", "prerelease", None, "1.1.0-rc.6"),
        ("1.1.0-rc.2", "finalize", None, "1.1.0"),
        (None, "patch", None, "0.0.1"),
        (None, "minor", None, "0.1.0"),
        (None, "major", None, "1.0.0"),
    ])
    def test_happy(self, current, kind, bump, expected):
        assert cctally._release_compute_next_version(current, kind, bump, "rc") == expected

    def test_prerelease_id_override(self):
        assert cctally._release_compute_next_version("1.0.0", "prerelease", "minor", "alpha") == "1.1.0-alpha.1"

    def test_prerelease_required_bump_when_stable(self):
        with pytest.raises(ValueError, match="--bump required"):
            cctally._release_compute_next_version("1.0.0", "prerelease", None, "rc")

    def test_bump_kind_on_prerelease_refuses(self):
        with pytest.raises(ValueError, match="run 'cctally release finalize'"):
            cctally._release_compute_next_version("1.1.0-rc.1", "patch", None, "rc")

    def test_bump_flag_on_prerelease_refuses(self):
        with pytest.raises(ValueError, match="--bump invalid when current"):
            cctally._release_compute_next_version("1.1.0-rc.1", "prerelease", "minor", "rc")

    def test_finalize_when_stable_refuses(self):
        with pytest.raises(ValueError, match="not a prerelease"):
            cctally._release_compute_next_version("1.0.0", "finalize", None, "rc")


class TestParseChangelog:
    def test_simple_unreleased(self):
        text = """# Changelog

## [Unreleased]

### Added
- Foo
- Bar
"""
        parsed = cctally._release_parse_changelog(text)
        assert parsed["sections"][0]["heading"] == "## [Unreleased]"
        added = next(s for s in parsed["sections"][0]["subsections"] if s["heading"] == "### Added")
        assert added["bullets"] == ["- Foo", "- Bar"]

    def test_unreleased_plus_prior_release(self):
        text = """# Changelog

## [Unreleased]

### Fixed
- Quux

## [1.0.0] - 2026-01-01

### Added
- Initial
"""
        parsed = cctally._release_parse_changelog(text)
        assert len(parsed["sections"]) == 2
        assert parsed["sections"][1]["heading"] == "## [1.0.0] - 2026-01-01"

    def test_multiline_bullets_preserved(self):
        text = """# Changelog

## [Unreleased]

### Added
- First line
  continuation
- Second
"""
        parsed = cctally._release_parse_changelog(text)
        added = parsed["sections"][0]["subsections"][0]
        # Continuation lines are part of the previous bullet block
        assert added["bullets"][0] == "- First line\n  continuation"
        assert added["bullets"][1] == "- Second"

    def test_code_fence_inside_bullet(self):
        text = """# Changelog

## [Unreleased]

### Added
- Foo with code:
  ```
  ### Fixed
  ```
- Bar
"""
        parsed = cctally._release_parse_changelog(text)
        added = parsed["sections"][0]["subsections"][0]
        assert len(added["bullets"]) == 2  # fenced "### Fixed" not mistaken for heading


class TestStampChangelog:
    def test_basic_stamp(self):
        text = """# Changelog

## [Unreleased]

### Added
- Foo
"""
        new_text, body = cctally._release_stamp_changelog(text, "1.0.0", "2026-05-07")
        assert "## [1.0.0] - 2026-05-07" in new_text
        assert "## [Unreleased]\n\n## [1.0.0]" in new_text  # empty Unreleased + new section
        assert "- Foo" in new_text
        # Body string is the new section's content
        assert body == "### Added\n- Foo"
        # Canonical-body invariant: same body string is reused for the
        # public-block of the stamp commit, the tag annotation, and the
        # GH Release notes (spec §7.4). Re-parsing the stamped CHANGELOG
        # and asking for the new section's canonical body must yield the
        # exact same string.
        reparsed = cctally._release_parse_changelog(new_text)
        new_section = next(
            s for s in reparsed["sections"] if s["heading"] == "## [1.0.0] - 2026-05-07"
        )
        assert cctally._release_canonical_body(new_section) == body

    def test_empty_unreleased_raises(self):
        text = """# Changelog

## [Unreleased]
"""
        with pytest.raises(ValueError, match=r"\[Unreleased\] is empty"):
            cctally._release_stamp_changelog(text, "1.0.0", "2026-05-07")

    def test_only_empty_subsections_raises(self):
        text = """# Changelog

## [Unreleased]

### Added

### Fixed
"""
        with pytest.raises(ValueError, match=r"\[Unreleased\] is empty"):
            cctally._release_stamp_changelog(text, "1.0.0", "2026-05-07")

    def test_multiple_subsections(self):
        text = """# Changelog

## [Unreleased]

### Added
- A1

### Fixed
- F1
- F2
"""
        new_text, body = cctally._release_stamp_changelog(text, "1.1.0", "2026-05-07")
        assert body == "### Added\n- A1\n\n### Fixed\n- F1\n- F2"

    def test_stamp_preserves_prior_release(self):
        text = """# Changelog

## [Unreleased]

### Added
- New

## [1.0.0] - 2026-01-01

### Added
- Old
"""
        new_text, _ = cctally._release_stamp_changelog(text, "1.1.0", "2026-05-07")
        # Order: Unreleased (empty), then 1.1.0 (new), then 1.0.0 (prior)
        idx_unr = new_text.find("## [Unreleased]")
        idx_new = new_text.find("## [1.1.0] - 2026-05-07")
        idx_old = new_text.find("## [1.0.0] - 2026-01-01")
        assert idx_unr < idx_new < idx_old
        # All preserved
        assert "- New" in new_text
        assert "- Old" in new_text


class TestStampChangelogIdempotency:
    def test_double_stamp_preserves_blank_line_count(self):
        text = """# Changelog

## [Unreleased]

### Added
- First entry
"""
        once, _ = cctally._release_stamp_changelog(text, "1.0.0", "2026-05-07")
        # Re-prime [Unreleased] for a second stamp
        text2 = once.replace(
            "## [Unreleased]\n",
            "## [Unreleased]\n\n### Fixed\n- F1\n",
            1,
        )
        twice, _ = cctally._release_stamp_changelog(text2, "1.1.0", "2026-05-08")
        # Count blank lines between preamble line "# Changelog" and the first "## [...]"
        # Should be exactly one blank line (matching the original input convention).
        idx_h = twice.index("# Changelog\n")
        idx_first_section = twice.index("\n## [")  # first section heading
        gap = twice[idx_h + len("# Changelog\n"):idx_first_section + 1]  # +1 to include the leading \n of the heading line
        # Strip the leading newline of the heading line; the slice between is the inter-block whitespace.
        # Expect exactly one blank line: the gap should be exactly "\n" (one newline = one blank line between content lines)
        assert gap == "\n", f"expected single blank line between preamble and first section, got {gap!r}"

    def test_real_changelog_round_trip_blank_line_stable(self):
        """Stamp twice on a synthetic CHANGELOG matching the project's structure; assert no monotonic drift."""
        text = """# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com).

## [Unreleased]

### Added
- A
"""
        n1, _ = cctally._release_stamp_changelog(text, "1.0.0", "2026-05-07")
        n2_prime = n1.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Added\n- B\n", 1)
        n2, _ = cctally._release_stamp_changelog(n2_prime, "1.1.0", "2026-05-08")
        # The number of newlines BEFORE the FIRST section heading should be the same in n1 and n2
        # (i.e., no monotonic drift across stamps)
        def newlines_before_first_section(s):
            idx = s.index("\n## [")
            # Count consecutive trailing \n in s[:idx+1]
            return len(s[:idx + 1]) - len(s[:idx + 1].rstrip("\n"))
        assert newlines_before_first_section(n1) == newlines_before_first_section(n2), \
            f"blank-line drift: stamp 1 had {newlines_before_first_section(n1)} newlines, stamp 2 had {newlines_before_first_section(n2)}"


@pytest.fixture
def temp_git_repo(tmp_path, monkeypatch):
    """Create a tmp git repo with one commit on `main`.

    `core.hooksPath=/dev/null` skips this repo's pre-commit / commit-msg
    hooks (we don't want the project's commit hooks running on a fixture
    repo with synthetic content). `user.email`/`user.name` configured so
    `git commit` works regardless of the host's global git identity.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    subprocess.run(["git", "init", "-q", "-b", "main"], check=True, cwd=repo)
    subprocess.run(["git", "config", "core.hooksPath", "/dev/null"], check=True, cwd=repo)
    subprocess.run(["git", "config", "user.email", "test@test"], check=True, cwd=repo)
    subprocess.run(["git", "config", "user.name", "Test"], check=True, cwd=repo)
    # Disable any signing inherited from host global config so fixture
    # commits + tags are deterministic regardless of the operator's
    # `commit.gpgsign` / `tag.gpgsign` / `gpg.format` settings.
    subprocess.run(["git", "config", "commit.gpgsign", "false"], check=True, cwd=repo)
    subprocess.run(["git", "config", "tag.gpgsign", "false"], check=True, cwd=repo)
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- Foo\n"
    )
    subprocess.run(["git", "add", "."], check=True, cwd=repo)
    subprocess.run(["git", "commit", "-q", "-m", "init"], check=True, cwd=repo)
    return repo


class TestPreflight:
    def test_preflight_clean_main(self, temp_git_repo, monkeypatch):
        """Clean tree on main: branch + clean-tree preflights pass silently."""
        monkeypatch.setattr(cctally, "CHANGELOG_PATH", temp_git_repo / "CHANGELOG.md")
        # Run preflights directly — expect no exception.
        cctally._release_preflight_branch(allow_branch=None)
        cctally._release_preflight_clean_tree()

    def test_preflight_wrong_branch_refuses(self, temp_git_repo, monkeypatch):
        """On a feature branch with no --allow-branch override, exit 2."""
        monkeypatch.setattr(cctally, "CHANGELOG_PATH", temp_git_repo / "CHANGELOG.md")
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature/foo"],
            check=True, cwd=temp_git_repo,
        )
        with pytest.raises(SystemExit) as e:
            cctally._release_preflight_branch(allow_branch=None)
        assert e.value.code == 2

    def test_preflight_allow_branch(self, temp_git_repo, monkeypatch):
        """--allow-branch matching the current branch lets the preflight pass."""
        monkeypatch.setattr(cctally, "CHANGELOG_PATH", temp_git_repo / "CHANGELOG.md")
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature/foo"],
            check=True, cwd=temp_git_repo,
        )
        # No raise.
        cctally._release_preflight_branch(allow_branch="feature/foo")

    def test_preflight_dirty_tree_refuses(self, temp_git_repo, monkeypatch):
        """Untracked file in working tree: clean-tree preflight exits 2."""
        monkeypatch.setattr(cctally, "CHANGELOG_PATH", temp_git_repo / "CHANGELOG.md")
        (temp_git_repo / "dirty.txt").write_text("x")
        with pytest.raises(SystemExit) as e:
            cctally._release_preflight_clean_tree()
        assert e.value.code == 2


class TestPhaseStamp:
    """Phase 1 — `_release_run_phase_stamp` wires CHANGELOG rewrite + commit
    behind an idempotent done-signal (spec §5.1)."""

    def test_stamp_commit_signal_done_after_run(
        self, temp_git_repo, monkeypatch
    ):
        """First run: rewrites CHANGELOG, lands a `chore(release): vX.Y.Z`
        commit, returns the new HEAD sha; signal-done flips True."""
        monkeypatch.setattr(cctally, "CHANGELOG_PATH", temp_git_repo / "CHANGELOG.md")
        monkeypatch.setenv("CCTALLY_RELEASE_DATE_UTC", "2026-05-07")
        sha = cctally._release_run_phase_stamp(
            "1.0.0", remote="origin", invocation="cctally release minor"
        )
        text = (temp_git_repo / "CHANGELOG.md").read_text()
        assert "## [1.0.0] - 2026-05-07" in text
        log = subprocess.check_output(
            ["git", "log", "-1", "--format=%H %s"],
            text=True, cwd=temp_git_repo,
        ).strip()
        assert sha in log
        assert "chore(release): v1.0.0" in log
        # Signal-done now reports True with the same SHA.
        done, sig_sha = cctally._release_phase_stamp_done("1.0.0")
        assert done is True
        assert sig_sha == sha

    def test_stamp_idempotent(self, temp_git_repo, monkeypatch):
        """Second invocation short-circuits: same SHA, no new commit."""
        monkeypatch.setattr(cctally, "CHANGELOG_PATH", temp_git_repo / "CHANGELOG.md")
        monkeypatch.setenv("CCTALLY_RELEASE_DATE_UTC", "2026-05-07")
        sha1 = cctally._release_run_phase_stamp(
            "1.0.0", remote="origin", invocation="cctally release minor"
        )
        commit_count_before = int(
            subprocess.check_output(
                ["git", "rev-list", "--count", "HEAD"],
                text=True, cwd=temp_git_repo,
            ).strip()
        )
        sha2 = cctally._release_run_phase_stamp(
            "1.0.0", remote="origin", invocation="cctally release minor"
        )
        assert sha1 == sha2
        commit_count_after = int(
            subprocess.check_output(
                ["git", "rev-list", "--count", "HEAD"],
                text=True, cwd=temp_git_repo,
            ).strip()
        )
        assert commit_count_after == commit_count_before


class TestPhaseTag:
    """Phase 2 — `_release_run_phase_tag` writes an annotated tag and pushes
    commit + tag with `--follow-tags`. Idempotent (spec §5.1)."""

    def _setup_with_stamp(self, repo, monkeypatch):
        """Build a bare upstream, push main, run Phase 1. Returns
        (upstream_path, stamp_sha)."""
        monkeypatch.setattr(cctally, "CHANGELOG_PATH", repo / "CHANGELOG.md")
        monkeypatch.setenv("CCTALLY_RELEASE_DATE_UTC", "2026-05-07")
        upstream = repo.parent / "upstream.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(upstream)], check=True
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(upstream)],
            check=True, cwd=repo,
        )
        subprocess.run(
            ["git", "push", "-q", "origin", "main"], check=True, cwd=repo
        )
        stamp_sha = cctally._release_run_phase_stamp(
            "1.0.0", "origin", "cctally release minor", "minor"
        )
        return upstream, stamp_sha

    def test_tag_creates_annotated_tag(self, temp_git_repo, monkeypatch):
        """Annotated tag landed locally and on remote; tag object is `tag`,
        not a lightweight `commit`."""
        upstream, stamp_sha = self._setup_with_stamp(temp_git_repo, monkeypatch)
        cctally._release_run_phase_tag("1.0.0", "origin", stamp_sha)
        out = subprocess.check_output(
            ["git", "tag", "-l", "v1.0.0"], text=True, cwd=temp_git_repo,
        ).strip()
        assert out == "v1.0.0"
        out = subprocess.check_output(
            ["git", "ls-remote", "--tags", str(upstream), "refs/tags/v1.0.0"],
            text=True,
        ).strip()
        assert "v1.0.0" in out
        kind = subprocess.check_output(
            ["git", "cat-file", "-t", "v1.0.0"],
            text=True, cwd=temp_git_repo,
        ).strip()
        assert kind == "tag"

    def test_tag_idempotent(self, temp_git_repo, monkeypatch):
        """Second invocation no-ops: same local + remote state, no exception."""
        _, stamp_sha = self._setup_with_stamp(temp_git_repo, monkeypatch)
        cctally._release_run_phase_tag("1.0.0", "origin", stamp_sha)
        # Second run is a no-op (signal-done short-circuit).
        cctally._release_run_phase_tag("1.0.0", "origin", stamp_sha)
        out = subprocess.check_output(
            ["git", "tag", "-l", "v1.0.0"], text=True, cwd=temp_git_repo,
        ).strip()
        assert out == "v1.0.0"

    def test_body_canonical_three_sources_invariant(
        self, temp_git_repo, monkeypatch
    ):
        """Spec §7.4: the canonical body string from Phase 1's commit
        public-block MUST equal Phase 2's tag annotation body byte-for-byte.

        We extract:
          - commit body = `git log -1 --format=%B` minus the prefix up to
            and including `--- public ---\\nchore(release): vX.Y.Z\\n\\n`,
            stripping the trailing newline (the build_stamp_message wraps
            the canonical body with one trailing `\\n`).
          - tag body = `git cat-file tag` minus the headers + tag-name
            line, stripping the trailing newline (the annotation file
            also wraps the canonical body with one trailing `\\n`).
        Both should equal `_release_canonical_body(...)` from the section
        re-parsed from CHANGELOG.md.
        """
        _, stamp_sha = self._setup_with_stamp(temp_git_repo, monkeypatch)
        cctally._release_run_phase_tag("1.0.0", "origin", stamp_sha)

        # Source 0: the canonical body, recomputed from CHANGELOG.
        text = (temp_git_repo / "CHANGELOG.md").read_text(encoding="utf-8")
        parsed = cctally._release_parse_changelog(text)
        section = next(
            s for s in parsed["sections"]
            if s["heading"].lstrip().startswith("## [1.0.0]")
        )
        canonical = cctally._release_canonical_body(section)

        # Source 1: commit message public block.
        commit_msg = subprocess.check_output(
            ["git", "log", "-1", "--format=%B"],
            text=True, cwd=temp_git_repo,
        )
        delim = "--- public ---\nchore(release): v1.0.0\n\n"
        assert delim in commit_msg
        commit_body = commit_msg.split(delim, 1)[1]
        # The build_stamp_message wraps the body with one trailing "\n".
        # `git log` adds its own trailing "\n" too, so strip both.
        commit_body = commit_body.rstrip("\n")

        # Source 2: tag annotation body.
        tag_obj = subprocess.check_output(
            ["git", "cat-file", "tag", "v1.0.0"],
            text=True, cwd=temp_git_repo,
        )
        # `git cat-file tag` output: headers, blank line, then the message.
        # Message is: "v1.0.0\n\n<body>\n" (per Phase 2's annotation file).
        _headers, _, message = tag_obj.partition("\n\n")
        # Strip PGP signature block if present (defensive — fixture tags
        # are unsigned so this is a no-op, but documents the contract).
        pgp_marker = "-----BEGIN PGP SIGNATURE-----"
        if pgp_marker in message:
            message = message.split(pgp_marker, 1)[0].rstrip("\n")
        # Drop the leading "v1.0.0\n\n"; strip trailing "\n".
        first_line, _, tag_body = message.partition("\n\n")
        assert first_line == "v1.0.0"
        tag_body = tag_body.rstrip("\n")

        assert commit_body == canonical
        assert tag_body == canonical
        assert commit_body == tag_body


class TestResumeSafety:
    """Issue #24 — `--resume` correctness gaps surfaced in code review."""

    def test_resume_refuses_when_head_is_not_stamp_commit(
        self, temp_git_repo, monkeypatch
    ):
        """Done-signal must refuse if HEAD's CHANGELOG carries the stamp
        but HEAD's subject is not `chore(release): vX.Y.Z` — meaning an
        unrelated commit landed on top. Tagging that SHA would mis-tag."""
        monkeypatch.setattr(cctally, "CHANGELOG_PATH", temp_git_repo / "CHANGELOG.md")
        monkeypatch.setenv("CCTALLY_RELEASE_DATE_UTC", "2026-05-07")
        # Stamp succeeds.
        cctally._release_run_phase_stamp(
            "1.0.0", "origin", "cctally release minor", "minor"
        )
        # Operator lands an unrelated commit on top.
        (temp_git_repo / "README.md").write_text("oops")
        subprocess.run(
            ["git", "add", "."], check=True, cwd=temp_git_repo
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "oops on top"],
            check=True, cwd=temp_git_repo,
        )
        # Done-signal must refuse with exit 2 (NOT silently return done).
        with pytest.raises(SystemExit) as e:
            cctally._release_phase_stamp_done("1.0.0")
        assert e.value.code == 2

    def test_tag_push_local_exists_remote_missing(
        self, temp_git_repo, monkeypatch
    ):
        """Resume scenario: tag created locally on a prior run but the push
        failed; operator pushed `main` manually. Re-running Phase 2 must
        skip `git tag` (already exists) and push the tag explicitly."""
        # Reuse TestPhaseTag's setup helper.
        tag_test = TestPhaseTag()
        upstream, stamp_sha = tag_test._setup_with_stamp(
            temp_git_repo, monkeypatch
        )
        # Simulate: tag created locally but never pushed.
        subprocess.check_call(
            [
                "git", "tag", "-a", "v1.0.0",
                "-m", "v1.0.0\n\n### Added\n- Foo\n",
                stamp_sha,
            ],
            cwd=temp_git_repo,
        )
        # Operator manually pushed main (without the tag).
        subprocess.check_call(
            ["git", "push", "-q", "origin", "main"],
            cwd=temp_git_repo,
        )
        # phase-tag-done returns False (remote tag missing).
        assert not cctally._release_phase_tag_done("1.0.0", "origin")
        # Re-run must succeed: skip `git tag`, push the tag explicitly.
        cctally._release_run_phase_tag("1.0.0", "origin", stamp_sha)
        # Remote should now have the tag.
        out = subprocess.check_output(
            ["git", "ls-remote", "--tags", str(upstream), "refs/tags/v1.0.0"],
            text=True,
        ).strip()
        assert "v1.0.0" in out


class TestPublicCloneDiscovery:
    """Spec §9.1 — Public-clone path discovery: --public-clone > git config
    > marker file. Each source silently skipped when missing; refuses
    with exit 2 only when ALL three sources are absent."""

    def test_flag_wins(self, temp_git_repo, tmp_path):
        """--public-clone <path> is the highest priority source."""
        public = tmp_path / "pub"
        public.mkdir()
        subprocess.run(["git", "init", "-q", str(public)], check=True)
        args = argparse.Namespace(public_clone=str(public))
        result = cctally._release_discover_public_clone(args)
        assert result.resolve() == public.resolve()

    def test_git_config_fallback(self, temp_git_repo, tmp_path):
        """No flag → falls back to `git config release.publicClone`.

        The key is camelCase (git 2.46+ rejects underscore-bearing
        keys at write time). Lookup is case-insensitive on the
        trailing variable.
        """
        public = tmp_path / "pub"
        public.mkdir()
        subprocess.run(["git", "init", "-q", str(public)], check=True)
        subprocess.run(
            ["git", "config", "release.publicClone", str(public)],
            check=True, cwd=temp_git_repo,
        )
        args = argparse.Namespace(public_clone=None)
        result = cctally._release_discover_public_clone(args)
        assert result.resolve() == public.resolve()

    def test_marker_file_fallback(
        self, temp_git_repo, tmp_path, monkeypatch
    ):
        """No flag, no git-config key → falls back to APP_DIR marker file."""
        public = tmp_path / "pub"
        public.mkdir()
        subprocess.run(["git", "init", "-q", str(public)], check=True)
        marker_dir = tmp_path / "share-cctally"
        marker_dir.mkdir()
        marker = marker_dir / "release-public-clone-path"
        marker.write_text(str(public) + "\n")
        monkeypatch.setattr(cctally, "APP_DIR", marker_dir)
        args = argparse.Namespace(public_clone=None)
        result = cctally._release_discover_public_clone(args)
        assert result.resolve() == public.resolve()

    def test_no_discovery_refuses(
        self, temp_git_repo, tmp_path, monkeypatch
    ):
        """All three sources absent → exit 2 with discovery-hint message."""
        marker_dir = tmp_path / "share-cctally"
        marker_dir.mkdir()
        monkeypatch.setattr(cctally, "APP_DIR", marker_dir)
        args = argparse.Namespace(public_clone=None)
        with pytest.raises(SystemExit) as e:
            cctally._release_discover_public_clone(args)
        assert e.value.code == 2


class TestPhaseMirrorDoneSignal:
    """Spec §9.1 — `_release_phase_mirror_done` is read-only; returns
    False on any subprocess failure (no origin remote, network glitch).
    Non-error fall-through is what lets the caller idempotently re-run
    the three sub-steps after a partial Phase 3 failure."""

    def test_returns_false_when_no_origin_remote(self, tmp_path):
        """A git repo without an `origin` remote can't have the tag on
        public origin → done-signal is False (not raise)."""
        public = tmp_path / "pub"
        public.mkdir()
        subprocess.run(["git", "init", "-q", str(public)], check=True)
        # No `git remote add origin` — `git remote get-url origin` will
        # exit non-zero, which the helper traps as "not done."
        assert cctally._release_phase_mirror_done("1.0.0", public) is False


class TestPhaseGh:
    """Phase 4 — `_release_run_phase_gh` issues `gh release create` with
    an auth-fallback path that returns 0 (don't fail the whole release;
    spec §9.2). Fake `gh` is PATH-injected and records argv to a logfile
    so we can assert on the exact command shape."""

    def _make_fake_gh(self, tmp_path, exit_code=0, status_exit=0):
        """Create a PATH-injected fake `gh` that records argv to a logfile.

        `auth status` and `api ...` calls return `status_exit` (controls
        the auth probe outcome). Anything else (i.e. `release create` and
        `release view`) returns `exit_code`. Returning the bin dir + log
        path lets the caller monkeypatch PATH and read argv.
        """
        bin_dir = tmp_path / "fake-bin"
        bin_dir.mkdir()
        log = tmp_path / "gh-argv.log"
        script = bin_dir / "gh"
        script.write_text(
            f"""#!/usr/bin/env bash
echo "$@" >> {log}
if [[ "$1" == "auth" && "$2" == "status" ]]; then exit {status_exit}; fi
if [[ "$1" == "api" ]]; then exit {status_exit}; fi
if [[ "$1" == "release" && "$2" == "view" ]]; then exit 1; fi
exit {exit_code}
"""
        )
        script.chmod(0o755)
        return bin_dir, log

    def test_gh_happy_path(self, tmp_path, monkeypatch):
        """Auth probe passes, `gh release create` returns 0 → helper
        returns 0 and invokes `release create v1.0.0 --repo
        omrikais/cctally` with a notes file."""
        bin_dir, log = self._make_fake_gh(tmp_path)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
        rc = cctally._release_run_phase_gh("1.0.0", body="### Added\n- Foo")
        assert rc == 0
        argv_log = log.read_text()
        assert "release create v1.0.0" in argv_log
        assert "--repo omrikais/cctally" in argv_log

    def test_gh_auth_fallback(self, tmp_path, monkeypatch, capsys):
        """Auth probe fails (gh auth status / gh api both non-zero) →
        helper returns 0 (don't fail) and prints a copy-pasteable
        `gh release create` command for the operator."""
        bin_dir, log = self._make_fake_gh(tmp_path, status_exit=1)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
        rc = cctally._release_run_phase_gh("1.0.0", body="### Added\n- Foo")
        assert rc == 0  # Don't fail the whole release.
        out = capsys.readouterr().out
        assert "skipped" in out.lower() or "manual" in out.lower()
        assert "gh release create v1.0.0" in out

    def test_gh_prerelease_flag(self, tmp_path, monkeypatch):
        """A version containing `-` (e.g. `1.0.0-rc.1`) emits
        `--prerelease` to `gh release create`."""
        bin_dir, log = self._make_fake_gh(tmp_path)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
        cctally._release_run_phase_gh("1.0.0-rc.1", body="### Added\n- X")
        argv_log = log.read_text()
        assert "--prerelease" in argv_log


class TestResume:
    """Spec §5.2/§5.5 — `cmd_release --resume` short-circuit. When all four
    phase signals report done, exit 0 with `already published` and DO NOT
    invoke any phase helper. When any signal is incomplete, fall through to
    the phase loop. Harness coverage (Task 11+) does end-to-end; these
    unit tests pin the short-circuit gate behavior in isolation."""

    def _make_resume_args(self):
        """Build a minimal argparse.Namespace for `cctally release --resume`."""
        return argparse.Namespace(
            kind=None,
            resume=True,
            bump=None,
            prerelease_id=None,
            no_publish=False,
            dry_run=False,
            allow_branch=None,
            remote="origin",
            public_clone=None,
            skip_npm=False,
            brew_clone=None,
            skip_brew=False,
        )

    def _stub_preflight_and_version(self, monkeypatch, version="1.0.0"):
        """Monkeypatch preflights + version reader to no-ops. The short-
        circuit gate runs after these, so they have to pass for the gate
        to be exercised but their internals don't matter for the test."""
        monkeypatch.setattr(
            cctally, "_release_preflight_branch", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            cctally, "_release_preflight_clean_tree", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            cctally, "_release_preflight_up_to_date", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            cctally,
            "_release_read_latest_release_version",
            lambda: (version, "2026-05-07"),
        )

    def test_resume_after_complete_already_published(
        self, monkeypatch, capsys
    ):
        """All 4 phase signals report done → cmd_release exits 0 with the
        `already published` message and NEVER calls a phase runner."""
        self._stub_preflight_and_version(monkeypatch)
        # All four signals: done.
        monkeypatch.setattr(
            cctally, "_release_phase_stamp_done", lambda v: (True, "abc1234")
        )
        monkeypatch.setattr(
            cctally, "_release_phase_tag_done", lambda v, r: True
        )
        monkeypatch.setattr(
            cctally,
            "_release_discover_public_clone",
            lambda args: Path("/tmp/fake-public-clone"),
        )
        monkeypatch.setattr(
            cctally, "_release_phase_mirror_done", lambda v, c: True
        )
        monkeypatch.setattr(cctally, "_release_phase_gh_done", lambda v: True)
        # Tripwire: phase runners must NOT be called.
        called: list[str] = []
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_stamp",
            lambda *a, **kw: called.append("stamp") or "deadbeef",
        )
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_tag",
            lambda *a, **kw: called.append("tag"),
        )
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_mirror",
            lambda *a, **kw: called.append("mirror"),
        )
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_gh",
            lambda *a, **kw: called.append("gh") or 0,
        )

        rc = cctally.cmd_release(self._make_resume_args())
        assert rc == 0
        assert called == [], f"phase runners called on already-done resume: {called}"
        out = capsys.readouterr().out
        assert "release v1.0.0 already published" in out

    def test_resume_after_stamp_only(self, monkeypatch, capsys):
        """Stamp landed but later phases incomplete → short-circuit must
        NOT fire; cmd_release falls through to the phase loop. We stub
        every phase runner to a counter so we can verify the loop ran
        without exercising real git/gh state."""
        self._stub_preflight_and_version(monkeypatch)
        # Only stamp signal-done; tag/mirror/gh are not.
        monkeypatch.setattr(
            cctally, "_release_phase_stamp_done", lambda v: (True, "abc1234")
        )
        monkeypatch.setattr(
            cctally, "_release_phase_tag_done", lambda v, r: False
        )
        monkeypatch.setattr(
            cctally,
            "_release_discover_public_clone",
            lambda args: Path("/tmp/fake-public-clone"),
        )
        monkeypatch.setattr(
            cctally, "_release_phase_mirror_done", lambda v, c: False
        )
        monkeypatch.setattr(cctally, "_release_phase_gh_done", lambda v: False)
        # Stub all four phase runners + body extractor.
        called: list[str] = []
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_stamp",
            lambda *a, **kw: called.append("stamp") or "deadbeef",
        )
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_tag",
            lambda *a, **kw: called.append("tag"),
        )
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_mirror",
            lambda *a, **kw: called.append("mirror"),
        )
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_gh",
            lambda *a, **kw: called.append("gh") or 0,
        )
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_npm",
            lambda *a, **kw: called.append("npm") or 0,
        )
        monkeypatch.setattr(
            cctally,
            "_release_extract_body_from_changelog",
            lambda v: "### Added\n- Foo",
        )

        rc = cctally.cmd_release(self._make_resume_args())
        assert rc == 0
        # Phase loop ran (each helper short-circuits internally based on
        # its own signal — the stubs simulate "do the work").
        assert called == ["stamp", "tag", "mirror", "gh", "npm"]
        out = capsys.readouterr().out
        # Non-already-done resume: prints the published-line, NOT
        # `already published`.
        assert "already published" not in out
        assert "release v1.0.0 published" in out

    def test_resume_already_published_with_missing_clone_short_circuits(
        self, monkeypatch, capsys
    ):
        """Spec §5.5 — fully-published-but-clone-missing path. The operator's
        public-clone configuration is gone (laptop restored from backup,
        marker deleted, git config unset, no flag), but the gh release
        already exists. `--resume` must trust the gh release as proof of
        phase 3 completion and exit 0 with `already published`, NOT exit
        2 from the discovery helper.
        """
        self._stub_preflight_and_version(monkeypatch)
        # Stamp + tag + gh: done. Discovery raises SystemExit(2).
        monkeypatch.setattr(
            cctally, "_release_phase_stamp_done", lambda v: (True, "abc1234")
        )
        monkeypatch.setattr(
            cctally, "_release_phase_tag_done", lambda v, r: True
        )

        def _raise_exit(*a, **kw):
            raise SystemExit(2)

        monkeypatch.setattr(
            cctally, "_release_discover_public_clone", _raise_exit
        )
        # Tripwire: mirror_done helper must NOT be reached (we trust gh).
        called: list[str] = []
        monkeypatch.setattr(
            cctally,
            "_release_phase_mirror_done",
            lambda v, c: called.append("mirror_done") or True,
        )
        monkeypatch.setattr(cctally, "_release_phase_gh_done", lambda v: True)
        # Tripwire: phase runners must NOT be called either.
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_stamp",
            lambda *a, **kw: called.append("stamp") or "deadbeef",
        )
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_tag",
            lambda *a, **kw: called.append("tag"),
        )
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_mirror",
            lambda *a, **kw: called.append("mirror"),
        )
        monkeypatch.setattr(
            cctally,
            "_release_run_phase_gh",
            lambda *a, **kw: called.append("gh") or 0,
        )

        rc = cctally.cmd_release(self._make_resume_args())
        captured = capsys.readouterr()
        assert rc == 0
        # mirror_done helper short-circuited under SystemExit, phase
        # runners untouched.
        assert called == [], (
            f"unexpected calls on already-done resume w/ missing clone: {called}"
        )
        assert "release v1.0.0 already published" in captured.out
        # Operator-visible diagnostic surfaced on stderr explaining the
        # trust-gh fallback path.
        assert "public clone not discoverable" in captured.err
        assert "trusting gh release existence" in captured.err
