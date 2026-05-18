"""Tests for bin/_lib_changelog._read_latest_changelog_version.

The helper is a public sibling of ``bin/cctally`` that reads the latest
stamped ``## [X.Y.Z] - YYYY-MM-DD`` header from CHANGELOG.md. It reaches
``CHANGELOG_PATH`` and ``RELEASE_HEADER_RE`` through the call-time
``_cctally()`` accessor (project memory ``_cctally() accessor pattern``),
so tests use ``load_script()`` to bind cctally + the sibling in the
canonical way and monkeypatch ``CHANGELOG_PATH`` on the loaded
namespace.
"""

import sys

import pytest

from conftest import load_script


@pytest.fixture()
def ns_and_lib(tmp_path, monkeypatch):
    """Load cctally fresh (which side-effects in _lib_changelog) and
    expose both the cctally globals dict and the loaded sibling. The
    monkeypatch fixture's setitem() on the namespace dict is observed
    by the sibling's call-time _cctally() accessor."""
    ns = load_script()
    lib = sys.modules["_lib_changelog"]
    return ns, lib, monkeypatch


def test_reads_latest_stamped_section(tmp_path, ns_and_lib):
    ns, lib, monkeypatch = ns_and_lib
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(
        "# Changelog\n\n"
        "## [Unreleased]\n\n"
        "### Added\n- thing\n\n"
        "## [1.8.2] - 2026-05-17\n\n"
        "### Fixed\n- bug\n\n"
        "## [1.8.1] - 2026-05-10\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(ns, "CHANGELOG_PATH", cl)
    assert lib._read_latest_changelog_version() == ("1.8.2", "2026-05-17")


def test_skips_unreleased_header(tmp_path, ns_and_lib):
    ns, lib, monkeypatch = ns_and_lib
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(
        "## [Unreleased]\n\n## [1.0.0] - 2026-01-01\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(ns, "CHANGELOG_PATH", cl)
    assert lib._read_latest_changelog_version() == ("1.0.0", "2026-01-01")


def test_returns_none_when_no_stamped_section(tmp_path, ns_and_lib):
    ns, lib, monkeypatch = ns_and_lib
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## [Unreleased]\n\n### Added\n- thing\n", encoding="utf-8")
    monkeypatch.setitem(ns, "CHANGELOG_PATH", cl)
    assert lib._read_latest_changelog_version() is None


def test_returns_none_when_file_missing(tmp_path, ns_and_lib):
    ns, lib, monkeypatch = ns_and_lib
    monkeypatch.setitem(ns, "CHANGELOG_PATH", tmp_path / "does-not-exist.md")
    assert lib._read_latest_changelog_version() is None


def test_prerelease_version(tmp_path, ns_and_lib):
    ns, lib, monkeypatch = ns_and_lib
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## [1.9.0-rc.1] - 2026-05-18\n", encoding="utf-8")
    monkeypatch.setitem(ns, "CHANGELOG_PATH", cl)
    assert lib._read_latest_changelog_version() == ("1.9.0-rc.1", "2026-05-18")
