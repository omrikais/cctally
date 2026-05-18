"""Regression: ``_boxed_table`` must align rows whose cells contain a
double-width Unicode glyph (e.g. ``⚡`` U+26A1, east_asian_width=W).

Pre-fix, the renderer computed column widths via ``len()`` and padded
via ``str.ljust`` / ``str.rjust`` — both count codepoints, not terminal
cells. A ``⚡``-prefixed cell consumes one codepoint but two cells, so
the right border on credit-annotated rows sat one cell beyond the
column's "correct" position, visibly breaking the table frame on
``cctally five-hour-blocks`` and the ``⚡ CREDIT`` divider row on
``cctally five-hour-breakdown``.

These tests pin uniform terminal-cell width across every rendered
line so the regression cannot return.
"""

import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import load_script  # noqa: E402

ANSI = re.compile(r"\033\[[0-9;]*m")


def _display_width(s: str) -> int:
    """Terminal cells consumed by ``s``: Wide/Fullwidth → 2, combining → 0, else → 1."""
    width = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def _table_widths(out: str) -> list[int]:
    return [_display_width(line) for line in _strip_ansi(out).splitlines()]


def test_boxed_table_aligns_wide_glyph_in_first_column():
    """``cctally five-hour-blocks`` credit-row case — ``⚡`` prefix on Block Start."""
    ns = load_script()
    _boxed_table = ns["_boxed_table"]

    headers = ["Block Start", "Status"]
    rows = [
        ["2026-05-17 07:50 IDT", "ACTIVE"],
        ["⚡ 2026-05-15 20:50 IDT", "closed"],
    ]
    out = _boxed_table(headers, rows, ["left", "left"], color_header=False)
    widths = _table_widths(out)
    assert len(set(widths)) == 1, (
        "_boxed_table rendered uneven terminal-cell widths across lines.\n"
        + "\n".join(
            f"  width={w:3d}  {line!r}"
            for w, line in zip(widths, _strip_ansi(out).splitlines())
        )
    )


def test_boxed_table_aligns_wide_glyph_in_inner_column():
    """``cctally five-hour-breakdown`` ⚡ CREDIT divider — Threshold (mid) column."""
    ns = load_script()
    _boxed_table = ns["_boxed_table"]

    headers = ["#", "Threshold", "Cumulative Cost", "Marginal Cost", "7d at crossing"]
    rows = [
        ["1", "10%", "$1.00", "$1.00", "10%"],
        ["2", "⚡ CREDIT", "-20pp", "", "@ 10:30"],
        ["3", "20%", "$2.00", "$1.00", "20%"],
    ]
    out = _boxed_table(headers, rows, ["right", "left", "right", "right", "right"], color_header=False)
    widths = _table_widths(out)
    assert len(set(widths)) == 1, (
        "_boxed_table rendered uneven terminal-cell widths across lines.\n"
        + "\n".join(
            f"  width={w:3d}  {line!r}"
            for w, line in zip(widths, _strip_ansi(out).splitlines())
        )
    )


def test_boxed_table_unchanged_when_no_wide_glyphs():
    """Pure-ASCII tables must render byte-identically — the fix is a no-op
    on the existing common case."""
    ns = load_script()
    _boxed_table = ns["_boxed_table"]

    headers = ["Date", "Amount"]
    rows = [
        ["2026-05-17", "$10.00"],
        ["2026-05-18", "$20.00"],
    ]
    out = _boxed_table(headers, rows, ["left", "right"], color_header=False)
    widths = _table_widths(out)
    assert len(set(widths)) == 1
    # Sanity: no Unicode-Wide chars in the rendered output.
    for ch in _strip_ansi(out):
        assert unicodedata.east_asian_width(ch) not in ("W", "F"), (
            f"unexpected wide glyph {ch!r} (U+{ord(ch):04X}) in ASCII-only table render"
        )
