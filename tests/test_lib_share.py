"""Layer A unit tests for bin/_lib_share.py."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import sys
from datetime import datetime, timezone

# Load _lib_share by path (same pattern bin/cctally uses for its peers).
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LIB_SHARE_PATH = _REPO_ROOT / "bin" / "_lib_share.py"
_spec = importlib.util.spec_from_file_location("_lib_share", _LIB_SHARE_PATH)
_lib_share = importlib.util.module_from_spec(_spec)
# Register in sys.modules BEFORE exec_module: Python 3.14's `dataclass`
# decorator looks up `cls.__module__` in `sys.modules` for KW_ONLY type checks
# during class processing, which fails if the module isn't registered yet.
sys.modules["_lib_share"] = _lib_share
_spec.loader.exec_module(_lib_share)

# Load bin/cctally as a module for testing destination/emit helpers. The
# script has no .py extension, so we supply an explicit SourceFileLoader
# (otherwise spec_from_file_location returns None for unrecognized suffixes).
# The module guards CLI entry behind `if __name__ == "__main__":`, so
# exec_module doesn't trigger argparse parsing. The CCTALLY_TEST_IMPORT env
# var is defensive for any future restructure that might run argparse at
# module import time.
os.environ.setdefault("CCTALLY_TEST_IMPORT", "1")
_CCTALLY_PATH = _REPO_ROOT / "bin" / "cctally"
_cctally_loader = importlib.machinery.SourceFileLoader(
    "_cctally_for_tests", str(_CCTALLY_PATH)
)
_cctally_spec = importlib.util.spec_from_loader(
    "_cctally_for_tests", _cctally_loader
)
_cctally = importlib.util.module_from_spec(_cctally_spec)
sys.modules["_cctally_for_tests"] = _cctally
_cctally_loader.exec_module(_cctally)

# Re-export for terse test bodies.
ShareSnapshot = _lib_share.ShareSnapshot
PeriodSpec = _lib_share.PeriodSpec
ColumnSpec = _lib_share.ColumnSpec
Row = _lib_share.Row
TextCell = _lib_share.TextCell
MoneyCell = _lib_share.MoneyCell
PercentCell = _lib_share.PercentCell
DateCell = _lib_share.DateCell
DeltaCell = _lib_share.DeltaCell
ProjectCell = _lib_share.ProjectCell
Totalled = _lib_share.Totalled
ChartPoint = _lib_share.ChartPoint
LineChart = _lib_share.LineChart
BarChart = _lib_share.BarChart
HorizontalBarChart = _lib_share.HorizontalBarChart


def _make_minimal_snapshot() -> ShareSnapshot:
    return ShareSnapshot(
        cmd="report",
        title="Weekly $ / % trend — last 4 weeks",
        subtitle="Apr 11 → May 9 (UTC) · light · projects anonymized",
        period=PeriodSpec(
            start=datetime(2026, 4, 11, tzinfo=timezone.utc),
            end=datetime(2026, 5, 9, tzinfo=timezone.utc),
            display_tz="UTC",
            label="Apr 11 → May 9 (UTC)",
        ),
        columns=(
            ColumnSpec(key="week", label="Week", align="left"),
            ColumnSpec(key="cost", label="$ Cost", align="right"),
        ),
        rows=(
            Row(cells={
                "week": TextCell("Apr 11"),
                "cost": MoneyCell(123.45),
            }),
        ),
        chart=None,
        totals=(Totalled(label="Sum", value="$123.45"),),
        notes=(),
        generated_at=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
        version="1.4.0",
    )


def test_snapshot_constructs_and_is_frozen():
    snap = _make_minimal_snapshot()
    assert snap.cmd == "report"
    assert snap.rows[0].cells["cost"].usd == 123.45
    # Frozen — should raise on mutation.
    import dataclasses
    try:
        snap.cmd = "daily"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("ShareSnapshot must be frozen")


def test_xml_escape_handles_all_xml_chars():
    assert _lib_share._xml_escape("a&b") == "a&amp;b"
    assert _lib_share._xml_escape("a<b") == "a&lt;b"
    assert _lib_share._xml_escape("a>b") == "a&gt;b"
    assert _lib_share._xml_escape('a"b') == "a&quot;b"
    assert _lib_share._xml_escape("a'b") == "a&#39;b"
    assert _lib_share._xml_escape("plain") == "plain"
    # Adversarial.
    assert _lib_share._xml_escape("Project<script>") == "Project&lt;script&gt;"


def test_attr_escape_normalizes_newlines():
    # Same as xml plus newline normalization.
    assert _lib_share._attr_escape("a\nb") == "a b"
    assert _lib_share._attr_escape("a&\nb") == "a&amp; b"


def test_md_escape_covers_html_and_md_chars():
    # HTML chars (Codex finding M8): markdown surfaces interpret raw HTML.
    assert _lib_share._md_escape("a<b") == "a&lt;b"
    assert _lib_share._md_escape("a>b") == "a&gt;b"
    assert _lib_share._md_escape("a&b") == "a&amp;b"
    # Markdown formatting chars.
    assert _lib_share._md_escape("a|b") == "a\\|b"
    assert _lib_share._md_escape("a*b") == "a\\*b"
    assert _lib_share._md_escape("a_b") == "a\\_b"
    assert _lib_share._md_escape("a`b") == "a\\`b"
    assert _lib_share._md_escape("a[b") == "a\\[b"
    assert _lib_share._md_escape("a]b") == "a\\]b"
    # Adversarial: HTML+md combo.
    assert _lib_share._md_escape("evil<img onerror=x>") == "evil&lt;img onerror=x&gt;"


def test_palettes_have_required_keys():
    """Both palettes must define every color slot used by SVG/HTML chrome and charts."""
    required_keys = {
        "bg", "fg", "muted", "grid", "axis",
        "series_primary", "series_secondary",
        "series_palette",
        "ref_warn", "ref_alarm",
        "table_header_bg", "table_row_alt", "footer_link",
    }
    assert set(_lib_share.PALETTE_LIGHT.keys()) >= required_keys
    assert set(_lib_share.PALETTE_DARK.keys()) >= required_keys
    # Palettes must differ on at least the bg color.
    assert _lib_share.PALETTE_LIGHT["bg"] != _lib_share.PALETTE_DARK["bg"]
    # series_palette must be non-empty so stack-color cycling can't divide-by-zero.
    assert len(_lib_share.PALETTE_LIGHT["series_palette"]) > 0
    assert len(_lib_share.PALETTE_DARK["series_palette"]) > 0


def test_render_dispatches_md():
    snap = _make_minimal_snapshot()
    out = _lib_share.render(snap, format="md", theme="light", branding=True)
    assert isinstance(out, str)
    assert snap.title in out


def test_render_unknown_format_raises():
    snap = _make_minimal_snapshot()
    try:
        _lib_share.render(snap, format="pdf", theme="light", branding=True)
    except ValueError as e:
        assert "format" in str(e).lower()
        return
    raise AssertionError("expected ValueError on unknown format")


def test_render_dispatches_svg():
    snap = _make_minimal_snapshot()
    out = _lib_share.render(snap, format="svg", theme="light", branding=True)
    assert isinstance(out, str)
    assert "<svg" in out
    # Title escaped into the SVG comment by the stub.
    assert _lib_share._xml_escape(snap.title) in out


def test_render_dispatches_html():
    snap = _make_minimal_snapshot()
    out = _lib_share.render(snap, format="html", theme="dark", branding=True)
    assert isinstance(out, str)
    assert "<!DOCTYPE html" in out
    assert _lib_share._xml_escape(snap.title) in out


def test_render_unknown_theme_raises():
    snap = _make_minimal_snapshot()
    try:
        _lib_share.render(snap, format="svg", theme="solarized", branding=True)
    except ValueError as e:
        assert "theme" in str(e).lower()
        return
    raise AssertionError("expected ValueError on unknown theme")


def test_md_escape_backslash_does_not_double_escape():
    # A literal backslash becomes \\ — single pass, no doubling.
    assert _lib_share._md_escape("a\\b") == "a\\\\b"
    # Backslash followed by a markdown-format char: each escapes once.
    assert _lib_share._md_escape("a\\*b") == "a\\\\\\*b"
    # An already-escaped sequence in the input still escapes byte-for-byte.
    assert _lib_share._md_escape("\\|") == "\\\\\\|"


def test_fmt_num_one_decimal():
    assert _lib_share._fmt_num(0) == "0.0"
    assert _lib_share._fmt_num(1) == "1.0"
    assert _lib_share._fmt_num(1.234) == "1.2"
    # Python's f"{x:.1f}" uses round-half-to-even (banker's rounding) on IEEE-754:
    # 1.25 has exact binary representation, ties to even → "1.2".
    assert _lib_share._fmt_num(1.25) == "1.2"
    assert _lib_share._fmt_num(-0.0) == "0.0"   # no negative-zero
    assert _lib_share._fmt_num(1e6) == "1000000.0"   # no scientific notation
    assert _lib_share._fmt_num(1e-9) == "0.0"        # tiny → 0.0, not 1e-09


def test_fmt_num_handles_float_not_int_specially():
    assert _lib_share._fmt_num(0.05) == "0.1"  # 1-decimal rounding


def test_fmt_num_rejects_non_finite():
    import math
    for bad in (float("nan"), float("inf"), -float("inf")):
        try:
            _lib_share._fmt_num(bad)
        except ValueError as e:
            assert "finite" in str(e).lower()
            continue
        raise AssertionError(f"_fmt_num({bad!r}) should have raised ValueError")


def test_serialize_attrs_lexical_order():
    out = _lib_share._serialize_attrs({"x": 1, "fill": "red", "y": 2, "id": "skip-me"})
    # Lexical: fill, id, x, y
    assert out == 'fill="red" id="skip-me" x="1.0" y="2.0"'


def test_serialize_attrs_escapes_attr_values():
    out = _lib_share._serialize_attrs({"data-label": 'Project<script>"evil"'})
    assert "<" not in out and ">" not in out and "&lt;" in out


def test_serialize_attrs_skips_none():
    out = _lib_share._serialize_attrs({"fill": "red", "stroke": None})
    assert out == 'fill="red"'


def test_serialize_attrs_handles_strings_and_numbers():
    out = _lib_share._serialize_attrs({"text-anchor": "middle", "font-size": 12})
    assert out == 'font-size="12.0" text-anchor="middle"'


def test_svg_rect():
    out = _lib_share.svg_rect(10, 20, 100, 50, fill="red")
    assert out == '<rect fill="red" height="50.0" width="100.0" x="10.0" y="20.0"/>'


def test_svg_text_with_anchor_and_weight():
    out = _lib_share.svg_text(50, 100, "Hello",
                              font_size=14, fill="#1a1a1a",
                              anchor="middle", weight="bold")
    assert out == (
        '<text fill="#1a1a1a" font-size="14.0" font-weight="bold" '
        'text-anchor="middle" x="50.0" y="100.0">Hello</text>'
    )


def test_svg_text_escapes_content():
    out = _lib_share.svg_text(0, 0, "<script>", font_size=10, fill="#000")
    assert "&lt;script&gt;" in out
    assert "<script>" not in out


def test_svg_text_falsy_weight_omits_attr():
    # Empty-string weight must not emit font-weight=""
    out_empty = _lib_share.svg_text(0, 0, "x", font_size=10, fill="#000", weight="")
    assert "font-weight" not in out_empty
    # Default "normal" weight: same behavior, no attribute.
    out_normal = _lib_share.svg_text(0, 0, "x", font_size=10, fill="#000")
    assert "font-weight" not in out_normal
    # Non-default explicit weight still emits.
    out_bold = _lib_share.svg_text(0, 0, "x", font_size=10, fill="#000", weight="bold")
    assert 'font-weight="bold"' in out_bold


def test_svg_line():
    out = _lib_share.svg_line(0, 0, 100, 100, stroke="#000", width=2)
    assert out == '<line stroke="#000" stroke-width="2.0" x1="0.0" x2="100.0" y1="0.0" y2="100.0"/>'


def test_svg_polyline():
    out = _lib_share.svg_polyline([(0.0, 0.0), (10.0, 20.0), (30.0, 5.0)],
                                  stroke="#2563eb", width=2.0)
    assert 'points="0.0,0.0 10.0,20.0 30.0,5.0"' in out
    assert 'fill="none"' in out


def test_svg_path():
    out = _lib_share.svg_path("M0 0 L10 10", stroke="#000")
    assert 'd="M0 0 L10 10"' in out


def test_svg_group_wraps_children():
    children = ['<rect x="0" y="0"/>', '<text x="0" y="0">x</text>']
    out = _lib_share.svg_group(children, transform="translate(5,5)")
    assert out.startswith('<g transform="translate(5,5)">')
    assert out.endswith("</g>")
    assert children[0] in out and children[1] in out


def test_line_chart_renders_chart_only_svg_byte_stable():
    """LineChart with 4 points renders to a stable SVG fragment."""
    chart = _lib_share.LineChart(
        points=(
            _lib_share.ChartPoint(x_label="Apr 11", x_value=0, y_value=2.5),
            _lib_share.ChartPoint(x_label="Apr 18", x_value=1, y_value=3.0),
            _lib_share.ChartPoint(x_label="Apr 25", x_value=2, y_value=2.8),
            _lib_share.ChartPoint(x_label="May 2",  x_value=3, y_value=3.4),
        ),
        y_label="$ / %",
    )
    out = _lib_share._render_line_chart_svg(
        chart,
        palette=_lib_share.PALETTE_LIGHT,
        x=20, y=20, width=560, height=180,
    )
    # Must start/end with <g> wrapper.
    assert out.startswith("<g")
    assert out.endswith("</g>")
    # Must include polyline for series.
    assert "<polyline" in out
    # Must include axis lines.
    assert "<line" in out
    # All numbers one-decimal.
    import re
    for match in re.findall(r'\d+\.\d+', out):
        assert match.count(".") == 1
        assert len(match.split(".")[1]) == 1
    # Defense-in-depth: regex above silently passes 'e+10', so explicitly check
    # for scientific-notation patterns (digit + e + sign + digit).
    # Use regex to avoid false positives on attribute names like 'text-anchor'.
    assert not re.search(r'\de[+-]\d', out), \
        f"scientific notation leaked into SVG output: {out!r}"
    # No randomness — repeatable.
    out2 = _lib_share._render_line_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT, x=20, y=20, width=560, height=180,
    )
    assert out == out2


def test_line_chart_with_reference_lines():
    chart = _lib_share.LineChart(
        points=(
            _lib_share.ChartPoint(x_label="Mon", x_value=0, y_value=20.0),
            _lib_share.ChartPoint(x_label="Tue", x_value=1, y_value=45.0),
        ),
        y_label="cumulative %",
        reference_lines=((90.0, "90%", "warn"), (100.0, "100%", "alarm")),
    )
    out = _lib_share._render_line_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT, x=0, y=0, width=400, height=200,
    )
    # Both reference lines render with their palette colors.
    assert _lib_share.PALETTE_LIGHT["ref_warn"] in out
    assert _lib_share.PALETTE_LIGHT["ref_alarm"] in out


def test_svg_chrome_header_includes_title_subtitle_timestamp():
    snap = _make_minimal_snapshot()
    out = _lib_share._render_svg_header(snap, palette=_lib_share.PALETTE_LIGHT,
                                        x=20, y=20, width=560)
    assert _lib_share._xml_escape(snap.title) in out
    assert _lib_share._xml_escape(snap.subtitle) in out
    # Generated-at timestamp ISO Z form.
    assert "2026-05-09T12:00:00Z" in out


def test_svg_chrome_footer_renders_branding_when_enabled():
    snap = _make_minimal_snapshot()
    out = _lib_share._render_svg_footer(snap, palette=_lib_share.PALETTE_LIGHT,
                                        x=20, y=380, width=560, branding=True)
    assert "Generated by cctally" in out
    assert "v1.4.0" in out


def test_svg_chrome_footer_omits_branding_when_disabled():
    snap = _make_minimal_snapshot()
    out = _lib_share._render_svg_footer(snap, palette=_lib_share.PALETTE_LIGHT,
                                        x=20, y=380, width=560, branding=False)
    assert "Generated by cctally" not in out


def test_svg_chrome_footer_pre_release_falls_back_to_dev():
    snap = _make_minimal_snapshot()
    snap_no_version = _lib_share.ShareSnapshot(
        **{**snap.__dict__, "version": ""}
    )
    out = _lib_share._render_svg_footer(snap_no_version, palette=_lib_share.PALETTE_LIGHT,
                                        x=20, y=380, width=560, branding=True)
    assert "· dev" in out
    assert "v" not in out.split("dev")[0].rsplit("·", 1)[1]


def test_render_svg_with_chrome_includes_title_and_branding():
    snap = _make_minimal_snapshot()
    out = _lib_share._render_svg(snap, palette=_lib_share.PALETTE_LIGHT,
                                 branding=True, include_chrome=True)
    assert _lib_share._xml_escape(snap.title) in out
    assert "Generated by cctally" in out
    assert out.startswith('<svg')
    assert out.endswith('</svg>')


def test_render_svg_chart_only_omits_chrome():
    snap = _make_minimal_snapshot()
    out = _lib_share._render_svg(snap, palette=_lib_share.PALETTE_LIGHT,
                                 branding=True, include_chrome=False)
    # Title and footer-link absent in chart-only mode.
    assert _lib_share._xml_escape(snap.title) not in out
    assert "Generated by cctally" not in out
    assert out.startswith('<svg')


def test_render_svg_chart_only_with_no_chart_returns_empty_svg():
    snap = _make_minimal_snapshot()  # chart=None
    out = _lib_share._render_svg(snap, palette=_lib_share.PALETTE_LIGHT,
                                 branding=True, include_chrome=False)
    # Inner content empty (or whitespace-only) for table-only snapshots.
    inner = out[len('<svg xmlns="http://www.w3.org/2000/svg" '):]
    # No chart elements (no <polyline>, <line>, <rect>).
    assert "<polyline" not in inner


def test_render_html_wraps_chart_only_svg():
    snap = _make_minimal_snapshot()
    out = _lib_share._render_html(snap, palette=_lib_share.PALETTE_LIGHT, branding=True)
    assert out.startswith("<!DOCTYPE html>")
    assert "<html" in out and "</html>" in out
    # Title rendered as HTML <h1>.
    assert "<h1" in out and _lib_share._xml_escape(snap.title) in out
    # Inline SVG (chart-only — no nested chrome).
    assert "<svg" in out
    # Footer present once.
    assert out.count("Generated by cctally") == 1


def test_render_html_no_branding_omits_footer():
    snap = _make_minimal_snapshot()
    out = _lib_share._render_html(snap, palette=_lib_share.PALETTE_LIGHT, branding=False)
    assert "Generated by cctally" not in out


def test_render_html_renders_table_from_rows():
    snap = _make_minimal_snapshot()
    out = _lib_share._render_html(snap, palette=_lib_share.PALETTE_LIGHT, branding=True)
    assert "<table" in out
    # Column header.
    assert "<th" in out and "$ Cost" in out
    # Row cell.
    assert "<td" in out and "$123.45" in out


def test_render_html_escapes_revealed_project_in_table():
    """If user supplied --reveal-projects and the project name contains HTML chars,
    the HTML output must escape them in the table cell."""
    from datetime import datetime, timezone
    snap = _lib_share.ShareSnapshot(
        cmd="project",
        title="Per-project usage",
        subtitle=None,
        period=_lib_share.PeriodSpec(
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 5, 9, tzinfo=timezone.utc),
            display_tz="UTC", label="May 1 → May 9 (UTC)",
        ),
        columns=(_lib_share.ColumnSpec(key="project", label="Project", align="left"),),
        rows=(_lib_share.Row(cells={"project": _lib_share.ProjectCell("evil<script>")}),),
        chart=None, totals=(), notes=(),
        generated_at=datetime(2026, 5, 9, 12, tzinfo=timezone.utc),
        version="1.4.0",
    )
    out = _lib_share._render_html(snap, palette=_lib_share.PALETTE_LIGHT, branding=True)
    assert "<script>" not in out
    assert "evil&lt;script&gt;" in out


def test_render_cell_text_dispatches_all_types():
    """Direct dispatch coverage for every Cell subtype."""
    from datetime import datetime, timezone

    # Text passthrough.
    assert _lib_share._render_cell_text(TextCell("hi")) == "hi"

    # Money: positive, negative, large.
    assert _lib_share._render_cell_text(MoneyCell(123.45)) == "$123.45"
    assert _lib_share._render_cell_text(MoneyCell(-12.34)) == "-$12.34"
    assert _lib_share._render_cell_text(MoneyCell(1234567.89)) == "$1,234,567.89"

    # Percent: 1 decimal.
    assert _lib_share._render_cell_text(PercentCell(12.345)) == "12.3%"

    # Date: ISO date.
    assert _lib_share._render_cell_text(
        DateCell(datetime(2026, 5, 9, tzinfo=timezone.utc))
    ) == "2026-05-09"

    # Delta percent: +/- sign + 1 decimal + %.
    assert _lib_share._render_cell_text(DeltaCell(1.5, "%")) == "+1.5%"
    assert _lib_share._render_cell_text(DeltaCell(-1.5, "%")) == "-1.5%"
    assert _lib_share._render_cell_text(DeltaCell(0.0, "%")) == "+0.0%"  # zero treated as non-negative

    # Delta dollar: +/- sign + currency + 2 decimals.
    assert _lib_share._render_cell_text(DeltaCell(1.5, "$")) == "+$1.50"
    assert _lib_share._render_cell_text(DeltaCell(-1.5, "$")) == "-$1.50"

    # Project label passthrough.
    assert _lib_share._render_cell_text(ProjectCell("/path/to/project")) == "/path/to/project"


def test_render_cell_text_unknown_type_raises():
    class FakeCell:
        pass
    try:
        _lib_share._render_cell_text(FakeCell())
    except TypeError as e:
        assert "FakeCell" in str(e) or "unknown" in str(e).lower()
        return
    raise AssertionError("expected TypeError on unknown cell type")


def test_render_svg_dark_palette_uses_dark_bg():
    """SVG output with dark theme must use the dark palette's bg color, not light."""
    snap = _make_minimal_snapshot()
    out_dark = _lib_share._render_svg(
        snap, palette=_lib_share.PALETTE_DARK,
        branding=True, include_chrome=True,
    )
    assert _lib_share.PALETTE_DARK["bg"] in out_dark
    assert _lib_share.PALETTE_LIGHT["bg"] not in out_dark
    # Dark-palette fg color also present (used by header).
    assert _lib_share.PALETTE_DARK["fg"] in out_dark


