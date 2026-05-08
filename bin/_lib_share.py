"""Pure-function render kernel for shareable reports.

Imported lazily from bin/cctally only when a headliner subcommand is invoked
with --format. Stdlib-only, no I/O, no DB, no filesystem, no locks.

Spec: docs/superpowers/specs/2026-05-08-shareable-reports-design.md
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
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
    """Horizontal bar chart with top-N cap.

    Contract: each point's `y_value` is treated as a non-negative magnitude.
    Negative `y_value` would produce visually-misleading negative-width
    rendering (silently zero in most SVG renderers); kernel-internal
    callers must pre-filter or coerce.
    """
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

_HBAR_LABEL_GUTTER = 120.0   # left gutter for project labels (anonymized fits; long revealed labels may overflow)
_HBAR_RIGHT_PAD = 10.0       # right-side breathing room for value labels


def _chart_inner_box(
    x: float, y: float, width: float, height: float,
) -> tuple[float, float, float, float]:
    """Compute (ix, iy, iw, ih) — the inner plot area inside chart padding."""
    ix = x + _PADDING_LEFT
    iy = y + _PADDING_TOP
    iw = width - _PADDING_LEFT - _PADDING_RIGHT
    ih = height - _PADDING_TOP - _PADDING_BOTTOM
    return ix, iy, iw, ih


def _scale_y(
    values: list[float], ih: float,
) -> tuple[float, Callable[[float], float]]:
    """Return y_max and a scale function f(value) -> y-pixel (top-down)."""
    if not values:
        return 1.0, lambda v: 0.0
    y_max = max(values)
    y_min = min(0.0, min(values))
    span = y_max - y_min if (y_max - y_min) > 1e-9 else 1.0
    def f(v: float) -> float:
        # Higher value → smaller y (SVG y axis is top-down).
        norm = (v - y_min) / span
        return ih - (norm * ih)
    return y_max, f


def _render_chart_no_data(palette: Mapping[str, str], *,
                          x: float, y: float, width: float, height: float) -> str:
    """Render the canonical '(no data)' placeholder for an empty chart."""
    return svg_group([
        svg_text(x + width / 2, y + height / 2, "(no data)",
                 font_size=12, fill=palette["muted"], anchor="middle"),
    ])


# --- Chart renderers ---

# Line chart.
def _render_line_chart_svg(chart: LineChart, *, palette: dict,
                           x: float, y: float, width: float, height: float) -> str:
    ix, iy, iw, ih = _chart_inner_box(x, y, width, height)
    pts = chart.points
    if not pts:
        return _render_chart_no_data(palette, x=x, y=y, width=width, height=height)

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


# Bar chart (vertical).
def _render_bar_chart_svg(chart: BarChart, *, palette: dict,
                          x: float, y: float, width: float, height: float) -> str:
    ix, iy, iw, ih = _chart_inner_box(x, y, width, height)
    pts = chart.points
    if not pts:
        return _render_chart_no_data(palette, x=x, y=y, width=width, height=height)

    n = len(pts)
    bar_gap = 4.0
    total_gap = bar_gap * (n - 1) if n > 1 else 0.0
    bar_w = max(2.0, (iw - total_gap) / n)

    y_values = [p.y_value for p in pts]
    _, scale_y = _scale_y(y_values, ih)

    elements = []
    elements.append(svg_line(ix, iy + ih, ix + iw, iy + ih,
                             stroke=palette["axis"], width=1))
    elements.append(svg_line(ix, iy, ix, iy + ih,
                             stroke=palette["axis"], width=1))

    for i, p in enumerate(pts):
        bx = ix + i * (bar_w + bar_gap)
        by = iy + scale_y(p.y_value)
        bh = (iy + ih) - by
        elements.append(svg_rect(bx, by, bar_w, bh, fill=palette["series_primary"]))
        # X-tick label centered under bar.
        tx = bx + bar_w / 2
        elements.append(svg_text(tx, iy + ih + 14, p.x_label,
                                 font_size=10, fill=palette["muted"], anchor="middle"))

    elements.append(svg_text(ix - 10, iy + ih / 2, chart.y_label,
                             font_size=10, fill=palette["muted"], anchor="end"))

    return svg_group(elements)


# Horizontal bar chart (top-N with cap).
def _render_hbar_chart_svg(chart: HorizontalBarChart, *, palette: dict,
                           x: float, y: float, width: float, height: float) -> str:
    pts = chart.points
    if chart.cap is not None:
        pts = pts[:chart.cap]
    if not pts:
        return _render_chart_no_data(palette, x=x, y=y, width=width, height=height)

    label_w = _HBAR_LABEL_GUTTER
    ix = x + label_w
    iy = y + 6
    iw = width - label_w - _HBAR_RIGHT_PAD
    ih = height - 12

    n = len(pts)
    row_h = ih / n
    bar_h = max(8.0, row_h * 0.7)
    bar_gap = (row_h - bar_h) / 2

    x_max = max(p.y_value for p in pts)
    if x_max <= 0:
        x_max = 1.0

    elements = []
    for i, p in enumerate(pts):
        ry = iy + i * row_h + bar_gap
        bw = (p.y_value / x_max) * iw
        elements.append(svg_rect(ix, ry, bw, bar_h, fill=palette["series_primary"]))
        # Label gutter (right-aligned to ix - 4).
        elements.append(svg_text(ix - 4, ry + bar_h / 2 + 3, p.x_label,
                                 font_size=11, fill=palette["fg"], anchor="end"))
        # Value label at end of bar.
        elements.append(svg_text(ix + bw + 4, ry + bar_h / 2 + 3,
                                 f"${p.y_value:,.2f}",
                                 font_size=10, fill=palette["muted"], anchor="start"))

    return svg_group(elements)


# --- SVG chrome helpers ---

def _format_generated_at_iso(dt: datetime) -> str:
    """ISO 8601, no microseconds. UTC datetimes use trailing 'Z' instead of '+00:00';
    non-UTC datetimes keep their offset-suffix form (no Z substitution applies)."""
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _version_label(version: str) -> str:
    """v<X.Y.Z> when version is set; 'dev' otherwise (Section 6.9 fallback)."""
    return f"v{version}" if version else "dev"


def _render_svg_header(snap: ShareSnapshot, *, palette: dict,
                       x: float, y: float, width: float) -> str:
    elements = []
    elements.append(svg_text(x, y + 18, snap.title,
                             font_size=18, fill=palette["fg"], weight="bold"))
    if snap.subtitle:
        elements.append(svg_text(x, y + 36, snap.subtitle,
                                 font_size=12, fill=palette["muted"]))
    elements.append(svg_text(x + width, y + 18,
                             _format_generated_at_iso(snap.generated_at),
                             font_size=10, fill=palette["muted"], anchor="end"))
    return svg_group(elements)


def _render_svg_footer(snap: ShareSnapshot, *, palette: dict,
                       x: float, y: float, width: float, branding: bool) -> str:
    if not branding:
        return ""
    label = (
        "Generated by cctally · github.com/omrikais/cctally · "
        + _version_label(snap.version)
    )
    return svg_group([
        svg_text(x, y, label, font_size=10, fill=palette["footer_link"]),
    ])


# --- Scrubber ---
#
# Anonymization chokepoint (spec Section 5.3 / 7 / 8.4). Operates on a
# ShareSnapshot before any renderer runs; returns a new snapshot with project
# labels rewritten everywhere they appear in the rendered output (ProjectCell
# in rows, ChartPoint.project_label / .x_label in chart points + multi-series
# + stacks). The Section 8.4 invariant — anonymized output contains zero
# original tokens across md/svg/html — is the canary; if any new project-
# label site is introduced in the data model later, both `_collect_project_
# costs` (gather) and `_apply_anon_mapping` (rewrite) must be extended.


def _collect_project_costs(snap: ShareSnapshot) -> dict[str, float]:
    """Walk rows: for each row containing a ProjectCell, sum MoneyCell values
    in the same row under the project label.

    Charts also contribute via ChartPoint.project_label + y_value (when y_value
    is in $). For consistency we union both sources; rows take precedence on
    duplicates."""
    costs: dict[str, float] = {}
    for row in snap.rows:
        proj_label: str | None = None
        money = 0.0
        for cell in row.cells.values():
            if isinstance(cell, ProjectCell):
                proj_label = cell.label
            elif isinstance(cell, MoneyCell):
                money = cell.usd
        if proj_label is not None:
            costs[proj_label] = costs.get(proj_label, 0.0) + money

    if snap.chart is not None:
        chart_pts: list[ChartPoint] = []
        if isinstance(snap.chart, (LineChart, BarChart)):
            chart_pts = list(snap.chart.points)
            # Multi-series / stacks: union additional points.
            extras = (
                getattr(snap.chart, "multi_series", None)
                or getattr(snap.chart, "stacks", None)
            )
            if extras:
                for series in extras.values():
                    chart_pts.extend(series)
        elif isinstance(snap.chart, HorizontalBarChart):
            chart_pts = list(snap.chart.points)
        for p in chart_pts:
            if p.project_label and p.project_label not in costs:
                costs[p.project_label] = p.y_value

    return costs


def _build_anon_mapping(project_costs: dict[str, float]) -> dict[str, str]:
    """Sort labels by descending cost (lex tie-break); assign project-1, project-2, ...

    "(unknown)" is never numbered — keeps its literal label.
    """
    items = [
        (label, cost)
        for label, cost in project_costs.items()
        if label != "(unknown)"
    ]
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    mapping: dict[str, str] = {
        label: f"project-{i + 1}" for i, (label, _cost) in enumerate(items)
    }
    if "(unknown)" in project_costs:
        mapping["(unknown)"] = "(unknown)"
    return mapping


# --- Format renderers ---

def _render_md(snap: ShareSnapshot, *, branding: bool) -> str:
    parts = [f"# {_md_escape(snap.title)}"]
    if snap.subtitle:
        parts.append(f"_{_md_escape(snap.subtitle)}_")
        parts.append(f"_{_format_generated_at_iso(snap.generated_at)}_")
    parts.append("")  # blank line before table
    parts.append(_render_md_table(snap))

    if snap.totals:
        parts.append("")
        for t in snap.totals:
            parts.append(f"- **{_md_escape(t.label)}:** {_md_escape(t.value)}")

    if snap.notes:
        parts.append("")
        for n in snap.notes:
            parts.append(f"> {_md_escape(n)}")

    if branding:
        parts.append("")
        parts.append(
            f"_Generated by [cctally](https://github.com/omrikais/cctally) · "
            f"{_version_label(snap.version)} · "
            f"{_format_generated_at_iso(snap.generated_at)}_"
        )

    return "\n".join(parts) + "\n"


# --- SVG composition ---

_SVG_WIDTH = 600
_SVG_HEADER_H = 60
_SVG_CHART_H = 220
_SVG_FOOTER_H = 30
_SVG_PADDING = 20
# Composition-level offset from the footer band's top edge to the text baseline.
# (Inside _render_svg_header, the raw `y + 18` / `y + 36` literals are font-metric
# baseline offsets for the 18pt title and 12pt subtitle — they live at the chrome
# helper site, not at the composition site.)
_SVG_FOOTER_BASELINE = 18


def _render_svg(snap: ShareSnapshot, *, palette: dict,
                branding: bool, include_chrome: bool = True) -> str:
    """Render snapshot to SVG.

    include_chrome=True → standalone SVG with title/subtitle/timestamp/footer.
    include_chrome=False → chart-only (HTML wrapper consumes this).
    """
    if include_chrome:
        height = _SVG_HEADER_H + _SVG_CHART_H + _SVG_FOOTER_H + (_SVG_PADDING * 2)
    else:
        height = _SVG_CHART_H + (_SVG_PADDING * 2)

    pieces = []

    if include_chrome:
        pieces.append(_render_svg_header(
            snap, palette=palette,
            x=_SVG_PADDING, y=_SVG_PADDING, width=_SVG_WIDTH,
        ))

    # Chart.
    chart_y = _SVG_PADDING + (_SVG_HEADER_H if include_chrome else 0)
    if snap.chart is not None:
        if isinstance(snap.chart, LineChart):
            pieces.append(_render_line_chart_svg(
                snap.chart, palette=palette,
                x=_SVG_PADDING, y=chart_y, width=_SVG_WIDTH, height=_SVG_CHART_H,
            ))
        elif isinstance(snap.chart, BarChart):
            pieces.append(_render_bar_chart_svg(
                snap.chart, palette=palette,
                x=_SVG_PADDING, y=chart_y, width=_SVG_WIDTH, height=_SVG_CHART_H,
            ))
        elif isinstance(snap.chart, HorizontalBarChart):
            pieces.append(_render_hbar_chart_svg(
                snap.chart, palette=palette,
                x=_SVG_PADDING, y=chart_y, width=_SVG_WIDTH, height=_SVG_CHART_H,
            ))

    if include_chrome:
        pieces.append(_render_svg_footer(
            snap, palette=palette,
            x=_SVG_PADDING,
            y=_SVG_PADDING + _SVG_HEADER_H + _SVG_CHART_H + _SVG_FOOTER_BASELINE,
            width=_SVG_WIDTH,
            branding=branding,
        ))

    total_w = _SVG_WIDTH + (_SVG_PADDING * 2)
    bg_rect = svg_rect(0, 0, total_w, height, fill=palette["bg"])
    inner = bg_rect + "".join(pieces)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_fmt_num(total_w)} {_fmt_num(height)}" '
        f'width="{_fmt_num(total_w)}" height="{_fmt_num(height)}">'
        f'{inner}'
        f'</svg>'
    )


# --- Cell renderers (used by HTML and markdown) ---

def _render_cell_text(cell: Cell) -> str:
    """Plain-text rendering of a cell — pre-escape. Used as base for md/html."""
    if isinstance(cell, TextCell):
        return cell.text
    if isinstance(cell, MoneyCell):
        # Sign goes outside the currency symbol: "-$12.34", not "$-12.34".
        sign = "-" if cell.usd < 0 else ""
        return f"{sign}${abs(cell.usd):,.2f}"
    if isinstance(cell, PercentCell):
        return f"{cell.pct:.1f}%"
    if isinstance(cell, DateCell):
        return cell.when.strftime("%Y-%m-%d")
    if isinstance(cell, DeltaCell):
        # Zero is conventionally treated as non-negative for deltas (renders "+0.0%").
        if cell.value > 0:
            sign = "+"
        elif cell.value < 0:
            sign = "-"
        else:
            sign = "+"
        if cell.unit == "%":
            return f"{sign}{abs(cell.value):.1f}%"
        # Sign goes outside the currency symbol for $-deltas too: "-$1.50".
        return f"{sign}${abs(cell.value):,.2f}"
    if isinstance(cell, ProjectCell):
        return cell.label
    raise TypeError(f"unknown cell type: {type(cell).__name__}")


def _render_cell_html(cell: Cell) -> str:
    return _xml_escape(_render_cell_text(cell))


def _render_cell_md(cell: Cell) -> str:
    return _md_escape(_render_cell_text(cell))


# --- HTML chrome and table ---

def _render_html_table(snap: ShareSnapshot, palette: dict) -> str:
    th_cells = "".join(
        f'<th style="text-align:{c.align};padding:6px 10px;background:{palette["table_header_bg"]};color:{palette["fg"]}">{_xml_escape(c.label)}</th>'
        for c in snap.columns
    )
    body_rows = []
    for i, row in enumerate(snap.rows):
        bg = palette["table_row_alt"] if i % 2 == 1 else palette["bg"]
        td_cells = "".join(
            f'<td style="text-align:{c.align};padding:6px 10px;background:{bg};color:{palette["fg"]}">{_render_cell_html(row.cells.get(c.key, TextCell("")))}</td>'
            for c in snap.columns
        )
        body_rows.append(f"<tr>{td_cells}</tr>")
    return (
        f'<table style="border-collapse:collapse;font-family:system-ui,-apple-system,sans-serif;font-size:13px;margin-top:12px">'
        f'<thead><tr>{th_cells}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody>'
        f'</table>'
    )


def _render_html(snap: ShareSnapshot, *, palette: dict, branding: bool) -> str:
    chart_svg = _render_svg(snap, palette=palette, branding=False, include_chrome=False)
    title_html = f'<h1 style="font-size:20px;color:{palette["fg"]};margin:0">{_xml_escape(snap.title)}</h1>'
    subtitle_html = (
        f'<div style="font-size:13px;color:{palette["muted"]};margin-top:4px">{_xml_escape(snap.subtitle)}</div>'
        if snap.subtitle else ""
    )
    timestamp_html = (
        f'<div style="font-size:11px;color:{palette["muted"]};margin-top:4px">'
        f'{_format_generated_at_iso(snap.generated_at)}</div>'
    )
    table_html = _render_html_table(snap, palette)
    if branding:
        # "Generated by cctally" stays as a single plain-text substring so HTML
        # consumers can grep for the branding marker uniformly with the SVG
        # footer; the project URL is the linkable element.
        footer_html = (
            f'<footer style="margin-top:16px;font-size:11px;color:{palette["muted"]}">'
            f'Generated by cctally · '
            f'<a href="https://github.com/omrikais/cctally" style="color:{palette["footer_link"]}">github.com/omrikais/cctally</a>'
            f' · {_version_label(snap.version)}'
            f'</footer>'
        )
    else:
        footer_html = ""
    body = (
        f'<header>{title_html}{subtitle_html}{timestamp_html}</header>'
        f'<div style="margin-top:12px">{chart_svg}</div>'
        f'{table_html}'
        f'{footer_html}'
    )
    return (
        f'<!DOCTYPE html>'
        f'<html lang="en"><head><meta charset="utf-8"><title>{_xml_escape(snap.title)}</title></head>'
        f'<body style="background:{palette["bg"]};font-family:system-ui,-apple-system,sans-serif;padding:20px;max-width:680px;margin:auto">'
        f'{body}'
        f'</body></html>'
    )


# --- Markdown chrome ---

def _render_md_table(snap: ShareSnapshot) -> str:
    """Markdown table per ColumnSpec + Row contract."""
    if not snap.columns:
        return ""
    head = "| " + " | ".join(_md_escape(c.label) for c in snap.columns) + " |"
    sep = "|" + "|".join(
        ":---:" if c.align == "center" else (
            "---:" if c.align == "right" else ":---"
        )
        for c in snap.columns
    ) + "|"
    lines = [head, sep]
    for row in snap.rows:
        cells_md = [
            _render_cell_md(row.cells.get(c.key, TextCell("")))
            for c in snap.columns
        ]
        lines.append("| " + " | ".join(cells_md) + " |")
    return "\n".join(lines)


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
