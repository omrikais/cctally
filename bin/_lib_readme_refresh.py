#!/usr/bin/env python3
"""Pure kernel for the public README auto-refresh (issue #354).

Four stdlib-only responsibilities, each a pure function so the private
release driver, a public pytest, and a `--check` CLI can all share one
implementation:

1. ``extract_highlights`` — pull up to N lint-clean highlight bullets from a
   specific ``## [X.Y.Z] - YYYY-MM-DD`` CHANGELOG section (exact version match,
   Added > Changed > Fixed priority, multiline flattening, terminal issue-ref
   stripping, fenced-code skipping).
2. ``normalize_highlight`` — deterministically rewrite a bullet so it satisfies
   the copy lint by construction (em/en dashes with surrounding spaces become
   ``, ``; doubled separators collapse; leading/trailing separators trim).
3. ``splice_marker_block`` — replace only the region between the HTML-comment
   markers, byte-preserving everything outside (CRLF included), failing closed
   on missing/duplicated/malformed markers.
4. ``lint_copy`` — scan README prose (outside fenced code) for em dash, en
   dash, a defined emoji set, and mid-line spaced-hyphen dash substitutes.

This module is PUBLIC (listed in `.mirror-allowlist` + `package.json` files[]),
because its pytest lands on the public mirror and a public test must not import
a private module. It contains nothing sensitive — Markdown parsing and linting.

CLI: ``python3 bin/_lib_readme_refresh.py --check README.md`` runs the copy lint
plus marker well-formedness and exits 0 (clean) / 1 (issues) / 2 (usage).
"""
from __future__ import annotations

import re
import sys

MARKER_BEGIN = "<!-- cctally:latest-stable:begin -->"
MARKER_END = "<!-- cctally:latest-stable:end -->"


class ChangelogSectionMissing(ValueError):
    """Raised when the requested version has no exact CHANGELOG section."""


class MarkerError(ValueError):
    """Raised when the latest-stable markers are missing/duplicated/malformed."""


# --------------------------------------------------------------------------
# Fence handling (shared by extraction + lint)
# --------------------------------------------------------------------------
# GFM code fences: a line whose content (after up to 3 leading spaces) begins
# with a run of >= 3 backticks or >= 3 tildes. The closing fence must use the
# same character, be at least as long as the opener, and (unlike the opener)
# carry no info string.
_FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})([^\n]*)$")


class _UnclosedFence(Exception):
    def __init__(self, line_no: int, excerpt: str) -> None:
        super().__init__(f"unclosed fence opened on line {line_no}")
        self.line_no = line_no
        self.excerpt = excerpt


def _fence_marker(line: str):
    """Return ``(char, length, info)`` if *line* is a fence marker, else None."""
    m = _FENCE_RE.match(line)
    if not m:
        return None
    run = m.group(1)
    return (run[0], len(run), m.group(2).strip())


def _iter_content_lines(text: str):
    """Yield ``(line_no, line)`` for lines OUTSIDE fenced code blocks.

    Raises ``_UnclosedFence`` (at the end of the walk) if a fence is opened but
    never closed. Operates on ``str.split("\\n")`` so a CRLF file keeps its
    trailing ``\\r`` on each line — harmless for the char scans below.
    """
    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_open_line = 0
    fence_open_text = ""
    for idx, line in enumerate(text.split("\n"), start=1):
        marker = _fence_marker(line)
        if in_fence:
            if (
                marker is not None
                and marker[0] == fence_char
                and marker[1] >= fence_len
                and marker[2] == ""
            ):
                in_fence = False
            continue
        if marker is not None:
            in_fence = True
            fence_char, fence_len = marker[0], marker[1]
            fence_open_line = idx
            fence_open_text = line.strip()
            continue
        yield idx, line
    if in_fence:
        raise _UnclosedFence(fence_open_line, fence_open_text)


# --------------------------------------------------------------------------
# Copy lint
# --------------------------------------------------------------------------
_EM_DASH = "—"
_EN_DASH = "–"
_BULLET_PREFIX_RE = re.compile(r"^\s*[-*] ")


def _has_emoji(s: str) -> bool:
    for ch in s:
        cp = ord(ch)
        if (
            0x1F000 <= cp <= 0x1FBFF
            or 0x2600 <= cp <= 0x27BF
            or 0x2B00 <= cp <= 0x2BFF
            or cp == 0xFE0F
            or cp == 0x200D
        ):
            return True
    return False