def test_bar_chart_renders():
    chart = _lib_share.BarChart(
        points=(
            _lib_share.ChartPoint(x_label="Mon", x_value=0, y_value=12.5),
            _lib_share.ChartPoint(x_label="Tue", x_value=1, y_value=18.0),
            _lib_share.ChartPoint(x_label="Wed", x_value=2, y_value=8.0),
        ),
        y_label="$",
    )
    out = _lib_share._render_bar_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT,
        x=20, y=20, width=560, height=180,
    )
    # Three <rect> bars.
    assert out.count("<rect") == 3
    # Y-label and x-tick labels present.
    assert "Mon" in out and "Tue" in out and "Wed" in out
    # Byte-stable.
    out2 = _lib_share._render_bar_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT,
        x=20, y=20, width=560, height=180,
    )
    assert out == out2


def test_bar_chart_handles_empty():
    chart = _lib_share.BarChart(points=(), y_label="$")
    out = _lib_share._render_bar_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT,
        x=0, y=0, width=400, height=200,
    )
    assert "(no data)" in out


def test_hbar_chart_renders():
    chart = _lib_share.HorizontalBarChart(
        points=(
            _lib_share.ChartPoint(x_label="project-1", x_value=0, y_value=120.0,
                                  project_label="project-1"),
            _lib_share.ChartPoint(x_label="project-2", x_value=1, y_value=80.0,
                                  project_label="project-2"),
        ),
        x_label="$",
    )
    out = _lib_share._render_hbar_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT,
        x=20, y=20, width=560, height=120,
    )
    assert out.count("<rect") == 2
    assert "project-1" in out and "project-2" in out
    # Byte-stable.
    out2 = _lib_share._render_hbar_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT,
        x=20, y=20, width=560, height=120,
    )
    assert out == out2


