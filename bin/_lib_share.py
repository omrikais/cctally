"""Pure-function render kernel for shareable reports.

Imported lazily from bin/cctally only when a headliner subcommand is invoked
with --format. Stdlib-only, no I/O, no DB, no filesystem, no locks.

Spec: docs/superpowers/specs/2026-05-08-shareable-reports-design.md
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime


# --- Version + digest ---
#
# KERNEL_VERSION is the contract version of the share renderer. Bump when
# output shape changes in a way that requires re-rendering historical
# basket items / share-history entries. The dashboard composer reads this
# off basket snapshots and tags rows whose stored version != current.
KERNEL_VERSION: int = 1


def _data_digest(payload: object) -> str:
    """Stable sha256 of a JSON-serializable payload.

    Used by share-snapshot envelopes to let the composer detect data drift
    between add-time and compose-time. Key ordering is sorted to make the
    digest insensitive to dict construction order.

    Payload must contain only JSON-native types or types with a stable
    `str()` (e.g. `datetime`); arbitrary objects fall through `default=str`
    and `<X object at 0x…>` reprs are per-process-unstable.
    """
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(canon).hexdigest()


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
    kind: str | None = None   # "project" | "model" | None — privacy chokepoint signal


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
    template_id: str | None = None


# --- Compose: multi-section stitching (M3.1) ---
#
# `compose()` is the multi-section counterpart of `render()`: every basket
# item is rendered via `_render_fragment` (the same body-only path the
# single-panel `render()` uses) and the fragments are stitched under one
# composite chrome — single <html>/<svg> wrapper or one MD frontmatter
# block. See `compose()` for the format-specific stitching rules; the
# dataclasses below pin the request shape.

@dataclass(frozen=True)
class ComposedSection:
    """One section in a multi-section compose request.

    `drift_detected` is metadata only — surfaced to the composer UI as the
    "Outdated" badge (spec §7.7). It must NOT alter the rendered body;
    the renderer ignores it. Compute it server-side by comparing the
    section's `data_digest_at_add` against a fresh `_data_digest` over
    the same panel_data slice.
    """
    snap: ShareSnapshot
    drift_detected: bool


@dataclass(frozen=True)
class ComposeOptions:
    """Composite knobs supplied by the composer modal (spec §8.5).

    `theme`, `format`, `reveal_projects`, and `no_branding` are
    single-source-of-truth: every section is re-rendered with these
    values, regardless of what was captured per-section at add-time.
    """
    title: str
    theme: str             # "light" | "dark"
    format: str            # "md" | "html" | "svg"
    no_branding: bool
    # kernel: informational only — actual scrub happens upstream in
    # the API layer before sections reach `compose()`.
    reveal_projects: bool


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
    # Cycled for stacked-bar segments by sorted-key index. Six entries cover
    # typical model counts (4-6); overflow wraps. Palette ordering is part
    # of the byte-stable contract — adding/reordering is a goldens churn.
    "series_palette": (
        "#2563eb",  # blue-600
        "#9333ea",  # purple-600
        "#059669",  # emerald-600
        "#d97706",  # amber-600
        "#dc2626",  # red-600
        "#0891b2",  # cyan-600
    ),
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
    "series_palette": (
        "#60a5fa",  # blue-400
        "#c084fc",  # purple-400
        "#34d399",  # emerald-400
        "#fbbf24",  # amber-400
        "#f87171",  # red-400
        "#22d3ee",  # cyan-400
    ),
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

    # Y-domain spans primary + multi_series + reference_lines so projected
    # values that exceed the actual-sample max don't clip past the inner box.
    y_values = [p.y_value for p in pts]
    if chart.multi_series:
        for series_pts in chart.multi_series.values():
            y_values.extend(p.y_value for p in series_pts)
    y_values.extend(r[0] for r in chart.reference_lines)
    _, scale_y = _scale_y(y_values, ih)

    # X-domain spans primary + multi_series so a projected ray that extends
    # past the latest actual sample (e.g. forecast `now` -> `week_end`) lands
    # at its true x position rather than getting pinned to enumerate-index.
    # When primary uses sequential `x_value=float(i)` (e.g. report trend),
    # this collapses to the prior `iw / (n-1)` spacing.
    x_values = [p.x_value for p in pts]
    if chart.multi_series:
        for series_pts in chart.multi_series.values():
            x_values.extend(p.x_value for p in series_pts)
    x_min = min(x_values)
    x_max = max(x_values)
    x_span = x_max - x_min
    if x_span <= 1e-9:
        # Degenerate: single point or zero-width domain — anchor at left edge.
        def scale_x(_v: float) -> float:
            return 0.0
    else:
        def scale_x(v: float) -> float:
            return iw * (v - x_min) / x_span

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
    poly_pts = [(ix + scale_x(p.x_value), iy + scale_y(p.y_value)) for p in pts]
    elements.append(svg_polyline(poly_pts, stroke=palette["series_primary"], width=2))

    # Optional multi-series (forecast actual + projected).
    if chart.multi_series:
        for series_key, series_pts in sorted(chart.multi_series.items()):
            series_color = palette["series_secondary"]
            spoly = [(ix + scale_x(p.x_value), iy + scale_y(p.y_value)) for p in series_pts]
            # Dashed for "projected" — simple stroke-dasharray.
            attrs = {
                "points": " ".join(f"{_fmt_num(px)},{_fmt_num(py)}" for px, py in spoly),
                "stroke": series_color,
                "stroke-width": 2,
                "stroke-dasharray": "4 3",
                "fill": "none",
            }
            elements.append(f'<polyline {_serialize_attrs(attrs)}/>')

    # X-tick labels (one per primary sample, positioned by x_value).
    for p in pts:
        tx = ix + scale_x(p.x_value)
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

    has_stacks = bool(chart.stacks)
    # Sorted keys give deterministic stack ordering; matches the
    # `sorted(all_model_keys)` ordering builders use for table columns,
    # so legend swatch -> table column line up by position.
    series_keys = sorted(chart.stacks.keys()) if has_stacks else []

    if has_stacks:
        per_bar_totals: list[float] = []
        for i in range(n):
            total = 0.0
            for k in series_keys:
                sp = chart.stacks[k]
                if i < len(sp):
                    total += sp[i].y_value
            per_bar_totals.append(total)
        y_values = per_bar_totals
    else:
        y_values = [p.y_value for p in pts]
    _, scale_y = _scale_y(y_values, ih)

    elements = []
    elements.append(svg_line(ix, iy + ih, ix + iw, iy + ih,
                             stroke=palette["axis"], width=1))
    elements.append(svg_line(ix, iy, ix, iy + ih,
                             stroke=palette["axis"], width=1))

    series_palette = palette["series_palette"]

    for i, p in enumerate(pts):
        bx = ix + i * (bar_w + bar_gap)
        if has_stacks:
            # Cumulative bottom-up segments. Skip zero/negative segments so
            # they don't emit a degenerate rect (and don't shift the next
            # segment's baseline incorrectly).
            y_running = 0.0
            for k_idx, k in enumerate(series_keys):
                sp = chart.stacks[k]
                seg_v = sp[i].y_value if i < len(sp) else 0.0
                if seg_v <= 0:
                    continue
                seg_top_y = iy + scale_y(y_running + seg_v)
                seg_bot_y = iy + scale_y(y_running)
                seg_h = seg_bot_y - seg_top_y
                color = series_palette[k_idx % len(series_palette)]
                elements.append(svg_rect(bx, seg_top_y, bar_w, seg_h, fill=color))
                y_running += seg_v
        else:
            by = iy + scale_y(p.y_value)
            bh = (iy + ih) - by
            elements.append(svg_rect(bx, by, bar_w, bh, fill=palette["series_primary"]))
        # X-tick label centered under bar.
        tx = bx + bar_w / 2
        elements.append(svg_text(tx, iy + ih + 14, p.x_label,
                                 font_size=10, fill=palette["muted"], anchor="middle"))

    # Legend (top-right of inner box, only when stacks are present).
    # SVG is the only artifact where the table doesn't double as a key, so
    # the legend matters most for `--format svg` output. Placed inside the
    # inner box so total chart dimensions stay byte-stable.
    if has_stacks:
        legend_swatch_w = 8.0
        legend_swatch_h = 8.0
        legend_row_h = 12.0
        legend_col_w = 160.0
        legend_left = ix + iw - legend_col_w
        for k_idx, k in enumerate(series_keys):
            row_y = iy + 4 + k_idx * legend_row_h
            color = series_palette[k_idx % len(series_palette)]
            elements.append(svg_rect(
                legend_left, row_y, legend_swatch_w, legend_swatch_h,
                fill=color,
            ))
            elements.append(svg_text(
                legend_left + legend_swatch_w + 4, row_y + 8, k,
                font_size=10, fill=palette["fg"], anchor="start",
            ))

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
                money += cell.usd
        if proj_label is not None:
            costs[proj_label] = costs.get(proj_label, 0.0) + money

    if snap.chart is not None:
        chart_pts: list[ChartPoint] = []
        if isinstance(snap.chart, LineChart):
            chart_pts = list(snap.chart.points)
            if snap.chart.multi_series:
                for series in snap.chart.multi_series.values():
                    chart_pts.extend(series)
        elif isinstance(snap.chart, BarChart):
            chart_pts = list(snap.chart.points)
            if snap.chart.stacks:
                for series in snap.chart.stacks.values():
                    chart_pts.extend(series)
        elif isinstance(snap.chart, HorizontalBarChart):
            chart_pts = list(snap.chart.points)
        # Chart-only fallback: tiebreaker only — `y_value` is dollars for project
        # bar charts but may be a ratio for trend charts. Affects sort order of
        # project-N labels, not anonymization correctness.
        for p in chart_pts:
            if p.project_label and p.project_label not in costs:
                costs[p.project_label] = p.y_value

    # project-typed columns (cross-tab Detail templates, issue #33). Sum the
    # MoneyCell values for each kind='project' column across all rows; the
    # column.label is the original project path (anon happens AFTER _collect).
    # No current panel mixes ProjectCell rows AND project-typed columns — if a
    # future template does, the `+=` here will double-count that project's
    # total. Refactor to a (path, source) keyed accumulator if/when that lands.
    for col in snap.columns:
        if col.kind != "project":
            continue
        col_total = 0.0
        for row in snap.rows:
            cell = row.cells.get(col.key)
            if isinstance(cell, MoneyCell):
                col_total += cell.usd
        costs[col.label] = costs.get(col.label, 0.0) + col_total

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


def _apply_anon_mapping(
    snap: ShareSnapshot, mapping: dict[str, str],
) -> ShareSnapshot:
    """Return a new ShareSnapshot with project labels replaced everywhere.

    Walks: (a) every Row.cells dict — replaces ProjectCell.label;
    (b) ChartPoint.project_label AND .x_label (when x_label == project_label,
    i.e. project-axis charts) on chart.points + multi_series + stacks.
    """
    new_rows: list[Row] = []
    for row in snap.rows:
        new_cells: dict[str, Cell] = {}
        for key, cell in row.cells.items():
            if isinstance(cell, ProjectCell) and cell.label in mapping:
                new_cells[key] = ProjectCell(mapping[cell.label])
            else:
                new_cells[key] = cell
        new_rows.append(Row(cells=new_cells))

    new_chart: ChartSpec | None = snap.chart
    if snap.chart is not None:
        def _rewrite_pt(p: ChartPoint) -> ChartPoint:
            if p.project_label:
                # Fail-safe: any label not in mapping (e.g., from drift between
                # _collect and _apply, or a future code path that adds chart
                # points after gather) is mapped to "(unknown)" rather than
                # passed through. Privacy invariant: never leak a non-anonymized
                # label, even if the gather pass missed it.
                new_label = mapping.get(p.project_label, "(unknown)")
            else:
                new_label = None
            # x_label rewrite stays guarded — only anonymize if x_label is the
            # project axis AND the label is in mapping (preserves non-project
            # x_label values like time labels).
            if (p.project_label
                    and p.x_label == p.project_label
                    and p.x_label in mapping):
                new_x = mapping[p.x_label]
            else:
                new_x = p.x_label
            return ChartPoint(
                x_label=new_x,
                x_value=p.x_value,
                y_value=p.y_value,
                project_label=new_label,
                series_key=p.series_key,
            )

        if isinstance(snap.chart, LineChart):
            new_chart = LineChart(
                points=tuple(_rewrite_pt(p) for p in snap.chart.points),
                y_label=snap.chart.y_label,
                reference_lines=snap.chart.reference_lines,
                multi_series=(
                    {k: tuple(_rewrite_pt(p) for p in v)
                     for k, v in snap.chart.multi_series.items()}
                    if snap.chart.multi_series else None
                ),
            )
        elif isinstance(snap.chart, BarChart):
            new_chart = BarChart(
                points=tuple(_rewrite_pt(p) for p in snap.chart.points),
                y_label=snap.chart.y_label,
                stacks=(
                    {k: tuple(_rewrite_pt(p) for p in v)
                     for k, v in snap.chart.stacks.items()}
                    if snap.chart.stacks else None
                ),
            )
        elif isinstance(snap.chart, HorizontalBarChart):
            new_chart = HorizontalBarChart(
                points=tuple(_rewrite_pt(p) for p in snap.chart.points),
                x_label=snap.chart.x_label,
                cap=snap.chart.cap,
            )

    # Rewrite project-typed column headers (cross-tab Detail templates, issue
    # #33). Fail-closed: any column.label not in `mapping` maps to "(unknown)",
    # mirroring the ChartPoint arm above. Frozen-dataclass-compliant — we emit
    # a new tuple of new ColumnSpec instances, never mutate snap.columns.
    new_columns: list[ColumnSpec] = []
    for col in snap.columns:
        if col.kind == "project":
            new_label = mapping.get(col.label, "(unknown)")
            new_columns.append(ColumnSpec(
                key=col.key, label=new_label,
                align=col.align, emphasis=col.emphasis, kind=col.kind,
            ))
        else:
            new_columns.append(col)

    # When ShareSnapshot grows a new field, add it to this constructor — the
    # scrubber must thread every field through to preserve frozen semantics.
    return ShareSnapshot(
        cmd=snap.cmd,
        title=snap.title,
        subtitle=snap.subtitle,
        period=snap.period,
        columns=tuple(new_columns),
        rows=tuple(new_rows),
        chart=new_chart,
        totals=snap.totals,
        notes=snap.notes,
        generated_at=snap.generated_at,
        version=snap.version,
        template_id=snap.template_id,
    )


def _scrub(snap: ShareSnapshot, *, reveal_projects: bool) -> ShareSnapshot:
    """Anonymize project labels unless reveal_projects is True.

    When reveal_projects is True, returns the SAME instance (identity preserved
    so callers can rely on `out is snap`). When False, returns a NEW snapshot
    with ProjectCell labels and ChartPoint project/x labels rewritten via
    `_build_anon_mapping`. If no project labels are present in the snapshot,
    also returns the original instance.
    """
    if reveal_projects:
        return snap
    project_costs = _collect_project_costs(snap)
    if not project_costs:
        return snap
    mapping = _build_anon_mapping(project_costs)
    return _apply_anon_mapping(snap, mapping)


# --- Format renderers ---

def _render_md_fragment(snap: ShareSnapshot, *, branding: bool) -> str:
    """Render the MD section body.

    M1.2 contract: returns the full current `_render_md` body. Frontmatter
    (added by M2.2) is layered on at the wrap step via `_build_md_frontmatter`
    + `_wrap_document`. Fragment shape is body-only by definition; even
    without frontmatter the wrap layer remains the single chrome chokepoint
    so future surfaces (compose, history) extend it once.
    """
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
# Vertical padding between stacked sections in `_stitch_svg`.
_SVG_SECTION_GAP = 20.0


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


def _render_html_fragment(snap: ShareSnapshot, *, palette: dict, branding: bool) -> str:
    """Render the HTML body fragment — header + chart + table + (branded footer).

    Document chrome (<!DOCTYPE>/<html>/<head>/<body>) is layered on at the wrap
    step via `_wrap_document`, keeping body-only content composable for v2's
    multi-section stitcher.
    """
    # `_share_apply_content_toggles` sets `snap.chart=None` for show_chart=False
    # and `snap.columns=()`/`snap.rows=()` for show_table=False. Gate the chart
    # wrapper div + the table chrome on those, so disabled sections drop entirely
    # rather than rendering empty chrome (an empty `<svg>` chart area or an
    # `<table>` with no `<th>`/`<td>`).
    chart_html = (
        f'<div style="margin-top:12px">{_render_svg(snap, palette=palette, branding=False, include_chrome=False)}</div>'
        if snap.chart is not None else ""
    )
    title_html = f'<h1 style="font-size:20px;color:{palette["fg"]};margin:0">{_xml_escape(snap.title)}</h1>'
    subtitle_html = (
        f'<div style="font-size:13px;color:{palette["muted"]};margin-top:4px">{_xml_escape(snap.subtitle)}</div>'
        if snap.subtitle else ""
    )
    timestamp_html = (
        f'<div style="font-size:11px;color:{palette["muted"]};margin-top:4px">'
        f'{_format_generated_at_iso(snap.generated_at)}</div>'
    )
    table_html = _render_html_table(snap, palette) if snap.columns else ""
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
    return (
        f'<header>{title_html}{subtitle_html}{timestamp_html}</header>'
        f'{chart_html}'
        f'{table_html}'
        f'{footer_html}'
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


# --- SVG fragment ---


def _strip_outer_svg_tag(full_svg: str) -> tuple[str, float, float]:
    """Extract inner XML + width/height from a standalone `<svg w h>...</svg>`.

    Contract drift between renderer and stripper would raise here. Used by the
    SVG fragment path so compose can position multiple sections vertically
    inside one outer `<svg viewBox>` without nested-document weirdness.
    """
    m = re.match(
        r'<svg[^>]*\bwidth="(?P<w>[\d.]+)"[^>]*\bheight="(?P<h>[\d.]+)"[^>]*>'
        r'(?P<body>.*)</svg>\s*$',
        full_svg,
        flags=re.DOTALL,
    )
    if not m:
        raise ValueError("unexpected SVG shape (renderer contract drift)")
    return m.group("body"), float(m.group("w")), float(m.group("h"))


def _render_svg_fragment(snap: ShareSnapshot, *, palette: dict, branding: bool) -> tuple[str, float, float]:
    """Return (inner_xml, width, height) for a chart-and-chrome section.

    Calls into the existing `_render_svg(include_chrome=True)` producer, then
    strips the outer `<svg ...>` so the wrap step can rewrap byte-identically
    today and compose can stitch sections under one viewBox later.
    """
    full = _render_svg(snap, palette=palette, branding=branding, include_chrome=True)
    return _strip_outer_svg_tag(full)


# --- Print stylesheet + MD frontmatter (placeholders for M2.x layering) ---

def _print_stylesheet() -> str:
    """Print-only CSS injected into HTML <head> for PDF export polish (spec §11.2).

    M1.2 stub returned this string but it was NOT wired into `_wrap_document`
    yet to keep v1 HTML goldens byte-stable through M1-M3. M4.2 wires it in
    so Print → PDF on a dark-theme export renders as black-on-white instead
    of a solid-black page, and forces page-break-inside avoidance on
    semantic blocks. v1 + v2 HTML goldens re-baseline once on first run
    after this change and are byte-stable thereafter; MD + SVG goldens are
    unaffected (the stylesheet only lives in the HTML document head).
    """
    return (
        '<style>@media print {'
        ' body { color-scheme: light; background: #fff !important; color: #000 !important; }'
        ' header, footer, section { page-break-inside: avoid; }'
        '}</style>'
    )


def _build_md_frontmatter(snap: ShareSnapshot) -> str:
    """YAML frontmatter prepended to MD exports (spec §11.5).

    Byte-stable: key order is fixed (title -> generated_at -> period ->
    panel -> optional template_id -> anonymized -> cctally_version);
    single-line values; no eolian formatting. `_wrap_document` strips this when
    `branding=False` so `--no-branding` behaves consistently with the
    HTML/SVG footer-link stripping.

    `template_id` is present for dashboard share-v2 snapshots and omitted
    for legacy CLI snapshots that have no template recipe.

    `anonymized` reflects whether `_scrub` has rewritten this snapshot --
    detected via `_snapshot_is_anonymized` (label-prefix heuristic; see
    that function for the contract).
    """
    period = snap.period
    period_iso = (
        f"{period.start.isoformat()}.."
        f"{period.end.isoformat()}"
    )
    anonymized = "true" if _snapshot_is_anonymized(snap) else "false"
    lines = [
        "---",
        f"title: {_yaml_scalar(snap.title)}",
        f"generated_at: {snap.generated_at.isoformat()}",
        f"period: {period_iso}",
        f"panel: {snap.cmd}",
    ]
    if snap.template_id:
        lines.append(f"template_id: {_yaml_scalar(snap.template_id)}")
    lines.extend([
        f"anonymized: {anonymized}",
        f"cctally_version: {snap.version}",
        "---",
        "",
    ])
    return "\n".join(lines)


def _yaml_scalar(s: str) -> str:
    """Quote a YAML scalar value when it would otherwise be ambiguous.

    YAML 1.2 reserves leading `:`, `#`, `&`, `*`, `!`, `|`, `>`, `'`,
    `"`, `%`, `@`, `` ` `` and embedded `:` in plain scalars. We quote
    aggressively (when the value contains any of these or leading/trailing
    whitespace) to keep frontmatter parsers happy. Single quotes use
    YAML's `''` escape for the rare title containing a quote.
    """
    if not s:
        return '""'
    if any(c in s for c in ":#&*!|>'\"%@`") or s.strip() != s:
        return "'" + s.replace("'", "''") + "'"
    return s


