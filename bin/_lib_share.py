"""Pure-function render kernel for shareable reports.

Imported lazily from bin/cctally only when a headliner subcommand is invoked
with --format. Stdlib-only, no I/O, no DB, no filesystem, no locks.

Spec: docs/superpowers/specs/2026-05-08-shareable-reports-design.md
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime


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

Cell = TextCell | MoneyCell | PercentCell | DateCell | DeltaCell | ProjectCell


# --- Table primitives ---

@dataclass(frozen=True)
class ColumnSpec:
    key: str
    label: str
    align: str = "left"   # "left" | "right" | "center"
    emphasis: bool = False


@dataclass(frozen=True)
class Row:
    cells: Mapping[str, "Cell"]


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
    project_label: str | None = None
    series_key: str | None = None


@dataclass(frozen=True)
class LineChart:
    points: tuple[ChartPoint, ...]
    y_label: str
    # Each reference line is (value, label, severity) where severity is "warn"|"alarm".
    # Renderer unpacks the 3-tuple; bare-float form (Implementor 1's tightening) was
    # incorrect — restored to the consumer-driven shape per Implementor Bundle 3.
    reference_lines: tuple[tuple[float, str, str], ...] = ()
    multi_series: Mapping[str, tuple[ChartPoint, ...]] | None = None


@dataclass(frozen=True)
class BarChart:
    points: tuple[ChartPoint, ...]
    y_label: str
    stacks: Mapping[str, tuple[ChartPoint, ...]] | None = None


@dataclass(frozen=True)
class HorizontalBarChart:
    points: tuple[ChartPoint, ...]
    x_label: str
    cap: int | None = None


ChartSpec = LineChart | BarChart | HorizontalBarChart


# --- Top-level snapshot ---
#
# Contract: ShareSnapshot and all nested dataclasses are nominally frozen.
# `frozen=True` blocks attribute rebinding (snap.cmd = ...) but cannot prevent
# mutation of the inner dict held by Row.cells or the inner tuple/dict held by
# chart fields. The scrubber and renderers MUST treat snapshots as read-only;
# the parameterized Mapping/tuple annotations exist to make a typechecker
# reject mutation attempts (e.g., dict assignment, list.append). Phase 4's
# scrubber returns a NEW snapshot rather than rewriting in place — see spec
# §5.3 (anonymization chokepoint) and Codex finding M6.

@dataclass(frozen=True)
class ShareSnapshot:
    cmd: str
    title: str
    subtitle: str | None
    period: PeriodSpec
    columns: tuple[ColumnSpec, ...]
    rows: tuple[Row, ...]
    chart: ChartSpec | None
    totals: tuple[Totalled, ...]
    notes: tuple[str, ...]
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
    HTML-char escaping. Backslash is in _MD_FMT_CHARS so a literal `\\` becomes
    `\\\\` — single-pass dispatch, each char checked independently.
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


# --- SVG primitives ---

def _fmt_num(n: float) -> str:
    """Format float with one decimal place, no scientific notation, no -0.0.

    Byte-stability invariant — every coordinate / value in SVG output
    routes through this so goldens are stable. Rejects non-finite inputs
    (NaN/inf) loudly so chart-layer divide-by-zero or bad-data bugs surface
    at the value site rather than rendering silently as a blank chart.
    """
    if not math.isfinite(n):
        raise ValueError(f"_fmt_num requires finite input, got {n!r}")
    out = f"{float(n):.1f}"
    if out == "-0.0":
        return "0.0"
    return out


def _serialize_attrs(attrs: Mapping[str, object]) -> str:
    """Serialize SVG/HTML attributes in lexical key order with escaped values.

    Numbers go through _fmt_num; strings through _attr_escape. None values
    skipped (lets primitives accept optional attributes uniformly).
    """
    parts = []
    for key in sorted(attrs):
        value = attrs[key]
        if value is None:
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            rendered = _fmt_num(value)
        else:
            rendered = _attr_escape(str(value))
        parts.append(f'{key}="{rendered}"')
    return " ".join(parts)


def svg_rect(x: float, y: float, w: float, h: float, *,
             fill: str, stroke: str | None = None) -> str:
    return f'<rect {_serialize_attrs({"x": x, "y": y, "width": w, "height": h, "fill": fill, "stroke": stroke})}/>'


def svg_text(x: float, y: float, text: str, *,
             font_size: float, fill: str,
             anchor: str = "start", weight: str = "normal") -> str:
    attrs = {
        "x": x,
        "y": y,
        "font-size": font_size,
        "fill": fill,
        "text-anchor": anchor,
    }
    if weight and weight != "normal":
        attrs["font-weight"] = weight
    return f'<text {_serialize_attrs(attrs)}>{_xml_escape(text)}</text>'


def svg_line(x1: float, y1: float, x2: float, y2: float, *,
             stroke: str, width: float = 1) -> str:
    return f'<line {_serialize_attrs({"x1": x1, "y1": y1, "x2": x2, "y2": y2, "stroke": stroke, "stroke-width": width})}/>'


def svg_polyline(points: list[tuple[float, float]], *, stroke: str,
                 width: float = 2, fill: str = "none") -> str:
    pts_str = " ".join(f"{_fmt_num(x)},{_fmt_num(y)}" for x, y in points)
    return f'<polyline {_serialize_attrs({"points": pts_str, "stroke": stroke, "stroke-width": width, "fill": fill})}/>'


def svg_path(d: str, *, stroke: str | None = None,
             fill: str | None = None) -> str:
    """SVG path element — `d` is the only opaque attribute in the kernel.

    Byte-stability caveat: callers building `d` from coordinates MUST format
    each numeric value through `_fmt_num` before stringification, e.g.,
    `f"M{_fmt_num(x0)} {_fmt_num(y0)} L{_fmt_num(x1)} {_fmt_num(y1)}"`. The
    `d` minilanguage is opaque to `_serialize_attrs`, so a stray `f"{x:.6f}"`
    would diverge goldens silently.
    """
    attrs: dict[str, object] = {"d": d}
    if stroke is not None:
        attrs["stroke"] = stroke
    if fill is not None:
        attrs["fill"] = fill
    return f'<path {_serialize_attrs(attrs)}/>'


def svg_group(children: list, *, transform: str | None = None) -> str:
    attrs: dict = {}
    if transform is not None:
        attrs["transform"] = transform
    open_tag = f'<g {_serialize_attrs(attrs)}>' if attrs else "<g>"
    return open_tag + "".join(children) + "</g>"


# --- Chart layout helpers ---

_PADDING_LEFT = 50    # axis labels
_PADDING_BOTTOM = 30  # x-tick labels
_PADDING_TOP = 10
_PADDING_RIGHT = 10


def _chart_inner_box(x, y, width, height):
    """Compute (ix, iy, iw, ih) — the inner plot area inside chart padding."""
    ix = x + _PADDING_LEFT
    iy = y + _PADDING_TOP
    iw = width - _PADDING_LEFT - _PADDING_RIGHT
    ih = height - _PADDING_TOP - _PADDING_BOTTOM
    return ix, iy, iw, ih


def _scale_y(values, ih):
    """Return y_max and a scale function f(value) -> y-pixel (top-down)."""
    if not values:
        return 1.0, lambda v: 0.0
    y_max = max(values)
    y_min = min(0.0, min(values))
    span = y_max - y_min if (y_max - y_min) > 1e-9 else 1.0
    def f(v):
        # Higher value → smaller y (SVG y axis is top-down).
        norm = (v - y_min) / span
        return ih - (norm * ih)
    return y_max, f


# --- Chart renderers ---

def _render_line_chart_svg(chart: LineChart, *, palette: dict,
                           x: float, y: float, width: float, height: float) -> str:
    ix, iy, iw, ih = _chart_inner_box(x, y, width, height)
    pts = chart.points
    if not pts:
        return svg_group([
            svg_text(x + width / 2, y + height / 2, "(no data)",
                     font_size=12, fill=palette["muted"], anchor="middle"),
        ])

    y_values = [p.y_value for p in pts] + [r[0] for r in chart.reference_lines]
    _, scale_y = _scale_y(y_values, ih)

    n = len(pts)
    if n == 1:
        x_step = 0.0
    else:
        x_step = iw / (n - 1)

    # Axes.
    elements = []
    elements.append(svg_line(ix, iy + ih, ix + iw, iy + ih,
                             stroke=palette["axis"], width=1))
    elements.append(svg_line(ix, iy, ix, iy + ih,
                             stroke=palette["axis"], width=1))

    # Reference lines.
    for (ref_value, ref_label, severity) in chart.reference_lines:
        ref_color = palette["ref_warn"] if severity == "warn" else palette["ref_alarm"]
        ry = iy + scale_y(ref_value)
        elements.append(svg_line(ix, ry, ix + iw, ry, stroke=ref_color, width=1))
        elements.append(svg_text(ix + iw - 4, ry - 3, ref_label,
                                 font_size=10, fill=ref_color, anchor="end"))

    # Series polyline (primary series).
    poly_pts = [(ix + i * x_step, iy + scale_y(p.y_value)) for i, p in enumerate(pts)]
    elements.append(svg_polyline(poly_pts, stroke=palette["series_primary"], width=2))

    # Optional multi-series (forecast actual + projected).
    if chart.multi_series:
        for series_key, series_pts in sorted(chart.multi_series.items()):
            series_color = palette["series_secondary"]
            spoly = [(ix + i * x_step, iy + scale_y(p.y_value)) for i, p in enumerate(series_pts)]
            # Dashed for "projected" — simple stroke-dasharray.
            attrs = {
                "points": " ".join(f"{_fmt_num(px)},{_fmt_num(py)}" for px, py in spoly),
                "stroke": series_color,
                "stroke-width": 2,
                "stroke-dasharray": "4 3",
                "fill": "none",
            }
            elements.append(f'<polyline {_serialize_attrs(attrs)}/>')

    # X-tick labels (every point).
    for i, p in enumerate(pts):
        tx = ix + i * x_step
        elements.append(svg_text(tx, iy + ih + 14, p.x_label,
                                 font_size=10, fill=palette["muted"], anchor="middle"))

    # Y-axis label.
    elements.append(svg_text(ix - 10, iy + ih / 2, chart.y_label,
                             font_size=10, fill=palette["muted"], anchor="end"))

    return svg_group(elements)


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
