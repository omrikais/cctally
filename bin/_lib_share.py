"""Pure-function render kernel for shareable reports.

Imported lazily from bin/cctally only when a headliner subcommand is invoked
with --format. Stdlib-only, no I/O, no DB, no filesystem, no locks.

Spec: docs/superpowers/specs/2026-05-08-shareable-reports-design.md
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


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
    """Anonymization chokepoint — scrubber rewrites the `label` field.

    `rank_cost` (#130): an explicit spend value used by
    `_collect_project_costs` to rank anonymized labels, replacing the old
    hidden-MoneyCell hack. When None, ranking falls back to summing sibling
    MoneyCells (back-compat for every non-budget construction site)."""
    label: str
    rank_cost: float | None = None
    identity: str | None = None

Cell = TextCell | MoneyCell | PercentCell | DateCell | DeltaCell | ProjectCell


# --- Table primitives ---

@dataclass(frozen=True)
class ColumnSpec:
    key: str
    label: str
    align: str = "left"   # "left" | "right" | "center"
    emphasis: bool = False
    kind: str | None = None   # "project" | "model" | None — privacy chokepoint signal
    project_identity: str | None = None


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
    """One chart datum.

    `x_label_kind` is the AXIS DISCRIMINATOR. Privacy preparation rewrites the
    `x_label` of a `"project"` axis and leaves a `"plain"` axis untouched; it
    must never infer the axis from `x_label == project_label`, because the two
    `sessions` builders deliberately break that equality (issue #503 F1).
    A `"project"` axis composes its label from the resolved project display
    name, prefixed by `x_label_prefix` (the cost rank) when one is present.
    """
    x_label: str
    x_value: float
    y_value: float
    project_label: str | None = None
    series_key: str | None = None
    project_identity: str | None = None
    x_label_kind: Literal["plain", "project"] = "plain"
    x_label_prefix: str | None = None


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
    source: Literal["claude", "codex"] = "claude"
    source_label: str | None = None
    availability: Literal["ok", "empty", "unavailable"] = "ok"
    availability_reason: str | None = None

    def __post_init__(self) -> None:
        if self.source not in {"claude", "codex"}:
            raise ValueError("share source must be claude or codex")
        if self.availability not in {"ok", "empty", "unavailable"}:
            raise ValueError("share availability must be ok, empty, or unavailable")
        if self.availability == "unavailable":
            if not self.availability_reason:
                raise ValueError("unavailable share snapshots require a reason")
        elif self.availability_reason is not None:
            raise ValueError("availability reason is only valid for unavailable snapshots")


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
    # `compose()` reads this to prepare every section itself (#503 S1).
    # Sections must arrive RAW; callers must not anonymize upstream.
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
             anchor: str = "start", weight: str = "normal",
             font_family: str | None = None) -> str:
    attrs = {
        "x": x,
        "y": y,
        "font-size": font_size,
        "fill": fill,
        "text-anchor": anchor,
    }
    if weight and weight != "normal":
        attrs["font-weight"] = weight
    if font_family:
        attrs["font-family"] = font_family
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
    # The right-most sample lands at the inner-box right edge (ix + iw). A
    # centered (anchor="middle") label there overflows the chart's right
    # padding and is clipped at the SVG viewBox boundary — most visible for
    # wide labels (10-char ISO dates, as the `$ / day` current-week chart
    # uses) at narrow render widths (#215). Right-align (anchor="end") any tick
    # that lands on the right edge so its full width stays inside the plot;
    # interior + left-edge ticks stay centered. Position-based (not index-based)
    # so a forecast chart whose last *primary* sample sits mid-plot — with a
    # projected ray extending past it via multi_series — keeps that tick
    # centered rather than mis-anchoring a non-edge label.
    right_edge_x = ix + iw
    for p in pts:
        tx = ix + scale_x(p.x_value)
        anchor = "end" if tx >= right_edge_x - 1e-6 else "middle"
        elements.append(svg_text(tx, iy + ih + 14, p.x_label,
                                 font_size=10, fill=palette["muted"], anchor=anchor))

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
    source_label, availability = _source_chrome(snap)
    if source_label:
        source_text = source_label if availability is None else f"{source_label} · {availability}"
        elements.append(svg_text(x, y + 54, source_text,
                                 font_size=10, fill=palette["muted"]))
    elements.append(svg_text(x + width, y + 18,
                             _format_generated_at_iso(snap.generated_at),
                             font_size=10, fill=palette["muted"], anchor="end"))
    return svg_group(elements)


def _svg_header_height(snap: ShareSnapshot, *, include_chrome: bool) -> float:
    """Reserve the added source/availability line without perturbing v1 SVGs."""
    if not include_chrome:
        return 0.0
    source_label, _availability = _source_chrome(snap)
    return _SVG_HEADER_H + (18.0 if source_label else 0.0)


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


def _source_chrome(snap: ShareSnapshot) -> tuple[str | None, str | None]:
    """Return privacy-safe provider and availability text for new snapshots.

    The all-default Claude shape deliberately returns no provider line so every
    existing share artifact remains byte-for-byte unchanged.
    """
    show_source = (
        snap.source != "claude"
        or snap.source_label is not None
        or snap.availability != "ok"
    )
    label = snap.source_label or ("Claude" if snap.source == "claude" else "Codex")
    status = (
        "No data" if snap.availability == "empty" else
        f"Unavailable: {snap.availability_reason}"
        if snap.availability == "unavailable" else None
    )
    return (label if show_source else None, status)


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


_ProjectAnonKey = tuple[Literal["legacy", "qualified"], str]


def _project_anon_key(label: str, identity: str | None) -> _ProjectAnonKey:
    """Keep opaque qualified keys disjoint from legacy display labels."""
    return ("qualified", identity) if identity is not None else ("legacy", label)


def _collect_project_identity_costs(snap: ShareSnapshot) -> dict[_ProjectAnonKey, float]:
    """Walk rows: for each row containing a ProjectCell, sum MoneyCell values
    in the same row under the project label — unless the ProjectCell carries an
    explicit ``rank_cost``, which takes precedence over the MoneyCell sum (#130).

    Charts also contribute via ChartPoint.project_label + y_value (when y_value
    is in $). For consistency we union both sources; rows take precedence on
    duplicates."""
    costs: dict[_ProjectAnonKey, float] = {}
    for row in snap.rows:
        project: ProjectCell | None = None
        money = 0.0
        for cell in row.cells.values():
            if isinstance(cell, ProjectCell):
                project = cell
            elif isinstance(cell, MoneyCell):
                money += cell.usd
        if project is not None:
            key = _project_anon_key(project.label, project.identity)
            contribution = project.rank_cost if project.rank_cost is not None else money
            costs[key] = costs.get(key, 0.0) + contribution

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
            if p.project_label:
                key = _project_anon_key(p.project_label, p.project_identity)
                if key not in costs:
                    costs[key] = p.y_value

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
        key = _project_anon_key(col.label, col.project_identity)
        costs[key] = costs.get(key, 0.0) + col_total

    return costs


def _collect_project_costs(
    snap: ShareSnapshot,
) -> dict[_ProjectAnonKey, float] | dict[str, float]:
    """Return the historical legacy shape unless qualified identity is present.

    The scrubber consumes :func:`_collect_project_identity_costs` directly so
    its internal keys remain domain tagged. This compatibility facade keeps
    callers of the older all-``None`` model on their established string-key
    contract.
    """
    costs = _collect_project_identity_costs(snap)
    if all(domain == "legacy" for domain, _value in costs):
        return {label: cost for (_domain, label), cost in costs.items()}
    return costs


def _build_anon_mapping(
    project_costs: dict[_ProjectAnonKey, float] | dict[str, float],
) -> dict[_ProjectAnonKey, str] | dict[str, str]:
    """Sort identities by descending cost (lex tie-break); assign project-1, project-2, ...

    "(unknown)" is never numbered — keeps its literal label.
    """
    legacy_input = all(isinstance(key, str) for key in project_costs)
    normalized: dict[_ProjectAnonKey, float] = {
        (("legacy", key) if isinstance(key, str) else key): cost
        for key, cost in project_costs.items()
    }
    items = [
        (key, cost)
        for key, cost in normalized.items()
        if key != ("legacy", "(unknown)")
    ]
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    mapping: dict[_ProjectAnonKey, str] = {
        key: f"project-{i + 1}" for i, (key, _cost) in enumerate(items)
    }
    if ("legacy", "(unknown)") in normalized:
        mapping[("legacy", "(unknown)")] = "(unknown)"
    if legacy_input:
        return {key[1]: value for key, value in mapping.items()}
    return mapping


def anonymize_account_label(
    label: str, index: int, *, reveal: bool,
) -> str:
    """Map an account label to ``Account A/B/C`` unless ``reveal`` (#341 Task 4).

    Account labels are user data (spec §4 Privacy) — a share/export must never
    leak them, exactly like project names. This is the fail-closed anonymization
    chokepoint: ``reveal`` is the SAME toggle that governs project reveal
    (``reveal_projects``), so anon-mode (the default) rewrites the label to a
    positional ``Account <letter>`` keyed by the account's deterministic registry
    ``index`` (0→A, 1→B, …); only an explicit reveal shows the real label. Emails
    never enter a share at all — the ``data.accounts[]`` wire carries no email
    field — so this covers the only account user-data a share can see.
    """
    if reveal:
        return label
    if index < 0:
        index = 0
    # A..Z then Account-27, Account-28, … (registries never approach 26 real
    # accounts, but stay total rather than raise).
    if index < 26:
        return f"Account {chr(ord('A') + index)}"
    return f"Account {index + 1}"


def _apply_anon_mapping(
    snap: ShareSnapshot, mapping: dict[_ProjectAnonKey, str] | dict[str, str],
) -> ShareSnapshot:
    """Return a new ShareSnapshot with project labels replaced everywhere.

    Kept for backward compatibility as `_scrub`'s applier. It normalizes the
    legacy all-string key shape and then delegates to the single project-field
    walker `_apply_project_mapping`, so there is exactly one implementation of
    "rewrite every typed project display field" rather than two that have to
    agree by inspection — which is how the `x_label` fall-through survived.
    """
    tagged_mapping: dict[_ProjectAnonKey, str] = {
        ("legacy", key) if isinstance(key, str) else key: value
        for key, value in mapping.items()
    }
    return _apply_project_mapping(snap, tagged_mapping)


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
    project_costs = _collect_project_identity_costs(snap)
    if not project_costs:
        return snap
    mapping = _build_anon_mapping(project_costs)
    return _apply_anon_mapping(snap, mapping)


# --- Preparation: the privacy contract (#503 S1) ---
#
# `_scrub` above is a hand-enumerated field walker: it visits three sites out
# of a seventeen-field frozen dataclass graph and copies everything else
# through untouched. A value leaks whenever a builder places it somewhere the
# enumeration does not reach. Preparation replaces it as the path the entry
# points use. `_scrub` stays public and identity-preserving for backward
# compatibility; it is simply no longer how `render()` and `compose()` get
# their anonymization.
#
# Preparation is stage 2 of the four-stage contract the entry points run:
# inventory -> prepare -> render -> verify. It rewrites ONLY typed project
# display fields, so a builder that puts a path into `title`, `notes` or
# `totals` is caught by stage 4 and raises rather than being silently
# corrected. That is intended: silent correction hides the builder defect.

ANON_UNKNOWN = "(unknown)"

_PREPARED_ATTR = "_share_prepared_provenance"


class SharePreparationError(Exception):
    """Raised when a snapshot reaches an entry point already prepared.

    A second preparation pass is not idempotent. On the legacy path — where
    `ProjectCell.identity` is None — the alias key of an already-aliased label
    becomes ``("legacy", "project-1")`` and a second pass RENUMBERS by
    re-ranking. In compose it is worse: two distinct raw projects that each
    mapped locally to ``project-1`` collapse into one legacy key before global
    ranking, merging two projects into a single alias.

    An alias-shape assertion is deliberately NOT the discriminator here — a
    real project can legitimately be named ``project-1`` — so preparation
    stamps an explicit provenance marker instead.
    """


@dataclass(frozen=True)
class _PreparedProvenance:
    """What preparation did, recorded for the verification stage.

    `originals` are the project display labels preparation consumed;
    `allowed` are the values it is permitted to have emitted in their place.
    Verification's provenance half checks the emitted values against
    `allowed` rather than searching the document for `originals`, because a
    project legitimately named `cctally` collides with the static branding
    string this module emits and a text search would fail a correctly
    anonymized artifact.
    """
    reveal_projects: bool
    originals: frozenset[str]
    allowed: frozenset[str]


def _is_prepared(snap: ShareSnapshot) -> bool:
    """True when `_prepare` produced this snapshot object."""
    return isinstance(getattr(snap, _PREPARED_ATTR, None), _PreparedProvenance)


def _provenance_of(snap: ShareSnapshot) -> "_PreparedProvenance | None":
    value = getattr(snap, _PREPARED_ATTR, None)
    return value if isinstance(value, _PreparedProvenance) else None


# --- Reveal-mode display labels ---

def _path_segments(path: str) -> list[str]:
    return [seg for seg in path.split("/") if seg]


def disambiguate_basenames(paths: Sequence[str]) -> dict[int, str]:
    """Return ``{index: displayed label}`` for a list of project paths.

    INPUT CONTRACT: `paths` is one entry per DISTINCT project identity.
    Callers holding several rows of the same project must deduplicate first
    and fan the returned label back out themselves. This function is total —
    given two identical paths it cannot tell them apart, so it appends a
    stable ordinal to keep the mapping injective — and that ordinal is wrong
    output for duplicates of one project: it displays one project under
    several names, and on the legacy path (where `ProjectCell.identity` is
    None and the alias key is the label) it also splits that project's cost
    across several alias slots. Every in-tree caller honors the contract:
    `_resolved_project_labels` and `_merged_project_mapping` pass one entry
    per `_ProjectAnonKey`, and `_cctally_share._session_disambiguate_labels`
    deduplicates by path.

    Reveal mode shows a project's basename, never its full path. Bare
    ``os.path.basename`` is NOT sufficient and using it would reintroduce a
    defect the CLI already solved: two ``app`` projects under different
    parents collapse into one indistinguishable label, and after
    anonymization into ONE ``project-N`` alias — losing both privacy
    uniqueness and chart-rank meaning. See the comment at
    ``bin/_cctally_share.py`` above ``_project_disambiguate_labels``'s call
    site, which records that reasoning.

    The algorithm is deterministic: basename; on collision a parent-directory
    suffix ``" (parent)"``; on a repeated parent, progressively more path
    segments; and a stable ordinal as the last resort when the paths
    themselves are indistinguishable. A label with no separator (already a
    basename, or an already-disambiguated ``app (work)`` from the CLI) is its
    own basename and passes through untouched.

    ``(unknown)`` is never suffixed: ``_build_anon_mapping`` protects only the
    exact literal, so a suffixed ``(unknown) (/)`` would be numbered like an
    ordinary project and lose the sentinel's meaning.
    """
    segs = [_path_segments(p or "") for p in paths]
    bases: list[str] = []
    for i, p in enumerate(paths):
        bases.append(segs[i][-1] if segs[i] else ((p or "") or ANON_UNKNOWN))
    labels: dict[int, str] = dict(enumerate(bases))
    max_depth = max((len(s) for s in segs), default=1)
    depth = 1
    while True:
        groups: dict[str, list[int]] = {}
        for idx, lab in labels.items():
            groups.setdefault(lab, []).append(idx)
        collided = [
            idxs for lab, idxs in groups.items()
            if len(idxs) > 1 and lab != ANON_UNKNOWN
        ]
        if not collided:
            return labels
        if depth > max_depth:
            # Indistinguishable inputs (identical paths, or paths that differ
            # only past every segment we can show). Stay total and stay
            # deterministic rather than emitting duplicates.
            for idxs in collided:
                for rank, idx in enumerate(sorted(idxs), 1):
                    labels[idx] = f"{labels[idx]} ({rank})"
            return labels
        for idxs in collided:
            for idx in idxs:
                tail = segs[idx][max(0, len(segs[idx]) - 1 - depth):len(segs[idx]) - 1]
                # `"/"` mirrors the CLI's `os.path.basename(os.path.dirname(p))
                # or "/"` fallback for a path with no parent segment.
                qualifier = "/".join(tail) or "/"
                labels[idx] = f"{bases[idx]} ({qualifier})"
        depth += 1


# --- Inventory ---

@dataclass(frozen=True)
class SensitiveInventory:
    """What verification knows about one render's project provenance.

    `project_labels` is what the raw snapshot carried at its typed project
    display sites, `prepared_labels` what preparation emitted there, and
    `allowed_labels` what preparation was permitted to emit. All three come
    from `_map_project_display`, the SINGLE enumeration of those sites.

    `all_strings` is populated only by `_collect_sensitive_inventory`, which
    is a diagnostic and test helper — NOT part of the render path. It walks
    the whole dataclass/mapping/sequence graph generically, which is useful
    for asking "does this snapshot carry X anywhere", but `_verify_output`
    never reads it, so paying for that walk on every `render()` bought
    nothing.
    """
    project_labels: frozenset[str] = frozenset()
    prepared_labels: frozenset[str] = frozenset()
    allowed_labels: frozenset[str] = frozenset()
    all_strings: frozenset[str] = frozenset()


EMPTY_INVENTORY = SensitiveInventory()


def _walk_strings(value: object, out: set[str], seen: set[int]) -> None:
    if isinstance(value, str):
        if value:
            out.add(value)
        return
    if value is None or isinstance(value, (bool, int, float, bytes, datetime)):
        return
    marker = id(value)
    if marker in seen:
        return
    seen.add(marker)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            _walk_strings(getattr(value, f.name, None), out, seen)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _walk_strings(key, out, seen)
            _walk_strings(item, out, seen)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _walk_strings(item, out, seen)
        return


def _iter_chart_points(chart: "ChartSpec | None"):
    if chart is None:
        return
    yield from chart.points
    if isinstance(chart, LineChart) and chart.multi_series:
        for series in chart.multi_series.values():
            yield from series
    elif isinstance(chart, BarChart) and chart.stacks:
        for series in chart.stacks.values():
            yield from series


def _collect_sensitive_inventory(snap: ShareSnapshot) -> SensitiveInventory:
    """Walk the whole snapshot graph and record every string it carries.

    A DIAGNOSTIC and test helper, deliberately NOT on the render path. The
    generic walk answers "does this snapshot carry this token anywhere",
    which is what a test wants; `_verify_output` reads only the three
    project-provenance sets, so `render()` no longer pays for the walk.
    """
    strings: set[str] = set()
    _walk_strings(snap, strings, set())
    return SensitiveInventory(
        all_strings=frozenset(strings),
        project_labels=frozenset(_project_display_labels(snap)),
    )


# --- Preparation ---

def _resolved_project_labels(
    snap: ShareSnapshot, *, reveal_projects: bool,
) -> dict[_ProjectAnonKey, str]:
    """Map each project identity to the label the document should show.

    Alias keys are collected from the FULL labels first — before any basename
    reduction — so two projects sharing a basename stay two identities.
    """
    costs = _collect_project_identity_costs(snap)
    if not costs:
        return {}
    if not reveal_projects:
        return dict(_build_anon_mapping(costs))
    # Reveal: rank cost-descending (matching the alias ranking) so the
    # disambiguation order is stable, then reduce to displayed basenames.
    #
    # The basename comes from the DISPLAY LABEL, never from the key's second
    # element: for a legacy key those are the same string, but a qualified
    # key's second element is the opaque provider identity, and reducing that
    # would replace the user-facing label with an internal identifier.
    by_key = _project_label_by_key(snap)
    ordered = sorted(costs.items(), key=lambda kv: (-kv[1], kv[0]))
    keys = [key for key, _cost in ordered]
    display = disambiguate_basenames([by_key.get(key, key[1]) for key in keys])
    return {key: display[idx] for idx, key in enumerate(keys)}


def _merged_project_costs(
    snaps: "Sequence[ShareSnapshot]",
) -> dict[_ProjectAnonKey, float]:
    """Accumulate project-identity costs across every section.

    The `+=` matters: overwriting would let the last section's cost decide the
    global rank, so a project that appears twice would be ranked on half its
    spend.
    """
    costs: dict[_ProjectAnonKey, float] = {}
    for snap in snaps:
        for key, cost in _collect_project_identity_costs(snap).items():
            costs[key] = costs.get(key, 0.0) + cost
    return costs


def _merged_project_mapping(
    snaps: "Sequence[ShareSnapshot]", *, reveal_projects: bool,
) -> dict[_ProjectAnonKey, str]:
    """One project display mapping shared by every section of a document.

    Provider qualification is preserved deliberately: the same directory used
    under both Claude and Codex keeps two distinct aliases, which
    `test_equal_labels_with_distinct_qualified_identities_get_distinct_aliases`
    pins as a shipped invariant. Within one provider, one alias means one
    project.

    The map is built BEFORE any basename reduction, so alias keys still derive
    from the full identities.
    """
    costs = _merged_project_costs(snaps)
    if not costs:
        return {}
    if not reveal_projects:
        return dict(_build_anon_mapping(costs))
    by_key: dict[_ProjectAnonKey, str] = {}
    for snap in snaps:
        for key, label in _project_label_by_key(snap).items():
            by_key.setdefault(key, label)
    ordered = sorted(costs.items(), key=lambda kv: (-kv[1], kv[0]))
    keys = [key for key, _cost in ordered]
    display = disambiguate_basenames([by_key.get(key, key[1]) for key in keys])
    return {key: display[idx] for idx, key in enumerate(keys)}


def _merged_anon_mapping(
    sections: "Sequence[ComposedSection]",
) -> dict[_ProjectAnonKey, str]:
    """The anonymize-mode alias namespace for a composed document."""
    return _merged_project_mapping(
        [sec.snap for sec in sections], reveal_projects=False)


# --- The single enumeration of project display sites -----------------------
#
# `_scrub` leaked because it hand-enumerated three field sites, and F1, F2
# and the `x_label` fall-through were three instances of that one shape.
# Replacing it with a second hand-enumeration would only move the shape:
# preparation would rewrite a set of fields, provenance collection would read
# a DIFFERENT set, and the two would drift the first time someone added a
# project display field to only one of them.
#
# So there is exactly ONE enumeration, `_map_project_display`, and every
# consumer derives from it:
#
#   `_apply_project_mapping`  rewrites the sites (preparation)
#   `_project_display_labels` reads them        (provenance, both halves)
#   `_project_label_by_key`   reads the KEYED ones (reveal-mode basenames)
#
# Adding a project display field therefore means editing `_map_project_display`
# and nothing else; a field reachable by preparation but invisible to
# provenance can no longer be constructed.


@dataclass(frozen=True)
class _ProjectDisplaySite:
    """One typed project display value, as seen by the single enumeration.

    `kind` distinguishes the KEYED sites — whose value is a project label and
    therefore mints a `_ProjectAnonKey` — from the DERIVED `chart_x_label`
    site, whose value is composed from another site's resolution and must
    never be used as a key.
    """
    kind: str                       # "cell" | "column" | "chart_project_label"
                                    # | "chart_x_label"
    value: "str | None"             # what is at the site right now
    identity: "str | None" = None   # the project identity governing the site
    prefix: "str | None" = None     # x_label_prefix (chart_x_label only)
    resolved: "str | None" = None   # the label already resolved for this point

    @property
    def keyed(self) -> bool:
        return self.kind != "chart_x_label"


def _map_project_display(
    snap: ShareSnapshot, visit: "Callable[[_ProjectDisplaySite], str | None]",
) -> ShareSnapshot:
    """Walk every typed project display site, replacing each with `visit`.

    THE enumeration. A read-only consumer passes a `visit` that records the
    site and returns its value unchanged; preparation passes one that resolves
    from the alias/basename mapping.

    The sites, in full:

      * `ProjectCell.label` for every `ProjectCell` in every row.
      * `ColumnSpec.label` for every column with `kind == "project"`.
      * `ChartPoint.project_label` for the chart's points, plus
        `LineChart.multi_series` and `BarChart.stacks`.
      * `ChartPoint.x_label` where the axis is project-keyed — derived from
        the point's resolved `project_label` rather than resolved on its own.

    A `"plain"` axis is preserved untouched.
    """
    new_rows: list[Row] = []
    for row in snap.rows:
        new_cells: dict[str, Cell] = {}
        for key, cell in row.cells.items():
            if isinstance(cell, ProjectCell):
                new_cells[key] = ProjectCell(
                    visit(_ProjectDisplaySite(
                        kind="cell", value=cell.label, identity=cell.identity)),
                    rank_cost=cell.rank_cost,
                    identity=cell.identity,
                )
            else:
                new_cells[key] = cell
        new_rows.append(Row(cells=new_cells))

    def _rewrite_pt(p: ChartPoint) -> ChartPoint:
        new_label = (
            visit(_ProjectDisplaySite(
                kind="chart_project_label", value=p.project_label,
                identity=p.project_identity))
            if p.project_label else None
        )
        # Two independent triggers. `x_label_kind` is the authoritative one.
        # String equality is retained only because `_scrub` stays public and
        # a caller may hand it a hand-built point that predates the
        # discriminator; it is subsumed by the marker at every shipped
        # construction site, and it can never widen the leak surface — the
        # worst it can do is anonymize an axis that already displayed the
        # project name.
        is_project_axis = (
            p.x_label_kind == "project"
            or bool(p.project_label and p.x_label == p.project_label)
        )
        new_x = (
            visit(_ProjectDisplaySite(
                kind="chart_x_label", value=p.x_label,
                identity=p.project_identity, prefix=p.x_label_prefix,
                resolved=new_label))
            if is_project_axis else p.x_label
        )
        return ChartPoint(
            x_label=new_x,
            x_value=p.x_value,
            y_value=p.y_value,
            project_label=new_label,
            series_key=p.series_key,
            project_identity=p.project_identity,
            x_label_kind=p.x_label_kind,
            x_label_prefix=p.x_label_prefix,
        )

    new_chart: ChartSpec | None = snap.chart
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

    new_columns: list[ColumnSpec] = []
    for col in snap.columns:
        if col.kind == "project":
            new_columns.append(ColumnSpec(
                key=col.key,
                label=visit(_ProjectDisplaySite(
                    kind="column", value=col.label,
                    identity=col.project_identity)),
                align=col.align, emphasis=col.emphasis, kind=col.kind,
                project_identity=col.project_identity,
            ))
        else:
            new_columns.append(col)

    return dataclasses.replace(
        snap, columns=tuple(new_columns), rows=tuple(new_rows), chart=new_chart,
    )


def _project_display_labels(snap: ShareSnapshot) -> set[str]:
    """The typed project display values present in one snapshot.

    Derived from `_map_project_display`, so it can never fall behind what
    preparation rewrites. The returned snapshot is discarded — the visitor
    returns each value unchanged, so this is a read.
    """
    labels: set[str] = set()

    def _record(site: _ProjectDisplaySite) -> "str | None":
        if site.value:
            labels.add(site.value)
        return site.value

    _map_project_display(snap, _record)
    return labels


def _project_label_by_key(snap: ShareSnapshot) -> dict[_ProjectAnonKey, str]:
    """First-seen display label per project identity key.

    Only the KEYED sites contribute: a project-keyed `x_label` is composed
    from another site's resolution, so keying on it would mint a bogus
    `("legacy", "1 · project-1")` entry.
    """
    out: dict[_ProjectAnonKey, str] = {}

    def _record(site: _ProjectDisplaySite) -> "str | None":
        if site.keyed and site.value:
            out.setdefault(_project_anon_key(site.value, site.identity),
                           site.value)
        return site.value

    _map_project_display(snap, _record)
    return out


def _apply_project_mapping(
    snap: ShareSnapshot, mapping: dict[_ProjectAnonKey, str],
) -> ShareSnapshot:
    """Rewrite every typed project display field from `mapping`.

    Fail closed: a key absent from the mapping resolves to `(unknown)` rather
    than falling through to the original value. The `x_label` arm used to be
    the one exception — it fell OPEN on a mapping miss while its sibling arms
    fell closed — which is the asymmetry this replaces.

    A project-keyed axis composes its `x_label` from the resolved label, with
    the `x_label_prefix` (the cost rank) ahead of it when present.
    """
    def _resolve(site: _ProjectDisplaySite) -> str:
        if site.kind == "chart_x_label":
            base = site.resolved if site.resolved is not None else ANON_UNKNOWN
            return f"{site.prefix} · {base}" if site.prefix else base
        if not site.value:
            return ANON_UNKNOWN
        return mapping.get(_project_anon_key(site.value, site.identity),
                           ANON_UNKNOWN)

    return _map_project_display(snap, _resolve)


def _prepare(
    snap: ShareSnapshot, *, reveal_projects: bool,
    mapping: "dict[_ProjectAnonKey, str] | None" = None,
) -> ShareSnapshot:
    """Resolve every typed project display field and stamp provenance.

    Always returns a NEW object and never marks its input, so the caller's
    snapshot stays renderable more than once. `mapping` lets `compose()`
    supply one merged alias namespace shared across all its sections; when
    omitted the mapping is derived from this snapshot alone.
    """
    if _is_prepared(snap):
        raise SharePreparationError(
            "snapshot is already prepared; entry points must receive raw "
            "snapshots (a second pass renumbers aliases on the legacy path "
            "and merges distinct projects in compose)"
        )
    originals = _project_display_labels(snap)
    resolved = (
        mapping if mapping is not None
        else _resolved_project_labels(snap, reveal_projects=reveal_projects)
    )
    out = _apply_project_mapping(snap, resolved)
    allowed = set(resolved.values())
    allowed.add(ANON_UNKNOWN)
    # A project-keyed axis emits `f"{prefix} · {resolved}"`, so the composed
    # forms belong on the allowlist too. They are derived from the RAW
    # snapshot's prefixes crossed with the mapping's values, never read back
    # off the prepared output, so the check stays a real comparison against
    # provenance rather than a tautology.
    prefixes = {
        point.x_label_prefix
        for point in _iter_chart_points(snap.chart)
        if point.x_label_kind == "project" and point.x_label_prefix
    }
    allowed |= {
        f"{prefix} · {value}" for prefix in prefixes for value in set(allowed)
    }
    object.__setattr__(out, _PREPARED_ATTR, _PreparedProvenance(
        reveal_projects=reveal_projects,
        originals=frozenset(originals),
        allowed=frozenset(allowed),
    ))
    return out


def _inventory_for(
    raw: ShareSnapshot, prepared: ShareSnapshot,
) -> SensitiveInventory:
    """Build the verification inventory from one raw/prepared snapshot pair."""
    return _merge_inventories([(raw, prepared)])


def has_project_identities(snap: ShareSnapshot) -> bool:
    """True when this snapshot carries a project identity the privacy toggle
    can act on (#503 S1 B1).

    Public, because the dashboard's render handler surfaces it so the share
    modal's status line can tell the user what the export will actually
    contain. Some renders produce artifacts that are byte-identical in both
    privacy modes apart from the `anonymized:` frontmatter line. Telling that
    user "Export will show real project names" is a false statement there, and
    a warning learned to be false on Forecast is one a user may disregard on
    Projects.

    WHICH renders those are is not a property of the code and is deliberately
    not enumerated anywhere. It is a property of the snapshot actually built:
    the same template carries project names over one store and none over
    another, and the split runs WITHIN a panel as well as between panels, so
    neither a panel list nor a template list can be right. Three independent
    counts taken during this session disagreed for exactly that reason — each
    was measured against a different dataset. Do not restore a number here.

    Derived from `_project_display_labels`, hence from `_map_project_display`,
    the single enumeration of typed project display sites. A panel list would
    be a second source of truth and would be wrong at template granularity.

    `(unknown)` does not count: it renders identically in both modes, so a
    snapshot carrying only it has nothing the toggle can change.
    """
    return bool(_project_display_labels(snap) - {ANON_UNKNOWN})


def _merge_inventories(
    pairs: "Sequence[tuple[ShareSnapshot, ShareSnapshot]]",
) -> SensitiveInventory:
    """Fold the project provenance of every raw/prepared section pair.

    Deliberately does NOT populate `all_strings`: `_verify_output` never
    reads it, so walking the whole snapshot graph here made every `render()`
    pay for a value nothing consumed. `_collect_sensitive_inventory` still
    offers that walk as a diagnostic.
    """
    originals: set[str] = set()
    prepared_labels: set[str] = set()
    allowed: set[str] = set()
    for raw, prepared in pairs:
        originals |= _project_display_labels(raw)
        prepared_labels |= _project_display_labels(prepared)
        prov = _provenance_of(prepared)
        if prov is not None:
            allowed |= set(prov.allowed)
    return SensitiveInventory(
        project_labels=frozenset(originals),
        prepared_labels=frozenset(prepared_labels),
        allowed_labels=frozenset(allowed),
    )


# --- Verification: the forbidden-class detector (#503 S1) ---
#
# Stage 4 of the contract. DETECTION ONLY — a finding raises
# `SharePrivacyViolation` and the render fails. It never redacts and
# continues, because a redact-and-continue gate hides the builder defect that
# put the identifier in the document, and the operator's decision is that a
# share artifact which cannot be produced safely is not produced.
#
# Verification has TWO DISJOINT HALVES, and the split is load-bearing.
#
# Half one — provenance-checked fields. Preparation knows which fields it
# rewrote and what it was allowed to write there, so verification compares the
# emitted values against that allowlist. It never searches the document for
# original project labels.
#
# Half two — unambiguous classes, scanned document-wide. Only identifier
# classes that cannot plausibly occur as legitimate artifact content.
#
# Original project labels are deliberately NOT in half two. A project
# legitimately named `cctally` collides with the static branding string this
# module emits, so a whole-document rejected-token set would fail a correctly
# anonymized artifact; `daily` and `svg` collide with ordinary chrome and
# markup the same way. Under the fail-the-render decision that is an outage,
# not a nuisance, so half one covers the real risk without the collision.


class SharePrivacyViolation(Exception):
    """A forbidden identifier class was found in a rendered share artifact.

    `classes` names the finding classes and NOTHING else — the message carries
    the matched value, this attribute deliberately does not. The two audiences
    differ: a user staring at a several-hundred-row artifact cannot act on
    "canonical UUID" alone, so the message names the value; a log line has no
    such need, and a dashboard log is a plausible thing to paste into a bug
    report (#503 S1 R10).

    The default is the fail-closed sentinel rather than an empty tuple: a
    caller that redacts by reading `classes` treats a falsy value as "nothing
    to redact by" and falls back to the full `repr`, so a raise site that
    forgot the keyword would put the matched value into the log — the exact
    outcome this attribute exists to prevent, and silently. With the sentinel
    the redaction is uninformative instead of unsafe. The structural tripwire
    `test_every_privacy_raise_site_names_its_classes` keeps raise sites from
    relying on it.
    """

    UNCLASSIFIED = "unclassified privacy violation"

    def __init__(
        self, message: str, *, classes: "Sequence[str]" = (UNCLASSIFIED,),
    ) -> None:
        super().__init__(message)
        self.classes: tuple[str, ...] = tuple(classes) or (self.UNCLASSIFIED,)


# Canonical UUID with exact 8-4-4-4-12 hex grouping. Claude session ids are
# canonical UUIDs, which is what F1 disclosed through the sessions charts.
_UUID_RE = re.compile(
    r"(?<![0-9A-Fa-f-])"
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f-])"
)

# A URI with a scheme and an authority. Stripped BEFORE the absolute-path scan
# so its path component cannot be read as a filesystem path — the shipped
# branded goldens carry `https://github.com/omrikais/cctally` and
# `http://www.w3.org/2000/svg`, and a naive path predicate matches both.
_URI_RE = re.compile(r"""[A-Za-z][A-Za-z0-9+.\-]*://[^\s"'<>)\]]*""")

# An absolute POSIX path of at least two segments. The lookbehind is the
# exclusion rule: a `/` preceded by a word character, `:`, `/`, `<` or an
# attribute quote is a URI or markup component, not a path start. That covers
# the bare-host form `github.com/omrikais/cctally` (preceded by `m`), the
# scheme-relative form (preceded by `/`), and closing / self-closing tags
# (preceded by `<` or forming a single segment).
_ABS_PATH_RE = re.compile(
    r"""(?<![A-Za-z0-9._\-:/<"'])(?:/[A-Za-z0-9._~+\-]+){2,}"""
)

# `~/`, `~user/`, `$HOME/`, `${HOME}/` home expansions. The trailing separator
# is required so the `~` prefix `blocks` uses for a heuristically-anchored row
# is not a finding.
_HOME_EXPANSION_RE = re.compile(
    r"""(?<![A-Za-z0-9._\-])(?:~[A-Za-z0-9._\-]*|\$\{?HOME\}?)/[A-Za-z0-9._~+\-]"""
)

_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9.\-])"
)

# `source_root_key` / `codex_file_key` are 32-character lowercase hex. The
# lookarounds keep this from matching inside a 64-character sha256 digest.
_SOURCE_ROOT_KEY_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9a-f]{32}(?![0-9A-Fa-f])")

# The canonical logical-limit JSON object `_lib_jsonl._codex_logical_limit_key`
# emits. Matched on the co-occurrence of its identity members rather than on
# the whole literal, so a member added later still trips it.
_LOGICAL_LIMIT_MEMBERS = ('"observedSlot"', '"windowMinutes"', '"sourceRootKey"')

_V1_IDENTITY_RE = re.compile(r"(?<![A-Za-z0-9._\-])v1\.[A-Za-z0-9_\-]{16,}")
_V1_IDENTITY_MEMBERS = frozenset(
    {"nativeKey", "resourceKind", "source", "version"})

# High-precision credential shapes. These mirror
# `_lib_conversation_anon.SECRET_PATTERNS`, deliberately COPIED rather than
# imported: that module's guarantee explicitly excludes emails, session ids and
# unknown identities, so reusing it here would import a weaker contract than
# this gate promises. They are used as DETECTION, never as redaction.
#
# LEFT BOUNDARY, and why it is mandatory HERE specifically. The source
# patterns carry no left anchor, so `sk-[A-Za-z0-9_\-]{20,}` matches inside
# any word ending in `sk` — `flask-restful-api-server-example`,
# `risk-analysis-toolkit-2026`, `desk-booking-service-frontend` — and
# `sk-ant-[…]` matches inside `flask-ant-design-theme-kit`. In the source
# module an unanchored match only OVER-REDACTS. Here detection FAILS the
# render, so the same regex is a shipping outage: the user cannot rename
# their repository in order to share a report. `_CRED_LEFT` requires the
# match to start at a non-word position, which is where a real credential
# always starts (after whitespace, a quote, `=`, `:` or the string start).
#
# WHAT THE ANCHOR GIVES UP. `_CRED_LEFT` excludes `-` and `_` from the
# preceding position as well as alphanumerics, so a credential glued to a
# word character on its left — `-sk-ant-api03-…` in a flag-like string, or
# `KEY_sk-ant-…` — is no longer detected. That narrowing is deliberate and is
# the price of the anchor: every realistic embedding of a real credential in a
# rendered artifact (after a space, a quote, `=`, `:`, `/`, or at the start of
# the string) still fires, while the ordinary-repository-name false positives
# above, which under the fail-the-render decision leave the user no recourse,
# do not.
_CRED_LEFT = r"(?<![A-Za-z0-9_\-])"
_CREDENTIAL_RES = (
    ("authorization-header", re.compile(r"\bAuthorization:[ \t]*\S", re.I)),
    ("bearer-token", re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{16,}", re.I)),
    ("anthropic-key", re.compile(_CRED_LEFT + r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("generic-sk-key", re.compile(_CRED_LEFT + r"sk-[A-Za-z0-9_\-]{20,}")),
    ("github-token", re.compile(
        _CRED_LEFT + r"(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}")),
    ("aws-access-key", re.compile(_CRED_LEFT + r"AKIA[0-9A-Z]{16}")),
    ("slack-token", re.compile(_CRED_LEFT + r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("secret-assignment", re.compile(
        r"\b(?:api[_-]?key|secret|passwd|password)\b[ \t]*[=:][ \t]*"
        r"""(?:"[^"\r\n]{6,}"|'[^'\r\n]{6,}'|[^\s"']{6,})""", re.I)),
)

_NUMERIC_ENTITY_RE = re.compile(r"&#(x[0-9A-Fa-f]+|[0-9]+);")
_MD_BACKSLASH_RE = re.compile(r"\\([\\|*_`\[\]])")


def _decode_entities(text: str) -> str:
    """Undo the XML/HTML escaping the renderers apply."""
    def _numeric(m: "re.Match[str]") -> str:
        token = m.group(1)
        try:
            code = int(token[1:], 16) if token[0] in "xX" else int(token)
        except ValueError:
            return m.group(0)
        return chr(code) if 0 < code < 0x110000 else m.group(0)

    out = _NUMERIC_ENTITY_RE.sub(_numeric, text)
    for entity, char in (("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&amp;", "&")):
        out = out.replace(entity, char)
    return out


def _scan_variants(text: str) -> tuple[str, ...]:
    """The raw document plus its decoded forms.

    Entity encoding and markdown backslash escaping both split a token across
    characters the scanners would otherwise not join up, so scanning the raw
    bytes alone would let an encoded identifier through.
    """
    decoded = _decode_entities(text)
    unescaped = _MD_BACKSLASH_RE.sub(r"\1", decoded)
    variants = [text]
    for candidate in (decoded, unescaped):
        if candidate not in variants:
            variants.append(candidate)
    return tuple(variants)


def _looks_like_identity_key(token: str) -> bool:
    """True when a `v1.` token base64url-decodes to a canonical IdentityV1."""
    body = token[3:]
    padded = body + "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return False
    return isinstance(payload, dict) and _V1_IDENTITY_MEMBERS <= set(payload)


# How much of an offending value the refusal message quotes.
#
# The message reaches a terminal (`cctally: refused to write a share artifact
# — …`), so an unbounded value would wrap the refusal off screen. Long enough
# that a user recognizes which of their directories or identifiers tripped the
# gate, which is the whole reason the value is named at all.
_FINDING_SAMPLE_MAX = 48


def _finding_sample(value: str) -> str:
    """A single-line, length-bounded form of a matched value."""
    collapsed = " ".join(value.split())
    if len(collapsed) <= _FINDING_SAMPLE_MAX:
        return collapsed
    return collapsed[:_FINDING_SAMPLE_MAX] + "…"


# The run of characters a path-shaped token may continue with. Used ONLY to
# widen a match for display: `_HOME_EXPANSION_RE` requires a single character
# after the separator, which is the right anchor for detection but reports
# `~/w` as the offending value where the user needs to see `~/work/app`.
# Widening here rather than in the pattern keeps detection semantics fixed.
_PATHY_TAIL_RE = re.compile(r"[A-Za-z0-9._~+\-/]*")


def _matched_with_pathy_tail(text: str, match: "re.Match[str]") -> str:
    tail = _PATHY_TAIL_RE.match(text, match.end())
    return text[match.start():tail.end()]


def _scan_forbidden_classes(text: str) -> "list[tuple[str, str]]":
    """Return `(class label, matched value)` per unambiguous class in `text`.

    The matched value is carried out of the scan, not just the class name.
    Naming only the class leaves the user nothing to act on: the accepted
    limitation in `docs/share-gotchas.md` is that a project whose basename is
    itself a canonical UUID or a 32-character hex token cannot be rendered in
    reveal mode, and "canonical UUID" alone does not tell that user which of
    their directories to rename.
    """
    findings: list[tuple[str, str]] = []
    match = _UUID_RE.search(text)
    if match:
        findings.append(("canonical UUID", match.group(0)))
    for token in _V1_IDENTITY_RE.findall(text):
        if _looks_like_identity_key(token):
            findings.append(("v1. identity key", token))
            break
    without_uris = _URI_RE.sub(" ", text)
    match = _ABS_PATH_RE.search(without_uris)
    if match:
        findings.append(("absolute path", match.group(0)))
    match = _HOME_EXPANSION_RE.search(text)
    if match:
        findings.append(
            ("home-directory expansion", _matched_with_pathy_tail(text, match)))
    match = _EMAIL_RE.search(text)
    if match:
        findings.append(("email address", match.group(0)))
    match = _SOURCE_ROOT_KEY_RE.search(text)
    if match:
        findings.append(("source-root key", match.group(0)))
    if all(member in text for member in _LOGICAL_LIMIT_MEMBERS):
        # No single regex match to quote; the members are what identify it.
        first = min(_LOGICAL_LIMIT_MEMBERS, key=text.index)
        findings.append(("logical-limit identity", first))
    for name, pattern in _CREDENTIAL_RES:
        match = pattern.search(text)
        if match:
            findings.append((f"credential ({name})", match.group(0)))
    return findings


def _describe_findings(findings: "Sequence[tuple[str, str]]") -> str:
    """Render `label (value)` per class, deduplicated, in a stable order."""
    seen: dict[str, str] = {}
    for label, value in findings:
        seen.setdefault(label, value)
    parts = [
        f"{label} ({_finding_sample(seen[label])})" for label in sorted(seen)
    ]
    if len(parts) > 5:
        return ", ".join(parts[:5]) + f", and {len(parts) - 5} more"
    return ", ".join(parts)


def _verify_output(
    text: str, *, inventory: SensitiveInventory,
) -> None:
    """Raise `SharePrivacyViolation` when the rendered document is unsafe.

    Detection only. Callers must let the exception propagate: the dashboard
    handler's exception converter turns it into the generic 500 envelope and
    the CLI converts it to a stderr refusal and exit 3.

    Takes NO privacy mode. Both halves are mode-independent — half one
    compares against the allowlist preparation itself built under whichever
    mode was asked for, and half two's classes are forbidden in reveal mode
    too, because reveal discloses a project's basename and never its path.
    The parameter existed and was read by nothing.
    """
    # Half one — provenance. Every project display value the prepared snapshot
    # carries must be one preparation was allowed to write. This catches a
    # preparation miss without a text search, so a project whose name happens
    # to be a common word is checked correctly.
    #
    # A CONSTRUCTION INVARIANT, not a runtime check that can fire in the real
    # pipeline: `_apply_project_mapping` writes every site from
    # `mapping.get(key, ANON_UNKNOWN)` and composes the axis label from the
    # same values, and `_prepare` builds `allowed` from that mapping plus the
    # raw prefixes, so the difference is empty by construction for every
    # in-tree input. It fires for a snapshot prepared outside `_prepare`, and
    # it is deliberately NOT strengthened into a document-wide search for
    # original project labels: a project named `cctally` collides with this
    # module's own branding string, and under the fail-the-render decision
    # that collision is an outage.
    if inventory.allowed_labels or inventory.prepared_labels:
        escaped = inventory.prepared_labels - inventory.allowed_labels
        if escaped:
            raise SharePrivacyViolation(
                "project display fields escaped preparation: "
                + ", ".join(sorted(escaped)[:5]),
                # The escaped values ARE project labels, so they stay out of
                # `classes` for the same reason a matched value does.
                classes=("project display fields escaped preparation",),
            )

    # Half two — unambiguous classes, document-wide, in both privacy modes.
    for variant in _scan_variants(text):
        findings = _scan_forbidden_classes(variant)
        if findings:
            raise SharePrivacyViolation(
                "share artifact would disclose: " + _describe_findings(findings),
                classes=sorted({label for label, _value in findings}),
            )


def _encode_probe_identity_key() -> str:
    """A syntactically valid IdentityV1 key, for the detector's own tests.

    Kept next to the decoder so the probe cannot drift away from the shape the
    detector recognizes.
    """
    payload = {
        "nativeKey": "probe",
        "parentKey": None,
        "resourceKind": "conversation",
        "source": "codex",
        "sourceRootKey": None,
        "version": 1,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "v1." + base64.urlsafe_b64encode(canonical).decode("ascii").rstrip("=")


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
    source_label, availability = _source_chrome(snap)
    if source_label:
        parts.extend(["", f"**{_md_escape(source_label)}**"])
    if availability:
        parts.extend(["", _md_escape(availability)])
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

# --- SVG table geometry (issue #38) ---
_SVG_TABLE_FONT = 11
_SVG_TABLE_CELL_PAD_X = 8
_SVG_TABLE_CELL_PAD_Y = 6
_SVG_TABLE_LINE_H_MULT = 1.4
_SVG_TABLE_GAP = 16
_SVG_TABLE_MAX_WRAP_LINES = 3
_SVG_TABLE_MIN_COL_W = 24
_SVG_AVG_GLYPH_WIDTH_FRACTION = 0.6
_SVG_WRAP_BREAK_CHARS = (" ", "/", "-", "_")
_SVG_ELLIPSIS = "…"
_SVG_NOTE_FONT = 11
_SVG_NOTE_LINE_H = 16
_SVG_NOTE_GAP = 14


def _svg_text_width(text: str, font_size: float) -> float:
    """Estimate rendered width of `text` at `font_size` in a sans-serif font.

    Heuristic-only: actual width depends on the UA-selected font. SVG
    goldens are diffed as source text, so this function's job is
    determinism, not pixel-perfect measurement. Wrap-then-ellipsize
    layout tolerates moderate over/under-allocation.
    """
    return len(text) * font_size * _SVG_AVG_GLYPH_WIDTH_FRACTION


def _wrap_for_width(text: str, content_w: float, font_size: float) -> list[str]:
    """Wrap `text` into lines that each fit within `content_w` pixels.

    Greedy left-to-right: binary-search the longest prefix that fits,
    then cut at the rightmost break-char inside the run. Cap output
    at `_SVG_TABLE_MAX_WRAP_LINES`. If the input still has tail after
    the cap, ellipsize the last emitted line until ellipsis fits.
    Empty text → [""]. Unbreakable token longer than `content_w` →
    hard-cut + ellipsis on the tail.
    """
    if not text:
        return [""]
    if _svg_text_width(text, font_size) <= content_w:
        return [text]

    lines: list[str] = []
    remaining = text
    while remaining and len(lines) < _SVG_TABLE_MAX_WRAP_LINES:
        lo, hi = 0, len(remaining)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _svg_text_width(remaining[:mid], font_size) <= content_w:
                lo = mid
            else:
                hi = mid - 1
        fit_end = lo

        if fit_end == len(remaining):
            lines.append(remaining)
            remaining = ""
            break

        if fit_end == 0:
            # Even one character overflows — abort wrap, fall through to ellipsis.
            break

        break_at = -1
        for ch in _SVG_WRAP_BREAK_CHARS:
            idx = remaining.rfind(ch, 0, fit_end + 1)
            if idx > break_at:
                break_at = idx

        if break_at <= 0:
            lines.append(remaining[:fit_end])
            remaining = remaining[fit_end:]
        else:
            lines.append(remaining[:break_at + 1].rstrip())
            remaining = remaining[break_at + 1:]

    if remaining:
        last = lines[-1] if lines else ""
        while last and _svg_text_width(last + _SVG_ELLIPSIS, font_size) > content_w:
            last = last[:-1]
        if last:
            lines[-1] = last + _SVG_ELLIPSIS
        elif lines:
            lines[-1] = _SVG_ELLIPSIS
        else:
            lines.append(_SVG_ELLIPSIS)

    return lines or [_SVG_ELLIPSIS]


def _render_svg_notes(
    snap: "ShareSnapshot", *, palette: dict, x: float, y: float, width: float,
) -> tuple[str, float]:
    """Render deterministic, wrapped artifact notes and return body + height."""
    lines: list[str] = []
    for note in snap.notes:
        lines.extend(_wrap_for_width(note, width, _SVG_NOTE_FONT))
    if not lines:
        return "", 0.0
    body = svg_group([
        svg_text(
            x, y + ((idx + 1) * _SVG_NOTE_LINE_H), line,
            font_size=_SVG_NOTE_FONT, fill=palette["ref_warn"],
        )
        for idx, line in enumerate(lines)
    ])
    return body, float(len(lines) * _SVG_NOTE_LINE_H)


def _svg_table_anchor_and_x(align: str, col_x: float, col_w: float,
                             pad_x: float) -> tuple[str, float]:
    """Map `ColumnSpec.align` to SVG text-anchor + x-coordinate."""
    if align == "right":
        return "end", col_x + col_w - pad_x
    if align == "center":
        return "middle", col_x + col_w / 2
    return "start", col_x + pad_x


def _render_svg_table(
    snap: "ShareSnapshot", *, palette: dict,
    x: float, y: float, max_width: float,
) -> tuple[str, float, float]:
    """Render the cross-tab / project / sessions table body as SVG.

    Returns (svg_fragment, total_height, used_width). Caller
    (`_render_svg`) uses the returned height to position the footer
    band, and `used_width` to size the outer canvas — at pathological
    `top_n` the min-width clamp can push `sum(widths)` past
    `max_width`, in which case the outer SVG expands so columns are
    never clipped (issue #38 follow-up: Codex P2 review on PR #40).
    Caller MUST short-circuit when `snap.columns` is empty — this
    helper is a precondition violation
    if called with `columns=()`.

    Layout: greedy auto-size, shrink only oversized columns, clamp
    to `_SVG_TABLE_MIN_COL_W` minimum, wrap headers AND body cells
    with the same break-priority rule. Visual: Treatment A (HTML-
    mirror) — header band + alternating row stripes + body text;
    no borders or inter-column rules.
    """
    cols = snap.columns
    rows = snap.rows
    n = len(cols)
    assert n > 0, "_render_svg_table: precondition snap.columns non-empty"

    font_size = _SVG_TABLE_FONT
    pad_x = _SVG_TABLE_CELL_PAD_X
    pad_y = _SVG_TABLE_CELL_PAD_Y
    line_h = font_size * _SVG_TABLE_LINE_H_MULT

    # 1. Pre-format every cell to plain text.
    cell_strs = [
        [_render_cell_text(row.cells.get(c.key, TextCell("")))
         for c in cols]
        for row in rows
    ]

    # 2. Natural per-column width = max(header, max body) + 2*pad_x.
    def _col_natural(i: int, c: "ColumnSpec") -> float:
        widths = [_svg_text_width(c.label, font_size)]
        for r in range(len(rows)):
            widths.append(_svg_text_width(cell_strs[r][i], font_size))
        return max(widths) + 2 * pad_x

    nat_w = [_col_natural(i, c) for i, c in enumerate(cols)]

    # 3+4. Fit or shrink.
    if sum(nat_w) <= max_width:
        widths = list(nat_w)
    else:
        fair = max_width / n
        oversize_idx = [i for i in range(n) if nat_w[i] > fair]
        oversize_set = set(oversize_idx)
        if not oversize_idx:
            scale = max_width / sum(nat_w)
            widths = [w * scale for w in nat_w]
        else:
            other_total = sum(nat_w[i] for i in range(n) if i not in oversize_set)
            total_oversize = sum(nat_w[i] for i in oversize_idx)
            budget = max_width - other_total
            scale = budget / total_oversize if total_oversize > 0 else 1.0
            widths = [
                (nat_w[i] * scale) if i in oversize_set else nat_w[i]
                for i in range(n)
            ]
        # 4b. Min-width clamp (pathological top_n).
        widths = [max(w, _SVG_TABLE_MIN_COL_W) for w in widths]

    # 5a. Header wrap.
    header_lines: list[list[str]] = []
    for i, c in enumerate(cols):
        content_w = widths[i] - 2 * pad_x
        header_lines.append(_wrap_for_width(c.label, content_w, font_size))
    max_header_lines = max((len(ls) for ls in header_lines), default=1)

    # 5b. Body wrap.
    wrapped_body: list[list[list[str]]] = []
    row_lines: list[int] = []
    for r in range(len(rows)):
        cells_wrapped: list[list[str]] = []
        mx = 1
        for i in range(n):
            content_w = widths[i] - 2 * pad_x
            ls = _wrap_for_width(cell_strs[r][i], content_w, font_size)
            cells_wrapped.append(ls)
            mx = max(mx, len(ls))
        wrapped_body.append(cells_wrapped)
        row_lines.append(mx)

    # 6. Heights.
    header_h = max_header_lines * line_h + 2 * pad_y
    body_heights = [nl * line_h + 2 * pad_y for nl in row_lines]
    total_h = header_h + sum(body_heights)

    # 7. Emit. Column x-offsets are cumulative.
    col_xs = [x]
    for w in widths[:-1]:
        col_xs.append(col_xs[-1] + w)

    # Band rects span the full rendered width — when min-col-w clamp
    # pushes sum(widths) past max_width, both the header band and the
    # alternating row stripes extend with the columns so right-side
    # cells sit on the band color, not on the outer SVG background.
    used_width = sum(widths)
    band_width = max(max_width, used_width)

    pieces: list[str] = []

    # Header band.
    pieces.append(svg_rect(x, y, band_width, header_h,
                           fill=palette["table_header_bg"]))
    # Header text.
    for i, c in enumerate(cols):
        cx = col_xs[i]
        cw = widths[i]
        anchor, tx = _svg_table_anchor_and_x(c.align, cx, cw, pad_x)
        for j, line in enumerate(header_lines[i]):
            baseline = y + pad_y + font_size + j * line_h
            pieces.append(svg_text(
                tx, baseline, line,
                font_size=font_size, fill=palette["fg"],
                anchor=anchor, weight="bold", font_family="sans-serif",
            ))

    # Body rows.
    row_y = y + header_h
    for r, _ in enumerate(rows):
        rh = body_heights[r]
        row_bg = palette["table_row_alt"] if (r % 2 == 1) else palette["bg"]
        pieces.append(svg_rect(x, row_y, band_width, rh, fill=row_bg))

        for i, c in enumerate(cols):
            cx = col_xs[i]
            cw = widths[i]
            anchor, tx = _svg_table_anchor_and_x(c.align, cx, cw, pad_x)
            for j, line in enumerate(wrapped_body[r][i]):
                baseline = row_y + pad_y + font_size + j * line_h
                pieces.append(svg_text(
                    tx, baseline, line,
                    font_size=font_size, fill=palette["fg"],
                    anchor=anchor, font_family="sans-serif",
                ))
        row_y += rh

    return "".join(pieces), total_h, used_width


def _render_svg(snap: ShareSnapshot, *, palette: dict,
                branding: bool,
                include_chrome: bool = True,
                include_table: bool = True) -> str:
    """Render snapshot to SVG.

    include_chrome=True  → standalone SVG with title/subtitle/timestamp/footer.
    include_chrome=False → chart(+optional table)-only (HTML wrapper consumes this).
    include_table=True   → emit `_render_svg_table` body when snap.columns
                           is non-empty. HTML wrapper passes False so the
                           chart-slot embed stays table-free (HTML <table>
                           is rendered separately as a sibling element).
    """
    has_table = include_table and bool(snap.columns)
    chart_h = _SVG_CHART_H if snap.chart is not None else 0
    header_h = _svg_header_height(snap, include_chrome=include_chrome)

    # Pre-layout the table (we need its height before declaring outer SVG height).
    if has_table:
        table_y = _SVG_PADDING + header_h + chart_h + _SVG_TABLE_GAP
        table_svg, table_h, table_w = _render_svg_table(
            snap, palette=palette, x=_SVG_PADDING, y=table_y, max_width=_SVG_WIDTH,
        )
    else:
        table_svg, table_h, table_w = "", 0.0, 0.0

    # Canvas grows when the table's used width exceeds _SVG_WIDTH — happens
    # only when the min-col-w clamp fired (pathological top_n). Header /
    # chart / footer keep their original positioning anchored at _SVG_WIDTH;
    # the canvas just extends to the right so wide tables aren't clipped at
    # the viewBox. Issue #38 follow-up (Codex PR #40 P2).
    content_w = max(_SVG_WIDTH, table_w)

    table_block_h = (_SVG_TABLE_GAP + table_h) if has_table else 0
    note_block_h = 0.0
    note_svg = ""
    if include_chrome and snap.notes:
        note_y = (
            _SVG_PADDING + header_h + chart_h + table_block_h + _SVG_NOTE_GAP
        )
        note_svg, note_h = _render_svg_notes(
            snap,
            palette=palette,
            x=_SVG_PADDING,
            y=note_y,
            width=_SVG_WIDTH,
        )
        note_block_h = _SVG_NOTE_GAP + note_h

    if include_chrome:
        height = (
            header_h + chart_h + table_block_h + note_block_h
            + _SVG_FOOTER_H + (_SVG_PADDING * 2)
        )
    else:
        height = chart_h + table_block_h + (_SVG_PADDING * 2)

    pieces: list[str] = []

    if include_chrome:
        pieces.append(_render_svg_header(
            snap, palette=palette,
            x=_SVG_PADDING, y=_SVG_PADDING, width=_SVG_WIDTH,
        ))

    chart_y = _SVG_PADDING + header_h
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

    if has_table:
        pieces.append(table_svg)

    if note_svg:
        pieces.append(note_svg)

    if include_chrome:
        footer_y = (
            _SVG_PADDING + header_h + chart_h + table_block_h + note_block_h
            + _SVG_FOOTER_BASELINE
        )
        pieces.append(_render_svg_footer(
            snap, palette=palette,
            x=_SVG_PADDING, y=footer_y,
            width=_SVG_WIDTH, branding=branding,
        ))

    total_w = content_w + (_SVG_PADDING * 2)
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
        f'<div style="margin-top:12px">{_render_svg(snap, palette=palette, branding=False, include_chrome=False, include_table=False)}</div>'
        if snap.chart is not None else ""
    )
    title_html = f'<h1 style="font-size:20px;color:{palette["fg"]};margin:0">{_xml_escape(snap.title)}</h1>'
    source_label, availability = _source_chrome(snap)
    source_html = (
        f'<div style="font-size:13px;color:{palette["muted"]};margin-top:4px">{_xml_escape(source_label)}</div>'
        if source_label else ""
    )
    availability_html = (
        f'<div style="font-size:13px;color:{palette["fg"]};margin-top:4px">{_xml_escape(availability)}</div>'
        if availability else ""
    )
    subtitle_html = (
        f'<div style="font-size:13px;color:{palette["muted"]};margin-top:4px">{_xml_escape(snap.subtitle)}</div>'
        if snap.subtitle else ""
    )
    timestamp_html = (
        f'<div style="font-size:11px;color:{palette["muted"]};margin-top:4px">'
        f'{_format_generated_at_iso(snap.generated_at)}</div>'
    )
    table_html = _render_html_table(snap, palette) if snap.columns else ""
    notes_html = (
        f'<aside aria-label="Data notes" '
        f'style="margin-top:14px;padding:8px 10px;border-left:3px solid {palette["ref_warn"]};'
        f'color:{palette["fg"]};font-size:13px">'
        + "".join(
            f'<div>{_xml_escape(note)}</div>' for note in snap.notes
        )
        + "</aside>"
        if snap.notes else ""
    )
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
        f'<header>{title_html}{source_html}{availability_html}{subtitle_html}{timestamp_html}</header>'
        f'{chart_html}'
        f'{table_html}'
        f'{notes_html}'
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

    `anonymized` reports the MODE the document was rendered in, read off the
    provenance marker preparation stamped: `not reveal_projects`. It used to
    be INFERRED by regex-matching `project-\\d+` over the labels, which was
    wrong three ways — it never inspected the chart, so `sessions-visual`
    stamped `false` onto a demonstrably scrubbed snapshot; a real project
    named `project-1` reported as anonymized; and a silently failed scrub
    producing conforming labels was indistinguishable from a successful one.

    An UNPREPARED snapshot never went through the privacy contract, so the
    document cannot claim anonymization and reports `false`. That reaches
    only the `_render_md` back-compat shim; `render()` always prepares.
    """
    period = snap.period
    period_iso = (
        f"{_format_generated_at_iso(period.start)}.."
        f"{_format_generated_at_iso(period.end)}"
    )
    prov = _provenance_of(snap)
    anonymized = "true" if (prov is not None and not prov.reveal_projects) else "false"
    lines = [
        "---",
        f"title: {_yaml_scalar(snap.title)}",
        f"generated_at: {_format_generated_at_iso(snap.generated_at)}",
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

    The second complete-document boundary that owns the privacy contract
    (#503 S1). `sections` must carry RAW snapshots: `compose()` prepares them
    itself under `opts.reveal_projects`, stitches, and then verifies the whole
    composed document. Callers must not pre-scrub — a pre-scrubbed section
    reaching a second aliasing pass merges two distinct projects that each
    mapped locally to `project-1` into one alias.
    """
    if not sections:
        raise ValueError("compose requires at least one section")
    fmt = opts.format
    # ONE alias namespace for the whole document (#503 S1 F4). The merged
    # mapping is built here in the kernel rather than in the handler, because
    # a handler-only fix would miss the CLI `source=all` path.
    merged = _merged_project_mapping(
        [sec.snap for sec in sections], reveal_projects=opts.reveal_projects)
    prepared = tuple(
        ComposedSection(
            snap=_prepare(sec.snap, reveal_projects=opts.reveal_projects,
                          mapping=merged),
            drift_detected=sec.drift_detected,
        )
        for sec in sections
    )
    if fmt == "html":
        body = _stitch_html(prepared, opts=opts)
    elif fmt == "md":
        body = _stitch_md(prepared, opts=opts)
    elif fmt == "svg":
        body = _stitch_svg(prepared, opts=opts)
    else:
        raise ValueError(f"unknown format: {fmt!r}")
    _verify_output(body, inventory=_merge_inventories([
        (raw.snap, out.snap) for raw, out in zip(sections, prepared)
    ]))
    return body


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
        # #503 S1: the composite frontmatter reports the composite MODE.
        # `compose()` re-renders every section with `opts.reveal_projects`
        # and discards each section's add-time value, so the composite flag
        # is the whole truth about what the document contains.
        anon_field = "false" if opts.reveal_projects else "true"
        parts.append(
            "---\n"
            f"title: {_yaml_scalar(opts.title)}\n"
            f"generated_at: {_format_generated_at_iso(first_snap.generated_at)}\n"
            f"period: {_format_generated_at_iso(earliest)}.."
            f"{_format_generated_at_iso(latest)}\n"
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

def render(snap: ShareSnapshot, *, format: str, theme: str, branding: bool,
           reveal_projects: bool) -> str:
    """Render a snapshot to the requested format.

    Pure function: no I/O, no DB, no filesystem, no locks. Caller is
    responsible for emitting the result (stdout/file/clipboard/open).

    One of the two complete-document boundaries that own the privacy contract
    (#503 S1). It runs inventory -> prepare -> render -> verify. The gate goes
    here and in `compose()`, not in `_render_fragment` and not in
    `_wrap_document`, because composition bypasses the latter and a fragment
    is not a complete document.

    `reveal_projects` is a REQUIRED keyword with no default. Three shipped
    sites had defaulted it open, and fixing three defaults leaves nothing
    stopping a fourth; a wrong default cannot exist where there is no
    default, so a caller that omits it raises `TypeError` at its own call
    site rather than silently revealing.

    `snap` must be RAW. Passing an already-scrubbed or already-prepared
    snapshot renumbers aliases on the legacy path, so preparation refuses it.
    """
    inventory_source = snap
    prepared = _prepare(snap, reveal_projects=reveal_projects)
    out = _render_prepared(prepared, format=format, theme=theme,
                           branding=branding)
    _verify_output(out, inventory=_inventory_for(inventory_source, prepared))
    return out


def _render_prepared(snap: ShareSnapshot, *, format: str, theme: str,
                     branding: bool) -> str:
    """Fragment + chrome for one already-prepared snapshot."""
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
