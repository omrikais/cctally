"""Layer A unit tests for bin/_lib_share.py."""
from __future__ import annotations

import importlib.util
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
        "ref_warn", "ref_alarm",
        "table_header_bg", "table_row_alt", "footer_link",
    }
    assert set(_lib_share.PALETTE_LIGHT.keys()) >= required_keys
    assert set(_lib_share.PALETTE_DARK.keys()) >= required_keys
    # Palettes must differ on at least the bg color.
    assert _lib_share.PALETTE_LIGHT["bg"] != _lib_share.PALETTE_DARK["bg"]


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
