"""Pure-function render kernel for shareable reports.

Imported lazily from bin/cctally only when a headliner subcommand is invoked
with --format. Stdlib-only, no I/O, no DB, no filesystem, no locks.

Spec: docs/superpowers/specs/2026-05-08-shareable-reports-design.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Union


# --- Cell tagged union ---

@dataclass(frozen=True)
class TextCell:
    text: str

@dataclass(frozen=True)
class MoneyCell:
    usd: float

@dataclass(frozen=True)
class PercentCell:
    pct: float

@dataclass(frozen=True)
class DateCell:
    when: datetime

@dataclass(frozen=True)
class DeltaCell:
    value: float
    unit: str  # "%" | "$"

@dataclass(frozen=True)
class ProjectCell:
    """Anonymization chokepoint — scrubber rewrites the `label` field."""
    label: str

# Pre-3.10 fallback: typing.Union; mypy treats this identically to `|`.
Cell = Union[TextCell, MoneyCell, PercentCell, DateCell, DeltaCell, ProjectCell]


# --- Table primitives ---

@dataclass(frozen=True)
class ColumnSpec:
    key: str
    label: str
    align: str = "left"   # "left" | "right" | "center"
    emphasis: bool = False


@dataclass(frozen=True)
class Row:
    cells: dict


@dataclass(frozen=True)
class Totalled:
    label: str
    value: str


@dataclass(frozen=True)
class PeriodSpec:
    start: datetime
    end: datetime
    display_tz: str
    label: str


# --- Chart primitives ---

@dataclass(frozen=True)
class ChartPoint:
    x_label: str
    x_value: float
    y_value: float
    project_label: Optional[str] = None
    series_key: Optional[str] = None


@dataclass(frozen=True)
class LineChart:
    points: tuple
    y_label: str
    reference_lines: tuple = ()
    multi_series: Optional[dict] = None


@dataclass(frozen=True)
class BarChart:
    points: tuple
    y_label: str
    stacks: Optional[dict] = None


@dataclass(frozen=True)
class HorizontalBarChart:
    points: tuple
    x_label: str
    cap: Optional[int] = None


ChartSpec = Union[LineChart, BarChart, HorizontalBarChart]


# --- Top-level snapshot ---

@dataclass(frozen=True)
class ShareSnapshot:
    cmd: str
    title: str
    subtitle: Optional[str]
    period: PeriodSpec
    columns: tuple
    rows: tuple
    chart: Optional[ChartSpec]
    totals: tuple
    notes: tuple
    generated_at: datetime
    version: str


# --- Escape helpers ---

_XML_ESCAPE_TABLE = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
}


def _xml_escape(s: str) -> str:
    """Escape `&`, `<`, `>`, `"`, `'`. For SVG <text> content and HTML body text."""
    out = []
    for ch in s:
        out.append(_XML_ESCAPE_TABLE.get(ch, ch))
    return "".join(out)


def _attr_escape(s: str) -> str:
    """Escape XML chars + collapse newlines to space. For SVG/HTML attribute values."""
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return _xml_escape(s)


_MD_HTML_TABLE = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_MD_FMT_CHARS = ("\\", "|", "*", "_", "`", "[", "]")


def _md_escape(s: str) -> str:
    """Escape markdown formatting chars + HTML chars.

    Markdown surfaces (GitHub, Slack, most renderers) interpret raw HTML inline,
    so a revealed project name like 'Project<script>' would inject without
    HTML-char escaping. Backslash is escaped first so subsequent escapes don't
    double-escape.
    """
    out = []
    for ch in s:
        if ch in _MD_HTML_TABLE:
            out.append(_MD_HTML_TABLE[ch])
        elif ch in _MD_FMT_CHARS:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# --- Palettes ---

PALETTE_LIGHT = {
    "bg": "#ffffff",
    "fg": "#1a1a1a",
    "muted": "#6b7280",
    "grid": "#e5e7eb",
    "axis": "#9ca3af",
    "series_primary": "#2563eb",     # blue-600
    "series_secondary": "#9333ea",   # purple-600
    "ref_warn": "#d97706",           # amber-600
    "ref_alarm": "#dc2626",          # red-600
    "table_header_bg": "#f3f4f6",
    "table_row_alt": "#f9fafb",
    "footer_link": "#2563eb",
}

PALETTE_DARK = {
    "bg": "#0b0f17",
    "fg": "#e5e7eb",
    "muted": "#9ca3af",
    "grid": "#1f2937",
    "axis": "#4b5563",
    "series_primary": "#60a5fa",     # blue-400
    "series_secondary": "#c084fc",   # purple-400
    "ref_warn": "#fbbf24",           # amber-400
    "ref_alarm": "#f87171",          # red-400
    "table_header_bg": "#111827",
    "table_row_alt": "#1f2937",
    "footer_link": "#60a5fa",
}


# --- Format renderers (stubs; full impl in later tasks) ---

def _render_md(snap: ShareSnapshot, *, branding: bool) -> str:
    # Minimal stub — Task 22 fills this out with table + chrome.
    lines = [f"# {_md_escape(snap.title)}"]
    return "\n".join(lines) + "\n"


def _render_svg(snap: ShareSnapshot, *, palette: dict, branding: bool, include_chrome: bool = True) -> str:
    # Minimal stub — Task 11 fills this out with chart + chrome.
    return f'<svg xmlns="http://www.w3.org/2000/svg"><!-- {_xml_escape(snap.title)} --></svg>'


def _render_html(snap: ShareSnapshot, *, palette: dict, branding: bool) -> str:
    # Minimal stub — Task 12 fills this out with HTML chrome around chart-only SVG.
    return f"<!DOCTYPE html><html><body><h1>{_xml_escape(snap.title)}</h1></body></html>"


# --- Public dispatch ---

def render(snap: ShareSnapshot, *, format: str, theme: str, branding: bool) -> str:
    """Render a snapshot to the requested format.

    Pure function: no I/O, no DB, no filesystem, no locks. Caller is
    responsible for emitting the result (stdout/file/clipboard/open).
    """
    if format == "md":
        return _render_md(snap, branding=branding)

    if theme == "light":
        palette = PALETTE_LIGHT
    elif theme == "dark":
        palette = PALETTE_DARK
    else:
        raise ValueError(f"unknown theme: {theme!r}")

    if format == "svg":
        return _render_svg(snap, palette=palette, branding=branding, include_chrome=True)
    if format == "html":
        return _render_html(snap, palette=palette, branding=branding)

    raise ValueError(f"unknown format: {format!r}")
