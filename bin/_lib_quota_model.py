"""Pure-fn kernel for the Claude weekly quota model (#661 S1).

No I/O, no clock, no database, no printing, no environment read. The caller
supplies an explicit clock instant and already-parsed aware UTC datetimes;
this module only computes. It is driven by the glue in
`bin/_cctally_quota_model.py`.

The one sibling import is `_lib_pricing._strip_anthropic_model_prefix`, which
is itself a pure-fn layer with no I/O at import time.

Spec: docs/superpowers/specs/2026-08-28-661-s1-quota-model-and-calibration.md
(each part is normative over the parts before it; Part V is the current one).

There is deliberately **no budget constant and no per-family multiplier** in
this module. The fitted quantity is one effective blended weighted-units per
general-weekly point, valid only for the observed blend; it cancels out of
every ratio the detector forms, which is why no scale constant is needed.
"""
from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import enum
import hashlib
import json
import math
import re
import statistics

from _lib_pricing import _strip_anthropic_model_prefix

# ---------------------------------------------------------------------------
# Constants. Every value below is serialized into `constants_payload()` and
# hashed into QUOTA_MODEL_CONSTANTS_FINGERPRINT; a persisted calibration
# fitted under a different fingerprint is `stale`.
# ---------------------------------------------------------------------------

#: Weights in "fresh input token" equivalents. Calibrated on Opus-5-dominated
#: traffic; the five-minute cache-write quantity is weighted as fresh input.
TOKEN_CLASS_WEIGHTS: dict[str, float] = {
    "fresh": 1.0,
    "output": 4.73,
    "cache_1h": 1.03,
    "cache_read": 0.0031,
}

#: Recognized model spellings (case-folded, provider prefix stripped, anchored
#: terminal -YYYYMMDD removed) resolved to their canonical family. An
#: unrecognized spelling resolves to None — never to a default weight.
FAMILY_ALIASES: dict[str, str] = {
    # Opus
    "claude-opus-5": "claude-opus-5",
    "claude-opus-4-8": "claude-opus-4-8",
    "claude-opus-4-7": "claude-opus-4-7",
    "claude-opus-4-6": "claude-opus-4-6",
    "claude-opus-4-5": "claude-opus-4-5",
    "claude-opus-4-1": "claude-opus-4-1",
    "claude-opus-4": "claude-opus-4",
    "claude-4-opus": "claude-opus-4",
    "claude-3-opus": "claude-3-opus",
    "claude-3-opus-latest": "claude-3-opus",
    # Sonnet
    "claude-sonnet-5": "claude-sonnet-5",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-sonnet-4-5": "claude-sonnet-4-5",
    "claude-sonnet-4": "claude-sonnet-4",
    "claude-4-sonnet": "claude-sonnet-4",
    "claude-3-7-sonnet": "claude-3-7-sonnet",
    "claude-3-7-sonnet-latest": "claude-3-7-sonnet",
    "claude-3-5-sonnet": "claude-3-5-sonnet",
    "claude-3-5-sonnet-latest": "claude-3-5-sonnet",
    # Haiku
    "claude-haiku-4-5": "claude-haiku-4-5",
    "claude-3-5-haiku": "claude-3-5-haiku",
    "claude-3-5-haiku-latest": "claude-3-5-haiku",
    "claude-3-haiku": "claude-3-haiku",
    # Fable / Mythos. Each -1 point release is its OWN canonical family
    # (#704). Pooling it into the family it follows would assert that per-unit
    # consumption is EQUAL, which the operator's statement about the meter does
    # not establish, and it would make a Fable 5 to 5.1 substitution invisible
    # on the one axis built to notice it. A preview is a different case:
    # claude-mythos-preview stays pooled into claude-mythos-5, because a
    # preview of a model is not a successor to one.
    "claude-fable-5": "claude-fable-5",
    "claude-fable-5-1": "claude-fable-5-1",
    "claude-mythos-5": "claude-mythos-5",
    "claude-mythos-5-1": "claude-mythos-5-1",
    "claude-mythos-preview": "claude-mythos-5",
}

#: Whether a canonical family drains the general weekly quota, drains a
#: separate dedicated pool, or has unknown participation. A family absent
#: here, or classified "unknown", yields `unsupported-model-mix`.
FAMILY_PARTICIPATION: dict[str, str] = {
    "claude-opus-5": "general",
    "claude-opus-4-8": "general",
    "claude-opus-4-7": "general",
    "claude-opus-4-6": "general",
    "claude-opus-4-5": "general",
    "claude-opus-4-1": "general",
    "claude-opus-4": "general",
    "claude-3-opus": "general",
    "claude-sonnet-5": "general",
    "claude-sonnet-4-6": "general",
    "claude-sonnet-4-5": "general",
    "claude-sonnet-4": "general",
    "claude-3-7-sonnet": "general",
    "claude-3-5-sonnet": "general",
    "claude-haiku-4-5": "general",
    "claude-3-5-haiku": "general",
    "claude-3-haiku": "general",
    "claude-fable-5": "general",
    "claude-fable-5-1": "general",
    "claude-mythos-5": "general",
    "claude-mythos-5-1": "general",
}

#: Where each classification came from, so a later reader can re-test it
#: rather than inherit it. The Fable 5 and Mythos dual participations are
#: operator-supplied and are NOT derivable from this repository. No family is
#: ever classified "unknown" on the strength of unfamiliarity with it: an
#: unrecognized family is escalated to the operator instead.
FAMILY_PROVENANCE: dict[str, str] = {
    "claude-fable-5": (
        "operator-supplied 2026-08-28: drains a dedicated pool AND the "
        "general weekly meter; only the general draining is modelled"
    ),
    "claude-mythos-5": (
        "operator-supplied 2026-08-28: drains a dedicated pool AND the "
        "general weekly meter, on the same terms as claude-fable-5; only "
        "the general draining is modelled. Both spellings are real, priced "
        "families in CLAUDE_MODEL_PRICING launched alongside Fable 5"
    ),
    "claude-fable-5-1": (
        "operator-supplied 2026-09-01, dedicated-pool participation "
        "confirmed 2026-09-03: drains a dedicated pool AND the general "
        "weekly meter on the same terms as claude-fable-5, and both consume "
        "it quickly; only the general draining is modelled"
    ),
    "claude-mythos-5-1": (
        "operator-supplied 2026-09-03: drains a dedicated pool AND the "
        "general weekly meter on the same terms as claude-mythos-5; only "
        "the general draining is modelled"
    ),
}

#: Eligibility fence parameters (section 5).
ELIGIBILITY: dict[str, float] = {
    "fence_mads": 3.0,
}

#: Change-point detector parameters (sections 5 and 15). Four of the five
#: entries are ints and their int-ness is load-bearing: `json.dumps(64)` is
#: "64" and `json.dumps(64.0)` is "64.0", so widening one to a float moves the
#: fingerprint and marks every persisted calibration stale.
DETECTOR: dict[str, "int | float"] = {
    "min_watch_days": 3,
    "fence_mads": 3.0,
    "min_daily_gain": 2,
    "alpha": 0.01,
    "max_auto_scan_days": 64,
}

#: Trust gates (section 6). `min_fit_days` / `max_fit_width` gate a fit worth
#: predicting from; the `detect_*` entries additionally gate a rate-change
#: verdict; the `incomplete` entries cap incomplete-history contamination.
TRUST: dict[str, float] = {
    "min_fit_days": 5,
    "max_fit_width": 0.15,
    "min_detect_baseline_days": 14,
    "max_detect_baseline_width": 0.10,
    "max_detect_watch_width": 0.15,
    "max_incomplete_baseline_days": 1,
    "max_incomplete_baseline_fraction": 0.05,
}

#: Composition-support policy (sections 18 and 34). Two radii, computed
#: identically over the family-share and token-class-share vectors, and both
#: required. The support vectors are RAW quantity shares — coefficient-weighted
#: ones cannot carry the materiality floor below — and the radius that decides
#: is `max(empirical, floor)`, because an empirical radius collapses to exactly
#: zero whenever the reference population never varied, which is the ordinary
#: single-model case.
MIX_SUPPORT: dict[str, object] = {
    "fence_mads": 3.0,
    "distance": "total-variation",
    "centre": "componentwise-median-renormalized",
    "axes": ("family", "token_class"),
    "vector": "raw-quantity-share",
    "effective_radius": "max(empirical, floor)",
    "family_tv_floor": 0.03,
    "token_class_max_relative_effect": 0.05,
}

#: Producer policy that changes which retained requests the fitted model
#: represents. It is fingerprinted with the arithmetic because moving the era
#: floor or admitting another billing class changes the fitted answer just as
#: surely as editing a coefficient does.
COEFFICIENT_SUPPORT: dict[str, str] = {
    "supportedCompositionFrom": "2026-07-25",
    "fastModeParticipation": "usage-credits-only-excluded",
}

#: The reading-to-interval rule of section 4. Bumped when that rule changes.
DISPLAY_RULE_REVISION: int = 1


def max_fit_population_days() -> int:
    """Most recent eligible days a no-change fit may be computed over.

    Derived rather than stored, so the guard band cannot drift away from the
    scan horizon it is defined against. The band keeps history older than the
    earliest detectable split out of the prediction, which section 24 records
    as a correctness matter and not only a cost one: the cumulative
    all-history fit narrows to a 10% interval while its point estimate climbs
    2.6x and stays wrong.
    """
    return (int(DETECTOR["max_auto_scan_days"])
            - int(TRUST["min_detect_baseline_days"]))

#: The out-of-scope marker section 21 requires. Section 26 extends it from
#: Fable 5 to Mythos on the same terms: all three families drain a dedicated
#: pool AS WELL as the general weekly meter, and only the general draining is
#: modelled here.
DEDICATED_POOL_SCOPE_NOTE: str = (
    "out-of-scope: claude-fable-5, claude-fable-5-1, claude-mythos-5, "
    "claude-mythos-5-1 and claude-mythos-preview also drain dedicated pools; "
    "only general-quota draining is modelled"
)

QUOTA_MODEL_ALGORITHM_REVISION: int = 3
QUOTA_MODEL_VERIFIED_AT: str = "2026-08-29"


def constants_payload() -> dict:
    """Every constant this kernel's arithmetic depends on, as plain data.

    Serialized canonically and hashed into the committed fingerprint. It
    publishes no scale constant: there is no quota size, no promotional
    hypothesis and no weekly-to-five-hour ratio to publish.
    """
    return {
        "algorithmRevision": QUOTA_MODEL_ALGORITHM_REVISION,
        "verifiedAt": QUOTA_MODEL_VERIFIED_AT,
        "displayRuleRevision": DISPLAY_RULE_REVISION,
        "tokenClassWeights": dict(TOKEN_CLASS_WEIGHTS),
        "familyAliases": dict(FAMILY_ALIASES),
        "familyParticipation": dict(FAMILY_PARTICIPATION),
        "familyProvenance": dict(FAMILY_PROVENANCE),
        "eligibility": dict(ELIGIBILITY),
        "detector": dict(DETECTOR),
        "trust": dict(TRUST),
        "mixSupport": dict(MIX_SUPPORT),
        "coefficientSupport": dict(COEFFICIENT_SUPPORT),
    }


