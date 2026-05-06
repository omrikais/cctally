"""Tests for diff data containers + asymmetric-row encoding."""
from conftest import load_script


def _ns():
    return load_script()


def test_metric_bundle_zero_initialized():
    ns = _ns()
    MB = ns["MetricBundle"]
    mb = MB(cost_usd=0.0, tokens_input=0, tokens_output=0,
            tokens_cache_read=0, tokens_cache_write=0,
            cache_hit_pct=None, used_pct=None)
    assert mb.cost_usd == 0.0
    assert mb.cache_hit_pct is None


def test_build_delta_bundle_changed_status():
    ns = _ns()
    MB = ns["MetricBundle"]
    build = ns["_build_delta_bundle"]
    a = MB(cost_usd=10.0, tokens_input=100, tokens_output=200,
           tokens_cache_read=0, tokens_cache_write=0,
           cache_hit_pct=80.0, used_pct=50.0)
    b = MB(cost_usd=15.0, tokens_input=150, tokens_output=300,
           tokens_cache_read=0, tokens_cache_write=0,
           cache_hit_pct=70.0, used_pct=60.0)
    delta = build(a, b)
    assert delta.cost_usd == 5.0
    assert abs(delta.cost_usd_pct - 50.0) < 1e-9
    assert delta.tokens_input == 50
    assert delta.cache_hit_pct_pp == -10.0
    assert delta.used_pct_pp == 10.0


def test_build_delta_bundle_new_row_status():
    ns = _ns()
    MB = ns["MetricBundle"]
    build = ns["_build_delta_bundle"]
    b = MB(cost_usd=2.31, tokens_input=410_000, tokens_output=0,
           tokens_cache_read=0, tokens_cache_write=0,
           cache_hit_pct=81.0, used_pct=None)
    delta = build(None, b)
    assert delta.cost_usd == 2.31
    assert delta.cost_usd_pct is None
    assert delta.tokens_input == 410_000
    assert delta.tokens_input_pct is None


def test_build_delta_bundle_dropped_row_status():
    ns = _ns()
    MB = ns["MetricBundle"]
    build = ns["_build_delta_bundle"]
    a = MB(cost_usd=0.42, tokens_input=8100, tokens_output=0,
           tokens_cache_read=0, tokens_cache_write=0,
           cache_hit_pct=45.0, used_pct=None)
    delta = build(a, None)
    assert delta.cost_usd == -0.42
    assert delta.cost_usd_pct is None
    assert delta.tokens_input == -8100


def test_build_delta_bundle_zero_a_avoids_divide_by_zero():
    ns = _ns()
    MB = ns["MetricBundle"]
    build = ns["_build_delta_bundle"]
    a = MB(cost_usd=0.0, tokens_input=0, tokens_output=0,
           tokens_cache_read=0, tokens_cache_write=0,
           cache_hit_pct=None, used_pct=None)
    b = MB(cost_usd=5.0, tokens_input=100, tokens_output=0,
           tokens_cache_read=0, tokens_cache_write=0,
           cache_hit_pct=80.0, used_pct=None)
    delta = build(a, b)
    assert delta.cost_usd == 5.0
    assert delta.cost_usd_pct is None


def test_humanize_tokens_none_returns_em_dash():
    ns = _ns()
    h = ns["_humanize_tokens"]
    assert h(None) == "—"


def test_humanize_tokens_under_1k_returns_plain_int():
    ns = _ns()
    h = ns["_humanize_tokens"]
    assert h(0) == "0"
    assert h(999) == "999"


def test_humanize_tokens_thousands():
    ns = _ns()
    h = ns["_humanize_tokens"]
    assert h(1_000) == "1.0K"
    assert h(1_234) == "1.2K"
    assert h(999_999) == "1000.0K"  # known precision quirk; pin it


def test_humanize_tokens_millions_and_billions():
    ns = _ns()
    h = ns["_humanize_tokens"]
    assert h(1_000_000) == "1.0M"
    assert h(1_500_000) == "1.5M"
    assert h(1_000_000_000) == "1.0B"


def test_humanize_tokens_negative():
    ns = _ns()
    h = ns["_humanize_tokens"]
    assert h(-1_500) == "-1.5K"
    assert h(-1) == "-1"
