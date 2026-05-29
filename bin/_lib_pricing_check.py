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


def stale_allowlist_entries(allowlist, claude_tbl, codex_tbl, litellm_scoped) -> list:
    """Return allowlist entries that NO LONGER correspond to a real divergence.

    An entry is stale if, with it removed, diff_pricing reports nothing it would
    have suppressed (i.e. the value now matches / the model is now present)."""
    ours = {**claude_tbl, **codex_tbl}
    stale: list = []
    for e in allowlist or []:
        model = e["model"]
        if e.get("field"):
            theirs = (litellm_scoped.get(model) or {}).get(e["field"])
            mine = (ours.get(model) or {}).get(e["field"])
            real = (theirs is not None and mine is not None
                    and abs(float(mine) - float(theirs)) > _DRIFT_EPS)
        else:
            # model-suppress entry: real only if litellm has it AND we don't
            real = (model in litellm_scoped and model not in ours)
        if not real:
            stale.append(e)
    return stale


_CLAUDE_REQUIRED = ("input_cost_per_token", "output_cost_per_token",
                    "cache_creation_input_token_cost", "cache_read_input_token_cost")
_CODEX_REQUIRED = ("input_cost_per_token", "cache_read_input_token_cost",
                   "output_cost_per_token")


def check_table_shapes(claude_tbl, codex_tbl, zero_sentinels) -> list:
    """Provider-specific well-formedness. Claude entries need the 4 required
    fields; Codex entries need the 3 base fields (NO cache_creation) and may
    carry optional *_above_272k_tokens tiered fields. All present cost fields
    must be >= 0. An all-zero Codex entry is allowed ONLY if its model is in
    `zero_sentinels` (e.g. gpt-5.3-codex-spark mirroring upstream $0)."""
    problems: list = []

    def _check(model, body, required, allow_zero):
        for f in required:
            if f not in body:
                problems.append(f"{model}: missing required field {f}")
        cost_fields = {k: v for k, v in body.items() if "cost" in k}
        for k, v in cost_fields.items():
            if not isinstance(v, (int, float)) or v < 0:
                problems.append(f"{model}: field {k} not a non-negative number ({v!r})")
        if cost_fields and all(float(v) == 0.0 for v in cost_fields.values()) and not allow_zero:
            problems.append(f"{model}: all cost fields zero but not a documented sentinel")

    for model, body in claude_tbl.items():
        _check(model, body, _CLAUDE_REQUIRED, allow_zero=False)
    for model, body in codex_tbl.items():
        _check(model, body, _CODEX_REQUIRED, allow_zero=model in zero_sentinels)
    return problems