def constants_fingerprint() -> str:
    """SHA-256 over the canonical JSON of `constants_payload()`."""
    blob = json.dumps(
        constants_payload(), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(blob).hexdigest()


#: Committed literal. A test recomputes it and fails on drift, so editing any
#: constant above without re-pinning this line is caught rather than silently
#: invalidating every persisted calibration.
QUOTA_MODEL_CONSTANTS_FINGERPRINT: str = (
    "0cd932a7f8caa5d9bec7010ed5d800935e5c78332ae060c1142650943512a24a"
)


# ---------------------------------------------------------------------------
# Student-t quantiles.
#
# The standard library has no inverse-t. A bisection over an incomplete-beta
# implementation would make every interval depend on a tolerance, so the table
# is frozen here, generated once at the 0.975 level for one through one
# hundred degrees of freedom and pinned by a test against published values.
# ---------------------------------------------------------------------------
_T_QUANTILE_975: tuple[float, ...] = (
    12.706205, 4.302653, 3.182446, 2.776445, 2.570582,   # df 1-5
    2.446912, 2.364624, 2.306004, 2.262157, 2.228139,    # df 6-10
    2.200985, 2.178813, 2.160369, 2.144787, 2.131450,    # df 11-15
    2.119905, 2.109816, 2.100922, 2.093024, 2.085963,    # df 16-20
    2.079614, 2.073873, 2.068658, 2.063899, 2.059539,    # df 21-25
    2.055529, 2.051831, 2.048407, 2.045230, 2.042272,    # df 26-30
    2.039513, 2.036933, 2.034515, 2.032245, 2.030108,    # df 31-35
    2.028094, 2.026192, 2.024394, 2.022691, 2.021075,    # df 36-40
    2.019541, 2.018082, 2.016692, 2.015368, 2.014103,    # df 41-45
    2.012896, 2.011741, 2.010635, 2.009575, 2.008559,    # df 46-50
    2.007584, 2.006647, 2.005746, 2.004879, 2.004045,    # df 51-55
    2.003241, 2.002465, 2.001717, 2.000995, 2.000298,    # df 56-60
    1.999624, 1.998972, 1.998341, 1.997730, 1.997138,    # df 61-65
    1.996564, 1.996008, 1.995469, 1.994945, 1.994437,    # df 66-70
    1.993943, 1.993464, 1.992997, 1.992543, 1.992102,    # df 71-75
    1.991673, 1.991254, 1.990847, 1.990450, 1.990063,    # df 76-80
    1.989686, 1.989319, 1.988960, 1.988610, 1.988268,    # df 81-85
    1.987934, 1.987608, 1.987290, 1.986979, 1.986675,    # df 86-90
    1.986377, 1.986086, 1.985802, 1.985523, 1.985251,    # df 91-95
    1.984984, 1.984723, 1.984467, 1.984217, 1.983972,    # df 96-100
)


def t_quantile_975(df: int) -> float:
    """Two-sided 0.975 Student-t quantile at `df` degrees of freedom.

    Raises ValueError at or below zero, because a caller reaching this with
    df <= 0 has fewer than two observations and must withhold rather than
    quietly receive a normal quantile.
    """
    if df <= 0:
        raise ValueError(f"degrees of freedom must be positive, got {df!r}")
    if df <= len(_T_QUANTILE_975):
        return _T_QUANTILE_975[df - 1]
    return statistics.NormalDist().inv_cdf(0.975)


# ---------------------------------------------------------------------------
# The closed status, cause and verdict contract (section 19).
#
# Three separate closed enums, not one overloaded set: a top-level calibration
# status, a per-observation withholding cause, and the reported verdict.
# ---------------------------------------------------------------------------
class CalibrationStatus(str, enum.Enum):
    """Top-level state of one account's calibration."""

    OK = "ok"
    INSUFFICIENT_HISTORY = "insufficient-history"
    FRAGMENTED_HISTORY = "fragmented-history"
    UNSTABLE_FIT = "unstable-fit"
    LOCAL_HISTORY_INCOMPLETE = "local-history-incomplete"
    UNSUPPORTED_MODEL_MIX = "unsupported-model-mix"
    UNVALIDATED_COEFFICIENT_ERA = "unvalidated-coefficient-era"
    TOKEN_SPLIT_UNKNOWN = "token-split-unknown"
    STALE = "stale"
    FUTURE = "future"
    UNAVAILABLE = "unavailable"


class WithholdingCause(str, enum.Enum):
    """Why one daily observation carries no usable value.

    `NO_LOCAL_HISTORY` and `SPARSE_LOCAL_HISTORY` are separate absences with
    separate remedies and must never be merged: the first means the ingest
    tail does not cover the day or the interval holds no entries at all, the
    second means the interval holds positive units below the eligibility
    fence.
    """

    NO_LOCAL_HISTORY = "no-local-history"
    SPARSE_LOCAL_HISTORY = "sparse-local-history"
    TRANSITION_DAY = "transition-day"
    RIGHT_CENSORED = "right-censored"
    TOKEN_SPLIT_UNKNOWN = "token-split-unknown"
    UNSUPPORTED_COMPOSITION = "unsupported-composition"


class Verdict(str, enum.Enum):
    RATE_CHANGE_DETECTED = "rate-change-detected"
    NO_RATE_CHANGE = "no-rate-change"
    WITHHELD = "withheld"


class CompositionProvenance(str, enum.Enum):
    """Which probe produced `unsupported-model-mix` (#688 section 4).

    A FOURTH closed vocabulary. The status name reaches `classify` from five
    distinct origins whose meanings are opposite, and
    `_composition_unsupported` discarded which one applied by returning a
    bare boolean. `transition_persistence_permitted` is control flow over
    that distinction, so it must read a typed member rather than a string.

    `UNSUPPORTED_FAMILY_DAY` is the one origin `_composition_provenance` does
    not produce: it names a day whose model family does not participate in
    the general weekly pool, which `analyse` withholds under
    `WithholdingCause.UNSUPPORTED_COMPOSITION` and `classify` maps to the
    status. It is a member here so that a caller which does file it, and the
    permit predicate which must refuse it, both name the same value.

    Do NOT read that member as the route by which origin one is refused.
    `transition_persistence_permitted` refuses it on the CAUSE axis, by
    finding `WithholdingCause.UNSUPPORTED_COMPOSITION` among the published
    window's causes. The provenance axis describes only what the composition
    test itself decided, and the composition test never sees that day —
    `analyse` withholds it earlier.
    """

    UNSUPPORTED_FAMILY_DAY = "unsupported-family-day"
    DECISIVE_DAY = "decisive-day"
    FORECAST_AGGREGATE = "forecast-aggregate"
    UNDEFINED_REFERENCE = "undefined-reference"
    UNAGGREGATABLE_FORECAST = "unaggregatable-forecast"


class BlockingReason(str, enum.Enum):
    """Why the verdict is withheld although the status does not withhold it.

    A THIRD closed vocabulary, disjoint from `CalibrationStatus` and from
    `WithholdingCause` (section 38). Section 23 made status and verdict
    independent axes; a blocking reason is an input to the verdict axis alone,
    so the status keeps describing the published population's own health.

    `SPARSE_DAY_IN_DECISIVE_RUN` is section 5's rule that a day below the
    eligibility fence inside the decisive run must not relocate the change
    point. Filing it as `local-history-incomplete` told a user whose days are
    fully ingested that their local token history was incomplete, which is the
    merge section 27 forbids; borrowing any other status for it would be the
    same defect.
    """

    SPARSE_DAY_IN_DECISIVE_RUN = "sparse-day-in-decisive-run"


#: Total order, first match wins. A non-`ok` status always renders the verdict
#: as withheld, never as "no rate change".
STATUS_PRECEDENCE: tuple[CalibrationStatus, ...] = (
    CalibrationStatus.UNAVAILABLE,
    CalibrationStatus.FUTURE,
    CalibrationStatus.STALE,
    CalibrationStatus.TOKEN_SPLIT_UNKNOWN,
    CalibrationStatus.LOCAL_HISTORY_INCOMPLETE,
    CalibrationStatus.UNSUPPORTED_MODEL_MIX,
    CalibrationStatus.UNVALIDATED_COEFFICIENT_ERA,
    CalibrationStatus.INSUFFICIENT_HISTORY,
    CalibrationStatus.FRAGMENTED_HISTORY,
    CalibrationStatus.UNSTABLE_FIT,
    CalibrationStatus.OK,
)

#: Exit code per status. `0` trustworthy result, `3` judgment withheld because
#: the data is unhealthy or a store is unavailable, `4` evidence healthy but
#: too thin, fragmented or unstable to fit. `1` is the detector's, not a
#: status's, and `2` is argument errors only — no analysis outcome maps to it.
STATUS_EXIT: dict[CalibrationStatus, int] = {
    CalibrationStatus.OK: 0,
    CalibrationStatus.INSUFFICIENT_HISTORY: 4,
    CalibrationStatus.FRAGMENTED_HISTORY: 4,
    CalibrationStatus.UNSTABLE_FIT: 4,
    CalibrationStatus.LOCAL_HISTORY_INCOMPLETE: 3,
    CalibrationStatus.UNSUPPORTED_MODEL_MIX: 3,
    CalibrationStatus.UNVALIDATED_COEFFICIENT_ERA: 3,
    CalibrationStatus.TOKEN_SPLIT_UNKNOWN: 3,
    CalibrationStatus.STALE: 3,
    CalibrationStatus.FUTURE: 3,
    CalibrationStatus.UNAVAILABLE: 3,
}


#: Statuses that withhold the verdict whatever the detector found, and exit
#: 3. Section 23: status describes the current predictive calibration and the
#: verdict describes the detector, so the two are independently gated — but a
#: health failure this severe means the evidence underneath the detector
#: cannot be trusted either.
VERDICT_BLOCKING_STATUSES: tuple[CalibrationStatus, ...] = (
    CalibrationStatus.UNAVAILABLE,
    CalibrationStatus.FUTURE,
    CalibrationStatus.STALE,
    CalibrationStatus.TOKEN_SPLIT_UNKNOWN,
    CalibrationStatus.LOCAL_HISTORY_INCOMPLETE,
    CalibrationStatus.UNSUPPORTED_MODEL_MIX,
    CalibrationStatus.UNVALIDATED_COEFFICIENT_ERA,
)


#: The blocking statuses under which a QUALIFIED detector may still persist
#: its regimes and record a durable transition (#688). A permit set rather
#: than a refuse set, so a blocking status added later refuses by default.
#: Membership is necessary and not sufficient: the status name alone cannot
#: say which probe produced it, which is what `composition_provenance` is for.
TRANSITION_PERSISTENCE_PERMITTED_BLOCKING_STATUSES: tuple = (
    CalibrationStatus.UNSUPPORTED_MODEL_MIX,
)


def resolve_outcome(status: CalibrationStatus, *, change_detected: bool,
                    blocking=()) -> tuple["Verdict", int]:
    """The one `(verdict, exit)` answer for a `(status, detector, blocking)`.

    `blocking` is the `BlockingReason` members that withhold the verdict
    whatever the status and the detector say (section 38). It is accepted HERE
    rather than returned by `classify` because this function is already the
    sole authority that joins the two independent axes of section 23, and a
    blocking reason is an input to the verdict axis alone: `classify` resolves
    one status through `worst_status`, the glue merges its own statuses into
    that same call, and neither has a counterpart for a reason.

    Section 23 supersedes Part II's claim that the status determines the
    verdict and the exit code exhaustively. A confirmed change whose only
    non-`ok` condition is thin successor evidence still reports
    `rate-change-detected` at exit 1, because detection has independently
    satisfied its own stricter gates — fourteen baseline days at 10% width,
    three watch days at 15%, strict disjointness and a Holm-corrected p —
    and exit 1 is this repository's actionable-finding convention.

    Section 23 names that case for `insufficient-history` and ONLY for it.
    Section 33 reverts an earlier widening to `fragmented-history` and
    `unstable-fit`: those two say the published successor fit is untrustworthy
    or absent, whereas `insufficient-history` says a computed fit merely
    covers too few days. `classify` guarantees the distinction, because it
    reports `unstable-fit` whenever no fit could be computed at all, so
    `insufficient-history` here means exactly `fit is not None and
    len(fit_days) < TRUST["min_fit_days"]`.

    `STATUS_EXIT` remains the status-only mapping and is correct exactly when
    no change was detected; this function is the authority.
    """
    if status in VERDICT_BLOCKING_STATUSES:
        return Verdict.WITHHELD, 3
    if tuple(blocking):
        return Verdict.WITHHELD, 3
    if change_detected and status in (CalibrationStatus.OK,
                                      CalibrationStatus.INSUFFICIENT_HISTORY):
        return Verdict.RATE_CHANGE_DETECTED, 1
    if status is CalibrationStatus.OK:
        return Verdict.NO_RATE_CHANGE, 0
    return Verdict.WITHHELD, 4


def worst_status(statuses) -> CalibrationStatus:
    """The earliest member of STATUS_PRECEDENCE present in `statuses`.

    An empty argument is `ok`, so a caller with nothing to report does not
    have to special-case the call.
    """
    present = set(statuses)
    for candidate in STATUS_PRECEDENCE:
        if candidate in present:
            return candidate
    return CalibrationStatus.OK


# ---------------------------------------------------------------------------
# Evidence.
#
# CalibrationEvidence is a NEW named type and deliberately not
# `_lib_diagnosis.EvidenceField`, whose real shape is
# state / value / population / qualifications / code and which carries no
# interval and no support. This type follows the same discipline — a withheld
# value carries a typed code and no value — and adds `interval` and `support`.
# It carries NO confidence field at all: the design never defined what a
# confidence would mean here, and an undefined confidence is worse than none.
# ---------------------------------------------------------------------------
#: The closed union `CalibrationEvidence.code` draws from (section 22):
#: every `WithholdingCause` member plus every non-`ok` `CalibrationStatus`
#: member. An observation-level defect uses a cause; a calibration-level
#: trust failure uses the corresponding status, so an immature successor
#: carries `insufficient-history`. No seventh `WithholdingCause` is added.
EVIDENCE_CODES: frozenset = frozenset(
    [c.value for c in WithholdingCause]
    + [s.value for s in CalibrationStatus if s is not CalibrationStatus.OK]
)


@dataclasses.dataclass(frozen=True)
class Interval:
    lo: float
    hi: float | None


@dataclasses.dataclass(frozen=True)
class Support:
    days: int
    segments: int


@dataclasses.dataclass(frozen=True)
class CalibrationEvidence:
    state: str                       # "available" | "withheld"
    value: float | None
    interval: Interval | None
    support: Support | None
    population: dict[str, int]
    #: When withheld, a member of the closed union `EVIDENCE_CODES`: any
    #: `WithholdingCause` value, or any non-`ok` `CalibrationStatus` value.
    #: None when available.
    code: str | None = None
    qualifications: tuple[str, ...] = ()


def evidence_available(value, interval, support, population,
                       qualifications=()) -> CalibrationEvidence:
    return CalibrationEvidence(
        state="available", value=value, interval=interval, support=support,
        population=dict(population), qualifications=tuple(qualifications),
    )


def evidence_withheld(code, population,
                      qualifications=()) -> CalibrationEvidence:
    """`code` may be a `WithholdingCause`, a `CalibrationStatus`, or the
    plain string value of either.

    It is stored as a plain `str` either way, so a JSON renderer never has to
    know whether the caller happened to hold the enum member. A value outside
    `EVIDENCE_CODES` raises: the union is closed, and the shipped kernel's
    defect was populating this field from two enums with nothing specifying
    which applied.
    """
    value = str(code.value) if isinstance(code, enum.Enum) else str(code)
    if value not in EVIDENCE_CODES:
        raise ValueError(
            f"withholding code {value!r} is outside the closed union "
            f"EVIDENCE_CODES"
        )
    return CalibrationEvidence(
        state="withheld", value=None, interval=None, support=None,
        population=dict(population), qualifications=tuple(qualifications),
        code=value,
    )


# ---------------------------------------------------------------------------
# Model identity, participation, token weighting, and the ceiling rule.
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class EntryRecord:
    """One priced request, as the glue reads it out of `session_entries`.

    `cache_create_total` is the whole cache-write quantity and `cache_1h` the
    one-hour part of it, which is NULL on a store predating the split. The
    five-minute quantity is the remainder, never a separate column.
    """

    at: "dt.datetime"
    model: str
    fresh: int
    output: int
    cache_create_total: int
    cache_1h: int | None
    cache_read: int


#: An anchored terminal model date. Anchored and exactly eight digits, so
#: `claude-opus-5-2026072` is not silently truncated to a known family.
_DATED_SUFFIX = re.compile(r"-\d{8}$")


def normalize_family(model: str) -> str | None:
    """Resolve a raw model id to its canonical family, or None.

    Case-folds FIRST and strips the provider prefix second, because
    `_lib_pricing._strip_anthropic_model_prefix` matches only the lowercase
    `anthropic/` and `anthropic.` forms — folding after stripping would leave
    `ANTHROPIC/...` unstripped. Then removes an anchored terminal -YYYYMMDD
    and resolves through the explicit alias catalogue.

    Never substring matching, and never the display-only `_short_model_name`.
    An unrecognized family is None, not a default weight: the research
    script's `MODEL_WEIGHT_DEFAULT = 0.70` silently priced every unknown model
    and is exactly what this return value replaces.
    """
    if not model:
        return None
    folded = _strip_anthropic_model_prefix(model.casefold())
    candidate = _DATED_SUFFIX.sub("", folded)
    return FAMILY_ALIASES.get(candidate)


def family_participation(family: str | None) -> str:
    """"general" | "dedicated" | "unknown" for a canonical family.

    No entry in `FAMILY_PARTICIPATION` is classified "dedicated" and a test
    pins that, so no caller branches on the value: a family is either known to
    drain the general weekly meter or its participation is unknown, and an
    unknown one yields `unsupported-composition`. Fable 5 and Mythos DO drain
    dedicated pools as well, but they are classified "general" because they
    drain the general meter too and only that draining is modelled. A branch
    no input can reach is not a safeguard, so the value stays in this
    function's vocabulary and nowhere else until a purely dedicated family is
    actually classified.
    """
    if family is None:
        return "unknown"
    return FAMILY_PARTICIPATION.get(family, "unknown")


def _finite_quantity(value) -> bool:
    """True when a token quantity is a real, finite number.

    `float('nan')` passes every `<= 0` guard and fails every `>` guard, so
    without this it flows straight through the weighting and reaches the fit,
    where it produces a `status = ok` calibration whose every value is NaN.
    Positive infinity is the mirror case: it PASSES `> 0`.
    """
    try:
        return math.isfinite(value)
    except (TypeError, OverflowError):
        return False


def token_class_units(e: EntryRecord) -> dict[str, float] | None:
    """Raw token quantity per weighted class, or None when the split is unknown.

    None means `token-split-unknown` and the caller must withhold. It happens
    when the one-hour quantity is NULL AND the total cache-create quantity is
    positive: where the total is zero the one-hour contribution is
    unambiguously zero, so a NULL there is not ambiguous. `coalesce(..., 0)`
    would read "split unknown" as "zero one-hour writes", and the two classes
    carry different weights.

    It also means None when any quantity is not a finite number, and when the
    one-hour quantity exceeds the whole cache-write total. Neither is a usable
    split, and reporting both through this same return value keeps the day
    withheld with a typed cause without adding a seventh `WithholdingCause`
    member, which section 22 forbids.
    """
    for quantity in (e.fresh, e.output, e.cache_create_total, e.cache_read):
        if not _finite_quantity(quantity):
            return None
    if e.cache_1h is None:
        if e.cache_create_total > 0:
            return None
        one_hour = 0
    else:
        if not _finite_quantity(e.cache_1h):
            return None
        one_hour = e.cache_1h
    five_minute = e.cache_create_total - one_hour
    if five_minute < 0:
        # The five-minute quantity is the REMAINDER, so a one-hour part
        # larger than the whole cache-write total is malformed input
        # (section 39). Left to the arithmetic it becomes a negative fresh
        # quantity, and the raw quantity total can then reach zero while the
        # weighted total stays positive, because the one-hour class carries
        # 1.03 against the fresh remainder's 1.0.
        return None
    return {
        "fresh": float(e.fresh + five_minute),
        "output": float(e.output),
        "cache_1h": float(one_hour),
        "cache_read": float(e.cache_read),
    }


def weighted_units(e: EntryRecord) -> float | None:
    """Weighted quota units for one entry, or None when the split is unknown."""
    classes = token_class_units(e)
    if classes is None:
        return None
    return sum(qty * TOKEN_CLASS_WEIGHTS[name] for name, qty in classes.items())


def integer_percent(value: float) -> int:
    """`math.floor(value + 1e-9)`.

    `0.57 * 100` is 56.99999999999999, so a bare floor reads it as 56. Bare
    `int()` misread 5 of 22,987 rows on the measured store and produced one
    spurious segment fork.
    """
    return math.floor(value + 1e-9)


def true_percent_point(shown: int) -> float | None:
    """Unbiased point estimate of true consumption behind a displayed reading.

    A reading is the interval it denotes: 0 is [0, 0.5), 1 is [0.5, 1), k is
    [k-1, k), and 100 is [99, +inf). Midpoints are used only where the
    interval is bounded, so 100 returns None — it is right-censored, and
    returning 99.5 would invent a finite value the observation does not
    supply.
    """
    if shown >= 100:
        return None
    if shown <= 0:
        return 0.25
    if shown == 1:
        return 0.75
    return shown - 0.5


def true_percent_interval(shown: int) -> tuple[float, float | None]:
    """The half-open interval a displayed reading denotes.

    The upper bound is None at 100, which is the right-censored case.
    """
    if shown >= 100:
        return (99.0, None)
    if shown <= 0:
        return (0.0, 0.5)
    if shown == 1:
        return (0.5, 1.0)
    return (float(shown - 1), float(shown))


# ---------------------------------------------------------------------------
# Credit-aware segmentation and the daily observation contract (section 14).
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class SnapshotRecord:
    """One weekly-meter reading.

    `rowid` carries the store's row identity so the endpoint tie-break of
    section 14 is total; a caller that has none may leave it at zero, and the
    order then falls back to the instant and the percent alone.
    """

    at: "dt.datetime"
    week_start: "dt.datetime"
    percent: float
    source: str
    rowid: int = 0


@dataclasses.dataclass(frozen=True)
class CreditRecord:
    """An authoritative credit instant.

    `kind` is "reset" for a `week_reset_events` row, which re-anchors the
    logical week, or "floor" for a `weekly_credit_floors` row, which does not.
    Never inferred from a meter decrease.
    """

    at: "dt.datetime"
    kind: str


@dataclasses.dataclass(frozen=True)
class Segment:
    segment_id: int
    week_anchor: "dt.datetime"
    rows: tuple
    restarted: bool


@dataclasses.dataclass(frozen=True)
class DailyObservation:
    date: "dt.date"
    segment_id: int
    meter_delta: float
    units: float
    class_shares: dict
    family_shares: dict
    cause: WithholdingCause | None = None


def require_aware(value: "dt.datetime", label: str = "datetime"):
    """Return `value`, or raise `ValueError` when it carries no offset.

    A naive datetime reaching `astimezone()` is interpreted in the HOST's
    zone, so every UTC date bucket in this module would silently depend on
    the timezone the process happened to run under. The kernel accepts aware
    datetimes only and the glue parses.

    ANY offset is accepted, not only UTC: the measured store holds 9,749 rows
    at +02:00 and 3,497 at +03:00, and this module compares instants rather
    than wall clocks.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{label} must be an aware datetime, got naive {value!r}")
    return value


def canonical_week_anchor(value: "dt.datetime") -> "dt.datetime":
    """Round a week boundary to the nearest hour.

    Matches `_cctally_core._normalize_week_boundary_dt`: minutes 0-29 down,
    30-59 up. Nearest-hour is correct and hour-flooring is not — the measured
    spread runs 07:59 to 08:28, and flooring would separate 07:59 from its own
    week. Grouping by the raw value gave 125 segments where this gives 61.
    """
    normalized = require_aware(value, "week_start").replace(
        second=0, microsecond=0)
    if normalized.minute >= 30:
        return (normalized + dt.timedelta(hours=1)).replace(minute=0)
    return normalized.replace(minute=0)


def _snapshot_sort_key(row: SnapshotRecord):
    """Total, reproducible order: instant, then higher percent, then rowid.

    The percent leg uses the RAW stored value, not the floored integer.
    Flooring makes two rows at one instant reading 10.2 and 10.7 tie, so the
    higher-percent rule never applies and the order falls through to rowid.
    The rowid is the final total-order guarantee, not the second key.
    """
    return (row.at, -row.percent, row.rowid)


def build_segments(snapshots, credits) -> list[Segment]:
    """Cut the reading series into segments at authoritative boundaries only.

    A segment ends at a canonical week-anchor change or at a credit instant,
    and NEVER at a percentage decrease: the measured store holds 30 bare
    decreases against 10 authoritative credit instants, so two thirds of the
    research script's forks were unsupported.

    A "reset" credit opens a newly anchored logical week, because a >=25pp
    auto-credit re-anchors the week. A "floor" credit opens a new segment
    retaining the existing week identity, because `record-credit` deliberately
    does not re-anchor. When SEVERAL credits fall between two consecutive
    snapshots the last RESET among them wins, not simply the last credit: a
    reset followed by a floor is still a re-anchoring, and filing it as a
    floor would silently merge the two paths section 5 keeps distinct.

    The segment's published `week_anchor` and the value a week change is
    detected against are tracked separately. A reset anchors the successor
    from the CREDIT instant while the store's own rows carry their own
    spelling of the new boundary, and the two round to different hours
    whenever the credit straddles the half-hour — a credit at 03:29 with
    post-reset rows recording 03:31 gives 03:00 and 04:00. Comparing later
    rows against the credit-derived value forked a spurious third segment
    that also lost `restarted`.
    """
    for row in snapshots:
        require_aware(row.at, "snapshot.at")
    for credit in credits:
        require_aware(credit.at, "credit.at")
    rows = sorted(snapshots, key=_snapshot_sort_key)
    cuts = sorted(credits, key=lambda c: c.at)
    segments: list[Segment] = []
    current: list[SnapshotRecord] = []
    anchor = None
    anchor_ref = None
    restarted = False
    prev_at = None
    for row in rows:
        row_anchor = canonical_week_anchor(row.week_start)
        crossed = None
        if prev_at is not None:
            spanned = [c for c in cuts if prev_at < c.at <= row.at]
            if spanned:
                resets = [c for c in spanned if c.kind == "reset"]
                crossed = resets[-1] if resets else spanned[-1]
        if current and (row_anchor != anchor_ref or crossed is not None):
            segments.append(
                Segment(len(segments), anchor, tuple(current), restarted)
            )
            current = []
            if crossed is not None and crossed.kind == "reset":
                anchor = canonical_week_anchor(crossed.at)
                restarted = True
            else:
                anchor = row_anchor
                restarted = False
            anchor_ref = row_anchor
        elif not current:
            anchor = row_anchor
            anchor_ref = row_anchor
            restarted = False
        current.append(row)
        prev_at = row.at
    if current:
        segments.append(Segment(len(segments), anchor, tuple(current), restarted))
    return segments


def _day_end(date) -> "dt.datetime":
    """The first instant after `date`, in UTC."""
    return dt.datetime.combine(
        date + dt.timedelta(days=1), dt.time(0, 0), tzinfo=dt.timezone.utc
    )


def day_has_ended(date, *, now) -> bool:
    """The clock has passed the day's final instant.

    Section 29 keeps this apart from `day_ingest_covers`. "The day is not over
    yet" is a statement about time, not about local history: a day failing
    THIS condition is excluded from the series entirely, carries no
    withholding cause, and enters no health budget.
    """
    return require_aware(now, "now") >= _day_end(date)


def day_ingest_covers(date, *, newest_entry_at) -> bool:
    """The ingest tail reaches past the day's final instant.

    A day whose local token history has not finished ingesting looks exactly
    like a day the meter moved without local work, so it is a real
    local-history defect and keeps `no-local-history`.
    """
    if newest_entry_at is None:
        return False
    return require_aware(
        newest_entry_at, "newest_entry_at") >= _day_end(date)


def day_is_complete(date, *, now, newest_entry_at) -> bool:
    """Both conditions above hold.

    Kept as one predicate for callers that want the conjunction, but the
    series never uses it: the two conditions have different consequences and
    conflating them made the current in-progress day report
    `no-local-history`, which then withheld every verdict at exit 3.
    """
    return (day_has_ended(date, now=now)
            and day_ingest_covers(date, newest_entry_at=newest_entry_at))


@dataclasses.dataclass(frozen=True)
class _DaySlice:
    """One (segment, date) pair, classified exactly as the series classifies
    it: "transition", "in-progress", "right-censored", "below-gain" or
    "attributed".

    The single definition both `build_daily_series` and `unattributed_units`
    read, so the token windows the diagnostic complements cannot drift from
    the ones the series actually summed.
    """

    segment_id: int
    date: "dt.date"
    first_at: "dt.datetime | None"
    last_at: "dt.datetime | None"
    meter_delta: float
    kind: str


def _day_slices(segments, now):
    """Classify every (segment, date) pair against the caller's clock.

    `now` is required rather than optional, because both readers of these
    slices — the series and the unattributed-token diagnostic — must agree on
    which days contribute a token window. A day the clock has not passed
    contributes none, so its tokens belong to no observation.
    """
    require_aware(now, "now")
    min_gain = DETECTOR["min_daily_gain"]
    date_segment_count: dict = {}
    for seg in segments:
        for date in {r.at.astimezone(dt.timezone.utc).date()
                     for r in seg.rows}:
            date_segment_count[date] = date_segment_count.get(date, 0) + 1
    for seg in segments:
        by_day: dict = {}
        for row in seg.rows:
            by_day.setdefault(
                row.at.astimezone(dt.timezone.utc).date(), []
            ).append(row)
        for date in sorted(by_day):
            if date_segment_count.get(date, 0) > 1:
                yield _DaySlice(seg.segment_id, date, None, None, 0.0,
                                "transition")
                continue
            if not day_has_ended(date, now=now):
                yield _DaySlice(seg.segment_id, date, None, None, 0.0,
                                "in-progress")
                continue
            rows = sorted(by_day[date], key=_snapshot_sort_key)
            first, last = rows[0], rows[-1]
            start = true_percent_point(integer_percent(first.percent))
            end = true_percent_point(integer_percent(last.percent))
            if start is None or end is None:
                yield _DaySlice(seg.segment_id, date, first.at, last.at, 0.0,
                                "right-censored")
                continue
            meter_delta = end - start
            yield _DaySlice(
                seg.segment_id, date, first.at, last.at, meter_delta,
                "attributed" if meter_delta >= min_gain else "below-gain",
            )


def build_daily_series(segments, entries, *, now,
                       newest_entry_at) -> list[DailyObservation]:
    """One observation per complete UTC date, in date order.

    Within one day and one segment the observation runs from the first to the
    last reading by the total order above, meter movement comes from the
    interval midpoints, and units are summed over the HALF-OPEN interval
    `[first_instant, last_instant)` — an entry at exactly the closing instant
    belongs to the next observation.

    A date appearing in more than one segment is a transition day: it yields
    exactly ONE withheld row and never two. That is the deterministic form of
    "exclude the transition interval", and it also removes the duplicate-date
    defect, so no calendar date ever contributes two rows and the consecutive
    run counter cannot count one day twice.

    A day whose meter moved less than the minimum daily gain is ABSENT from
    the series rather than withheld, because "consecutive" counts consecutive
    eligible days in the series and a barely-moved day must not occupy a slot
    in it. Its tokens are then attributed to no observation, which is what
    `unattributed_units` reports.

    A day the clock has not passed is absent for the same reason and NOT
    withheld (section 29). Its meter has moved and its ingest is by definition
    unfinished, so reporting it as `no-local-history` asserts something false
    and, because it always falls on or after any split date, withheld every
    verdict at exit 3. `in_progress_dates` reports it as a diagnostic instead.
    """
    require_aware(now, "now")
    if newest_entry_at is not None:
        require_aware(newest_entry_at, "newest_entry_at")
    for entry in entries:
        require_aware(entry.at, "entry.at")
    ordered = sorted(entries, key=lambda e: e.at)
    instants = [e.at for e in ordered]

    out: list[DailyObservation] = []
    emitted_transition: set = set()
    for slice_ in _day_slices(segments, now):
        date = slice_.date
        segment_id = slice_.segment_id
        if slice_.kind == "in-progress":
            continue
        if slice_.kind == "transition":
            if date not in emitted_transition:
                emitted_transition.add(date)
                out.append(DailyObservation(
                    date, segment_id, 0.0, 0.0, {}, {},
                    WithholdingCause.TRANSITION_DAY,
                ))
            continue
        if slice_.kind == "right-censored":
            out.append(DailyObservation(
                date, segment_id, 0.0, 0.0, {}, {},
                WithholdingCause.RIGHT_CENSORED,
            ))
            continue
        if slice_.kind == "below-gain":
            continue

        lo = bisect.bisect_left(instants, slice_.first_at)
        hi = bisect.bisect_left(instants, slice_.last_at)
        # Two accumulators, because they answer different questions. `units`
        # is the WEIGHTED total the fit consumes; `quantities` is the RAW
        # token count per class, which is what the composition support vector
        # is (section 34) — a coefficient-weighted share cannot carry the
        # materiality floor derived from those same coefficients.
        raw = {name: 0.0 for name in TOKEN_CLASS_WEIGHTS}
        units = 0.0
        families: dict = {}
        split_unknown = False
        unsupported = False
        for entry in ordered[lo:hi]:
            family = normalize_family(entry.model)
            if family_participation(family) != "general":
                unsupported = True
                continue
            quantities = token_class_units(entry)
            if quantities is None:
                split_unknown = True
                continue
            entry_units = 0.0
            for name, qty in quantities.items():
                raw[name] += qty
                entry_units += qty * TOKEN_CLASS_WEIGHTS[name]
            units += entry_units
            families[family] = families.get(family, 0.0) + entry_units
        raw_total = sum(raw.values())

        # Cause precedence follows STATUS_PRECEDENCE: an unknown split
        # outranks incomplete history, which outranks an unsupported mix.
        if split_unknown or not _finite_quantity(units) \
                or not _finite_quantity(raw_total):
            cause = WithholdingCause.TOKEN_SPLIT_UNKNOWN
        elif not day_ingest_covers(date, newest_entry_at=newest_entry_at):
            cause = WithholdingCause.NO_LOCAL_HISTORY
        elif unsupported:
            cause = WithholdingCause.UNSUPPORTED_COMPOSITION
        elif units <= 0.0 or raw_total <= 0.0:
            cause = WithholdingCause.NO_LOCAL_HISTORY
        else:
            cause = None

        if cause is None:
            class_shares = {n: v / raw_total for n, v in raw.items()}
            family_shares = {f: v / units for f, v in families.items()}
        else:
            class_shares, family_shares = {}, {}
        out.append(DailyObservation(
            date, segment_id, slice_.meter_delta, units,
            class_shares, family_shares, cause,
        ))
    out.sort(key=lambda o: (o.date, o.segment_id))
    return out


def in_progress_dates(segments, *, now) -> tuple:
    """UTC dates the series dropped because the clock had not passed them.

    Section 29 requires this reported as the `inProgressDayExcluded`
    diagnostic. It is a separate entry point because `build_daily_series`
    returns observations and an excluded day is, by construction, not one.

    The return value is a sorted set of dates, which is the contract callers
    read: one entry per calendar date, in ascending order, whatever order the
    segments happened to arrive in.
    """
    return tuple(sorted({sl.date for sl in _day_slices(segments, now)
                         if sl.kind == "in-progress"}))


def unattributed_units(segments, entries, *, now) -> dict:
    """Weighted units and entry count on NO observation's token interval.

    Section 14 requires this diagnostic rather than a silent discard. An
    entry lands outside every interval when it precedes the day's first
    reading, follows its last, falls on a transition, in-progress or
    right-censored day, or falls on a day the meter barely moved — none of
    which contributes a row the series sums tokens over.

    `now` is required because the in-progress day is one of those cases, and
    this diagnostic reads the same `_day_slices` the series does so the two
    cannot drift apart.

    `entries` counts every such entry whatever its family. `units` sums only
    the general-quota entries whose token split is known, because that is the
    quantity the fit would have used; a dedicated-pool entry is reported as
    present and contributes nothing, exactly as it would inside an interval.
    """
    windows = sorted((s.first_at, s.last_at)
                     for s in _day_slices(segments, now)
                     if s.kind == "attributed")
    starts = [w[0] for w in windows]
    total = 0.0
    count = 0
    for entry in entries:
        require_aware(entry.at, "entry.at")
        index = bisect.bisect_right(starts, entry.at) - 1
        if index >= 0 and entry.at < windows[index][1]:
            continue
        count += 1
        if family_participation(normalize_family(entry.model)) != "general":
            continue
        units = weighted_units(entry)
        if units is not None:
            total += units
    return {"units": total, "entries": count}


# ---------------------------------------------------------------------------
# Robust statistics, the eligibility fence, and composition support (§5, §15).
# ---------------------------------------------------------------------------
def median(xs) -> float:
    """Median, NaN over an empty population rather than an exception."""
    values = list(xs)
    if not values:
        return float("nan")
    return statistics.median(values)


def scaled_mad(xs, med=None) -> float:
    """`1.4826 * median(|x - median(x)|)`.

    NaN below two observations, because a single point supplies no spread.
    A ZERO return is a real answer and means the population has no spread;
    the caller must treat the resulting fence as undefined rather than
    collapsing it to the median, which would flag every above-median day.
    """
    values = list(xs)
    if len(values) < 2:
        return float("nan")
    centre = median(values) if med is None else med
    return 1.4826 * median([abs(x - centre) for x in values])


def eligibility_fence(days) -> float | None:
    """Lower unit fence: `expm1(median(x) - 3 * scaledMAD(x))` over
    `x = log1p(weighted_units)` of the positive-unit candidate days.

    None when the fence is undefined — fewer than two positive days, or a
    zero scaled MAD. The caller then yields `unstable-fit`.

    This depends on no scale constant, which is the point. The research
    script's `modelled <= 0.5` gate was a function of its shipped quota size
    and misdiagnosed 4 of 4 blind days on the measured store, every one of
    which carried between 0.8 and 1.2 million weighted units against a
    message claiming no local token history at all.
    """
    xs = [math.log1p(u) for u in days if u is not None and u > 0.0]
    if len(xs) < 2:
        return None
    centre = median(xs)
    spread = scaled_mad(xs, centre)
    if not math.isfinite(spread) or spread == 0.0:
        return None
    return math.expm1(centre - ELIGIBILITY["fence_mads"] * spread)


def composition_centre(vectors) -> dict[str, float] | None:
    """Componentwise median of the share vectors, RENORMALIZED to sum to one.

    A componentwise median need not be a distribution — the medians of
    (0.6,0.4,0), (0.4,0,0.6) and (0,0.6,0.4) are (0.4,0.4,0.4) — so the
    normalization is required, not cosmetic. None when the componentwise
    median is all zeros; the caller then yields `unsupported-model-mix`.
    """
    rows = [dict(v) for v in vectors]
    if not rows:
        return None
    keys = sorted({k for row in rows for k in row})
    raw = {k: median([row.get(k, 0.0) for row in rows]) for k in keys}
    total = sum(raw.values())
    if not math.isfinite(total) or total <= 0.0:
        return None
    return {k: v / total for k, v in raw.items()}


def tv_distance(p, centre) -> float:
    """Total-variation distance, `0.5 * sum(|p_i - c_i|)`."""
    keys = set(p) | set(centre)
    return 0.5 * sum(abs(p.get(k, 0.0) - centre.get(k, 0.0)) for k in keys)


def support_radius(vectors, centre) -> float | None:
    """`median(distance) + 3 * scaledMAD(distance)` from `centre`.

    None when the radius is undefined. A hypothesis test was rejected for
    this role because failure to reject is not equivalence: at the measured
    three watch days the p-value was 0.314 on the only family carrying
    information.
    """
    if centre is None:
        return None
    distances = [tv_distance(dict(v), centre) for v in vectors]
    if len(distances) < 2:
        return None
    spread = scaled_mad(distances)
    if not math.isfinite(spread):
        return None
    return median(distances) + MIX_SUPPORT["fence_mads"] * spread


def token_class_floor(centre) -> float | None:
    """Materiality floor for the token-class radius, at `centre`.

    Derived, not asserted. For raw share distributions
    `|c.p - c.q| <= TV(p, q) * (cmax - cmin)`, so bounding the relative
    movement of the weighted-unit total at
    `MIX_SUPPORT["token_class_max_relative_effect"]` bounds the admissible
    total-variation distance at `effect * mu / spread`, where `mu` is the
    centre's own weighted value and `spread` is the coefficient range. At the
    shipped coefficients the spread is 4.73 - 0.0031 = 4.7269, so a fresh-only
    centre admits about 0.01058 — half the 10% detection-width gate and a
    third of the 15% prediction-width gate.

    None when the centre is undefined, which is a mix finding in its own right
    and never reaches here.
    """
    if centre is None:
        return None
    mu = sum(float(centre.get(name, 0.0)) * weight
             for name, weight in TOKEN_CLASS_WEIGHTS.items())
    spread = max(TOKEN_CLASS_WEIGHTS.values()) - min(TOKEN_CLASS_WEIGHTS.values())
    if spread <= 0.0 or not math.isfinite(mu) or mu <= 0.0:
        return None
    return float(MIX_SUPPORT["token_class_max_relative_effect"]) * mu / spread


def effective_radius(empirical, floor) -> float | None:
    """`max(empirical, floor)` — the radius that actually decides support.

    A ZERO empirical radius is a real answer and means the reference
    population never varied, which is the ordinary single-model case and the
    case the standard fixture produces. Treating it as undefined and skipping
    the test would admit arbitrarily large shifts, including the 0.79
    token-class attack section 25 requires be caught; treating it as a literal
    zero locked a user out for adding one percent of a second model. The floor
    is the materiality bound between the two.

    None only when the empirical radius is undefined, which means fewer than
    two reference observations.
    """
    if empirical is None:
        return None
    if floor is None or not math.isfinite(floor):
        return empirical
    return max(empirical, floor)


def aggregate_composition(entries) -> tuple | None:
    """One `(family_shares, class_shares)` pair over a set of entries.

    The forecast population is a set of entries rather than a set of days
    (section 35), so it needs its own aggregation. Family shares are by
    WEIGHTED units, matching `build_daily_series`; token-class shares are RAW
    quantity shares, matching section 34.

    None when no general-quota entry with a known token split contributes,
    because an empty population supports no comparison.
    """
    raw = {name: 0.0 for name in TOKEN_CLASS_WEIGHTS}
    families: dict = {}
    for entry in entries:
        family = normalize_family(entry.model)
        if family_participation(family) != "general":
            continue
        quantities = token_class_units(entry)
        if quantities is None:
            continue
        units = 0.0
        for name, qty in quantities.items():
            raw[name] += qty
            units += qty * TOKEN_CLASS_WEIGHTS[name]
        families[family] = families.get(family, 0.0) + units
    raw_total = sum(raw.values())
    unit_total = sum(families.values())
    if not _finite_quantity(raw_total) or not _finite_quantity(unit_total):
        return None
    if raw_total <= 0.0 or unit_total <= 0.0:
        return None
    return ({f: v / unit_total for f, v in families.items()},
            {n: v / raw_total for n, v in raw.items()})


def is_supported(family_shares, class_shares, family_centre, family_radius,
                 class_centre, class_radius) -> bool:
    """A day is supported only when it lies inside BOTH radii.

    The family radius alone is not enough: a workload can hold its family
    shares perfectly constant while shifting its output-to-cache composition
    enough to move the effective scale, and the token-class coefficients were
    themselves fitted on Opus-5-dominated traffic. An undefined centre or
    radius is unsupported, never "everything passes".
    """
    for shares, centre, radius in (
        (family_shares, family_centre, family_radius),
        (class_shares, class_centre, class_radius),
    ):
        if centre is None or radius is None:
            return False
        if tv_distance(dict(shares), centre) > radius:
            return False
    return True


# ---------------------------------------------------------------------------
# The fit (section 6 and 15).
# ---------------------------------------------------------------------------
def relative_width(interval, point) -> float | None:
    """`(hi - lo) / point`, or None when either end is undefined."""
    if interval is None or interval.hi is None or point is None or point <= 0:
        return None
    return (interval.hi - interval.lo) / point


def intervals_disjoint(a, b) -> bool:
    """STRICT disjointness: `a.lo > b.hi` or `b.lo > a.hi`.

    A non-strict comparison calls two touching intervals disjoint, which is
    one of the three conditions that manufacture a rate-change verdict.
    """
    if a is None or b is None or a.hi is None or b.hi is None:
        return False
    return a.lo > b.hi or b.lo > a.hi


def jackknife_fit(days) -> tuple[float, Interval] | None:
    """Effective blended weighted units per general-weekly point.

    The point estimate is total units over total meter movement, which is why
    any scale constant cancels. The interval is a delete-one day jackknife,
    `se = sqrt((n-1)/n * sum((v_i - mean)^2))`, using the two-sided Student-t
    quantile at n-1 degrees of freedom — NOT the research script's hardcoded
    1.96, which at the three watch days this model actually fits is 4.303 and
    so nearly doubles the half-width. The lower bound is floored at zero.

    None below two days, when any day carries a non-finite unit count or
    meter movement, or when the totals cannot support a ratio.
    """
    rows = list(days)
    n = len(rows)
    if n < 2:
        return None
    if any(not _finite_quantity(d.units) or not _finite_quantity(d.meter_delta)
           for d in rows):
        return None
    total_units = sum(d.units for d in rows)
    total_meter = sum(d.meter_delta for d in rows)
    if total_meter <= 0.0 or total_units <= 0.0:
        return None
    point = total_units / total_meter
    replicates = []
    for row in rows:
        rest_meter = total_meter - row.meter_delta
        if rest_meter <= 0.0:
            return None
        replicates.append((total_units - row.units) / rest_meter)
    mean = sum(replicates) / n
    se = math.sqrt((n - 1) / n * sum((v - mean) ** 2 for v in replicates))
    half = t_quantile_975(n - 1) * se
    return point, Interval(max(0.0, point - half), point + half)


# ---------------------------------------------------------------------------
# The exact conditional Wilcoxon rank-sum test (section 15).
# ---------------------------------------------------------------------------
def _mid_ranks(values) -> list[float]:
    """One-based ranks, tied values sharing their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j + 2) / 2.0
        for t in range(i, j + 1):
            ranks[order[t]] = average
        i = j + 1
    return ranks


