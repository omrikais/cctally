"""Layer A unit tests for bin/_lib_share.py."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import sys
from datetime import datetime, timezone

import pytest

# Every HOME-derived path constant resolves under a per-test directory
# (#529 S4). This module loads bin/cctally (or a sibling) through
# SourceFileLoader or at import, and neither re-derives the path constants,
# so pinning HOME alone would leave whatever the previous test on this xdist
# worker left behind -- which is why it was green alone and red under -n 4.
pytestmark = pytest.mark.usefixtures("isolated_paths")

# Load _lib_share by path (same pattern bin/cctally uses for its peers).
# Reuse an already-loaded module if a peer test file (e.g.
# `tests/test_lib_share_v2.py`) registered one — otherwise the LAST loader
# wins for `sys.modules["_lib_share"]`, and `_lib_share_templates._LS`
# (bound at templates' module-load time) and the `bin/cctally` API
# handler's `_share_load_lib()` end up pointing at *different* module
# objects depending on import order. That breaks `isinstance(cell,
# TextCell)` in the kernel renderer when cells are constructed by the
# templates and rendered through the API handler. Matching v2's
# get-or-load pattern keeps a single module identity across the entire
# pytest session.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LIB_SHARE_PATH = _REPO_ROOT / "bin" / "_lib_share.py"
if "_lib_share" in sys.modules:
    _lib_share = sys.modules["_lib_share"]
else:
    _spec = importlib.util.spec_from_file_location("_lib_share", _LIB_SHARE_PATH)
    _lib_share = importlib.util.module_from_spec(_spec)
    # Register in sys.modules BEFORE exec_module: Python 3.14's `dataclass`
    # decorator looks up `cls.__module__` in `sys.modules` for KW_ONLY type
    # checks during class processing, which fails if the module isn't
    # registered yet.
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
    out = _lib_share.render(snap, format="md", theme="light", branding=True, reveal_projects=True)
    assert isinstance(out, str)
    assert snap.title in out


def test_render_unknown_format_raises():
    snap = _make_minimal_snapshot()
    try:
        _lib_share.render(snap, format="pdf", theme="light", branding=True, reveal_projects=True)
    except ValueError as e:
        assert "format" in str(e).lower()
        return
    raise AssertionError("expected ValueError on unknown format")


def test_render_dispatches_svg():
    snap = _make_minimal_snapshot()
    out = _lib_share.render(snap, format="svg", theme="light", branding=True, reveal_projects=True)
    assert isinstance(out, str)
    assert "<svg" in out
    # Title escaped into the SVG comment by the stub.
    assert _lib_share._xml_escape(snap.title) in out


def test_render_dispatches_html():
    snap = _make_minimal_snapshot()
    out = _lib_share.render(snap, format="html", theme="dark", branding=True, reveal_projects=True)
    assert isinstance(out, str)
    assert "<!DOCTYPE html" in out
    assert _lib_share._xml_escape(snap.title) in out


def test_render_unknown_theme_raises():
    snap = _make_minimal_snapshot()
    try:
        _lib_share.render(snap, format="svg", theme="solarized", branding=True, reveal_projects=True)
    except ValueError as e:
        assert "theme" in str(e).lower()
        return
    raise AssertionError("expected ValueError on unknown theme")


# ---------------------------------------------------------------------
# show_chart / show_table toggles drop the chart wrapper and table chrome
# rather than emitting empty `<svg>` chart areas / empty `<table>` chrome
# in HTML output. The toggle is applied upstream by
# `_share_apply_content_toggles`; this test pins the renderer side of
# the contract — chart=None must mean "no chart wrapper", columns=() must
# mean "no table at all" — so a future inversion (e.g. emitting a
# placeholder rect for "the user disabled the chart") is caught.
# ---------------------------------------------------------------------

import dataclasses as _dc  # noqa: E402 — used by the toggle-gating tests below


def test_render_html_omits_chart_wrapper_when_chart_none():
    snap = _dc.replace(_make_minimal_snapshot(), chart=None)
    out = _lib_share.render(snap, format="html", theme="light", branding=True, reveal_projects=True)
    # The chart wrapper div has `margin-top:12px` and contains an `<svg`
    # element — emitting the wrapper means the renderer leaked the
    # empty chart area into the document.
    assert "<svg" not in out, "HTML body should not contain <svg> when chart is None"


def test_render_html_omits_table_when_columns_empty():
    snap = _dc.replace(_make_minimal_snapshot(), columns=(), rows=())
    out = _lib_share.render(snap, format="html", theme="light", branding=True, reveal_projects=True)
    # The HTML table chrome always starts with `<table style=…><thead>…`;
    # gating on `snap.columns` drops the whole element rather than
    # emitting empty `<thead><tr></tr></thead><tbody></tbody>`.
    assert "<table" not in out, "HTML body should not contain <table> when columns are empty"


def test_render_html_emits_chart_when_chart_present():
    """Sanity: gating doesn't drop content when toggles are on."""
    snap = _dc.replace(
        _make_minimal_snapshot(),
        chart=LineChart(
            points=(ChartPoint(x_label="0", x_value=0.0, y_value=1.0),),
            y_label="$",
            reference_lines=(),
        ),
    )
    out = _lib_share.render(snap, format="html", theme="light", branding=True, reveal_projects=True)
    assert "<svg" in out
    assert "<table" in out


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


def test_line_chart_right_edge_xtick_label_right_anchored():
    """Right-most x-tick label is right-anchored so it doesn't clip the viewBox (#215).

    The right-most sample lands at the inner-box right edge (ix + iw). A
    centered (anchor="middle") label there overflows the chart's right padding
    and is clipped at the SVG viewBox boundary — most visible for wide labels
    (10-char ISO dates, as the `$ / day` current-week chart uses) at narrow
    render widths. The edge tick is right-aligned (anchor="end") so its full
    width stays inside the plot; interior + left-edge ticks stay centered.
    """
    import re
    chart = LineChart(
        points=tuple(
            ChartPoint(x_label=f"2026-04-2{i}", x_value=float(i), y_value=float(i + 1))
            for i in range(7)
        ),
        y_label="$ / day",
    )
    out = _lib_share._render_line_chart_svg(
        chart, palette=_lib_share.PALETTE_LIGHT,
        x=20, y=20, width=600, height=220,
    )
    # ix = 20 + 50 = 70, iw = 600 - 50 - 10 = 540 → right edge at x = 610.
    # svg_text serializes attrs lexically: fill, font-size, text-anchor, x, y,
    # so text-anchor precedes x in every tick tag.
    ticks = re.findall(
        r'<text\b[^>]*text-anchor="(?P<anchor>[^"]+)"[^>]*\bx="(?P<x>[\d.]+)"[^>]*>'
        r'(?P<label>2026-04-2\d)</text>',
        out,
    )
    by_label = {label: (anchor, float(x)) for anchor, x, label in ticks}
    assert set(by_label) == {f"2026-04-2{i}" for i in range(7)}, by_label
    # Right-most tick sits exactly at the inner-box right edge and must be
    # end-anchored.
    assert abs(by_label["2026-04-26"][1] - 610.0) < 1e-6, by_label
    assert by_label["2026-04-26"][0] == "end", \
        f"right-edge tick must be anchor=end, got {by_label['2026-04-26']}"
    # Interior and left-edge ticks stay centered.
    assert by_label["2026-04-20"][0] == "middle", by_label  # left edge (x=70)
    assert by_label["2026-04-23"][0] == "middle", by_label  # interior


def test_svg_chrome_header_includes_title_subtitle_timestamp():
    snap = _make_minimal_snapshot()
    out = _lib_share._render_svg_header(snap, palette=_lib_share.PALETTE_LIGHT,
                                        x=20, y=20, width=560,
                                        shows_table=True)
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
    # Explicit include_table=False — name's contract ("chart_only_omits_chrome")
    # demands neither chrome nor table. SVG learned to render tables in #38,
    # so the chart-only embed (used by _render_html_fragment) must opt out.
    out = _lib_share._render_svg(snap, palette=_lib_share.PALETTE_LIGHT,
                                 branding=True, include_chrome=False,
                                 include_table=False)
    # Title and footer-link absent in chart-only mode.
    assert _lib_share._xml_escape(snap.title) not in out
    assert "Generated by cctally" not in out
    assert out.startswith('<svg')


def test_render_svg_chart_only_with_and_without_table():
    """Lock the two calling conventions of `include_chrome=False` (#38).

    The minimal snapshot has 2 columns + 1 row + chart=None, so the table
    path emits its header band + body row when `include_table` defaults
    to True. Passing `include_table=False` opts out of both chrome AND
    the table — the contract used by `_render_html_fragment`'s
    chart-only embed call to keep the HTML wrapper's standalone
    `<table>` from being duplicated inside the embedded chart SVG.
    """
    snap = _make_minimal_snapshot()

    # 1. include_chrome=False, include_table=False → no chrome, no table.
    no_table = _lib_share._render_svg(
        snap, palette=_lib_share.PALETTE_LIGHT,
        branding=True, include_chrome=False, include_table=False,
    )
    assert "Generated by cctally" not in no_table  # no chrome footer
    assert _lib_share._xml_escape(snap.title) not in no_table  # no chrome header
    # No table elements: header-band fill + cell text from columns must be absent.
    # The minimal snapshot's columns label "Week" / "$ Cost" — no SVG `<text>`
    # node should render either label.
    assert ">Week<" not in no_table
    assert ">$ Cost<" not in no_table
    # No table-header band rect (palette table_header_bg = "#f3f4f6" in light).
    assert _lib_share.PALETTE_LIGHT["table_header_bg"] not in no_table

    # 2. include_chrome=False (default include_table=True) → no chrome, BUT table present.
    with_table = _lib_share._render_svg(
        snap, palette=_lib_share.PALETTE_LIGHT,
        branding=True, include_chrome=False,
    )
    assert "Generated by cctally" not in with_table  # still no chrome
    assert _lib_share._xml_escape(snap.title) not in with_table  # still no chrome header
    # Table elements present: column labels + header-band fill.
    assert ">Week<" in with_table
    assert ">$ Cost<" in with_table
    assert _lib_share.PALETTE_LIGHT["table_header_bg"] in with_table


def test_render_svg_chart_only_with_no_chart_returns_empty_svg():
    # `include_table=False` keeps this test honest to its name: with
    # only `include_chrome=False` (table defaulting True) the snapshot's
    # 2-column table would emit and "empty SVG" would no longer be
    # accurate. The contract proven here: chart=None + chrome=False +
    # table=False → no <polyline> chart geometry.
    snap = _make_minimal_snapshot()  # chart=None
    out = _lib_share._render_svg(snap, palette=_lib_share.PALETTE_LIGHT,
                                 branding=True, include_chrome=False,
                                 include_table=False)
    inner = out[len('<svg xmlns="http://www.w3.org/2000/svg" '):]
    assert "<polyline" not in inner


def test_render_html_wraps_chart_only_svg():
    # Use a chart-bearing snap — the post-toggle renderer gates the chart
    # wrapper on `snap.chart is not None` (chart=None drops the wrapper
    # entirely; covered by `test_render_html_omits_chart_wrapper_when_chart_none`).
    snap = _dc.replace(
        _make_minimal_snapshot(),
        chart=LineChart(
            points=(ChartPoint(x_label="0", x_value=0.0, y_value=1.0),),
            y_label="$",
            reference_lines=(),
        ),
    )
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