def test_hbar_chart_respects_cap():
    chart = _lib_share.HorizontalBarChart(
        points=tuple(
            _lib_share.ChartPoint(x_label=f"p{i}", x_value=i, y_value=100.0 - i,
                                  project_label=f"p{i}")
            for i in range(20)
        ),
        x_label="$",
        cap=12,
    )
    out = _lib_share._render_hbar_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT,
        x=0, y=0, width=560, height=400,
    )
    # Only top 12 bars rendered.
    assert out.count("<rect") == 12
    assert "p0" in out and "p11" in out
    assert "p12" not in out


def test_render_md_includes_title_subtitle_table_footer():
    snap = _make_minimal_snapshot()
    out = _lib_share._render_md(snap, branding=True)
    # Title as # heading.
    assert f"# {_lib_share._md_escape(snap.title)}" in out
    # Subtitle as italic line beneath.
    assert _lib_share._md_escape(snap.subtitle) in out
    # Table header.
    assert "| Week | $ Cost |" in out or "| Week |" in out
    # Separator: alignment-encoded form (`:---|---:`) is contract per
    # _render_md_table; the GFM-loose forms are tolerated for forward-compat.
    assert (
        "| --- |" in out
        or "|---|" in out
        or ":---" in out
        or "---:" in out
    )
    # Row content.
    assert "$123.45" in out
    # Footer (single occurrence).
    assert out.count("Generated by [cctally]") == 1