def _snapshot_is_anonymized(snap: ShareSnapshot) -> bool:
    """Return True if every project label (cell or column) is anon or sentinel.

    `_scrub` rewrites labels to `project-<N>` (1-indexed, cost-descending).
    A snapshot with no `ProjectCell` rows AND no `kind='project'` columns
    returns False (nothing was anonymized because there was nothing to
    anonymize). `(unknown)` is the project-share sentinel for missing
    project_path (see `cmd_project`'s `_proj_label_for`) — it is never a
    revealed real name, so it is counted as also-anonymized. Mixed snapshots
    (some scrubbed, some revealed) are reported False to keep the
    frontmatter semantic ("are projects revealed in this MD?").

    Cross-tab Detail templates (issue #33) carry project labels in
    `kind='project'` columns rather than `ProjectCell` rows; we walk both
    surfaces so MD frontmatter `anonymized:` stays correct for those panels.
    """
    cells = [
        cell
        for row in snap.rows
        for cell in row.cells.values()
        if isinstance(cell, ProjectCell)
    ]
    project_cols = [col for col in snap.columns if col.kind == "project"]
    if not cells and not project_cols:
        return False

    def _is_anon(label: str) -> bool:
        return bool(re.fullmatch(r"project-\d+", label)) or label == "(unknown)"

    return (
        all(_is_anon(c.label) for c in cells)
        and all(_is_anon(col.label) for col in project_cols)
    )


