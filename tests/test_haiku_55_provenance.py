"""#929 S1 A8: cost provenance covers the whole-request (100K) pricing tier.

A closed amount of exactly $0 proves a missing card only while no card prices
a used token class at zero. For Claude Haiku 5.5 that rule must cover the four
``*_above_100k_tokens`` rates, the threshold marker and the derived higher
1-hour write rate, and a malformed or mixed declaration must count as a
violation. The per-entry predicate must judge the card the entry was actually
priced with (the selected one), and refuse a malformed declaration.
"""
from __future__ import annotations

import copy
import math
import pathlib
import sys
from types import SimpleNamespace

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import _lib_cost_provenance as prov  # noqa: E402
import _lib_pricing as pricing  # noqa: E402

HAIKU = "claude-haiku-5-5"
MARKER = "whole_request_prompt_threshold_tokens"
READ_TIER = "cache_read_input_token_cost_above_100k_tokens"
INPUT_TIER = "input_cost_per_token_above_100k_tokens"
# LiteLLM carries this field; cctally derives the 1h write, so it is not one of
# the four recognised whole-request fields (#929 S1 review 929-CD-001).
STRAY = "cache_creation_input_token_cost_above_1hr_above_100k_tokens"
TIER_KEYS = (
    "input_cost_per_token_above_100k_tokens",
    "output_cost_per_token_above_100k_tokens",
    "cache_creation_input_token_cost_above_100k_tokens",
    "cache_read_input_token_cost_above_100k_tokens",
)


def _card(**changes):
    card = copy.deepcopy(pricing.CLAUDE_MODEL_PRICING[HAIKU])
    for key, value in changes.items():
        if value is _DROP:
            del card[key]
        else:
            card[key] = value
    return card


_DROP = object()


def _snapshot(card):
    present = pricing.current_pricing_snapshot()
    cards = dict(present.claude_pricing)
    cards[HAIKU] = card
    return pricing.PricingSnapshot(
        snapshot_date=present.snapshot_date,
        claude_pricing=cards,
        codex_pricing=present.codex_pricing,
        aliases=present.aliases,
        tier_thresholds=present.tier_thresholds,
        fallback_model=present.fallback_model,
        cache_write_1h_multiplier=present.cache_write_1h_multiplier,
        fast_multipliers=present.fast_multipliers,
    )


def _haiku_violations(card):
    return [v for v in prov.claude_card_rate_violations(_snapshot(card))
            if v[0] == HAIKU]


def _entry(**kw):
    base = dict(model=HAIKU, input_tokens=40_001, output_tokens=1_000,
                cache_creation_tokens=40_000, cache_read_tokens=20_000,
                cache_1h_tokens=20_000, speed=None)
    base.update(kw)
    return SimpleNamespace(**base)


LONG = _entry()                                     # prompt 100,001
SHORT = _entry(input_tokens=40_000)                 # prompt 100,000

MALFORMED = [
    _card(**{READ_TIER: _DROP}),
    _card(**{MARKER: _DROP}),
    _card(**{k: _DROP for k in TIER_KEYS}),
    _card(**{MARKER: 200_000}),
    _card(**{MARKER: True}),
    _card(**{MARKER: 100000.0}),
    _card(**{READ_TIER: 0.0}),
    _card(**{READ_TIER: -1e-07}),
    _card(**{READ_TIER: math.nan}),
    _card(**{READ_TIER: math.inf}),
    _card(**{READ_TIER: True}),
    _card(input_cost_per_token_above_200k_tokens=6e-07),
    _card(**{STRAY: 1e-06}),
]


def test_live_snapshot_has_no_haiku_violation():
    live = prov.claude_card_rate_violations(pricing.current_pricing_snapshot())
    assert HAIKU in pricing.current_pricing_snapshot().claude_pricing
    assert [v for v in live if v[0] == HAIKU] == []


DERIVED_1H = "cache_write_1h_above_100k_tokens"