def test_render_md_no_branding_omits_footer():
    snap = _make_minimal_snapshot()
    out = _lib_share._render_md(snap, branding=False)
    assert "Generated by [cctally]" not in out


def test_render_md_no_chart_link():
    """Markdown is text-only — no `![chart](...)` link emitted (Section 5.7)."""
    snap = _make_minimal_snapshot()
    out = _lib_share._render_md(snap, branding=True)
    assert "![" not in out


def test_render_md_escapes_html_chars_in_revealed_project():
    snap = _lib_share.ShareSnapshot(
        cmd="project",
        title="Per-project usage",
        subtitle=None,
        period=_lib_share.PeriodSpec(
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 5, 9, tzinfo=timezone.utc),
            display_tz="UTC", label="May 1 → May 9 (UTC)",
        ),
        columns=(_lib_share.ColumnSpec(key="project", label="Project", align="left"),),
        rows=(_lib_share.Row(cells={"project": _lib_share.ProjectCell("evil<script>")}),),
        chart=None, totals=(), notes=(),
        generated_at=datetime(2026, 5, 9, 12, tzinfo=timezone.utc),
        version="1.4.0",
    )
    out = _lib_share._render_md(snap, branding=True)
    assert "<script>" not in out
    assert "evil&lt;script&gt;" in out


def test_render_md_notes_become_blockquotes():
    """Notes render as Markdown blockquote lines."""
    base = _make_minimal_snapshot()
    snap = _lib_share.ShareSnapshot(
        **{**base.__dict__,
           "notes": ("LOW CONF: thin data", "5h reset crossed week")},
    )
    out = _lib_share._render_md(snap, branding=True)
    assert "> LOW CONF: thin data" in out
    assert "> 5h reset crossed week" in out


# --- Task 15: anonymization mapping ---


def test_collect_project_costs_from_rows():
    """Walk Row.cells; pair ProjectCell with sibling MoneyCell in same row."""
    rows = (
        _lib_share.Row(cells={
            "project": _lib_share.ProjectCell("alpha"),
            "cost": _lib_share.MoneyCell(50.0),
        }),
        _lib_share.Row(cells={
            "project": _lib_share.ProjectCell("beta"),
            "cost": _lib_share.MoneyCell(120.0),
        }),
        _lib_share.Row(cells={
            "project": _lib_share.ProjectCell("(unknown)"),
            "cost": _lib_share.MoneyCell(10.0),
        }),
    )
    snap = _lib_share.ShareSnapshot(
        cmd="project", title="t", subtitle=None,
        period=_lib_share.PeriodSpec(
            start=datetime.now(timezone.utc), end=datetime.now(timezone.utc),
            display_tz="UTC", label="x",
        ),
        columns=(
            _lib_share.ColumnSpec(key="project", label="Project", align="left"),
            _lib_share.ColumnSpec(key="cost", label="$ Cost", align="right"),
        ),
        rows=rows,
        chart=None, totals=(), notes=(),
        generated_at=datetime.now(timezone.utc), version="1.0.0",
    )
    costs = _lib_share._collect_project_costs(snap)
    assert costs == {"alpha": 50.0, "beta": 120.0, "(unknown)": 10.0}


def test_build_anon_mapping_descending_by_cost():
    costs = {"alpha": 50.0, "beta": 120.0, "(unknown)": 10.0, "gamma": 80.0}
    mapping = _lib_share._build_anon_mapping(costs)
    # beta (120) -> project-1, gamma (80) -> project-2, alpha (50) -> project-3.
    assert mapping["beta"] == "project-1"
    assert mapping["gamma"] == "project-2"
    assert mapping["alpha"] == "project-3"
    # (unknown) is never numbered.
    assert mapping["(unknown)"] == "(unknown)"


def test_build_anon_mapping_stable_for_ties():
    """Equal costs sort by name (stable)."""
    costs = {"alpha": 100.0, "beta": 100.0}
    mapping = _lib_share._build_anon_mapping(costs)
    # Lex order on tie: alpha -> project-1, beta -> project-2.
    assert mapping["alpha"] == "project-1"
    assert mapping["beta"] == "project-2"


# --- Task 16: _scrub + _apply_anon_mapping ---


def test_scrub_replaces_project_cell_labels():
    snap = _lib_share.ShareSnapshot(
        cmd="project", title="t", subtitle=None,
        period=_lib_share.PeriodSpec(
            start=datetime.now(timezone.utc), end=datetime.now(timezone.utc),
            display_tz="UTC", label="x",
        ),
        columns=(
            _lib_share.ColumnSpec(key="project", label="Project", align="left"),
            _lib_share.ColumnSpec(key="cost", label="$ Cost", align="right"),
        ),
        rows=(
            _lib_share.Row(cells={
                "project": _lib_share.ProjectCell("client-foo-internal"),
                "cost": _lib_share.MoneyCell(120.0),
            }),
            _lib_share.Row(cells={
                "project": _lib_share.ProjectCell("acme-cloud"),
                "cost": _lib_share.MoneyCell(50.0),
            }),
        ),
        chart=None, totals=(), notes=(),
        generated_at=datetime.now(timezone.utc), version="1.0.0",
    )
    scrubbed = _lib_share._scrub(snap, reveal_projects=False)
    labels_after = [r.cells["project"].label for r in scrubbed.rows]
    assert labels_after == ["project-1", "project-2"]
    # Original snapshot untouched (frozen + new instance returned).
    assert [r.cells["project"].label for r in snap.rows] == [
        "client-foo-internal", "acme-cloud",
    ]


def test_scrub_reveal_projects_is_noop():
    snap = _lib_share.ShareSnapshot(
        cmd="project", title="t", subtitle=None,
        period=_lib_share.PeriodSpec(
            start=datetime.now(timezone.utc), end=datetime.now(timezone.utc),
            display_tz="UTC", label="x",
        ),
        columns=(_lib_share.ColumnSpec(key="project", label="Project", align="left"),),
        rows=(_lib_share.Row(cells={"project": _lib_share.ProjectCell("real-name")}),),
        chart=None, totals=(), notes=(),
        generated_at=datetime.now(timezone.utc), version="1.0.0",
    )
    out = _lib_share._scrub(snap, reveal_projects=True)
    assert out is snap