def _subset_sum_counts(values, k) -> tuple[int, list[int]]:
    """Counts of achievable sums when choosing exactly `k` of `values`.

    Returns `(offset, counts)` where `counts[i]` is the number of k-subsets
    summing to `offset + i`. Dynamic programming over achievable sums, which
    is polynomial in the number of days, rather than enumeration of the
    C(n,k) arrangements, which is not.

    The sum axis is bounded per intermediate size by the k smallest and k
    largest values, which keeps the table proportional to the achievable
    range rather than to the grand total. The caller picks the smaller of the
    two groups, so `k` never exceeds half the population.
    """
    ordered = sorted(values)
    total_count = len(ordered)
    low = [0] * (k + 1)
    high = [0] * (k + 1)
    for j in range(1, k + 1):
        low[j] = low[j - 1] + ordered[j - 1]
        high[j] = high[j - 1] + ordered[total_count - j]
    table = [[0] * (high[j] - low[j] + 1) for j in range(k + 1)]
    table[0][0] = 1
    for taken, value in enumerate(values):
        upper = min(k, taken + 1)
        for j in range(upper, 0, -1):
            source = table[j - 1]
            target = table[j]
            base = low[j - 1] + value - low[j]
            for index, count in enumerate(source):
                if count:
                    target[base + index] += count
    return low[k], table[k]


