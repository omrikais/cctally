"""Durable evidence for cost facts whose model lacked a direct rate card."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math


def _pricing():
    """The pricing kernel the cost engine prices with, resolved per call.

    A module-top binding pins whichever `_lib_pricing` object existed when this
    module first loaded. A test loader that replaces that entry would then
    leave this evidence describing a different rate table from the one the
    cost it annotates was computed with.
    """
    import _lib_pricing
    return _lib_pricing


def codex_cost_with_provenance(entries, *, end, speed):
    """Price one retained Codex population and fingerprint its exact inputs."""
    _lib_pricing = _pricing()
    total = 0.0
    fallback_cost = 0.0
    model_costs = {}
    source = []
    fallback = set()
    for entry in entries:
        if entry.timestamp >= end:  # budgets use a half-open period
            continue
        item = [
            entry.timestamp.isoformat(), entry.source_path, entry.session_id,
            entry.model, entry.input_tokens, entry.cached_input_tokens,
            entry.output_tokens, entry.reasoning_output_tokens,
        ]
        source.append(item)
        used_fallback = (
            entry.input_tokens or entry.output_tokens
        ) and _lib_pricing._is_codex_fallback(entry.model)
        if used_fallback:
            fallback.add(entry.model)
        cost = _lib_pricing._calculate_codex_entry_cost(
            entry.model, entry.input_tokens, entry.cached_input_tokens,
            entry.output_tokens, entry.reasoning_output_tokens, speed=speed,
        )
        total += cost
        model_costs[entry.model] = model_costs.get(entry.model, 0.0) + cost
        if used_fallback:
            fallback_cost += cost
    encoded = json.dumps(
        sorted(source), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    evidence = {
        "version": 1,
        "pricingDate": _lib_pricing.current_pricing_snapshot().snapshot_date,
        "fallbackModels": sorted(fallback),
        "sourceHash": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "entryCount": len(source),
        "speed": speed,
        "directCostUsd": total - fallback_cost,
        "modelCostsUsd": model_costs,
    }
    return total, evidence


# ---------------------------------------------------------------------------
# Historical Claude five-hour closes (#869)
# ---------------------------------------------------------------------------

#: The ``basis`` of a close whose missing card was proven AFTER it closed, from
#: its retained entries, rather than recorded while its cost was computed. The
#: marker freezes the corrected close against every later card revision.
HISTORICAL_CLOSE_BASIS = "historical-zero-cost-inference"

#: The ``basis`` of a close corrected from the missing-card state its own cost
#: computation recorded. Without it the corrected marker is indistinguishable
#: from a close that was priced all along, and the plan could not freeze the
#: corrected close's five-hour milestones with it.
RECORDED_CLOSE_CORRECTION_BASIS = "computation-time-missing-card"

_BLOCK_TOKEN_KEYS = (
    "input_tokens", "output_tokens", "cache_create_tokens", "cache_read_tokens",
)
_CLAUDE_BASE_RATES = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_creation_input_token_cost",
    "cache_read_input_token_cost",
)
_CLAUDE_TIER_RATES = (
    "input_cost_per_token_above_200k_tokens",
    "output_cost_per_token_above_200k_tokens",
    "cache_creation_input_token_cost_above_200k_tokens",
    "cache_read_input_token_cost_above_200k_tokens",
)


def _positive_rate(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _zero_amount(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == 0
    )


def claude_card_rate_violations(snapshot) -> list:
    """Every ``(model, rate)`` a zero-cost inference would silently rely on.

    A closed amount of exactly $0 proves a missing card only while no Claude
    card prices a used token class at zero. Base rates, any present long-
    context tier, the derived 1-hour cache-write rate and any Fast multiplier
    must all be positive; an empty list is that invariant.
    """
    _lib_pricing = _pricing()
    out = []
    multiplier = snapshot.cache_write_1h_multiplier
    fast = snapshot.fast_multipliers.get("claude") or {}
    for model, card in sorted(snapshot.claude_pricing.items()):
        for field in _CLAUDE_BASE_RATES:
            if not _positive_rate(card.get(field)):
                out.append((model, field))
        for field in _CLAUDE_TIER_RATES:
            if field in card and not _positive_rate(card[field]):
                out.append((model, field))
        if not (_positive_rate(multiplier)
                and _positive_rate(card.get("input_cost_per_token"))):
            out.append((model, "cache_write_1h"))
        tier_input = card.get("input_cost_per_token_above_200k_tokens")
        if tier_input is not None and not (
            _positive_rate(multiplier) and _positive_rate(tier_input)
        ):
            out.append((model, "cache_write_1h_above_200k_tokens"))
        stripped = _lib_pricing._strip_anthropic_model_prefix(model)
        if stripped in fast and not _positive_rate(fast[stripped]):
            out.append((model, "fast_multiplier"))
    return out


def claude_entry_rates_positive(card, entry, snapshot) -> bool:
    """True iff every rate that prices one of ``entry``'s tokens is positive.

    Mirrors `_lib_pricing._calculate_entry_cost` term by term: each token class
    is priced below the long-context threshold at its base rate and above it at
    a present tier rate, and a known 1-hour cache-write split prices its share
    at the derived input-times-multiplier rate.
    """
    if card is None:
        return False
    _lib_pricing = _pricing()
    threshold = snapshot.tier_thresholds["claude"]

    def tiered(tokens, base_key, tier_key):
        if tokens <= 0:
            return True
        if not _positive_rate(card.get(base_key)):
            return False
        tier = card.get(tier_key)
        if tokens > threshold and tier is not None:
            return _positive_rate(tier)
        return True

    if not (
        tiered(int(entry.input_tokens or 0), "input_cost_per_token",
               "input_cost_per_token_above_200k_tokens")
        and tiered(int(entry.output_tokens or 0), "output_cost_per_token",
                   "output_cost_per_token_above_200k_tokens")
        and tiered(int(entry.cache_read_tokens or 0),
                   "cache_read_input_token_cost",
                   "cache_read_input_token_cost_above_200k_tokens")
    ):
        return False
    flat = int(entry.cache_creation_tokens or 0)
    if flat > 0:
        raw_1h = getattr(entry, "cache_1h_tokens", None)
        hour = 0 if raw_1h is None else max(0, min(int(raw_1h), flat))
        if hour == 0:
            if not tiered(flat, "cache_creation_input_token_cost",
                          "cache_creation_input_token_cost_above_200k_tokens"):
                return False
        else:
            multiplier = snapshot.cache_write_1h_multiplier
            base = card.get("input_cost_per_token", 0.0)
            rate_5m = card.get("cache_creation_input_token_cost", 0.0)
            below = min(flat, threshold)
            above = max(0, flat - threshold)
            terms = (
                (below and hour, _positive_rate(multiplier)
                 and _positive_rate(base)),
                (above and hour, _positive_rate(multiplier) and _positive_rate(
                    card.get("input_cost_per_token_above_200k_tokens", base))),
                (below and flat - hour, _positive_rate(rate_5m)),
                (above and flat - hour, _positive_rate(card.get(
                    "cache_creation_input_token_cost_above_200k_tokens",
                    rate_5m))),
            )
            if any(quantity and not positive for quantity, positive in terms):
                return False
    if getattr(entry, "speed", None) == "fast":
        fast = (snapshot.fast_multipliers.get("claude") or {}).get(
            _lib_pricing._strip_anthropic_model_prefix(entry.model), 1.0)
        if not _positive_rate(fast):
            return False
    return True


def historical_close_marker(close_payload, owned_entries, *, unattributed):
    """The correction marker for an unmarked zero-cost close, or None.

    The close must carry no computation-time marker, total exactly $0, and have
    every positive-token model child at exactly $0; one priced model makes the
    whole close unprovable, because its project children have no model axis.
    ``owned_entries`` are the retained entries the current ownership rule
    assigns to this close. Each one must belong to the close's account, a
    positive-token model's entries must carry no provider cost, and today's
    card for that model must price every token class the entry used at a
    positive rate. The caller still compares the replayed child populations.
    """
    if (
        close_payload.get("_pricing")
        or close_payload.get("pricing_provenance_json") is not None
        or close_payload.get("is_closed") != 1
        or not _zero_amount(close_payload.get("total_cost_usd"))
        or owned_entries is None
    ):
        return None
    inferred = set()
    for child in close_payload.get("_models") or []:
        if any(int(child.get(key) or 0) > 0 for key in _BLOCK_TOKEN_KEYS):
            model = child.get("model")
            if not isinstance(model, str) or not model:
                return None
            if not _zero_amount(child.get("cost_usd")):
                return None
            inferred.add(model)
    if not inferred or any(
        not _zero_amount(child.get("cost_usd"))
        for child in close_payload.get("_projects") or []
    ):
        return None
    account = close_payload.get("account_key") or unattributed
    _lib_pricing = _pricing()
    snapshot = _lib_pricing.current_pricing_snapshot()
    source = []
    priced_models = set()
    for entry in owned_entries:
        offset = getattr(entry, "source_line_offset", None)
        if (getattr(entry, "source_account_key", None) or unattributed) != account:
            return None
        if offset is None:
            return None
        positive = any((
            entry.input_tokens, entry.output_tokens,
            entry.cache_creation_tokens, entry.cache_read_tokens,
        ))
        if entry.model in inferred:
            if entry.cost_usd is not None:
                return None
            if positive and not claude_entry_rates_positive(
                _lib_pricing._resolve_model_pricing(entry.model, warn=False),
                entry, snapshot,
            ):
                return None
            if positive:
                priced_models.add(entry.model)
        elif positive:
            return None
        source.append([
            entry.timestamp.astimezone(dt.timezone.utc).isoformat(),
            entry.source_path, int(offset), entry.model,
            int(entry.input_tokens or 0), int(entry.output_tokens or 0),
            int(entry.cache_creation_tokens or 0),
            getattr(entry, "cache_1h_tokens", None),
            int(entry.cache_read_tokens or 0),
            getattr(entry, "speed", None),
        ])
    if priced_models != inferred:
        return None
    encoded = json.dumps(
        sorted(source, key=lambda item: json.dumps(item)),
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return {
        "version": 1,
        "basis": HISTORICAL_CLOSE_BASIS,
        "inferredModels": sorted(inferred),
        "pricingDate": snapshot.snapshot_date,
        "sourceHash": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "entryCount": len(source),
        "unpricedModels": [],
    }
