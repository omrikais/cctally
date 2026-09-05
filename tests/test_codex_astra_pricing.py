"""GPT-6 Astra uses OpenAI's published rate card without fallback."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
MODEL = "gpt-6-astra"

ASTRA_CARD = {
    "input_cost_per_token": 1e-05,
    "cache_read_input_token_cost": 1e-06,
    "output_cost_per_token": 5e-05,
    "input_cost_per_token_above_272k_tokens": 2e-05,
    "cache_read_input_token_cost_above_272k_tokens": 2e-06,
    "output_cost_per_token_above_272k_tokens": 7.5e-05,
}


def _load(module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name, BIN / f"{module_name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


pricing = _load("_lib_pricing")
pricing_check = _load("_lib_pricing_check")


def test_astra_has_the_complete_vendor_rate_card_without_fallback(capsys):
    pricing._unknown_codex_model_warnings.discard(MODEL)

    resolved, is_fallback = pricing._resolve_codex_pricing(MODEL)

    assert pricing.CODEX_MODEL_PRICING[MODEL] == ASTRA_CARD
    assert resolved is pricing.CODEX_MODEL_PRICING[MODEL]
    assert is_fallback is False
    assert pricing._is_codex_fallback(MODEL) is False
    assert pricing._codex_fast_multiplier(MODEL) == 2.0
    assert "unknown model" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("speed", "expected_cost"),
    [("standard", 1.14), ("fast", 2.28)],
)
def test_astra_prices_standard_cached_output_and_fast_tokens(
    speed, expected_cost, capsys,
):
    pricing._unknown_codex_model_warnings.discard(MODEL)

    cost = pricing._calculate_codex_entry_cost(
        MODEL,
        input_tokens=100_000,
        cached_input_tokens=40_000,
        output_tokens=10_000,
        reasoning_output_tokens=2_000,
        speed=speed,
    )

    assert cost == pytest.approx(expected_cost)
    assert "unknown model" not in capsys.readouterr().err


def test_astra_prices_the_above_272k_card():
    # The non-cached input axis crosses cctally's per-axis tier boundary.
    cost = pricing._calculate_codex_entry_cost(
        MODEL,
        input_tokens=400_000,
        cached_input_tokens=100_000,
        output_tokens=20_000,
        reasoning_output_tokens=5_000,
    )

    expected = (
        272_000 * 1e-05
        + 28_000 * 2e-05
        + 100_000 * 1e-06
        + 20_000 * 5e-05
    )
    assert cost == pytest.approx(expected)


def test_pricing_coverage_accepts_astra_as_directly_priced():
    gaps = pricing_check.classify_coverage(
        [("codex", MODEL, 3, 123_456)],
        lambda _model: None,
        pricing._is_codex_fallback,
    )

    assert gaps == []


def test_litellm_scope_keeps_astra_for_future_drift_detection():
    scoped = pricing_check.scope_litellm({
        MODEL: {
            "litellm_provider": "openai",
            "input_cost_per_token": 1e-05,
        },
        "gpt-4o": {
            "litellm_provider": "openai",
            "input_cost_per_token": 2.5e-06,
        },
    })

    assert set(scoped) == {MODEL}