# --- Fragment + wrap ---

def _render_fragment(snap: ShareSnapshot, *, format: str,
                     palette: Mapping[str, str], branding: bool) -> "str | tuple[str, float, float]":
    """Body-only render — no document chrome.

    Returns:
      - format="html": str — the body fragment (header + chart + table + footer).
      - format="md":   str — the markdown body (frontmatter not prepended).
      - format="svg":  tuple[str, float, float] — (inner_xml, width, height).

    Callers compose this into either:
      - render(): wraps in full document chrome via `_wrap_document`.
      - compose(): stitches multiple fragments under one wrapper (M3.x).
    """
    if format == "html":
        return _render_html_fragment(snap, palette=palette, branding=branding)
    if format == "svg":
        return _render_svg_fragment(snap, palette=palette, branding=branding)
    if format == "md":
        return _render_md_fragment(snap, branding=branding)
    raise ValueError(f"unknown format: {format!r}")


def _wrap_document(fragment, *, format: str, palette: Mapping[str, str] | None,
                   snap: ShareSnapshot, branding: bool = True) -> str:
    """Wrap a fragment in document chrome.

    Byte-stability invariant: for v1 single-section snapshots, the wrapped
    HTML/SVG output must equal the pre-refactor `_render_<fmt>` output
    character-for-character. The v1 share goldens (`bin/cctally-share-test`)
    are the gate.

    MD: prepends `_build_md_frontmatter(snap)` when `branding=True` (spec
    §11.5). Suppressed when `branding=False` -- same surface as the
    HTML/SVG footer-link strip done inside the per-format renderers --
    so `--no-branding` behaves consistently across all three formats.
    """
    if format == "html":
        return (
            f'<!DOCTYPE html>'
            f'<html lang="en"><head><meta charset="utf-8">'
            f'<title>{_xml_escape(snap.title)}</title>'
            f'{_print_stylesheet()}'
            f'</head>'
            f'<body style="background:{palette["bg"]};font-family:system-ui,-apple-system,sans-serif;padding:20px;max-width:680px;margin:auto">'
            f'{fragment}'
            f'</body></html>'
        )
    if format == "svg":
        inner, w, h = fragment
        # Mirror `_render_svg`'s exact outer-tag shape (xmlns, viewBox+w+h via
        # `_fmt_num`) so single-section wraps are byte-identical to the v1
        # producer. The 0 0 origin matches `_render_svg`'s `viewBox` literal.
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {_fmt_num(w)} {_fmt_num(h)}" '
            f'width="{_fmt_num(w)}" height="{_fmt_num(h)}">'
            f'{inner}'
            f'</svg>'
        )
    if format == "md":
        front = _build_md_frontmatter(snap) if branding else ""
        # Frontmatter already ends with "---\n" (trailing "" in the join
        # adds the separator newline); concat directly so the byte shape
        # is `---\n...---\n<fragment>`. When branding=False, front is
        # "" and the fragment passes through untouched.
        return (front + fragment) if front else fragment
    raise ValueError(f"unknown format: {format!r}")


