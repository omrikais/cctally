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


def test_render_block_names_and_links_the_complete_stable_span():
    block = render_latest_stable_block(
        "1.101.0",
        "2026-08-19",
        ["target highlight"],
        previous_stable="1.95.5",
        release_url=(
            "https://github.com/omrikais/cctally/releases/tag/v1.101.0"
        ),
    )
    assert block == (
        "**Latest stable: v1.101.0** (2026-08-19)\n\n"
        "Highlights from the `v1.95.5` to `v1.101.0` stable upgrade:\n\n"
        "- target highlight\n\n"
        "[See every change in this stable upgrade]"
        "(https://github.com/omrikais/cctally/releases/tag/v1.101.0)"
    )
    assert lint_copy(block) == []


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
# The first three bullets of the released v1.81.0 `### Added` subsection, copied
# out of CHANGELOG.md verbatim, and the three strings extract_highlights turns
# them into. Both are written out here rather than recomputed from the file,
# because the version this replaced computed its expected value from the same
# three lines the kernel reads, by nearly the same rule. Both sides therefore
# moved together, and a rewrite that replaced every word of the section left it
# green. A literal is the only form of the pin that cannot do that.
V1810_ADDED_FIRST_THREE = (
    "cctally tracks usage per account for each provider. If you use more than "
    "one Claude or Codex account on this machine, each account's percent, "
    "5-hour and quota milestones — and their alerts — are recorded and fire "
    "independently.",
    "`cctally account list|show|label` shows every observed account with its "
    "provider, label, email, plan, first and last seen, and which is active, "
    "and lets you set a durable friendly label that survives a stats rebuild.",
    "Optional per-account weekly budgets through `config set budget.accounts` "
    "and `config set budget.codex.accounts`. A budget targets an immutable "
    "account even after you rename its label.",
)
V1810_EXPECTED_HIGHLIGHTS = (
    "cctally tracks usage per account for each provider. If you use more than "
    "one Claude or Codex account on this machine, each account's percent, "
    "5-hour and quota milestones, and their alerts, are recorded and fire "
    "independently.",
    V1810_ADDED_FIRST_THREE[1],
    V1810_ADDED_FIRST_THREE[2],
)


def test_live_changelog_v1810_extracts_the_pinned_highlights():
    """Two literals: what the section says, and what the kernel makes of it.

    PINNED. The exact text and order of the first three bullets of the real
    v1.81.0 `### Added` subsection, and the exact text and order of the three
    highlights `extract_highlights` returns for that version. Reword any of
    those bullets, reorder them, delete one, or add an issue reference to one,
    and one or both assertions fail. The two are separate because the kernel
    strips a terminal `(#NNN)` before normalizing, so an issue reference added
    to a pinned bullet is invisible in the extractor's output and is caught only
    by the raw pin. It also pins that all three survive the copy lint, which is
    what the README's block requires of a highlight.

    NOT PINNED. Every other release section, and the README. Whether the
    README's block agrees with the changelog is
    ``test_committed_latest_stable_block_is_internally_consistent_not_current``,
    and that test reads whatever version the README names, which is not this one.

    v1.81.0 is released, so its section is frozen and these literals are not
    expected to need updating. A failure is therefore either an edit to
    published history or a change in how the kernel extracts, and both are
    worth stopping for.
    """
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    section = text.split("\n## [1.81.0]", 1)[1].split("\n## [", 1)[0]
    added = section.split("### Added", 1)[1].split("\n### ", 1)[0]
    raw = tuple(
        line[2:] for line in added.split("\n") if line.startswith("- ")
    )[:3]
    assert raw == V1810_ADDED_FIRST_THREE

    bullets = extract_highlights(text, "1.81.0")
    assert bullets == list(V1810_EXPECTED_HIGHLIGHTS)
    for b in bullets:
        assert lint_copy("- " + b) == [], b


