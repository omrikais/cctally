"""Unit tests for bin/_lib_readme_refresh.py (issue #354).

The kernel is a public, stdlib-only module: CHANGELOG highlight extraction,
lint-safe normalization, marker splicing, and the README copy lint. It is
loaded the same way the sibling fixture-builder tests load bin/ scripts
(importlib.util.spec_from_file_location — the file has no package layout).

Also carries THE STANDING GUARD: the real README.md must pass the copy lint
and carry exactly one well-formed marker pair. That guard is xfail during
Task 1 (the README is still the pre-rewrite copy) and becomes a real
assertion once Task 2 rewrites README.md.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KERNEL = REPO_ROOT / "bin" / "_lib_readme_refresh.py"


def _load_kernel():
    spec = importlib.util.spec_from_file_location("_lib_readme_refresh", KERNEL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_lib_readme_refresh"] = mod
    spec.loader.exec_module(mod)
    return mod


rr = _load_kernel()
extract_highlights = rr.extract_highlights
changelog_release_date = rr.changelog_release_date
normalize_highlight = rr.normalize_highlight
render_latest_stable_block = rr.render_latest_stable_block
splice_marker_block = rr.splice_marker_block
lint_copy = rr.lint_copy
MARKER_BEGIN = rr.MARKER_BEGIN
MARKER_END = rr.MARKER_END
ChangelogSectionMissing = rr.ChangelogSectionMissing
MarkerError = rr.MarkerError


# The real v1.81.0 Added section carries two em dashes in its first bullet —
# a mandatory normalization fixture (spec Section 2 item 2).
V1810_SECTION = """## [1.81.0] - 2026-07-24