# --- Compose: stitch many fragments under one chrome (M3.1) ---

def compose(sections: tuple[ComposedSection, ...], *, opts: ComposeOptions) -> str:
    """Stitch multiple section fragments into a single document.

    Pure function. Each section's body comes from `_render_fragment(...)` —
    the same body-only renderer used by single-panel share. `compose`
    wraps them all in composite chrome (one title, one footer, one outer
    wrapper) per format-specific stitching rules in spec §4.3.

    `opts.reveal_projects` does NOT scrub here — scrubbing must have
    happened upstream (in the API layer) before the snapshot reaches
    this function. The kernel is anon-agnostic at compose time; the
    composer endpoint is the chokepoint.
    """
    if not sections:
        raise ValueError("compose requires at least one section")
    fmt = opts.format
    if fmt == "html":
        return _stitch_html(sections, opts=opts)
    if fmt == "md":
        return _stitch_md(sections, opts=opts)
    if fmt == "svg":
        return _stitch_svg(sections, opts=opts)
    raise ValueError(f"unknown format: {fmt!r}")


def _stitch_html(sections: tuple[ComposedSection, ...], *,
                 opts: ComposeOptions) -> str:
    """HTML compose: single ``<html><body>`` wrapper, sections as ``<section>`` blocks."""
    palette = PALETTE_LIGHT if opts.theme == "light" else PALETTE_DARK
    body_open = (
        f'<body style="background:{palette["bg"]};'
        f'font-family:system-ui,-apple-system,sans-serif;'
        f'padding:20px;max-width:680px;margin:auto">'
    )
    header = f'<header><h1>{_xml_escape(opts.title)}</h1></header>'
    blocks = []
    for sec in sections:
        # branding here is for the *fragment* — composite footer is one
        # level up, so per-section branding is unconditional False to
        # keep the chrome single.
        frag = _render_fragment(sec.snap, format="html",
                                palette=palette, branding=False)
        blocks.append(f'<section class="share-section">{frag}</section>')
    footer = (
        f'<footer style="font-size:11px;color:{palette["muted"]};margin-top:24px">'
        f'cctally · composed</footer>' if not opts.no_branding else ""
    )
    return (
        f'<!DOCTYPE html>'
        f'<html lang="en"><head><meta charset="utf-8">'
        f'<title>{_xml_escape(opts.title)}</title>'
        f'{_print_stylesheet()}'
        f'</head>{body_open}'
        f'{header}{"".join(blocks)}{footer}'
        f'</body></html>'
    )


