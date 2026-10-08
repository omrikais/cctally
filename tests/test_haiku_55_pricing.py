"""#929 S1: Claude Haiku 5.5 is priced by prompt length (whole-request tier).

A request whose prompt (input + FLAT cache creation + cache read) exceeds
100,000 tokens bills EVERY class of that request — output included — at the
``*_above_100k_tokens`` card; at or under 100,000 it bills the base card. The
1-hour cache write is always the SELECTED input rate x 2.0. Every expected
value here is independently written vendor arithmetic; none is computed by
calling the kernel.

Acceptance rows: A1, A2, A3, A5, A10, A11, A13, A14 of
docs/superpowers/specs/2026-10-07-929-s1-haiku-55-pricing.md.
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_BIN = _ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import _lib_pricing as pricing  # noqa: E402
import _lib_pricing_check as pc  # noqa: E402

HAIKU = "claude-haiku-5-5"
MARKER = "whole_request_prompt_threshold_tokens"
BASE = dict(i=1e-07, o=5e-07, w5=1.25e-07, r=1e-08)
HIGH = dict(i=5e-07, o=2.5e-06, w5=6.25e-07, r=5e-08)
TIER_KEYS = (
    "input_cost_per_token_above_100k_tokens",
    "output_cost_per_token_above_100k_tokens",
    "cache_creation_input_token_cost_above_100k_tokens",
    "cache_read_input_token_cost_above_100k_tokens",
)


def vendor(card, *, inp=0, out=0, cc=0, h=0, cr=0):
    return (inp * card["i"] + out * card["o"] + (cc - h) * card["w5"]
            + h * (card["i"] * 2.0) + cr * card["r"])


def cost(**kw):
    usage = pricing.claude_usage_dict(
        cache_1h_tokens=kw.pop("h", None), speed=kw.pop("speed", None), **kw)
    return pricing._calculate_entry_cost(HAIKU, usage, mode="calculate")


def _stored_card():
    return pricing.CLAUDE_MODEL_PRICING[HAIKU]


def _drop(key):
    def mutate(card):
        del card[key]
    return mutate


def _set(key, value):
    def mutate(card):
        card[key] = value
    return mutate


def _drop_all_tiers(card):
    for key in TIER_KEYS:
        del card[key]


READ_TIER = "cache_read_input_token_cost_above_100k_tokens"
STRAY = "cache_creation_input_token_cost_above_1hr_above_100k_tokens"
_THRESHOLD_PROBLEM = (f"{MARKER} must be the integer 100000 the "
                      "_above_100k_tokens fields name (got {!r})")

# (label, mutation of a deep copy of the stored card, the EXACT list
# `claude_whole_request_declaration_problems` returns for it, in order)
MALFORMED = [
    ("three of four tier rates", _drop(READ_TIER), [f"missing {READ_TIER}"]),
    ("orphan tier rates without the marker", _drop(MARKER),
     [f"missing {MARKER}"]),
    ("marker without tier rates", _drop_all_tiers,
     [f"missing {k}" for k in TIER_KEYS]),
    ("threshold 200_000", _set(MARKER, 200_000),
     [_THRESHOLD_PROBLEM.format(200_000)]),
    ("threshold True", _set(MARKER, True), [_THRESHOLD_PROBLEM.format(True)]),
    ("threshold 100000.0", _set(MARKER, 100000.0),
     [_THRESHOLD_PROBLEM.format(100000.0)]),
    ("tier rate 0.0", _set(READ_TIER, 0.0),
     [f"{READ_TIER} not a finite positive number (0.0)"]),
    ("tier rate negative", _set(READ_TIER, -1e-07),
     [f"{READ_TIER} not a finite positive number (-1e-07)"]),
    ("tier rate nan", _set(READ_TIER, float("nan")),
     [f"{READ_TIER} not a finite positive number (nan)"]),
    ("tier rate inf", _set(READ_TIER, float("inf")),
     [f"{READ_TIER} not a finite positive number (inf)"]),
    ("tier rate True", _set(READ_TIER, True),
     [f"{READ_TIER} not a finite positive number (True)"]),
    ("mixed with a 200K marginal tier",
     _set("input_cost_per_token_above_200k_tokens", 6e-07),
     ["mixes whole-request and marginal tiers: "
      "input_cost_per_token_above_200k_tokens"]),
    ("unrecognised 100K field", _set(STRAY, 1e-06),
     [f"unrecognised _above_100k_tokens fields: {STRAY}"]),
]

# Problems `check_table_shapes`' generic field check adds BEFORE the
# declaration problems for the same card.
SHAPE_EXTRA = {
    "tier rate negative":
        [f"field {READ_TIER} not a non-negative number (-1e-07)"],
}


def _malformed(mutate):
    card = copy.deepcopy(_stored_card())
    mutate(card)
    return card


# --- A1 ---------------------------------------------------------------------

def test_card_pins_vendor_rates_and_marker(capsys, monkeypatch):
    card = _stored_card()
    assert (card["input_cost_per_token"], card["output_cost_per_token"],
            card["cache_creation_input_token_cost"],
            card["cache_read_input_token_cost"]) == (1e-07, 5e-07, 1.25e-07, 1e-08)
    assert tuple(card[k] for k in TIER_KEYS) == (5e-07, 2.5e-06, 6.25e-07, 5e-08)
    assert card[MARKER] == 100_000
    assert type(card[MARKER]) is int
    # No stored 1h, batch or fast field: the 1h write is derived.
    assert not [k for k in card if "1h" in k or "1hr" in k or "batch" in k]
    assert HAIKU not in pricing.CLAUDE_FAST_MULTIPLIER_OVERRIDES

    # A fresh, test-local warning set: never clear the process-wide one.
    monkeypatch.setattr(pricing, "_unknown_model_warnings", set())
    capsys.readouterr()
    for name in (HAIKU, "anthropic/" + HAIKU, "anthropic." + HAIKU):
        assert pricing._resolve_model_pricing(name) is card, name
    assert "unknown model" not in capsys.readouterr().err

    short = cost(input_tokens=50_000, output_tokens=1_000)
    assert abs(short - vendor(BASE, inp=50_000, out=1_000)) < 1e-12
    assert abs(short - 0.0055) < 1e-12
    assert "unknown model" not in capsys.readouterr().err


# --- A2 ---------------------------------------------------------------------

def test_boundary_whole_request():
    at = cost(input_tokens=40_000, cache_creation_tokens=40_000, h=20_000,
              cache_read_tokens=20_000, output_tokens=1_000)
    assert abs(at - 0.0112) < 1e-12
    assert abs(at - vendor(BASE, inp=40_000, out=1_000, cc=40_000, h=20_000,
                           cr=20_000)) < 1e-12
    over = cost(input_tokens=40_001, cache_creation_tokens=40_000, h=20_000,
                cache_read_tokens=20_000, output_tokens=1_000)
    assert abs(over - 0.0560005) < 1e-12
    assert abs(over - vendor(HIGH, inp=40_001, out=1_000, cc=40_000, h=20_000,
                             cr=20_000)) < 1e-12

    # Output never counts toward the prompt.
    assert abs(cost(input_tokens=99_000, output_tokens=5_000)
               - vendor(BASE, inp=99_000, out=5_000)) < 1e-12
    # The 1h sub-split is part of the flat creation total; never counted twice.
    assert abs(cost(input_tokens=60_000, cache_creation_tokens=30_000,
                    h=30_000, cache_read_tokens=10_000)
               - vendor(BASE, inp=60_000, cc=30_000, h=30_000,
                        cr=10_000)) < 1e-12


# --- A3 ---------------------------------------------------------------------

def test_each_class_and_output_only():
    assert abs(cost(input_tokens=100_001)
               - vendor(HIGH, inp=100_001)) < 1e-12
    assert abs(cost(cache_read_tokens=100_001)
               - vendor(HIGH, cr=100_001)) < 1e-12
    assert abs(cost(cache_creation_tokens=100_001, h=0)
               - vendor(HIGH, cc=100_001)) < 1e-12
    assert abs(cost(output_tokens=200_000)
               - vendor(BASE, out=200_000)) < 1e-12
    assert abs(cost(input_tokens=50_000, cache_creation_tokens=60_000,
                    h=10_000, cache_read_tokens=40_000, output_tokens=3_000)
               - vendor(HIGH, inp=50_000, cc=60_000, h=10_000, cr=40_000,
                        out=3_000)) < 1e-12
    # Exactly at the threshold stays on the base card.
    assert abs(cost(input_tokens=100_000)
               - vendor(BASE, inp=100_000)) < 1e-12


# --- A5 ---------------------------------------------------------------------

def test_recorded_cost_bypasses_selection(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("selection must not run for a recorded cost")

    # raising=False: on the pre-#929 tree the selector does not exist yet, and
    # this row is a preservation guard that must pass there too (spec §7).
    monkeypatch.setattr(pricing, "_select_claude_request_card", boom,
                        raising=False)
    monkeypatch.setattr(pricing, "_resolve_model_pricing", boom)
    long_usage = pricing.claude_usage_dict(
        cache_1h_tokens=20_000, speed=None, input_tokens=140_001,
        cache_creation_tokens=40_000, cache_read_tokens=20_000,
        output_tokens=1_000)
    calc = pricing._calculate_entry_cost
    assert calc(HAIKU, long_usage, mode="auto", cost_usd=1.25) == 1.25
    assert calc(HAIKU, long_usage, mode="auto", cost_usd=0.0) == 0.0
    assert calc(HAIKU, long_usage, mode="display", cost_usd=0.75) == 0.75
    assert calc(HAIKU, long_usage, mode="display") == 0.0


# --- A13 --------------------------------------------------------------------

def test_selector_purity_and_malformed_matrix():
    stored = _stored_card()
    stored_before = copy.deepcopy(stored)
    snap_card = pricing.current_pricing_snapshot().claude_pricing[HAIKU]
    snap_before = copy.deepcopy(snap_card)

    long_kw = dict(input_tokens=40_001, cache_creation_tokens=40_000,
                   h=20_000, cache_read_tokens=20_000, output_tokens=1_000)
    first = cost(**dict(long_kw))
    short = cost(input_tokens=50_000, output_tokens=1_000)
    second = cost(**dict(long_kw))
    assert abs(first - 0.0560005) < 1e-12
    assert abs(short - 0.0055) < 1e-12
    assert abs(second - 0.0560005) < 1e-12
    assert stored == stored_before
    assert snap_card == snap_before

    sel = pricing._select_claude_request_card
    high = sel(stored, input_tokens=100_001, cache_creation_tokens=0,
               cache_read_tokens=0)
    assert high is not stored
    assert MARKER not in high
    assert not [k for k in high if "_above_" in k]
    assert (high["input_cost_per_token"], high["output_cost_per_token"],
            high["cache_creation_input_token_cost"],
            high["cache_read_input_token_cost"]) == (5e-07, 2.5e-06, 6.25e-07, 5e-08)
    assert stored == stored_before
    assert sel(stored, input_tokens=100_000, cache_creation_tokens=0,
               cache_read_tokens=0) is stored
    # The override replaces the computed prompt in both directions.
    assert sel(stored, input_tokens=10, cache_creation_tokens=0,
               cache_read_tokens=0, prompt_tokens_for_tier=100_001) is not stored
    assert sel(stored, input_tokens=200_000, cache_creation_tokens=0,
               cache_read_tokens=0, prompt_tokens_for_tier=100_000) is stored

    legacy = pricing.CLAUDE_MODEL_PRICING["claude-sonnet-5-5"]
    assert sel(legacy, input_tokens=900_000, cache_creation_tokens=0,
               cache_read_tokens=0) is legacy

    for label, mutate, expected in MALFORMED:
        bad = _malformed(mutate)
        with pytest.raises(ValueError) as raised:
            sel(bad, input_tokens=1, cache_creation_tokens=0,
                cache_read_tokens=0)
        assert str(raised.value) == (
            "malformed whole-request pricing declaration: "
            + "; ".join(expected)), label


def test_selected_card_carries_no_tier_field():
    sel = pricing._select_claude_request_card
    high = sel(_stored_card(), input_tokens=100_001, cache_creation_tokens=0,
               cache_read_tokens=0)
    assert [k for k in high
            if k == MARKER or k.endswith("_above_100k_tokens")] == []


# --- A14 --------------------------------------------------------------------

def test_mixed_ttl_direct_expression_witness():
    usage = pricing.claude_usage_dict(
        cache_1h_tokens=123_456, speed=None, cache_creation_tokens=555_205)
    got = pricing._calculate_entry_cost(HAIKU, usage, mode="calculate")
    assert got.hex() == "0x1.92bd017dae819p-2"


# --- A10 --------------------------------------------------------------------

def _scoped_fixture():
    raw = json.loads((_ROOT / "tests" / "fixtures" / "pricing"
                      / "litellm_scoped.json").read_text())
    return pc.scope_litellm(raw)


def test_drift_agrees_and_detects_mutation():
    scoped = _scoped_fixture()
    assert HAIKU in scoped
    res = pc.diff_pricing(pricing.CLAUDE_MODEL_PRICING,
                          pricing.CODEX_MODEL_PRICING, scoped,
                          pricing.PRICING_DRIFT_ALLOWLIST)
    assert not [r for r in res.value_drift if r.model == HAIKU]
    assert HAIKU not in res.missing_from_us

    ours = copy.deepcopy(pricing.CLAUDE_MODEL_PRICING)
    ours[HAIKU]["cache_read_input_token_cost_above_100k_tokens"] = 1e-07
    res = pc.diff_pricing(ours, pricing.CODEX_MODEL_PRICING, scoped,
                          pricing.PRICING_DRIFT_ALLOWLIST)
    rows = [(r.model, r.field) for r in res.value_drift if r.model == HAIKU]
    assert rows == [(HAIKU, "cache_read_input_token_cost_above_100k_tokens")]


# --- A11 --------------------------------------------------------------------

def test_shape_check_whole_request_completeness():
    assert pc.check_table_shapes(
        pricing.CLAUDE_MODEL_PRICING, pricing.CODEX_MODEL_PRICING,
        zero_sentinels={"gpt-5.3-codex-spark"}) == []
    for label, mutate, expected in MALFORMED:
        problems = pc.check_table_shapes({HAIKU: _malformed(mutate)}, {}, set())
        assert problems == [f"{HAIKU}: {p}"
                            for p in SHAPE_EXTRA.get(label, []) + expected], label


def test_shape_check_refuses_a_100k_field_on_a_card_without_the_marker():
    # A card carrying only an unrecognised `_above_100k_tokens` field (here
    # LiteLLM's 1h-write spelling) declares a whole-request tier the cost
    # engine does not recognise; it must not pass as a legacy card.
    legacy = "claude-haiku-4-5"
    card = copy.deepcopy(pricing.CLAUDE_MODEL_PRICING[legacy])
    card[STRAY] = 1e-06
    problems = pc.check_table_shapes({legacy: card}, {}, set())
    assert problems == (
        [f"{legacy}: missing {MARKER}"]
        + [f"{legacy}: missing {k}" for k in TIER_KEYS]
        + [f"{legacy}: unrecognised _above_100k_tokens fields: {STRAY}"])
