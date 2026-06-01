"""Shared fmt / color / table render primitives (pure-fn kernel).

Holds the low-level formatting primitives extracted from bin/cctally
(#126, C11): timestamp/week-window compaction, color/unicode capability
detection, ANSI styling, display-width-aware boxed tables, number
formatting + width-budget truncation. Pure: the only environment reads
(os.environ, sys.stdout.isatty(), sys.stdout.encoding) happen INSIDE the
functions at call time, never at import.

Imported honestly by _lib_render.py and _lib_diff_kernel.py (via their
_load_lib helpers); re-exported on the bin/cctally namespace for the
_cctally_* command siblings and _lib_view_models.py (c.X accessor) and the
test monkeypatch surfaces.

Spec: docs/superpowers/specs/2026-06-01-extract-fmt-color-table-primitives-design.md
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import re
import sys
import unicodedata
from typing import Any

# parse_iso_datetime: bare import matches the established _lib_* convention
# (_lib_aggregators.py:86, _lib_diff_kernel.py:123 do the same).
from _cctally_core import parse_iso_datetime


# format_display_dt: loaded via the file-path _load_lib helper, NOT a bare
# `from _lib_display_tz import …`. The repo's sibling loading deliberately
# bypasses sys.path (test/loader contexts may lack bin/ on the path); every
# _lib_* consumer of _lib_display_tz uses _load_lib (_lib_render.py:100,
# _lib_diff_kernel.py:114, _lib_aggregators.py:75). _lib_fmt matches them.
def _load_lib(name: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    import importlib.util as _ilu
    p = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    spec = _ilu.spec_from_file_location(name, p)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_lib_display_tz = _load_lib("_lib_display_tz")
format_display_dt = _lib_display_tz.format_display_dt


def _parse_iso_datetime_optional(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_iso_datetime(value, "timestamp")
    except ValueError:
        return None


def _format_ts_compact(
    value: str | None,
    tz: "ZoneInfo | None" = None,
) -> str:
    """Compact ISO-instant -> "YYYY-MM-DD HH:MM" line.

    F5 fix: optional ``tz`` localizes the parsed instant before strftime
    AND appends the offset suffix via ``display_tz_label`` so the column
    becomes unambiguous (mirrors ``format_display_dt``'s pattern). When
    ``tz`` is None, returns the legacy UTC-clock string with no suffix
    so existing callers keep their byte-stable output.
    """
    parsed = _parse_iso_datetime_optional(value)
    if parsed is None:
        return "n/a"
    if tz is None:
        # Host-local fallback / default-config path: preserve the original
        # byte-stable host-naive strftime output (UTC-aware datetimes render
        # UTC clock, no suffix). This branch is reachable in production for
        # users whose ``display.tz`` resolves to None — NOT a legacy or
        # back-compat path. The non-None branch routes through
        # ``format_display_dt`` for tz-aware rendering.
        return parsed.strftime("%Y-%m-%d %H:%M")
    return format_display_dt(parsed, tz, fmt="%Y-%m-%d %H:%M", suffix=True)


def _format_week_window(
    week_start_date: str | None,
    week_end_date: str | None,
    week_start_at: str | None,
    week_end_at: str | None,
    tz: "ZoneInfo | None" = None,
) -> str:
    """Render a "<start> -> <end>" week-window column. F5 adds tz-aware
    rendering for ISO-timestamp-bearing rows; legacy date-only rows pass
    through unchanged. ``tz=None`` preserves byte-stable callers."""
    if week_start_at and week_end_at:
        return (
            f"{_format_ts_compact(week_start_at, tz=tz)} -> "
            f"{_format_ts_compact(week_end_at, tz=tz)}"
        )
    return f"{week_start_date or 'n/a'} -> {week_end_date or 'n/a'}"


def _supports_color_stdout() -> bool:
    # Matches ccusage's picocolors behavior exactly.
    # FORCE_COLOR always enables (any value, including empty)
    if "FORCE_COLOR" in os.environ:
        return True
    # NO_COLOR always disables (any value, including empty)
    if "NO_COLOR" in os.environ:
        return False
    # CI environments get color
    if "CI" in os.environ:
        return True
    # TTY check on stdout or stderr
    if sys.stdout.isatty() or sys.stderr.isatty():
        term = os.environ.get("TERM", "")
        return term.lower() != "dumb"
    return False


def _style_ansi(text: str, code: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"\033[{code}m{text}\033[0m"


def _supports_unicode_stdout() -> bool:
    encoding = (sys.stdout.encoding or "").upper()
    return "UTF" in encoding


def _display_width(s: str) -> int:
    """Terminal cells consumed by ``s``.

    Counts each codepoint by its East Asian Width: ``W`` / ``F`` (Wide
    / Fullwidth) → 2 cells; combining marks → 0; everything else → 1.
    Ambiguous (``A``) defaults to 1, matching every non-CJK terminal
    locale — cctally has no CJK content in cell data, and `→` / `—` /
    `·` (all `A`) are intentionally rendered narrow.

    Used by `_boxed_table` so cells containing wide glyphs (notably
    `⚡` U+26A1 on credit-row annotations) pad to the right cell count
    rather than the right codepoint count. Without this, `len()`-based
    padding under-pads by one cell per wide glyph and the right border
    drifts off-column on those rows only.
    """
    width = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _boxed_table(
    headers: list[str],
    rows: list[list[str]],
    aligns: list[str] | None = None,
    *,
    color_header: bool = True,
    compact: bool = False,
) -> str:
    if not headers:
        return ""
    col_count = len(headers)
    aligns = aligns or ["left"] * col_count
    if len(aligns) != col_count:
        raise ValueError("aligns length must match headers length")

    sanitized_rows: list[list[str]] = []
    for row in rows:
        normalized = [str(cell).replace("\n", " ") for cell in row]
        if len(normalized) != col_count:
            raise ValueError("row length must match headers length")
        sanitized_rows.append(normalized)

    widths: list[int] = []
    for idx, header in enumerate(headers):
        max_cell = max((_display_width(r[idx]) for r in sanitized_rows), default=0)
        widths.append(max(_display_width(header), max_cell))

    def _pad(text: str, width: int, align: str) -> str:
        deficit = width - _display_width(text)
        if deficit <= 0:
            return text
        pad = " " * deficit
        if align == "right":
            return pad + text
        if align == "center":
            left = deficit // 2
            return (" " * left) + text + (" " * (deficit - left))
        return text + pad

    if _supports_unicode_stdout():
        chars = {
            "top_left": "┌",
            "top_mid": "┬",
            "top_right": "┐",
            "mid_left": "├",
            "mid_mid": "┼",
            "mid_right": "┤",
            "bottom_left": "└",
            "bottom_mid": "┴",
            "bottom_right": "┘",
            "h": "─",
            "v": "│",
        }
    else:
        chars = {
            "top_left": "+",
            "top_mid": "+",
            "top_right": "+",
            "mid_left": "+",
            "mid_mid": "+",
            "mid_right": "+",
            "bottom_left": "+",
            "bottom_mid": "+",
            "bottom_right": "+",
            "h": "-",
            "v": "|",
        }

    color_enabled = _supports_color_stdout()

    def _dim(s: str) -> str:
        return _style_ansi(s, "90", color_enabled)

    # Issue #91 (Shape B): ``compact`` drops the 1-space cell padding to
    # 0 on this content-sized table (which has no proportional-width path
    # to force). Borders and rows both key off ``pad`` so the default
    # (``pad == 1``) reproduces the prior output byte-for-byte.
    pad = 0 if compact else 1
    pad_s = " " * pad

    def make_border(left: str, mid: str, right: str) -> str:
        return _dim(
            left
            + mid.join(chars["h"] * (w + 2 * pad) for w in widths)
            + right
        )

    def make_row(cells: list[str], *, header: bool = False) -> str:
        is_total = not header and cells and cells[0].strip() == "Total"
        styled_cells: list[str] = []
        for i, raw in enumerate(cells):
            text = _pad(raw, widths[i], aligns[i])
            if header and color_header:
                text = _style_ansi(text, "36", color_enabled)  # cyan text, like ccusage table head
            elif is_total:
                text = _style_ansi(text, "32", color_enabled)  # green text for totals
            styled_cells.append(text)
        v = _dim(chars["v"])
        return (
            v
            + pad_s
            + f"{pad_s}{v}{pad_s}".join(styled_cells)
            + pad_s
            + v
        )

    top = make_border(chars["top_left"], chars["top_mid"], chars["top_right"])
    mid = make_border(chars["mid_left"], chars["mid_mid"], chars["mid_right"])
    bottom = make_border(chars["bottom_left"], chars["bottom_mid"], chars["bottom_right"])

    out_lines = [top, make_row(headers, header=True), mid]
    for idx, row in enumerate(sanitized_rows):
        out_lines.append(make_row(row, header=False))
        if idx < len(sanitized_rows) - 1:
            out_lines.append(mid)
    out_lines.append(bottom)
    return "\n".join(out_lines)


def _fmt_num(n: int) -> str:
    """Format integer with comma separators: 1234567 -> '1,234,567'."""
    return f"{n:,}"


def _truncate_num(formatted: str, width: int) -> str:
    """Truncate a formatted number to fit width, replacing tail with '…'."""
    if len(formatted) <= width:
        return formatted
    return formatted[: width - 1] + "\u2026"


_ANSI_ESC_RE = re.compile(r"\033\[[0-9;]*m")


def _truncate_display(text: str, width: int) -> str:
    """Truncate to `width` visible chars, preserving ANSI escape sequences.

    Unlike `_truncate_num`, which slices raw string indices, this walks
    the text treating `\\033[...m` sequences as zero-width and counts
    printable chars toward the width budget. Used for left-aligned
    cells that may carry a styled anomaly-glyph prefix — slicing those
    with `_truncate_num` can cut through an ANSI escape and bleed
    color into adjacent cells.
    """
    # Fast path: no ANSI codes, fall back to raw-slice truncation.
    if "\033" not in text:
        return _truncate_num(text, width)
    stripped_len = len(_ANSI_ESC_RE.sub("", text))
    if stripped_len <= width:
        return text
    # Walk chars until we've emitted (width - 1) visible chars, copying
    # ANSI sequences verbatim. Append reset + ellipsis to close any open
    # style and preserve the fit-to-width contract.
    out: list[str] = []
    visible = 0
    i = 0
    target = width - 1
    while i < len(text) and visible < target:
        m = _ANSI_ESC_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        out.append(text[i])
        visible += 1
        i += 1
    return "".join(out) + "\033[0m\u2026"