def _stitch_md(sections: tuple[ComposedSection, ...], *,
               opts: ComposeOptions) -> str:
    """MD compose: one composite frontmatter + ``## `` headers + bodies."""
    parts: list[str] = []
    if not opts.no_branding:
        # Composite frontmatter: same key set as the single-section
        # `_build_md_frontmatter` but `panel` becomes `composed` and
        # `template_id` is omitted because one composed document can contain
        # multiple section templates.
        # `generated_at` and `cctally_version` are taken from the first
        # section since the composite document has no independent
        # provenance — every section was rendered in the same request.
        first_snap = sections[0].snap
        # `period` for the composite document = earliest start ..
        # latest end across all sections (per spec §11.5 implied
        # convention; reference test uses identical periods so the
        # union collapses).
        earliest = min(sec.snap.period.start for sec in sections)
        latest = max(sec.snap.period.end for sec in sections)
        anon_field = (
            "true"
            if all(_snapshot_is_anonymized(s.snap) for s in sections)
            else "false"
        )
        parts.append(
            "---\n"
            f"title: {_yaml_scalar(opts.title)}\n"
            f"generated_at: {first_snap.generated_at.isoformat()}\n"
            f"period: {earliest.isoformat()}..{latest.isoformat()}\n"
            f"panel: composed\n"
            f"anonymized: {anon_field}\n"
            f"cctally_version: {first_snap.version}\n"
            "---\n\n"
        )
    # Title as H1 (when frontmatter is present, this duplicates the
    # title key visually — accept the duplication; markdown readers
    # vary in how they render frontmatter and the H1 is the universal
    # fallback). Title and per-section heading go through `_md_escape`
    # to match the single-section path (`_render_md_body` at line 915);
    # otherwise inline HTML or MD specials in a user-entered title
    # would survive into the export unescaped.
    parts.append(f"# {_md_escape(opts.title)}\n\n")
    last_idx = len(sections) - 1
    for idx, sec in enumerate(sections):
        frag = _render_fragment(sec.snap, format="md", palette=PALETTE_LIGHT,
                                branding=False)
        parts.append(f"## {_md_escape(sec.snap.title)}\n\n")
        parts.append(frag.rstrip("\n"))
        parts.append("\n\n" if idx < last_idx else "\n")
    return "".join(parts)


