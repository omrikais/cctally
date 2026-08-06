"""Canonical visible-text projection and matching for Codex conversation find.

The module is deliberately stdlib-only.  Its Markdown scanner models the
visible text-node boundaries used by the dashboard's ReactMarkdown/remark-gfm
surface; it is not an HTML renderer and never interprets raw HTML.
"""
from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import Iterable, Iterator, Sequence


CODEX_FIND_PROJECTION_VERSION = 2


@dataclass(frozen=True)
class RenderLeaf:
    key: str
    text: str


@dataclass(frozen=True)
class ProjectedLeaf:
    key: str
    start: int
    end: int


@dataclass(frozen=True)
class FindRange:
    start: int
    end: int


@dataclass(frozen=True)
class LeafFragment:
    leaf_key: str
    start: int
    end: int


class _ProjectionBuilder:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.leaves: list[dict[str, int | str]] = []
        self.length = 0
        self._open_leaf: int | None = None

    def boundary(self) -> None:
        self._open_leaf = None

    def separator(self, value: str) -> None:
        if not value:
            return
        self.boundary()
        self.parts.append(value)
        self.length += len(value)

    def emit(self, value: str, *, boundary: bool = False, key: str | None = None) -> None:
        if not value:
            return
        if boundary:
            self.boundary()
        start = self.length
        self.parts.append(value)
        self.length += len(value)
        if key is not None:
            self.leaves.append({"key": key, "start": start, "end": self.length})
            self._open_leaf = None
            return
        if self._open_leaf is None:
            self._open_leaf = len(self.leaves)
            self.leaves.append({
                "key": f"t{self._open_leaf}",
                "start": start,
                "end": self.length,
            })
        else:
            self.leaves[self._open_leaf]["end"] = self.length

    def value(self) -> tuple[str, tuple[ProjectedLeaf, ...]]:
        return (
            "".join(self.parts),
            tuple(ProjectedLeaf(**leaf) for leaf in self.leaves),
        )


_TABLE_DELIMITER_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_BLOCK_PREFIX_RE = re.compile(
    r"^\s*(?:(?:#{1,6})\s+|>\s?|(?:[-+*]|\d+[.)])\s+)"
)
_TASK_MARKER_RE = re.compile(r"^\[[ xX]\]\s+")
_AUTOLINK_RE = re.compile(r"<((?:https?://|mailto:)[^ <>]+|[^ <>@]+@[^ <>@]+)>")


