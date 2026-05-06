"""Tests for diff renderer cell-formatter helpers."""
from conftest import load_script


def _ns():
    return load_script()


def test_cost_cell_formats_a_to_b():
    fmt = _ns()["_diff_fmt_cost_cell"]
    assert fmt(12.43, 18.91) == "$12.43 → $18.91"


def test_cost_cell_uses_emdash_for_missing_side():
    fmt = _ns()["_diff_fmt_cost_cell"]
    assert fmt(None, 2.31) == "— → $2.31"
    assert fmt(0.42, None) == "$0.42 → —"


def test_delta_cost_cell_signs_and_percent():
    fmt = _ns()["_diff_fmt_delta_cost_cell"]
    assert fmt(6.48, 52.13) == "+$6.48 (+52%)"
    assert fmt(-3.20, -25.0) == "-$3.20 (-25%)"


def test_delta_cost_cell_emdash_for_undefined_pct():
    fmt = _ns()["_diff_fmt_delta_cost_cell"]
    assert fmt(2.31, None) == "+$2.31 (—)"
    assert fmt(-0.42, None) == "-$0.42 (—)"


def test_pct_cell_no_decimals():
    fmt = _ns()["_diff_fmt_pct_cell"]
    assert fmt(84.2, 79.1) == "84% → 79%"
    assert fmt(None, 81.0) == "— → 81%"


def test_pp_cell_signed_with_pp_suffix():
    fmt = _ns()["_diff_fmt_pp_cell"]
    assert fmt(-5.1) == "-5pp"
    assert fmt(12.0) == "+12pp"
    assert fmt(None) == "—"


def test_tokens_cell_humanized():
    fmt = _ns()["_diff_fmt_tokens_cell"]
    assert fmt(2_100_000, 3_000_000) == "2.1M → 3.0M"
    assert fmt(None, 410_000) == "— → 410.0K"


def test_delta_tokens_cell_signed():
    fmt = _ns()["_diff_fmt_delta_tokens_cell"]
    assert fmt(900_000) == "+900.0K"
    assert fmt(-8100) == "-8.1K"
    assert fmt(None) == "—"


def test_color_for_delta_cost_positive_is_red():
    color = _ns()["_diff_color_for_delta"]
    assert color("cost", 5.0, enabled=True) == "31"


def test_color_for_delta_cost_negative_is_green():
    color = _ns()["_diff_color_for_delta"]
    assert color("cost", -3.0, enabled=True) == "32"


def test_color_for_cache_pp_positive_is_green():
    color = _ns()["_diff_color_for_delta"]
    assert color("cache_pp", 5.0, enabled=True) == "32"


def test_color_disabled_returns_empty_string():
    color = _ns()["_diff_color_for_delta"]
    assert color("cost", 5.0, enabled=False) == ""


import datetime as dt


def _utc(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)


def test_banner_contains_title():
    ns = _ns()
    banner = ns["_diff_render_banner"]()
    assert "Diff" in banner
    assert banner.count("\n") >= 4


def test_window_header_two_lines_with_dates():
    ns = _ns()
    ParsedWindow = ns["ParsedWindow"]
    pw_a = ParsedWindow(label="this-week",
                        start_utc=_utc("2026-04-19T07:00:00Z"),
                        end_utc=_utc("2026-04-25T19:30:00Z"),
                        length_days=6.521, kind="week",
                        week_aligned=True, full_weeks_count=1)
    pw_b = ParsedWindow(label="last-week",
                        start_utc=_utc("2026-04-12T07:00:00Z"),
                        end_utc=_utc("2026-04-19T07:00:00Z"),
                        length_days=7.0, kind="week",
                        week_aligned=True, full_weeks_count=1)
    DiffResult = ns["DiffResult"]
    NT = ns["NoiseThreshold"]
    result = DiffResult(window_a=pw_a, window_b=pw_b,
                        mismatched_length=False, normalization="none",
                        used_pct_mode_a="exact", used_pct_mode_b="exact",
                        sections=[], threshold=NT())
    text = ns["_diff_render_window_header"](result, color=False)
    assert "A: this-week" in text
    assert "B: last-week" in text
    assert "exact Used %" in text


def test_window_header_warns_on_mismatched_length():
    ns = _ns()
    ParsedWindow = ns["ParsedWindow"]
    DiffResult = ns["DiffResult"]
    NT = ns["NoiseThreshold"]
    pw_a = ParsedWindow(label="last-7d",
                        start_utc=_utc("2026-04-18T00:00:00Z"),
                        end_utc=_utc("2026-04-25T00:00:00Z"),
                        length_days=7.0, kind="day-range",
                        week_aligned=False, full_weeks_count=0)
    pw_b = ParsedWindow(label="prev-14d",
                        start_utc=_utc("2026-04-04T00:00:00Z"),
                        end_utc=_utc("2026-04-18T00:00:00Z"),
                        length_days=14.0, kind="day-range",
                        week_aligned=False, full_weeks_count=0)
    result = DiffResult(window_a=pw_a, window_b=pw_b,
                        mismatched_length=True, normalization="per-day",
                        used_pct_mode_a="n/a", used_pct_mode_b="n/a",
                        sections=[], threshold=NT())
    text = ns["_diff_render_window_header"](result, color=False)
    assert "Mismatched window lengths" in text
    assert "normalized per-day" in text


