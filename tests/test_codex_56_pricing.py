"""Issue #643: the gpt-5.6 alias, the cyber card, and the duplicate rate table.

Pins the three decisions in
``docs/superpowers/specs/2026-08-25-643-gpt56-pricing-drift.md`` that no other
test covers: ``gpt-5.6`` is an alias of ``gpt-5.6-sol`` rather than a duplicated
card (D3), ``gpt-5.6-cyber`` prices from its own card including above the
272,000-token tier (D4), and the second rate card in
``bin/codex-session-metrics`` still agrees with the authoritative table (the
silent-drift seam #441 recorded). D1/D2 are pinned too, because Sol's embedded
rate is deliberately held at the pre-promotional value under dated
suppressions.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import sys
import types

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"

ALIAS = "gpt-5.6"
CANONICAL = "gpt-5.6-sol"
CYBER = "gpt-5.6-cyber"
ASTRA = "gpt-6-astra"

# Every runtime identifier OpenAI documents as pointing at Sol's card.
# `docs/pricing-gotchas.md` requires the same three properties of each of them
# — `isFallback=false`, no unknown-model warning, and the canonical model's
# fast multiplier — so the alias tests below run over both rather than over
# #643's new one alone. `gpt-daybreak-blue-latest` is the older of the two and
# is the alias this branch adds to the second rate card's map.
SOL_ALIASES = ("gpt-5.6", "gpt-daybreak-blue-latest")

# The second rate card is maintainer-local and mirror-private, so a public
# clone does not carry the script and the two assertions over it have nothing
# to check there. The existence gate is what keeps this public test module off
# the public suite's failure list (tests/test_public_test_dep_closure.py).
SESSION_METRICS = BIN / "codex-session-metrics"
requires_session_metrics = pytest.mark.skipif(
    not SESSION_METRICS.exists(),
    reason="bin/codex-session-metrics is maintainer-local and not mirrored",
)

# OpenAI publishes gpt-5.6-cyber with a 400,000-token input window, so a test
# vector above that is not a turn the vendor would accept.
CYBER_MAX_INPUT_TOKENS = 400_000

# D1: the pre-promotional card Sol is deliberately held at while OpenAI's
# promotion runs. Compared by whole-dict equality so an added or dropped field
# fails here rather than silently changing what is priced.
SOL_EMBEDDED_CARD = {
    "input_cost_per_token": 5e-06,
    "cache_read_input_token_cost": 5e-07,
    "output_cost_per_token": 3e-05,
    "input_cost_per_token_above_272k_tokens": 1e-05,
    "cache_read_input_token_cost_above_272k_tokens": 1e-06,
    "output_cost_per_token_above_272k_tokens": 4.5e-05,
}

# D2: OpenAI guarantees the promotional rate "at least through" this date.
SOL_SUPPRESSION_EXPIRY = "2026-11-21"

# `expired_allowlist_entries` compares strictly, so the suppressions stay valid
# THROUGH the guaranteed date and become actionable the following day. Derived
# rather than written out, so the two halves of the cutover assertion below
# cannot disagree if the guaranteed date is ever corrected.
SOL_SUPPRESSION_LAPSE = (
    dt.date.fromisoformat(SOL_SUPPRESSION_EXPIRY) + dt.timedelta(days=1)
).isoformat()


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


def _load_session_metrics():
    """Load ``bin/codex-session-metrics``, which has no ``.py`` suffix.

    Compiling the source directly sidesteps the suffix question, the same way
    ``tests/_script_loader.py`` loads ``bin/cctally``. The script guards its
    entry point with ``if __name__ == "__main__"``, so executing it here runs
    only the module-level definitions.
    """
    module = types.ModuleType("codex_session_metrics_under_test")
    module.__file__ = str(SESSION_METRICS)  # mirror-private-ok: metadata only
    exec(compile(SESSION_METRICS.read_text(),  # mirror-private-ok: all callers carry requires_session_metrics
                 str(SESSION_METRICS), "exec"),
         module.__dict__)
    return module


def _axis_cost(card: dict, tokens: int, base_key: str, tiered_key: str) -> float:
    """Apply the documented per-axis tier rule to an embedded card.

    Only the portion above ``CODEX_TIERED_THRESHOLD`` is charged at the tier
    rate. This derives the expectation from the card and the rule rather than
    restating a number, so the assertion still fails if the card is wrong.
    """
    threshold = pricing.CODEX_TIERED_THRESHOLD
    if tokens <= threshold:
        return tokens * card[base_key]
    return threshold * card[base_key] + (tokens - threshold) * card[tiered_key]


@pytest.mark.parametrize("alias", SOL_ALIASES)
def test_sol_alias_resolves_to_the_canonical_card(alias, capsys):
    """D3 — an alias resolves to one canonical card, not a duplicate."""
    pricing._unknown_codex_model_warnings.discard(alias)

    resolved, is_fallback = pricing._resolve_codex_pricing(alias)

    assert alias not in pricing.CODEX_MODEL_PRICING
    assert pricing.CODEX_MODEL_ALIASES[alias] == CANONICAL
    assert resolved is pricing.CODEX_MODEL_PRICING[CANONICAL]
    assert is_fallback is False
    assert pricing._is_codex_fallback(alias) is False

    vector = (120_000, 40_000, 10_000, 2_000)
    assert (pricing._calculate_codex_entry_cost(alias, *vector)
            == pricing._calculate_codex_entry_cost(CANONICAL, *vector))
    assert "unknown model" not in capsys.readouterr().err


@pytest.mark.parametrize("alias", SOL_ALIASES)
def test_sol_alias_bills_the_fast_tier_at_the_canonical_multiplier(
    alias, monkeypatch,
):
    """D3 — the alias's third property: it uses the canonical fast multiplier.

    ``docs/pricing-gotchas.md`` requires an alias to resolve with
    ``isFallback=false``, emit no unknown-model warning, and use the canonical
    model's fast multiplier. The first two are pinned above; this pins the
    third, and it prices at ``speed="fast"`` so the multiplier is engaged
    rather than merely read back from the map.
    """
    vector = (120_000, 40_000, 10_000, 2_000)
    multiplier = pricing._codex_fast_multiplier(CANONICAL)

    assert pricing._codex_fast_multiplier(alias) == multiplier
    standard = pricing._calculate_codex_entry_cost(alias, *vector)
    fast = pricing._calculate_codex_entry_cost(alias, *vector, speed="fast")
    # A 1.0 multiplier would make the equality below hold without the fast
    # branch ever changing the figure.
    assert multiplier != 1.0
    assert fast == pytest.approx(standard * multiplier)
    assert fast == pricing._calculate_codex_entry_cost(
        CANONICAL, *vector, speed="fast")

    # Sol takes CODEX_FAST_MULTIPLIER_FALLBACK today, which every unlisted
    # model takes, so the equalities above cannot yet tell a resolved alias
    # from an unresolved one. Give the canonical card a distinct override and
    # the alias must follow it there too.
    monkeypatch.setitem(
        pricing.CODEX_FAST_MULTIPLIER_OVERRIDES, CANONICAL, 3.0)
    assert pricing._codex_fast_multiplier(alias) == 3.0
    assert pricing._calculate_codex_entry_cost(
        alias, *vector, speed="fast") == pytest.approx(standard * 3.0)


@pytest.mark.parametrize(
    ("input_tokens", "cached_input_tokens", "output_tokens"),
    [
        (380_000, 100_000, 20_000),   # non-cached input crosses the threshold
        (390_000, 300_000, 10_000),   # cached input crosses it instead
    ],
)
def test_cyber_prices_from_its_own_card_across_the_272k_tier(
    input_tokens, cached_input_tokens, output_tokens, capsys,
):
    """D4 — cyber has its own card and its above-272k tier is applied per axis."""
    pricing._unknown_codex_model_warnings.discard(CYBER)
    card = pricing.CODEX_MODEL_PRICING[CYBER]

    # input_tokens INCLUDES cached input, so this is the whole prompt.
    assert input_tokens <= CYBER_MAX_INPUT_TOKENS
    non_cached_input = input_tokens - cached_input_tokens

    expected = (
        _axis_cost(card, non_cached_input,
                   "input_cost_per_token",
                   "input_cost_per_token_above_272k_tokens")
        + _axis_cost(card, cached_input_tokens,
                     "cache_read_input_token_cost",
                     "cache_read_input_token_cost_above_272k_tokens")
        + _axis_cost(card, output_tokens,
                     "output_cost_per_token",
                     "output_cost_per_token_above_272k_tokens")
    )
    untiered = (non_cached_input * card["input_cost_per_token"]
                + cached_input_tokens * card["cache_read_input_token_cost"]
                + output_tokens * card["output_cost_per_token"])
    # Without this the vector could sit entirely below the threshold and the
    # assertion below would pass without ever exercising the tier.
    assert expected > untiered

    cost = pricing._calculate_codex_entry_cost(
        CYBER, input_tokens, cached_input_tokens, output_tokens, 0,
    )

    assert cost == pytest.approx(expected)
    assert pricing._is_codex_fallback(CYBER) is False
    assert "unknown model" not in capsys.readouterr().err


@requires_session_metrics
def test_session_metrics_rate_card_matches_the_authoritative_table():
    """Every model the second estimator prices agrees with the real table."""
    metrics = _load_session_metrics()

    assert metrics.PRICING, "the estimator prices no model at all"
    assert CYBER in metrics.PRICING
    assert metrics.PRICING[ASTRA] == (10.0, 50.0)
    for model, (input_per_mtok, output_per_mtok) in metrics.PRICING.items():
        card = pricing.CODEX_MODEL_PRICING[pricing._canonical_codex_model(model)]
        assert input_per_mtok == pytest.approx(
            card["input_cost_per_token"] * 1e6), model
        assert output_per_mtok == pytest.approx(
            card["output_cost_per_token"] * 1e6), model
        # The estimator DERIVES its cached-input rate as the input rate times
        # CACHED_INPUT_FACTOR instead of storing one, so the ratio is itself a
        # rate that can drift from the card. It holds for every model today; a
        # model whose cached input is not a tenth of its input would silently
        # estimate wrong without this. approx is required on both sides of the
        # scaling: gpt-5.6-terra derives 0.2 against a card that scales to
        # 0.19999999999999998, and gpt-5.6-luna derives 0.020000000000000004
        # against a card that scales to 0.02.
        assert input_per_mtok * metrics.CACHED_INPUT_FACTOR == pytest.approx(
            card["cache_read_input_token_cost"] * 1e6), model


@requires_session_metrics
def test_session_metrics_alias_map_mirrors_every_alias_it_can_price():
    """Iterating PRICING cannot detect a MISSING alias, so iterate the source.

    ``MODEL_ALIASES`` documents itself as mirroring ``CODEX_MODEL_ALIASES`` for
    the models this estimator prices, and a mirror is only checkable from the
    authoritative side. Both directions are asserted so the check stays honest:
    an alias whose canonical card the estimator prices MUST be present, and one
    whose card it does not price MUST be absent, because normalizing that name
    would still resolve to no rate.
    """
    metrics = _load_session_metrics()
    priced = set(metrics.PRICING)

    for alias, canonical in pricing.CODEX_MODEL_ALIASES.items():
        if canonical in priced:
            assert metrics.MODEL_ALIASES.get(alias) == canonical, alias
        else:
            assert alias not in metrics.MODEL_ALIASES, alias

    for alias, canonical in metrics.MODEL_ALIASES.items():
        assert pricing.CODEX_MODEL_ALIASES.get(alias) == canonical, alias
        assert canonical in priced, alias

    # Each branch above passes over an empty set without a live example, so
    # both are guarded. The absent-if-not-priceable branch has exactly one
    # instance today (`codex-auto-review` -> `gpt-5.5`, a model this estimator
    # carries no card for) and would go silently vacuous if that alias were
    # dropped from CODEX_MODEL_ALIASES.
    assert [a for a, c in pricing.CODEX_MODEL_ALIASES.items() if c in priced], \
        "no alias resolves to a model this estimator prices"
    assert [a for a, c in pricing.CODEX_MODEL_ALIASES.items()
            if c not in priced], \
        "no alias resolves to a model this estimator does NOT price"


@requires_session_metrics
def test_session_metrics_normalizes_the_gpt_56_alias():
    """The estimator must not silently count an aliased turn as $0."""
    metrics = _load_session_metrics()
    totals = {
        "input_tokens": 100_000,
        "cached_input_tokens": 40_000,
        "output_tokens": 10_000,
    }

    aliased = metrics._est_cost({ALIAS: 10}, totals)

    assert aliased > 0
    assert aliased == metrics._est_cost({CANONICAL: 10}, totals)


def test_sol_holds_the_prepromotional_card_under_dated_suppressions():
    """D1/D2 — Sol's values are unchanged and every suppression is dated."""
    assert pricing.CODEX_MODEL_PRICING[CANONICAL] == SOL_EMBEDDED_CARD

    sol_entries = [e for e in pricing.PRICING_DRIFT_ALLOWLIST
                   if e["model"] == CANONICAL]
    assert {e["field"] for e in sol_entries} == set(SOL_EMBEDDED_CARD)
    assert all(e["expires"] == SOL_SUPPRESSION_EXPIRY for e in sol_entries)

    # The alias omission is durable and unrelated to the promotion, so its
    # model-only entry carries no expiry.
    alias_entries = [e for e in pricing.PRICING_DRIFT_ALLOWLIST
                     if e["model"] == ALIAS]
    assert len(alias_entries) == 1
    assert "field" not in alias_entries[0]
    assert "expires" not in alias_entries[0]


def test_sol_suppressions_expire_the_day_after_the_guaranteed_date():
    """D2 — the expiry cutover, asserted at a pinned clock over the REAL list.

    Other tests run the shipped ``PRICING_DRIFT_ALLOWLIST`` through the date
    leg — ``test_pricing_check.py`` runs the CLI over it at pinned clocks, and
    ``test_no_suppression_expires_before_the_pricing_snapshot_date`` holds the
    floor those pins stand on. This is the one place the CUTOVER itself is
    asserted: valid through the last day OpenAI guarantees the promotional
    rate, actionable the next.
    """
    guaranteed_day = pricing_check.expired_allowlist_entries(
        pricing.PRICING_DRIFT_ALLOWLIST, SOL_SUPPRESSION_EXPIRY)
    assert guaranteed_day == [], (
        "the comparison is strict, so an entry is valid THROUGH its `expires` "
        "and nothing may be expired on Sol's guaranteed date"
    )

    day_after = pricing_check.expired_allowlist_entries(
        pricing.PRICING_DRIFT_ALLOWLIST, SOL_SUPPRESSION_LAPSE)
    assert {e["model"] for e in day_after} == {CANONICAL}
    assert len(day_after) == len(SOL_EMBEDDED_CARD)
