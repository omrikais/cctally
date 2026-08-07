"""Layer A unit tests for bin/_lib_share.py."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import sys
from datetime import datetime, timezone

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
        theme="light",
        reveal_projects=False,
        top_n=None,
        tz=timezone.utc,
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