def test_each_nonpositive_higher_rate_is_reported():
    for bad in (0.0, -1e-07, math.nan, math.inf, True):
        assert _haiku_violations(_card(**{READ_TIER: bad})) == [
            (HAIKU, READ_TIER)], bad


def test_missing_tier_field_is_reported():
    for key in TIER_KEYS:
        expected = [(HAIKU, key)]
        if key == INPUT_TIER:
            # The derived higher 1h write is the higher input rate x 2.
            expected.append((HAIKU, DERIVED_1H))
        assert _haiku_violations(_card(**{key: _DROP})) == expected, key


def test_marker_problems_are_reported():
    for value in (_DROP, 200_000, True, 100000.0):
        assert _haiku_violations(_card(**{MARKER: value})) == [
            (HAIKU, MARKER)], value
    # Marker without any higher rate: every missing rate is a violation.
    assert _haiku_violations(_card(**{k: _DROP for k in TIER_KEYS})) == (
        [(HAIKU, key) for key in TIER_KEYS] + [(HAIKU, DERIVED_1H)])


def test_mixed_tiers_are_reported():
    got = _haiku_violations(_card(input_cost_per_token_above_200k_tokens=6e-07))
    assert got == [(HAIKU, "whole_request_mixed_tiers")]


def test_unrecognised_tier_field_on_a_whole_request_card_is_reported():
    # The selector and check_table_shapes refuse this card; the card-wide
    # provenance invariant must agree with them (929-CD-001).
    assert _haiku_violations(_card(**{STRAY: 1e-06})) == [(HAIKU, STRAY)]


def test_unrecognised_tier_field_on_a_legacy_card_is_reported():
    # No marker and none of the four recognised fields: the card still
    # half-declares a whole-request tier, so it is malformed, not legacy.
    legacy = "claude-haiku-4-5"
    present = pricing.current_pricing_snapshot()
    cards = dict(present.claude_pricing)
    card = dict(cards[legacy])
    card[STRAY] = 1e-06
    cards[legacy] = card
    snapshot = pricing.PricingSnapshot(
        snapshot_date=present.snapshot_date,
        claude_pricing=cards,
        codex_pricing=present.codex_pricing,
        aliases=present.aliases,
        tier_thresholds=present.tier_thresholds,
        fallback_model=present.fallback_model,
        cache_write_1h_multiplier=present.cache_write_1h_multiplier,
        fast_multipliers=present.fast_multipliers,
    )
    got = [v for v in prov.claude_card_rate_violations(snapshot)
           if v[0] == legacy]
    assert got == ([(legacy, key) for key in TIER_KEYS]
                   + [(legacy, MARKER), (legacy, DERIVED_1H), (legacy, STRAY)])


def test_zero_higher_input_also_breaks_the_derived_higher_1h_rate():
    got = _haiku_violations(_card(**{INPUT_TIER: 0.0}))
    assert got == [(HAIKU, INPUT_TIER), (HAIKU, DERIVED_1H)]


def test_entry_predicate_judges_the_selected_card():
    live = pricing.current_pricing_snapshot()
    card = live.claude_pricing[HAIKU]
    assert prov.claude_entry_rates_positive(card, LONG, live)
    assert prov.claude_entry_rates_positive(card, SHORT, live)

    # Only the higher input rate is zero. That declaration is malformed (every
    # whole-request rate must be positive), so the selector raises and the
    # predicate refuses the card for EVERY entry, the short one included —
    # not just the long entry that would select the zero rate.
    zero_high_input = _card(**{INPUT_TIER: 0.0})
    assert not prov.claude_entry_rates_positive(zero_high_input, LONG, live)
    assert not prov.claude_entry_rates_positive(zero_high_input, SHORT, live)

    for bad in MALFORMED:
        assert not prov.claude_entry_rates_positive(bad, LONG, live), bad
        assert not prov.claude_entry_rates_positive(bad, SHORT, live), bad