def test_section_table_renders_borders_and_rows():
    ns = _ns()
    DiffSection = ns["DiffSection"]
    DiffRow = ns["DiffRow"]
    MB = ns["MetricBundle"]
    ColumnSpec = ns["ColumnSpec"]
    a = MB(12.43, 21034, 1840562, 14_000_000, 421000, 84.2, None)
    b = MB(18.91, 30412, 2640122, 19_200_000, 580000, 79.1, None)
    delta = ns["_build_delta_bundle"](a, b)
    rows = [DiffRow("model:s46", "claude-sonnet-4-6", "changed",
                    a, b, delta, sort_key=6.48)]
    section = DiffSection(
        name="models", scope="all", rows=rows, hidden_count=0,
        columns=[
            ColumnSpec("cost_usd", "Cost", "usd", False),
            ColumnSpec("cache_hit_pct", "Cache %", "pct", False),
            ColumnSpec("tokens_input", "Tokens", "tokens", False),
        ],
    )
    text = ns["_diff_render_section_table"](
        section, total_a=a, total_b=b, width=144, color=False,
        used_pct_mode_a="exact", used_pct_mode_b="exact",
    )
    assert "Models" in text
    assert any(ch in text for ch in ("┌", "+"))
    assert any(ch in text for ch in ("├", "+"))
    assert any(ch in text for ch in ("└", "+"))
    assert "claude-sonnet-4-6" in text
    assert "$12.43 → $18.91" in text
    assert "+$6.48" in text
    assert "Total" in text


def test_section_table_marks_new_and_dropped_rows():
    ns = _ns()
    DiffSection = ns["DiffSection"]
    DiffRow = ns["DiffRow"]
    MB = ns["MetricBundle"]
    ColumnSpec = ns["ColumnSpec"]
    new_row = DiffRow(
        "model:new", "claude-sonnet-4-7", "new",
        a=None, b=MB(2.31, 410_000, 0, 0, 0, 81.0, None),
        delta=ns["_build_delta_bundle"](None, MB(2.31, 410_000, 0, 0, 0, 81.0, None)),
        sort_key=2.31,
    )
    dropped = DiffRow(
        "model:drop", "claude-haiku-4-5", "dropped",
        a=MB(0.42, 8100, 0, 0, 0, 45.0, None), b=None,
        delta=ns["_build_delta_bundle"](MB(0.42, 8100, 0, 0, 0, 45.0, None), None),
        sort_key=0.42,
    )
    total_a = MB(0.42, 8100, 0, 0, 0, 45.0, None)
    total_b = MB(2.31, 410_000, 0, 0, 0, 81.0, None)
    section = DiffSection(
        name="models", scope="all", rows=[new_row, dropped], hidden_count=0,
        columns=[ColumnSpec("cost_usd", "Cost", "usd", False),
                 ColumnSpec("cache_hit_pct", "Cache %", "pct", False),
                 ColumnSpec("tokens_input", "Tokens", "tokens", False)],
    )
    text = ns["_diff_render_section_table"](
        section, total_a=total_a, total_b=total_b,
        width=144, color=False, used_pct_mode_a="exact", used_pct_mode_b="exact",
    )
    assert "(new)" in text
    assert "(dropped)" in text
    assert "— → $2.31" in text
    assert "$0.42 → —" in text


def test_section_table_emits_hidden_count_footer():
    ns = _ns()
    DiffSection = ns["DiffSection"]
    DiffRow = ns["DiffRow"]
    MB = ns["MetricBundle"]
    ColumnSpec = ns["ColumnSpec"]
    a = MB(10.0, 0, 0, 0, 0, None, None)
    b = MB(11.0, 0, 0, 0, 0, None, None)
    visible = DiffRow("model:big", "big", "changed", a, b,
                      ns["_build_delta_bundle"](a, b), sort_key=1.0)
    section = DiffSection(
        name="models", scope="all", rows=[visible], hidden_count=12,
        columns=[ColumnSpec("cost_usd", "Cost", "usd", False),
                 ColumnSpec("cache_hit_pct", "Cache %", "pct", False),
                 ColumnSpec("tokens_input", "Tokens", "tokens", False)],
    )
    text = ns["_diff_render_section_table"](
        section, total_a=a, total_b=b, width=144, color=False,
        used_pct_mode_a="n/a", used_pct_mode_b="n/a",
    )
    assert "12 rows hidden" in text
    assert "$0.10" in text


def test_section_table_footer_uses_threshold_values_when_overridden():
    """Footer literal `$0.10`/`1.0` is replaced with user's actual
    `--min-delta` overrides when a threshold is threaded through."""
    ns = _ns()
    DiffSection = ns["DiffSection"]
    DiffRow = ns["DiffRow"]
    MB = ns["MetricBundle"]
    NoiseThreshold = ns["NoiseThreshold"]
    ColumnSpec = ns["ColumnSpec"]
    a = MB(10.0, 0, 0, 0, 0, None, None)
    b = MB(11.0, 0, 0, 0, 0, None, None)
    visible = DiffRow("model:big", "big", "changed", a, b,
                      ns["_build_delta_bundle"](a, b), sort_key=1.0)
    section = DiffSection(
        name="models", scope="all", rows=[visible], hidden_count=12,
        columns=[ColumnSpec("cost_usd", "Cost", "usd", False),
                 ColumnSpec("cache_hit_pct", "Cache %", "pct", False),
                 ColumnSpec("tokens_input", "Tokens", "tokens", False)],
    )
    overridden = NoiseThreshold(min_delta_usd=0.50, min_delta_pct=2.5,
                                user_override=True)
    text = ns["_diff_render_section_table"](
        section, total_a=a, total_b=b, width=144, color=False,
        used_pct_mode_a="n/a", used_pct_mode_b="n/a",
        threshold=overridden,
    )
    assert "12 rows hidden" in text
    assert "$0.50" in text
    assert "2.5" in text
    assert "$0.10" not in text