### Added
- cctally now tracks usage per account for each provider: if you use more than one Claude or Codex account on this machine, each account's percent, 5-hour, and quota milestones — and their alerts — are recorded and fire independently instead of silently colliding. (#341)
- New `cctally account list|show|label` subcommand shows every observed account (provider, label, email, plan, first/last seen, and which is currently active) and lets you set a durable friendly label that survives a stats rebuild. (#341)

### Changed
- Multi-account is byte-stable: if you use a single account per provider (the common case), every report, alert, and status line is unchanged. Account labels and columns appear only once a provider has more than one real account. (#341)

## [1.80.4] - 2026-07-23

### Fixed
- other version bullet. (#999)
"""


# --------------------------------------------------------------------------
# extract_highlights
# --------------------------------------------------------------------------
def test_extract_exact_version_no_prefix_match():
    cl = (
        "## [1.80.0] - 2026-02-02\n"
        "\n"
        "### Added\n"
        "- eighty bullet.\n"
        "\n"
        "## [1.8] - 2026-01-01\n"
        "\n"
        "### Added\n"
        "- eight bullet.\n"
    )
    assert extract_highlights(cl, "1.8") == ["eight bullet."]
    assert extract_highlights(cl, "1.80.0") == ["eighty bullet."]


def test_extract_priority_and_cap():
    bullets = extract_highlights(V1810_SECTION, "1.81.0")
    assert len(bullets) == 3
    assert bullets[0].startswith("cctally now tracks usage per account")
    assert bullets[1].startswith("New `cctally account list|show|label`")
    # Added exhausted at 2, so the third comes from Changed (priority order).
    assert bullets[2].startswith("Multi-account is byte-stable")


def test_extract_strips_qualified_issue_refs():
    cl = (
        "## [2.0.0] - 2026-03-03\n"
        "\n"
        "### Added\n"
        "- alpha end ref (#341)\n"
        "- beta with (#341) inline still here (#294 S7)\n"
    )
    bullets = extract_highlights(cl, "2.0.0")
    assert bullets[0] == "alpha end ref"
    # trailing "(#294 S7)" stripped; the inline "(#341)" preserved.
    assert bullets[1] == "beta with (#341) inline still here"


def test_extract_flattens_multiline_bullets():
    cl = (
        "## [2.1.0] - 2026-03-04\n"
        "\n"
        "### Added\n"
        "- first line of bullet\n"
        "  continues here\n"
    )
    assert extract_highlights(cl, "2.1.0") == ["first line of bullet continues here"]


def test_extract_skips_fenced_content_and_errors_on_unclosed():
    cl_closed = (
        "## [2.2.0] - 2026-03-05\n"
        "\n"
        "### Added\n"
        "- real alpha\n"
        "```text\n"
        "- fake bullet inside fence\n"
        "```\n"
        "- real beta\n"
    )
    assert extract_highlights(cl_closed, "2.2.0") == ["real alpha", "real beta"]

    cl_unclosed = (
        "## [2.3.0] - 2026-03-06\n"
        "\n"
        "### Added\n"
        "- alpha\n"
        "```text\n"
        "- still in fence, never closed\n"
    )
    with pytest.raises(ValueError):
        extract_highlights(cl_unclosed, "2.3.0")


def test_extract_missing_section_raises():
    cl = "## [9.9.9] - 2026-01-01\n\n### Added\n- x\n"
    with pytest.raises(ChangelogSectionMissing):
        extract_highlights(cl, "1.2.3")


def test_extract_real_v1810_is_lint_clean():
    bullets = extract_highlights(V1810_SECTION, "1.81.0")
    assert len(bullets) == 3
    # em dashes normalized to commas.
    assert ", and their alerts, are recorded" in bullets[0]
    for b in bullets:
        assert lint_copy("- " + b) == [], b


def test_extract_drop_bullet_fallback_and_empty_block():
    cl = (
        "## [3.0.0] - 2026-04-04\n"
        "\n"
        "### Added\n"
        "- has an emoji ⭐ here\n"
    )
    assert extract_highlights(cl, "3.0.0") == []
    assert (
        render_latest_stable_block("3.0.0", "2026-04-04", [])
        == "**Latest stable: v3.0.0** (2026-04-04)\n"
    )


# --------------------------------------------------------------------------
# normalize_highlight / render_latest_stable_block
# --------------------------------------------------------------------------
def test_normalize_em_en_dashes_to_commas():
    assert (
        normalize_highlight("milestones — and their alerts — are recorded")
        == "milestones, and their alerts, are recorded"
    )
    assert normalize_highlight("a – b") == "a, b"


def test_render_block_shape():
    assert (
        render_latest_stable_block("1.81.0", "2026-07-24", ["x"])
        == "**Latest stable: v1.81.0** (2026-07-24)\n\n- x"
    )


# --------------------------------------------------------------------------
# changelog_release_date
# --------------------------------------------------------------------------
def test_changelog_release_date():
    assert changelog_release_date(V1810_SECTION, "1.81.0") == "2026-07-24"
    with pytest.raises(ChangelogSectionMissing):
        changelog_release_date(V1810_SECTION, "1.2.3")


# --------------------------------------------------------------------------
# splice_marker_block
# --------------------------------------------------------------------------
def test_splice_replaces_only_marked_region_bytewise():
    before = "intro line\r\n\r\n"
    after = "\r\n\r\ntrailer line\r\n"
    text = before + MARKER_BEGIN + "\nOLD INTERIOR\n" + MARKER_END + after
    spliced = splice_marker_block(text, "**Latest stable: v1.0.0** (2026-01-01)")
    # Everything outside the marked region is byte-identical (CRLF preserved).
    assert spliced.startswith(before + MARKER_BEGIN)
    assert spliced.endswith(MARKER_END + after)
    # Interior replaced with "\n" + block + "\n".
    interior = spliced[
        spliced.index(MARKER_BEGIN) + len(MARKER_BEGIN):spliced.index(MARKER_END)
    ]
    assert interior == "\n**Latest stable: v1.0.0** (2026-01-01)\n"


def test_splice_fail_closed():
    block = "x"
    good = MARKER_BEGIN + "\nfoo\n" + MARKER_END
    # missing begin
    with pytest.raises(MarkerError):
        splice_marker_block("no markers here", block)
    # missing end
    with pytest.raises(MarkerError):
        splice_marker_block(MARKER_BEGIN + "\nfoo\n", block)
    # duplicated begin
    with pytest.raises(MarkerError):
        splice_marker_block(good + "\n" + MARKER_BEGIN + "\n", block)
    # end before begin
    with pytest.raises(MarkerError):
        splice_marker_block(MARKER_END + "\nfoo\n" + MARKER_BEGIN, block)


# --------------------------------------------------------------------------
# lint_copy
# --------------------------------------------------------------------------
def test_lint_catches_each_class_and_exempts_fences():
    text = "\n".join(
        [
            "line one is clean",                    # 1
            "has em dash — here",              # 2 em-dash
            "has en dash – here",              # 3 en-dash
            "has emoji ⭐ here",               # 4 emoji
            "a spaced - hyphen here",               # 5 spaced-hyphen
            "- leading bullet dash is fine",        # 6 clean (bullet prefix)
            "```",                                   # 7 fence open
            "inside — fence not flagged",      # 8 skipped
            "```",                                   # 9 fence close
            "~~~",                                   # 10 fence open
            "inside ⭐ tilde fence",           # 11 skipped
            "~~~",                                   # 12 fence close
        ]
    )
    issues = lint_copy(text)
    codes = {(ln, code) for ln, code, _ in issues}
    assert (2, "em-dash") in codes
    assert (3, "en-dash") in codes
    assert (4, "emoji") in codes
    assert (5, "spaced-hyphen") in codes
    flagged_lines = {ln for ln, _, _ in issues}
    assert flagged_lines == {2, 3, 4, 5}


def test_lint_unclosed_fence_is_single_issue():
    text = "clean line\n```\nem — inside unclosed fence\n"
    issues = lint_copy(text)
    assert [code for _, code, _ in issues] == ["unclosed-fence"]


def test_lint_clean_text_returns_empty():
    assert lint_copy("all ascii, commas, colons: fine.\n- a bullet line\n") == []


# --------------------------------------------------------------------------
# Live CHANGELOG + standing README guard
# --------------------------------------------------------------------------
def test_live_changelog_v1810_extracts_clean():
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    bullets = extract_highlights(text, "1.81.0")
    assert len(bullets) == 3
    for b in bullets:
        assert lint_copy("- " + b) == [], b


def test_readme_copy_lint_clean_and_markers_present():
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert lint_copy(text) == []
    assert text.count(MARKER_BEGIN) == 1
    assert text.count(MARKER_END) == 1
    assert text.index(MARKER_BEGIN) < text.index(MARKER_END)


def test_committed_block_matches_kernel_render_for_v1810():
    # The committed latest-stable block must equal what the kernel renders from
    # the CHANGELOG for the same version. Valid only while the block still says
    # v1.81.0 (the refresh op rewrites it on the next stable release).
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    interior = readme[
        readme.index(MARKER_BEGIN) + len(MARKER_BEGIN):readme.index(MARKER_END)
    ]
    if "v1.81.0" not in interior:
        pytest.skip("committed latest-stable block is no longer v1.81.0")
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    bullets = extract_highlights(changelog, "1.81.0")
    date = changelog_release_date(changelog, "1.81.0")
    block = render_latest_stable_block("1.81.0", date, bullets)
    assert interior == "\n" + block + "\n"


def test_check_cli(tmp_path):
    marked = (
        "clean intro line\n\n"
        + MARKER_BEGIN + "\n**Latest stable: v1.0.0** (2026-01-01)\n" + MARKER_END
        + "\n\ntrailer line\n"
    )
    clean = tmp_path / "clean.md"
    clean.write_text(marked, encoding="utf-8")
    r0 = subprocess.run(
        [sys.executable, str(KERNEL), "--check", str(clean)],
        capture_output=True, text=True,
    )
    assert r0.returncode == 0, r0.stderr

    dirty = tmp_path / "dirty.md"
    dirty.write_text(marked.replace("clean intro line", "bad — dash"), encoding="utf-8")
    r1 = subprocess.run(
        [sys.executable, str(KERNEL), "--check", str(dirty)],
        capture_output=True, text=True,
    )
    assert r1.returncode == 1
    assert "em-dash" in r1.stderr

    r2 = subprocess.run(
        [sys.executable, str(KERNEL)],
        capture_output=True, text=True,
    )
    assert r2.returncode == 2
