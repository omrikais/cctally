"""Unit tests for bin/_cctally_cache_report kernel.

Loads the kernel as a sibling module (matches the project pattern used
by other tests targeting bin/_cctally_*.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow ``import _cctally_cache_report`` (the bin/ siblings convention).
_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import _cctally_cache_report as crk  # noqa: E402


# ---------------------------------------------------------------------------
# Task A2 — leaf helpers: _compute_cache_hit_percent + _compute_entry_cache_dollars
# ---------------------------------------------------------------------------

_PRICING_SONNET = {
    "claude-sonnet-4-6": {
        "input_cost_per_token": 3e-6,
        "output_cost_per_token": 15e-6,
        "cache_creation_input_token_cost": 3.75e-6,
        "cache_read_input_token_cost": 0.3e-6,
    },
}


def test_cache_hit_percent_zero_when_no_tokens():
    assert crk._compute_cache_hit_percent(0, 0, 0) == 0.0


def test_cache_hit_percent_pure_read():
    # 100 cache_read out of 200 total inputs (100 input + 0 create + 100 read) → 50%
    assert crk._compute_cache_hit_percent(100, 0, 100) == 50.0


def test_cache_dollars_zero_when_no_tokens():
    saved, wasted, net = crk._compute_entry_cache_dollars(
        "claude-sonnet-4-6", 0, 0, pricing=_PRICING_SONNET,
    )
    assert (saved, wasted, net) == (0.0, 0.0, 0.0)


def test_cache_dollars_unknown_model_returns_zeros():
    saved, wasted, net = crk._compute_entry_cache_dollars(
        "unknown-model-x", 1000, 1000, pricing=_PRICING_SONNET,
    )
    assert (saved, wasted, net) == (0.0, 0.0, 0.0)


def test_cache_dollars_saved_when_cache_read():
    # Pure cache_read: saved = read * (base - read_rate), wasted = 0.
    saved, wasted, net = crk._compute_entry_cache_dollars(
        "claude-sonnet-4-6", 0, 1000, pricing=_PRICING_SONNET,
    )
    # base=3e-6, read_rate=0.3e-6 → saved = 1000 * 2.7e-6 = 0.0027
    assert wasted == 0.0
    assert abs(saved - 0.0027) < 1e-9
    assert abs(net - 0.0027) < 1e-9


def test_cache_dollars_wasted_when_cache_creation():
    # Pure cache_creation: wasted = creation * (create_rate - base).
    saved, wasted, net = crk._compute_entry_cache_dollars(
        "claude-sonnet-4-6", 1000, 0, pricing=_PRICING_SONNET,
    )
    # base=3e-6, create=3.75e-6 → wasted = 1000 * 0.75e-6 = 0.00075
    assert saved == 0.0
    assert abs(wasted - 0.00075) < 1e-9
    assert abs(net - (-0.00075)) < 1e-9
