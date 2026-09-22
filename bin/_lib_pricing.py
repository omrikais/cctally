"""Model pricing tables and cost-computation primitives.

Pure-fn layer (no I/O at import time): holds the embedded Anthropic
(`CLAUDE_MODEL_PRICING`) and OpenAI Codex (`CODEX_MODEL_PRICING`) pricing
snapshots plus the helpers that consume them — model-name normalization,
chip palette, per-entry cost calculation for both providers.

`bin/cctally` re-exports every symbol below so internal call sites resolve
unchanged. Tests reach into this layer via the re-exported names on the
`cctally` module; no direct import of `_lib_pricing` is expected from tests.

A private `_eprint` duplicates `bin/cctally:eprint` (two-line stderr helper)
so this pure layer carries zero back-imports per the split design's
Section 5.3 contract.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import contextlib as _contextlib
import contextvars as _contextvars
import dataclasses
import datetime as dt
import re
import sys
import threading as _threading
from typing import Any

_sys = sys


def _eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


TIERED_THRESHOLD = 200_000


def _chip_for_model(name: str) -> str:
    """Bucket a canonical model id into a small chip palette.

    Returns one of 'opus' | 'sonnet' | 'haiku' | 'fable' | 'other'. Used by the
    dashboard's Weekly / Monthly panels and modals so per-model
    coloring stays consistent across the UI. #244 — `fable` is a dedicated
    family (Fable is a current first-class model); it must NOT fall through to
    the neutral 'other' bucket, mirroring the frontend `modelChipClass`.
    """
    n = (name or "").lower()
    if "opus" in n:
        return "opus"
    if "sonnet" in n:
        return "sonnet"
    if "haiku" in n:
        return "haiku"
    if "fable" in n:
        return "fable"
    return "other"


# Date the embedded pricing snapshots below were last verified against
# vendor sources. Bump whenever CLAUDE_MODEL_PRICING / CODEX_MODEL_PRICING
# is synced. Read by `pricing-check` + the release pre-flight staleness nudge.
#
# CONTRACT (#705): a pricing revision must always ADVANCE this date. A second
# revision within one UTC day therefore takes the NEXT day's date rather than
# repeating one. The reason is the ordered-write guard in
# `_cctally_cache._pricing_write_authorized`, which compares this value to the
# fingerprint a store recorded and refuses a write from an older process. That
# comparison is day-granular by construction, so two revisions sharing a date
# compare equal and the older process is authorized to write.
PRICING_SNAPSHOT_DATE = "2026-09-23"
PRICING_STALENESS_DAYS = 60  # release pre-flight WARNs past this age


def parse_pricing_fingerprint(value):
    """Parse a recorded pricing fingerprint into a ``datetime.date``, or return
    None when it cannot be ordered against PRICING_SNAPSHOT_DATE (#705).

    THE single parse of that contract. It has two callers that must never
    disagree: `_cctally_cache._pricing_write_authorized`, which refuses a write
    it cannot order, and `_lib_doctor._check_pricing_conversation_rollup_writer`,
    which reports which of the two refusal states a store is in. They were two
    separate `date.fromisoformat` calls, and they had already diverged — the
    doctor one coerced with `str()`, so on Python 3.11+ an integer fingerprint
    parsed there as a basic-format ISO date and raised TypeError in the guard.
    Doctor then printed the ordinary "restart or upgrade the writing process"
    remedy for a store no version can write.

    A non-string is therefore NOT coerced. The value is passed to
    `date.fromisoformat` exactly as the store returned it.
    """
    try:
        return dt.date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def pricing_fingerprint_is_comparable(value) -> bool:
    """Whether a stored fingerprint is one the ordered-write guard can order.

    Absent and empty are comparable: the guard treats both as older than
    anything, which is the ordinary state of a store that has never recorded a
    fingerprint, not a corrupt one.
    """
    if not value:
        return True
    return parse_pricing_fingerprint(value) is not None


class PricingFingerprintObservation:
    """ONE read of a store's recorded pricing fingerprint, in four states
    (#728).

    ``absent``    — the read succeeded and no value is recorded.
    ``present``   — read and parsed; ``parsed_date`` is orderable.
    ``malformed`` — read successfully, but the value is not an ISO date.
    ``degraded``  — the read ITSELF failed; nothing is known about the store.

    Four rather than three, because ``present(parsed_date, raw)`` cannot also
    carry an unparseable value: a ``present`` observation whose ``parsed_date``
    is None would either raise on the comparison or be silently read as
    absent. And ``degraded`` is separate from ``absent`` because the whole
    defect this replaces was a failed ``SELECT`` degrading to None and then
    reading as "a fresh store with nothing to protect", which authorized every
    writer over a store it had not managed to read.

    Frozen, so a consumer cannot downgrade a degraded read to an absent one by
    assignment.

    Written by hand rather than with ``@dataclasses.dataclass(frozen=True)``,
    and that is load-bearing for this module rather than a style choice. This
    file declares ``from __future__ import annotations``, so every field
    annotation is a string, and `dataclasses` resolves a string annotation via
    ``sys.modules.get(cls.__module__).__dict__`` while building the class. This
    module is deliberately a pure stdlib leaf that several callers load by
    executing the file under a private name that is never registered in
    ``sys.modules`` — `tests/test_claude_fast_pricing.py` is one — and for
    those the lookup returns None and the class body raises ``AttributeError``
    at import. A hand-rolled frozen class has no such dependency.
    """

    __slots__ = ("state", "parsed_date", "raw", "error_kind")

    def __init__(self, state, parsed_date=None, raw=None, error_kind=None):
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "parsed_date", parsed_date)
        object.__setattr__(self, "raw", raw)
        object.__setattr__(self, "error_kind", error_kind)

    def __setattr__(self, name, value):
        raise dataclasses.FrozenInstanceError(f"cannot assign to field {name!r}")

    def __delattr__(self, name):
        raise dataclasses.FrozenInstanceError(f"cannot delete field {name!r}")

    def __copy__(self):
        # An immutable value shares rather than duplicates. Required as well as
        # correct: `__slots__` plus a refusing `__setattr__` means the generic
        # `copy._reconstruct` path re-assigns each slot and raises, which
        # `dataclasses.asdict` hits the moment one of these becomes a field of
        # a dataclass — `DoctorState` carries two.
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return (self.__class__, self._key())

    def _key(self):
        return (self.state, self.parsed_date, self.raw, self.error_kind)

    def __eq__(self, other):
        if not isinstance(other, PricingFingerprintObservation):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self):
        return hash(self._key())

    def __repr__(self):
        return (
            "PricingFingerprintObservation("
            f"state={self.state!r}, parsed_date={self.parsed_date!r}, "
            f"raw={self.raw!r}, error_kind={self.error_kind!r})"
        )


def classify_pricing_fingerprint(
    *, found: bool, raw: Any, error_kind: "str | None",
) -> PricingFingerprintObservation:
    """Interpret ONE fingerprint read. Pure: the caller performs the SELECT and
    reports what happened; this decides what it means.

    ``error_kind`` set means the read itself failed, which is NOT the same as a
    store with nothing recorded — it wins over any value the partial read
    returned, because a torn observation is not evidence about the store.

    ``parse_pricing_fingerprint`` stays the sole date parser: the guard, doctor
    and this classifier must never disagree about which values are orderable,
    and two parses of that one contract have already drifted apart once.
    """
    if error_kind is not None:
        return PricingFingerprintObservation("degraded", None, raw, error_kind)
    if not found or not raw:
        return PricingFingerprintObservation("absent")
    parsed = parse_pricing_fingerprint(raw)
    if parsed is None:
        return PricingFingerprintObservation("malformed", None, raw)
    return PricingFingerprintObservation("present", parsed, raw)


def _coerce_process_date(process_date: Any) -> "dt.date | None":
    """The writing process's own snapshot date as an orderable ``date``.

    Accepts the date directly or the recorded string form, and returns None for
    anything that cannot be ordered — which every predicate below refuses, so
    the guard fails closed on BOTH sides rather than only on the stored one.
    """
    if isinstance(process_date, dt.datetime):
        return process_date.date()
    if isinstance(process_date, dt.date):
        return process_date
    return parse_pricing_fingerprint(process_date)


def _ordered_ok(
    obs: PricingFingerprintObservation, process_date: Any,
) -> bool:
    """Day-granular ordering: absent is older than anything, present compares,
    and neither malformed nor degraded is orderable at all."""
    if obs.state == "absent":
        return True
    if obs.state != "present":
        return False
    resolved = _coerce_process_date(process_date)
    if resolved is None or obs.parsed_date is None:
        return False
    return obs.parsed_date <= resolved


def may_write_materialized_cost(
    obs: PricingFingerprintObservation, process_date: Any,
) -> bool:
    """Whether this process may materialize cost over the observed store.

    A refused write costs a stale rollup, so ``absent`` still fails open: a
    store that genuinely recorded nothing has nothing to protect.
    """
    return _ordered_ok(obs, process_date)


def may_reset_rebuild_target(
    obs: PricingFingerprintObservation, process_date: Any,
) -> bool:
    """Whether this process may CLEAR a rebuild target before replacing it.

    Same ordering, and this is the path that must fail closed: a refused clear
    costs nothing, while a clear performed on the strength of a read that never
    happened destroys a rollup this process may then be refused permission to
    re-derive.
    """
    return _ordered_ok(obs, process_date)

# Canonical machine-readable pricing source (Claude values + Codex values).
LITELLM_PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# Deliberate divergences from LiteLLM the drift check must NOT flag. Each
# entry suppresses either a specific value mismatch ({"model","field","reason"})
# or an intentionally-omitted in-scope model ({"model","reason"} — no field).
# Guarded by `stale_allowlist_entries` (tests/test_pricing_check.py): an entry
# that no longer corresponds to a real divergence fails the suite.
#
# claude-mythos-preview (#560): Anthropic's Project Glasswing launch priced the
# historical Preview at $25/$125 per MTok after its credit period. LiteLLM
# currently mirrors successor Mythos 5's lower $10/$50 rate onto the Preview
# identifier. Retained Preview rows therefore keep the explicit historical
# rate rather than being rewritten to the successor's rate.
#
# gpt-5.6-sol (#643): OpenAI's pricing page states that "GPT-5.6 Sol's
# promotional pricing is available at least through November 21, 2026" and
# publishes no post-promotional price, so LiteLLM's $4/$20 per MTok rate is
# time-boxed. This table is date-blind and roughly half the retained Sol rows
# predate the promotion, so the pre-promotional $5/$30 card is kept and the
# promotion is suppressed here. `expires` is what stops the divergence
# ossifying: `stale_allowlist_entries` only fires if LiteLLM reverts, so a
# promotion made permanent would otherwise never surface.
#
# gpt-5.6 (#643): a model-only entry, which suppresses `missing_from_us` rather
# than a value drift. OpenAI lists `gpt-5.6` as an alias of `gpt-5.6-sol`, so
# CODEX_MODEL_ALIASES resolves it instead of duplicating the rate card.
# `diff_pricing` keys on raw table membership and is deliberately NOT
# alias-aware, so the omission needs this entry. It is self-policing: it goes
# stale automatically if upstream drops `gpt-5.6` or we restore a direct card.
PRICING_DRIFT_ALLOWLIST: list[dict] = [
    {
        "model": "claude-mythos-preview",
        "field": field,
        "reason": (
            "Anthropic priced historical Claude Mythos Preview at $25/$125 "
            "per MTok after its Project Glasswing credit period; LiteLLM "
            "currently mirrors successor Mythos 5's $10/$50 rate onto the "
            "Preview identifier (#560)."
        ),
    }
    for field in (
        "input_cost_per_token",
        "output_cost_per_token",
        "cache_creation_input_token_cost",
        "cache_read_input_token_cost",
    )
] + [
    {
        "model": "gpt-5.6-sol",
        "field": field,
        "expires": "2026-11-21",
        "reason": (
            "OpenAI's pricing page guarantees GPT-5.6 Sol's promotional "
            "$4/$20 per MTok rate only 'at least through November 21, 2026' "
            "and publishes no post-promotional price. This table is "
            "date-blind, so the pre-promotional $5/$30 card is kept rather "
            "than adopting a rate that would have to be reverted by hand "
            "when the promotion ends (#643)."
        ),
    }
    for field in (
        "input_cost_per_token",
        "cache_read_input_token_cost",
        "output_cost_per_token",
        "input_cost_per_token_above_272k_tokens",
        "cache_read_input_token_cost_above_272k_tokens",
        "output_cost_per_token_above_272k_tokens",
    )
] + [
    {
        "model": "gpt-5.6",
        "reason": (
            "OpenAI lists gpt-5.6 as an alias of gpt-5.6-sol, so "
            "CODEX_MODEL_ALIASES resolves it to Sol's card and this table "
            "carries no duplicate entry. `diff_pricing` is deliberately not "
            "alias-aware, so the intentional omission is suppressed here "
            "(#643)."
        ),
    },
]

# Anthropic API pricing snapshot:
# - Source: https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
# - Captured/verified: 2026-08-13 against LiteLLM plus Anthropic's current
#   pricing page and model launch pages.
# - Vendor sources: https://platform.claude.com/docs/en/about-claude/pricing,
#   https://www.anthropic.com/news/claude-sonnet-5,
#   https://www.anthropic.com/news/claude-fable-5-mythos-5, and
#   https://www.anthropic.com/glasswing. Update in changes touching this table.
#   2026-06-10: added claude-fable-5 ($10/$50 per MTok; 1M context, no
#   long-context premium) — issue #172.
#   2026-07-01 (#274, superseded by #560): initially added claude-sonnet-5 at
#   the then-announced future $3/$15 standard rate, preferring it over the
#   launch promotion because this table is date-blind. LiteLLM's $2/$10 row was
#   temporarily suppressed on all four fields. Anthropic later cancelled that
#   transition; the 2026-08-13 entry below records the replacement decision.
#   2026-07-24: added claude-opus-5 ($5/$25 per MTok — identical to Opus
#   4.5/4.6/4.7/4.8; $6.25 5-minute cache write and $0.50 cache read at the
#   standard 1.25x/0.1x multipliers). 1M context at standard pricing, so NO
#   above-200k tier. Dateless pinned id with no dated twin (matches Anthropic's
#   published ID/alias table). LiteLLM has no opus-5 entry yet, so this table is
#   simply ahead of it — `ahead_of_litellm` is never a drift finding, so no
#   PRICING_DRIFT_ALLOWLIST entry is needed.
#   2026-07-25 (#195): no VALUES changed. The snapshot date is bumped because
#   cache WRITES are now priced by TTL — the 1-hour rate is DERIVED as
#   `input_cost_per_token * CACHE_WRITE_1H_MULTIPLIER` (2.0) rather than stored
#   per-model, so it is automatically correct for any model added later and
#   cannot be silently missed the way an explicit field can be. The stored
#   `cache_creation_input_token_cost` remains the 5-minute (1.25x) rate. The
#   bump is also the deliberate fingerprint bust that re-arms the conversation
#   rollup's materialized cost (`_arm_rollup_backfill_on_pricing_change`).
#   2026-07-28 (#413): modelled Claude fast mode from the authoritative retained
#   `message.usage.speed` value. Current Opus 5/4.8 fast rows are $10/$50 per
#   MTok (2x standard). Historical effective-fast Opus 4.6/4.7 rows retain
#   their documented $30/$150 rate (6x standard). Current Opus 4.6 fallback
#   reports `speed="standard"` and is therefore never premium-priced. Prompt
#   cache multipliers stack on the fast base rate. This snapshot bump is the
#   pricing-fingerprint bust for safe conversation-rollup rederivation; durable
#   journaled milestones and weekly snapshots are not rewritten.
#   2026-08-13 (#560): Sonnet 5's $2/$10 launch pricing is now permanent, so
#   replaced the cancelled $3/$15 rate and removed its four temporary drift
#   suppressions. Added Mythos 5 at $10/$50 from Anthropic's launch/current
#   pricing pages and historical Mythos Preview at the explicit $25/$125
#   Project Glasswing rate. The snapshot bump re-arms the existing conversation
#   rollup pricing fingerprint; immutable journaled/stored facts stay unchanged.
#   2026-09-02: added claude-fable-5-1 and claude-mythos-5-1 at $10/$50 per
#   MTok with a $12.50 5-minute cache write, matching Fable 5 and Mythos 5.
#   Their cache READ is $0.25 per MTok, which is 0.025x base input rather than
#   the standard 0.1x. Anthropic's pricing page documents that rate for these
#   two models in a footnote as their standard price, not as an introductory
#   one, so the value is adopted here rather than suppressed in
#   PRICING_DRIFT_ALLOWLIST. Verified against LiteLLM and the vendor pricing
#   page on 2026-09-02. Anthropic's /v1/models lists claude-fable-5-1, so
#   until now real Fable 5.1 usage was priced at zero with only a warning. 1M
#   context at standard pricing, so NO above-200k tier.
#   2026-09-22: added claude-opus-5-5 at Anthropic's published $4/$20 per
#   MTok, $5 five-minute cache write, $0.20 cache read, and 2x fast rate.
#   Its 1M context uses standard rates throughout. The pricing fingerprint
#   advances to 2026-09-23 because another revision already used 2026-09-22.
# Anthropic prices a cache WRITE by TTL: 1.25x base input for a 5-minute write,
# 2x for a 1-hour write. Both WRITE multipliers are documented as applying
# consistently across all supported models, so the 1h rate is DERIVED from
# input_cost_per_token rather than stored per-model — a model added later
# cannot silently miss it (#195). Cache READS are NOT uniform and are NOT
# derived: they are 0.1x base input on most models but 0.025x on Claude Fable
# 5.1 and Claude Mythos 5.1 and 0.05x on Claude Opus 5.5, so the read rate stays stored per-model in
# `cache_read_input_token_cost`, which represents a model-specific rate.
CACHE_WRITE_1H_MULTIPLIER = 2.0

CLAUDE_MODEL_PRICING: dict[str, dict[str, Any]] = {
    "claude-3-5-haiku-20241022": {
        "input_cost_per_token": 8e-07,
        "output_cost_per_token": 4e-06,
        "cache_creation_input_token_cost": 1e-06,
        "cache_read_input_token_cost": 8e-08,
    },
    "claude-3-5-haiku-latest": {
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 5e-06,
        "cache_creation_input_token_cost": 1.25e-06,
        "cache_read_input_token_cost": 1e-07,
    },
    "claude-3-5-sonnet-20240620": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
    "claude-3-5-sonnet-20241022": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
    "claude-3-5-sonnet-latest": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
    "claude-3-7-sonnet-20250219": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
    "claude-3-7-sonnet-latest": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
    "claude-3-haiku-20240307": {
        "input_cost_per_token": 2.5e-07,
        "output_cost_per_token": 1.25e-06,
        "cache_creation_input_token_cost": 3e-07,
        "cache_read_input_token_cost": 3e-08,
    },
    "claude-3-opus-20240229": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_creation_input_token_cost": 1.875e-05,
        "cache_read_input_token_cost": 1.5e-06,
    },
    "claude-3-opus-latest": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_creation_input_token_cost": 1.875e-05,
        "cache_read_input_token_cost": 1.5e-06,
    },
    "claude-4-opus-20250514": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_creation_input_token_cost": 1.875e-05,
        "cache_read_input_token_cost": 1.5e-06,
    },
    "claude-4-sonnet-20250514": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
        "input_cost_per_token_above_200k_tokens": 6e-06,
        "output_cost_per_token_above_200k_tokens": 2.25e-05,
        "cache_creation_input_token_cost_above_200k_tokens": 7.5e-06,
        "cache_read_input_token_cost_above_200k_tokens": 6e-07,
    },
    "claude-fable-5": {
        "input_cost_per_token": 1e-05,
        "output_cost_per_token": 5e-05,
        "cache_creation_input_token_cost": 1.25e-05,
        "cache_read_input_token_cost": 1e-06,
    },
    "claude-fable-5-1": {
        "input_cost_per_token": 1e-05,
        "output_cost_per_token": 5e-05,
        "cache_creation_input_token_cost": 1.25e-05,
        # 0.025x base input, not the standard 0.1x — see the 2026-09-02 note.
        "cache_read_input_token_cost": 2.5e-07,
    },
    "claude-haiku-4-5": {
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 5e-06,
        "cache_creation_input_token_cost": 1.25e-06,
        "cache_read_input_token_cost": 1e-07,
    },
    "claude-haiku-4-5-20251001": {
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 5e-06,
        "cache_creation_input_token_cost": 1.25e-06,
        "cache_read_input_token_cost": 1e-07,
    },
    "claude-mythos-5": {
        "input_cost_per_token": 1e-05,
        "output_cost_per_token": 5e-05,
        "cache_creation_input_token_cost": 1.25e-05,
        "cache_read_input_token_cost": 1e-06,
    },
    "claude-mythos-5-1": {
        "input_cost_per_token": 1e-05,
        "output_cost_per_token": 5e-05,
        "cache_creation_input_token_cost": 1.25e-05,
        # 0.025x base input, not the standard 0.1x — see the 2026-09-02 note.
        "cache_read_input_token_cost": 2.5e-07,
    },
    "claude-mythos-preview": {
        "input_cost_per_token": 2.5e-05,
        "output_cost_per_token": 1.25e-04,
        "cache_creation_input_token_cost": 3.125e-05,
        "cache_read_input_token_cost": 2.5e-06,
    },
    "claude-opus-4-1": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_creation_input_token_cost": 1.875e-05,
        "cache_read_input_token_cost": 1.5e-06,
    },
    "claude-opus-4-1-20250805": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_creation_input_token_cost": 1.875e-05,
        "cache_read_input_token_cost": 1.5e-06,
    },
    "claude-opus-4-20250514": {
        "input_cost_per_token": 1.5e-05,
        "output_cost_per_token": 7.5e-05,
        "cache_creation_input_token_cost": 1.875e-05,
        "cache_read_input_token_cost": 1.5e-06,
    },
    "claude-opus-4-5": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-4-5-20251101": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-4-6": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-4-6-20260205": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-4-7": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-4-7-20260416": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-4-8": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-5": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_creation_input_token_cost": 6.25e-06,
        "cache_read_input_token_cost": 5e-07,
    },
    "claude-opus-5-5": {
        # Source: https://platform.claude.com/docs/en/models/opus-5-5/overview
        "input_cost_per_token": 4e-06,
        "output_cost_per_token": 2e-05,
        "cache_creation_input_token_cost": 5e-06,
        "cache_read_input_token_cost": 2e-07,
    },
    "claude-sonnet-4-20250514": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
        "input_cost_per_token_above_200k_tokens": 6e-06,
        "output_cost_per_token_above_200k_tokens": 2.25e-05,
        "cache_creation_input_token_cost_above_200k_tokens": 7.5e-06,
        "cache_read_input_token_cost_above_200k_tokens": 6e-07,
    },
    "claude-sonnet-4-5": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
        "input_cost_per_token_above_200k_tokens": 6e-06,
        "output_cost_per_token_above_200k_tokens": 2.25e-05,
        "cache_creation_input_token_cost_above_200k_tokens": 7.5e-06,
        "cache_read_input_token_cost_above_200k_tokens": 6e-07,
    },
    "claude-sonnet-4-5-20250929": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
        "input_cost_per_token_above_200k_tokens": 6e-06,
        "output_cost_per_token_above_200k_tokens": 2.25e-05,
        "cache_creation_input_token_cost_above_200k_tokens": 7.5e-06,
        "cache_read_input_token_cost_above_200k_tokens": 6e-07,
    },
    "claude-sonnet-4-6": {
        "input_cost_per_token": 3e-06,
        "output_cost_per_token": 1.5e-05,
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_read_input_token_cost": 3e-07,
    },
    "claude-sonnet-5": {
        "input_cost_per_token": 2e-06,
        "output_cost_per_token": 1e-05,
        "cache_creation_input_token_cost": 2.5e-06,
        "cache_read_input_token_cost": 2e-07,
    },
}

# Anthropic fast-mode pricing is genuinely model-specific. Unsupported models
# deliberately have no fallback multiplier: only a retained authoritative
# `usage.speed == "fast"` row on one of these exact model IDs is premium-priced.
# The 4.6/4.7 entries are historical retention rules; current new fast requests
# are supported only on Opus 5.5, Opus 5 and Opus 4.8.
CLAUDE_FAST_MULTIPLIER_OVERRIDES: dict[str, float] = {
    "claude-opus-4-6": 6.0,
    "claude-opus-4-6-20260205": 6.0,
    "claude-opus-4-7": 6.0,
    "claude-opus-4-7-20260416": 6.0,
    "claude-opus-4-8": 2.0,
    "claude-opus-5": 2.0,
    "claude-opus-5-5": 2.0,
}


def _strip_anthropic_model_prefix(model: str) -> str:
    """Return the pricing-table model ID behind supported provider aliases."""
    for prefix in ("anthropic/", "anthropic."):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def _claude_fast_multiplier(model: str) -> float:
    """Fast-tier multiplier for a retained Claude model (standard = 1.0)."""
    return current_pricing_snapshot().fast_multipliers["claude"].get(
        _strip_anthropic_model_prefix(model), 1.0
    )

_unknown_model_warnings: set[str] = set()

# ---------------------------------------------------------------------------
# Codex / OpenAI pricing table
# ---------------------------------------------------------------------------
#
# Codex (OpenAI) API pricing snapshot:
# - Source: https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
# - Captured: 2026-07-19 — the last FULL Codex sync. PRICING_SNAPSHOT_DATE has
#   since moved for six targeted syncs (2026-07-24, the Claude-side opus-5
#   sync; 2026-07-31, the gpt-5.6-terra/-luna correction logged below;
#   2026-08-13, the Claude-side Sonnet/Mythos sync above; 2026-08-25, the
#   gpt-5.6-cyber addition logged below; 2026-09-05, the gpt-6-astra
#   addition; and 2026-09-22, the gpt-6-sol addition logged below). Codex
#   values outside those four Codex corrections were NOT re-verified on those
#   days.
# - As of the 2026-07-19 sync this carries every openai-provider
#   gpt-5* model the LiteLLM snapshot lists, so `pricing-check`'s scope finds
#   nothing missing. Models absent from this table still fall back to `gpt-5`
#   pricing with isFallback=true (matches upstream's LEGACY_FALLBACK_MODEL
#   behavior); a one-shot stderr warning is emitted per unknown model name.
#   2026-07-10: added the gpt-5.6 family (gpt-5.6, gpt-5.6-sol, gpt-5.6-terra,
#   gpt-5.6-luna) that LiteLLM published since the last sync — flagged by
#   `pricing-check` as missing_from_us (gpt-5.6-sol had real usage falling back
#   to gpt-5). Exact model_prices_and_context_window.json values, each with the
#   above-272k tier (max_input_tokens 1050000).
#   2026-07-19: verified all tracked values and the complete openai-provider
#   gpt-5* model set against the live LiteLLM snapshot; no named table entries
#   were missing. Official OpenAI model pages were used as the vendor cross-check.
#   2026-07-31 (#441): OpenAI repriced two gpt-5.6 variants on 2026-07-30 —
#   gpt-5.6-terra $2.50/$15 -> $2.00/$12 per MTok, and gpt-5.6-luna $1.00/$6 ->
#   $0.20/$1.20 (an 80% cut) — which left our 2026-07-10 values stale on all six
#   cost fields each. Adopted the vendor's post-cut rates from
#   developers.openai.com/api/docs/pricing (standard + long-context tiers, all
#   twelve fields), which match the live LiteLLM snapshot exactly. This is NOT
#   the introductory-rate pattern PRICING_DRIFT_ALLOWLIST exists to suppress:
#   the vendor lists these as standard ongoing prices with no promotional or
#   expiring annotation, so the durable rate is the cut rate. gpt-5.6 and
#   gpt-5.6-sol were not repriced and are unchanged.
#   2026-08-25 (#643): added gpt-5.6-cyber at OpenAI's published $12.50 input /
#   $1.25 cached input / $75.00 output per MTok, with LiteLLM's above-272k tier
#   ($25.00 / $2.50 / $112.50 per MTok). The vendor page shows dashes in the
#   long-context columns, but max_input_tokens is 400,000, so a turn above the
#   272,000 threshold is reachable and pricing it at the base rate would be
#   knowingly wrong; LiteLLM's tier ratios match every other gpt-5.6 member.
#   Removed the duplicated gpt-5.6 card and aliased that identifier to
#   gpt-5.6-sol, which OpenAI lists it as an alias of. gpt-5.6-sol's own values
#   are UNCHANGED: LiteLLM now carries OpenAI's promotional $4/$20 per MTok
#   rate, which the vendor guarantees only "at least through November 21, 2026"
#   and publishes no successor for. That promotion is suppressed in
#   PRICING_DRIFT_ALLOWLIST with expires 2026-11-21 instead of being written
#   into this date-blind table, because roughly half the retained Sol rows
#   predate it. The accepted cost is that while the promotion runs, Sol
#   reporting is high by 25% on input and 50% on output.
#   2026-09-05 (#767): added gpt-6-astra at OpenAI's published $10.00 input /
#   $1.00 cached input / $50.00 output per MTok. Prompts above 272,000 input
#   tokens use the published $20.00 / $2.00 / $75.00 long-context card, and
#   Fast mode is 2x the applicable rates. Verified against OpenAI's model page
#   and the live LiteLLM snapshot; max_input_tokens is 922,000.
#   2026-09-22: added gpt-6-sol at OpenAI's published Standard $2.00 input /
#   $0.20 cached input / $10.00 output per MTok. Prompts above 272,000 input
#   tokens use $4.00 / $0.40 / $15.00; Fast mode is 2x the applicable rates.
#   Verified against OpenAI's model and pricing pages and the live LiteLLM
#   openai-provider card; max_input_tokens is 922,000.
#   2026-09-22: added gpt-6-luna at OpenAI's published Standard $0.10 input /
#   $0.01 cached input / $0.50 output per MTok; above 272K, $0.20 / $0.02 /
#   $0.75. Fast mode is 2x. OpenAI documents the Daybreak Red alias as the
#   existing gpt-5.6-cyber card.
#   2026-09-23: adopted LiteLLM's historical gpt-5.5-cyber OpenAI-provider
#   card at $12.50 input / $1.25 cached input / $75.00 output per MTok.
#   The current OpenAI table lists its 5.6 successor instead. LiteLLM does
#   not publish an above-272K rate for 5.5 Cyber, so do not infer that tier.
#
# Billing rules:
# - reasoning_output_tokens is billed at the *output* rate (matches
#   LiteLLM / upstream).
# - If cache_read_input_token_cost is absent for a model, we fall back to
#   input_cost_per_token / 4 (matches LiteLLM's documented fallback).
# - Above-272k tiered rates are applied per-turn (row), mirroring the Claude
#   pattern via a dedicated CODEX_TIERED_THRESHOLD.
CODEX_TIERED_THRESHOLD = 272_000

CODEX_MODEL_PRICING: dict[str, dict[str, Any]] = {
    "gpt-5": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5-codex": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5.1-codex": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5.1-codex-max": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5.1-codex-mini": {
        "input_cost_per_token": 2.5e-07,
        "cache_read_input_token_cost": 2.5e-08,
        "output_cost_per_token": 2e-06,
    },
    "gpt-5.2": {
        "input_cost_per_token": 1.75e-06,
        "cache_read_input_token_cost": 1.75e-07,
        "output_cost_per_token": 1.4e-05,
    },
    "gpt-5.2-codex": {
        "input_cost_per_token": 1.75e-06,
        "cache_read_input_token_cost": 1.75e-07,
        "output_cost_per_token": 1.4e-05,
    },
    "gpt-5.3-codex": {
        "input_cost_per_token": 1.75e-06,
        "cache_read_input_token_cost": 1.75e-07,
        "output_cost_per_token": 1.4e-05,
    },
    "gpt-5.3-codex-spark": {
        # Not in LiteLLM snapshot. Upstream emits isFallback: false with $0
        # billing contribution, so we mirror with an all-zero pricing entry
        # rather than the unknown-model warn-path.
        "input_cost_per_token": 0.0,
        "cache_read_input_token_cost": 0.0,
        "output_cost_per_token": 0.0,
    },
    "gpt-5.4": {
        # Source: LiteLLM model_prices_and_context_window.json (openai provider entry
        # for "gpt-5.4"). Also matches OpenAI's published /api/docs/pricing page
        # (input $2.50/M, cached $0.25/M, output $15.00/M; above-272k tier doubles
        # input/cache and 1.5x's output).
        "input_cost_per_token": 2.5e-06,
        "cache_read_input_token_cost": 2.5e-07,
        "output_cost_per_token": 1.5e-05,
        "input_cost_per_token_above_272k_tokens": 5e-06,
        "cache_read_input_token_cost_above_272k_tokens": 5e-07,
        "output_cost_per_token_above_272k_tokens": 2.25e-05,
    },
    "gpt-5.4-mini": {
        # Source: LiteLLM model_prices_and_context_window.json (openai provider
        # entry for "gpt-5.4-mini"). Matches OpenAI published pricing: input
        # $0.75/M, cached $0.075/M, output $4.50/M. No above-272k tier
        # (max_input_tokens in LiteLLM is 272000 — the ceiling, not a tier break).
        "input_cost_per_token": 7.5e-07,
        "cache_read_input_token_cost": 7.5e-08,
        "output_cost_per_token": 4.5e-06,
    },
    "gpt-5.5": {
        # Source: LiteLLM model_prices_and_context_window.json (announced
        # 2026-04-23). Input $5.00/M, cached $0.50/M, output $30.00/M. The
        # above-272k tier ($10.00/M / $1.00/M / $45.00/M) was published in the
        # 2026-05-30 sync (issue #123) and matches the dated alias below.
        "input_cost_per_token": 5e-06,
        "cache_read_input_token_cost": 5e-07,
        "output_cost_per_token": 3e-05,
        "input_cost_per_token_above_272k_tokens": 1e-05,
        "cache_read_input_token_cost_above_272k_tokens": 1e-06,
        "output_cost_per_token_above_272k_tokens": 4.5e-05,
    },
    # ── gpt-5.6 family (LiteLLM openai-provider entries) ──
    # Every member carries the above-272k tier, but the context windows differ:
    # -sol, -terra and -luna are max_input_tokens 922,000 and -cyber is 400,000.
    # gpt-5.6-sol keeps gpt-5.5's rate card from the 2026-07-10 sync and is
    # deliberately HELD there while OpenAI's promotion runs — see the
    # gpt-5.6-sol block in PRICING_DRIFT_ALLOWLIST (#643). -terra and -luna
    # carry OpenAI's 2026-07-30 post-cut rates (#441) and no longer track
    # gpt-5.4's card or any other model's. -cyber carries its own launch rates.
    # The bare `gpt-5.6` identifier is OpenAI's alias of -sol and has NO card
    # here; CODEX_MODEL_ALIASES resolves it.
    "gpt-5.6-sol": {
        "input_cost_per_token": 5e-06,
        "cache_read_input_token_cost": 5e-07,
        "output_cost_per_token": 3e-05,
        "input_cost_per_token_above_272k_tokens": 1e-05,
        "cache_read_input_token_cost_above_272k_tokens": 1e-06,
        "output_cost_per_token_above_272k_tokens": 4.5e-05,
    },
    "gpt-5.6-terra": {
        "input_cost_per_token": 2e-06,
        "cache_read_input_token_cost": 2e-07,
        "output_cost_per_token": 1.2e-05,
        "input_cost_per_token_above_272k_tokens": 4e-06,
        "cache_read_input_token_cost_above_272k_tokens": 4e-07,
        "output_cost_per_token_above_272k_tokens": 1.8e-05,
    },
    "gpt-5.6-luna": {
        "input_cost_per_token": 2e-07,
        "cache_read_input_token_cost": 2e-08,
        "output_cost_per_token": 1.2e-06,
        "input_cost_per_token_above_272k_tokens": 4e-07,
        "cache_read_input_token_cost_above_272k_tokens": 4e-08,
        "output_cost_per_token_above_272k_tokens": 1.8e-06,
    },
    # No cache_creation field: the Codex cost kernel never reads one, so
    # carrying LiteLLM's would only give `diff_pricing` a value to compare.
    "gpt-5.5-cyber": {
        # Historical OpenAI-provider card retained by LiteLLM; unlike the
        # 5.6 successor, no long-context fields are published for this ID.
        "input_cost_per_token": 1.25e-05,
        "cache_read_input_token_cost": 1.25e-06,
        "output_cost_per_token": 7.5e-05,
    },
    "gpt-5.6-cyber": {
        "input_cost_per_token": 1.25e-05,
        "cache_read_input_token_cost": 1.25e-06,
        "output_cost_per_token": 7.5e-05,
        "input_cost_per_token_above_272k_tokens": 2.5e-05,
        "cache_read_input_token_cost_above_272k_tokens": 2.5e-06,
        "output_cost_per_token_above_272k_tokens": 1.125e-04,
    },
    "gpt-6-astra": {
        # Source: https://developers.openai.com/api/docs/models/gpt-6-astra
        # Standard $10.00/M input, $1.00/M cached input, $50.00/M output;
        # above 272K input, rates are 2x input/cache and 1.5x output.
        "input_cost_per_token": 1e-05,
        "cache_read_input_token_cost": 1e-06,
        "output_cost_per_token": 5e-05,
        "input_cost_per_token_above_272k_tokens": 2e-05,
        "cache_read_input_token_cost_above_272k_tokens": 2e-06,
        "output_cost_per_token_above_272k_tokens": 7.5e-05,
    },
    "gpt-6-sol": {
        # Source: https://developers.openai.com/api/docs/models/gpt-6-sol
        # Standard $2.00/M input, $0.20/M cached input, $10.00/M output;
        # above 272K input, rates are 2x input/cache and 1.5x output.
        "input_cost_per_token": 2e-06,
        "cache_read_input_token_cost": 2e-07,
        "output_cost_per_token": 1e-05,
        "input_cost_per_token_above_272k_tokens": 4e-06,
        "cache_read_input_token_cost_above_272k_tokens": 4e-07,
        "output_cost_per_token_above_272k_tokens": 1.5e-05,
    },
    "gpt-6-luna": {
        # Source: https://developers.openai.com/api/docs/models/gpt-6-luna
        "input_cost_per_token": 1e-07,
        "cache_read_input_token_cost": 1e-08,
        "output_cost_per_token": 5e-07,
        "input_cost_per_token_above_272k_tokens": 2e-07,
        "cache_read_input_token_cost_above_272k_tokens": 2e-08,
        "output_cost_per_token_above_272k_tokens": 7.5e-07,
    },
    # ── Issue #123: full gpt-5.x LiteLLM sync (2026-05-30 snapshot) ──
    # Exact model_prices_and_context_window.json values for every
    # openai-provider gpt-5* model `pricing-check`'s scope flags but the
    # curated set above didn't carry (bare/dated/-chat/-pro/-mini/-nano/
    # -search variants). The four *-pro models omit cache_read upstream; we
    # set it to input_cost_per_token / 4 (the documented LiteLLM cache
    # fallback) so the table stays well-formed and cached-input turns price.
    "gpt-5-2025-08-07": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5-chat": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5-chat-latest": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5-mini": {
        "input_cost_per_token": 2.5e-07,
        "cache_read_input_token_cost": 2.5e-08,
        "output_cost_per_token": 2e-06,
    },
    "gpt-5-mini-2025-08-07": {
        "input_cost_per_token": 2.5e-07,
        "cache_read_input_token_cost": 2.5e-08,
        "output_cost_per_token": 2e-06,
    },
    "gpt-5-nano": {
        "input_cost_per_token": 5e-08,
        "cache_read_input_token_cost": 5e-09,
        "output_cost_per_token": 4e-07,
    },
    "gpt-5-nano-2025-08-07": {
        "input_cost_per_token": 5e-08,
        "cache_read_input_token_cost": 5e-09,
        "output_cost_per_token": 4e-07,
    },
    "gpt-5-pro": {
        # *-pro: LiteLLM omits cache_read; input/4 documented fallback.
        "input_cost_per_token": 1.5e-05,
        "cache_read_input_token_cost": 3.75e-06,
        "output_cost_per_token": 0.00012,
    },
    "gpt-5-pro-2025-10-06": {
        # *-pro: LiteLLM omits cache_read; input/4 documented fallback.
        "input_cost_per_token": 1.5e-05,
        "cache_read_input_token_cost": 3.75e-06,
        "output_cost_per_token": 0.00012,
    },
    "gpt-5-search-api": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5-search-api-2025-10-14": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5.1": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5.1-2025-11-13": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5.1-chat-latest": {
        "input_cost_per_token": 1.25e-06,
        "cache_read_input_token_cost": 1.25e-07,
        "output_cost_per_token": 1e-05,
    },
    "gpt-5.2-2025-12-11": {
        "input_cost_per_token": 1.75e-06,
        "cache_read_input_token_cost": 1.75e-07,
        "output_cost_per_token": 1.4e-05,
    },
    "gpt-5.2-chat-latest": {
        "input_cost_per_token": 1.75e-06,
        "cache_read_input_token_cost": 1.75e-07,
        "output_cost_per_token": 1.4e-05,
    },
    "gpt-5.2-pro": {
        # *-pro: LiteLLM omits cache_read; input/4 documented fallback.
        "input_cost_per_token": 2.1e-05,
        "cache_read_input_token_cost": 5.25e-06,
        "output_cost_per_token": 0.000168,
    },
    "gpt-5.2-pro-2025-12-11": {
        # *-pro: LiteLLM omits cache_read; input/4 documented fallback.
        "input_cost_per_token": 2.1e-05,
        "cache_read_input_token_cost": 5.25e-06,
        "output_cost_per_token": 0.000168,
    },
    "gpt-5.3-chat-latest": {
        "input_cost_per_token": 1.75e-06,
        "cache_read_input_token_cost": 1.75e-07,
        "output_cost_per_token": 1.4e-05,
    },
    "gpt-5.4-2026-03-05": {
        "input_cost_per_token": 2.5e-06,
        "cache_read_input_token_cost": 2.5e-07,
        "output_cost_per_token": 1.5e-05,
        "input_cost_per_token_above_272k_tokens": 5e-06,
        "cache_read_input_token_cost_above_272k_tokens": 5e-07,
        "output_cost_per_token_above_272k_tokens": 2.25e-05,
    },
    "gpt-5.4-mini-2026-03-17": {
        "input_cost_per_token": 7.5e-07,
        "cache_read_input_token_cost": 7.5e-08,
        "output_cost_per_token": 4.5e-06,
    },
    "gpt-5.4-nano": {
        "input_cost_per_token": 2e-07,
        "cache_read_input_token_cost": 2e-08,
        "output_cost_per_token": 1.25e-06,
    },
    "gpt-5.4-nano-2026-03-17": {
        "input_cost_per_token": 2e-07,
        "cache_read_input_token_cost": 2e-08,
        "output_cost_per_token": 1.25e-06,
    },
    "gpt-5.4-pro": {
        "input_cost_per_token": 3e-05,
        "cache_read_input_token_cost": 3e-06,
        "output_cost_per_token": 0.00018,
        "input_cost_per_token_above_272k_tokens": 6e-05,
        "cache_read_input_token_cost_above_272k_tokens": 6e-06,
        "output_cost_per_token_above_272k_tokens": 0.00027,
    },
    "gpt-5.4-pro-2026-03-05": {
        "input_cost_per_token": 3e-05,
        "cache_read_input_token_cost": 3e-06,
        "output_cost_per_token": 0.00018,
        "input_cost_per_token_above_272k_tokens": 6e-05,
        "cache_read_input_token_cost_above_272k_tokens": 6e-06,
        "output_cost_per_token_above_272k_tokens": 0.00027,
    },
    "gpt-5.5-2026-04-23": {
        "input_cost_per_token": 5e-06,
        "cache_read_input_token_cost": 5e-07,
        "output_cost_per_token": 3e-05,
        "input_cost_per_token_above_272k_tokens": 1e-05,
        "cache_read_input_token_cost_above_272k_tokens": 1e-06,
        "output_cost_per_token_above_272k_tokens": 4.5e-05,
    },
    "gpt-5.5-pro": {
        "input_cost_per_token": 3e-05,
        "cache_read_input_token_cost": 3e-06,
        "output_cost_per_token": 0.00018,
        "input_cost_per_token_above_272k_tokens": 6e-05,
        "cache_read_input_token_cost_above_272k_tokens": 6e-06,
        "output_cost_per_token_above_272k_tokens": 0.00027,
    },
    "gpt-5.5-pro-2026-04-23": {
        "input_cost_per_token": 3e-05,
        "cache_read_input_token_cost": 3e-06,
        "output_cost_per_token": 0.00018,
        "input_cost_per_token_above_272k_tokens": 6e-05,
        "cache_read_input_token_cost_above_272k_tokens": 6e-06,
        "output_cost_per_token_above_272k_tokens": 0.00027,
    },
}

_unknown_codex_model_warnings: set[str] = set()

# Upstream ccusage-codex maps unknown Codex model names to `gpt-5` pricing
# and marks them isFallback: true. We mirror that behavior so cost figures
# match what a user would see with `ccusage-codex` on the same JSONL data.
# Behavior matches LEGACY_FALLBACK_MODEL in upstream ccusage-codex — both
# tools fall back to gpt-5 pricing for unknown model names so output remains
# directly comparable.
CODEX_LEGACY_FALLBACK_MODEL = "gpt-5"

# Runtime identifiers without their own rate card normalize here so pricing
# drift retains one source of truth per card. OpenAI's live pricing page states
# that ``daybreak-blue-latest`` currently points to ``gpt-5.6-sol`` and inherits
# the underlying model's pricing; Codex emits the prefixed runtime identifier
# retained below. OpenAI's models page lists the bare ``gpt-5.6`` identifier as
# an alias of ``gpt-5.6-sol`` too, so it resolves here rather than duplicating
# Sol's rates (#643) — its intentional absence from CODEX_MODEL_PRICING is
# covered by a model-only PRICING_DRIFT_ALLOWLIST entry.
# ``codex-auto-review`` is the hidden Guardian model and maps to the model
# current when it appeared, covering every retained event observed for issue
# #535.
CODEX_MODEL_ALIASES: dict[str, str] = {
    "codex-auto-review": "gpt-5.5",
    "gpt-5.6": "gpt-5.6-sol",
    "gpt-daybreak-blue-latest": "gpt-5.6-sol",
    "gpt-daybreak-red-latest": "gpt-5.6-cyber",
}

# Per-model fast-tier price multipliers, ported from ryoppippi/ccusage
# fast-multiplier-overrides.json ("exact" map — Codex/gpt entries only; the
# upstream claude-opus-* entries are for ccusage's Claude adapter and never
# price Codex models). Any fast-tier model NOT listed falls back to
# CODEX_FAST_MULTIPLIER_FALLBACK — upstream's `fast_multiplier == 1.0 → 2.0`
# rule in adapter/codex/report.rs:calculate_codex_model_cost.
CODEX_FAST_MULTIPLIER_OVERRIDES: dict[str, float] = {
    "gpt-5.5": 2.5,
    "gpt-5.4": 2.0,
    "gpt-5.3-codex": 2.0,
}
CODEX_FAST_MULTIPLIER_FALLBACK = 2.0


def _codex_fast_multiplier(model: str) -> float:
    """Fast-tier price multiplier for a Codex model (standard tier = 1.0)."""
    snapshot = current_pricing_snapshot()
    return snapshot.fast_multipliers["codex"].get(
        _canonical_codex_model(model),
        snapshot.fast_multipliers["codex_fallback"],
    )


def _canonical_codex_model(model: str) -> str:
    """Return the priced model name for a known Codex runtime alias."""
    return current_pricing_snapshot().aliases.get(model, model)


def _codex_config_requests_fast_service_tier(content: str) -> bool:
    """True iff any line sets ``service_tier = "fast"|"priority"``.

    Naive line-scan ported from ryoppippi/ccusage adapter/codex/speed.rs
    (NOT a TOML parse): strip the trailing ``#``-comment, split on the first
    ``=``, the key must be exactly ``service_tier``, and the quote-stripped
    value must be ``fast`` or ``priority``. Matches a ``service_tier`` line in
    ANY table; ignores ``service_tier_override`` and substrings like
    ``"breakfast"``.
    """
    for line in content.splitlines():
        setting = line.split("#", 1)[0].strip()
        key, sep, value = setting.partition("=")
        if not sep or key.strip() != "service_tier":
            continue
        if value.strip().strip("\"'") in ("fast", "priority"):
            return True
    return False


def _resolve_codex_pricing(model: str) -> tuple[dict[str, Any] | None, bool]:
    """Return (pricing_dict, is_fallback).

    Returns (entry, False) when the model has a direct pricing entry or a known
    canonical alias. Returns (gpt-5-entry, True) when the model is unknown —
    matches upstream's LEGACY_FALLBACK_MODEL semantics. Returns (None, True)
    only if the fallback model itself is missing from the pricing dict
    (programming error; warn once).
    """
    snapshot = current_pricing_snapshot()
    direct = snapshot.codex_pricing.get(_canonical_codex_model(model))
    if direct is not None:
        return direct, False
    fallback = snapshot.codex_pricing.get(snapshot.fallback_model)
    return fallback, True


def _is_codex_fallback(model: str) -> bool:
    """True iff `model` would resolve via the LEGACY_FALLBACK_MODEL path."""
    return (_canonical_codex_model(model)
            not in current_pricing_snapshot().codex_pricing)


def _resolve_model_pricing(model: str, warn: bool = True) -> dict[str, Any] | None:
    """Look up pricing for a model name. Returns None if unknown.

    `warn=True` (default) emits a one-shot `[cost] unknown model` stderr warning
    on a miss — correct for cost computation. Detection-only callers (e.g. the
    doctor pricing-coverage scan, whose whole job is to find unpriced models)
    pass `warn=False` so they don't fire the cost-engine warning as a side
    effect, and don't poison `_unknown_model_warnings` (which would suppress a
    later genuine cost-path warning for the same model).
    """
    table = current_pricing_snapshot().claude_pricing
    pricing = table.get(model)
    if pricing is not None:
        return pricing
    stripped = _strip_anthropic_model_prefix(model)
    if stripped != model:
        pricing = table.get(stripped)
        if pricing is not None:
            return pricing
    if warn and model not in _unknown_model_warnings:
        _unknown_model_warnings.add(model)
        _eprint(f"[cost] unknown model, treating cost as $0: {model}")
    return None


def claude_usage_dict(*, cache_1h_tokens, speed, input_tokens=0, output_tokens=0,
                      cache_creation_tokens=0, cache_read_tokens=0, **extra) -> dict:
    """Canonical usage dict for `_calculate_entry_cost` (#195).

    `cache_1h_tokens` and `speed` are REQUIRED keywords: a site that forgets
    either raises
    TypeError instead of silently pricing every 1-hour cache write at the
    5-minute rate. Pass an explicit None for a genuinely unknown or synthetic
    split — that reads as a deliberate declaration at the call site.

    None OMITS the key entirely, which is the exact sentinel
    `_calculate_entry_cost` branches on for "price as before #195".
    """
    usage = {
        "input_tokens": input_tokens or 0,
        "output_tokens": output_tokens or 0,
        "cache_creation_input_tokens": cache_creation_tokens or 0,
        "cache_read_input_tokens": cache_read_tokens or 0,
    }
    if cache_1h_tokens is not None:
        usage["cache_creation_1h_input_tokens"] = int(cache_1h_tokens)
    if speed is not None:
        usage["speed"] = speed
    usage.update(extra)
    return usage


def _cache_create_cost(pricing: dict, flat: int, h_raw, tiered) -> float:
    """USD for one entry's cache-CREATION tokens, priced by TTL (#195).

    `flat` is the authoritative total (`cache_creation_input_tokens`); `h_raw`
    is the 1-hour portion or None when the split is unknown. The 5-minute
    quantity is the REMAINDER (flat - h), never the stored 5m column, so
    priced tokens always equal `cache_create_tokens` and a breakdown that
    disagrees with the flat total cannot bill tokens at $0.

    `tiered` is the caller's `_tiered` closure, reused verbatim for the
    h == 0 path so that path is byte-identical to pre-#195 behavior.
    """
    if flat <= 0:
        return tiered(flat, "cache_creation_input_token_cost",
                      "cache_creation_input_token_cost_above_200k_tokens")
    h = 0 if h_raw is None else max(0, min(int(h_raw), flat))
    if h == 0:
        # Split unknown (pre-#195 row / no breakdown) OR genuinely all-5m.
        # BOTH execute the pre-change expression VERBATIM. This early return
        # is the byte-stability guarantee, not an optimization: the
        # proportional form below is NOT float-identical to `_tiered` for a
        # model with no above-200k cache-write rate (#195 gate P1-2).
        return tiered(flat, "cache_creation_input_token_cost",
                      "cache_creation_input_token_cost_above_200k_tokens")

    snapshot = current_pricing_snapshot()
    threshold = snapshot.tier_thresholds["claude"]
    multiplier_1h = snapshot.cache_write_1h_multiplier
    below = min(flat, threshold)
    above = max(0, flat - threshold)
    frac = h / flat
    base = pricing.get("input_cost_per_token", 0.0)
    r1h = base * multiplier_1h
    r1h_200k = pricing.get("input_cost_per_token_above_200k_tokens", base) \
        * multiplier_1h
    r5m = pricing.get("cache_creation_input_token_cost", 0.0)
    r5m_200k = pricing.get("cache_creation_input_token_cost_above_200k_tokens", r5m)
    return ((below * frac) * r1h + (above * frac) * r1h_200k
            + (below * (1 - frac)) * r5m + (above * (1 - frac)) * r5m_200k)


def _calculate_entry_cost(
    model: str,
    usage: dict[str, Any],
    mode: str = "auto",
    cost_usd: float | None = None,
) -> float:
    """Calculate USD cost for a single API call entry."""
    if mode == "display":
        return cost_usd if cost_usd is not None else 0.0
    if mode == "auto" and cost_usd is not None:
        return cost_usd

    pricing = _resolve_model_pricing(model)
    if pricing is None:
        return 0.0

    def _tiered(tokens: int, base_key: str, tiered_key: str) -> float:
        base_rate = pricing.get(base_key, 0.0)
        tiered_rate = pricing.get(tiered_key)
        if tokens <= 0:
            return 0.0
        threshold = current_pricing_snapshot().tier_thresholds["claude"]
        if tokens > threshold and tiered_rate is not None:
            below = min(tokens, threshold)
            above = tokens - threshold
            return below * base_rate + above * tiered_rate
        return tokens * base_rate

    input_cost = _tiered(
        usage.get("input_tokens", 0),
        "input_cost_per_token",
        "input_cost_per_token_above_200k_tokens",
    )
    output_cost = _tiered(
        usage.get("output_tokens", 0),
        "output_cost_per_token",
        "output_cost_per_token_above_200k_tokens",
    )
    # `flat` is passed RAW, exactly as the pre-#195 `_tiered(...)` call did.
    # Coercing it here would be an unflagged behavior change in both
    # directions: `int()` truncates a fractional count, and it would newly
    # ACCEPT a numeric string that `_tiered`'s `tokens <= 0` used to reject.
    # `_cache_create_cost` needs no coercion — every use of `flat` is
    # arithmetic or comparison, and the `h_raw` side does its own `int()`
    # inside the clamp (which IS load-bearing there: `min(int(h_raw), flat)`
    # is what pins the 1h portion to whole tokens).
    cache_create_cost = _cache_create_cost(
        pricing,
        usage.get("cache_creation_input_tokens", 0),
        usage.get("cache_creation_1h_input_tokens"),
        _tiered,
    )
    cache_read_cost = _tiered(
        usage.get("cache_read_input_tokens", 0),
        "cache_read_input_token_cost",
        "cache_read_input_token_cost_above_200k_tokens",
    )
    total = input_cost + output_cost + cache_create_cost + cache_read_cost
    if usage.get("speed") == "fast":
        multiplier = _claude_fast_multiplier(model)
        if multiplier != 1.0:
            return total * multiplier
    return total


def _warn_unknown_codex_model(model: str) -> None:
    """One-shot stderr warning for a Codex model absent from the pricing dict."""
    if model in _unknown_codex_model_warnings:
        return
    _unknown_codex_model_warnings.add(model)
    _eprint(f"[codex] unknown model, using gpt-5 fallback pricing (isFallback=true): {model}")


def _calculate_codex_entry_cost(
    model: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
    speed: str = "standard",
) -> float:
    """Compute USD cost for one Codex `token_count` event.

    Token-field semantics (LiteLLM convention, matched by upstream ccusage-codex):
      - input_tokens INCLUDES cached_input_tokens (cached is a subset).
      - output_tokens INCLUDES reasoning_output_tokens (reasoning is a subset).
    Cost formula:
      non_cached_input = max(0, input_tokens - cached_input_tokens)
      cost = non_cached_input * input_cost_per_token
           + cached_input_tokens * cache_read_input_token_cost
           + output_tokens * output_cost_per_token
    The reasoning_output_tokens parameter is accepted for API stability but
    not used directly — its contribution is already billed inside output_tokens.

    A request with more than 272k inclusive input tokens uses the long-context
    rates for its entire request, including cached input and output. The
    corresponding _above_272k_tokens key must be present in the pricing entry.
    """
    del reasoning_output_tokens  # already billed inside output_tokens
    pricing, is_fallback = _resolve_codex_pricing(model)
    if pricing is None:
        # Only possible if CODEX_LEGACY_FALLBACK_MODEL itself is missing — treat as
        # $0 to avoid crashing; a programming error we want to notice.
        _warn_unknown_codex_model(model)
        return 0.0
    if is_fallback:
        _warn_unknown_codex_model(model)  # one-shot per unique model name

    long_context = input_tokens > current_pricing_snapshot().tier_thresholds["codex"]

    def _tiered(tokens: int, base_key: str, tiered_key: str) -> float:
        if tokens <= 0:
            return 0.0
        base_rate = pricing.get(base_key, 0.0)
        if not base_rate:
            return 0.0
        tiered_rate = pricing.get(tiered_key)
        rate = tiered_rate if long_context and tiered_rate is not None else base_rate
        return tokens * rate

    non_cached_input = max(0, input_tokens - cached_input_tokens)

    input_cost = _tiered(
        non_cached_input,
        "input_cost_per_token",
        "input_cost_per_token_above_272k_tokens",
    )
    cached_input_cost = _tiered(
        cached_input_tokens,
        "cache_read_input_token_cost",
        "cache_read_input_token_cost_above_272k_tokens",
    )
    output_cost = _tiered(
        output_tokens,
        "output_cost_per_token",
        "output_cost_per_token_above_272k_tokens",
    )
    base = input_cost + cached_input_cost + output_cost
    if speed == "fast":
        base *= _codex_fast_multiplier(model)
    return base


def _short_model_name(model: str) -> str:
    """Shorten model name for display: 'claude-opus-4-6' -> 'opus-4-6'."""
    name = model
    # Strip 'claude-' prefix
    if name.startswith("claude-"):
        name = name[len("claude-"):]
    # Strip date suffixes like '-20251001'
    if re.match(r".*-\d{8}$", name):
        name = name[:-9]
    return name


# ---------------------------------------------------------------------------
# #714 — the published pricing snapshot, and the reader that replaces it
#
# Every CLI invocation re-imports this module, so only a long-lived process —
# the dashboard — can hold pricing an installer has already replaced on disk.
# Two rules shape everything below.
#
# A PARTIAL SWAP IS WORSE THAN NO SWAP. `PRICING_SNAPSHOT_DATE` is what gates
# the materialized-cost write refusal in `_cctally_cache`, so replacing the
# date alone clears the refusal and resumes writing cost computed from the old
# tables. A candidate is therefore validated whole and adopted whole, or it is
# rejected and the live snapshot is retained unchanged.
#
# NEVER `importlib.reload`. It executes arbitrary module code from a file an
# installer has just written; it mutates a live module dict incrementally, so a
# request in flight can see half a table; and it would not even fix the bug,
# because reloading this module rebinds none of the import-time copies listed
# in `PRICING_EXPORT_BINDINGS`. `ast.literal_eval` cannot execute code.


class PricingCandidateRejected(Exception):
    """A candidate pricing file was not adopted. Carries the reason."""


class PricingSnapshot:
    """One complete, immutable pricing revision.

    Every pricing-affecting value lives here — not only the two tables. A
    tier threshold, the Codex fallback model, an alias or a fast multiplier
    left behind by a partial swap prices part of an entry from the previous
    revision, which is the same defect as a stale table and harder to see.

    Hand-written frozen rather than ``@dataclasses.dataclass(frozen=True)``,
    for the reason `PricingFingerprintObservation` above states at length:
    this file declares ``from __future__ import annotations``, `dataclasses`
    resolves a string annotation through
    ``sys.modules.get(cls.__module__).__dict__``, and several callers load
    this module by executing the file under a private name that is never
    registered in ``sys.modules``. For those the lookup returns None and the
    class body raises ``AttributeError`` at import.
    """

    __slots__ = ("snapshot_date", "claude_pricing", "codex_pricing",
                 "aliases", "tier_thresholds", "fallback_model",
                 "cache_write_1h_multiplier", "fast_multipliers")

    def __init__(self, *, snapshot_date, claude_pricing, codex_pricing,
                 aliases, tier_thresholds, fallback_model,
                 cache_write_1h_multiplier, fast_multipliers):
        for name, value in (
            ("snapshot_date", snapshot_date),
            ("claude_pricing", claude_pricing),
            ("codex_pricing", codex_pricing),
            ("aliases", aliases),
            ("tier_thresholds", tier_thresholds),
            ("fallback_model", fallback_model),
            ("cache_write_1h_multiplier", cache_write_1h_multiplier),
            ("fast_multipliers", fast_multipliers),
        ):
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise dataclasses.FrozenInstanceError(
            f"cannot assign to field {name!r}")

    def __delattr__(self, name):
        raise dataclasses.FrozenInstanceError(
            f"cannot delete field {name!r}")

    def __repr__(self):
        return (f"PricingSnapshot(snapshot_date={self.snapshot_date!r}, "
                f"claude_models={len(self.claude_pricing)}, "
                f"codex_models={len(self.codex_pricing)})")


#: The complete inventory of import-time copies, made explicit so it can be
#: tested rather than asserted. `(module name, attribute, field accessor)`.
#: `bin/cctally` copies the tables at import and both dashboard source modules
#: read those copies, so a swap that missed one would price from a stale table
#: with the refusal latch already cleared.
PRICING_EXPORT_BINDINGS = (
    ("_lib_pricing", "PRICING_SNAPSHOT_DATE", lambda s: s.snapshot_date),
    ("_lib_pricing", "CLAUDE_MODEL_PRICING", lambda s: s.claude_pricing),
    ("_lib_pricing", "CODEX_MODEL_PRICING", lambda s: s.codex_pricing),
    ("_lib_pricing", "CODEX_MODEL_ALIASES", lambda s: s.aliases),
    ("_lib_pricing", "TIERED_THRESHOLD",
     lambda s: s.tier_thresholds["claude"]),
    ("_lib_pricing", "CODEX_TIERED_THRESHOLD",
     lambda s: s.tier_thresholds["codex"]),
    ("_lib_pricing", "CODEX_LEGACY_FALLBACK_MODEL", lambda s: s.fallback_model),
    ("_lib_pricing", "CACHE_WRITE_1H_MULTIPLIER",
     lambda s: s.cache_write_1h_multiplier),
    ("_lib_pricing", "CLAUDE_FAST_MULTIPLIER_OVERRIDES",
     lambda s: s.fast_multipliers["claude"]),
    ("_lib_pricing", "CODEX_FAST_MULTIPLIER_OVERRIDES",
     lambda s: s.fast_multipliers["codex"]),
    ("_lib_pricing", "CODEX_FAST_MULTIPLIER_FALLBACK",
     lambda s: s.fast_multipliers["codex_fallback"]),
    ("cctally", "PRICING_SNAPSHOT_DATE", lambda s: s.snapshot_date),
    ("cctally", "CLAUDE_MODEL_PRICING", lambda s: s.claude_pricing),
    ("cctally", "CODEX_MODEL_PRICING", lambda s: s.codex_pricing),
    ("cctally", "TIERED_THRESHOLD", lambda s: s.tier_thresholds["claude"]),
    ("cctally", "CODEX_TIERED_THRESHOLD",
     lambda s: s.tier_thresholds["codex"]),
    ("cctally", "CODEX_LEGACY_FALLBACK_MODEL", lambda s: s.fallback_model),
    ("cctally", "CACHE_WRITE_1H_MULTIPLIER",
     lambda s: s.cache_write_1h_multiplier),
    ("cctally", "CLAUDE_FAST_MULTIPLIER_OVERRIDES",
     lambda s: s.fast_multipliers["claude"]),
    ("cctally", "CODEX_FAST_MULTIPLIER_OVERRIDES",
     lambda s: s.fast_multipliers["codex"]),
    ("cctally", "CODEX_FAST_MULTIPLIER_FALLBACK",
     lambda s: s.fast_multipliers["codex_fallback"]),
    # `_cctally_cache` binds the date as a module global on purpose, so a test
    # can monkeypatch it; that affordance is preserved and the binding is
    # simply refreshed here too.
    ("_cctally_cache", "PRICING_SNAPSHOT_DATE", lambda s: s.snapshot_date),
    # `_lib_cache_report` unpacks the multiplier and the Claude tier threshold
    # at import. The threshold used to be a literal `200_000` in that file with
    # nothing keeping it equal to `TIERED_THRESHOLD` here, which made it a
    # pricing value no reload could replace; it is read from this module now
    # and refreshed through this entry.
    ("_lib_cache_report", "CACHE_WRITE_1H_MULTIPLIER",
     lambda s: s.cache_write_1h_multiplier),
    ("_lib_cache_report", "DEFAULT_TIERED_THRESHOLD",
     lambda s: s.tier_thresholds["claude"]),
)

#: The names `read_pricing_snapshot_from_source` requires, each mapped to the
#: `PricingSnapshot` field it fills. A missing OR non-literal name rejects the
#: whole candidate.
_CANDIDATE_ASSIGNMENTS = (
    "PRICING_SNAPSHOT_DATE",
    "CLAUDE_MODEL_PRICING",
    "CODEX_MODEL_PRICING",
    "CODEX_MODEL_ALIASES",
    "TIERED_THRESHOLD",
    "CODEX_TIERED_THRESHOLD",
    "CODEX_LEGACY_FALLBACK_MODEL",
    "CACHE_WRITE_1H_MULTIPLIER",
    "CLAUDE_FAST_MULTIPLIER_OVERRIDES",
    "CODEX_FAST_MULTIPLIER_OVERRIDES",
    "CODEX_FAST_MULTIPLIER_FALLBACK",
)

_PRICING_PUBLISH_LOCK = _threading.Lock()
_PRICING_CONTEXT = _contextvars.ContextVar("cctally_pricing_snapshot",
                                           default=None)


def _snapshot_from_module_globals() -> PricingSnapshot:
    return PricingSnapshot(
        snapshot_date=PRICING_SNAPSHOT_DATE,
        claude_pricing=CLAUDE_MODEL_PRICING,
        codex_pricing=CODEX_MODEL_PRICING,
        aliases=CODEX_MODEL_ALIASES,
        tier_thresholds={"claude": TIERED_THRESHOLD,
                         "codex": CODEX_TIERED_THRESHOLD},
        fallback_model=CODEX_LEGACY_FALLBACK_MODEL,
        cache_write_1h_multiplier=CACHE_WRITE_1H_MULTIPLIER,
        fast_multipliers={
            "claude": CLAUDE_FAST_MULTIPLIER_OVERRIDES,
            "codex": CODEX_FAST_MULTIPLIER_OVERRIDES,
            "codex_fallback": CODEX_FAST_MULTIPLIER_FALLBACK,
        },
    )


_LIVE_PRICING_SNAPSHOT = _snapshot_from_module_globals()


def current_pricing_snapshot() -> PricingSnapshot:
    """The snapshot this caller must price from.

    A caller inside `pricing_snapshot_context()` keeps the revision it entered
    with, so a request, a dashboard snapshot build or a sync pass already
    under way is never assembled from two revisions. Everyone else reads the
    live pointer, which is one immutable object replaced by one assignment.
    """
    captured = _PRICING_CONTEXT.get()
    return _LIVE_PRICING_SNAPSHOT if captured is None else captured


@_contextlib.contextmanager
def pricing_snapshot_context(snapshot: "PricingSnapshot | None" = None):
    """Pin one revision for the duration of a request, build or sync pass."""
    pinned = current_pricing_snapshot() if snapshot is None else snapshot
    token = _PRICING_CONTEXT.set(pinned)
    try:
        yield pinned
    finally:
        _PRICING_CONTEXT.reset(token)


def publish_pricing_snapshot(snapshot: PricingSnapshot) -> None:
    """Swap the live pointer and refresh every import-time copy.

    The pointer swap is one assignment of one frozen object, so a concurrent
    reader observes the old revision whole or the new one whole. The binding
    refresh that follows exists for call sites that still read a copied name;
    every cost kernel in this module reads `current_pricing_snapshot()`
    instead, so correctness does not depend on the refresh reaching a module
    that has not been imported.
    """
    global _LIVE_PRICING_SNAPSHOT
    with _PRICING_PUBLISH_LOCK:
        _LIVE_PRICING_SNAPSHOT = snapshot
        for module_name, attr, accessor in PRICING_EXPORT_BINDINGS:
            module = _sys.modules.get(module_name)
            if module is None:
                continue
            try:
                setattr(module, attr, accessor(snapshot))
            except Exception:  # noqa: BLE001
                # A module that refuses an attribute set must not stop the
                # rest of the inventory being refreshed.
                _eprint(f"[pricing] could not refresh {module_name}.{attr}")


def adopt_pricing_candidate(candidate: PricingSnapshot, *,
                            force: bool = False) -> bool:
    """Publish `candidate` iff it is strictly newer than the live snapshot.

    `_lib_pricing` documents that a pricing revision always ADVANCES
    `PRICING_SNAPSHOT_DATE`, so a candidate that does not is a rollback, a
    corrupt read or the same revision read twice; each of those is a reason to
    keep what is running. `force` exists for tests restoring a captured
    snapshot and for nothing else.
    """
    if force:
        publish_pricing_snapshot(candidate)
        return True
    live = _LIVE_PRICING_SNAPSHOT
    new_date = parse_pricing_fingerprint(candidate.snapshot_date)
    live_date = parse_pricing_fingerprint(live.snapshot_date)
    if new_date is None:
        return False
    if live_date is not None and new_date <= live_date:
        return False
    publish_pricing_snapshot(candidate)
    return True


def _file_signature(path) -> tuple:
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def read_pricing_snapshot_from_source(path) -> PricingSnapshot:
    """Build a candidate from the literal assignments in a pricing file.

    `ast.parse` plus `ast.literal_eval`, never `importlib.reload` and never
    `exec`: the file is one an installer has just written and this process
    must not run it.

    Raises `PricingCandidateRejected` — never returns a partial snapshot — on
    a missing name, a name whose value is not a literal, a date that does not
    parse, or a file whose inode/size/mtime signature changes while it is
    being read, which is an installer replacing it mid-parse.
    """
    import ast
    import pathlib as _pathlib

    path = _pathlib.Path(path)
    try:
        before = _file_signature(path)
        source = path.read_bytes()
        after = _file_signature(path)
    except OSError as exc:
        raise PricingCandidateRejected(
            f"pricing candidate could not be read: {exc}") from exc
    if before != after:
        raise PricingCandidateRejected(
            "pricing candidate signature changed while it was being read")

    try:
        tree = ast.parse(source.decode("utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise PricingCandidateRejected(
            f"pricing candidate does not parse: {exc}") from exc

    found: "dict[str, Any]" = {}
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                            ast.Name):
            targets = [node.target]
            value = node.value
        else:
            continue
        for target in targets:
            if target.id not in _CANDIDATE_ASSIGNMENTS or value is None:
                continue
            try:
                found[target.id] = ast.literal_eval(value)
            except (ValueError, SyntaxError, TypeError) as exc:
                raise PricingCandidateRejected(
                    f"{target.id} is no longer a literal, so the in-process "
                    f"pricing reload cannot read it: {exc}") from exc

    missing = [name for name in _CANDIDATE_ASSIGNMENTS if name not in found]
    if missing:
        raise PricingCandidateRejected(
            "pricing candidate is incomplete; adopting a partial revision "
            "would clear the write refusal and price from the old tables. "
            f"Missing: {', '.join(missing)}")

    if parse_pricing_fingerprint(found["PRICING_SNAPSHOT_DATE"]) is None:
        raise PricingCandidateRejected(
            "pricing candidate has no orderable PRICING_SNAPSHOT_DATE: "
            f"{found['PRICING_SNAPSHOT_DATE']!r}")

    return PricingSnapshot(
        snapshot_date=found["PRICING_SNAPSHOT_DATE"],
        claude_pricing=found["CLAUDE_MODEL_PRICING"],
        codex_pricing=found["CODEX_MODEL_PRICING"],
        aliases=found["CODEX_MODEL_ALIASES"],
        tier_thresholds={"claude": found["TIERED_THRESHOLD"],
                         "codex": found["CODEX_TIERED_THRESHOLD"]},
        fallback_model=found["CODEX_LEGACY_FALLBACK_MODEL"],
        cache_write_1h_multiplier=found["CACHE_WRITE_1H_MULTIPLIER"],
        fast_multipliers={
            "claude": found["CLAUDE_FAST_MULTIPLIER_OVERRIDES"],
            "codex": found["CODEX_FAST_MULTIPLIER_OVERRIDES"],
            "codex_fallback": found["CODEX_FAST_MULTIPLIER_FALLBACK"],
        },
    )