def _stitch_svg(sections: tuple[ComposedSection, ...], *,
                opts: ComposeOptions) -> str:
    """SVG compose: single outer ``<svg>``, sections positioned vertically.

    `opts.no_branding` is intentionally unused: the SVG composite has no
    chrome footer band, so there is nothing to strip. HTML stitcher uses
    it to gate the `<footer>cctally · composed</footer>` line.
    """
    palette = PALETTE_LIGHT if opts.theme == "light" else PALETTE_DARK
    inners: list[tuple[str, float, float]] = []
    for sec in sections:
        inner, w, h = _render_fragment(sec.snap, format="svg",
                                       palette=palette, branding=False)
        inners.append((inner, w, h))
    total_w = max(w for _, w, _ in inners)
    total_h = sum(h for _, _, h in inners) + _SVG_SECTION_GAP * (len(inners) - 1)
    body_blocks: list[str] = []
    y = 0.0
    for inner, _w, h in inners:
        body_blocks.append(
            f'<g transform="translate(0,{_fmt_num(y)})">{inner}</g>'
        )
        y += h + _SVG_SECTION_GAP
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {_fmt_num(total_w)} {_fmt_num(total_h)}" '
        f'width="{_fmt_num(total_w)}" height="{_fmt_num(total_h)}">'
        f'{"".join(body_blocks)}'
        f'</svg>'
    )