def test_scrub_replaces_chart_point_project_label():
    chart = _lib_share.HorizontalBarChart(
        points=(
            _lib_share.ChartPoint(x_label="alpha", x_value=0, y_value=120.0,
                                  project_label="alpha"),
            _lib_share.ChartPoint(x_label="beta", x_value=1, y_value=50.0,
                                  project_label="beta"),
        ),
        x_label="$",
    )
    snap = _lib_share.ShareSnapshot(
        cmd="project", title="t", subtitle=None,
        period=_lib_share.PeriodSpec(
            start=datetime.now(timezone.utc), end=datetime.now(timezone.utc),
            display_tz="UTC", label="x",
        ),
        columns=(),
        rows=(
            _lib_share.Row(cells={
                "project": _lib_share.ProjectCell("alpha"),
                "cost": _lib_share.MoneyCell(120.0),
            }),
            _lib_share.Row(cells={
                "project": _lib_share.ProjectCell("beta"),
                "cost": _lib_share.MoneyCell(50.0),
            }),
        ),
        chart=chart, totals=(), notes=(),
        generated_at=datetime.now(timezone.utc), version="1.0.0",
    )
    scrubbed = _lib_share._scrub(snap, reveal_projects=False)
    chart_labels = [p.project_label for p in scrubbed.chart.points]
    chart_x_labels = [p.x_label for p in scrubbed.chart.points]
    assert chart_labels == ["project-1", "project-2"]
    # x_label also rewritten (used as visible axis label).
    assert chart_x_labels == ["project-1", "project-2"]


def test_anonymized_output_contains_zero_original_tokens():
    """Section 8.4 invariant: anonymized output contains no original project basename."""
    snap = _lib_share.ShareSnapshot(
        cmd="project", title="Per-project", subtitle="x",
        period=_lib_share.PeriodSpec(
            start=datetime.now(timezone.utc), end=datetime.now(timezone.utc),
            display_tz="UTC", label="x",
        ),
        columns=(
            _lib_share.ColumnSpec(key="project", label="Project", align="left"),
            _lib_share.ColumnSpec(key="cost", label="$ Cost", align="right"),
        ),
        rows=(
            _lib_share.Row(cells={
                "project": _lib_share.ProjectCell("client-foo-internal"),
                "cost": _lib_share.MoneyCell(120.0),
            }),
        ),
        chart=_lib_share.HorizontalBarChart(
            points=(
                _lib_share.ChartPoint(
                    x_label="client-foo-internal", x_value=0,
                    y_value=120.0, project_label="client-foo-internal",
                ),
            ),
            x_label="$",
        ),
        totals=(), notes=(),
        generated_at=datetime(2026, 5, 9, 12, tzinfo=timezone.utc), version="1.4.0",
    )
    scrubbed = _lib_share._scrub(snap, reveal_projects=False)
    for fmt in ("md", "svg", "html"):
        out = _lib_share.render(scrubbed, format=fmt, theme="light", branding=True)
        assert "client-foo-internal" not in out, f"original token leaked into {fmt}"


def test_scrub_anonymizes_chart_only_project_label():
    """Chart-only labels (not present in any row) must still be anonymized.

    Locks the chart-fallback gather path against accidental removal — the
    main canary test (`test_anonymized_output_contains_zero_original_tokens`)
    has matching row+chart entries, so a regression that drops chart-walk
    in `_collect_project_costs` would not surface there.
    """
    chart = _lib_share.HorizontalBarChart(
        points=(
            _lib_share.ChartPoint(
                x_label="acme-secret-project",
                x_value=0,
                y_value=42.0,
                project_label="acme-secret-project",
            ),
        ),
        x_label="$",
    )
    snap = _lib_share.ShareSnapshot(
        cmd="project", title="t", subtitle=None,
        period=_lib_share.PeriodSpec(
            start=datetime.now(timezone.utc),
            end=datetime.now(timezone.utc),
            display_tz="UTC", label="x",
        ),
        columns=(),
        rows=(),  # NO ROWS — only chart-point label.
        chart=chart, totals=(), notes=(),
        generated_at=datetime.now(timezone.utc), version="1.0.0",
    )
    scrubbed = _lib_share._scrub(snap, reveal_projects=False)
    # Both project_label AND x_label must be rewritten.
    assert scrubbed.chart.points[0].project_label == "project-1"
    assert scrubbed.chart.points[0].x_label == "project-1"
    # And the original token must not survive into ANY render output.
    for fmt in ("md", "svg", "html"):
        out = _lib_share.render(scrubbed, format=fmt, theme="light", branding=True)
        assert "acme-secret-project" not in out, f"chart-only token leaked into {fmt}"


# ============================================================
# Destination + emit helpers (live in bin/cctally, not _lib_share).
# ============================================================


def test_resolve_destination_md_default_stdout():
    args = type("A", (), {"format": "md", "output": None, "copy": False,
                          "open_after_write": False})()
    kind, value = _cctally._resolve_destination(args, cmd="daily",
                                                generated_at_utc_date="2026-05-09")
    assert kind == "stdout"
    assert value is None


def test_resolve_destination_md_copy():
    args = type("A", (), {"format": "md", "output": None, "copy": True,
                          "open_after_write": False})()
    kind, value = _cctally._resolve_destination(args, cmd="daily",
                                                generated_at_utc_date="2026-05-09")
    assert kind == "clipboard"
    assert value is None


def test_resolve_destination_html_default_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path))
    args = type("A", (), {"format": "html", "output": None, "copy": False,
                          "open_after_write": False})()
    kind, value = _cctally._resolve_destination(args, cmd="daily",
                                                generated_at_utc_date="2026-05-09")
    assert kind == "file"
    assert str(value).startswith(str(tmp_path))
    assert "cctally-daily-2026-05-09.html" in str(value)


def test_resolve_destination_html_collision_appends_counter(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path))
    # Pre-create the would-be path.
    (tmp_path / "cctally-daily-2026-05-09.html").write_text("x")
    args = type("A", (), {"format": "html", "output": None, "copy": False,
                          "open_after_write": False})()
    kind, value = _cctally._resolve_destination(args, cmd="daily",
                                                generated_at_utc_date="2026-05-09")
    assert "cctally-daily-2026-05-09-2.html" in str(value)


def test_resolve_destination_html_explicit_output(tmp_path):
    target = tmp_path / "myreport.html"
    args = type("A", (), {"format": "html", "output": str(target), "copy": False,
                          "open_after_write": False})()
    kind, value = _cctally._resolve_destination(args, cmd="daily",
                                                generated_at_utc_date="2026-05-09")
    assert kind == "file"
    assert str(value) == str(target)


def test_resolve_destination_explicit_dash_means_stdout():
    args = type("A", (), {"format": "html", "output": "-", "copy": False,
                          "open_after_write": False})()
    kind, value = _cctally._resolve_destination(args, cmd="daily",
                                                generated_at_utc_date="2026-05-09")
    assert kind == "stdout"
    assert value is None


def test_emit_stdout_writes_content(capsys):
    _cctally._emit("hello\n", kind="stdout", value=None)
    captured = capsys.readouterr()
    assert captured.out == "hello\n"


def test_emit_file_writes_path_and_logs_to_stderr(tmp_path, capsys):
    target = tmp_path / "out.html"
    _cctally._emit("<html>", kind="file", value=str(target))
    assert target.read_text() == "<html>"
    captured = capsys.readouterr()
    assert str(target) in captured.err


# ============================================================
# _share_render_and_emit wrapper (lazy-imports _lib_share, runs scrub
# -> render -> resolve_destination -> emit -> optional open).
# ============================================================


def test_share_render_and_emit_routes_md_to_stdout(capsys):
    snap = _make_minimal_snapshot()
    args = type("A", (), {"format": "md", "theme": "light", "no_branding": False,
                          "reveal_projects": False, "output": None, "copy": False,
                          "open_after_write": False})()
    _cctally._share_render_and_emit(snap, args)
    captured = capsys.readouterr()
    assert snap.title in captured.out


def test_share_render_and_emit_html_writes_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DOWNLOAD_DIR", str(tmp_path))
    snap = _make_minimal_snapshot()
    args = type("A", (), {"format": "html", "theme": "light", "no_branding": False,
                          "reveal_projects": False, "output": None, "copy": False,
                          "open_after_write": False})()
    _cctally._share_render_and_emit(snap, args)
    files = list(tmp_path.glob("cctally-*.html"))
    assert len(files) == 1
    content = files[0].read_text()
    assert snap.title in content


