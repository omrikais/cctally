"""Unit tests for the provider-parameterized cache-report wire builder (#443 S2).

The builder is the single serializer behind all three cache-report envelope
sites. Its safety property is that the Claude branch emits exactly what the
hand-built Claude serializer emitted, so every field S2 introduces has to be
Codex-only and optional.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys


def _load():
    p = pathlib.Path(__file__).resolve().parents[1] / "bin" / "_lib_cache_report_wire.py"
    spec = importlib.util.spec_from_file_location("_lib_cache_report_wire", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_lib_cache_report_wire"] = mod
    spec.loader.exec_module(mod)
    return mod


WIRE = _load()


def _today(**over):
    base = {
        "date": "2026-08-01", "cache_hit_percent": 71.0,
        "baseline_median_percent": 67.0, "delta_pp": 4.0,
        "net_usd": 1.5, "saved_usd": 2.0, "wasted_usd": 0.5,
        "anomaly_triggered": False, "anomaly_reasons": [],
        "baseline_daily_row_count": 9,
        "anomaly_unevaluated": [], "observed": True,
    }
    base.update(over)
    return base


def _day(**over):
    base = {
        "date": "2026-08-01", "cache_hit_percent": 71.0,
        "input_tokens": 10, "output_tokens": 20,
        "cache_creation_tokens": 30, "cache_read_tokens": 40,
        "saved_usd": 2.0, "wasted_usd": 0.5, "net_usd": 1.5,
        "anomaly_triggered": False, "anomaly_reasons": [],
        "anomaly_unevaluated": [], "observed": True,
    }
    base.update(over)
    return base


def _call(provider, **over):
    kwargs = dict(
        provider=provider, window_days=14, anomaly_threshold_pp=15,
        anomaly_window_days=14, today=_today(), days=[_day()],
        by_project=[{"key": "p", "cache_hit_percent": 71.0, "net_usd": 1.5}],
        by_model=[{"key": "m", "cache_hit_percent": 71.0, "net_usd": 1.5}],
        seven_day_net_usd=1.5, seven_day_anomaly_count=0,
        fourteen_day_counterfactual_usd=2.0,
        fourteen_day_efficiency_ratio=0.8, is_empty=False,
    )
    kwargs.update(over)
    return WIRE.build_cache_report_wire(**kwargs)


def test_claude_branch_emits_no_s2_fields():
    out = _call("claude")
    assert "not_applicable" not in out
    assert "anomaly_predicates" not in out
    assert "cached_input_percent" not in out["today"]
    assert "cached_input_percent" not in out["days"][0]
    assert "cached_input_percent" not in out["by_project"][0]
    assert out["today"]["cache_hit_percent"] == 71.0


def test_codex_branch_publishes_only_authoritative_percent_everywhere():
    out = _call("codex")
    for block in (out["today"], out["days"][0], out["by_project"][0], out["by_model"][0]):
        assert block["cached_input_percent"] == 71.0
        assert "cache_hit_percent" not in block


def test_codex_branch_publishes_metadata_with_null_values():
    out = _call("codex")
    assert set(out["not_applicable"]) == {"wasted_usd", "fourteen_day_efficiency_ratio"}
    assert out["anomaly_predicates"] == ["cache_drop"]
    assert out["today"]["wasted_usd"] is None
    assert out["days"][0]["wasted_usd"] is None
    assert out["fourteen_day_efficiency_ratio"] is None


def test_unknown_provider_is_rejected():
    import pytest
    with pytest.raises(ValueError):
        _call("gemini")


def test_codex_filter_drops_an_inapplicable_reason_and_recomputes():
    row = _day(
        anomaly_triggered=True,
        anomaly_reasons=["net_negative"],
        anomaly_unevaluated=["cache_drop"],
    )
    out = WIRE.filter_inapplicable("codex", row)
    assert out["anomaly_reasons"] == []
    assert out["anomaly_unevaluated"] == ["cache_drop"]
    # Nothing applicable survived, so the verdict must be recomputed, not kept.
    assert out["anomaly_triggered"] is False


def test_codex_filter_keeps_an_applicable_reason():
    row = _day(anomaly_triggered=True, anomaly_reasons=["cache_drop"])
    out = WIRE.filter_inapplicable("codex", row)
    assert out["anomaly_reasons"] == ["cache_drop"]
    assert out["anomaly_triggered"] is True


def test_claude_filter_is_identity():
    row = _day(anomaly_triggered=True, anomaly_reasons=["net_negative"])
    assert WIRE.filter_inapplicable("claude", row) == row


def test_codex_filter_reaches_every_row_through_the_builder():
    out = _call(
        "codex",
        today=_today(anomaly_triggered=True, anomaly_reasons=["net_negative"],
                     anomaly_unevaluated=["net_negative", "cache_drop"]),
        days=[_day(anomaly_triggered=True, anomaly_reasons=["net_negative"],
                   anomaly_unevaluated=["net_negative", "cache_drop"])],
    )
    assert out["today"]["anomaly_reasons"] == []
    assert out["today"]["anomaly_triggered"] is False
    assert out["today"]["anomaly_unevaluated"] == ["cache_drop"]
    assert out["days"][0]["anomaly_reasons"] == []
    assert out["days"][0]["anomaly_triggered"] is False
    assert out["days"][0]["anomaly_unevaluated"] == ["cache_drop"]


def test_seven_day_anomaly_count_is_reconciled_after_filtering():
    rows = [_day(date=f"2026-07-{d:02d}", anomaly_triggered=True,
                 anomaly_reasons=["net_negative"]) for d in range(25, 32)]
    out = _call("codex", days=rows, seven_day_anomaly_count=7)
    # Every reason was inapplicable, so the count the caller supplied is stale.
    assert out["seven_day_anomaly_count"] == 0


def test_claude_seven_day_anomaly_count_is_passed_through_untouched():
    """Claude byte-stability: the builder must not re-derive a Claude count."""
    rows = [_day(date=f"2026-07-{d:02d}", anomaly_triggered=True,
                 anomaly_reasons=["net_negative"]) for d in range(25, 32)]
    out = _call("claude", days=rows, seven_day_anomaly_count=3)
    assert out["seven_day_anomaly_count"] == 3
