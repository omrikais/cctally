"""Pure-fn kernel for the pricing-freshness check (spec 2026-05-29).

No I/O, no import of `cctally`/`_lib_pricing` at module scope — every
dependency (pricing predicates, tables, observed rows, LiteLLM snapshot)
is passed in by the I/O glue in bin/cctally. Re-exported there like the
other _lib_* kernels.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class CoverageGap:
    provider: str        # "claude" | "codex"
    model: str
    kind: str            # "unpriced" | "fallback"
    entry_count: int
    token_total: int


@dataclasses.dataclass(frozen=True)
class DriftRow:
    model: str
    field: str           # "" for whole-model categories
    ours: "float | None"
    theirs: "float | None"


@dataclasses.dataclass(frozen=True)
class DriftResult:
    value_drift: list          # list[DriftRow]
    missing_from_us: list      # list[str]
    ahead_of_litellm: list     # list[str] — informational; never actionable


def classify_coverage(observed, resolve_claude, is_codex_fallback) -> list[CoverageGap]:
    """observed: iterable of (provider, model, entry_count, token_total).

    Claude model with resolve_claude(model) is None -> kind="unpriced".
    Codex model with is_codex_fallback(model) True  -> kind="fallback".
    Priced models produce no gap. Order preserved.
    """
    gaps: list[CoverageGap] = []
    for provider, model, entry_count, token_total in observed:
        if provider == "claude":
            if resolve_claude(model) is None:
                gaps.append(CoverageGap("claude", model, "unpriced", entry_count, token_total))
        elif provider == "codex":
            if is_codex_fallback(model):
                gaps.append(CoverageGap("codex", model, "fallback", entry_count, token_total))
    return gaps


def _is_codex_scope(name: str) -> bool:
    # The Codex models we track are the gpt-5* family (incl. -codex variants).
    # Keep this in sync with CODEX_MODEL_PRICING's key prefixes.
    return name.startswith("gpt-5")


def scope_litellm(litellm: dict) -> dict[str, dict]:
    """Filter a full LiteLLM model_prices map down to the models we track:
    anthropic-provider Claude models, and the gpt-5* Codex family. Skips the
    `sample_spec` doc entry and any entry lacking a dict body."""
    scoped: dict[str, dict] = {}
    for name, body in litellm.items():
        if not isinstance(body, dict):
            continue
        provider = body.get("litellm_provider")
        if provider == "anthropic" and name.startswith("claude-"):
            scoped[name] = body
        elif provider == "openai" and _is_codex_scope(name):
            scoped[name] = body
    return scoped


_DRIFT_EPS = 1e-12  # cost-per-token values are tiny; compare with a small abs epsilon


def _allow_index(allowlist):
    field_suppress = set()   # (model, field)
    model_suppress = set()   # model (no field -> suppresses missing_from_us)
    for e in allowlist or []:
        if e.get("field"):
            field_suppress.add((e["model"], e["field"]))
        else:
            model_suppress.add(e["model"])
    return field_suppress, model_suppress


def diff_pricing(claude_tbl, codex_tbl, litellm_scoped, allowlist=None) -> "DriftResult":
    """Direction-aware drift between our embedded tables and the scoped LiteLLM
    snapshot.

    value_drift     — shared model, a cost field differs beyond _DRIFT_EPS
                      (actionable, unless allowlisted by model+field).
    missing_from_us — scoped LiteLLM model absent from our tables
                      (actionable, unless allowlisted by model with no field).
    ahead_of_litellm — model we price that scoped LiteLLM lacks (informational;
                      NEVER actionable — we may legitimately lead the source).
    """
    field_suppress, model_suppress = _allow_index(allowlist)
    ours = {**claude_tbl, **codex_tbl}
    value_drift: list = []
    missing: list = []
    ahead: list = []

    for model, body in litellm_scoped.items():
        if model in ours:
            for field, theirs in body.items():
                if not field.endswith("_cost_per_token") and "cost" not in field:
                    continue
                if not isinstance(theirs, (int, float)):
                    continue
                if (model, field) in field_suppress:
                    continue
                mine = ours[model].get(field)
                if mine is None:
                    continue  # we don't carry this field; not a value-drift signal
                if abs(float(mine) - float(theirs)) > _DRIFT_EPS:
                    value_drift.append(DriftRow(model, field, float(mine), float(theirs)))
        else:
            if model not in model_suppress:
                missing.append(model)

    for model in ours:
        if model not in litellm_scoped:
            ahead.append(model)

    return DriftResult(value_drift=value_drift, missing_from_us=missing, ahead_of_litellm=ahead)