def test_readme_copy_lint_clean_and_markers_present():
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert lint_copy(text) == []
    assert text.count(MARKER_BEGIN) == 1
    assert text.count(MARKER_END) == 1
    assert text.index(MARKER_BEGIN) < text.index(MARKER_END)


class ReadmeBlockMalformed(AssertionError):
    """The committed marker interior does not parse.

    An ``AssertionError`` rather than a bare exception because a malformed
    interior IS the failure. The two tests this replaced each opened with
    ``if "v1.81.0" not in interior: pytest.skip(...)``, so the day the block
    stopped naming that version they stopped checking anything and said nothing.
    """


_HEADER_PREFIX = "**Latest stable: v"
_SPAN_PREFIX = "Highlights from the `v"
_SPAN_MIDDLE = "` to `v"
_SPAN_SUFFIX = "` stable upgrade:"
_LINK_PREFIX = "[See every change in this stable upgrade]("


def _public_repo() -> str:
    """``PUBLIC_REPO`` read out of ``bin/cctally`` without importing the CLI.

    Derived from the tree so it cannot drift from the constant the release path
    builds the same URL from, and read by AST so this leaf test does not take on
    the whole CLI's import surface for one string.
    """
    import ast

    tree = ast.parse((REPO_ROOT / "bin" / "cctally").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "PUBLIC_REPO":
                return ast.literal_eval(node.value)
    raise ReadmeBlockMalformed("bin/cctally declares no PUBLIC_REPO")


def parse_latest_stable_interior(interior: str):
    """(version, date, previous_stable, release_url), or RAISE.

    There is no "nothing to check" return. Every component it extracts is
    required to agree with the version in the header, so a block whose header,
    span sentence and release link disagree is a failure rather than a partial
    match.
    """
    if not interior.startswith("\n") or not interior.endswith("\n"):
        raise ReadmeBlockMalformed(
            "the marker interior must begin and end with a newline"
        )
    lines = interior[1:-1].split("\n")
    headers = [line for line in lines if line.startswith(_HEADER_PREFIX)]
    if len(headers) != 1:
        raise ReadmeBlockMalformed(
            f"expected exactly one latest-stable header line, found {len(headers)}"
        )
    if lines[0] != headers[0]:
        raise ReadmeBlockMalformed(
            "the latest-stable header must be the first line of the block"
        )
    version, sep, tail = headers[0][len(_HEADER_PREFIX):].partition("** (")
    if not sep or not tail.endswith(")") or not version or len(tail) < 2:
        raise ReadmeBlockMalformed(f"unparseable header line: {headers[0]!r}")
    date = tail[:-1]

    spans = [line for line in lines if line.startswith(_SPAN_PREFIX)]
    if len(spans) > 1:
        raise ReadmeBlockMalformed(f"expected at most one span sentence, found {len(spans)}")
    previous_stable = None
    if spans:
        previous_stable, sep, tail = spans[0][len(_SPAN_PREFIX):].partition(_SPAN_MIDDLE)
        if not sep or not tail.endswith(_SPAN_SUFFIX) or not previous_stable:
            raise ReadmeBlockMalformed(f"unparseable span sentence: {spans[0]!r}")
        span_target = tail[: -len(_SPAN_SUFFIX)]
        if span_target != version:
            raise ReadmeBlockMalformed(
                f"the span sentence names v{span_target} but the header names v{version}"
            )

    links = [line for line in lines if line.startswith(_LINK_PREFIX)]
    if len(links) > 1:
        raise ReadmeBlockMalformed(f"expected at most one release link, found {len(links)}")
    release_url = None
    if links:
        if not links[0].endswith(")"):
            raise ReadmeBlockMalformed(f"unparseable release link: {links[0]!r}")
        release_url = links[0][len(_LINK_PREFIX):-1]
        if not release_url.endswith(f"/releases/tag/v{version}"):
            raise ReadmeBlockMalformed(
                f"the release link points at {release_url}, which does not name v{version}"
            )

    if (previous_stable is None) != (release_url is None):
        raise ReadmeBlockMalformed(
            "a stable-span block carries both the span sentence and the release "
            "link, or neither"
        )
    return version, date, previous_stable, release_url


def test_committed_latest_stable_block_is_internally_consistent_not_current():
    """The committed block equals what the kernel renders for the version IT names.

    This verifies CONSISTENCY, not CURRENCY, and the distinction is the point.
    The target version is read out of the README, so a release that fails to
    update the README leaves this test extracting the stale version, finding its
    retained CHANGELOG section, rendering it and passing. Nothing in the tree
    carries an independent offline notion of channel state — the release path
    takes it from the immutable tag and the full GitHub release inventory — so
    currency belongs to the release path and this test is named for what it
    actually holds.

    What it does hold is unskippable. It replaces two tests that each hard-coded
    a version and skipped when the block no longer named it: #354 wrote the
    v1.81.0 one, it died silently on 2026-07-25, and the response was a second
    pinned test for v1.101.0 rather than a repaired guard. A malformed interior
    fails here; there is no `pytest.skip` in this test at any version.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    interior = readme[
        readme.index(MARKER_BEGIN) + len(MARKER_BEGIN):readme.index(MARKER_END)
    ]
    version, _date, previous_stable, _url = parse_latest_stable_interior(interior)

    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    bullets = extract_highlights(changelog, version)
    date = changelog_release_date(changelog, version)
    if previous_stable is None:
        block = render_latest_stable_block(version, date, bullets)
    else:
        block = render_latest_stable_block(
            version,
            date,
            bullets,
            previous_stable=previous_stable,
            release_url=(
                f"https://github.com/{_public_repo()}/releases/tag/v{version}"
            ),
        )
    assert interior == "\n" + block + "\n"


@pytest.mark.parametrize(
    "corruption,expected",
    [
        ("no header", "exactly one latest-stable header line"),
        ("span disagrees", "span sentence names"),
        ("link disagrees", "does not name"),
        ("two headers", "found 2"),
        ("half a span", "both the span sentence"),
    ],
)
def test_a_malformed_interior_fails_rather_than_reporting_nothing_to_check(
    corruption, expected
):
    """The parse must never answer "no version found, nothing to check".

    That answer is what the two version-pinned predecessors gave, in the form of
    a skip, and it is why a dead guard stayed green for a month. Each case below
    is a real corruption of the committed shape.
    """
    good = (
        "\n**Latest stable: v9.9.9** (2026-01-01)\n"
        "\nHighlights from the `v9.9.8` to `v9.9.9` stable upgrade:\n"
        "\n- a highlight\n"
        "\n[See every change in this stable upgrade]"
        "(https://github.com/omrikais/cctally/releases/tag/v9.9.9)\n"
    )
    # The good shape must parse, or every case below would pass vacuously.
    assert parse_latest_stable_interior(good) == (
        "9.9.9", "2026-01-01", "9.9.8",
        "https://github.com/omrikais/cctally/releases/tag/v9.9.9",
    )
    broken = {
        "no header": good.replace("**Latest stable: v9.9.9**", "Latest stable 9.9.9"),
        "span disagrees": good.replace("to `v9.9.9` stable", "to `v9.9.7` stable"),
        "link disagrees": good.replace("releases/tag/v9.9.9", "releases/tag/v9.9.7"),
        "two headers": good + "**Latest stable: v9.9.8** (2025-01-01)\n",
        "half a span": good.replace(
            "\n[See every change in this stable upgrade]"
            "(https://github.com/omrikais/cctally/releases/tag/v9.9.9)\n",
            "\n",
        ),
    }[corruption]
    with pytest.raises(ReadmeBlockMalformed) as exc:
        parse_latest_stable_interior(broken)
    assert expected in str(exc.value), str(exc.value)


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
