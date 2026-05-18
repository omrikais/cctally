"""Tests for bin/_lib_semver — semver math used by cctally update + release.

Originally lived in tests/test_release_helpers.py; extracted into a public
test file so the semver smoke-coverage survives the release-tooling
privatization. See docs/superpowers/specs/2026-05-18-release-command-split-design.md §4.
"""

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"

_SPEC = importlib.util.spec_from_loader(
    "_lib_semver",
    importlib.machinery.SourceFileLoader("_lib_semver", str(_BIN / "_lib_semver.py")),
)
_lib_semver = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_lib_semver)


class TestParseSemver:
    def test_stable(self):
        assert _lib_semver._release_parse_semver("1.0.0") == (1, 0, 0, None, None)

    def test_prerelease_rc(self):
        assert _lib_semver._release_parse_semver("1.1.0-rc.1") == (1, 1, 0, "rc", 1)

    def test_prerelease_alpha(self):
        assert _lib_semver._release_parse_semver("2.0.0-alpha.5") == (2, 0, 0, "alpha", 5)

    def test_zero_components(self):
        assert _lib_semver._release_parse_semver("0.0.0") == (0, 0, 0, None, None)

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
            _lib_semver._release_parse_semver(bad)


class TestFormatSemver:
    def test_stable(self):
        assert _lib_semver._release_format_semver(1, 0, 0) == "1.0.0"

    def test_prerelease(self):
        assert _lib_semver._release_format_semver(1, 1, 0, "rc", 2) == "1.1.0-rc.2"


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
        assert _lib_semver._release_compute_next_version(current, kind, bump, "rc") == expected

    def test_prerelease_id_override(self):
        assert _lib_semver._release_compute_next_version("1.0.0", "prerelease", "minor", "alpha") == "1.1.0-alpha.1"

    def test_prerelease_required_bump_when_stable(self):
        with pytest.raises(ValueError, match="--bump required"):
            _lib_semver._release_compute_next_version("1.0.0", "prerelease", None, "rc")

    def test_bump_kind_on_prerelease_refuses(self):
        with pytest.raises(ValueError, match="run 'cctally-release finalize'"):
            _lib_semver._release_compute_next_version("1.1.0-rc.1", "patch", None, "rc")

    def test_bump_flag_on_prerelease_refuses(self):
        with pytest.raises(ValueError, match="--bump invalid when current"):
            _lib_semver._release_compute_next_version("1.1.0-rc.1", "prerelease", "minor", "rc")

    def test_finalize_when_stable_refuses(self):
        with pytest.raises(ValueError, match="not a prerelease"):
            _lib_semver._release_compute_next_version("1.0.0", "finalize", None, "rc")