def lint_copy(readme_text: str) -> "list[tuple[int, str, str]]":
    """Return copy-lint issues as ``(1-based line_no, code, excerpt)`` tuples.

    Empty list means clean. Codes: ``em-dash``, ``en-dash``, ``emoji``,
    ``spaced-hyphen``. Scans only outside fenced code blocks; an unclosed fence
    collapses to a single ``unclosed-fence`` issue.
    """
    try:
        content = list(_iter_content_lines(readme_text))
    except _UnclosedFence as uf:
        return [(uf.line_no, "unclosed-fence", uf.excerpt)]

    issues: "list[tuple[int, str, str]]" = []
    for line_no, line in content:
        excerpt = line.strip()
        if _EM_DASH in line:
            issues.append((line_no, "em-dash", excerpt))
        if _EN_DASH in line:
            issues.append((line_no, "en-dash", excerpt))
        if _has_emoji(line):
            issues.append((line_no, "emoji", excerpt))
        # Line-leading "- "/"* " bullets are fine; only a REMAINING mid-line
        # " - " is a dash substitute.
        remainder = _BULLET_PREFIX_RE.sub("", line, count=1)
        if " - " in remainder:
            issues.append((line_no, "spaced-hyphen", excerpt))
    return issues


# --------------------------------------------------------------------------
# Highlight normalization
# --------------------------------------------------------------------------
def normalize_highlight(bullet: str) -> str:
    """Rewrite a bullet so it satisfies the copy lint deterministically."""
    b = re.sub(r"\s*[—–]\s*", ", ", bullet)
    b = re.sub(r"(, )+", ", ", b)
    b = b.strip()
    b = b.strip(", ")
    return b.strip()


# --------------------------------------------------------------------------
# CHANGELOG extraction
# --------------------------------------------------------------------------
_ISSUE_REF_RE = re.compile(r"\s*\(#\d+[^)]*\)\s*$")
_SUBSECTIONS = (("### Added", "added"), ("### Changed", "changed"), ("### Fixed", "fixed"))
_SUBSECTION_KEYS = {name: key for name, key in _SUBSECTIONS}
_BULLET_RE = re.compile(r"^[-*] ")


def _section_heading_re(version: str) -> "re.Pattern[str]":
    return re.compile(
        r"^## \[" + re.escape(version) + r"\] - (\d{4}-\d{2}-\d{2})\s*$"
    )


def _section_body_lines(changelog_text: str, version: str) -> "list[str]":
    lines = changelog_text.split("\n")
    heading_re = _section_heading_re(version)
    start = None
    for i, line in enumerate(lines):
        if heading_re.match(line):
            start = i
            break
    if start is None:
        raise ChangelogSectionMissing(version)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return lines[start + 1:end]


def changelog_release_date(changelog_text: str, version: str) -> str:
    """Return the ``YYYY-MM-DD`` release date for *version*'s section."""
    heading_re = _section_heading_re(version)
    for line in changelog_text.split("\n"):
        m = heading_re.match(line)
        if m:
            return m.group(1)
    raise ChangelogSectionMissing(version)


def _collect_raw_bullets(body_lines: "list[str]") -> "dict[str, list[str]]":
    """Group raw (un-normalized) bullets by subsection, flattening wraps.

    Raises ``ValueError`` if a fence is opened inside the section and never
    closed before the section ends.
    """
    buckets: "dict[str, list[str]]" = {"added": [], "changed": [], "fixed": []}
    subsection: "str | None" = None
    current: "str | None" = None
    in_fence = False
    fence_char = ""
    fence_len = 0

    def finalize() -> None:
        nonlocal current
        if current is not None and subsection in buckets:
            buckets[subsection].append(current.rstrip())
        current = None

    for line in body_lines:
        marker = _fence_marker(line)
        if in_fence:
            if (
                marker is not None
                and marker[0] == fence_char
                and marker[1] >= fence_len
                and marker[2] == ""
            ):
                in_fence = False
            continue
        if marker is not None:
            finalize()
            in_fence = True
            fence_char, fence_len = marker[0], marker[1]
            continue
        stripped = line.rstrip()
        if stripped.startswith("## "):
            finalize()
            subsection = None
            continue
        if stripped.startswith("### "):
            finalize()
            subsection = _SUBSECTION_KEYS.get(stripped)
            continue
        if _BULLET_RE.match(line):
            finalize()
            current = line[2:].strip() if subsection in buckets else None
            continue
        if not stripped:
            finalize()
            continue
        # Continuation of the current bullet.
        if current is not None:
            current = current + " " + line.strip()
    if in_fence:
        raise ValueError("unclosed code fence inside CHANGELOG section")
    finalize()
    return buckets