def rank_sum_p(baseline, watch) -> float:
    """Two-sided exact conditional Wilcoxon rank-sum on mid-ranks.

    `p = min(1.0, 2 * min(P(W <= w), P(W >= w)))`. Mid-ranks are doubled so
    the null distribution is over integers, because a tie makes a rank a half
    integer.

    The test is symmetric in direction, so a rate change downward is detected
    exactly as a change upward is.
    """
    left = list(baseline)
    right = list(watch)
    n, m = len(left), len(right)
    if n == 0 or m == 0:
        return 1.0
    ranks = _mid_ranks(left + right)
    doubled = [int(round(r * 2)) for r in ranks]
    # The complement of a k-subset is an (N-k)-subset, and the two tails swap
    # under that map, so the two-sided p is unchanged by taking the smaller
    # group. Doing so halves the worst-case table.
    if m <= n:
        k = m
        observed = sum(doubled[n:])
    else:
        k = n
        observed = sum(doubled[:n])
    offset, counts = _subset_sum_counts(doubled, k)
    arrangements = sum(counts)
    if arrangements <= 0:
        return 1.0
    index = observed - offset
    at_or_below = sum(counts[:index + 1]) if index >= 0 else 0
    at_or_above = sum(counts[index:]) if index < len(counts) else 0
    tail = min(at_or_below, at_or_above) / arrangements
    return min(1.0, 2.0 * tail)