def test_share_render_and_emit_scrubs_project_labels(tmp_path, monkeypatch, capsys):
    """Privacy regression: wrapper-level scrub must fire when reveal_projects=False.

    Bypass-scrub regressions (e.g., refactoring _share_render_and_emit and
    accidentally dropping the _scrub call) would not surface in the existing
    md/html-routing tests because the minimal snapshot has no project cells.
    Any future refactor that drops or short-circuits the scrub step must fail
    here — original project name leaks into stdout.
    """
    snap = ShareSnapshot(
        cmd="project",
        title="Per-project",
        subtitle=None,
        period=PeriodSpec(
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 5, 9, tzinfo=timezone.utc),
            display_tz="UTC",
            label="May 1 -> May 9 (UTC)",
        ),
        columns=(
            ColumnSpec(key="project", label="Project", align="left"),
            ColumnSpec(key="cost", label="$ Cost", align="right"),
        ),
        rows=(
            Row(cells={
                "project": ProjectCell("acme-secret-project"),
                "cost": MoneyCell(120.0),
            }),
        ),
        chart=None,
        totals=(),
        notes=(),
        generated_at=datetime(2026, 5, 9, 12, tzinfo=timezone.utc),
        version="1.4.0",
    )
    args = type("A", (), {
        "format": "md", "theme": "light", "no_branding": False,
        "reveal_projects": False,  # The chokepoint MUST scrub.
        "output": None, "copy": False, "open_after_write": False,
    })()
    _cctally._share_render_and_emit(snap, args)
    captured = capsys.readouterr()
    assert "acme-secret-project" not in captured.out, (
        "wrapper bypassed _scrub: original project name leaked to output"
    )
    assert "project-1" in captured.out, (
        "wrapper rendered without scrub-replacement label"
    )


# ============================================================
# Task 29 — Cross-format theme + branding integration tests
#
# Branding/theme code shipped in Tasks 9, 11, 14, 18; these tests verify
# the runtime args reach the renderer correctly across all three formats
# (md theme is no-op, svg/html switch palettes, --no-branding strips
# footer everywhere) and that the html chrome ownership invariant from
# Codex finding M5 (single <h1> + single <footer>) holds.
# ============================================================


def test_dark_theme_uses_dark_palette_in_svg():
    snap = _make_minimal_snapshot()
    out_dark = _lib_share.render(snap, format="svg", theme="dark", branding=True)
    out_light = _lib_share.render(snap, format="svg", theme="light", branding=True)
    assert _lib_share.PALETTE_DARK["bg"] in out_dark
    assert _lib_share.PALETTE_LIGHT["bg"] in out_light
    assert out_dark != out_light


def test_dark_theme_uses_dark_palette_in_html():
    snap = _make_minimal_snapshot()
    out_dark = _lib_share.render(snap, format="html", theme="dark", branding=True)
    out_light = _lib_share.render(snap, format="html", theme="light", branding=True)
    assert _lib_share.PALETTE_DARK["bg"] in out_dark
    assert _lib_share.PALETTE_LIGHT["bg"] in out_light


def test_md_theme_is_noop():
    """Markdown is theme-agnostic — rendered output is identical for light/dark."""
    snap = _make_minimal_snapshot()
    light = _lib_share.render(snap, format="md", theme="light", branding=True)
    dark = _lib_share.render(snap, format="md", theme="dark", branding=True)
    assert light == dark


def test_no_branding_strips_footer_in_all_formats():
    snap = _make_minimal_snapshot()
    for fmt in ("md", "svg", "html"):
        with_branding = _lib_share.render(snap, format=fmt, theme="light", branding=True)
        without_branding = _lib_share.render(snap, format=fmt, theme="light", branding=False)
        assert "Generated by" in with_branding or "cctally" in with_branding, fmt
        assert "Generated by" not in without_branding, fmt


def test_html_chrome_appears_exactly_once_with_branding():
    """Chrome ownership invariant (Codex finding M5)."""
    snap = _make_minimal_snapshot()
    out = _lib_share.render(snap, format="html", theme="light", branding=True)
    assert out.count("Generated by") == 1
    # Title appears once in <h1> and once in <title>; no extra duplication.
    assert out.count("<h1") == 1
    assert out.count("<footer") == 1


# ============================================================
# Task 30 — Argparse + emit edge-case tests
#
# argparse's mutex group covers --format x --json x --status-line. But
# --copy x --format html and --open x --format md are runtime-only checks
# inside _resolve_destination / _share_render_and_emit, plus the
# clipboard-tool-missing error path inside _emit. These tests pin down
# the runtime-mutex contract that argparse can't enforce.
# ============================================================


def test_copy_rejected_for_html_format():
    args = type("A", (), {"format": "html", "output": None, "copy": True,
                          "open_after_write": False})()
    try:
        _cctally._resolve_destination(args, cmd="daily",
                                      generated_at_utc_date="2026-05-09")
    except SystemExit as e:
        assert e.code == 2
        return
    raise AssertionError("expected SystemExit")


def test_open_for_md_rejects_with_exit_2():
    """`--open + --format md` is rejected at the wrapper (Section 4.4).

    Test-spec adjustment vs. plan: Implementor 6's fix-loop turned this
    from a silent no-op into an explicit SystemExit(2) (md routes to
    stdout; --open is meaningless without a file destination). The plan
    was authored under the prior "silently skipped" semantics; this test
    asserts the new hard-reject behavior at bin/cctally:25917-25926.
    """
    snap = _make_minimal_snapshot()
    args = type("A", (), {"format": "md", "theme": "light", "no_branding": False,
                          "reveal_projects": False, "output": None, "copy": False,
                          "open_after_write": True})()
    try:
        _cctally._share_render_and_emit(snap, args)
    except SystemExit as e:
        assert e.code == 2
        return
    raise AssertionError("expected SystemExit(2) for --open + --format md")


def test_line_chart_multi_series_scales_by_x_value():
    """Projected ray (multi_series) must land at its true x-coordinate, not at enumerate index.

    Regression for the review finding "Scale line charts by x_value": prior
    to the fix the renderer used `enumerate(...)` for both primary and
    multi_series, pinning a 2-point projected ray to the left edge of the
    chart even when its x-values landed at the right edge.
    """
    # Primary: one early sample. Projected: 2 points spanning to the right edge.
    primary = (
        ChartPoint(x_label="early", x_value=10.0, y_value=20.0),
    )
    projected = (
        ChartPoint(x_label="now", x_value=130.0, y_value=20.0),
        ChartPoint(x_label="end", x_value=168.0, y_value=60.0),
    )
    chart = LineChart(
        points=primary,
        y_label="%",
        multi_series={"projected": projected},
    )
    out = _lib_share._render_line_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT,
        x=0, y=0, width=200, height=100,
    )
    # Inner box: ix = 50, iw = 200 - 50 - 10 = 140. Domain [10, 168],
    # span = 158. Primary point at x_value=10 → ix + 0 = 50.0.
    # Projected end at x_value=168 → ix + iw = 190.0. Right-edge anchor
    # is the regression-proof: under the old enumerate-index renderer it
    # would have been ix + x_step (left of mid).
    import re
    polylines = re.findall(r'points="([^"]+)"', out)
    assert len(polylines) >= 2  # primary + projected
    # Projected ray's last point must be at the right edge (190.0).
    proj_pts = polylines[-1]
    last_point_x = float(proj_pts.split()[-1].split(",")[0])
    assert abs(last_point_x - 190.0) < 1e-6, \
        f"projected ray last x={last_point_x}, expected ~190.0"


