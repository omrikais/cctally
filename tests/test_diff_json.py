"""Tests for diff JSON envelope shape + stability."""
import datetime as dt
import json

from conftest import load_script


def _ns():
    return load_script()


def _utc(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)


def _make_result(ns):
    ParsedWindow = ns["ParsedWindow"]
    DiffResult = ns["DiffResult"]
    DiffSection = ns["DiffSection"]
    DiffRow = ns["DiffRow"]
    MB = ns["MetricBundle"]
    ColumnSpec = ns["ColumnSpec"]
    NT = ns["NoiseThreshold"]

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
    a = MB(12.43, 21034, 1840562, 14_000_000, 421000, 84.2, 76.0)
    b = MB(18.91, 30412, 2640122, 19_200_000, 580000, 79.1, 88.0)
    delta = ns["_build_delta_bundle"](a, b)
    overall_row = DiffRow("overall", "Overall", "changed", a, b, delta, sort_key=6.48)
    overall = DiffSection(
        "overall", "all", [overall_row], hidden_count=0,
        columns=[ColumnSpec("cost_usd", "Cost", "usd", True)],
    )
    return DiffResult(
        window_a=pw_a, window_b=pw_b,
        mismatched_length=False, normalization="none",
        used_pct_mode_a="exact", used_pct_mode_b="exact",
        sections=[overall], threshold=NT(),
    )


def test_json_top_level_keys_match_spec():
    ns = _ns()
    result = _make_result(ns)
    payload = ns["_diff_to_json_payload"](result, options={})
    assert payload["schema_version"] == 1
    assert payload["subcommand"] == "diff"
    assert "windows" in payload
    assert payload["windows"]["a"]["label"] == "this-week"
    assert payload["windows"]["a"]["used_pct_mode"] == "exact"
    assert payload["windows"]["a"]["start_at"] == "2026-04-19T07:00:00Z"
    assert payload["mismatched_length"] is False
    assert payload["normalization"] == "none"
    assert "options" in payload
    assert isinstance(payload["sections"], list)


def test_json_changed_row_has_a_b_delta():
    ns = _ns()
    result = _make_result(ns)
    payload = ns["_diff_to_json_payload"](result, options={})
    overall = payload["sections"][0]["rows"][0]
    assert overall["status"] == "changed"
    assert overall["a"]["cost_usd"] == 12.43
    assert overall["b"]["cost_usd"] == 18.91
    assert abs(overall["delta"]["cost_usd"] - 6.48) < 1e-9


def test_json_new_row_a_is_null():
    ns = _ns()
    DiffRow = ns["DiffRow"]
    DiffSection = ns["DiffSection"]
    MB = ns["MetricBundle"]
    ColumnSpec = ns["ColumnSpec"]
    result = _make_result(ns)
    new_row = DiffRow(
        "model:s47", "claude-sonnet-4-7", "new",
        a=None, b=MB(2.31, 410_000, 0, 0, 0, 81.0, None),
        delta=ns["_build_delta_bundle"](None, MB(2.31, 410_000, 0, 0, 0, 81.0, None)),
        sort_key=2.31,
    )
    section = DiffSection(
        "models", "all", [new_row], hidden_count=0,
        columns=[ColumnSpec("cost_usd", "Cost", "usd", False)],
    )
    result.sections.append(section)
    payload = ns["_diff_to_json_payload"](result, options={})
    models = next(s for s in payload["sections"] if s["name"] == "models")
    row = models["rows"][0]
    assert row["status"] == "new"
    assert row["a"] is None
    assert row["b"]["cost_usd"] == 2.31
    assert row["delta"]["cost_usd"] == 2.31
    assert row["delta"]["cost_usd_pct"] is None


def test_json_render_returns_indent_2_string():
    ns = _ns()
    result = _make_result(ns)
    s = ns["_diff_render_json"](result, options={})
    parsed = json.loads(s)
    assert parsed["schema_version"] == 1
    assert "\n  " in s


def test_json_now_hook_makes_generated_at_deterministic():
    """Passing an explicit `now` kwarg pins `generated_at` byte-exactly,
    so CCTALLY_AS_OF in tests/fixtures yields a stable JSON envelope."""
    ns = _ns()
    result = _make_result(ns)
    pinned = _utc("2026-04-25T19:30:00Z")
    payload = ns["_diff_to_json_payload"](result, options={}, now=pinned)
    assert payload["generated_at"] == "2026-04-25T19:30:00Z"
    rendered = ns["_diff_render_json"](result, options={}, now=pinned)
    assert '"generated_at": "2026-04-25T19:30:00Z"' in rendered