def holm(pvalues) -> list[float]:
    """Holm-Bonferroni adjusted p-values, returned in ascending order.

    Adjusted value `i` is `max` over the first `i` of `(m - j) * p_j`, capped
    at one, which is what makes the sequence monotone.
    """
    ordered = sorted(pvalues)
    size = len(ordered)
    running = 0.0
    out: list[float] = []
    for i, p in enumerate(ordered):
        running = max(running, min(1.0, (size - i) * p))
        out.append(running)
    return out


# ---------------------------------------------------------------------------
# The change-point detector (section 5).
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class DetectorResult:
    """What the scan found, and what it was allowed to look at.

    `baseline_days` and `watch_days` describe the winning split and are BOTH
    zero when no split qualified, so the type never implies a split that does
    not exist; the size of the population actually scanned is
    `scanned_eligible_days`.

    The truncation fields are disclosure, not a status: a bounded exact scan
    is still valid evidence, but concealing the bound would misstate both the
    days used and the Holm family size.
    """

    split_date: "dt.date | None"
    raw_p: float | None
    holm_p: float | None
    holm_family_size: int
    baseline_days: int
    watch_days: int
    longest_run: int
    qualified: bool
    input_eligible_days: int
    scanned_eligible_days: int
    truncated_eligible_days: int
    scan_start_date: "dt.date | None"
    max_auto_scan_days: int
    history_truncated: bool