def test_line_chart_y_domain_includes_multi_series():
    """Multi_series y-values that exceed primary max must not clip past inner box top.

    Regression for the review finding: prior y_values list excluded
    multi_series, so a projected high above the actual-sample max would
    render at iy (clipped to top) rather than at its scaled position.
    """
    # Primary max y = 20. Projected high = 90 (well above primary max).
    chart = LineChart(
        points=(
            ChartPoint(x_label="a", x_value=0.0, y_value=10.0),
            ChartPoint(x_label="b", x_value=1.0, y_value=20.0),
        ),
        y_label="%",
        multi_series={"projected": (
            ChartPoint(x_label="now", x_value=1.0, y_value=20.0),
            ChartPoint(x_label="end", x_value=2.0, y_value=90.0),
        )},
    )
    out = _lib_share._render_line_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT,
        x=0, y=0, width=200, height=100,
    )
    # Inner box height ih = 100 - 10 - 30 = 60, iy = 10.
    # _scale_y over [0, 90] (min(0, primary+projected min) → max=90) maps:
    #   y=20 → 60 - 60*(20/90) ≈ 46.67  (primary max)
    #   y=90 → 0                         (projected max, at top)
    # Projected end y in SVG = iy + 0 = 10.0 (top of inner box). The
    # primary-only y-domain [0, 20] would have placed it at y < 10 (above
    # iy) — i.e. visually clipped.
    import re
    polylines = re.findall(r'points="([^"]+)"', out)
    assert len(polylines) >= 2
    proj_pts = polylines[-1].split()
    last_y = float(proj_pts[-1].split(",")[1])
    # Allow tiny float drift; key invariant is "not clipped above iy=10".
    assert last_y >= 10.0 - 1e-6, \
        f"projected high y={last_y} clipped above iy=10 (multi_series excluded from y-domain)"


def test_bar_chart_renders_stacks_when_present():
    """BarChart.stacks must render as cumulative segments, not be silently ignored.

    Regression for the review finding: weekly --breakdown populates
    `BarChart.stacks` but the renderer previously read only
    `chart.points`, producing an unstacked chart.
    """
    chart = BarChart(
        points=(
            ChartPoint(x_label="W1", x_value=0.0, y_value=30.0),
            ChartPoint(x_label="W2", x_value=1.0, y_value=50.0),
        ),
        y_label="$",
        stacks={
            "model-a": (
                ChartPoint(x_label="W1", x_value=0.0, y_value=10.0),
                ChartPoint(x_label="W2", x_value=1.0, y_value=20.0),
            ),
            "model-b": (
                ChartPoint(x_label="W1", x_value=0.0, y_value=20.0),
                ChartPoint(x_label="W2", x_value=1.0, y_value=30.0),
            ),
        },
    )
    out = _lib_share._render_bar_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT,
        x=0, y=0, width=400, height=200,
    )
    palette = _lib_share.PALETTE_LIGHT["series_palette"]
    # Both stack colors must appear (segments rendered).
    assert palette[0] in out
    assert palette[1] in out
    # Legend must include both model labels.
    assert "model-a" in out
    assert "model-b" in out
    # Sorted-key ordering: "model-a" gets palette[0], "model-b" gets palette[1].
    # Legend rows are stacked vertically with row height 12 starting at iy+4;
    # earlier sorted key sits above later sorted key.
    a_idx = out.index("model-a")
    b_idx = out.index("model-b")
    assert a_idx < b_idx, "expected sorted-key ordering in legend"


def test_bar_chart_unstacked_path_unchanged_when_no_stacks():
    """BarChart with stacks=None still renders the unstacked path."""
    chart = BarChart(
        points=(
            ChartPoint(x_label="W1", x_value=0.0, y_value=30.0),
            ChartPoint(x_label="W2", x_value=1.0, y_value=50.0),
        ),
        y_label="$",
    )
    out = _lib_share._render_bar_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT,
        x=0, y=0, width=400, height=200,
    )
    # Unstacked path uses series_primary (single color); palette[2..5] must NOT appear.
    palette = _lib_share.PALETTE_LIGHT["series_palette"]
    assert _lib_share.PALETTE_LIGHT["series_primary"] in out
    # Tertiary stack colors aren't on the unstacked path.
    assert palette[2] not in out
    assert palette[3] not in out


def test_share_validate_args_passes_with_format():
    """_share_validate_args is a no-op when --format is set."""
    import argparse
    args = argparse.Namespace(
        format="md", output=None, copy=False, open_after_write=False,
    )
    # Must not raise / not exit.
    _cctally._share_validate_args(args)


def test_share_validate_args_passes_with_no_share_flags():
    """_share_validate_args is a no-op when no share flags are set."""
    import argparse
    args = argparse.Namespace(
        format=None, output=None, copy=False, open_after_write=False,
    )
    _cctally._share_validate_args(args)


def test_share_validate_args_rejects_output_without_format():
    """--output without --format must exit 2 with a stderr message."""
    import argparse
    args = argparse.Namespace(
        format=None, output="/tmp/x.md", copy=False, open_after_write=False,
    )
    try:
        _cctally._share_validate_args(args)
    except SystemExit as e:
        assert e.code == 2
        return
    raise AssertionError("expected SystemExit when --output passed without --format")


def test_share_validate_args_rejects_copy_without_format():
    import argparse
    args = argparse.Namespace(
        format=None, output=None, copy=True, open_after_write=False,
    )
    try:
        _cctally._share_validate_args(args)
    except SystemExit as e:
        assert e.code == 2
        return
    raise AssertionError("expected SystemExit when --copy passed without --format")


def test_share_validate_args_rejects_open_without_format():
    import argparse
    args = argparse.Namespace(
        format=None, output=None, copy=False, open_after_write=True,
    )
    try:
        _cctally._share_validate_args(args)
    except SystemExit as e:
        assert e.code == 2
        return
    raise AssertionError("expected SystemExit when --open passed without --format")


def test_share_validate_args_rejects_copy_with_output():
    """--copy + --output is a destination mutex; must reject early."""
    import argparse
    args = argparse.Namespace(
        format="md", output="/tmp/x.md", copy=True, open_after_write=False,
    )
    try:
        _cctally._share_validate_args(args)
    except SystemExit as e:
        assert e.code == 2
        return
    raise AssertionError("expected SystemExit on --copy + --output mutex")


def test_share_validate_args_rejects_copy_with_non_md():
    """--copy clipboard write is only meaningful for md format."""
    import argparse
    args = argparse.Namespace(
        format="svg", output=None, copy=True, open_after_write=False,
    )
    try:
        _cctally._share_validate_args(args)
    except SystemExit as e:
        assert e.code == 2
        return
    raise AssertionError("expected SystemExit on --copy + --format svg")


def test_share_validate_args_rejects_open_with_md():
    """--open is only meaningful for html/svg writes (md routes to stdout)."""
    import argparse
    args = argparse.Namespace(
        format="md", output=None, copy=False, open_after_write=True,
    )
    try:
        _cctally._share_validate_args(args)
    except SystemExit as e:
        assert e.code == 2
        return
    raise AssertionError("expected SystemExit on --open + --format md")


def test_share_validate_args_rejects_open_with_stdout_output():
    """--open --output - is a silent no-op pre-fix; now an explicit exit 2."""
    import argparse
    args = argparse.Namespace(
        format="html", output="-", copy=False, open_after_write=True,
    )
    try:
        _cctally._share_validate_args(args)
    except SystemExit as e:
        assert e.code == 2
        return
    raise AssertionError("expected SystemExit on --open + --output -")


def test_share_validate_args_accepts_open_with_file_output():
    """--open with a real file path (html/svg) is the happy path."""
    import argparse
    args = argparse.Namespace(
        format="html", output="/tmp/report.html", copy=False, open_after_write=True,
    )
    # Must not raise / exit.
    _cctally._share_validate_args(args)


