"""The one builder for every cache-report envelope block.

Before #443 S2 this dict was hand-built at three sites — the Claude
serializer and both returns of the Codex wire — with nothing enforcing a
shared key set, which is how the Codex empty return and its populated
return drifted apart (#443 F18).

Provider parameterization resolves exactly four things and nothing else:
the percent key, whether the not-applicable metadata is emitted, the
applicable predicate set, and the reason text. Everything else is
provider-independent by construction.

Every field this module adds for Codex is OPTIONAL and CODEX-ONLY.
Absence carries the Claude meaning. That is what keeps the Claude
serializer byte-identical and the dashboard goldens' Claude blocks
unmoved, which is the safety property the F18 refactor rests on.
"""

CLAUDE_PREDICATES = ("net_negative", "cache_drop")
CODEX_PREDICATES = ("cache_drop",)

# Every predicate any provider can carry. Claude happens to span all of
# them today, but the filter below must key on THIS, not on
# CLAUDE_PREDICATES: if Claude's set ever shrank, another provider's set
# could become a superset of it and the filter would silently degrade to a
# no-op for that provider — a filter that discards nothing, which is the
# exact failure `filter_inapplicable` exists to prevent.
ALL_PREDICATES = tuple(
    dict.fromkeys(CLAUDE_PREDICATES + CODEX_PREDICATES)
)

# Codex figures that are structurally absent rather than unmeasured.
# OpenAI charges no cache-write premium, so there is nothing to waste and
# therefore no ratio of saved to wasted. Their values are None on the Codex
# wire; this map remains the authoritative user-facing reason.
CODEX_NOT_APPLICABLE = {
    "wasted_usd": "OpenAI charges no cache-write premium, so Codex has no wasted-cache figure.",
    "fourteen_day_efficiency_ratio": "Efficiency compares saved against wasted, and Codex has no wasted-cache figure.",
}

_PREDICATES = {"claude": CLAUDE_PREDICATES, "codex": CODEX_PREDICATES}


def applicable_predicates(provider):
    """Return the anomaly predicates that can apply to ``provider``."""
    try:
        return _PREDICATES[provider]
    except KeyError:
        raise ValueError(f"unknown cache-report provider: {provider!r}") from None


def filter_inapplicable(provider, row):
    """Drop predicates that do not apply to ``provider`` and re-derive.

    Filtering is an ACTIVE step, not an assumption. Today no Codex row can
    carry `net_negative` — saved is floored at zero and wasted is
    hard-zero — but that is a property of how entries are built, not of
    this builder, and a test that only observed the absence would pass
    over a builder that discards nothing.

    `anomaly_triggered` is RECOMPUTED from the surviving reasons rather
    than carried through: keeping a True flag whose only reason was
    filtered away would publish a verdict with nothing behind it.
    """
    applicable = set(applicable_predicates(provider))
    if applicable.issuperset(ALL_PREDICATES):
        return row
    out = dict(row)
    out["anomaly_reasons"] = [r for r in row["anomaly_reasons"] if r in applicable]
    out["anomaly_unevaluated"] = [
        r for r in row["anomaly_unevaluated"] if r in applicable
    ]
    out["anomaly_triggered"] = bool(out["anomaly_reasons"])
    return out


def _percent(provider, row):
    """Emit the provider-authoritative percent key for one row mapping."""
    value = row["cache_hit_percent"]
    if provider == "codex":
        return {"cached_input_percent": value}
    return {"cache_hit_percent": value}


def _today_block(provider, today):
    today = filter_inapplicable(provider, today)
    block = {"date": today["date"]}
    block.update(_percent(provider, today))
    block.update({
        "baseline_median_percent": today["baseline_median_percent"],
        "delta_pp": today["delta_pp"],
        "net_usd": today["net_usd"],
        "saved_usd": today["saved_usd"],
        "wasted_usd": None if provider == "codex" else today["wasted_usd"],
        "anomaly_triggered": today["anomaly_triggered"],
        "anomaly_reasons": list(today["anomaly_reasons"]),
        "baseline_daily_row_count": today["baseline_daily_row_count"],
        "anomaly_unevaluated": list(today["anomaly_unevaluated"]),
        "observed": today["observed"],
    })
    return block


def _day_block(provider, d):
    d = filter_inapplicable(provider, d)
    block = {"date": d["date"]}
    block.update(_percent(provider, d))
    block.update({
        "input_tokens": d["input_tokens"],
        "output_tokens": d["output_tokens"],
        "cache_creation_tokens": d["cache_creation_tokens"],
        "cache_read_tokens": d["cache_read_tokens"],
        "saved_usd": d["saved_usd"],
        "wasted_usd": None if provider == "codex" else d["wasted_usd"],
        "net_usd": d["net_usd"],
        "anomaly_triggered": d["anomaly_triggered"],
        "anomaly_reasons": list(d["anomaly_reasons"]),
        "anomaly_unevaluated": list(d["anomaly_unevaluated"]),
        "observed": d["observed"],
    })
    return block


def _breakdown_block(provider, b):
    block = {"key": b["key"]}
    block.update(_percent(provider, b))
    block["net_usd"] = b["net_usd"]
    return block


def build_cache_report_wire(
    *, provider, window_days, anomaly_threshold_pp, anomaly_window_days,
    today, days, by_project, by_model, seven_day_net_usd,
    seven_day_anomaly_count, fourteen_day_counterfactual_usd,
    fourteen_day_efficiency_ratio, is_empty,
):
    """Serialize one cache-report block for ``provider``.

    ``days`` MUST arrive newest-first and already capped at
    ``window_days`` — the Codex ``seven_day_anomaly_count`` reconciliation
    below slices ``[:7]`` positionally, so an oldest-first caller would get
    a wrong count with no error raised.
    """
    applicable_predicates(provider)  # validates
    day_blocks = [_day_block(provider, d) for d in days]
    out = {
        "window_days": window_days,
        "anomaly_threshold_pp": anomaly_threshold_pp,
        "anomaly_window_days": anomaly_window_days,
        "today": _today_block(provider, today),
        "days": day_blocks,
        "by_project": [_breakdown_block(provider, b) for b in by_project],
        "by_model": [_breakdown_block(provider, b) for b in by_model],
        "seven_day_net_usd": seven_day_net_usd,
        "seven_day_anomaly_count": seven_day_anomaly_count,
        "fourteen_day_counterfactual_usd": fourteen_day_counterfactual_usd,
        "fourteen_day_efficiency_ratio": fourteen_day_efficiency_ratio,
        "is_empty": is_empty,
    }
    if provider == "codex":
        out["fourteen_day_efficiency_ratio"] = None
        out["not_applicable"] = dict(CODEX_NOT_APPLICABLE)
        out["anomaly_predicates"] = list(CODEX_PREDICATES)
        # The caller counted anomalies BEFORE inapplicable predicates were
        # dropped, so its number can outlive every verdict behind it.
        # Reconciled only where the filter can actually change something:
        # on Claude the filter is the identity, and re-deriving there
        # would risk moving a value the byte-stability pin protects.
        out["seven_day_anomaly_count"] = sum(
            bool(block["anomaly_triggered"]) for block in day_blocks[:7]
        )
    return out