def detector_diagnostics(detector: DetectorResult) -> dict:
    """`detector` as JSON-serializable camelCase, for `method.detector`.

    `dataclasses.asdict` cannot be used: it emits snake_case keys into an
    otherwise camelCase envelope, and it leaves `split_date` as a
    `datetime.date`, which `json.dumps` refuses.
    """
    return {
        "splitDate": (detector.split_date.isoformat()
                      if detector.split_date is not None else None),
        "rawP": detector.raw_p,
        "holmP": detector.holm_p,
        "holmFamilySize": detector.holm_family_size,
        "baselineDays": detector.baseline_days,
        "watchDays": detector.watch_days,
        "longestRun": detector.longest_run,
        "qualified": detector.qualified,
        "inputEligibleDays": detector.input_eligible_days,
        "scannedEligibleDays": detector.scanned_eligible_days,
        "truncatedEligibleDays": detector.truncated_eligible_days,
        "scanStartDate": (detector.scan_start_date.isoformat()
                          if detector.scan_start_date is not None else None),
        "maxAutoScanDays": detector.max_auto_scan_days,
        "historyTruncated": detector.history_truncated,
    }


def _longest_run_beyond(values, low_fence, high_fence) -> int:
    """Longest run of consecutive values beyond a fence in ONE direction.

    Both directions are measured and the larger returned, so a metering rate
    that fell is detected exactly as one that rose.
    """
    best = 0
    for fence, sign in ((high_fence, 1), (low_fence, -1)):
        if fence is None:
            continue
        run = 0
        for v in values:
            beyond = v > fence if sign > 0 else v < fence
            run = run + 1 if beyond else 0
            best = max(best, run)
    return best


def detect_change(series, *, override_split=None) -> DetectorResult:
    """Scan every admissible split for a change in the implied scale.

    The per-day statistic is `log(units / meter_delta)`, the log of that day's
    implied units-per-point, with no constant dividing it.

    The Holm family is EVERY admissible split — at least
    `TRUST["min_detect_baseline_days"]` baseline and
    `DETECTOR["min_watch_days"]` watch eligible days. The other qualification
    gates are applied AFTER Holm and never remove a member, because removing
    members post hoc would invalidate the correction.

    A split qualifies only when its Holm-adjusted p clears alpha, at least
    `min_watch_days` consecutive watch days lie beyond the baseline fence in
    the same direction, both fits meet their width gates, and the two
    intervals are strictly disjoint. Among qualifying splits the smallest raw
    p wins, ties breaking to the earliest date.

    `override_split` is the maintainer override. It bypasses the scan and the
    multiplicity correction ONLY — never the width, run or disjointness gates.
    It is also exempt from the scan horizon below, because it evaluates one
    operator-selected split rather than running a corrected scan.

    The automatic scan is bounded to the most recent
    `DETECTOR["max_auto_scan_days"]` eligible days. That retained set is the
    COMPLETE rank-sum population — mid-ranks, every admissible split and the
    Holm correction are all computed within it. Merely restricting the
    candidate split dates while leaving the population unbounded would not
    bound the cost, because the dynamic program over the population dominates
    it: measured at roughly O(N^5.5), an unbounded scan over the 201 eligible
    days this model was built from blocks for over six minutes. The horizon
    is fixed before any p-value is observed, so the correction stays valid.
    """
    days = [o for o in series
            if o.cause is None
            and math.isfinite(o.units) and o.units > 0.0
            and math.isfinite(o.meter_delta) and o.meter_delta > 0.0]
    days.sort(key=lambda o: o.date)
    input_days = len(days)
    horizon = int(DETECTOR["max_auto_scan_days"])
    if override_split is None and input_days > horizon:
        days = days[-horizon:]
    total = len(days)
    truncated = input_days - total
    scan_start = days[0].date if days else None
    stats = [math.log(o.units / o.meter_delta) for o in days]
    min_base = int(TRUST["min_detect_baseline_days"])
    min_watch = int(DETECTOR["min_watch_days"])
    fence_mads = DETECTOR["fence_mads"]
    alpha = DETECTOR["alpha"]

    def _result(split_date, raw_p, holm_p, family, baseline, watch, run,
                qualified):
        return DetectorResult(
            split_date=split_date, raw_p=raw_p, holm_p=holm_p,
            holm_family_size=family, baseline_days=baseline, watch_days=watch,
            longest_run=run, qualified=qualified,
            input_eligible_days=input_days, scanned_eligible_days=total,
            truncated_eligible_days=truncated, scan_start_date=scan_start,
            max_auto_scan_days=horizon, history_truncated=truncated > 0,
        )

    if override_split is not None:
        forced = [k for k in range(total) if days[k].date >= override_split]
        admissible = forced[:1] if forced else []
    else:
        admissible = list(range(min_base, total - min_watch + 1))

    if not admissible:
        return _result(None, None, None, len(admissible), 0, 0, 0, False)

    raw = [rank_sum_p(stats[:k], stats[k:]) for k in admissible]
    if override_split is None:
        order = sorted(range(len(raw)), key=lambda i: raw[i])
        adjusted_sorted = holm([raw[i] for i in order])
        adjusted = [0.0] * len(raw)
        for position, i in enumerate(order):
            adjusted[i] = adjusted_sorted[position]
    else:
        adjusted = list(raw)

    winner = None
    best_run = 0
    for i, k in enumerate(admissible):
        baseline, watch = days[:k], days[k:]
        if len(baseline) < min_base or len(watch) < min_watch:
            continue
        centre = median(stats[:k])
        spread = scaled_mad(stats[:k], centre)
        if math.isfinite(spread) and spread > 0.0:
            run = _longest_run_beyond(
                stats[k:], centre - fence_mads * spread,
                centre + fence_mads * spread,
            )
        else:
            run = 0
        best_run = max(best_run, run)
        baseline_fit = jackknife_fit(baseline)
        watch_fit = jackknife_fit(watch)
        if baseline_fit is None or watch_fit is None:
            continue
        baseline_width = relative_width(baseline_fit[1], baseline_fit[0])
        watch_width = relative_width(watch_fit[1], watch_fit[0])
        qualifies = (
            adjusted[i] <= alpha
            and run >= min_watch
            and baseline_width is not None
            and baseline_width <= TRUST["max_detect_baseline_width"]
            and watch_width is not None
            and watch_width <= TRUST["max_detect_watch_width"]
            and intervals_disjoint(baseline_fit[1], watch_fit[1])
        )
        if not qualifies:
            continue
        candidate = (raw[i], days[k].date, i, k, run)
        if winner is None or candidate[:2] < winner[:2]:
            winner = candidate
    if winner is None:
        return _result(None, None, None, len(admissible), 0, 0, best_run,
                       False)
    _, split_date, i, k, run = winner
    return _result(split_date, raw[i], adjusted[i], len(admissible), k,
                   total - k, run, True)


# ---------------------------------------------------------------------------
# The second pass: the adaptive unit floor, then the trust gates.
# ---------------------------------------------------------------------------
def apply_eligibility_fence(series, fence) -> list[DailyObservation]:
    """Mark positive-unit days below `fence` as `sparse-local-history`.

    `fence` of None leaves the series untouched, because an undefined fence
    is not a reason to withhold every day; the caller reports `unstable-fit`
    separately. Zero-unit days already carry `no-local-history` and are a
    different absence with a different remedy.
    """
    if fence is None:
        return list(series)
    out = []
    for o in series:
        if o.cause is None and 0.0 < o.units < fence:
            out.append(dataclasses.replace(
                o, cause=WithholdingCause.SPARSE_LOCAL_HISTORY,
            ))
        else:
            out.append(o)
    return out


def classify(series, fit, detector, *, fingerprint_matches, newest_at,
             now, fence, population=None) -> CalibrationStatus:
    """The single top-level status, resolved through `worst_status`.

    `series` is the PUBLISHED WINDOW, not all of retained history (section
    30). Every withholding-cause count that feeds a status decision is
    computed over it, because scanning all retained history makes one bad day
    anywhere permanent. Days outside the window are reported in diagnostics
    and decide nothing.

    The fit-driven gates are read from `population`, the day list `fit` was
    actually computed over, defaulting to every eligible day in the window.

    `fence` is the ONE eligibility floor of section 5, computed by the caller
    over the pre-watch prefix and passed in rather than recomputed here. An
    undefined fence means the population has no spread, which is
    `unstable-fit`.

    `unvalidated-coefficient-era` is deliberately NOT produced here: deciding
    that an era's composition has no coefficient validation needs an era
    catalogue the glue owns, so the glue combines its own finding with this
    one through `worst_status`.
    """
    require_aware(now, "now")
    if newest_at is not None:
        require_aware(newest_at, "newest_at")
    statuses: list[CalibrationStatus] = []
    if newest_at is not None and newest_at > now + dt.timedelta(seconds=60):
        statuses.append(CalibrationStatus.FUTURE)
    if not fingerprint_matches:
        statuses.append(CalibrationStatus.STALE)

    causes = {o.cause for o in series if o.cause is not None}
    if WithholdingCause.TOKEN_SPLIT_UNKNOWN in causes:
        statuses.append(CalibrationStatus.TOKEN_SPLIT_UNKNOWN)

    eligible = [o for o in series if o.cause is None]
    # The section 6 budget — one baseline day, at most 5%, zero inside the
    # decisive run — is the INCOMPLETE-HISTORY budget and counts
    # `no-local-history` alone. `sparse-local-history` is a different absence
    # with a different remedy (section 14): merging them told a user whose
    # days merely fall below the eligibility fence that the ingest tail does
    # not cover them.
    absent = [o for o in series
              if o.cause is WithholdingCause.NO_LOCAL_HISTORY]
    considered = len(eligible) + len(absent)
    over_budget = bool(absent) and (
        len(absent) > TRUST["max_incomplete_baseline_days"]
        or (considered
            and len(absent) / considered
            > TRUST["max_incomplete_baseline_fraction"])
    )
    inside_run = False
    if detector is not None and detector.qualified and detector.split_date:
        inside_run = any(o.date >= detector.split_date for o in absent)
    if over_budget or inside_run:
        statuses.append(CalibrationStatus.LOCAL_HISTORY_INCOMPLETE)

    if WithholdingCause.UNSUPPORTED_COMPOSITION in causes:
        statuses.append(CalibrationStatus.UNSUPPORTED_MODEL_MIX)

    fit_days = eligible if population is None else list(population)
    # Section 33: a fit that CANNOT be computed and a fit that exists over too
    # few days are different conditions. Reporting the former as a day-count
    # shortfall let a confirmed change reach exit 1 with no successor fit at
    # all, because section 23's exit-1 path names `insufficient-history`.
    if fit is None:
        statuses.append(CalibrationStatus.UNSTABLE_FIT)
    elif len(fit_days) < TRUST["min_fit_days"]:
        statuses.append(CalibrationStatus.INSUFFICIENT_HISTORY)
    else:
        point, interval = fit
        segment_ids = sorted({o.segment_id for o in fit_days})
        if len(segment_ids) > 1:
            for segment_id in segment_ids:
                rest = [o for o in fit_days if o.segment_id != segment_id]
                left_out = jackknife_fit(rest)
                if left_out is None or not (
                    interval.lo <= left_out[0] <= (interval.hi or math.inf)
                ):
                    statuses.append(CalibrationStatus.FRAGMENTED_HISTORY)
                    break
        width = relative_width(interval, point)
        if width is None or width > TRUST["max_fit_width"] or fence is None:
            statuses.append(CalibrationStatus.UNSTABLE_FIT)
    return worst_status(statuses)


def blocking_reasons(series, detector) -> tuple:
    """The `BlockingReason` members the published window carries.

    Section 31's rule, and section 38's home for it. Section 5 requires that a
    day below the eligibility fence inside the decisive run withhold the
    verdict rather than relocate the change point. That is a statement about
    the verdict, not about the health of the population the command publishes,
    so it produces a reason here and never a status in `classify`. The two
    rules therefore live in different functions and neither can be implemented
    as a side effect of the other.

    There is no decisive run without a confirmed split, so an unsplit series
    blocks nothing however sparse its days are.
    """
    if (detector is None or not detector.qualified
            or detector.split_date is None):
        return ()
    if any(o.cause is WithholdingCause.SPARSE_LOCAL_HISTORY
           and o.date >= detector.split_date for o in series):
        return (BlockingReason.SPARSE_DAY_IN_DECISIVE_RUN,)
    return ()