def test_report_builder_renders_none_metrics_as_em_dash():
    """Missing weeklyPercent / weeklyCostUSD / dollarsPerPercent must render
    as TextCell("—") in the share table — parity with terminal's em-dash
    convention. Coercing None to 0.0 conflates missing data with genuine zero.
    """
    rows = [
        # Week with all metrics present.
        {"weekStartDate": "2026-04-13", "weeklyPercent": 65.2,
         "weeklyCostUSD": 35.40, "dollarsPerPercent": 0.54},
        # Week with NO usage snapshot — all metrics None.
        {"weekStartDate": "2026-04-20", "weeklyPercent": None,
         "weeklyCostUSD": None, "dollarsPerPercent": None},
        # Week with cost recorded but no usage snapshot — used_pct/dpp None.
        {"weekStartDate": "2026-04-27", "weeklyPercent": None,
         "weeklyCostUSD": 12.50, "dollarsPerPercent": None},
    ]
    snap = _cctally._build_report_snapshot(
        rows,
        period_start=datetime(2026, 4, 13, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 4, tzinfo=timezone.utc),
        display_tz="UTC",
        version="9.9.9",
        theme="light",
        reveal_projects=False,
    )
    assert len(snap.rows) == 3
    # Row 0: all metrics present.
    r0 = snap.rows[0]
    assert isinstance(r0.cells["used"], _lib_share.PercentCell)
    assert isinstance(r0.cells["cost"], _lib_share.MoneyCell)
    assert isinstance(r0.cells["dpp"], _lib_share.MoneyCell)
    # Row 1: all None → em-dash on every metric column.
    r1 = snap.rows[1]
    for col in ("used", "cost", "dpp"):
        assert isinstance(r1.cells[col], _lib_share.TextCell), col
        assert r1.cells[col].text == "—"
    # Row 2: cost present, others None.
    r2 = snap.rows[2]
    assert isinstance(r2.cells["cost"], _lib_share.MoneyCell)
    assert isinstance(r2.cells["used"], _lib_share.TextCell)
    assert isinstance(r2.cells["dpp"], _lib_share.TextCell)


def test_report_builder_skips_none_dpp_from_chart_and_avg():
    """Chart points with None dpp must be skipped, not rendered as 0.

    Otherwise the line chart drops to 0 at that point (visually misleading)
    and the avg_dpp is divided by an inflated count. Verify both: chart
    has the correct number of points AND the Avg total averages over only
    present samples.
    """
    rows = [
        {"weekStartDate": f"2026-04-{day:02d}",
         "weeklyPercent": 50.0, "weeklyCostUSD": 10.0, "dollarsPerPercent": 0.20}
        for day in (6, 13, 20, 27)
    ]
    # Inject a None-dpp week in the middle.
    rows[2]["dollarsPerPercent"] = None
    rows[2]["weeklyCostUSD"] = None
    rows[2]["weeklyPercent"] = None
    snap = _cctally._build_report_snapshot(
        rows,
        period_start=datetime(2026, 4, 6, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 4, tzinfo=timezone.utc),
        display_tz="UTC", version="9.9.9", theme="light",
        reveal_projects=False,
    )
    # Chart has 3 points (skipped the None middle row).
    assert snap.chart is not None
    assert len(snap.chart.points) == 3
    # Avg label: "Avg $/%" totalled over the 3 present samples = 0.20.
    avg = next(t for t in snap.totals if t.label == "Avg $/%")
    assert avg.value == "$0.20"


def test_weekly_builder_renders_none_used_pct_as_em_dash():
    """Weekly --breakdown share table must em-dash missing overlay used_pct.

    `BucketUsage.cost_usd` is genuinely 0 when there are no entries (not
    missing), so cost cells stay MoneyCell. Only the overlay-provided
    `used_pct` carries the missing-vs-zero distinction.
    """
    BucketUsage = _cctally.BucketUsage
    buckets = [
        BucketUsage(
            bucket="2026-04-13", input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0, total_tokens=0,
            cost_usd=10.0, models=["m"], model_breakdowns=[],
        ),
        BucketUsage(
            bucket="2026-04-20", input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0, total_tokens=0,
            cost_usd=12.0, models=["m"], model_breakdowns=[],
        ),
    ]
    overlay = [(50.0, 0.20), (None, None)]  # second week missing snapshot
    snap = _cctally._build_weekly_snapshot(
        buckets, overlay,
        period_start=datetime(2026, 4, 13, tzinfo=timezone.utc),
        period_end=datetime(2026, 4, 27, tzinfo=timezone.utc),
        display_tz="UTC", version="9.9.9", theme="light",
        reveal_projects=False, breakdown_model=False,
    )
    assert isinstance(snap.rows[0].cells["used"], _lib_share.PercentCell)
    assert isinstance(snap.rows[1].cells["used"], _lib_share.TextCell)
    assert snap.rows[1].cells["used"].text == "—"
    # Cost cell stays MoneyCell (0 cost is real, not missing).
    assert isinstance(snap.rows[1].cells["cost"], _lib_share.MoneyCell)


def test_project_builder_renders_none_attributed_pct_as_em_dash():
    """Project share table must em-dash missing attributed_pct."""
    ProjectKey = _cctally.ProjectKey
    rows = [
        {"key": ProjectKey(display_key="alpha", bucket_path="/x/alpha", git_root=None),
         "cost_usd": 0.05, "attributed_pct": 12.5, "sessions": {"s1"}},
        {"key": ProjectKey(display_key="beta", bucket_path="/x/beta", git_root=None),
         "cost_usd": 0.03, "attributed_pct": None, "sessions": {"s2"}},
    ]
    snap = _cctally._build_project_snapshot(
        rows,
        period_start=datetime(2026, 5, 4, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 11, tzinfo=timezone.utc),
        display_tz="UTC", version="9.9.9", theme="light", reveal_projects=True,
    )
    # Cost-desc default: alpha first, beta second.
    assert isinstance(snap.rows[0].cells["used"], _lib_share.PercentCell)
    assert isinstance(snap.rows[1].cells["used"], _lib_share.TextCell)
    assert snap.rows[1].cells["used"].text == "—"


def test_project_builder_preserves_caller_order_for_table():
    """`_build_project_snapshot` must not re-sort the table rows.

    Regression: prior version sorted-by-cost-desc internally, ignoring
    `--sort name` / `--order asc` from the caller. Chart points stay
    cost-sorted (anonymization rank stability) — verified separately.
    """
    ProjectKey = _cctally.ProjectKey
    # Caller order: alphabetical asc (alpha, beta, gamma) — does NOT match
    # cost-desc order (gamma=$5, beta=$3, alpha=$1).
    rows = [
        {"key": ProjectKey(display_key="alpha", bucket_path="/x/alpha", git_root=None),
         "cost_usd": 1.0, "attributed_pct": 10.0, "sessions": {"s1"}},
        {"key": ProjectKey(display_key="beta", bucket_path="/x/beta", git_root=None),
         "cost_usd": 3.0, "attributed_pct": 30.0, "sessions": {"s2"}},
        {"key": ProjectKey(display_key="gamma", bucket_path="/x/gamma", git_root=None),
         "cost_usd": 5.0, "attributed_pct": 50.0, "sessions": {"s3"}},
    ]
    snap = _cctally._build_project_snapshot(
        rows,
        period_start=datetime(2026, 5, 4, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 11, tzinfo=timezone.utc),
        display_tz="UTC", version="9.9.9", theme="light", reveal_projects=True,
    )
    # Table rows preserve caller (alphabetical asc) order.
    table_labels = [r.cells["project"].label for r in snap.rows]
    assert table_labels == ["alpha", "beta", "gamma"]
    # Chart points are cost-desc (gamma > beta > alpha) — anonymization
    # rank invariant. project_label is REAL pre-scrub.
    chart_labels = [p.project_label for p in snap.chart.points]
    assert chart_labels == ["gamma", "beta", "alpha"]


def test_copy_falls_back_when_no_clipboard_tool(monkeypatch):
    """If no pbcopy/xclip/clip on PATH, --copy must error clearly.

    Test-spec adjustment vs. plan: Implementor 6's fix-loop dropped the
    unused `fmt` parameter from `_emit`; the plan's call site passed
    `fmt="md"`, which would now TypeError. Current `_emit` signature is
    `_emit(content, *, kind, value)` (bin/cctally:24582).
    """
    monkeypatch.setenv("PATH", "/nonexistent")
    try:
        _cctally._emit("hello", kind="clipboard", value=None)
    except SystemExit as e:
        # _emit prints "cctally: --copy requires pbcopy, xclip, or clip
        # on PATH" to stderr and sys.exit(2). The exit code is the
        # stable contract; the message text is captured in stderr.
        assert e.code == 2
        return
    raise AssertionError("expected SystemExit when no clipboard tool present")