# --- Public dispatch ---

def render(snap: ShareSnapshot, *, format: str, theme: str, branding: bool) -> str:
    """Render a snapshot to the requested format.

    Pure function: no I/O, no DB, no filesystem, no locks. Caller is
    responsible for emitting the result (stdout/file/clipboard/open).

    Thin delegator over `_render_fragment` + `_wrap_document`: separates
    body-only rendering from document chrome so compose can stitch multiple
    sections under a single wrapper (M3.x).
    """
    if format == "md":
        frag = _render_fragment(snap, format="md", palette=PALETTE_LIGHT, branding=branding)
        return _wrap_document(frag, format="md", palette=PALETTE_LIGHT, snap=snap,
                              branding=branding)

    if theme == "light":
        palette = PALETTE_LIGHT
    elif theme == "dark":
        palette = PALETTE_DARK
    else:
        raise ValueError(f"unknown theme: {theme!r}")

    if format not in ("svg", "html"):
        raise ValueError(f"unknown format: {format!r}")
    frag = _render_fragment(snap, format=format, palette=palette, branding=branding)
    return _wrap_document(frag, format=format, palette=palette, snap=snap,
                          branding=branding)


# --- Backward-compat shims (Layer-A unit tests target these private helpers) ---
#
# The `_render_md` / `_render_html` names predate the fragment+wrap split.
# v1 share goldens (`bin/cctally-share-test`) go through `render()` — these
# shims exist solely to keep the Layer-A unit suite in `tests/test_lib_share.py`
# pointed at byte-identical output without rewriting every call site. New code
# should use `_render_fragment` + `_wrap_document` directly.

def _render_md(snap: ShareSnapshot, *, branding: bool) -> str:
    frag = _render_fragment(snap, format="md", palette=PALETTE_LIGHT, branding=branding)
    return _wrap_document(frag, format="md", palette=PALETTE_LIGHT, snap=snap,
                          branding=branding)


def _render_html(snap: ShareSnapshot, *, palette: dict, branding: bool) -> str:
    frag = _render_fragment(snap, format="html", palette=palette, branding=branding)
    return _wrap_document(frag, format="html", palette=palette, snap=snap,
                          branding=branding)