# ---------------------------------------------------------------------------
# Composition.
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class QuotaAnalysis:
    """One account's complete analysis.

    `fitted`, `consumption`, `projection` and `headroom` describe the
    SUCCESSOR regime alone after a confirmed change, never a value blended
    across the split, because a blended fit describes neither of the two
    regimes the persistence reducer creates. `baseline_fit` and `watch_fit`
    publish the two regimes as evidence in their own right.

    `status` and `verdict` are independent axes: the status describes the
    current predictive calibration, the verdict describes the detector, and a
    `BlockingReason` is a third input that withholds the verdict alone
    (section 38). `exit_code` is their joint resolution and is the value the
    command returns; reading an exit code from `STATUS_EXIT` alone is wrong
    whenever the detector confirmed a change or a reason blocked the verdict.
    Each blocking reason is published as a qualification on `fitted`,
    `consumption`, `projection` and `headroom`, so a renderer can state what
    happened rather than infer a remedy from a status that does not apply.
    """

    status: CalibrationStatus
    verdict: Verdict
    exit_code: int
    fitted: CalibrationEvidence
    consumption: CalibrationEvidence
    projection: CalibrationEvidence
    headroom: CalibrationEvidence
    baseline_fit: CalibrationEvidence
    watch_fit: CalibrationEvidence
    observed_percent: int | None
    #: The BASELINE composition centres, which the support radii are measured
    #: from, and the CURRENT ones over the published population. Sections 8
    #: and 21 require both pairs; publishing only the baseline's leaves a
    #: reader unable to see what actually moved.
    family_shares: dict
    class_shares: dict
    current_family_shares: dict
    current_class_shares: dict
    #: The EFFECTIVE radii of section 34, which are
    #: `max(empirical, floor)` -- NOT the measured spread. A single-model
    #: user's measured 0.0 appears here as the 0.03 family floor, so a
    #: renderer must not label these as observed quantities. The empirical
    #: values are published separately as `familyRadiusEmpirical` and
    #: `classRadiusEmpirical` in `diagnostics`.
    family_radius: float | None
    class_radius: float | None
    detector: DetectorResult
    #: Why the verdict was withheld while the status stayed healthy, per
    #: section 38. A closed vocabulary, so a renderer matches enum members
    #: rather than searching `qualifications`, which carries seven unrelated
    #: string vocabularies and would silently stop matching when a second
    #: reason is added.
    blocking: tuple
    diagnostics: dict
    #: The withholding causes present in the published window this analysis
    #: was classified over, and which probes inside the composition test
    #: fired (#688). Both are TYPED because `transition_persistence_permitted`
    #: is control flow: the status alone cannot distinguish a cause that
    #: removed days from the detector's own population from one that only
    #: bears on projecting forward. They are not diagnostics, and a consumer
    #: must never re-derive either from the `diagnostics` strings.
    #:
    #: `None` means an analysis built by a path that does not publish them.
    #: Every consumer must REFUSE on that rather than assume, because the
    #: harmful direction on this axis is permitting.
    detector_input_causes: frozenset | None = None
    composition_provenance: frozenset | None = None


def overlay_status(analysis: QuotaAnalysis,
                   contributed: CalibrationStatus) -> QuotaAnalysis:
    """Merge one late status without recomputing the exact detector.

    The glue uses this for a stale persisted prior. The prior affects
    publication only when the fresh fit is already non-trustworthy. Re-running
    ``analyse`` merely to add that status doubled the bounded scan on every
    post-upgrade thin history; replacing the already-withheld publication
    fields is equivalent and keeps the statistical pass single-shot.
    """
    status = worst_status((analysis.status, contributed))
    if status is analysis.status:
        return analysis
    if status is CalibrationStatus.OK:
        raise ValueError("a status overlay cannot improve an analysis")
    verdict, exit_code = resolve_outcome(
        status, change_detected=analysis.detector.qualified,
        blocking=analysis.blocking)

    def withheld(field):
        return evidence_withheld(status, field.population,
                                 field.qualifications)

    return dataclasses.replace(
        analysis, status=status, verdict=verdict, exit_code=exit_code,
        fitted=withheld(analysis.fitted),
        consumption=withheld(analysis.consumption),
        projection=withheld(analysis.projection),
        headroom=withheld(analysis.headroom),
    )


def transition_persistence_permitted(analysis) -> bool:
    """Whether a withheld analysis may still persist and record (#688).

    `reduce_state` derives its own `confirmed` from the VERDICT, and
    `resolve_outcome` forces the verdict to withheld for every blocking
    status and for any blocking reason. That made the durable history depend
    on a trustworthy fit while the documentation promised it from the first
    detection. This function is the exception, and it is deliberately narrow.

    `blocking` must be EMPTY. `resolve_outcome` withholds on a non-empty
    reason tuple independently of the status, so a sparse day in the decisive
    run combined with a forward-looking composition status satisfies every
    other condition here and would otherwise persist.

    Both typed fields must be PRESENT and well-formed. An analysis built by a
    path that does not publish them cannot be judged, and the harmful
    direction is permitting, so absence refuses. The member type checks are
    not defensive noise: `CompositionProvenance` is a `str` enum, so a
    frozenset of bare strings compares EQUAL to the permitted set and the
    final equality alone would admit it.

    `resolve_outcome` is not touched. It remains the sole authority over the
    verdict and the exit code, and this answers a separate question about
    persistence.

    The argument's own TYPE is checked first, which makes this predicate
    TOTAL over any input. Read that as a statement about this axis only, not
    as a claim to protect the command: `persist_and_detect` calls
    `reduce_state` BEFORE it reaches here, and `reduce_state` dereferences
    `analysis.verdict` unguarded, so a foreign object raises there first and
    the run fails anyway. What actually reaches this line with a stub is a
    test that monkeypatches `reduce_state` away. The check earns its place by
    keeping the harmful direction closed rather than by preventing a crash.

    One constraint follows from that check being an `isinstance`. It resolves
    `QuotaAnalysis` as THIS module generation defines it. Production has a
    single generation, because `_load_sibling` returns the `sys.modules`
    entry and the only production caller passes an analysis this same module
    produced. Under pytest, `conftest.load_script` can create a second
    generation, and an analysis built by one passed to the other's glue would
    be refused SILENTLY rather than raising. Keep a test's analysis and its
    glue on one generation.
    """
    if not isinstance(analysis, QuotaAnalysis):
        return False
    if analysis.verdict is not Verdict.WITHHELD:
        return False
    if tuple(analysis.blocking or ()):
        return False
    if analysis.status not in TRANSITION_PERSISTENCE_PERMITTED_BLOCKING_STATUSES:
        return False
    detector = analysis.detector
    if detector is None or not detector.qualified \
            or detector.split_date is None:
        return False
    causes = analysis.detector_input_causes
    if not isinstance(causes, frozenset) or not all(
            isinstance(c, WithholdingCause) for c in causes):
        return False
    if WithholdingCause.UNSUPPORTED_COMPOSITION in causes:
        return False
    provenance = analysis.composition_provenance
    if not isinstance(provenance, frozenset) or not all(
            isinstance(p, CompositionProvenance) for p in provenance):
        return False
    return provenance == frozenset({CompositionProvenance.FORECAST_AGGREGATE})


def _composition_provenance(*, confirmed, reference_days, decisive_days,
                            forecast_probed=False,
                            forecast_shares, family_centre, family_radius,
                            class_centre, class_radius) -> frozenset:
    """Apply section 18's two-radius test, which section 25 requires.

    Two probes, because section 18 names two populations:

    * every DECISIVE WATCH DAY, individually, after a confirmed change;
    * the FORECAST POPULATION, as one aggregate.

    The forecast population is the current-regime entries whose units become
    `consumption`, `projection` and `headroom` (section 35), supplied
    separately by the caller. Comparing the published population's own centre
    against the reference centre instead was dead in both branches: with no
    confirmed change the two are the same days, so the distance is identically
    zero, and after a change the per-day probe already covers those days more
    strictly.

    The forecast probe is an AGGREGATE rather than per-day. A radius of median
    plus three scaled MADs leaves an ordinary tail outside it by construction,
    so a per-day rule here would flip most healthy workloads to exit 3.

    An UNDEFINED CENTRE is unsupported (section 25). An undefined RADIUS is
    not: it means the reference population held fewer than two observations,
    which is thin evidence rather than a mix finding, and reporting it as one
    would move a thin population from exit 4 to exit 3.

    The return is TYPED PROVENANCE rather than a boolean (#688 section 4).
    An empty frozenset means supported; every other value names which of the
    probes above fired. The status decision is unchanged, because the caller
    branches on truthiness, but a boolean discarded the one distinction the
    persistence permit needs: the decisive-day probe guards DETECTION and the
    forecast probe guards PROJECTION, and they must be answered differently.
    """
    if not reference_days:
        return frozenset()
    if family_centre is None or class_centre is None:
        return frozenset({CompositionProvenance.UNDEFINED_REFERENCE})
    if family_radius is None or class_radius is None:
        return frozenset()
    # A population that WAS probed but could not be aggregated is unsupported,
    # not exempt. Every entry from an unrecognised family contributes no
    # units, so `aggregate_composition` returns None and the probe list would
    # otherwise stay empty -- disabling the section 18 test exactly as an
    # omitted `forecast_population` would, which is what section 40 made that
    # parameter required to prevent.
    if forecast_probed and forecast_shares is None:
        return frozenset({CompositionProvenance.UNAGGREGATABLE_FORECAST})
    found = set()
    for o in (decisive_days if confirmed else ()):
        if not is_supported(o.family_shares, o.class_shares, family_centre,
                            family_radius, class_centre, class_radius):
            found.add(CompositionProvenance.DECISIVE_DAY)
            break
    if forecast_shares is not None:
        forecast_family_shares, forecast_class_shares = forecast_shares
        if not is_supported(forecast_family_shares, forecast_class_shares,
                            family_centre, family_radius, class_centre,
                            class_radius):
            found.add(CompositionProvenance.FORECAST_AGGREGATE)
    return frozenset(found)


