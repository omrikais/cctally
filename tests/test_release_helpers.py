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
