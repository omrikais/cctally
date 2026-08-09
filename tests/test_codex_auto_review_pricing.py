"""Issue #535: canonical pricing for the hidden Codex Guardian model."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest


BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"


def _load(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, BIN / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


pricing = _load("_lib_pricing")
pricing_check = _load("_lib_pricing_check")


def test_auto_review_resolves_to_gpt_5_5_without_legacy_fallback():
    """Deleting the known-alias branch must make this hit the gpt-5 fallback."""
    resolved, is_fallback = pricing._resolve_codex_pricing("codex-auto-review")

    assert resolved is pricing.CODEX_MODEL_PRICING["gpt-5.5"]
    assert is_fallback is False
    assert pricing._is_codex_fallback("codex-auto-review") is False
    assert pricing._codex_fast_multiplier("codex-auto-review") == 2.5


@pytest.mark.parametrize(
    ("speed", "expected_cost"),
    [
        ("standard", 3.5),
        ("fast", 8.75),
    ],
)
def test_auto_review_cost_uses_canonical_standard_and_fast_rates(speed, expected_cost):
    """Using the alias for multiplier lookup would charge 2x instead of 2.5x."""
    cost = pricing._calculate_codex_entry_cost(
        "codex-auto-review",
        100_000,
        0,
        100_000,
        0,
        speed=speed,
    )

    assert cost == pytest.approx(expected_cost)


def test_pricing_coverage_accepts_auto_review_but_keeps_unknown_actionable():
    """Broadening alias recognition must not hide a genuinely unknown model."""
    observed = [
        ("codex", "codex-auto-review", 9, 308_964),
        ("codex", "gpt-hypothetical-99", 2, 9_000),
    ]

    gaps = pricing_check.classify_coverage(
        observed,
        lambda _model: None,
        pricing._is_codex_fallback,
    )

    assert [(gap.model, gap.kind) for gap in gaps] == [
        ("gpt-hypothetical-99", "fallback"),
    ]