def analyse(series, *, now, newest_at, forecast_population,
            fingerprint_matches=True, observed_percent=None,
            current_week_units=None, current_week_start=None,
            current_week_end=None, override_split=None, extra_statuses=(),
            unattributed=None, in_progress_excluded=None,
            diagnostics=None) -> QuotaAnalysis:
    """Run both passes and return one account's complete analysis.

    Pass one chooses the split from budget-independent structural candidates.
    Pass two computes the adaptive unit floor from the selected pre-watch
    prefix — or from the whole regime when no split qualified — and applies
    the trust gates. The floor never feeds back into split selection, so a
    sparse day inside the decisive run withholds the verdict rather than
    relocating the change point.

    `extra_statuses` lets the glue contribute the statuses this kernel cannot
    see, such as `unavailable` from a store failure or
    `unvalidated-coefficient-era` from its era catalogue; they are merged
    through `worst_status`, so the precedence order stays in one place.

    `forecast_population` is the set of `EntryRecord`s whose units become
    `consumption`, `projection` and `headroom` — the current regime, not the
    historical fit population (section 35). It is the population section 18
    requires be supported, and it is supplied separately because the kernel
    cannot derive it from the daily series. It is REQUIRED and carries no
    default: an omitted population disables section 35's probe entirely and
    returns `ok` at exit 0, so a caller that never wired it must fail loudly
    rather than lose a spec-mandated check. Pass an explicit empty tuple to
    state that the population really is empty.

    `unattributed` is the dict `unattributed_units()` returns and
    `in_progress_excluded` the tuple `in_progress_dates()` returns. Both are
    always published, as null when the caller supplied none, so a glue that
    never wired a diagnostic is visible on the wire rather than silently
    absent.

    Every numeric parameter is checked for finiteness and sign (section 32). A
    non-finite or negative value withholds the evidence that depends on it
    with a typed code, because a NaN propagates through the headroom
    arithmetic into a published interval instead of raising.
    """
    require_aware(now, "now")
    for label, value in (("newest_at", newest_at),
                         ("current_week_start", current_week_start),
                         ("current_week_end", current_week_end)):
        if value is not None:
            require_aware(value, label)
    forecast_entries = list(forecast_population)
    for entry in forecast_entries:
        require_aware(entry.at, "forecast_entry.at")

    rejected: list[str] = []
    if current_week_units is not None and not _usable_quantity(
            current_week_units):
        rejected.append("current_week_units")
        current_week_units = None
        week_units_invalid = True
    else:
        week_units_invalid = False
    if observed_percent is not None and not _usable_quantity(observed_percent):
        rejected.append("observed_percent")
        observed_percent = None

    # `analyse` is a public entry point and takes "most recent" positionally
    # through `eligible[-n:]`, so it sorts rather than assuming its caller did.
    series = sorted(series, key=lambda o: (o.date, o.segment_id))
    detector = detect_change(series, override_split=override_split)
    # The fence shares the detector's horizon so that the scan, the floor and
    # the fit all judge the same span of history. A fence computed over every
    # retained day would be a floor on absolute daily volume taken across
    # eras whose units-per-point differ several-fold.
    structural = [o for o in series if o.cause is None
                  and (detector.scan_start_date is None
                       or o.date >= detector.scan_start_date)]
    if detector.split_date is not None:
        prefix = [o for o in structural if o.date < detector.split_date]
    else:
        prefix = structural
    fence = eligibility_fence([o.units for o in prefix])
    fenced = apply_eligibility_fence(series, fence)

    eligible = [o for o in fenced if o.cause is None]
    # Successor-only publication is spec section 22.
    confirmed = detector.qualified and detector.split_date is not None
    if confirmed:
        baseline_days = [o for o in eligible if o.date < detector.split_date]
        published_days = [o for o in eligible
                          if o.date >= detector.split_date]
    else:
        baseline_days = []
        published_days = eligible[-max_fit_population_days():]
    baseline_raw = jackknife_fit(baseline_days)
    fit = jackknife_fit(published_days)
    # The reference centres and radii come from the regime that ESTABLISHED
    # the composition, so a successor is measured against what preceded it.
    # With no confirmed change the bounded fit population is the reference,
    # and what is measured against it is the separately supplied forecast
    # population rather than those same days.
    reference_days = baseline_days if confirmed else published_days
    family_centre = composition_centre(
        [o.family_shares for o in reference_days])
    class_centre = composition_centre(
        [o.class_shares for o in reference_days])
    family_radius = support_radius(
        [o.family_shares for o in reference_days], family_centre)
    class_radius = support_radius(
        [o.class_shares for o in reference_days], class_centre)
    current_family_centre = composition_centre(
        [o.family_shares for o in published_days])
    current_class_centre = composition_centre(
        [o.class_shares for o in published_days])
    # The radius that DECIDES is the effective one (section 34). The empirical
    # values are published as diagnostics so a reader can see when the floor
    # bound rather than the observed spread.
    family_radius_empirical, class_radius_empirical = (family_radius,
                                                       class_radius)
    family_radius = effective_radius(
        family_radius, float(MIX_SUPPORT["family_tv_floor"]))
    class_radius = effective_radius(
        class_radius, token_class_floor(class_centre))

    # Section 35: after a confirmed change only the entries at or after the
    # successor boundary belong to the current regime, and they are measured
    # against the BASELINE references, which `reference_days` already is.
    if confirmed:
        probed_entries = [
            e for e in forecast_entries
            if e.at.astimezone(dt.timezone.utc).date() >= detector.split_date]
    else:
        probed_entries = forecast_entries
    forecast_shares = (aggregate_composition(probed_entries)
                       if probed_entries else None)

    contributed = list(extra_statuses)
    composition_provenance = _composition_provenance(
        confirmed=confirmed, reference_days=reference_days,
        decisive_days=published_days, forecast_shares=forecast_shares,
        forecast_probed=bool(probed_entries),
        family_centre=family_centre, family_radius=family_radius,
        class_centre=class_centre, class_radius=class_radius)
    if composition_provenance:
        contributed.append(CalibrationStatus.UNSUPPORTED_MODEL_MIX)
    if confirmed:
        window_start = detector.split_date
    elif published_days:
        window_start = published_days[0].date
    else:
        window_start = None
    window = [o for o in fenced
              if window_start is None or o.date >= window_start]

    # The status is classified over the population actually PUBLISHED — the
    # successor regime after a confirmed change, the bounded fit population
    # otherwise — and its health is scanned over that same window (section
    # 30), so a bad day in history the command neither fits nor scans cannot
    # withhold a verdict forever.
    status = worst_status(
        [classify(window, fit, detector, newest_at=newest_at, now=now,
                  fingerprint_matches=fingerprint_matches, fence=fence,
                  population=published_days)]
        + contributed
    )
    blocking = blocking_reasons(window, detector)
    verdict, exit_code = resolve_outcome(
        status, change_detected=detector.qualified, blocking=blocking)

    population = {
        "days": len(published_days),
        "segments": len({o.segment_id for o in published_days}),
        "withheld": sum(1 for o in window if o.cause is not None),
        "considered": len(window),
    }
    support = Support(population["days"], population["segments"])

    baseline_population = {
        "days": len(baseline_days),
        "segments": len({o.segment_id for o in baseline_days}),
        "withheld": sum(1 for o in fenced
                        if o.cause is not None
                        and (window_start is None or o.date < window_start)),
        "considered": len(fenced) - len(window),
    }
    if not confirmed:
        no_split = ("no-confirmed-split",)
        baseline_fit = evidence_withheld(
            CalibrationStatus.INSUFFICIENT_HISTORY, baseline_population,
            no_split)
        watch_fit = evidence_withheld(
            CalibrationStatus.INSUFFICIENT_HISTORY, population, no_split)
    else:
        superseded = ("superseded",
                      f"split-at:{detector.split_date.isoformat()}")
        if baseline_raw is None:
            baseline_fit = evidence_withheld(
                CalibrationStatus.INSUFFICIENT_HISTORY, baseline_population,
                superseded)
        else:
            baseline_fit = evidence_available(
                baseline_raw[0], baseline_raw[1],
                Support(baseline_population["days"],
                        baseline_population["segments"]),
                baseline_population, superseded)
        prediction_ready = status is CalibrationStatus.OK and fit is not None
        if fit is None:
            watch_fit = evidence_withheld(
                CalibrationStatus.INSUFFICIENT_HISTORY, population,
                ("successor-regime",))
        else:
            watch_fit = evidence_available(
                fit[0], fit[1], support, population,
                ("successor-regime",) if prediction_ready
                else ("successor-regime", "detection-only"))

    published_qualifications = (("successor-regime",) if confirmed else ()) \
        + tuple(reason.value for reason in blocking)
    if status is not CalibrationStatus.OK or fit is None:
        withheld_code = status.value if status is not CalibrationStatus.OK \
            else CalibrationStatus.INSUFFICIENT_HISTORY.value
        fitted = evidence_withheld(withheld_code, population,
                                   published_qualifications)
        consumption = evidence_withheld(withheld_code, population,
                                        published_qualifications)
        headroom = evidence_withheld(withheld_code, population,
                                     published_qualifications)
        projection = evidence_withheld(withheld_code, population,
                                       published_qualifications)
    else:
        point, interval = fit
        fitted = evidence_available(point, interval, support, population,
                                    published_qualifications)
        if current_week_units is None:
            # Section 27's principle, which section 40 extends from the week
            # WINDOW to the week FIGURE: a caller supplying nothing is not the
            # user's history being missing. Both absences are `unavailable`
            # and the qualification says which one happened.
            code = CalibrationStatus.UNAVAILABLE.value
            marks = published_qualifications + (
                ("current-week-units-invalid",) if week_units_invalid
                else ("current-week-units-unknown",))
            consumption = evidence_withheld(code, population, marks)
            headroom = evidence_withheld(code, population, marks)
            projection = evidence_withheld(code, population, marks)
        else:
            used = current_week_units / point
            # A larger units-per-point means a smaller consumption, so the
            # bounds invert. A floored-at-zero lower bound leaves the upper
            # consumption bound unbounded rather than infinite.
            used_lo = current_week_units / interval.hi if interval.hi else None
            used_hi = (current_week_units / interval.lo
                       if interval.lo > 0.0 else None)
            consumption = evidence_available(
                used, Interval(used_lo if used_lo is not None else 0.0,
                               used_hi), support, population,
                published_qualifications)
            # Headroom is quota remaining, so it lives in [0, 100]. The
            # point and BOTH bounds go through one clamp, which is monotone,
            # so `lo <= point <= hi` survives it. Clamping the lower bound
            # alone inverts the interval for a user above 100% of quota.
            headroom = evidence_available(
                _clamp_percent(100.0 - used),
                Interval(
                    _clamp_percent(100.0 - used_hi)
                    if used_hi is not None else 0.0,
                    _clamp_percent(100.0 - used_lo)
                    if used_lo is not None else 100.0,
                ),
                support, population, published_qualifications)
            projection = _project(used, consumption.interval, support,
                                  population, now=now,
                                  week_start=current_week_start,
                                  week_end=current_week_end,
                                  qualifications=published_qualifications)

    merged = dict(diagnostics or {})
    merged.update({
        "detector": detector_diagnostics(detector),
        "eligibilityFence": fence,
        "eligibleDays": len(eligible),
        "baselineDays": len(baseline_days),
        "currentRegimeDays": len(published_days),
        "withheldDays": len(fenced) - len(eligible),
        "dedicatedPoolScope": DEDICATED_POOL_SCOPE_NOTE,
        "familyRadiusEmpirical": family_radius_empirical,
        "classRadiusEmpirical": class_radius_empirical,
        "forecastPopulationEntries": len(probed_entries),
        "rejectedNumericInputs": sorted(rejected),
        "unattributedUnits": (None if unattributed is None
                              else float(unattributed["units"])),
        "unattributedEntries": (None if unattributed is None
                                else int(unattributed["entries"])),
        "inProgressDayExcluded": (
            None if in_progress_excluded is None
            else [d.isoformat() for d in in_progress_excluded]),
    })
    if consumption.value is not None and observed_percent is not None:
        merged["observedMinusModelled"] = observed_percent - consumption.value
    # `json.dumps` emits a bare `NaN` for a non-finite float, which is not
    # valid JSON, and a caller-supplied diagnostic is as capable of carrying
    # one as this kernel is.
    merged = _finite_json(merged)
    return QuotaAnalysis(
        status=status, verdict=verdict, exit_code=exit_code, fitted=fitted,
        consumption=consumption, projection=projection, headroom=headroom,
        baseline_fit=baseline_fit, watch_fit=watch_fit,
        observed_percent=observed_percent,
        family_shares=family_centre or {}, class_shares=class_centre or {},
        current_family_shares=current_family_centre or {},
        current_class_shares=current_class_centre or {},
        family_radius=family_radius, class_radius=class_radius,
        detector=detector, blocking=tuple(blocking), diagnostics=merged,
        # From the SAME published `window` that was passed to `classify`, so
        # the two describe one population and cannot drift apart.
        detector_input_causes=frozenset(
            o.cause for o in window if o.cause is not None),
        composition_provenance=composition_provenance,
    )


def _usable_quantity(value) -> bool:
    """A finite, non-negative number the arithmetic below can consume."""
    return _finite_quantity(value) and value >= 0.0


def _finite_json(value):
    """`value` with every non-finite float replaced by None, recursively.

    KEYS as well as values. `json.dumps({float("nan"): 1})` emits
    `{"NaN": 1}`, which is not valid JSON, and a dict key was the one
    remaining path by which a non-finite float reached the serializer.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {_finite_json(k): _finite_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(v) for v in value]
    return value


def _clamp_percent(value: float) -> float:
    """Confine a meter-point quantity to the [0, 100] the meter can express.

    Headroom is quota remaining, so both limbs belong to its definition
    whether or not an input reaches them. The function must stay monotone,
    because the caller applies it to the point and to both interval bounds
    and relies on `lo <= point <= hi` surviving.
    """
    return max(0.0, min(100.0, value))


def _project(used, interval, support, population, *, now, week_start,
             week_end, qualifications=()) -> CalibrationEvidence:
    """Linear pace projection of consumption to the week's end.

    A missing week window is `unavailable`, not `no-local-history`: the
    latter tells the user their token history is missing when the actual
    cause is that the caller supplied no window.
    """
    if week_start is None or week_end is None:
        return evidence_withheld(
            CalibrationStatus.UNAVAILABLE, population,
            tuple(qualifications) + ("week-window-unknown",))
    elapsed = (min(now, week_end) - week_start).total_seconds()
    span = (week_end - week_start).total_seconds()
    if elapsed <= 0 or span <= 0:
        return evidence_withheld(
            CalibrationStatus.UNAVAILABLE, population,
            tuple(qualifications) + ("week-window-invalid",))
    factor = span / elapsed
    scaled = Interval(
        interval.lo * factor,
        interval.hi * factor if interval.hi is not None else None,
    ) if interval is not None else None
    return evidence_available(used * factor, scaled, support, population,
                              qualifications)