def extract_highlights(
    changelog_text: str, version: str, *, limit: int = 3
) -> "list[str]":
    """Return up to ``limit`` normalized, lint-clean highlight bullets.

    Exact heading match (never prefix), Added > Changed > Fixed priority,
    multiline flattening, one terminal issue-ref stripped, then normalized.
    A bullet that still fails the copy lint after normalization is dropped and
    the next candidate is taken. Raises ``ChangelogSectionMissing`` if the
    version section is absent.
    """
    body = _section_body_lines(changelog_text, version)
    buckets = _collect_raw_bullets(body)
    candidates = buckets["added"] + buckets["changed"] + buckets["fixed"]
    kept: "list[str]" = []
    for raw in candidates:
        if len(kept) >= limit:
            break
        stripped = _ISSUE_REF_RE.sub("", raw)
        normalized = normalize_highlight(stripped)
        if not normalized:
            continue
        if lint_copy("- " + normalized):
            continue  # drop-bullet fallback
        kept.append(normalized)
    return kept


def render_latest_stable_block(
    version: str, date: str, bullets: "list[str]"
) -> str:
    """Render the auto-maintained latest-stable block (no trailing newline
    after the last bullet; version+date only when *bullets* is empty)."""
    base = f"**Latest stable: v{version}** ({date})\n"
    if bullets:
        return base + "\n" + "\n".join(f"- {b}" for b in bullets)
    return base


# --------------------------------------------------------------------------
# Marker splice
# --------------------------------------------------------------------------
def _marker_alone_on_line(text: str, idx: int, marker: str) -> bool:
    before = text[:idx]
    if before and not before.endswith("\n"):
        return False
    tail_start = idx + len(marker)
    nl = text.find("\n", tail_start)
    tail = text[tail_start:] if nl == -1 else text[tail_start:nl]
    return tail.strip() == ""


def _find_marker_pair(text: str) -> "tuple[int, int]":
    if text.count(MARKER_BEGIN) != 1:
        raise MarkerError(
            f"expected exactly one {MARKER_BEGIN!r}, found "
            f"{text.count(MARKER_BEGIN)}"
        )
    if text.count(MARKER_END) != 1:
        raise MarkerError(
            f"expected exactly one {MARKER_END!r}, found {text.count(MARKER_END)}"
        )
    begin_idx = text.index(MARKER_BEGIN)
    end_idx = text.index(MARKER_END)
    if begin_idx >= end_idx:
        raise MarkerError("begin marker does not precede end marker")
    if not _marker_alone_on_line(text, begin_idx, MARKER_BEGIN):
        raise MarkerError("begin marker is not alone on its line")
    if not _marker_alone_on_line(text, end_idx, MARKER_END):
        raise MarkerError("end marker is not alone on its line")
    return begin_idx, end_idx


def splice_marker_block(readme_text: str, block: str) -> str:
    """Replace the interior between the markers with ``\\n`` + block + ``\\n``.

    Every byte outside the region is preserved (CRLF included). Fails closed on
    missing, duplicated, or malformed markers.
    """
    begin_idx, end_idx = _find_marker_pair(readme_text)
    region_start = begin_idx + len(MARKER_BEGIN)
    return (
        readme_text[:region_start]
        + "\n"
        + block
        + "\n"
        + readme_text[end_idx:]
    )


# --------------------------------------------------------------------------
# CLI (--check)
# --------------------------------------------------------------------------
def _marker_wellformed_issues(text: str) -> "list[tuple[int, str, str]]":
    try:
        _find_marker_pair(text)
    except MarkerError as exc:
        line_no = 1
        if MARKER_BEGIN in text:
            line_no = text[: text.index(MARKER_BEGIN)].count("\n") + 1
        return [(line_no, "markers", str(exc))]
    return []


def _check(path: str) -> int:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    issues = lint_copy(text) + _marker_wellformed_issues(text)
    for line_no, code, excerpt in issues:
        print(f"{path}:{line_no}: {code}: {excerpt}", file=sys.stderr)
    return 1 if issues else 0


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Copy-lint + marker well-formedness check for README.md.",
    )
    parser.add_argument(
        "--check",
        metavar="FILE",
        required=True,
        help="Markdown file to lint (copy rules + latest-stable markers).",
    )
    args = parser.parse_args(argv)
    return _check(args.check)


if __name__ == "__main__":
    sys.exit(main())
