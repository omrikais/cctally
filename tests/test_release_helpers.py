"""Tests for bin/cctally release-automation helpers (issue #24)."""
import importlib.machinery
import importlib.util
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