def _table_cells(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


def _find_closing(source: str, token: str, start: int) -> int:
    cursor = start
    while True:
        found = source.find(token, cursor)
        if found < 0:
            return -1
        backslashes = 0
        probe = found - 1
        while probe >= 0 and source[probe] == "\\":
            backslashes += 1
            probe -= 1
        if backslashes % 2 == 0:
            return found
        cursor = found + len(token)


def _project_inline(source: str, builder: _ProjectionBuilder) -> None:
    plain: list[str] = []

    def flush() -> None:
        if plain:
            builder.emit(html.unescape("".join(plain)))
            plain.clear()

    cursor = 0
    while cursor < len(source):
        if source[cursor] == "\\" and cursor + 1 < len(source):
            plain.append(source[cursor + 1])
            cursor += 2
            continue

        if source[cursor] == "`":
            run = 1
            while cursor + run < len(source) and source[cursor + run] == "`":
                run += 1
            token = "`" * run
            close = _find_closing(source, token, cursor + run)
            if close >= 0:
                flush()
                builder.emit(source[cursor + run:close].strip(" "), boundary=True)
                builder.boundary()
                cursor = close + run
                continue

        if source.startswith("![", cursor) or source[cursor] == "[":
            image = source.startswith("![", cursor)
            label_start = cursor + (2 if image else 1)
            label_end = source.find("](", label_start)
            if label_end >= 0:
                destination_end = source.find(")", label_end + 2)
                if destination_end >= 0:
                    flush()
                    builder.boundary()
                    if not image:
                        _project_inline(source[label_start:label_end], builder)
                    builder.boundary()
                    cursor = destination_end + 1
                    continue

        if source[cursor] == "<":
            autolink = _AUTOLINK_RE.match(source, cursor)
            if autolink is not None:
                flush()
                builder.boundary()
                label = autolink.group(1)
                builder.emit(label[7:] if label.startswith("mailto:") else label)
                builder.boundary()
                cursor = autolink.end()
                continue

        matched_delimiter = False
        for token in ("**", "__", "~~", "*", "_"):
            if not source.startswith(token, cursor):
                continue
            close = _find_closing(source, token, cursor + len(token))
            if close < 0 or close == cursor + len(token):
                continue
            flush()
            builder.boundary()
            _project_inline(source[cursor + len(token):close], builder)
            builder.boundary()
            cursor = close + len(token)
            matched_delimiter = True
            break
        if matched_delimiter:
            continue

        plain.append(source[cursor])
        cursor += 1
    flush()


def _strip_block_prefix(line: str) -> str:
    value = _BLOCK_PREFIX_RE.sub("", line, count=1)
    if _TASK_MARKER_RE.match(value):
        value = _TASK_MARKER_RE.sub(" ", value, count=1)
    if value.endswith("  "):
        value = value[:-2]
    elif value.endswith("\\"):
        value = value[:-1]
    return value


def project_markdown(source: str) -> tuple[str, tuple[ProjectedLeaf, ...]]:
    builder = _ProjectionBuilder()
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[tuple[str, object]] = []
    cursor = 0
    while cursor < len(lines):
        line = lines[cursor]
        if not line.strip():
            cursor += 1
            continue

        fence = re.match(r"^\s*(`{3,}|~{3,})(?:[^`]*)$", line)
        if fence:
            token = fence.group(1)
            body: list[str] = []
            cursor += 1
            while cursor < len(lines) and not re.match(
                rf"^\s*{re.escape(token[0])}{{{len(token)},}}\s*$", lines[cursor]
            ):
                body.append(lines[cursor])
                cursor += 1
            closed = cursor < len(lines)
            if closed:
                cursor += 1
            code = "\n".join(body)
            if body:
                code += "\n"
            blocks.append(("code", code))
            continue

        if cursor + 1 < len(lines) and "|" in line and _TABLE_DELIMITER_RE.match(lines[cursor + 1]):
            rows = [_table_cells(line)]
            cursor += 2
            while cursor < len(lines) and lines[cursor].strip() and "|" in lines[cursor]:
                rows.append(_table_cells(lines[cursor]))
                cursor += 1
            blocks.append(("table", rows))
            continue

        paragraph = [_strip_block_prefix(line)]
        cursor += 1
        while cursor < len(lines) and lines[cursor].strip():
            if re.match(r"^\s*(`{3,}|~{3,})", lines[cursor]):
                break
            paragraph.append(_strip_block_prefix(lines[cursor]))
            cursor += 1
        blocks.append(("paragraph", paragraph))

    for block_index, (kind, value) in enumerate(blocks):
        if block_index:
            builder.separator("\n")
        if kind == "code":
            builder.emit(str(value), boundary=True)
            builder.boundary()
        elif kind == "table":
            for row_index, row in enumerate(value):
                if row_index:
                    builder.separator("\n")
                for cell_index, cell in enumerate(row):
                    if cell_index:
                        builder.separator("\t")
                    builder.boundary()
                    _project_inline(cell, builder)
                    builder.boundary()
        else:
            for line_index, line in enumerate(value):
                if line_index:
                    builder.separator("\n")
                _project_inline(line, builder)
    return builder.value()


def project_plain(leaves: Sequence[RenderLeaf]) -> tuple[str, tuple[ProjectedLeaf, ...]]:
    builder = _ProjectionBuilder()
    for leaf in leaves:
        builder.emit(leaf.text, key=leaf.key)
    return builder.value()


_CONTEXT_DIFF_GIT_RE = re.compile(r"diff --git a/\S+ b/\S+")
_CONTEXT_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_CONTEXT_EXTENDED_HEADER_PREFIXES = (
    "old mode ", "new mode ", "new file mode ", "deleted file mode ",
    "rename from ", "rename to ", "copy from ", "copy to ",
    "similarity index ", "dissimilarity index ", "index ",
)


def _context_is_diff_line(line: str) -> bool:
    if _CONTEXT_DIFF_GIT_RE.search(line):
        return True
    if line.startswith(("--- ", "+++ ", "@@")):
        return True
    if line.startswith(_CONTEXT_EXTENDED_HEADER_PREFIXES):
        return True
    return line == "" or line[0] in "+- \\"


def _segment_context_body(text: str) -> list[tuple[str, str]]:
    """Mirror ``contextDiff.ts::segmentContextBody`` without rendering HTML."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    segments: list[tuple[str, str]] = []
    prose: list[str] = []
    diff: list[str] = []
    in_diff = False

    def flush(kind: str, values: list[str]) -> None:
        if values:
            segments.append((kind, "\n".join(values)))
            values.clear()

    for line in lines:
        if not in_diff:
            match = _CONTEXT_DIFF_GIT_RE.search(line)
            if match is None:
                prose.append(line)
                continue
            before = line[:match.start()].rstrip()
            if before:
                prose.append(before)
            flush("prose", prose)
            in_diff = True
            diff.append(line[match.start():])
        elif _context_is_diff_line(line):
            diff.append(line)
        else:
            flush("diff", diff)
            in_diff = False
            prose.append(line)
    flush("prose", prose)
    flush("diff", diff)
    return segments


def _context_diff_rows(text: str) -> list[tuple[int, int, int, str]]:
    """Mirror the visible row walk in ``contextDiff.ts::parseUnifiedDiff``."""
    rows: list[tuple[int, int, int, str]] = []
    file_index = -1
    hunk_index = -1
    row_index = 0
    in_hunk = False
    for line in text.split("\n"):
        if _CONTEXT_DIFF_GIT_RE.search(line):
            file_index += 1
            hunk_index = -1
            row_index = 0
            in_hunk = False
            continue
        if _CONTEXT_HUNK_RE.match(line):
            hunk_index += 1
            row_index = 0
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith(("--- ", "+++ ")) or line.startswith(
            _CONTEXT_EXTENDED_HEADER_PREFIXES
        ):
            continue
        if line == "" or line.startswith("\\"):
            continue
        rows.append((file_index, hunk_index, row_index, line[1:]))
        row_index += 1
    return rows


def _append_projected(
    builder: _ProjectionBuilder,
    projected: tuple[str, tuple[ProjectedLeaf, ...]],
    *,
    prefix: str,
) -> None:
    text, leaves = projected
    if not text:
        return
    start = builder.length
    builder.parts.append(text)
    builder.length += len(text)
    builder.boundary()
    builder.leaves.extend({
        "key": f"{prefix}/{leaf.key}",
        "start": start + leaf.start,
        "end": start + leaf.end,
    } for leaf in leaves)


def project_context(source: str) -> tuple[str, tuple[ProjectedLeaf, ...]]:
    """Project visible prose and diff-row leaves from one context body.

    File headers and +/- statistics are derived card chrome, matching #482's
    rule that only provider-authored render leaves enter the search surface.
    """
    builder = _ProjectionBuilder()
    for segment_index, (kind, text) in enumerate(_segment_context_body(source)):
        if kind == "prose":
            projected = project_markdown(text)
            if not projected[0]:
                continue
            if builder.parts:
                builder.separator("\n")
            _append_projected(
                builder, projected, prefix=f"segments.{segment_index}.prose"
            )
            continue
        for file_index, hunk_index, row_index, row_text in _context_diff_rows(text):
            if not row_text:
                continue
            if builder.parts:
                builder.separator("\n")
            builder.emit(
                row_text,
                key=(
                    f"segments.{segment_index}.files.{file_index}."
                    f"hunks.{hunk_index}.rows.{row_index}"
                ),
            )
    return builder.value()


def _single_scalar_lower(value: str) -> str:
    return "".join((lowered if len(lowered := scalar.lower()) == 1 else scalar) for scalar in value)


def iter_literal_ranges(
    text: str, query: str, *, case_sensitive: bool,
) -> Iterator[FindRange]:
    if not query:
        return
    haystack = text if case_sensitive else _single_scalar_lower(text)
    needle = query if case_sensitive else _single_scalar_lower(query)
    if not needle:
        return
    cursor = 0
    while cursor <= len(haystack) - len(needle):
        found = haystack.find(needle, cursor)
        if found < 0:
            break
        yield FindRange(found, found + len(needle))
        cursor = found + len(needle)


def literal_ranges(text: str, query: str, *, case_sensitive: bool) -> tuple[FindRange, ...]:
    return tuple(iter_literal_ranges(text, query, case_sensitive=case_sensitive))


def iter_regex_ranges(text: str, pattern: re.Pattern[str]) -> Iterator[FindRange]:
    for match in pattern.finditer(text):
        if match.end() > match.start():
            yield FindRange(match.start(), match.end())


def regex_ranges(text: str, pattern: re.Pattern[str]) -> tuple[FindRange, ...]:
    return tuple(iter_regex_ranges(text, pattern))


def slice_range_to_leaves(
    match: FindRange,
    leaves: Sequence[ProjectedLeaf],
) -> tuple[LeafFragment, ...]:
    fragments: list[LeafFragment] = []
    for leaf in leaves:
        start = max(match.start, leaf.start)
        end = min(match.end, leaf.end)
        if end <= start:
            continue
        fragments.append(LeafFragment(
            leaf_key=leaf.key,
            start=start - leaf.start,
            end=end - leaf.start,
        ))
    return tuple(fragments)


__all__ = [
    "CODEX_FIND_PROJECTION_VERSION",
    "FindRange",
    "LeafFragment",
    "ProjectedLeaf",
    "RenderLeaf",
    "literal_ranges",
    "iter_literal_ranges",
    "iter_regex_ranges",
    "project_markdown",
    "project_context",
    "project_plain",
    "regex_ranges",
    "slice_range_to_leaves",
]