def test_notes_render_visibly_in_every_artifact_format():
    base = _make_minimal_snapshot()
    snap = _lib_share.ShareSnapshot(
        **{
            **base.__dict__,
            "notes": (
                "Codex current-week spend is based on stale provider-cycle evidence.",
            ),
        },
    )
    for format in ("md", "html", "svg"):
        out = _lib_share.render(
            snap, format=format, theme="light", branding=False,
            reveal_projects=True,
        )
        assert (
            "Codex current-week spend is based on stale provider-cycle evidence."
            in out
        )


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
        out = _lib_share.render(snap, format=fmt, theme="light",
                                branding=True, reveal_projects=False)
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
        out = _lib_share.render(snap, format=fmt, theme="light",
                                branding=True, reveal_projects=False)
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
    """Privacy regression: the wrapper must reach the anonymizing chokepoint
    when reveal_projects=False.

    Since #503 S1 that chokepoint is `render()`'s preparation pass, not
    `_scrub`. A regression that stops reaching it (refactoring
    _share_render_and_emit so it renders without passing reveal_projects
    through, say) would not surface in the existing md/html-routing tests
    because the minimal snapshot has no project cells. Any future refactor
    that drops or short-circuits anonymization must fail here — the original
    project name leaks into stdout.
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
        "wrapper bypassed anonymization: original project name leaked to output"
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
    out_dark = _lib_share.render(snap, format="svg", theme="dark", branding=True, reveal_projects=True)
    out_light = _lib_share.render(snap, format="svg", theme="light", branding=True, reveal_projects=True)
    assert _lib_share.PALETTE_DARK["bg"] in out_dark
    assert _lib_share.PALETTE_LIGHT["bg"] in out_light
    assert out_dark != out_light


def test_dark_theme_uses_dark_palette_in_html():
    snap = _make_minimal_snapshot()
    out_dark = _lib_share.render(snap, format="html", theme="dark", branding=True, reveal_projects=True)
    out_light = _lib_share.render(snap, format="html", theme="light", branding=True, reveal_projects=True)
    assert _lib_share.PALETTE_DARK["bg"] in out_dark
    assert _lib_share.PALETTE_LIGHT["bg"] in out_light


def test_md_theme_is_noop():
    """Markdown is theme-agnostic — rendered output is identical for light/dark."""
    snap = _make_minimal_snapshot()
    light = _lib_share.render(snap, format="md", theme="light", branding=True, reveal_projects=True)
    dark = _lib_share.render(snap, format="md", theme="dark", branding=True, reveal_projects=True)
    assert light == dark


def test_no_branding_strips_footer_in_all_formats():
    snap = _make_minimal_snapshot()
    for fmt in ("md", "svg", "html"):
        with_branding = _lib_share.render(snap, format=fmt, theme="light", branding=True, reveal_projects=True)
        without_branding = _lib_share.render(snap, format=fmt, theme="light", branding=False, reveal_projects=True)
        assert "Generated by" in with_branding or "cctally" in with_branding, fmt
        assert "Generated by" not in without_branding, fmt


def test_html_chrome_appears_exactly_once_with_branding():
    """Chrome ownership invariant (Codex finding M5)."""
    snap = _make_minimal_snapshot()
    out = _lib_share.render(snap, format="html", theme="light", branding=True, reveal_projects=True)
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


def test_stacked_bar_legend_is_reserved_outside_the_plot():
    """The legend key must not be painted over the bars it identifies."""
    import xml.etree.ElementTree as ET

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
    root = ET.fromstring(f"<svg>{out}</svg>")
    bars = [
        node for node in root.iter("rect")
        if float(node.attrib["width"]) > 8.0
    ]
    legend_labels = [
        node for node in root.iter("text")
        if (node.text or "") in chart.stacks
    ]
    assert len(bars) == 4
    assert len(legend_labels) == 2
    assert max(float(node.attrib["y"]) for node in legend_labels) < min(
        float(node.attrib["y"]) for node in bars
    )


def test_hbar_chart_grows_to_keep_eighteen_labels_comfortably_spaced():
    """Detail templates may draw 18+ rows; the fixed 220px slot crowded them."""
    import dataclasses
    import xml.etree.ElementTree as ET

    chart = HorizontalBarChart(
        points=tuple(
            ChartPoint(
                x_label=f"session-{index:02d}",
                x_value=float(index),
                y_value=float(18 - index),
            )
            for index in range(18)
        ),
        x_label="$",
        cap=None,
    )
    snap = dataclasses.replace(
        _make_minimal_snapshot(),
        columns=(), rows=(), chart=chart, totals=(),
    )
    out = _lib_share._render_svg(
        snap,
        palette=_lib_share.PALETTE_LIGHT,
        branding=False,
        include_chrome=False,
        include_table=False,
    )
    root = ET.fromstring(out)
    baselines = sorted(
        float(node.attrib["y"])
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] == "text"
        and (node.text or "").startswith("session-")
    )
    assert len(baselines) == 18
    assert min(b - a for a, b in zip(baselines, baselines[1:])) >= 14.0


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

    View-model unification (Bundle 1): `_build_report_snapshot` now
    consumes a `TrendView` of typed `TuiTrendRow` instances instead of
    a `list[dict]`. Construct the rows directly so the builder reads
    them by attribute.
    """
    TuiTrendRow = _cctally.TuiTrendRow
    TrendView = _cctally.TrendView
    base = datetime(2026, 4, 13, tzinfo=timezone.utc)
    trend_rows = (
        # Week with all metrics present.
        TuiTrendRow(
            week_label="Apr 13", week_start_at=base,
            used_pct=65.2, dollars_per_percent=0.54, delta_dpp=None,
            spark_height=1, is_current=False,
            week_start_date=datetime(2026, 4, 13).date(),
            weekly_cost_usd=35.40,
        ),
        # Week with NO usage snapshot — all metrics None.
        TuiTrendRow(
            week_label="Apr 20",
            week_start_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
            used_pct=None, dollars_per_percent=None, delta_dpp=None,
            spark_height=1, is_current=False,
            week_start_date=datetime(2026, 4, 20).date(),
            weekly_cost_usd=None,
        ),
        # Week with cost recorded but no usage snapshot.
        TuiTrendRow(
            week_label="Apr 27",
            week_start_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
            used_pct=None, dollars_per_percent=None, delta_dpp=None,
            spark_height=1, is_current=False,
            week_start_date=datetime(2026, 4, 27).date(),
            weekly_cost_usd=12.50,
        ),
    )
    view = TrendView(rows=trend_rows, avg_dollars_per_pct=None)
    snap = _cctally._build_report_snapshot(
        view,
        period_start=datetime(2026, 4, 13, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 4, tzinfo=timezone.utc),
        display_tz="UTC",
        version="9.9.9",
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
    TuiTrendRow = _cctally.TuiTrendRow
    TrendView = _cctally.TrendView
    trend_rows = []
    for day in (6, 13, 20, 27):
        trend_rows.append(TuiTrendRow(
            week_label=f"Apr {day:02d}",
            week_start_at=datetime(2026, 4, day, tzinfo=timezone.utc),
            used_pct=50.0, dollars_per_percent=0.20, delta_dpp=None,
            spark_height=1, is_current=False,
            week_start_date=datetime(2026, 4, day).date(),
            weekly_cost_usd=10.0,
        ))
    # Inject a None-dpp week in the middle (index 2 = day 20).
    trend_rows[2] = _dc.replace(
        trend_rows[2],
        used_pct=None, dollars_per_percent=None, weekly_cost_usd=None,
    )
    # 3 valid dpp samples → builder's avg path uses view.avg_dollars_per_pct
    # (3-sample rule). Mean = 0.20.
    view = TrendView(
        rows=tuple(trend_rows), avg_dollars_per_pct=0.20,
    )
    snap = _cctally._build_report_snapshot(
        view,
        period_start=datetime(2026, 4, 6, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 4, tzinfo=timezone.utc),
        display_tz="UTC", version="9.9.9",)
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

    View-model unification (Bundle 1): `_build_weekly_snapshot` now
    consumes a `WeeklyView` instead of `(buckets, overlay)`. The
    builder's `view.aggregated` is newest-first; we provide it
    accordingly so the builder's `reversed` step yields the asc
    order the test fixtures expect.
    """
    BucketUsage = _cctally.BucketUsage
    WeeklyView = _cctally.WeeklyView
    # The builder iterates view.aggregated reversed (newest-first ->
    # asc); we pass newest-first.
    aggregated_newest_first = (
        BucketUsage(
            bucket="2026-04-20", input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0, total_tokens=0,
            cost_usd=12.0, models=["m"], model_breakdowns=[],
        ),
        BucketUsage(
            bucket="2026-04-13", input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0, total_tokens=0,
            cost_usd=10.0, models=["m"], model_breakdowns=[],
        ),
    )
    # Overlay parallel to aggregated (newest-first): second week
    # (2026-04-20) missing snapshot.
    overlay_newest_first = ((None, None), (50.0, 0.20))
    view = WeeklyView(
        rows=(), aggregated=aggregated_newest_first,
        overlay=overlay_newest_first,
        total_cost_usd=22.0, total_tokens=0,
        period_start=datetime(2026, 4, 13, tzinfo=timezone.utc),
        period_end=datetime(2026, 4, 27, tzinfo=timezone.utc),
        display_tz_label="UTC",
    )
    snap = _cctally._build_weekly_snapshot(
        view,
        period_start=datetime(2026, 4, 13, tzinfo=timezone.utc),
        period_end=datetime(2026, 4, 27, tzinfo=timezone.utc),
        display_tz="UTC", version="9.9.9", breakdown_model=False,
        since_explicit=True,
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
        display_tz="UTC", version="9.9.9",)
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
        display_tz="UTC", version="9.9.9",)
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


# ---------------------------------------------------------------------
# #503 S1 Task A1 — explicit chart-axis discriminator.
#
# Before this, `_apply_anon_mapping` decided whether an `x_label` was a
# project axis by testing `x_label == project_label`. That is a guess, and
# the two `sessions` builders deliberately violate it (they put a session
# id in `x_label` and the project path in `project_label`), which is the
# proximate cause of F1: the scrubber rewrote the invisible field and left
# the visible one alone.
# ---------------------------------------------------------------------

def test_chart_point_defaults_to_plain_axis():
    p = _lib_share.ChartPoint(x_label="2026-05-07", x_value=0.0, y_value=1.0)
    assert p.x_label_kind == "plain"
    assert p.x_label_prefix is None


def _chart_point_arg_blocks(src: str):
    """Yield the full argument text of every `ChartPoint(...)` call.

    Paren-balanced rather than a non-greedy regex: `x_label=str(s.get("id"))`
    closes a paren before the call does, so a `.*?\\)` capture would truncate
    the block and hide the `x_label_kind=` keyword that follows.
    """
    import re as _re
    for m in _re.finditer(r"ChartPoint\(", src):
        i = m.end()
        depth = 1
        while i < len(src) and depth:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
            i += 1
        yield src[m.end():i - 1]


def _chart_point_kwarg(block: str, name: str) -> "str | None":
    """Extract one keyword argument's VALUE from a `ChartPoint(...)` block.

    Splitting on the first comma only reads the FIRST argument, so a site
    that put `x_value=` ahead of `x_label=` slipped the scan silently. This
    finds the named keyword wherever it appears and returns its value up to
    the next TOP-LEVEL comma, so `x_label=str(s.get("a", "b"))` is not cut in
    half at the nested comma.
    """
    import re as _re
    m = _re.search(rf"\b{name}\s*=", block)
    if not m:
        return None
    i = start = m.end()
    depth = 0
    while i < len(block):
        c = block[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            break
        i += 1
    return block[start:i].strip()


def _share_source_files():
    """Every source file in `bin/`, including the extensionless entry point.

    A `bin/*.py` glob misses `bin/cctally`, which is a real Python module and
    a real re-export surface — structural scans have silently skipped it
    before.
    """
    import pathlib as _pl
    repo = _pl.Path(__file__).resolve().parent.parent
    return sorted(
        p for p in (repo / "bin").iterdir()
        if p.is_file() and (p.suffix == ".py" or p.name == "cctally")
    )


# A project- or session-derived `x_label`. Applied to the EXTRACTED value of
# the `x_label` keyword, never to the whole argument block: a legitimate
# date axis may carry `project_label=` alongside a plain `x_label`, so a
# whole-block match would flag correct code and train the next reader to
# ignore this test.
_PROJECT_DERIVED_X_LABEL = r"project|proj_|session|sess_|^label$|^name$|^key$"

# A date-derived `x_label`. Used only to EXEMPT a site from the structural arm
# below, never to flag one. A chart whose gutter is a calendar axis may carry
# `project_label=` alongside it for the tooltip, and that combination is
# correct code.
_DATE_DERIVED_X_LABEL = r"date|month|week|day|hour|start_at|time|period"


def _project_axis_offenders():
    """Every ChartPoint construction that must carry `x_label_kind` and does
    not. Shared by the guard and its own non-vacuity check.

    TWO ARMS, because the name-based arm alone is not sufficient. It reads the
    EXPRESSION assigned to `x_label`, so it only fires when that expression is
    spelled with a project-ish word. `bin/_lib_share_templates.py`'s two
    sessions builders assign `str(i + 1)` — the cost rank — which names
    nothing, so the name arm does not reach them even though both are
    project-keyed axes and both carry `project_label=`. Deleting the marker
    from either left the guard green, which is the defect this second arm
    closes.

    Arm 1, by name: an `x_label` expression naming a project or session
    identity must be marked `"project"` specifically.

    Arm 2, structural: a construction that sets `project_label` at all, and
    whose `x_label` is not a calendar expression, must set `x_label_kind`.
    Presence rather than the literal `"project"`, because the preparation pass
    in `bin/_lib_share.py` rebuilds points with `x_label_kind=p.x_label_kind`
    — forwarding the discriminator is what that site owes, and pinning the
    literal there would demand it hardcode a value it must propagate.
    """
    import re as _re
    offenders = []
    for path in _share_source_files():
        src = path.read_text(encoding="utf-8", errors="replace")
        for block in _chart_point_arg_blocks(src):
            value = _chart_point_kwarg(block, "x_label")
            if not value:
                continue
            kind = _chart_point_kwarg(block, "x_label_kind")
            if _re.search(_PROJECT_DERIVED_X_LABEL, value):
                if kind != '"project"':
                    offenders.append((path.name, "named", block.strip()[:80]))
                continue
            project_label = _chart_point_kwarg(block, "project_label")
            if not project_label or project_label == "None":
                continue
            if _re.search(_DATE_DERIVED_X_LABEL, value):
                continue
            if kind is None:
                offenders.append((path.name, "structural", block.strip()[:80]))
    return offenders


def test_no_project_derived_axis_is_left_plain():
    """Every ChartPoint construction whose x_label comes from a project or
    session identity must be marked project-keyed. Guards the F1 class:
    a new project axis added later without the marker cannot be scrubbed.

    Scans ALL of `bin/`, not the two modules that happen to hold the sites
    today — a project-keyed axis built in a third module would otherwise
    pass silently.
    """
    offenders = _project_axis_offenders()
    assert offenders == [], f"project-derived x_label left plain: {offenders}"


def test_the_structural_arm_covers_the_two_sessions_builders():
    """Non-vacuity of arm 2 against the real tree, not a synthetic source.

    Both sessions builders assign the cost rank to `x_label`, so arm 1 cannot
    see them. If this count ever falls to zero, arm 2 has stopped reaching the
    sites it was written for and `test_no_project_derived_axis_is_left_plain`
    is back to guarding three sites out of five.
    """
    import re as _re
    reached = 0
    for path in _share_source_files():
        src = path.read_text(encoding="utf-8", errors="replace")
        for block in _chart_point_arg_blocks(src):
            value = _chart_point_kwarg(block, "x_label")
            if not value or _re.search(_PROJECT_DERIVED_X_LABEL, value):
                continue
            project_label = _chart_point_kwarg(block, "project_label")
            if not project_label or project_label == "None":
                continue
            if _re.search(_DATE_DERIVED_X_LABEL, value):
                continue
            reached += 1
    assert reached >= 3, (
        "arm 2 reaches only %d sites; it was written to cover the two "
        "sessions builders plus the preparation rewrite" % reached)


def test_the_project_axis_scan_actually_matches_something():
    """A scan that matched nothing would pass vacuously forever.

    Pins that the scan finds ChartPoint sites at all, and that it classifies
    both a known project-keyed axis and a known date axis correctly.
    """
    import re as _re
    total = 0
    project_axes = 0
    plain_axes = 0
    for path in _share_source_files():
        src = path.read_text(encoding="utf-8", errors="replace")
        for block in _chart_point_arg_blocks(src):
            value = _chart_point_kwarg(block, "x_label")
            if not value:
                continue
            total += 1
            if _re.search(_PROJECT_DERIVED_X_LABEL, value):
                project_axes += 1
            elif _chart_point_kwarg(block, "x_label_kind") is None:
                plain_axes += 1
    assert total >= 30, f"the ChartPoint scan found only {total} sites"
    assert project_axes >= 3, "the project-derived predicate matched nothing"
    assert plain_axes >= 15, "the plain-axis population disappeared"


def test_the_project_axis_scan_catches_a_planted_offender(tmp_path):
    """Non-vacuity of the predicate itself, over synthetic sources.

    Includes the two shapes the previous narrower scan missed: a site whose
    `x_label` is not the first keyword argument, and one in a module outside
    the two the scan used to name.
    """
    import re as _re
    planted = [
        'ChartPoint(x_label=proj_label, x_value=1.0, y_value=1.0)',
        'ChartPoint(x_value=1.0, x_label=proj_label, y_value=1.0)',
        'ChartPoint(x_label=str(s.get("session_id", "")), x_value=1.0)',
    ]
    caught = 0
    for src in planted:
        for block in _chart_point_arg_blocks(src):
            value = _chart_point_kwarg(block, "x_label")
            if value and _re.search(_PROJECT_DERIVED_X_LABEL, value) \
                    and _chart_point_kwarg(block, "x_label_kind") != '"project"':
                caught += 1
    assert caught == len(planted), f"predicate caught {caught}/{len(planted)}"


def test_the_structural_arm_catches_a_rank_labelled_offender():
    """Arm 2 over synthetic sources, including the exact shape the two
    sessions builders use and the two shapes arm 2 must NOT flag."""
    import re as _re

    def _is_offender(src):
        for block in _chart_point_arg_blocks(src):
            value = _chart_point_kwarg(block, "x_label")
            if not value or _re.search(_PROJECT_DERIVED_X_LABEL, value):
                return False
            project_label = _chart_point_kwarg(block, "project_label")
            if not project_label or project_label == "None":
                return False
            if _re.search(_DATE_DERIVED_X_LABEL, value):
                return False
            return _chart_point_kwarg(block, "x_label_kind") is None
        return False

    offenders = [
        # The sessions-builder shape: a rank, so arm 1's name predicate is
        # blind to it.
        'ChartPoint(x_label=str(i + 1), x_value=1.0, project_label=raw)',
        # A project-keyed axis whose variable name carries no project word.
        'ChartPoint(x_label=bucket, x_value=1.0, project_label=bucket)',
    ]
    exempt = [
        # A calendar axis carrying a project label for the tooltip.
        'ChartPoint(x_label=d["date"], x_value=1.0, project_label=raw)',
        # Already marked.
        'ChartPoint(x_label=str(i + 1), project_label=raw, '
        'x_label_kind="project")',
        # No project label at all.
        'ChartPoint(x_label=str(i + 1), x_value=1.0)',
    ]
    assert all(_is_offender(s) for s in offenders), offenders
    assert not any(_is_offender(s) for s in exempt), exempt


# ---------------------------------------------------------------------
# #503 S1 Task A2 — kernel preparation.
#
# Privacy ownership moves out of `_scrub` (a hand-enumerated field walker
# that visits 3 of 17 snapshot fields) into a preparation pass the render
# and compose boundaries own. Preparation resolves every typed project
# display field, stamps a provenance marker, and refuses to run twice.
# ---------------------------------------------------------------------

def _make_snapshot_with_projects(paths, *, title="Projects", identities=None):
    """A snapshot carrying one ProjectCell row + one project-keyed chart
    point per supplied path, cost-descending so alias rank is deterministic."""
    costs = [100.0 - i for i in range(len(paths))]
    ids = identities or [None] * len(paths)
    return ShareSnapshot(
        cmd="project",
        title=title,
        subtitle=None,
        period=PeriodSpec(
            start=datetime(2026, 4, 11, tzinfo=timezone.utc),
            end=datetime(2026, 5, 9, tzinfo=timezone.utc),
            display_tz="UTC", label="Apr 11 -> May 9 (UTC)",
        ),
        columns=(
            ColumnSpec(key="project", label="Project", align="left"),
            ColumnSpec(key="cost", label="$ Cost", align="right"),
        ),
        rows=tuple(
            Row(cells={
                "project": ProjectCell(label=p, identity=ids[i]),
                "cost": MoneyCell(costs[i]),
            })
            for i, p in enumerate(paths)
        ),
        chart=HorizontalBarChart(
            points=tuple(
                ChartPoint(x_label=p, x_value=costs[i], y_value=costs[i],
                           project_label=p, project_identity=ids[i],
                           x_label_kind="project")
                for i, p in enumerate(paths)
            ),
            x_label="$", cap=None,
        ),
        totals=(),
        notes=(),
        generated_at=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
        version="9.9.9",
    )


def test_disambiguate_appends_parent_on_basename_collision():
    out = _lib_share.disambiguate_basenames(
        ["/work/app", "/personal/app", "/x/solo"])
    assert out[0] == "app (work)"
    assert out[1] == "app (personal)"
    assert out[2] == "solo"


def test_disambiguate_qualifies_further_when_parents_also_repeat():
    """A bare parent suffix is not always enough; the fallback must stay
    deterministic rather than collapsing the two back together."""
    out = _lib_share.disambiguate_basenames(["/a/w/app", "/b/w/app"])
    assert out[0] != out[1]
    assert out[0].startswith("app (") and out[1].startswith("app (")


def test_disambiguate_leaves_a_label_without_a_separator_alone():
    out = _lib_share.disambiguate_basenames(["alpha", "(unknown)", "app (work)"])
    assert out == {0: "alpha", 1: "(unknown)", 2: "app (work)"}


def test_reveal_mode_renders_two_colliding_projects_distinctly():
    """Bare os.path.basename would collapse these into one indistinguishable
    label, and post-scrub into ONE alias — losing privacy uniqueness."""
    snap = _make_snapshot_with_projects(["/work/app", "/personal/app"])
    out = _lib_share.render(snap, format="md", theme="light", branding=True,
                            reveal_projects=True)
    assert "app (work)" in out and "app (personal)" in out


def test_prepare_stamps_provenance_and_render_rejects_double_preparation():
    snap = _make_snapshot_with_projects(["/work/app"])
    prepared = _lib_share._prepare(snap, reveal_projects=False)
    assert _lib_share._is_prepared(prepared)
    assert not _lib_share._is_prepared(snap), "preparation must not mark its input"
    try:
        _lib_share.render(prepared, format="md", theme="light", branding=True,
                          reveal_projects=False)
    except _lib_share.SharePreparationError:
        return
    raise AssertionError("render must reject an already-prepared snapshot")


def test_inventory_reaches_fields_the_scrubber_never_visited():
    snap = _make_snapshot_with_projects(["/work/app"], title="/Volumes/x/secret")
    inv = _lib_share._collect_sensitive_inventory(snap)
    assert "/Volumes/x/secret" in inv.all_strings


def test_prepare_composes_a_project_axis_label_from_the_prefix():
    """P7: session gutter labels become rank + project, never the id."""
    snap = _make_snapshot_with_projects(["/work/app"])
    ranked = _dc.replace(snap, chart=HorizontalBarChart(
        points=(ChartPoint(x_label="9f2b-session-id", x_value=1.0, y_value=1.0,
                           project_label="/work/app", x_label_kind="project",
                           x_label_prefix="1"),),
        x_label="$", cap=None,
    ))
    prepared = _lib_share._prepare(ranked, reveal_projects=False)
    assert prepared.chart.points[0].x_label == "1 · project-1"


def test_prepare_leaves_a_plain_axis_untouched():
    snap = _dc.replace(
        _make_minimal_snapshot(),
        chart=BarChart(
            points=(ChartPoint(x_label="2026-04-11", x_value=0.0, y_value=1.0),),
            y_label="$",
        ),
    )
    prepared = _lib_share._prepare(snap, reveal_projects=False)
    assert prepared.chart.points[0].x_label == "2026-04-11"


def test_prepare_keeps_distinct_identities_distinct_under_a_shared_basename():
    """Alias keys derive from the FULL labels before any basename reduction,
    so two `app` projects stay two aliases rather than merging into one."""
    snap = _make_snapshot_with_projects(["/work/app", "/personal/app"])
    prepared = _lib_share._prepare(snap, reveal_projects=False)
    labels = [r.cells["project"].label for r in prepared.rows]
    assert labels == ["project-1", "project-2"]


# ---------------------------------------------------------------------
# #503 S1 R1 — the CLI session wrapper must feed the kernel DISTINCT
# identities.
#
# `disambiguate_basenames` is total: given identical paths it cannot tell
# them apart, so it appends a stable ordinal. `_build_session_snapshot`
# calls the wrapper with one entry per SESSION, not per distinct project,
# so several sessions inside one repository were handed to the kernel as
# several colliding inputs and came back as `repo (parent) (1)`,
# `repo (parent) (2)`, `repo (parent) (3)`. `ProjectCell.identity` is
# None on that path, so the alias key is the LABEL — one project then
# consumed three alias slots and split its cost across three ranks.
# ---------------------------------------------------------------------

def _session_stub(session_id: str, project_path: str, cost: float):
    """A `ClaudeSessionUsage`-shaped stub carrying only what the builder reads."""
    ts = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
    return _cctally.ClaudeSessionUsage(
        session_id=session_id,
        project_path=project_path,
        source_paths=[f"/fake/{session_id}.jsonl"],
        first_activity=ts,
        last_activity=ts,
        input_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cost_usd=cost,
        models=["claude-sonnet-4-5"],
        model_breakdowns=[],
    )


def _session_snapshot_for(paths_and_costs):
    sessions = tuple(
        _session_stub(f"sess-{i:04d}", path, cost)
        for i, (path, cost) in enumerate(paths_and_costs, 1)
    )
    view = _cctally.SessionsView(
        rows=(),
        aggregated=sessions,
        total_sessions=len(sessions),
        total_cost_usd=sum(c for _p, c in paths_and_costs),
    )
    return _cctally._build_session_snapshot(
        view,
        period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 5, 9, tzinfo=timezone.utc),
        display_tz="Etc/UTC",
        version="9.9.9",
        top_n=None,
        tz=timezone.utc,
        since_explicit=True,
    )


def test_sessions_in_one_project_share_one_label_and_one_alias():
    """Two sessions in one repository are ONE project, not two.

    Before the wrapper deduplicated, the kernel's ordinal fallback gave the
    pair two different labels, and because the session path carries no
    identity the alias key is the label — so one project anonymized to
    `project-1` AND `project-2`, splitting its cost across two ranks.
    """
    snap = _session_snapshot_for([
        ("/Volumes/FIXTURE/repos/cctally", 3.0),
        ("/Volumes/FIXTURE/repos/cctally", 2.0),
        ("/Volumes/FIXTURE/repos/other", 1.0),
    ])
    raw_labels = [r.cells["project"].label for r in snap.rows]
    assert raw_labels[0] == raw_labels[1], (
        f"two sessions in one project must carry one label, got {raw_labels}")
    assert raw_labels[2] != raw_labels[0]

    prepared = _lib_share._prepare(snap, reveal_projects=False)
    aliases = {r.cells["project"].label for r in prepared.rows}
    assert aliases == {"project-1", "project-2"}, (
        f"one project must consume one alias slot, got {sorted(aliases)}")
    # The two-session project outspends the single-session one, so it must
    # rank first.
    assert [r.cells["project"].label for r in prepared.rows] == [
        "project-1", "project-1", "project-2"]


def test_sessions_in_colliding_basenames_still_disambiguate():
    """Deduplication must not weaken the collision case it exists to serve."""
    snap = _session_snapshot_for([
        ("/work/app", 3.0),
        ("/personal/app", 2.0),
    ])
    labels = [r.cells["project"].label for r in snap.rows]
    assert labels == ["app (work)", "app (personal)"]


def test_session_disambiguation_dedupes_before_reaching_the_kernel():
    """The wrapper's contract with `disambiguate_basenames`: distinct inputs.

    `disambiguate_basenames` documents that its input is a set of DISTINCT
    identities; handing it duplicates makes it invent ordinals to keep the
    mapping total.
    """
    sessions = [
        _session_stub("a", "/repos/cctally", 3.0),
        _session_stub("b", "/repos/cctally", 2.0),
        _session_stub("c", "/repos/cctally", 1.0),
    ]
    out = _cctally._session_disambiguate_labels(sessions)
    # Sessions absent from the dict fall back to the bare basename.
    displayed = {out.get(i, "cctally") for i in range(len(sessions))}
    assert displayed == {"cctally"}, (
        f"identical paths must resolve to one label, got {out}")


# ---------------------------------------------------------------------
# #503 S1 R4 — ONE enumeration of project display sites.
#
# `_scrub` leaked because it hand-enumerated three field sites out of a
# seventeen-field graph. Replacing it with a second hand-enumeration would
# only move that shape: preparation would rewrite one set of fields and
# provenance collection would read a different one, and the two would drift
# the first time someone added a project display field to only one. So
# `_apply_project_mapping`, `_project_display_labels` and
# `_project_label_by_key` all derive from `_map_project_display`.
# ---------------------------------------------------------------------

def test_the_single_enumeration_reaches_all_four_display_site_kinds():
    """The enumeration must reach cells, project columns, chart project
    labels (including a stacked series) and project-keyed axes.

    The equality against `_project_display_labels` holds by construction —
    that function IS this visitor — and is asserted here as a record of the
    derivation. The load-bearing assertion is the membership check below: a
    site kind the enumeration stopped reaching would drop out of it silently.
    """
    snap = _dc.replace(
        _make_snapshot_with_projects(["/work/app", "/personal/app"]),
        columns=(
            ColumnSpec(key="project", label="Project", align="left"),
            ColumnSpec(key="colproj", label="/work/columnar", align="right",
                       kind="project"),
        ),
        chart=BarChart(
            points=(ChartPoint(x_label="rank-1", x_value=0.0, y_value=1.0,
                               project_label="/work/app",
                               x_label_kind="project", x_label_prefix="1"),),
            y_label="$",
            stacks={"s": (ChartPoint(x_label="/work/stacked", x_value=0.0,
                                     y_value=1.0,
                                     project_label="/work/stacked",
                                     x_label_kind="project"),)},
        ),
    )
    seen: set[str] = set()

    def _record(site):
        if site.value:
            seen.add(site.value)
        return site.value

    _lib_share._map_project_display(snap, _record)
    assert seen == _lib_share._project_display_labels(snap)
    # Non-vacuity: all four site kinds must actually have contributed.
    assert {"/work/app", "/personal/app", "/work/columnar", "/work/stacked",
            "rank-1"} <= seen


def test_every_site_the_enumeration_reaches_is_rewritten_by_preparation():
    """A site reachable by the enumeration but skipped by preparation would
    be a silent leak; assert preparation resolves all of them."""
    snap = _dc.replace(
        _make_snapshot_with_projects(["/work/app"]),
        columns=(
            ColumnSpec(key="project", label="Project", align="left"),
            ColumnSpec(key="colproj", label="/work/columnar", align="right",
                       kind="project"),
        ),
    )
    prepared = _lib_share._prepare(snap, reveal_projects=False)
    emitted = _lib_share._project_display_labels(prepared)
    assert not any(v.startswith("/") for v in emitted), emitted


def test_project_label_by_key_ignores_the_derived_axis_site():
    """A project-keyed `x_label` is composed from another site's resolution,
    so keying on it would mint a bogus ("legacy", "1 · project-1") entry."""
    snap = _dc.replace(
        _make_snapshot_with_projects(["/work/app"]),
        chart=HorizontalBarChart(
            points=(ChartPoint(x_label="1 · something", x_value=1.0,
                               y_value=1.0, project_label="/work/app",
                               x_label_kind="project", x_label_prefix="1"),),
            x_label="$", cap=None,
        ),
    )
    by_key = _lib_share._project_label_by_key(snap)
    assert set(by_key.values()) == {"/work/app"}


def test_render_does_not_pay_for_the_generic_string_walk():
    """R5: `_verify_output` never read `all_strings`, so building it on every
    render was dead computation. It stays available as a diagnostic."""
    snap = _make_snapshot_with_projects(["/work/app"])
    prepared = _lib_share._prepare(snap, reveal_projects=False)
    inv = _lib_share._inventory_for(snap, prepared)
    assert inv.all_strings == frozenset()
    assert inv.project_labels == frozenset({"/work/app"})
    # The diagnostic helper still walks the whole graph.
    diag = _lib_share._collect_sensitive_inventory(snap)
    assert "/work/app" in diag.all_strings


# ---------------------------------------------------------------------
# #503 S1 R6 — a privacy refusal must be a message, not a traceback.
#
# `render()` raises `SharePrivacyViolation` when the artifact would disclose
# a forbidden identifier. Nothing between the kernel and `main()` converted
# it, so the user-facing result of a privacy detection was a Python stack
# trace. The dashboard side was already correct — `_share_public_failure`
# routes it into the generic envelope.
# ---------------------------------------------------------------------

def _leaking_snapshot():
    """A snapshot whose UNTYPED `title` carries an absolute path.

    Preparation rewrites only TYPED project fields, so a builder that puts a
    path into `title` raises rather than being silently corrected — that is
    the intended behavior, and it is what reaches the CLI user.
    """
    return _dc.replace(_make_minimal_snapshot(),
                       title="/Volumes/FIXTURE/repos/sample-project")


class _ShareArgs:
    format = "md"
    theme = "light"
    no_branding = False
    reveal_projects = False
    open_after_write = False
    output = None
    copy = False


def test_cli_refuses_with_a_message_and_exit_3(capsys):
    import pytest as _pytest
    with _pytest.raises(SystemExit) as exc:
        _cctally._share_render_and_emit(_leaking_snapshot(), _ShareArgs())
    assert exc.value.code == 3
    err = capsys.readouterr().err
    assert err.startswith("cctally: refused to write a share artifact"), err
    # The finding class must be named so the user can act on it.
    assert "absolute path" in err
    # And the OFFENDING VALUE, because the class alone is not actionable
    # (#503 S1 A8). The user whose repository basename is itself a UUID, or
    # who put a path in a title, cannot find the offending string in a
    # multi-hundred-row artifact from the word "absolute path" alone.
    assert "/Volumes/FIXTURE/repos/sample-project" in err, err


def test_cli_privacy_refusal_writes_nothing(tmp_path):
    """The refusal must precede the write, not truncate a file after it."""
    import pytest as _pytest
    target = tmp_path / "report.md"

    class _Args(_ShareArgs):
        output = str(target)

    with _pytest.raises(SystemExit):
        _cctally._share_render_and_emit(_leaking_snapshot(), _Args())
    assert not target.exists()


# =====================================================================
# #503 S2 — the artifact states its own period, timezone and privacy.
#
# D7: the period is computed in, and labelled with, the RESOLVED CONCRETE
# IANA zone. The literal config token `local` never reaches an artifact.
# =====================================================================

import datetime as _dt2
import re as _re
from zoneinfo import ZoneInfo as _ZoneInfo

_TPL_PATH_S2 = _REPO_ROOT / "bin" / "_lib_share_templates.py"
if "_lib_share_templates" in sys.modules:
    _T2 = sys.modules["_lib_share_templates"]
else:
    _spec_t2 = importlib.util.spec_from_file_location(
        "_lib_share_templates", _TPL_PATH_S2)
    _T2 = importlib.util.module_from_spec(_spec_t2)
    sys.modules["_lib_share_templates"] = _T2
    _spec_t2.loader.exec_module(_T2)

_DISPLAY_TZ_PATH_S2 = _REPO_ROOT / "bin" / "_lib_display_tz.py"
if "_lib_display_tz" in sys.modules:
    _DTZ = sys.modules["_lib_display_tz"]
else:
    _spec_dtz = importlib.util.spec_from_file_location(
        "_lib_display_tz", _DISPLAY_TZ_PATH_S2)
    _DTZ = importlib.util.module_from_spec(_spec_dtz)
    sys.modules["_lib_display_tz"] = _DTZ
    _spec_dtz.loader.exec_module(_DTZ)

import pytest as _s2_pytest  # noqa: E402 — module-level skip gate below

# The share-v2 fixture tree is MIRROR-PRIVATE. This file is public, and the
# public suite runs it, so every case that reads that tree is gated on the
# directory being present rather than left to fail on a clone that does not
# carry it (`tests/test_public_test_dep_closure.py` Scope A2). The gate can
# only fire where the fixtures are genuinely absent, so it never silences a
# case in this repository.
_S2_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "share-v2"
_s2_needs_fixtures = _s2_pytest.mark.skipif(
    not _S2_FIXTURES.is_dir(),
    reason="share-v2 fixture tree is mirror-private and absent on a public clone",
)


# `.mirror-allowlist` is itself unmatched by the allowlist, so it is
# never published — which makes its presence the marker for "this is the
# private repository". This FILE is public, so the guard below has to be
# able to tell the two trees apart rather than asserting unconditionally.
_S2R_PRIVATE_REPO = (_REPO_ROOT / ".mirror-allowlist").is_file()


@_s2_pytest.mark.skipif(not _S2R_PRIVATE_REPO,
                        reason="the guard is about the private tree only")
def test_the_share_v2_fixture_gate_is_inactive_in_this_repository():
    """VACUITY GUARD. `_s2_needs_fixtures` skips the F16 bounds sweep,
    the M4 collision sweep, the F17 empty-table cases and the whole D7
    template classification. If the private fixture tree were ever absent
    HERE, every one of them would pass silently. The gate exists only for
    the public clone, so a skip in this repository is a failure."""
    assert _S2_FIXTURES.is_dir(), _S2_FIXTURES
    assert (_S2_FIXTURES / "compose" / "scenarios.json").is_file()
    assert any(_S2_FIXTURES.glob("*/panel_data.json")), _S2_FIXTURES

# Which builders carry CIVIL BUCKET boundaries (a `YYYY-MM-DD` / `YYYY-MM`
# label lifted to a UTC-midnight sentinel) and which carry REAL INSTANTS.
# Converting a civil bucket with `astimezone()` shifts it by a day; not
# converting an instant labels a UTC civil date with another zone's name.
#
# Written out literally rather than derived, so a new template or a flipped
# flag fails this test instead of being absorbed by it.
_S2_CIVIL_BUCKET_TEMPLATES = {
    "weekly-recap", "weekly-visual", "weekly-detail",
    "current-week-recap", "current-week-visual", "current-week-detail",
    "trend-recap", "trend-visual", "trend-detail",
    "daily-recap", "daily-visual", "daily-detail",
    "monthly-recap", "monthly-visual", "monthly-detail",
    "forecast-recap", "forecast-visual", "forecast-detail",
}
_S2_INSTANT_TEMPLATES = {
    "blocks-recap", "blocks-visual", "blocks-detail",
    "sessions-recap", "sessions-visual", "sessions-detail",
    "projects-recap", "projects-visual", "projects-detail",
}

_S2_DEFAULT_TOP_N = {
    "current-week": 3, "trend": 3, "weekly": 5, "daily": 5, "monthly": 5,
    "blocks": 3, "forecast": 5, "sessions": 15, "projects": 5,
}


def _s2_panel_data(panel: str) -> dict:
    import json as _json
    payload = _json.loads(
        (_S2_FIXTURES / panel / "panel_data.json").read_text(encoding="utf-8"))
    out = dict(payload)
    for key in ("period_start", "period_end"):
        value = out.get(key)
        if isinstance(value, str):
            out[key] = _dt2.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return out


def _s2_build_template(template_id: str, *, display_tz: str = "Etc/UTC",
                       panel_data=None):
    tpl = _T2.get_template(template_id)
    options = {
        "format": "md", "theme": "light", "reveal_projects": False,
        "no_branding": False, "top_n": _S2_DEFAULT_TOP_N[tpl.panel],
        "show_chart": True, "show_table": True, "period": None,
        "project_allowlist": None, "display_tz": display_tz,
    }
    for key, value in tpl.default_options.items():
        if key not in ("reveal_projects", "theme", "no_branding"):
            options[key] = value
    if panel_data is None:
        panel_data = _s2_panel_data(tpl.panel)
    return tpl.builder(panel_data=panel_data, options=options)


def test_resolve_display_tz_name_resolves_every_token_to_a_concrete_zone():
    """D7: `local` and `utc` are config TOKENS, not zone names."""
    for token in (None, "", "local", "LOCAL", "utc", "America/New_York"):
        name = _DTZ.resolve_display_tz_name(token)
        assert name not in ("local", "utc", ""), token
        assert _ZoneInfo(name).key == name, token


def test_share_display_tz_label_never_emits_the_local_token():
    """Every v1 CLI golden prints `(local)` today; no artifact may name it."""
    label = _cctally._share_display_tz_label(None)
    assert label != "local"
    assert "/" in label, label
    assert _ZoneInfo(label).key == label


def test_instant_builder_period_dates_are_civil_in_the_labelled_zone():
    """An instant 03:30 UTC on May 5 is May 4 in America/New_York (UTC-4)."""
    civil_start, civil_end = _lib_share.period_civil_dates(
        _lib_share.PeriodSpec(
            start=_dt2.datetime(2026, 5, 5, 3, 30, tzinfo=_dt2.timezone.utc),
            end=_dt2.datetime(2026, 5, 9, 3, 30, tzinfo=_dt2.timezone.utc),
            display_tz="America/New_York", label="x"))
    assert civil_start == "2026-05-04"
    assert civil_end == "2026-05-08"


def test_civil_bucket_boundaries_are_not_shifted_by_zone_conversion():
    """A daily bucket labelled May 4 stays May 4 in every zone."""
    start = _dt2.datetime(2026, 5, 4, 0, 0, tzinfo=_dt2.timezone.utc)
    end = _dt2.datetime(2026, 5, 9, 0, 0, tzinfo=_dt2.timezone.utc)
    for tz in ("America/New_York", "Asia/Tokyo", "Etc/UTC"):
        civil_start, civil_end = _lib_share.period_civil_dates(
            _lib_share.PeriodSpec(start=start, end=end, display_tz=tz,
                                  label="x", civil_bucket=True))
        assert (civil_start, civil_end) == ("2026-05-04", "2026-05-09"), tz


def test_period_civil_dates_tolerates_an_unloadable_zone_name():
    """Defensive: a stale label must not turn a render into a crash."""
    start = _dt2.datetime(2026, 5, 4, 6, 0, tzinfo=_dt2.timezone.utc)
    assert _lib_share.period_civil_dates(
        _lib_share.PeriodSpec(start=start, end=start,
                              display_tz="Not/AZone", label="x")
    ) == ("2026-05-04", "2026-05-04")


@_s2_needs_fixtures
def test_every_template_declares_its_period_boundary_kind():
    registered = {t.id for t in _T2.SHARE_TEMPLATES}
    assert registered == (_S2_CIVIL_BUCKET_TEMPLATES | _S2_INSTANT_TEMPLATES), (
        "template registry drifted from the boundary-kind classification"
    )
    for template_id in sorted(registered):
        snap = _s2_build_template(template_id)
        assert snap.period.civil_bucket is (
            template_id in _S2_CIVIL_BUCKET_TEMPLATES), template_id


@_s2_needs_fixtures
def test_civil_bucket_template_dates_are_zone_invariant():
    """`daily` starts at 2026-05-02; in America/New_York a blind
    `astimezone()` would report 2026-05-01."""
    for template_id in sorted(_S2_CIVIL_BUCKET_TEMPLATES):
        baseline = _lib_share.period_civil_dates(
            _s2_build_template(template_id, display_tz="Etc/UTC").period)
        for tz in ("America/New_York", "Asia/Tokyo"):
            snap = _s2_build_template(template_id, display_tz=tz)
            assert _lib_share.period_civil_dates(snap.period) == baseline, (
                template_id, tz)


# =====================================================================
# #503 S2 review — the command-line and Codex halves of D7.
#
# The two tests above enumerate `_T2.SHARE_TEMPLATES`, which is the set
# that was already correct: `civil_bucket=True` is set only in
# `bin/_lib_share_templates.py`. The 16 `PeriodSpec` construction sites in
# the five command-line and Codex modules were never classified, and five
# commands stated a period one day early under any zone west of UTC,
# because a calendar label was lifted to a UTC-midnight sentinel and then
# converted into the zone the artifact names.
#
# The fix grounds a calendar label at midnight IN THE LABELLED ZONE at the
# site that lifts it, so `period_civil_dates` recovers the same label in
# every zone while a real instant still converts. The two mechanisms are
# therefore both live and must stay distinguishable: templates declare
# `civil_bucket=True`; the command line grounds instead.
# =====================================================================

_S2R_PERIOD_MODULES = (
    "bin/_lib_share_templates.py",
    "bin/_cctally_share.py",
    "bin/_cctally_forecast.py",
    "bin/_cctally_dashboard_share.py",
    "bin/_cctally_source_analytics.py",
    "bin/_cctally_codex.py",
)

# Every `PeriodSpec` construction site outside the template registry,
# keyed by `(module, enclosing function, ORDINAL within that function)`
# and valued by `{case: (start_kind, end_kind)}`.
#
# Three things this keying and this value shape fix (#503 S2 second
# review N2). The ordinal: `_build_codex_source_share_snapshot` builds
# THREE `PeriodSpec`s, and a set of `(module, function)` pairs collapsed
# them into one entry, so a fourth call inside a listed function could
# not fail the test. The per-boundary pair: two sites carry boundaries of
# DIFFERENT kinds, and a single value per function cannot say so. The
# per-case map: two more sites take their kind from their INPUT, and both
# branches are real.
#
# The kinds, and how each is told apart from the others by driving the
# site in three zones:
#
#   GROUNDED      the site re-anchors the boundary at midnight in the
#                 labelled zone, so the ABSOLUTE instant differs in all
#                 three zones while the stated civil date does not.
#   INSTANT       the boundary is one real moment, identical in all three
#                 zones, whose civil date is read in the labelled zone.
#   PARAMETERIZED the site takes `civil_bucket` from its caller and must
#                 propagate it. Only `_lib_share_templates._period`.
#
# Being identical in every zone AND being different in every zone are
# mutually exclusive, so every value below is load-bearing: flip one and
# its site fails. The previous map asserted only its KEYS, and five of
# its fifteen values were in fact wrong.
_S2R_GROUNDED = "grounded"
_S2R_INSTANT = "instant"
_S2R_PARAMETERIZED = "parameterized"

_S2R_PERIOD_SITE_KINDS = {
    ("bin/_lib_share_templates.py", "_period", 0):
        {"": (_S2R_PARAMETERIZED, _S2R_PARAMETERIZED)},
    # `weekStartDate` / `weekEndDate` are the calendar labels the `Week`
    # column prints.
    ("bin/_cctally_share.py", "_build_report_snapshot", 0):
        {"": (_S2R_GROUNDED, _S2R_GROUNDED)},
    # `--since` is a calendar label; the open end is `now`. Four sites
    # share that shape and all four were classified `instant`.
    ("bin/_cctally_share.py", "_build_daily_snapshot", 0):
        {"": (_S2R_GROUNDED, _S2R_INSTANT)},
    ("bin/_cctally_share.py", "_build_monthly_snapshot", 0):
        {"": (_S2R_GROUNDED, _S2R_INSTANT)},
    ("bin/_cctally_share.py", "_build_weekly_snapshot", 0):
        {"": (_S2R_GROUNDED, _S2R_INSTANT)},
    ("bin/_cctally_share.py", "_build_session_snapshot", 0):
        {"": (_S2R_GROUNDED, _S2R_INSTANT)},
    # The forecast window is anchored on the week's reset INSTANT, not on
    # a calendar label — it was classified `grounded`.
    ("bin/_cctally_share.py", "_build_forecast_snapshot", 0):
        {"": (_S2R_INSTANT, _S2R_INSTANT)},
    ("bin/_cctally_share.py", "_build_project_snapshot", 0):
        {"": (_S2R_INSTANT, _S2R_INSTANT)},
    # MIXED BY INPUT: a `--since`/`--until` filter is grounded, while the
    # block-derived fallback is a real instant.
    ("bin/_cctally_share.py", "_build_five_hour_blocks_snapshot", 0):
        {"block-derived": (_S2R_INSTANT, _S2R_INSTANT),
         "since-until": (_S2R_GROUNDED, _S2R_GROUNDED)},
    ("bin/_cctally_forecast.py", "_build_budget_snapshot", 0):
        {"": (_S2R_INSTANT, _S2R_INSTANT)},
    ("bin/_cctally_forecast.py", "_build_budget_no_data_snapshot", 0):
        {"": (_S2R_GROUNDED, _S2R_GROUNDED)},
    ("bin/_cctally_forecast.py", "_build_budget_no_budget_snapshot", 0):
        {"": (_S2R_INSTANT, _S2R_INSTANT)},
    # MIXED BY INPUT, three times over: `_share_codex_period_bounds`
    # grounds the oldest bucket label the rows carry and leaves the end
    # as `now`, but falls back to the end itself when no row names a
    # bucket.
    ("bin/_cctally_dashboard_share.py",
     "_build_codex_source_share_snapshot", 0):
        {"bucketed-rows": (_S2R_GROUNDED, _S2R_INSTANT),
         "unbucketed-rows": (_S2R_INSTANT, _S2R_INSTANT)},
    ("bin/_cctally_dashboard_share.py",
     "_build_codex_source_share_snapshot", 1):
        {"bucketed-rows": (_S2R_GROUNDED, _S2R_INSTANT),
         "unbucketed-rows": (_S2R_INSTANT, _S2R_INSTANT)},
    ("bin/_cctally_dashboard_share.py",
     "_build_codex_source_share_snapshot", 2):
        {"bucketed-rows": (_S2R_GROUNDED, _S2R_INSTANT),
         "unbucketed-rows": (_S2R_INSTANT, _S2R_INSTANT)},
    # The all-source CLI path labels every artifact `Etc/UTC` and takes
    # no zone input at all, so both boundaries are the same instant
    # whatever the host zone is.
    ("bin/_cctally_source_analytics.py", "build_source_share_snapshot", 0):
        {"": (_S2R_INSTANT, _S2R_INSTANT)},
    # MIXED BY INPUT: the VIEW declares whether its start is a bucket
    # label through `period_civil_bucket`; the end is always `now`.
    ("bin/_cctally_codex.py", "_build_codex_share_snapshot", 0):
        {"civil-bucket-view": (_S2R_GROUNDED, _S2R_INSTANT),
         "instant-view": (_S2R_INSTANT, _S2R_INSTANT)},
}


def _s2r_construction_sites(call_name: str) -> "list[tuple[str, str, int]]":
    """`(module, enclosing function, ordinal)` for every call to `call_name`.

    Enumerated from the source with `ast`, not from a registry the
    production code also reads, so the inventory cannot agree with a
    drifted implementation by construction. The ordinal counts calls
    within one function in source order, so a SECOND call added inside an
    already-listed function is a new site rather than a duplicate the set
    absorbs.
    """
    import ast as _ast
    found: list[tuple[str, str, int]] = []
    for rel in _S2R_PERIOD_MODULES:
        tree = _ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))
        stack: list[str] = []
        counts: dict[tuple[str, str], int] = {}

        def walk(node, stack=stack, rel=rel, counts=counts):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                stack.append(node.name)
            if isinstance(node, _ast.Call):
                func = node.func
                name = (func.attr if isinstance(func, _ast.Attribute)
                        else getattr(func, "id", None))
                if name == call_name:
                    key = (rel, stack[-1] if stack else "<module>")
                    found.append((*key, counts.get(key, 0)))
                    counts[key] = counts.get(key, 0) + 1
            for child in _ast.iter_child_nodes(node):
                walk(child)
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                stack.pop()

        walk(tree)
    return found


_SHARE_SNAPSHOT_REGISTRY_SITES = frozenset(
    ("bin/_lib_share_templates.py", fn, 0)
    for fn in (
        "_build_weekly_recap", "_build_current_week_recap",
        "_build_trend_recap", "_build_daily_recap",
        "_build_monthly_recap", "_build_blocks_recap",
        "_build_forecast_recap", "_build_sessions_recap",
        "_build_weekly_visual", "_build_weekly_detail",
        "_build_current_week_visual", "_build_current_week_detail",
        "_build_trend_visual", "_build_trend_detail",
        "_build_daily_visual", "_build_daily_detail",
        "_build_monthly_visual", "_build_monthly_detail",
        "_build_blocks_visual", "_build_blocks_detail",
        "_build_forecast_visual", "_build_forecast_detail",
        "_build_sessions_visual", "_build_sessions_detail",
        "_build_projects_recap", "_build_projects_visual",
        "_build_projects_detail",
    )
)

_SHARE_SNAPSHOT_CLI_SITES = frozenset(
    ("bin/_cctally_share.py", fn, 0)
    for fn in (
        "_build_report_snapshot", "_build_daily_snapshot",
        "_build_monthly_snapshot", "_build_weekly_snapshot",
        "_build_forecast_snapshot", "_build_project_snapshot",
        "_build_five_hour_blocks_snapshot", "_build_session_snapshot",
    )
)

_SHARE_SNAPSHOT_BYPASS_WAIVERS = {
    ("bin/_cctally_forecast.py", "_build_budget_snapshot", 0):
        "budget snapshots carry no project cell",
    ("bin/_cctally_forecast.py", "_build_budget_no_data_snapshot", 0):
        "budget no-data snapshots carry no project cell",
    ("bin/_cctally_forecast.py", "_build_budget_no_budget_snapshot", 0):
        "budget no-budget snapshots carry no project cell",
    ("bin/_cctally_dashboard_share.py", "_build_codex_source_share_snapshot", 0):
        "Codex source projects carry identity and kernel preparation covers them",
    ("bin/_cctally_dashboard_share.py", "_build_codex_source_share_snapshot", 1):
        "Codex source projects carry identity and kernel preparation covers them",
    ("bin/_cctally_dashboard_share.py", "_build_codex_source_share_snapshot", 2):
        "Codex source projects carry identity and kernel preparation covers them",
    ("bin/_cctally_source_analytics.py", "build_source_share_snapshot", 0):
        "source analytics snapshots carry no project cell",
    ("bin/_cctally_codex.py", "_build_codex_share_snapshot", 0):
        "Codex command snapshots carry no project cell",
}


def _share_snapshot_construction_sites():
    """Find every `ShareSnapshot(...)` call in any text file under `bin/`.

    The token prefilter lets the scan include extensionless Python entry points
    without trying to parse every Bash wrapper. A matching file that is not
    valid Python fails loudly instead of disappearing from the inventory.
    """
    import ast as _ast
    import re as _re

    found = []
    for path in sorted((_REPO_ROOT / "bin").rglob("*")):
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if not _re.search(r"\bShareSnapshot\s*\(", source):
            continue
        tree = _ast.parse(source, filename=str(path))
        rel = str(path.relative_to(_REPO_ROOT))
        stack = []
        counts = {}

        def walk(node):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                stack.append(node.name)
            if isinstance(node, _ast.Call):
                func = node.func
                name = (func.attr if isinstance(func, _ast.Attribute)
                        else getattr(func, "id", None))
                if name == "ShareSnapshot":
                    key = (rel, stack[-1] if stack else "<module>")
                    ordinal = counts.get(key, 0)
                    counts[key] = ordinal + 1
                    found.append((*key, ordinal))
            for child in _ast.iter_child_nodes(node):
                walk(child)
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                stack.pop()

        walk(tree)
    return found


def test_every_share_snapshot_constructor_is_classified_or_waived():
    """Every constructor in `bin/` is registry, CLI, or explicitly waived."""
    categories = (
        _SHARE_SNAPSHOT_REGISTRY_SITES,
        _SHARE_SNAPSHOT_CLI_SITES,
        frozenset(_SHARE_SNAPSHOT_BYPASS_WAIVERS),
    )
    assert not (categories[0] & categories[1])
    assert not (categories[0] & categories[2])
    assert not (categories[1] & categories[2])
    assert all(reason.strip() for reason in _SHARE_SNAPSHOT_BYPASS_WAIVERS.values())

    actual = set(_share_snapshot_construction_sites())
    classified = set().union(*categories)
    assert actual == classified, (
        "ShareSnapshot construction sites drifted from their structural "
        "classification: "
        f"unclassified={sorted(actual - classified)} "
        f"missing={sorted(classified - actual)}"
    )


def test_every_period_construction_site_declares_its_boundary_kind():
    """Every site is classified, and a second call in one function is a
    new site rather than a duplicate the previous set absorbed."""
    sites = set(_s2r_construction_sites("PeriodSpec"))
    assert sites == set(_S2R_PERIOD_SITE_KINDS), (
        "PeriodSpec construction sites drifted from the boundary-kind "
        "classification: "
        f"unclassified={sorted(sites - set(_S2R_PERIOD_SITE_KINDS))} "
        f"missing={sorted(set(_S2R_PERIOD_SITE_KINDS) - sites)}"
    )


def test_civil_bucket_is_declared_only_where_the_templates_declare_it():
    """`declared` is not a kind any driven site below can carry.

    A CLI-driven site is observed through its rendered artifact, which
    does not print `civil_bucket`, so the behavioural assertions can only
    tell GROUNDED from INSTANT. This closes that hole from the source:
    `civil_bucket` is written in exactly one module.

    POSITIONAL ARGUMENTS COUNT TOO. `PeriodSpec` is an ordinary
    dataclass, so `civil_bucket` is its fifth positional parameter and a
    five-argument call sets it without naming it — which a scan for the
    keyword alone would not see (#503 S2 third review).
    """
    import ast as _ast
    writers = set()
    for rel in _S2R_PERIOD_MODULES:
        for node in _ast.walk(
                _ast.parse((_REPO_ROOT / rel).read_text(encoding="utf-8"))):
            if not isinstance(node, _ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "civil_bucket":
                    writers.add(rel)
            func = node.func
            name = (func.attr if isinstance(func, _ast.Attribute)
                    else getattr(func, "id", None))
            if name in ("PeriodSpec", "_period") and len(node.args) >= 5:
                writers.add(rel)
    assert writers == {"bin/_lib_share_templates.py"}, sorted(writers)
    # NON-VACUITY: the position the scan guards is the one the dataclass
    # actually declares, read from the class rather than assumed.
    assert [f.name for f in _dc.fields(_lib_share.PeriodSpec)][4] == (
        "civil_bucket")


from collections import namedtuple as _s2r_namedtuple  # noqa: E402

_S2R_BOUNDARY_ZONES = ("Etc/UTC", "America/New_York", "Asia/Tokyo")


class _S2RBoundaryFacts(_s2r_namedtuple(
        "_S2RBoundaryFacts",
        "start_utc end_utc civil_start civil_end civil_bucket")):
    """What one drive of one site, in one zone, says about its period."""


def _s2r_facts_from_period(spec) -> _S2RBoundaryFacts:
    civil_start, civil_end = _lib_share.period_civil_dates(spec)
    return _S2RBoundaryFacts(
        start_utc=spec.start.astimezone(timezone.utc),
        end_utc=spec.end.astimezone(timezone.utc),
        civil_start=civil_start, civil_end=civil_end,
        civil_bucket=spec.civil_bucket)


def _s2r_facts_from_markdown(markdown: str) -> _S2RBoundaryFacts:
    """The same facts read off a rendered artifact.

    `period:` carries the ABSOLUTE boundaries with their offsets, so a
    re-anchored boundary and a fixed instant are distinguishable from the
    bytes. `civil_bucket` is not printed, which is why
    `test_civil_bucket_is_declared_only_where_the_templates_declare_it`
    exists.
    """
    raw = _re.search(r"^period: (\S+)\.\.(\S+)$", markdown, _re.M)
    assert raw, markdown[:400]
    civil = _s2r_facts_line(markdown)
    start_civil, rest = civil.split(" → ", 1)
    end_civil = rest.split(" (", 1)[0]
    return _S2RBoundaryFacts(
        start_utc=datetime.fromisoformat(
            raw.group(1).replace("Z", "+00:00")).astimezone(timezone.utc),
        end_utc=datetime.fromisoformat(
            raw.group(2).replace("Z", "+00:00")).astimezone(timezone.utc),
        civil_start=start_civil, civil_end=end_civil, civil_bucket=None)


def _s2r_cli_driver(scenario: str, command: str, extra=()):
    def drive(tmp_path):
        return {"": {
            zone: _s2r_facts_from_markdown(
                _s2r_run_cli(scenario, command, tz=zone, tmp_path=tmp_path,
                             extra=extra))
            for zone in _S2R_BOUNDARY_ZONES}}
    return drive


def _s2r_drive_five_hour_blocks(tmp_path):
    cases = {}
    for case, extra in (("block-derived", ()),
                        ("since-until",
                         ("--since", "2026-05-07", "--until", "2026-05-07"))):
        cases[case] = {
            zone: _s2r_facts_from_markdown(
                _s2r_run_cli("five-hour-blocks-md", "five-hour-blocks",
                             tz=zone, tmp_path=tmp_path, extra=extra))
            for zone in _S2R_BOUNDARY_ZONES}
    return cases


def _s2r_drive_template_period(_tmp_path):
    """`_period` propagates the kind its caller declares, and nothing else."""
    start = _dt2.datetime(2026, 5, 4, tzinfo=_dt2.timezone.utc)
    end = _dt2.datetime(2026, 5, 10, tzinfo=_dt2.timezone.utc)
    return {
        flag: {
            zone: _s2r_facts_from_period(
                _T2._period(start, end, label="x", display_tz=zone,
                            civil_bucket=flag))
            for zone in _S2R_BOUNDARY_ZONES}
        for flag in (True, False)
    }


def _s2r_drive_source_analytics(_tmp_path):
    module = _cctally._load_sibling("_cctally_source_analytics")
    result_cls = _s2r_load_sibling("_lib_source_analytics").SourceResult
    facts = {}
    for zone in _S2R_BOUNDARY_ZONES:
        # The zone is not an input to this builder at all — it labels
        # every artifact `Etc/UTC` — so driving it three times is what
        # PROVES the boundary is one fixed instant.
        snap = module.build_source_share_snapshot(
            "range-cost",
            result_cls(source="codex", status="unavailable",
                       data={"rangeStart": "2026-05-04T00:00:00Z",
                             "rangeEnd": "2026-05-09T12:00:00Z"}),
            reveal_projects=False)
        facts[zone] = _s2r_facts_from_period(snap.period)
    return {"": facts}


def _s2r_drive_codex_share(_tmp_path):
    import types as _types
    module = _cctally._load_sibling("_cctally_codex")
    cases = {}
    for case, bucket in (("civil-bucket-view", True), ("instant-view", False)):
        cases[case] = {}
        for zone in _S2R_BOUNDARY_ZONES:
            view = _types.SimpleNamespace(
                period_start=_dt2.datetime(2026, 5, 4, tzinfo=_dt2.timezone.utc),
                period_end=_dt2.datetime(2026, 5, 9, 12, 0,
                                         tzinfo=_dt2.timezone.utc),
                period_civil_bucket=bucket, display_tz_label=zone,
                total_cost_usd=1.0)
            snap = module._build_codex_share_snapshot("codex-daily", view, ())
            cases[case][zone] = _s2r_facts_from_period(snap.period)
    return cases


def _s2r_budget_args(zone: str):
    import types as _types
    return _types.SimpleNamespace(_resolved_tz=_ZoneInfo(zone),
                                  week_start_name="monday")


def _s2r_drive_budget_no_budget(_tmp_path):
    module = _cctally._load_sibling("_cctally_forecast")
    # This site's boundary IS the wall clock, so three unpinned drives
    # differ by microseconds and every kind would fail. `CCTALLY_AS_OF`
    # is the same pin the CLI drivers use.
    previous = os.environ.get("CCTALLY_AS_OF")
    os.environ["CCTALLY_AS_OF"] = "2026-05-09T12:00:00Z"
    try:
        return {"": {
            zone: _s2r_facts_from_period(
                module._build_budget_no_budget_snapshot(
                    _s2r_budget_args(zone)).period)
            for zone in _S2R_BOUNDARY_ZONES}}
    finally:
        if previous is None:
            os.environ.pop("CCTALLY_AS_OF", None)
        else:
            os.environ["CCTALLY_AS_OF"] = previous


def _s2r_drive_budget_no_data(_tmp_path):
    module = _cctally._load_sibling("_cctally_forecast")
    now = _dt2.datetime(2026, 5, 9, 12, 0, tzinfo=_dt2.timezone.utc)
    facts = {}
    for zone in _S2R_BOUNDARY_ZONES:
        facts[zone] = _s2r_facts_from_period(
            module._build_budget_no_data_snapshot(
                _s2r_budget_args(zone), {"weekly_usd": 50.0}, now).period)
    return {"": facts}


def _s2r_drive_budget(_tmp_path):
    import types as _types
    module = _cctally._load_sibling("_cctally_forecast")
    inputs = _types.SimpleNamespace(
        week_start_at=_dt2.datetime(2026, 5, 4, 15, 0,
                                    tzinfo=_dt2.timezone.utc),
        week_end_at=_dt2.datetime(2026, 5, 11, 15, 0,
                                  tzinfo=_dt2.timezone.utc),
        target_usd=50.0)
    status = _types.SimpleNamespace(
        spent_usd=10.0, consumption_pct=20.0, remaining_usd=40.0,
        daily_pace_usd=2.0, verdict="ok", low_confidence=False,
        projected_eow_low_usd=35.0, projected_eow_high_usd=40.0)
    facts = {}
    for zone in _S2R_BOUNDARY_ZONES:
        snap = module._build_budget_snapshot(
            _s2r_budget_args(zone), {"weekly_usd": 50.0}, inputs, status,
            tz=_ZoneInfo(zone))
        facts[zone] = _s2r_facts_from_period(snap.period)
    return {"": facts}


def _s2r_codex_source_state(bucketed: bool):
    """A Codex source state whose rows do — or do not — name a bucket."""
    import types as _types
    row = {"key": "codex-row", "label": "Codex quota",
           "cost_usd": 1.0, "total_tokens": 10, "current_percent": 5.0}
    if bucketed:
        row["first_seen"] = "2026-05-04"
    return _types.SimpleNamespace(
        availability="ok",
        last_success_at=_dt2.datetime(2026, 5, 9, 12, 0,
                                      tzinfo=_dt2.timezone.utc),
        data={
            # `histories` feeds the forecast panel, `blocks` the blocks
            # panel and `projects.rows` the projects panel — the three
            # branches that build a `PeriodSpec` of their own.
            "quota": {"histories": (row,), "blocks": (row,)},
            "projects": {"rows": (row,), "total_cost_usd": 1.0},
        })


_S2R_CODEX_SOURCE_PANELS = ("forecast", "projects", "blocks")


def _s2r_codex_source_panel_order() -> "list[str]":
    """The panel each `PeriodSpec` in the Codex source builder belongs to.

    Read from the source in construction order, because
    `_S2R_PERIOD_DRIVERS` keys those three sites by ORDINAL and the
    mapping to a panel was positional and unasserted: reorder the
    branches and every driver would silently test the wrong one, with
    nothing failing, because all three declare the same boundary kinds
    (#503 S2 third review).
    """
    import ast as _ast
    tree = _ast.parse((_REPO_ROOT / "bin" / "_cctally_dashboard_share.py")
                      .read_text(encoding="utf-8"))
    target = next(
        node for node in _ast.walk(tree)
        if isinstance(node, _ast.FunctionDef)
        and node.name == "_build_codex_source_share_snapshot")
    order: list[str] = []
    for node in _ast.walk(target):
        if not isinstance(node, _ast.If):
            continue
        panels = [c.value for c in _ast.walk(node.test)
                  if isinstance(c, _ast.Constant) and isinstance(c.value, str)]
        builds = sum(
            1 for child in node.body
            for call in _ast.walk(child)
            if isinstance(call, _ast.Call)
            and (call.func.attr if isinstance(call.func, _ast.Attribute)
                 else getattr(call.func, "id", None)) == "PeriodSpec")
        order += [panels[0] if panels else "?"] * builds
    return order


def test_the_codex_source_period_drivers_address_the_panel_they_name():
    assert _s2r_codex_source_panel_order() == list(_S2R_CODEX_SOURCE_PANELS)


def _s2r_drive_codex_source(ordinal: int):
    panel = _S2R_CODEX_SOURCE_PANELS[ordinal]

    def drive(_tmp_path):
        module = _cctally._load_sibling("_cctally_dashboard_share")
        lib = _cctally._share_load_lib()
        cases = {}
        for case, bucketed in (("bucketed-rows", True),
                               ("unbucketed-rows", False)):
            cases[case] = {}
            for zone in _S2R_BOUNDARY_ZONES:
                snap = module._build_codex_source_share_snapshot(
                    lib, state=_s2r_codex_source_state(bucketed), panel=panel,
                    template_id=f"{panel}-recap",
                    options={"format": "md", "theme": "light",
                             "reveal_projects": False, "display_tz": zone})
                cases[case][zone] = _s2r_facts_from_period(snap.period)
        return cases
    return drive


# One driver per site. The equality assertion below is what stops a site
# from having none: before this, nine of the fifteen classified sites had
# no behavioural coverage at all.
_S2R_PERIOD_DRIVERS = {
    ("bin/_lib_share_templates.py", "_period", 0): _s2r_drive_template_period,
    ("bin/_cctally_share.py", "_build_report_snapshot", 0):
        _s2r_cli_driver("report-md", "report"),
    ("bin/_cctally_share.py", "_build_daily_snapshot", 0):
        _s2r_cli_driver("daily-md", "daily"),
    ("bin/_cctally_share.py", "_build_monthly_snapshot", 0):
        _s2r_cli_driver("monthly-md", "monthly"),
    ("bin/_cctally_share.py", "_build_weekly_snapshot", 0):
        _s2r_cli_driver("weekly-md", "weekly"),
    ("bin/_cctally_share.py", "_build_session_snapshot", 0):
        _s2r_cli_driver("session-md", "session"),
    ("bin/_cctally_share.py", "_build_forecast_snapshot", 0):
        _s2r_cli_driver("forecast-md", "forecast"),
    ("bin/_cctally_share.py", "_build_project_snapshot", 0):
        _s2r_cli_driver("project-md-anon", "project"),
    ("bin/_cctally_share.py", "_build_five_hour_blocks_snapshot", 0):
        _s2r_drive_five_hour_blocks,
    ("bin/_cctally_forecast.py", "_build_budget_snapshot", 0):
        _s2r_drive_budget,
    ("bin/_cctally_forecast.py", "_build_budget_no_data_snapshot", 0):
        _s2r_drive_budget_no_data,
    ("bin/_cctally_forecast.py", "_build_budget_no_budget_snapshot", 0):
        _s2r_drive_budget_no_budget,
    ("bin/_cctally_dashboard_share.py",
     "_build_codex_source_share_snapshot", 0): _s2r_drive_codex_source(0),
    ("bin/_cctally_dashboard_share.py",
     "_build_codex_source_share_snapshot", 1): _s2r_drive_codex_source(1),
    ("bin/_cctally_dashboard_share.py",
     "_build_codex_source_share_snapshot", 2): _s2r_drive_codex_source(2),
    ("bin/_cctally_source_analytics.py", "build_source_share_snapshot", 0):
        _s2r_drive_source_analytics,
    ("bin/_cctally_codex.py", "_build_codex_share_snapshot", 0):
        _s2r_drive_codex_share,
}


def test_every_period_construction_site_has_a_behavioural_driver():
    assert set(_S2R_PERIOD_DRIVERS) == set(_S2R_PERIOD_SITE_KINDS), (
        "sites without a driver: "
        f"{sorted(set(_S2R_PERIOD_SITE_KINDS) - set(_S2R_PERIOD_DRIVERS))}; "
        "drivers without a site: "
        f"{sorted(set(_S2R_PERIOD_DRIVERS) - set(_S2R_PERIOD_SITE_KINDS))}")


def _s2r_assert_boundary_kind(kind, facts, which, where):
    absolutes = {getattr(f, f"{which}_utc") for f in facts.values()}
    civils = {getattr(f, f"civil_{which}") for f in facts.values()}
    if kind == _S2R_GROUNDED:
        assert len(absolutes) == len(facts), (
            f"{where}: declared GROUNDED, but the {which} boundary is the "
            f"same absolute instant in every zone: {sorted(absolutes)}")
        assert len(civils) == 1, (
            f"{where}: declared GROUNDED, but the stated {which} date "
            f"moves between zones: {sorted(civils)}")
    elif kind == _S2R_INSTANT:
        assert len(absolutes) == 1, (
            f"{where}: declared INSTANT, but the {which} boundary is "
            f"re-anchored per zone: {sorted(absolutes)}")
    else:
        raise AssertionError(f"{where}: unknown kind {kind!r}")


def test_every_period_site_kind_is_asserted_by_driving_it(tmp_path):
    """Each site is driven in `Etc/UTC`, `America/New_York` and
    `Asia/Tokyo`, and its declared kind is checked against what it did.

    The two properties are mutually exclusive — a boundary cannot be the
    same absolute instant in all three zones AND a different one in each
    — so no value here can be flipped without failing.
    """
    if not (_S2R_SHARE_FIXTURES / "report-md" / "cache.db").is_file():
        _s2_pytest.skip("v1 share fixtures are built by "
                        "bin/build-share-fixtures.py")
    for site, cases in sorted(_S2R_PERIOD_SITE_KINDS.items()):
        driven = _S2R_PERIOD_DRIVERS[site](tmp_path)
        if site[1] == "_period":
            # PARAMETERIZED: the site must carry its caller's declaration
            # through, and the two declarations must behave differently.
            for flag in (True, False):
                for facts in driven[flag].values():
                    assert facts.civil_bucket is flag, site
            assert len({f.civil_start for f in driven[True].values()}) == 1
            continue
        assert set(driven) == set(cases), (site, sorted(driven), sorted(cases))
        for case, (start_kind, end_kind) in cases.items():
            facts = driven[case]
            assert set(facts) == set(_S2R_BOUNDARY_ZONES), (site, case)
            where = f"{site[0]}::{site[1]}#{site[2]} [{case or 'only'}]"
            _s2r_assert_boundary_kind(start_kind, facts, "start", where)
            _s2r_assert_boundary_kind(end_kind, facts, "end", where)


def _s2r_zone(name: str):
    return _ZoneInfo(name)


def test_share_parse_date_to_dt_grounds_a_calendar_label_in_its_zone():
    """`weekStartDate` is a calendar label, not an instant.

    Lifting `2026-04-13` to UTC midnight and then converting it into
    `America/New_York` reports `2026-04-12` — a day the artifact's own
    table does not contain.
    """
    for zone_name in ("Etc/UTC", "America/New_York", "Asia/Tokyo"):
        zone = _s2r_zone(zone_name)
        grounded = _cctally._share_parse_date_to_dt("2026-04-13", zone)
        assert grounded.date().isoformat() == "2026-04-13", zone_name
        civil_start, civil_end = _lib_share.period_civil_dates(
            _lib_share.PeriodSpec(start=grounded, end=grounded,
                                  display_tz=zone_name, label="x"))
        assert (civil_start, civil_end) == ("2026-04-13", "2026-04-13"), zone_name


def test_share_parse_date_to_dt_grounds_in_the_resolved_zone_when_tz_is_none():
    """`display.tz = local` (the default) resolves to `None`, and the
    period label still names the host's concrete IANA zone — so a UTC
    midnight sentinel would disagree with its own label."""
    label = _cctally._share_display_tz_label(None)
    grounded = _cctally._share_parse_date_to_dt("2026-04-13", None)
    assert _lib_share.period_civil_dates(
        _lib_share.PeriodSpec(start=grounded, end=grounded,
                              display_tz=label, label="x")
    ) == ("2026-04-13", "2026-04-13")


_S2R_SHARE_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "share"
_s2r_needs_share_fixtures = _s2_pytest.mark.skipif(
    not (_S2R_SHARE_FIXTURES / "report-md" / "cache.db").is_file(),
    reason="v1 share fixtures are built by bin/build-share-fixtures.py",
)


def _s2r_run_cli(scenario: str, command: str, *, tz: str, tmp_path,
                 extra=()) -> str:
    """Render one committed v1 share fixture through the real CLI.

    Markdown only, deliberately: `--format html` and `--format svg` write
    to a file rather than to stdout, so a format parameter here would
    return an empty string. Every caller wants the Markdown artifact.
    """
    import shutil as _shutil
    import subprocess as _subprocess
    fixture = _S2R_SHARE_FIXTURES / scenario
    home = tmp_path / f"{scenario}-{tz.replace('/', '-')}-md"
    (home / ".local" / "share" / "cctally").mkdir(parents=True, exist_ok=True)
    for name in ("cache.db", "stats.db"):
        _shutil.copy(fixture / name, home / ".local" / "share" / "cctally" / name)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "TZ": tz,
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
        "CCTALLY_AS_OF": "2026-05-09T12:00:00Z",
        "CCTALLY_TEST_CHANGELOG_PATH": str(fixture / "CHANGELOG.md"),
    })
    env.pop("CCTALLY_DATA_DIR", None)
    result = _subprocess.run(
        [sys.executable, str(_REPO_ROOT / "bin" / "cctally"), command,
         "--format", "md", "--theme", "light", *extra],
        capture_output=True, text=True, env=env, check=False,
    )
    assert result.returncode == 0, (scenario, tz, result.stderr)
    return result.stdout


def _s2r_facts_line(markdown: str) -> str:
    for line in markdown.splitlines():
        if "→" in line and line.startswith("_") and line.endswith("_"):
            return line.strip("_").replace("\\_", "_")
    raise AssertionError(f"no facts strip in artifact:\n{markdown}")


@_s2r_needs_share_fixtures
@_s2_pytest.mark.parametrize(
    "scenario,command,expected",
    [
        # `report`'s boundaries are `weekStartDate` / `weekEndDate` — the
        # same calendar labels its own `Week` column prints.
        ("report-md", "report", "2026-04-13 → 2026-05-11"),
        # `five-hour-blocks` takes the DATE PART of the oldest and newest
        # block. Under America/New_York this named 2026-05-06, a day with
        # no row in the table below it.
        ("five-hour-blocks-md", "five-hour-blocks", "2026-05-07 → 2026-05-07"),
        # #527: an omitted `--since` is not a user-selected all-history
        # bound. Share artifacts start at their first displayed bucket/row.
        ("daily-md", "daily", "2026-05-04 → 2026-05-09"),
        ("monthly-md", "monthly", "2026-05-01 → 2026-05-09"),
        ("weekly-md", "weekly", "2026-04-27 → 2026-05-09"),
        # The first 2026-05-04 entry resumes into a session whose displayed
        # Last Activity is 2026-05-05, so the artifact's first visible day is
        # the fifth rather than the first raw entry's day.
        ("session-md", "session", "2026-05-05 → 2026-05-09"),
        ("project-md-anon", "project", "2026-05-04 → 2026-05-09"),
    ],
)
def test_cli_artifacts_state_the_dates_their_own_rows_name(
        scenario, command, expected, tmp_path):
    """Run each family from its committed fixture in two zones.

    Every one of these boundaries is either a calendar label or an
    instant anchored in the display zone, so the stated dates are the
    same in both zones. A UTC-midnight sentinel converted into
    `America/New_York` is what breaks that.
    """
    for tz in ("Etc/UTC", "America/New_York"):
        facts = _s2r_facts_line(_s2r_run_cli(scenario, command, tz=tz,
                                             tmp_path=tmp_path))
        assert facts.startswith(expected), (scenario, tz, facts)
        zone = facts.rsplit("(", 1)[1].split(")", 1)[0]
        assert _ZoneInfo(zone).key == zone, (scenario, tz, facts)


@_s2r_needs_share_fixtures
@_s2_pytest.mark.parametrize(
    "scenario,command",
    [
        ("daily-md", "daily"),
        ("monthly-md", "monthly"),
        ("weekly-md", "weekly"),
        ("session-md", "session"),
    ],
)
def test_cli_share_artifacts_preserve_an_explicit_since(
        scenario, command, tmp_path):
    """#527: content-derived defaults must never narrow a requested range."""
    for tz in ("Etc/UTC", "America/New_York"):
        facts = _s2r_facts_line(_s2r_run_cli(
            scenario, command, tz=tz, tmp_path=tmp_path,
            extra=("--since", "2020-01-01"),
        ))
        assert facts.startswith("2020-01-01 → 2026-05-09"), (
            scenario, tz, facts)


def _s2r_codex_entries():
    """Two Codex entries whose UTC calendar day is the bucket label."""
    agg = _s2r_load_sibling("_lib_aggregators")
    return [
        agg.CodexEntry(
            timestamp=_dt2.datetime(2026, 5, 4, 12, 0, tzinfo=_dt2.timezone.utc),
            session_id="s1", model="gpt-5", input_tokens=100,
            cached_input_tokens=0, output_tokens=10,
            reasoning_output_tokens=0, total_tokens=110,
            source_path="/tmp/x.jsonl",
        ),
    ]


def _s2r_load_sibling(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, _REPO_ROOT / "bin" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@_s2_pytest.mark.parametrize("command", ["codex-daily", "codex-monthly",
                                         "codex-weekly"])
def test_codex_bucket_period_dates_are_zone_invariant(command):
    """`build_codex_*_view` sets `period_start` to `datetime.combine(bucket,
    time.min, tzinfo=UTC)` — a calendar label, not an instant."""
    vm = _s2r_load_sibling("_lib_view_models")
    codex = _s2r_load_sibling("_cctally_codex")
    builder = {
        "codex-daily": vm.build_codex_daily_view,
        "codex-monthly": vm.build_codex_monthly_view,
        "codex-weekly": vm.build_codex_weekly_view,
    }[command]
    now = _dt2.datetime(2026, 5, 9, 12, 0, tzinfo=_dt2.timezone.utc)
    baseline = None
    for zone_name in ("Etc/UTC", "America/New_York", "Asia/Tokyo"):
        view = builder(_s2r_codex_entries(), now_utc=now, tz_name=zone_name)
        snap = codex._build_codex_share_snapshot(command, view, view.rows)
        assert snap.period.display_tz == zone_name
        start_civil, _end = _lib_share.period_civil_dates(snap.period)
        if baseline is None:
            baseline = start_civil
        assert start_civil == baseline, (command, zone_name)
    assert baseline is not None and baseline.startswith("2026-05"), baseline


# =====================================================================
# #503 S2 Task 2 — the facts strip.
#
# 27 of the 43 `ShareSnapshot` construction sites pass `subtitle=None`, and
# `_render_md_fragment` gated BOTH the subtitle and the generated-at
# timestamp on that one field — so those artifacts stated neither the
# window they cover nor when they were made. Provenance is a property of
# the render path, not something each construction site opts into (D1).
# =====================================================================

_S2_GENERATED_AT = _dt2.datetime(2026, 5, 9, 12, 0, tzinfo=_dt2.timezone.utc)


def _s2_period(display_tz: str = "Etc/UTC"):
    return PeriodSpec(
        start=_dt2.datetime(2026, 5, 4, tzinfo=_dt2.timezone.utc),
        end=_dt2.datetime(2026, 5, 9, tzinfo=_dt2.timezone.utc),
        display_tz=display_tz, label="This week", civil_bucket=True)


def _s2_snapshot(*, projects=(), subtitle=None, display_tz="Etc/UTC",
                 totals=(), columns=None, rows=None, chart=None):
    if columns is None:
        columns = (
            ColumnSpec(key="project", label="Project", align="left"),
            ColumnSpec(key="cost", label="$ Cost", align="right"),
        ) if projects else (
            ColumnSpec(key="week", label="Week", align="left"),
            ColumnSpec(key="cost", label="$ Cost", align="right"),
        )
    if rows is None:
        rows = tuple(
            Row(cells={"project": ProjectCell(name, float(idx + 1)),
                       "cost": MoneyCell(float(idx + 1))})
            for idx, name in enumerate(projects)
        ) if projects else (
            Row(cells={"week": TextCell("2026-05-04"),
                       "cost": MoneyCell(12.34)}),
        )
    return ShareSnapshot(
        cmd="weekly", title="Weekly recap", subtitle=subtitle,
        period=_s2_period(display_tz), columns=columns, rows=rows,
        chart=chart, totals=totals, notes=(),
        generated_at=_S2_GENERATED_AT, version="9.9.9",
        template_id="weekly-recap")


def _s2_snapshot_with_projects(display_tz="Etc/UTC"):
    return _s2_snapshot(projects=("repos/alpha", "repos/beta"),
                        display_tz=display_tz)


def _s2_snapshot_without_projects():
    return _s2_snapshot()


def _s2_prepared(snap, *, reveal_projects: bool):
    return _lib_share._prepare(snap, reveal_projects=reveal_projects)


def test_facts_line_states_period_timezone_and_anonymization():
    snap = _s2_prepared(_s2_snapshot_with_projects(), reveal_projects=False)
    assert _lib_share.share_facts_line(snap, shows_chart=True, shows_table=True) == (
        "2026-05-04 → 2026-05-09 (Etc/UTC) · projects anonymized")


def test_facts_line_reports_reveal_mode_when_revealed():
    snap = _s2_prepared(_s2_snapshot_with_projects(), reveal_projects=True)
    assert _lib_share.share_facts_line(snap, shows_chart=True, shows_table=True).endswith("· real project names")


def test_facts_line_omits_the_privacy_clause_when_there_are_no_project_names():
    """Trend and Forecast carry no project names; claiming anonymization
    would be a false statement about the document."""
    snap = _s2_prepared(_s2_snapshot_without_projects(), reveal_projects=False)
    line = _lib_share.share_facts_line(snap, shows_chart=True, shows_table=True)
    assert line == "2026-05-04 → 2026-05-09 (Etc/UTC)"
    assert "anonymi" not in line and "real project" not in line


def test_facts_line_reads_the_provenance_marker_not_the_rendered_shape():
    """F5's defect class: a project genuinely named `project-1` is NOT
    anonymized, and no inspection of the rendered labels can tell."""
    snap = _s2_prepared(_s2_snapshot(projects=("project-1",)),
                        reveal_projects=True)
    assert "real project names" in _lib_share.share_facts_line(snap, shows_chart=True, shows_table=True)


def test_facts_line_omits_the_privacy_clause_on_an_unprepared_snapshot():
    """Test-only path: with no marker, neither state can be claimed."""
    line = _lib_share.share_facts_line(_s2_snapshot_with_projects(), shows_chart=True, shows_table=True)
    assert "anonymi" not in line and "real project" not in line
    assert line == "2026-05-04 → 2026-05-09 (Etc/UTC)"


def test_facts_line_ignores_the_semantic_period_label():
    """Every dashboard builder sets a semantic label; printing it would put
    'This week' where the dates belong."""
    snap = _s2_prepared(_s2_snapshot_without_projects(), reveal_projects=False)
    assert snap.period.label == "This week"
    assert "This week" not in _lib_share.share_facts_line(snap, shows_chart=True, shows_table=True)


def test_every_facts_line_passes_the_forbidden_class_detector():
    for tz in ("Etc/UTC", "America/New_York",
               "America/Argentina/Buenos_Aires"):
        for reveal in (True, False):
            snap = _s2_prepared(_s2_snapshot_with_projects(display_tz=tz),
                                reveal_projects=reveal)
            line = _lib_share.share_facts_line(snap, shows_chart=True, shows_table=True)
            assert _lib_share._scan_forbidden_classes(line) == [], (tz, reveal)
            for variant in _lib_share._scan_variants(_md_wrap(line)):
                assert _lib_share._scan_forbidden_classes(variant) == [], variant


def _md_wrap(text: str) -> str:
    return f"_{_lib_share._md_escape(text)}_"


def test_markdown_fragment_emits_the_facts_line_without_a_subtitle():
    """The subtitle-less templates previously emitted neither facts nor
    timestamp, because one `if snap.subtitle:` gated both."""
    md = _lib_share._render_md_fragment(
        _s2_prepared(_s2_snapshot_without_projects(), reveal_projects=False),
        branding=True)
    assert "2026-05-04 → 2026-05-09 (Etc/UTC)" in md
    assert _lib_share._format_generated_at_iso(_S2_GENERATED_AT) in md


def test_markdown_fragment_states_the_facts_exactly_once_with_a_subtitle():
    md = _lib_share._render_md_fragment(
        _s2_prepared(_s2_snapshot(projects=("repos/alpha",),
                                  subtitle="5 projects"),
                     reveal_projects=False),
        branding=True)
    assert md.count("2026-05-04 → 2026-05-09 (Etc/UTC)") == 1
    assert "_5 projects_" in md


# =====================================================================
# #503 S2 Task 3 — facts and totals in HTML and SVG, gated correctly.
#
# `_render_html_fragment` embeds `_render_svg(include_chrome=False,
# include_table=False)`, and that path must carry the chart ALONE. Emitting
# facts or totals unconditionally from `_render_svg` would print each of
# them twice in every HTML artifact.
# =====================================================================

_S2_BUDGET_TOTALS = (
    Totalled(label="Verdict", value="OVER"),
    Totalled(label="Budget", value="$120.00"),
)


def _s2_chart_snapshot(**kwargs):
    return _s2_snapshot(
        chart=LineChart(
            points=tuple(
                ChartPoint(x_label=f"2026-05-0{i}", x_value=float(i),
                           y_value=float(i))
                for i in (4, 5, 6)),
            y_label="$ / %"),
        **kwargs)


def test_html_and_svg_each_state_the_facts_exactly_once():
    expected = "2026-05-04 → 2026-05-09 (Etc/UTC) · projects anonymized"
    for fmt in ("md", "html", "svg"):
        out = _lib_share.render(
            _s2_chart_snapshot(projects=("repos/alpha", "repos/beta")),
            format=fmt, theme="light", branding=True, reveal_projects=False)
        assert out.count(expected) == 1, (fmt, out.count(expected))


def test_totals_render_in_all_three_formats_exactly_once():
    for fmt in ("md", "html", "svg"):
        out = _lib_share.render(
            _s2_chart_snapshot(totals=_S2_BUDGET_TOTALS),
            format=fmt, theme="light", branding=True, reveal_projects=False)
        assert out.count("OVER") == 1, (fmt, out)
        assert out.count("$120.00") == 1, (fmt, out)


def test_the_chart_svg_embedded_in_html_carries_neither_facts_nor_totals():
    """`include_chrome=False` means chart only — the contract that keeps
    HTML from stating each fact twice (docs/share-gotchas.md)."""
    prepared = _s2_prepared(_s2_chart_snapshot(totals=_S2_BUDGET_TOTALS),
                            reveal_projects=False)
    chart = _lib_share._render_svg(
        prepared, palette=_lib_share.PALETTE_LIGHT, branding=False,
        include_chrome=False, include_table=False)
    assert "OVER" not in chart
    assert "$120.00" not in chart
    assert "(Etc/UTC)" not in chart


def test_budget_shaped_html_and_svg_carry_the_verdict_and_target():
    """These live only in `totals`, so today both formats omit them."""
    for fmt in ("html", "svg"):
        out = _lib_share.render(
            _s2_snapshot(totals=_S2_BUDGET_TOTALS),
            format=fmt, theme="light", branding=True, reveal_projects=False)
        assert "OVER" in out, fmt
        assert "$120.00" in out, fmt


def test_svg_canvas_height_covers_the_facts_and_totals_bands():
    """A band emitted below the declared height is invisible."""
    import re as _re
    bare = _lib_share.render(_s2_snapshot(), format="svg", theme="light",
                             branding=True, reveal_projects=False)
    with_totals = _lib_share.render(
        _s2_snapshot(totals=_S2_BUDGET_TOTALS), format="svg", theme="light",
        branding=True, reveal_projects=False)

    def _height(svg):
        return float(_re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', svg).group(1))

    def _max_baseline(svg):
        return max(float(m) for m in _re.findall(r'<text [^>]*\by="([\d.]+)"', svg))

    assert _height(with_totals) > _height(bare)
    for svg in (bare, with_totals):
        assert _max_baseline(svg) <= _height(svg), svg[:200]


def test_html_and_svg_state_the_facts_without_a_subtitle():
    for fmt in ("html", "svg"):
        out = _lib_share.render(_s2_snapshot(), format=fmt, theme="light",
                                branding=True, reveal_projects=False)
        assert "2026-05-04 → 2026-05-09 (Etc/UTC)" in out, fmt


# =====================================================================
# #503 S2 Task 8 — chart label geometry (F16, widened).
#
# The horizontal bar chart assumed a FIXED 120px left gutter and started
# its value label at a fixed `ix + bw + 4`, so a long revealed project
# label ran off the left edge and even `$0.01` ran off the right. The line
# chart's y-axis label is end-anchored at `ix - 10` with no reservation at
# all, so `projected %` starts at -6px in every shipped forecast SVG.
# =====================================================================

_S2_TEXT_RE = _re.compile(r'<text ([^>]*)>([^<]*)</text>')
_S2_ATTR_RE = _re.compile(r'([\w-]+)="([^"]*)"')


def _s2_viewbox_width(svg: str) -> float:
    return float(_re.search(r'viewBox="0 0 ([\d.]+) ', svg).group(1))


def _s2_emitted_text_boxes(svg: str):
    """`(left, right, text)` for every EMITTED `<text>`.

    Read back out of the rendered SVG, not recomputed from the layout
    inputs — so a renderer that forgot to reserve space for a label fails
    here even though it uses the same width estimator.
    """
    for raw_attrs, text in _S2_TEXT_RE.findall(svg):
        attrs = dict(_S2_ATTR_RE.findall(raw_attrs))
        x = float(attrs["x"])
        size = float(attrs["font-size"])
        anchor = attrs.get("text-anchor", "start")
        width = _lib_share._svg_text_width(text, size)
        if anchor == "end":
            yield (x - width, x, text)
        elif anchor == "middle":
            yield (x - width / 2, x + width / 2, text)
        else:
            yield (x, x + width, text)


def _s2_render_template_svg(template_id: str, *, reveal_projects: bool):
    tpl = _T2.get_template(template_id)
    options = {
        "format": "svg", "theme": "light", "reveal_projects": reveal_projects,
        "no_branding": False, "top_n": _S2_DEFAULT_TOP_N[tpl.panel],
        "show_chart": True, "show_table": True, "period": None,
        "project_allowlist": None, "display_tz": "Etc/UTC",
    }
    for key, value in tpl.default_options.items():
        if key not in ("reveal_projects", "theme", "no_branding"):
            options[key] = value
    return _lib_share.render(
        tpl.builder(panel_data=_s2_panel_data(tpl.panel), options=options),
        format="svg", theme="light", branding=True,
        reveal_projects=reveal_projects)


@_s2_needs_fixtures
def test_no_emitted_svg_text_extends_beyond_the_viewbox():
    """Covers both hbar edges and the line chart's y-axis label, in both
    privacy modes — a long REVEALED project label is the left-edge case
    and an anonymized alias is not."""
    offenders = []
    for reveal in (False, True):
        for tpl in _T2.SHARE_TEMPLATES:
            svg = _s2_render_template_svg(tpl.id, reveal_projects=reveal)
            view_w = _s2_viewbox_width(svg)
            for left, right, text in _s2_emitted_text_boxes(svg):
                if left < -0.01 or right > view_w + 0.01:
                    offenders.append((tpl.id, reveal, text, left, right,
                                      view_w))
    assert not offenders, offenders


def test_the_longest_hbar_value_label_fits():
    """Today every value label begins at x=614 on a 640 canvas, so even
    `$0.01` overflows."""
    chart = HorizontalBarChart(
        points=(
            ChartPoint(x_label="alpha", x_value=0.0, y_value=1045.13),
            ChartPoint(x_label="beta", x_value=1.0, y_value=99.99),
            ChartPoint(x_label="gamma", x_value=2.0, y_value=0.01),
        ),
        x_label="$", cap=None)
    svg = _lib_share.render(
        _s2_snapshot(chart=chart, columns=(), rows=()),
        format="svg", theme="light", branding=True, reveal_projects=False)
    view_w = _s2_viewbox_width(svg)
    values = [(l, r) for l, r, t in _s2_emitted_text_boxes(svg)
              if t.startswith("$")]
    assert values
    assert max(r for _l, r in values) <= view_w


def test_a_long_revealed_project_label_is_not_clipped_on_the_left():
    chart = HorizontalBarChart(
        points=(ChartPoint(x_label="risk-analysis-toolkit-2026",
                           x_value=0.0, y_value=8.42),),
        x_label="$", cap=None)
    svg = _lib_share.render(
        _s2_snapshot(chart=chart, columns=(), rows=()),
        format="svg", theme="light", branding=True, reveal_projects=False)
    gutter = [(l, r) for l, r, t in _s2_emitted_text_boxes(svg)
              if t == "risk-analysis-toolkit-2026"]
    assert gutter
    assert min(l for l, _r in gutter) >= 0.0


@_s2_needs_fixtures
def test_line_chart_y_axis_labels_are_not_clipped_on_the_left():
    """Shipped forecast SVGs start `projected %` at -6px."""
    for template_id in ("forecast-recap", "forecast-visual",
                        "forecast-detail"):
        svg = _s2_render_template_svg(template_id, reveal_projects=False)
        labels = [(l, r) for l, r, t in _s2_emitted_text_boxes(svg)
                  if t.endswith("%") and not t[0].isdigit()]
        assert labels, template_id
        assert min(l for l, _r in labels) >= 0.0, template_id


def test_hbar_bars_are_not_shrunk_to_make_labels_fit():
    """The canvas widens; the plot keeps its nominal width."""
    short = HorizontalBarChart(
        points=(ChartPoint(x_label="a", x_value=0.0, y_value=1.0),),
        x_label="$", cap=None)
    long_labels = HorizontalBarChart(
        points=(ChartPoint(x_label="risk-analysis-toolkit-2026",
                           x_value=0.0, y_value=1.0),),
        x_label="$", cap=None)

    def _bar_width(chart):
        svg = _lib_share.render(
            _s2_snapshot(chart=chart, columns=(), rows=()),
            format="svg", theme="light", branding=True, reveal_projects=False)
        rects = _re.findall(
            r'<rect ([^>]*)/>', svg)
        widths = []
        for raw in rects:
            attrs = dict(_S2_ATTR_RE.findall(raw))
            if attrs.get("fill") == _lib_share.PALETTE_LIGHT["series_primary"]:
                widths.append(float(attrs["width"]))
        return max(widths)

    assert _bar_width(long_labels) >= _bar_width(short) - 0.01


def test_svg_canvas_widens_rather_than_clipping_a_long_label():
    narrow = _lib_share.render(
        _s2_snapshot(chart=HorizontalBarChart(
            points=(ChartPoint(x_label="a", x_value=0.0, y_value=1.0),),
            x_label="$", cap=None), columns=(), rows=()),
        format="svg", theme="light", branding=True, reveal_projects=False)
    wide = _lib_share.render(
        _s2_snapshot(chart=HorizontalBarChart(
            points=(ChartPoint(x_label="risk-analysis-toolkit-2026",
                               x_value=0.0, y_value=1.0),),
            x_label="$", cap=None), columns=(), rows=()),
        format="svg", theme="light", branding=True, reveal_projects=False)
    assert _s2_viewbox_width(wide) > _s2_viewbox_width(narrow)


# =====================================================================
# #503 S2 Task 9 — empty tables (F17).
#
# HTML gates its table on `snap.columns` and SVG does too; Markdown did
# not, so the eight chart-only `*-visual` templates emitted a blank line
# for a table that renders as the empty string, then another before the
# totals — a four-newline run in the middle of the document.
#
# `projects-visual` is the contradictory site: its docstring says "no
# table" while it passes a full column set with zero rows, so it emits a
# header-only table in all three formats.
# =====================================================================

@_s2_needs_fixtures
def test_a_visual_markdown_artifact_has_no_blank_run():
    for template_id in ("weekly-visual", "current-week-visual",
                        "trend-visual", "daily-visual", "monthly-visual",
                        "blocks-visual", "forecast-visual",
                        "sessions-visual"):
        tpl = _T2.get_template(template_id)
        options = {
            "format": "md", "theme": "light", "reveal_projects": False,
            "no_branding": False, "top_n": _S2_DEFAULT_TOP_N[tpl.panel],
            "show_chart": True, "show_table": True, "period": None,
            "project_allowlist": None, "display_tz": "Etc/UTC",
        }
        for key, value in tpl.default_options.items():
            if key not in ("reveal_projects", "theme", "no_branding"):
                options[key] = value
        md = _lib_share.render(
            tpl.builder(panel_data=_s2_panel_data(tpl.panel), options=options),
            format="md", theme="light", branding=True, reveal_projects=False)
        assert "\n\n\n" not in md, template_id


def test_a_header_only_table_is_not_rendered_in_any_format():
    """DELIBERATELY REPLACES the S2 assertion that a columns-and-no-rows
    snapshot still renders its header row.

    Which of the two `budget` artifacts produces the schema-only shape
    was stated backwards here, and the correction that fixed it
    everywhere else did not reach this docstring (#503 S2 second review
    N8). `_build_budget_no_budget_snapshot` is the one with `rows=()`;
    `_build_budget_no_data_snapshot` carries a `Weekly budget` row. So
    the shape does have one shipped producer — and it is still
    suppressed, because that artifact is titled `Budget — no budget set`
    and carries a note telling the reader how to set one, which makes
    the header row a frame around nothing there too. What the shape
    actually produced was a dead frame: `share/report-empty-html` drew a
    four-cell header with no body under a title reading `— no data`, and
    seventeen `tests/fixtures/source-aware/*-empty` goldens did the same.
    `source-report-all-codex-unavailable` printed `Codex quota state is
    unavailable.` and then drew a `Quota Series | % Used | $ Cost` frame
    anyway. The gate is therefore on columns AND rows in all three
    formats (#503 S2 review F3).
    """
    snap = _s2_snapshot(
        columns=(ColumnSpec(key="metric", label="Metric", align="left"),
                 ColumnSpec(key="value", label="Value", align="right")),
        rows=())
    md = _lib_share.render(snap, format="md", theme="light", branding=True,
                           reveal_projects=False)
    assert "| Metric | Value |" not in md
    assert "\n\n\n\n" not in md
    html = _lib_share.render(snap, format="html", theme="light",
                             branding=True, reveal_projects=False)
    assert "<table" not in html and "<th" not in html
    svg = _lib_share.render(snap, format="svg", theme="light", branding=True,
                            reveal_projects=False)
    assert ">Metric<" not in svg


def test_a_table_with_rows_still_renders_in_every_format():
    """The gate must not suppress a real table."""
    snap = _s2_snapshot()
    for fmt, marker in (("md", "| Week | $ Cost |"), ("html", "<table"),
                        ("svg", ">Week<")):
        out = _lib_share.render(snap, format=fmt, theme="light",
                                branding=True, reveal_projects=False)
        assert marker in out, fmt


@_s2_needs_fixtures
def test_projects_visual_renders_no_table_in_any_format():
    tpl = _T2.get_template("projects-visual")
    for fmt in ("md", "html", "svg"):
        options = {
            "format": fmt, "theme": "light", "reveal_projects": False,
            "no_branding": False, "top_n": 5, "show_chart": True,
            "show_table": True, "period": None, "project_allowlist": None,
            "display_tz": "Etc/UTC",
        }
        for key, value in tpl.default_options.items():
            if key not in ("reveal_projects", "theme", "no_branding"):
                options[key] = value
        out = _lib_share.render(
            tpl.builder(panel_data=_s2_panel_data("projects"),
                        options=options),
            format=fmt, theme="light", branding=True, reveal_projects=False)
        assert "| Project |" not in out, fmt
        assert "<table" not in out, fmt
        assert "$ Cost" not in out, fmt


@_s2_needs_fixtures
def test_projects_visual_declares_no_columns_at_the_builder():
    """Fixed at the builder, not by teaching the renderers a row-count
    rule that would suppress legitimate schema-only tables."""
    tpl = _T2.get_template("projects-visual")
    snap = _s2_build_template("projects-visual")
    assert snap.columns == ()
    assert snap.rows == ()
    assert tpl.default_options.get("show_table") is False


# =====================================================================
# #503 S2 Task 10 — full dates, and no subtitle that repeats the facts.
#
# F15: the CLI period label and six titles rendered `%b %d`, so a report
# spanning 2020 to 2026 read `Jan 01 → May 09` and named no year. D4 makes
# them full ISO. D5 then removes the subtitles whose whole content the
# facts strip now states — including the theme, which is dropped outright.
# =====================================================================

def _s2_trend_row(date_iso, cost, pct, dpp):
    from types import SimpleNamespace
    return SimpleNamespace(
        week_start_date=_dt2.date.fromisoformat(date_iso),
        used_pct=pct, weekly_cost_usd=cost, dollars_per_percent=dpp)


def _s2_report_snapshot():
    from types import SimpleNamespace
    view = SimpleNamespace(
        rows=(_s2_trend_row("2020-01-01", 10.0, 1.0, 10.0),
              _s2_trend_row("2026-05-04", 20.0, 2.0, 10.0)),
        avg_dollars_per_pct=10.0)
    return _cctally._build_report_snapshot(
        view,
        period_start=_dt2.datetime(2020, 1, 1, tzinfo=_dt2.timezone.utc),
        period_end=_dt2.datetime(2026, 5, 9, tzinfo=_dt2.timezone.utc),
        display_tz="Etc/UTC", version="9.9.9")


def test_period_label_carries_the_year():
    label = _cctally._share_period_label(
        _dt2.datetime(2020, 1, 1, tzinfo=_dt2.timezone.utc),
        _dt2.datetime(2026, 5, 9, tzinfo=_dt2.timezone.utc), "Etc/UTC")
    assert label == "2020-01-01 → 2026-05-09 (Etc/UTC)"


def test_a_cross_year_artifact_is_unambiguous():
    snap = _s2_report_snapshot()
    assert snap.period.label == "2020-01-01 → 2026-05-09 (Etc/UTC)"
    week_labels = [row.cells["week"].text for row in snap.rows]
    assert week_labels == ["2020-01-01", "2026-05-04"]
    assert not any("Jan 01" in label for label in week_labels)


def test_the_cli_builders_no_longer_carry_a_subtitle():
    """Period, theme and privacy: the facts strip states two of the three
    and D5 drops the theme, so the subtitle has nothing left to say — and
    the builders no longer take `theme` or `reveal_projects` at all, which
    keeps a builder from acting on the privacy mode `render()` owns."""
    import inspect
    snap = _s2_report_snapshot()
    assert snap.subtitle is None
    for builder in ("_build_report_snapshot", "_build_daily_snapshot",
                    "_build_monthly_snapshot", "_build_weekly_snapshot",
                    "_build_forecast_snapshot", "_build_project_snapshot",
                    "_build_five_hour_blocks_snapshot",
                    "_build_session_snapshot"):
        params = inspect.signature(getattr(_cctally, builder)).parameters
        assert "theme" not in params, builder
        assert "reveal_projects" not in params, builder


def test_the_projects_subtitle_keeps_only_its_count():
    subtitle = _T2._projects_subtitle(5)
    assert subtitle == "5 projects"
    assert "anonymi" not in subtitle and "real project" not in subtitle
    assert _T2._projects_subtitle(1) == "1 project"


def test_no_share_snapshot_builder_still_formats_a_yearless_date():
    """Structural tripwire over the four builder modules. The literal is
    hardcoded, not imported, so it keeps guarding after a rename."""
    offenders = []
    for name in ("_cctally_share.py", "_cctally_forecast.py",
                 "_cctally_codex.py", "_cctally_source_analytics.py",
                 "_lib_share_templates.py"):
        text = (_REPO_ROOT / "bin" / name).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            # `%b %-d` (the ANSI terminal forecast panel) is deliberately
            # not matched: it is not a share artifact. Comments are
            # stripped so a note ABOUT the retired format does not count
            # as a use of it.
            if "%b %d" not in code:
                continue
            offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert offenders == [], offenders


# =====================================================================
# #503 S2 review — F6: the text-width heuristic at the ends.
#
# `_svg_text_width` estimated every glyph at 0.6 em. It sizes the hbar
# gutter and value reserve, the line chart's y-axis indent, and the SVG
# TABLE's column widths — including the ellipsis decision — so being
# wrong at the ends both clipped labels and overprinted table cells.
#
# The reference widths below were MEASURED in a real browser during the
# QA round, at font-size 11, and are hardcoded here: an estimator checked
# against itself proves nothing.
# =====================================================================

# Every figure below was measured with `getComputedTextLength()` in
# Chromium at font-size 11, where `sans-serif` resolves to Helvetica.
#
# The three ASCII entries this table used to carry (W×20 = 214.20,
# i×36 = 100.08, `risk-analysis-toolkit` = 103.34) came from a DIFFERENT
# font: none of them matches any Helvetica or Arial advance, and W×20 in
# particular implies 0.974 em where both fonts give 0.944. They are
# retired rather than kept beside numbers they contradict, and the
# realistic-project-name case is dropped because no measurement of it
# exists in the current font (#503 S2 second review N6).
_S2R_MEASURED_WIDTHS = (
    # (text, font_size, measured px, max under-reservation, max over)
    ("M" * 20, 11.0, 183.27, 0.02, 0.02),
    ("i" * 36, 11.0, 87.98, 0.02, 0.02),
    ("W" * 20, 11.0, 207.66, 0.02, 0.02),
    # Wide scripts. Over-reservation is the SAFE direction — an
    # under-reserved label overprints its neighbour — so hangul, which
    # the system fallback draws at 0.865 em against the full em the
    # estimator reserves, is allowed to run well over.
    ("数据平台迁移项目二零二六", 11.0, 134.91, 0.0, 0.10),
    ("データ基盤移行", 11.0, 77.97, 0.0, 0.10),
    ("프로젝트관리", 11.0, 57.09, 0.0, 0.25),
    # Emoji are wider than a full em.
    ("\U0001F680" * 8, 11.0, 112.00, 0.0, 0.10),
    # A combining mark advances the pen by zero: twelve acute accents
    # over twelve `e` measure exactly the twelve `e`.
    ("é" * 12, 11.0, 73.42, 0.02, 0.02),
)


def test_the_text_width_estimate_matches_measured_widths_in_every_script():
    for text, size, measured, max_under, max_over in _S2R_MEASURED_WIDTHS:
        estimate = _lib_share._svg_text_width(text, size)
        error = (estimate - measured) / measured
        assert -max_under <= error <= max_over, (
            f"{text[:12]!r}: estimated {estimate:.2f} against a measured "
            f"{measured:.2f} ({error:+.1%}), outside "
            f"[-{max_under:.0%}, +{max_over:.0%}]")


def test_the_text_width_estimate_still_scales_linearly_with_font_size():
    for text, _size, _measured, _under, _over in _S2R_MEASURED_WIDTHS:
        assert _lib_share._svg_text_width(text, 20.0) == _s2_pytest.approx(
            2 * _lib_share._svg_text_width(text, 10.0))


def test_a_non_latin_label_is_never_under_reserved_against_the_fallback():
    """DELIBERATELY REPLACES `test_an_unknown_glyph_keeps_a_finite_estimate`.

    That test asserted only `width > 0.0`, which the flat 0.6-em fallback
    satisfied while under-reserving a Chinese project name by 41.3% and
    an emoji run by 52.9% — so it could not observe the degradation it
    was written for. The property that matters is the DIRECTION of the
    error against the fallback, because an under-reserved label prints on
    top of the value beside it (#503 S2 second review N6).
    """
    fallback = _lib_share._SVG_AVG_GLYPH_WIDTH_FRACTION * 11.0
    for text in ("数据平台", "データ", "프로젝트", "\U0001F680\U0001F680"):
        estimate = _lib_share._svg_text_width(text, 11.0)
        assert estimate > len(text) * fallback, (text, estimate)
    # A combining mark is the one case that must measure LESS.
    assert _lib_share._svg_text_width("é", 11.0) == _s2_pytest.approx(
        _lib_share._svg_text_width("e", 11.0))
    # And the characters that are neither keep the fallback rather than
    # measuring zero — the arrow the facts strip prints is one.
    for text in ("→", "…", "café"):
        assert _lib_share._svg_text_width(text, 11.0) > 0.0, text


# =====================================================================
# #503 S2 review — F5: a standalone SVG rendered in two fonts.
#
# Only the 24 table cells carried `font-family="sans-serif"`. The other
# 17 text elements — title, subtitle, the facts strip, the timestamp, the
# footer, every chart label and every composed section heading — carried
# none and fell back to the viewer default, which is Times in Chromium.
# So the same report was typographically different as `.svg` and as
# `.html`, and F16's width guarantee was calibrated for a sans-serif the
# standalone form did not actually get.
# =====================================================================

def _s2r_svg_roots(text: str) -> "list[str]":
    import re as _re
    return _re.findall(r"<svg\b[^>]*>", text)


@_s2_pytest.mark.parametrize("fmt", ["svg", "html"])
def test_every_emitted_svg_declares_one_font_family(fmt):
    # A chart-bearing snapshot: HTML emits an `<svg>` only for the chart.
    out = _lib_share.render(
        _s2_snapshot(chart=BarChart(
            points=(ChartPoint(x_label="2026-05-04", x_value=0.0,
                               y_value=1.0),),
            y_label="$ / week")),
        format=fmt, theme="light", branding=True, reveal_projects=False)
    roots = _s2r_svg_roots(out)
    assert roots, fmt
    for root in roots:
        assert 'font-family="sans-serif"' in root, (fmt, root[:120])



# =====================================================================
# #503 S2 review — M3: the widened canvas overflowed the HTML body.
#
# `chart_required_width` widens `content_w`, and `_render_html_fragment`
# embeds that SVG with hard `width=`/`height=` attributes inside a body
# capped at `max-width:680px; padding:20px`, with no `max-width:100%`
# and no scroll container.
#
# The content box is 680px, not 640px: the padding lies outside the cap
# under the default `content-box`. Measured over the four fixture roots,
# the committed goldens hold 83 embedded charts; 35 exceed 640px and NONE
# exceeds 680px, the widest being 670.9px in
# `share-v2/sessions/output.detail.reveal-light-branded.html.golden`.
#
# So the goldens alone cannot demonstrate this defect, and an earlier
# record of it — "0 of 152 before, 141 after", and a 696.6px canvas in
# the `projects` golden, which renders 669.0px — measured against the
# wrong box. Real data does reach it: `project --reveal-projects`
# produces a 755.3px canvas, and a real-PDF A/B showed the unfixed
# document losing its top bar's `$2,814.88` value label.
# =====================================================================

# `_wrap_document` emits `padding:20px;max-width:680px` on `<body>`, and
# nothing in an artifact sets `box-sizing`, so under the default
# `content-box` the padding lies OUTSIDE the cap and the content box is
# the full 680px. Subtracting the padding gave 640, which is the figure
# an earlier pass recorded and measured against (#503 S2 fourth review).
_S2R_HTML_BODY_CONTENT_W = 680.0


def _s2r_wide_chart():
    """A chart whose labels force `chart_required_width` past 680px."""
    return HorizontalBarChart(
        points=tuple(
            ChartPoint(x_label=f"enterprise-data-platform-migration-{n}",
                       x_value=float(i), y_value=1000.0 - i)
            for i, n in enumerate((2026, 2025, 2024))
        ),
        x_label="$", cap=None)


_S2R_CSS_WIDTH_CAP = _re.compile(r"max-width\s*:\s*(?!none)")


def _s2r_scroll_box_around(document: str, needle: str) -> str:
    """The innermost element wrapping `needle`, as its opening tag.

    Read from the emitted bytes rather than asserted against a literal,
    so the test observes the relationship between the element and its
    container instead of the presence of a CSS token.
    """
    at = document.index(needle)
    stack: list[str] = []
    for match in _re.finditer(r"<div\b[^>]*>|</div>", document[:at]):
        if match.group(0) == "</div>":
            stack.pop()
        else:
            stack.append(match.group(0))
    assert stack, f"{needle[:60]!r} has no element around it"
    return stack[-1]


def _s2r_box_overflow(box: str) -> str:
    """The `overflow` an emitted container DECLARES.

    Read as a parsed declaration rather than as a substring, so a value
    that merely contains the expected token cannot pass. Whether the box
    then actually scrolls is a browser property and is checked at the
    real-browser gate; the declaration is what this layer can observe.
    """
    return _s2r_declarations(_s2r_tag_attrs(box)[1].get("style", "")).get(
        "overflow", "")


def test_a_widened_embedded_chart_keeps_its_size_and_its_box_scrolls():
    """The mechanism has to be able to fire.

    The earlier pairing declared a scroll container around an element it
    had already capped to that container, so `scrollWidth - clientWidth`
    was 0 in all 582 browser checks and the cap silently shrank the chart
    instead — to 5.93 CSS px of axis text at a 420px viewport (#503 S2
    second review N1). Here: the embedded chart's own width exceeds the
    content box, nothing on it reduces that width, and the element around
    it is a scroll container.
    """
    snap = _s2_snapshot(chart=_s2r_wide_chart(), columns=(), rows=())
    html = _lib_share.render(snap, format="html", theme="light",
                             branding=True, reveal_projects=False)
    root = _s2r_svg_roots(html)[0]
    declared = _s2_viewbox_width(html)
    assert declared > _S2R_HTML_BODY_CONTENT_W, declared
    assert float(_re.search(r'\bwidth="([\d.]+)"', root).group(1)) == declared
    assert not _S2R_CSS_WIDTH_CAP.search(root), root[:200]
    box = _s2r_scroll_box_around(html, root)
    assert _s2r_box_overflow(box) in ("auto", "scroll"), box


def test_the_data_table_scrolls_inside_its_own_box():
    """The `<table>` is what actually overflowed the page.

    Measured at a 420px viewport, `documentElement.scrollWidth -
    clientWidth` was 129px in three `daily` detail artifacts and the
    rightmost columns were off-screen. A table shrink-wraps to its
    content, so the box around it is the only place the scroll can live.
    """
    snap = _s2_snapshot(
        columns=tuple(
            ColumnSpec(key=f"c{i}", label=f"enterprise-column-{i}",
                       align="right")
            for i in range(8)),
        rows=(Row(cells={f"c{i}": TextCell(f"value-{i}") for i in range(8)}),),
        chart=None)
    html = _lib_share.render(snap, format="html", theme="light",
                             branding=True, reveal_projects=False)
    table = _re.search(r"<table\b[^>]*>", html).group(0)
    assert not _S2R_CSS_WIDTH_CAP.search(table), table
    box = _s2r_scroll_box_around(html, table)
    assert _s2r_box_overflow(box) in ("auto", "scroll"), box


def test_a_standalone_svg_scales_to_its_viewport():
    """A `.svg` artifact IS the whole viewport's content, so scaling it
    down scales everything together and the reader can zoom. That is the
    opposite answer from the embedded chart, and deliberately so."""
    snap = _s2_snapshot(chart=_s2r_wide_chart(), columns=(), rows=())
    svg = _lib_share.render(snap, format="svg", theme="light",
                            branding=True, reveal_projects=False)
    root = _s2r_svg_roots(svg)[0]
    assert "max-width:100%" in root
    assert "height:auto" in root


# =====================================================================
# #503 S2 third review — the scroll boxes reached PAPER, where a scroll
# container is a crop.
#
# The screen answer above (an intrinsically-sized chart and table, each
# inside its own `overflow:auto` box) is wrong on paper. A print engine
# clips a scroll container to its box, so the chart loses everything past
# the content box, and that is where the bar value labels and the last
# axis tick sit.
#
# The content box is 680px, not 640: `body` declares `padding:20px;
# max-width:680px` under default `content-box` sizing, so the padding is
# outside the cap. Measured against the true box, NONE of the 83 embedded
# charts in the committed goldens overflows — the widest declares 670.9 —
# and the fix is justified by data the fixtures do not carry: real
# `project --reveal-projects` output produces a 755.3px chart, and a
# real-PDF A/B showed the uncorrected version losing the top bar's
# `$2,814.88` value label (#503 S2 fourth review).
#
# The table half of the rule is specification-conformance insurance
# rather than an observed fix. CSS fragmentation makes any box whose
# `overflow` is not `visible` monolithic, but Chromium 151 paginates an
# `overflow:auto` table across pages regardless — 240 rows over 8 pages,
# identically with and without the rule.
#
# Both are corrected inside `@media print`, which keeps the print delta
# to one replaced `<style>` line per HTML golden and cannot touch the
# screen behaviour at all.
# =====================================================================

_S2R_PRINT_BLOCK = _re.compile(r"<style>@media print \{(.*)\}</style>",
                               _re.DOTALL)
_S2R_CSS_RULE = _re.compile(r"([^{}]+?)\s*\{\s*([^{}]*?)\s*\}")
_S2R_SIMPLE_SELECTOR = _re.compile(
    r'^(?P<tag>[a-z]+)'
    r'(?:\[(?P<attr>[a-z-]+)(?P<op>\*?=)"(?P<value>[^"]*)"\])?$')


def _s2r_print_rules(document: str) -> "list[tuple[str, dict]]":
    """Every `(selector, declarations)` pair inside the print stylesheet."""
    block = _S2R_PRINT_BLOCK.search(document)
    assert block, "the document carries no print stylesheet"
    rules = []
    for selector, body in _S2R_CSS_RULE.findall(block.group(1)):
        rules.append((selector.strip(), _s2r_declarations(body)))
    return rules


def _s2r_declarations(style: str) -> dict:
    """`{property: value}` from a declaration list, `!important` dropped."""
    out = {}
    for declaration in style.split(";"):
        if ":" not in declaration:
            continue
        name, _, value = declaration.partition(":")
        out[name.strip().lower()] = (
            value.replace("!important", "").strip().lower())
    return out


def _s2r_tag_attrs(tag: str) -> "tuple[str, dict]":
    """`(name, attributes)` read off an emitted opening tag."""
    name = _re.match(r"<([a-zA-Z]+)", tag).group(1).lower()
    return name, dict(_S2_ATTR_RE.findall(tag))


def _s2r_rules_selecting(document: str, element: str) -> "list[dict]":
    """The print declarations that actually SELECT this emitted element.

    The selectors are evaluated against the element's own attributes as
    the renderer wrote them, so the test observes whether the print rule
    reaches the markup rather than whether some string is present in the
    stylesheet. That distinction is the whole point here: the scroll
    boxes were added to the markup and the print stylesheet was never
    told about them, and no token check would have noticed.
    """
    name, attrs = _s2r_tag_attrs(element)
    reaching = []
    for selector, declarations in _s2r_print_rules(document):
        for one in selector.split(","):
            match = _S2R_SIMPLE_SELECTOR.match(one.strip())
            if match is None or match.group("tag") != name:
                continue
            if match.group("attr") is None:
                reaching.append(declarations)
                break
            have = attrs.get(match.group("attr"))
            if have is None:
                continue
            wanted = match.group("value")
            if (have == wanted if match.group("op") == "="
                    else wanted in have):
                reaching.append(declarations)
                break
    return reaching


def _s2r_printable_document() -> str:
    """A dark artifact carrying both scroll boxes and an over-wide chart."""
    snap = _s2_snapshot(
        chart=_s2r_wide_chart(),
        columns=tuple(
            ColumnSpec(key=f"c{i}", label=f"enterprise-column-{i}",
                       align="right")
            for i in range(8)),
        rows=tuple(
            Row(cells={f"c{i}": TextCell(f"value-{i}-{n}") for i in range(8)})
            for n in range(4)),
    )
    return _lib_share.render(snap, format="html", theme="dark",
                             branding=True, reveal_projects=False)


def _s2r_emitted_scroll_boxes(document: str) -> "list[str]":
    """Every emitted `<div>` that DECLARES itself a scroll container.

    Swept from the document rather than looked up by needle. The two boxes
    this rule was written for are the chart wrapper and the table wrapper,
    and enumerating those two would leave a third wrapper added later
    unreleased on paper with nothing failing — which is precisely the
    shape that produced this session's P1.
    """
    boxes = []
    for tag in _re.findall(r"<div\b[^>]*>", document):
        declarations = _s2r_declarations(_s2r_tag_attrs(tag)[1].get("style", ""))
        if declarations.get("overflow") in ("auto", "scroll", "hidden"):
            boxes.append(tag)
    return boxes


def test_print_releases_every_scroll_box_the_document_emits():
    """A scroll container crops on paper and cannot break across pages.

    The class, not the two instances: any box whose `overflow` is not
    `visible` is cropped by a print engine and made monolithic by CSS
    fragmentation, so every one the document emits must be released.
    `hidden` is swept alongside `auto` and `scroll` because it has the
    same two consequences on paper and none of the screen benefit — and
    because the shipped print selector is `div[style*="overflow:auto"]`,
    which reaches neither of the other two values. A wrapper declaring
    `overflow:scroll` would therefore be emitted, cropped on paper and
    released by nothing, and only a sweep of the class can see that.
    """
    html = _s2r_printable_document()
    boxes = _s2r_emitted_scroll_boxes(html)
    # The two known wrappers are present, so the sweep is not passing on
    # an empty set: the chart's box and the table's box.
    assert _s2r_scroll_box_around(html, _s2r_svg_roots(html)[0]) in boxes
    assert _s2r_scroll_box_around(
        html, _re.search(r"<table\b[^>]*>", html).group(0)) in boxes
    for box in boxes:
        released = [d for d in _s2r_rules_selecting(html, box)
                    if d.get("overflow") == "visible"]
        assert released, (
            "no print rule releases this scroll box: " + box[:160])


def test_print_scales_the_embedded_chart_into_the_page():
    """On paper the chart must fit the sheet, not keep its intrinsic width.

    On screen the embedded chart deliberately carries no `max-width`, so
    that the box around it can scroll (#503 S2 second review N1). Paper
    has no scrolling, so the same absence crops the chart instead — the
    right edge, which is where the bar value labels and the last axis
    tick are.
    """
    html = _s2r_printable_document()
    root = _s2r_svg_roots(html)[0]
    assert _s2_viewbox_width(html) > _S2R_HTML_BODY_CONTENT_W
    assert not _S2R_CSS_WIDTH_CAP.search(root), root[:200]
    sizing = [d for d in _s2r_rules_selecting(html, root)
              if d.get("max-width") == "100%"]
    assert sizing, "no print rule caps the embedded chart's width"
    assert any(d.get("height") == "auto" for d in sizing), (
        "the print cap does not keep the chart's aspect ratio")


# =====================================================================
# #503 S2 review — M4: chart text that overprints its neighbour.
#
# F16 widened the canvas for the y-axis label and the hbar gutter, and
# did nothing for x-tick spacing — so an uncorrected member of F16's own
# class survived. In the `blocks` templates the ticks are full ISO
# timestamps about 103 units wide at 56 units of spacing, and in the
# forecast chart two ticks sit 1.73 units apart and visually touch.
#
# The rule is the class, not the two instances: chart text that does not
# fit must not collide or clip, wherever it is emitted. This sweep sits
# beside `test_no_emitted_svg_text_extends_beyond_the_viewbox` and covers
# the other axis of the same rule.
# =====================================================================

_S2R_TRANSLATE_GROUP = _re.compile(r'<g transform="translate\(0,[\d.]+\)">')
_S2R_SVG_BODY = _re.compile(r"<svg\b[^>]*>(.*?)</svg>", _re.DOTALL)


def _s2r_text_collisions(document: str):
    """Every pair of `<text>` boxes that overlap on one baseline.

    Compared per `<svg>` element and, inside a composed stack, per
    translated section group — two sections legitimately reuse the same
    local `y`.
    """
    offenders = []
    for body in _S2R_SVG_BODY.findall(document):
        for segment in _S2R_TRANSLATE_GROUP.split(body):
            rows: dict = {}
            for left, right, text, baseline in _s2r_boxes(segment):
                rows.setdefault(round(baseline, 1), []).append(
                    (left, right, text))
            for baseline, items in rows.items():
                items.sort()
                for i in range(len(items) - 1):
                    if items[i][1] > items[i + 1][0] + 0.01:
                        offenders.append(
                            (baseline, items[i][2], items[i + 1][2],
                             round(items[i][1] - items[i + 1][0], 2)))
    return offenders


def _s2r_boxes(segment: str):
    for raw_attrs, text in _S2_TEXT_RE.findall(segment):
        attrs = dict(_S2_ATTR_RE.findall(raw_attrs))
        x = float(attrs["x"])
        baseline = float(attrs["y"])
        size = float(attrs["font-size"])
        anchor = attrs.get("text-anchor", "start")
        width = _lib_share._svg_text_width(text, size)
        if anchor == "end":
            yield (x - width, x, text, baseline)
        elif anchor == "middle":
            yield (x - width / 2, x + width / 2, text, baseline)
        else:
            yield (x, x + width, text, baseline)


@_s2_needs_fixtures
def test_no_emitted_svg_text_overprints_a_sibling():
    offenders = []
    for reveal in (False, True):
        for tpl in _T2.SHARE_TEMPLATES:
            svg = _s2_render_template_svg(tpl.id, reveal_projects=reveal)
            for hit in _s2r_text_collisions(svg):
                offenders.append((tpl.id, reveal, *hit))
    assert not offenders, offenders[:10]


def test_a_dense_axis_drops_labels_rather_than_overprinting_them():
    """Twenty full-ISO ticks cannot all fit; the ones that remain must
    still name the first and the last sample."""
    points = tuple(
        ChartPoint(x_label=f"2026-05-{day:02d}T05:00:00Z", x_value=float(i),
                   y_value=float(i + 1))
        for i, day in enumerate(range(1, 21))
    )
    svg = _lib_share.render(
        _s2_snapshot(chart=BarChart(points=points, y_label="$ / block"),
                     columns=(), rows=()),
        format="svg", theme="light", branding=True, reveal_projects=False)
    assert not _s2r_text_collisions(svg), _s2r_text_collisions(svg)
    assert "2026-05-01T05:00:00Z" in svg
    assert "2026-05-20T05:00:00Z" in svg


def test_a_two_tick_axis_drops_the_last_rather_than_overprinting_the_first():
    """The dense case above only exercises twenty ticks.

    With two or three tight ISO ticks the sweep returns a SINGLE label:
    the last is reserved before the interior ticks are walked, but it is
    dropped when it cannot coexist with the first. That is the documented
    behaviour and it was previously untested, so the docstring could have
    described both endpoints as reserved without anything failing
    (#503 S2 second review N8).
    """
    for count in (2, 3):
        ticks = [
            (60.0 + 10.0 * i,
             "end" if i == count - 1 else "middle",
             f"2026-05-0{i + 1}T05:00:00Z", 11.0)
            for i in range(count)
        ]
        assert _lib_share._visible_axis_ticks(ticks) == {0}, count


def test_the_axis_sweep_reads_left_to_right_whatever_order_it_is_given():
    """`_render_line_chart_svg` does not sort its points, so a sweep that
    assumed ascending order dropped the actual leftmost label."""
    ticks = [(300.0, "middle", "B", 11.0),
             (60.0, "middle", "A", 11.0),
             (500.0, "middle", "C", 11.0)]
    kept = _lib_share._visible_axis_ticks(ticks)
    assert {ticks[i][2] for i in kept} == {"A", "B", "C"}
    # Tight enough that only the ends survive — and the ends are the
    # LEFTMOST and RIGHTMOST samples, not the first and last supplied.
    tight = [(165.0, "middle", "2026-05-02T05:00:00Z", 11.0),
             (60.0, "middle", "2026-05-01T05:00:00Z", 11.0),
             (270.0, "middle", "2026-05-03T05:00:00Z", 11.0)]
    kept = _lib_share._visible_axis_ticks(tight)
    assert {tight[i][2] for i in kept} == {"2026-05-01T05:00:00Z",
                                           "2026-05-03T05:00:00Z"}


def test_a_sparse_axis_keeps_every_label():
    points = tuple(
        ChartPoint(x_label=str(i), x_value=float(i), y_value=float(i + 1))
        for i in range(4)
    )
    svg = _lib_share.render(
        _s2_snapshot(chart=BarChart(points=points, y_label="$"),
                     columns=(), rows=()),
        format="svg", theme="light", branding=True, reveal_projects=False)
    for i in range(4):
        assert f">{i}<" in svg, i


# =====================================================================
# #503 S2 review — F9: defensive gaps on D7's own path.
# =====================================================================

_S2R_EMPTY_PANEL_KEY = {
    "trend-recap": "weeks", "trend-visual": "weeks", "trend-detail": "weeks",
    "daily-recap": "days", "daily-visual": "days", "daily-detail": "days",
    "monthly-recap": "months", "monthly-visual": "months",
    "monthly-detail": "months",
}


@_s2_needs_fixtures
@_s2_pytest.mark.parametrize("template_id", sorted(_S2R_EMPTY_PANEL_KEY))
def test_an_empty_civil_panel_states_today_in_the_labelled_zone(
        template_id, monkeypatch):
    """`_utc_now()` is a real INSTANT, and marking one `civil_bucket=True`
    tells `period_civil_dates` to skip the conversion — so an empty
    artifact stated the UTC calendar day under another zone's name."""
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-05-09T02:00:00Z")
    tpl = _T2.get_template(template_id)
    payload = dict(_s2_panel_data(tpl.panel))
    payload[_S2R_EMPTY_PANEL_KEY[template_id]] = []
    for zone, expected in (("Etc/UTC", "2026-05-09"),
                           ("America/New_York", "2026-05-08"),
                           ("Asia/Tokyo", "2026-05-09")):
        options = {
            "format": "md", "theme": "light", "reveal_projects": False,
            "no_branding": False, "top_n": _S2_DEFAULT_TOP_N[tpl.panel],
            "show_chart": True, "show_table": True, "period": None,
            "project_allowlist": None, "display_tz": zone,
        }
        for key, value in tpl.default_options.items():
            if key not in ("reveal_projects", "theme", "no_branding"):
                options[key] = value
        snap = tpl.builder(panel_data=payload, options=options)
        start_civil, _end = _lib_share.period_civil_dates(snap.period)
        assert start_civil == expected, (template_id, zone, start_civil)


def test_the_dashboard_share_display_tz_is_resolved_not_passed_through():
    """`_lib_view_models._display_tz_label` returns the literal `local`
    for a `None` zone, so a panel value used verbatim puts `(local)`
    back into an artifact."""
    share = _cctally._load_sibling("_cctally_dashboard_share")
    for token in ("local", "utc", "", None, "America/New_York"):
        resolved = share._share_resolved_display_tz(token)
        assert resolved not in ("local", "utc", ""), token
        assert _ZoneInfo(resolved).key == resolved, token


def test_a_block_derived_period_bound_is_an_instant_that_converts():
    """A 5-hour block that began at 03:30 UTC on May 5 is May 4 in an
    American zone — which is what the artifact's own row cell says, and
    what the CHANGELOG entry for D7 promises. The site used to keep only
    the UTC date part, naming a day the table does not contain."""
    five_hour = _s2r_load_sibling("_cctally_five_hour")
    instant = five_hour._blocks_period_instant("2026-05-05T03:30:00Z")
    for zone, expected in (("Etc/UTC", "2026-05-05"),
                           ("America/New_York", "2026-05-04"),
                           ("Asia/Tokyo", "2026-05-05")):
        civil_start, _end = _lib_share.period_civil_dates(
            _lib_share.PeriodSpec(start=instant, end=instant,
                                  display_tz=zone, label="x"))
        assert civil_start == expected, (zone, civil_start)


def test_a_user_supplied_since_date_is_a_calendar_label():
    """`--since 2026-05-06` names a civil day in the user's own zone, so
    it must NOT move when converted — the opposite of a block start."""
    for zone in ("Etc/UTC", "America/New_York", "Asia/Tokyo"):
        grounded = _cctally._share_parse_date_to_dt("2026-05-06",
                                                    _ZoneInfo(zone))
        civil_start, _end = _lib_share.period_civil_dates(
            _lib_share.PeriodSpec(start=grounded, end=grounded,
                                  display_tz=zone, label="x"))
        assert civil_start == "2026-05-06", zone


def test_a_codex_view_label_is_resolved_at_the_share_boundary():
    """F9's second item, answered where it belongs.

    `_lib_view_models._codex_tz_label` can return an abbreviation such as
    `EDT`, which `period_civil_dates` cannot load. That value is the
    dashboard ENVELOPE's display label, whose shape is pinned by oracle
    tests, so it is resolved at the two share boundaries that turn it
    into a `PeriodSpec.display_tz` rather than in the envelope wire.
    """
    codex = _s2r_load_sibling("_cctally_codex")
    share = _cctally._load_sibling("_cctally_dashboard_share")
    now = _dt2.datetime(2026, 5, 9, 12, 0, tzinfo=_dt2.timezone.utc)

    class _View:
        rows = ()
        total_cost_usd = 0.0
        total_tokens = 0
        period_start = None
        period_end = now
        display_tz_label = "EDT"

    snap = codex._build_codex_share_snapshot("codex-daily", _View(), ())
    assert _ZoneInfo(snap.period.display_tz).key == snap.period.display_tz
    assert snap.period.display_tz not in ("EDT", "local", "utc", "")
    for token in ("EDT", "IST", "UTC"):
        resolved = share._share_resolved_display_tz(token)
        assert _ZoneInfo(resolved).key == resolved, token


# =====================================================================
# #503 S2 third review — one rule, swept over the whole class.
#
# "The period an artifact states must describe what that artifact
# actually displays" shipped as a template-only guard, so it saw 27 of
# the 43 `ShareSnapshot` construction sites and none of the command-line
# ones. Four `source-aware` artifacts therefore stated a period NARROWER
# than their own table — one of them a zero-width instant above a row
# dated three days earlier — and passed a test whose docstring stated the
# rule in general terms.
#
# The shipped extractor was also blind to a whole cell type: it read
# dates out of `str` attributes only, so `DateCell.when` — a `datetime` —
# contributed nothing and `sessions-recap` and `sessions-detail` checked
# zero cells each. Every sweep below reads the RENDERED BYTES instead of
# the snapshot graph, which closes that blindness by construction:
# whatever the artifact prints is what the sweep sees, whichever field it
# came from.
#
# The two directions are separate tests, because they fail for opposite
# reasons — one artifact understates and another overstates.
# =====================================================================

_S2T_DATE_TOKEN = _re.compile(
    r"^(\d{4}-\d{2})(-\d{2})?"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?$")
_S2T_HTML_CELL = _re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", _re.DOTALL)
_S2T_SVG_TEXT = _re.compile(r"<text\b[^>]*>(.*?)</text>", _re.DOTALL)
_S2T_TAG = _re.compile(r"<[^>]+>")
_S2T_FACTS_MD = _re.compile(r"^_(\d{4}-\d{2}-\d{2}) → (\d{4}-\d{2}-\d{2}) \(",
                            _re.M)
_S2T_FACTS_HTML = _re.compile(
    r"<div[^>]*>(\d{4}-\d{2}-\d{2}) → (\d{4}-\d{2}-\d{2}) \(")
_S2T_FRONTMATTER_PERIOD = _re.compile(r"^period: (\S+)\.\.(\S+)$", _re.M)


def _s2t_token(text: str) -> "str | None":
    """`YYYY-MM` or `YYYY-MM-DD` when the WHOLE string is a date.

    Anchored on purpose. A substring match would read a date out of a
    title (`Weekly recap — week of 2026-05-04`), which names what the
    artifact is ABOUT rather than what it displays, and out of the facts
    strip itself.
    """
    match = _S2T_DATE_TOKEN.match(text.strip())
    if match is None:
        return None
    return match.group(1) + (match.group(2) or "")


def _s2t_plain(fragment: str) -> str:
    return (_S2T_TAG.sub("", fragment)
            .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("\\", "").strip())


def _s2t_md_dates(markdown: str) -> "list[tuple[str, str]]":
    """Every date a Markdown fragment's TABLE displays.

    Markdown draws no chart, so its table is the whole of what it
    displays. Table lines are the only ones read, which is what keeps
    the title, the strip itself and the frontmatter out.
    """
    found = []
    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        for cell in line.strip("|").split("|"):
            token = _s2t_token(cell.replace("\\", "").strip())
            if token:
                found.append(("table", token))
    return found


def _s2t_html_dates(document: str) -> "list[tuple[str, str]]":
    """Every date an HTML fragment displays — its table AND its chart.

    The only `<svg>` in an HTML artifact is the embedded chart, rendered
    with `include_chrome=False`, so every `<text>` in it is a chart label
    rather than document chrome.
    """
    found = [("table", token)
             for raw in _S2T_HTML_CELL.findall(document)
             if (token := _s2t_token(_s2t_plain(raw)))]
    found += [("chart", token)
              for raw in _S2T_SVG_TEXT.findall(document)
              if (token := _s2t_token(_s2t_plain(raw)))]
    return found


def _s2t_sections(document: str, fmt: str) -> "list[tuple[tuple, str]]":
    """`((start, end), body)` per facts strip, in document order.

    A composed document carries one strip per section, and each section's
    content follows its own strip. Slicing on the strips is what lets one
    sweep read a standalone artifact and a composite the same way.
    """
    pattern = _S2T_FACTS_MD if fmt == "md" else _S2T_FACTS_HTML
    marks = list(pattern.finditer(document))
    out = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(document)
        out.append(((mark.group(1), mark.group(2)),
                    document[mark.end():end]))
    return out


def _s2t_covers(stated: "tuple[str, str]", token: str) -> bool:
    start, end = stated
    return start[:len(token)] <= token <= end[:len(token)]


def _s2t_offenders(document: str, fmt: str) -> list:
    """Every displayed date its own section's stated period does not cover."""
    extract = _s2t_md_dates if fmt == "md" else _s2t_html_dates
    found = []
    for stated, body in _s2t_sections(document, fmt):
        for origin, token in extract(body):
            if not _s2t_covers(stated, token):
                found.append((fmt, origin, token, stated))
    return found


def _s2t_counted(document: str, fmt: str) -> int:
    extract = _s2t_md_dates if fmt == "md" else _s2t_html_dates
    return sum(len(extract(body))
               for _stated, body in _s2t_sections(document, fmt))


def _s2t_template_sites():
    for template_id in sorted(t.id for t in _T2.SHARE_TEMPLATES):
        yield f"template:{template_id}", _s2_build_template(template_id)


def _s2t_builder_sites():
    """The `ShareSnapshot` sites outside the template registry and the CLI.

    Constructed here rather than reached through `_S2R_PERIOD_DRIVERS`,
    which returns period facts rather than snapshots.
    """
    import types as _types
    forecast = _cctally._load_sibling("_cctally_forecast")
    dash = _cctally._load_sibling("_cctally_dashboard_share")
    analytics = _cctally._load_sibling("_cctally_source_analytics")
    codex = _cctally._load_sibling("_cctally_codex")
    lib = _cctally._share_load_lib()
    result_cls = _s2r_load_sibling("_lib_source_analytics").SourceResult

    args = _s2r_budget_args("Etc/UTC")
    inputs = _types.SimpleNamespace(
        week_start_at=_dt2.datetime(2026, 5, 4, 15, 0,
                                    tzinfo=_dt2.timezone.utc),
        week_end_at=_dt2.datetime(2026, 5, 11, 15, 0,
                                  tzinfo=_dt2.timezone.utc),
        target_usd=50.0)
    status = _types.SimpleNamespace(
        spent_usd=10.0, consumption_pct=20.0, remaining_usd=40.0,
        daily_pace_usd=2.0, verdict="ok", low_confidence=False,
        projected_eow_low_usd=35.0, projected_eow_high_usd=40.0)
    now = _dt2.datetime(2026, 5, 9, 12, 0, tzinfo=_dt2.timezone.utc)

    previous = os.environ.get("CCTALLY_AS_OF")
    os.environ["CCTALLY_AS_OF"] = "2026-05-09T12:00:00Z"
    try:
        yield ("forecast:_build_budget_snapshot",
               forecast._build_budget_snapshot(
                   args, {"weekly_usd": 50.0}, inputs, status,
                   tz=_ZoneInfo("Etc/UTC")))
        yield ("forecast:_build_budget_no_data_snapshot",
               forecast._build_budget_no_data_snapshot(
                   args, {"weekly_usd": 50.0}, now))
        yield ("forecast:_build_budget_no_budget_snapshot",
               forecast._build_budget_no_budget_snapshot(args))
    finally:
        if previous is None:
            os.environ.pop("CCTALLY_AS_OF", None)
        else:
            os.environ["CCTALLY_AS_OF"] = previous

    for panel in _S2R_CODEX_SOURCE_PANELS:
        for case, bucketed in (("bucketed", True), ("unbucketed", False)):
            yield (f"dashboard:codex-source:{panel}:{case}",
                   dash._build_codex_source_share_snapshot(
                       lib, state=_s2r_codex_source_state(bucketed),
                       panel=panel, template_id=f"{panel}-recap",
                       options={"format": "md", "theme": "light",
                                "reveal_projects": False,
                                "display_tz": "Etc/UTC"}))

    yield ("analytics:build_source_share_snapshot",
           analytics.build_source_share_snapshot(
               "range-cost",
               result_cls(source="codex", status="unavailable",
                          data={"rangeStart": "2026-05-04T00:00:00Z",
                                "rangeEnd": "2026-05-09T12:00:00Z"}),
               reveal_projects=False))

    view = _types.SimpleNamespace(
        period_start=_dt2.datetime(2026, 5, 4, tzinfo=_dt2.timezone.utc),
        period_end=_dt2.datetime(2026, 5, 9, 12, 0, tzinfo=_dt2.timezone.utc),
        period_civil_bucket=True, display_tz_label="Etc/UTC",
        total_cost_usd=1.0)
    yield ("codex:_build_codex_share_snapshot",
           codex._build_codex_share_snapshot("codex-daily", view, ()))


_S2T_CLI_SITES = (
    ("report-md", "report"),
    ("daily-md", "daily"),
    ("monthly-md", "monthly"),
    ("weekly-md", "weekly"),
    ("session-md", "session"),
    ("forecast-md", "forecast"),
    ("project-md-anon", "project"),
    ("five-hour-blocks-md", "five-hour-blocks"),
)


def _s2t_render(snap, fmt: str) -> str:
    return _lib_share.render(snap, format=fmt, theme="light", branding=True,
                             reveal_projects=False)


def _s2t_all_sites():
    return list(_s2t_template_sites()) + list(_s2t_builder_sites())


@_s2_needs_fixtures
def test_every_snapshot_site_states_a_period_covering_what_it_displays():
    """NEVER NARROWER THAN THE CONTENT — every driveable builder site.

    The shipped guard enumerated `_T2.SHARE_TEMPLATES` only, so the
    command-line, budget, Codex and source-aware builders were outside
    it entirely.
    """
    offenders = []
    checked = 0
    sites = _s2t_all_sites()
    assert len(sites) >= 35, len(sites)
    for where, snap in sites:
        for fmt in ("md", "html"):
            document = _s2t_render(snap, fmt)
            checked += _s2t_counted(document, fmt)
            offenders += [(where, *o) for o in _s2t_offenders(document, fmt)]
    assert checked > 250, f"vacuous: only {checked} displayed dates were found"
    assert not offenders, offenders[:12]


@_s2r_needs_share_fixtures
def test_every_cli_artifact_states_a_period_covering_what_it_displays(tmp_path):
    """The same rule, driven through the real command line.

    These eight builders live in `bin/_cctally_share.py`, and none of
    them is constructible from a literal, so they are observed through
    the artifact the CLI actually writes. Markdown only: the HTML and
    SVG forms of these same commands are covered by the committed-corpus
    sweep below, and `--format html` writes to a file rather than to
    stdout.
    """
    offenders = []
    checked = 0
    for scenario, command in _S2T_CLI_SITES:
        document = _s2r_run_cli(scenario, command, tz="Etc/UTC",
                                tmp_path=tmp_path)
        checked += _s2t_counted(document, "md")
        offenders += [(scenario, *o) for o in _s2t_offenders(document, "md")]
    assert checked > 12, f"vacuous: only {checked} displayed dates were found"
    assert not offenders, offenders[:12]


_S2T_CORPUS_ROOTS = ("share", "share-v2", "source-aware", "budget")
# The census, MEASURED over the committed tree rather than predicted from
# any manifest. Exact rather than a floor, because the failure it replaces
# was a whole family leaving the sweep unnoticed, and a floor cannot
# observe that. Changing a count here is a deliberate act: a new golden
# family is added to the census in the same commit that adds the family.
_S2T_CORPUS_CENSUS = {"md": 133, "html": 122, "svg": 120}
# The files whose NAME carries no format token. They are what the previous
# filename classifier could not see; counted so this sweep cannot quietly
# stop reaching them.
_S2T_CORPUS_UNHINTED = 20


def _s2t_has_format_hint(name: str) -> bool:
    """Whether a filename states the format of the artifact inside it."""
    return (".md" in name or ".html" in name or ".svg" in name
            or name.endswith("-md-light.txt"))


def _s2t_artifact_format(text: str) -> "str | None":
    """The format a committed file IS, read from its own bytes.

    Classifying by FILENAME is what let a whole family out of the sweep.
    The nine `share-v2/compose` goldens are named `all-anon.golden`,
    `dark-composite-html.golden`, `no-branding-stripped.golden` and so on,
    and eleven `budget/*.txt` goldens spell their format with dashes
    (`golden-fmt-md-anon.txt`), so `".md" in name` / `".html" in name`
    excluded fifteen committed Markdown and HTML artifacts. Among them:
    every compose golden — the family the composite-period sweep below is
    named for — and `no-branding-stripped.golden`, the one golden whose
    stated period narrowed in this session's fourth round.
    """
    stripped = text.lstrip()
    if stripped.startswith("<svg"):
        return "svg"
    if stripped[:20].lower().startswith("<!doctype html"):
        return "html"
    # Every committed Markdown artifact opens with frontmatter; the facts
    # strip is the second reading, so a fragment golden would still be
    # seen. Neither matches this repository's prose Markdown — a
    # `CHANGELOG.md` or the delta ledger — which the filename rule read as
    # an artifact and then discarded on an empty section list.
    if text.startswith("---\n") or _S2T_FACTS_MD.search(text):
        return "md"
    return None


def _s2t_corpus():
    """Every committed share artifact in the tree, classified by CONTENT.

    The live sweeps above prove the MECHANISM on the sites a test can
    drive. This one proves the RESULT on what the repository actually
    ships, including the four `source-aware` composites whose builders no
    literal reconstructs — which is where the defect was.
    """
    root = _REPO_ROOT / "tests" / "fixtures"
    for name in _S2T_CORPUS_ROOTS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                # The v1 share fixtures commit their SQLite databases
                # beside their goldens.
                continue
            fmt = _s2t_artifact_format(text)
            if fmt is not None:
                yield path, fmt


@_s2_needs_fixtures
def test_the_committed_artifact_corpus_is_a_census_no_family_drops_out_of():
    """Every shipped artifact reaches the sweeps, whatever it is named.

    Two independent measurements of this corpus disagreed and are
    reconciled here. A review counted fifteen excluded artifacts by hand;
    a browser round re-extracted by content and reported 375 artifacts
    with a larger unhinted set. Both are readings of the same tree: 375
    artifacts exist, 20 of them carry no format token in their filename,
    and 15 of those 20 are Markdown or HTML — which is the set the two
    period sweeps below were missing.
    """
    seen = {"md": 0, "html": 0, "svg": 0}
    unhinted = []
    for path, fmt in _s2t_corpus():
        seen[fmt] += 1
        if not _s2t_has_format_hint(path.name):
            unhinted.append((fmt, path.relative_to(_REPO_ROOT).as_posix()))
    assert seen == _S2T_CORPUS_CENSUS, seen
    assert len(unhinted) == _S2T_CORPUS_UNHINTED, unhinted
    # Named rather than merely counted: these are the two families a
    # filename rule cannot classify, so their presence is what proves the
    # sniff is doing the work.
    assert sum(1 for _f, p in unhinted if "/compose/" in p) == 9, unhinted
    assert sum(1 for _f, p in unhinted if "/budget/" in p) == 11, unhinted


@_s2_needs_fixtures
def test_every_committed_artifact_states_a_period_covering_what_it_displays():
    """NEVER NARROWER THAN THE CONTENT — over the shipped corpus."""
    offenders = []
    checked = 0
    documents = 0
    for path, fmt in _s2t_corpus():
        if fmt == "svg":
            # SVG chrome is `<text>` too, so its displayed dates are not
            # structurally separable; `test_svg_and_html_state_the_same_
            # period_for_the_same_snapshot` anchors that format instead.
            continue
        document = path.read_text(encoding="utf-8")
        if not _s2t_sections(document, fmt):
            continue
        documents += 1
        checked += _s2t_counted(document, fmt)
        offenders += [(path.relative_to(_REPO_ROOT).as_posix(), *o)
                      for o in _s2t_offenders(document, fmt)]
    assert documents > 240, f"vacuous: only {documents} artifacts were read"
    assert checked > 700, f"vacuous: only {checked} displayed dates were found"
    assert not offenders, offenders[:12]


@_s2_needs_fixtures
def test_a_composed_markdown_period_covers_every_section_it_carries():
    """The frontmatter's union is the document's own claim about itself.

    The union is only a UNION in a document that carries more than one
    section, so a floor over all checks is satisfied by single-section
    artifacts that state the same period twice. `composite_checks` counts
    the sections that belong to a multi-section document, and is what
    makes this test non-vacuous about the composed case it is named for.
    """
    offenders = []
    checked = 0
    composite_checks = 0
    for path, fmt in _s2t_corpus():
        if fmt != "md":
            continue
        document = path.read_text(encoding="utf-8")
        declared = _S2T_FRONTMATTER_PERIOD.search(document)
        sections = _s2t_sections(document, "md")
        if declared is None or not sections:
            continue
        span = (declared.group(1)[:10], declared.group(2)[:10])
        for stated, _body in sections:
            checked += 1
            if len(sections) > 1:
                composite_checks += 1
            if not (_s2t_covers(span, stated[0])
                    and _s2t_covers(span, stated[1])):
                offenders.append(
                    (path.relative_to(_REPO_ROOT).as_posix(), span, stated))
    assert checked > 140, f"vacuous: only {checked} sections were checked"
    assert composite_checks > 35, (
        f"vacuous for COMPOSED documents: only {composite_checks} of "
        f"{checked} checked sections came from a multi-section artifact")
    assert not offenders, offenders[:12]


@_s2_needs_fixtures
def test_no_markdown_period_is_widened_by_a_chart_markdown_never_draws():
    """NEVER WIDER THAN WHAT THAT FORMAT SHOWS.

    Markdown renders no chart, so a chart may not move what a Markdown
    artifact says about itself. Observed by rendering each snapshot twice
    — once as built and once with its chart removed — and requiring the
    two Markdown artifacts to state the same period. The second render is
    what makes this non-tautological: the same code path, with one input
    taken away.

    The defect this closes: `weekly-recap`, `weekly-visual`,
    `blocks-recap` and `blocks-visual` stated the span their CHART covers
    above a Markdown document that draws no chart at all, so
    `weekly-recap` claimed `2026-03-16 → 2026-05-10` above one week's
    spend and `blocks-recap` claimed thirty-four hours above one block's.
    """
    offenders = []
    charted = 0
    for where, snap in _s2t_all_sites():
        if snap.chart is None:
            continue
        charted += 1
        with_chart = _s2t_sections(_s2t_render(snap, "md"), "md")[0][0]
        without = _s2t_sections(
            _s2t_render(_dc.replace(snap, chart=None), "md"), "md")[0][0]
        if with_chart != without:
            offenders.append((where, with_chart, without))
    assert charted >= 20, f"vacuous: only {charted} snapshots carry a chart"
    assert not offenders, offenders[:12]


# A panel whose declared window IS the span of its records: `daily`'s
# days, `monthly`'s months, `trend`'s weeks, `sessions`' sessions and
# `forecast`'s projection curve. Their TABLES draw that span in every
# format, so declaring it is a correct statement rather than an
# overstatement, and there is no smaller record to reduce them to.
_S2T_WINDOW_IS_THE_SPAN = frozenset(
    {"daily", "monthly", "trend", "sessions", "forecast"})
# A panel whose declared window is ABOUT one focal record, with the rest
# of its data drawn by some formats and not others.
_S2T_FOCAL_PANELS = frozenset({"weekly", "blocks", "current-week", "projects"})


def _s2t_focal_panel_data(panel: str, data: dict) -> "dict | None":
    """`panel_data` reduced to the records the panel's window is ABOUT.

    `None` for a panel in `_S2T_WINDOW_IS_THE_SPAN`, which has no smaller
    record to reduce to. `top_projects` is emptied wherever it appears: a
    cost ranking is never what a window is about.
    """
    if panel in _S2T_WINDOW_IS_THE_SPAN:
        return None
    trimmed = dict(data)
    if isinstance(trimmed.get("top_projects"), list):
        trimmed["top_projects"] = []
    if panel == "weekly":
        trimmed["weeks"] = [data["weeks"][data.get("current_week_index", 0)]]
        trimmed["current_week_index"] = 0
    elif panel == "blocks":
        trimmed["recent_blocks"] = []       # the focal record is current_block
    elif panel == "current-week":
        trimmed["daily_progression"] = []   # the focal record is the week
    elif panel == "projects":
        trimmed["rows"] = []                # explicit period_start/period_end
    return trimmed


@_s2_needs_fixtures
def test_no_declared_period_depends_on_history_the_focal_record_excludes():
    """NEVER WIDER — the rule, at the BUILDER, across every template.

    The overstatement lived in the builder's declared period, and the
    chart-removal sweep above cannot see it: restore the previous pass's
    `_weekly_period_bounds` and `_blocks_period_bounds` and that test
    still passes, because removing the chart does not change what the
    builder declared. Its only detector was six hardcoded parametrized
    expectations, which is the fixed-instance shape this epic keeps
    recording as the wrong answer.

    The checkable rule is the one those builders' docstrings state: the
    declared window must not depend on history the narrowest format does
    not draw. Observed by rebuilding each template from `panel_data`
    trimmed to the focal record and requiring the declared period to be
    unchanged — the same code path, with the surrounding history taken
    away.

    Every panel must be classified into one of the two sets, so a panel
    added later cannot be silently skipped.
    """
    panels = {tpl.panel for tpl in _T2.SHARE_TEMPLATES}
    assert panels == _S2T_WINDOW_IS_THE_SPAN | _S2T_FOCAL_PANELS, panels
    covered = 0
    offenders = []
    for tpl in sorted(_T2.SHARE_TEMPLATES, key=lambda t: t.id):
        trimmed = _s2t_focal_panel_data(tpl.panel, _s2_panel_data(tpl.panel))
        if trimmed is None:
            continue
        covered += 1
        full = _s2_build_template(tpl.id).period
        focal = _s2_build_template(tpl.id, panel_data=trimmed).period
        if ((full.start, full.end, full.civil_bucket)
                != (focal.start, focal.end, focal.civil_bucket)):
            offenders.append((tpl.id, (full.start, full.end),
                              (focal.start, focal.end)))
    assert covered >= 12, f"vacuous: only {covered} templates were rebuilt"
    assert not offenders, offenders


@_s2_needs_fixtures
def test_svg_and_html_state_the_same_period_for_the_same_snapshot():
    """The two formats display the same chart and the same table.

    SVG's own chrome makes a text-level extraction of its DISPLAYED dates
    unreliable — its title, facts strip, timestamp, totals and footer are
    `<text>` elements too — so its leg of the sweep is anchored to HTML,
    whose data regions are structurally identifiable.
    """
    for where, snap in _s2t_all_sites():
        html = _s2t_sections(_s2t_render(snap, "html"), "html")[0][0]
        svg_line = next(_s2t_plain(raw)
                        for raw in _S2T_SVG_TEXT.findall(
                            _s2t_render(snap, "svg"))
                        if "→" in raw)
        start, rest = svg_line.split(" → ", 1)
        assert html == (start.strip(), rest.split(" (", 1)[0].strip()), where


@_s2_needs_fixtures
def test_a_markdown_period_is_never_wider_than_its_own_html_period():
    """HTML displays everything Markdown does, plus the chart.

    So Markdown's stated period is bounded by HTML's on both sides. The
    reverse — Markdown claiming a span HTML does not — is the shape the
    overstatement took.
    """
    for where, snap in _s2t_all_sites():
        md = _s2t_sections(_s2t_render(snap, "md"), "md")[0][0]
        html = _s2t_sections(_s2t_render(snap, "html"), "html")[0][0]
        assert html[0] <= md[0] and md[1] <= html[1], (where, md, html)


@_s2_needs_fixtures
@_s2_pytest.mark.parametrize(
    "template_id,markdown,rich",
    [
        # The focal week, above one week's spend, in a format that draws
        # no chart — and the charted eight weeks where the chart exists.
        ("weekly-recap", ("2026-05-04", "2026-05-10"),
         ("2026-03-16", "2026-05-10")),
        ("weekly-visual", ("2026-05-04", "2026-05-10"),
         ("2026-03-16", "2026-05-10")),
        # `weekly-detail`'s TABLE carries the eight weeks, and a table is
        # drawn in every format, so this one does not differ.
        ("weekly-detail", ("2026-03-16", "2026-05-10"),
         ("2026-03-16", "2026-05-10")),
        ("blocks-recap", ("2026-05-08", "2026-05-08"),
         ("2026-05-07", "2026-05-08")),
        ("blocks-visual", ("2026-05-08", "2026-05-08"),
         ("2026-05-07", "2026-05-08")),
        ("blocks-detail", ("2026-05-07", "2026-05-08"),
         ("2026-05-07", "2026-05-08")),
    ],
)
def test_the_weekly_and_blocks_panels_state_the_span_each_format_draws(
        template_id, markdown, rich):
    """The four artifacts the overstatement was measured on, plus their
    two siblings whose tables make the wide span genuine."""
    snap = _s2_build_template(template_id)
    assert _s2t_sections(_s2t_render(snap, "md"), "md")[0][0] == markdown
    assert _s2t_sections(_s2t_render(snap, "html"), "html")[0][0] == rich


# =====================================================================
# #503 S2 fourth review — the END side of the completion.
#
# `effective_period` widens both bounds to midnight of the widened civil
# date. That is the minimal bound consistent with the strip on the START
# side and WRONG on the end side, twice over:
#
#   * A displayed value may be a full ISO INSTANT — the Codex quota
#     `blocks` panel's `Resets` column prints `2026-05-07T18:00:00+00:00`
#     — and midnight of its own day precedes it, so the machine-readable
#     frontmatter bound excluded content the civil date admits.
#   * `2026-05` widened to `2026-05-01` is right as a start bound and
#     backwards as an end bound: a displayed May bucket above an earlier
#     period ended the stated window on the first of the month.
#
# Neither is reachable through a committed golden, so both are asserted
# at the kernel.
# =====================================================================

def _s2t_period(start, end, *, civil_bucket=False, display_tz="Etc/UTC"):
    return _lib_share.PeriodSpec(start=start, end=end, label="window",
                                 display_tz=display_tz,
                                 civil_bucket=civil_bucket)


def _s2t_snapshot_with_period(period, *, columns, rows, chart=None):
    return ShareSnapshot(
        cmd="blocks", title="Quota blocks", subtitle=None, period=period,
        columns=columns, rows=rows, chart=chart, totals=(), notes=(),
        generated_at=_S2_GENERATED_AT, version="9.9.9")


def test_a_widened_end_bound_covers_the_instant_the_artifact_prints():
    """An end completed from an INSTANT must not precede that instant.

    The `Resets` column renders a raw ISO timestamp, so the artifact
    displays the instant itself rather than a date derived from it. The
    stated civil date must not move — that is what a next-midnight bound
    would break — while the machine bound must reach the value on the
    page.
    """
    printed = _dt2.datetime(2026, 5, 8, 10, 0, tzinfo=_dt2.timezone.utc)
    period = _s2t_period(
        _dt2.datetime(2026, 5, 7, 5, 0, tzinfo=_dt2.timezone.utc),
        _dt2.datetime(2026, 5, 7, 10, 0, tzinfo=_dt2.timezone.utc))
    snap = _s2t_snapshot_with_period(
        period,
        columns=(ColumnSpec(key="resets", label="Resets", align="left"),),
        rows=(Row(cells={"resets": TextCell(
            printed.isoformat().replace("+00:00", "+00:00"))}),))
    widened = _lib_share.effective_period(snap, shows_chart=False,
                                          shows_table=True)
    assert _lib_share.period_civil_dates(widened) == ("2026-05-07",
                                                      "2026-05-08")
    assert widened.end >= printed, widened.end


def test_a_widened_start_bound_covers_a_utc_instant_west_of_utc():
    """The serialized start is absolute even when the strip is civil.

    New York midnight is 04:00Z in May.  A row that prints 02:00Z on
    the widened UTC date must not sit before the frontmatter bound, while
    the facts strip must continue to name the date printed by the row.
    """
    printed = _dt2.datetime(2026, 5, 7, 2, 0, tzinfo=_dt2.timezone.utc)
    period = _s2t_period(
        _dt2.datetime(2026, 5, 8, 5, 0, tzinfo=_dt2.timezone.utc),
        _dt2.datetime(2026, 5, 8, 6, 0, tzinfo=_dt2.timezone.utc),
        display_tz="America/New_York")
    snap = _s2t_snapshot_with_period(
        period,
        columns=(ColumnSpec(key="resets", label="Resets", align="left"),),
        rows=(Row(cells={"resets": TextCell(printed.isoformat())}),))

    facts = _s2r_facts_from_markdown(_s2t_render(snap, "md"))

    assert facts.start_utc <= printed, facts
    assert (facts.civil_start, facts.civil_end) == ("2026-05-07",
                                                    "2026-05-08")


def test_a_widened_end_bound_covers_a_utc_instant_east_of_utc():
    """Tokyo's local end-of-day must not truncate a printed UTC row."""
    printed = _dt2.datetime(2026, 5, 8, 18, 0, 0, 500000,
                            tzinfo=_dt2.timezone.utc)
    period = _s2t_period(
        _dt2.datetime(2026, 5, 7, 0, 0, tzinfo=_dt2.timezone.utc),
        _dt2.datetime(2026, 5, 7, 10, 0, tzinfo=_dt2.timezone.utc),
        display_tz="Asia/Tokyo")
    snap = _s2t_snapshot_with_period(
        period,
        columns=(ColumnSpec(key="resets", label="Resets", align="left"),),
        rows=(Row(cells={"resets": TextCell(printed.isoformat())}),))

    facts = _s2r_facts_from_markdown(_s2t_render(snap, "md"))

    assert facts.end_utc >= printed, facts
    assert (facts.civil_start, facts.civil_end) == ("2026-05-07",
                                                    "2026-05-08")


def test_a_widened_end_bound_from_a_month_bucket_covers_the_whole_month():
    """`2026-05` as an END bound is the last of May, not the first."""
    period = _s2t_period(
        _dt2.datetime(2026, 4, 1, tzinfo=_dt2.timezone.utc),
        _dt2.datetime(2026, 4, 30, tzinfo=_dt2.timezone.utc),
        civil_bucket=True)
    snap = _s2t_snapshot_with_period(
        period,
        columns=(ColumnSpec(key="month", label="Month", align="left"),),
        rows=(Row(cells={"month": TextCell("2026-05")}),))
    widened = _lib_share.effective_period(snap, shows_chart=False,
                                          shows_table=True)
    assert _lib_share.period_civil_dates(widened) == ("2026-04-01",
                                                      "2026-05-31")


def test_a_widened_start_bound_still_names_the_first_of_its_month():
    """The start side is unchanged: a month bucket opens on the first."""
    period = _s2t_period(
        _dt2.datetime(2026, 6, 1, tzinfo=_dt2.timezone.utc),
        _dt2.datetime(2026, 6, 30, tzinfo=_dt2.timezone.utc),
        civil_bucket=True)
    snap = _s2t_snapshot_with_period(
        period,
        columns=(ColumnSpec(key="month", label="Month", align="left"),),
        rows=(Row(cells={"month": TextCell("2026-05")}),))
    widened = _lib_share.effective_period(snap, shows_chart=False,
                                          shows_table=True)
    assert _lib_share.period_civil_dates(widened) == ("2026-05-01",
                                                      "2026-06-30")


def test_displayed_dates_reads_every_series_a_bar_chart_draws():
    """`_chart_points` claimed completeness it did not have.

    It read `points` and `multi_series` while `BarChart` also carries
    `stacks`, which `_render_bar_chart_svg` draws as cumulative segments.
    No shipped builder exercises the gap — `_build_weekly_snapshot`
    builds its stack points from the same `week_label` values as
    `points` — but it is the same blindness the `DateCell` walk had, and
    the docstring asserted the coverage either way.
    """
    chart = BarChart(
        points=(ChartPoint(x_label="2026-05-04", x_value=0.0, y_value=1.0),),
        y_label="$",
        stacks={"opus": (ChartPoint(x_label="2026-05-11", x_value=0.0,
                                    y_value=1.0),)})
    snap = _s2t_snapshot_with_period(
        _s2t_period(_dt2.datetime(2026, 5, 4, tzinfo=_dt2.timezone.utc),
                    _dt2.datetime(2026, 5, 10, tzinfo=_dt2.timezone.utc),
                    civil_bucket=True),
        columns=(), rows=(), chart=chart)
    tokens = _lib_share.displayed_dates(snap, shows_chart=True,
                                        shows_table=False)
    assert "2026-05-11" in tokens, tokens
    widened = _lib_share.effective_period(snap, shows_chart=True,
                                          shows_table=False)
    assert _lib_share.period_civil_dates(widened)[1] == "2026-05-11"
