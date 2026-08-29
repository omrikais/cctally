"""Validation and application of a persisted quota calibration (#661 S2).

Pure: no clock, no filesystem, no database, no rendering. The two adapters
here gate every S2 consumer's access to the regime S1 persisted, because
neither half of that access is safe to do by hand.

`_cctally_quota_model.load_calibrations` validates only the outer envelope and
`stored_regimes` shallow-copies arbitrary inner dictionaries, so a consumer
reading the file directly inherits whatever a `unitsPerPoint` key happens to
hold — including a NaN, which passes every `> 0` guard the caller is likely to
write. `validate_regime` is the predicate that refuses those shapes.

A stored `status: "ok"` is a historical fact about the population S1 fitted
on. It is NOT evidence that the population a consumer is about to measure
still sits inside the recorded composition radii. `apply_regime` re-tests
support against the consumer's own entries before it converts anything.

Spec: docs/superpowers/specs/2026-08-28-661-s2-quota-insight-surfaces.md §1.1
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import math

import _lib_quota_model as qm

UTC = dt.timezone.utc


class RegimeRejection(str, enum.Enum):
    """Why a stored regime cannot be used. A closed union.

    Every member is a typed cause a renderer states; none of them is an
    error condition, because a consumer that cannot reach a calibration
    withholds its modelled figure and carries on.
    """

    SCHEMA_VERSION = "calibration-schema-version"
    FINGERPRINT = "calibration-fingerprint-mismatch"
    REVISION = "calibration-revision-mismatch"
    MALFORMED = "calibration-malformed"
    ABSENT = "calibration-absent"
    UNREADABLE = "calibration-unreadable"
    #: S1 fits a successor regime as soon as it detects a rate change and
    #: marks it `detection-only` while that fit stays below its prediction
    #: gate. The mark is S1 stating that the regime must not be used to
    #: predict, so it is refused here rather than applied.
    DETECTION_ONLY = "calibration-detection-only"


#: The qualification S1 writes onto a successor fit that is below its
#: prediction gate. `_lib_quota_model` is the only writer of this string; the
#: constant is named here so the gate is one literal rather than a spelling
#: repeated at each reader.
DETECTION_ONLY_QUALIFICATION = "detection-only"


class ApplyRejection(str, enum.Enum):
    """Why a validated regime cannot be applied to a given population."""

    UNSUPPORTED_COMPOSITION = "unsupported-composition"
    EMPTY_POPULATION = "no-local-history"


@dataclasses.dataclass(frozen=True)
class ValidatedRegime:
    """One stored regime that passed every check in `validate_regime`.

    `family_radius` and `class_radius` are the EFFECTIVE, floor-clamped radii
    S1 recorded. S1's spec binds one renderer instruction about them: they are
    not measured quantities and must never be labelled as such. The empirical
    values are separate fields of the analysis and are deliberately not
    carried here, so a renderer reading this record cannot mistake one for the
    other.
    """

    account_key: "str | None"
    effective_from: "dt.datetime"          # aware
    effective_until: "dt.datetime | None"  # aware when present
    units_per_point: float                 # finite, > 0
    interval_lo: float                     # finite, > 0, <= units_per_point
    interval_hi: "float | None"            # None, or >= units_per_point
    status: str
    as_of: "dt.datetime"                   # aware
    family_shares: dict
    class_shares: dict
    family_radius: "float | None"
    class_radius: "float | None"
    qualifications: tuple = ()


@dataclasses.dataclass(frozen=True)
class AppliedQuota:
    """Modelled consumption for one population under one regime.

    `consumed_lo` derives from `interval_hi` and `consumed_hi` from
    `interval_lo`. Points are units divided by units-per-point, so the LARGER
    budget yields the SMALLER estimate and the bounds swap. An interval copied
    through without that inversion is not merely imprecise, it is inverted.
    """

    consumed_points: float
    consumed_lo: float
    consumed_hi: "float | None"
    units: float


def _finite(value) -> bool:
    """True when `value` is a real, finite number.

    `math.isfinite` raises `TypeError` on a string and `OverflowError` on an
    arbitrarily large integer, and a stored JSON value can be either.
    """
    try:
        return math.isfinite(value)
    except (TypeError, OverflowError):
        return False


def _aware_instant(value):
    """Parse a stored ISO-8601 string to an aware UTC instant, or None.

    Strict by design. A naive spelling is rejected rather than assumed: every
    writer in this repository stores an aware instant, and a reader that
    silently grounds a naive one in host-local time shifts a stored UTC stamp
    by the local offset with nothing reporting it.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _shares(value) -> "dict | None":
    """A composition mapping of finite floats, or None when unusable."""
    if not isinstance(value, dict):
        return None
    out = {}
    for name, share in value.items():
        if not isinstance(name, str) or not _finite(share):
            return None
        out[name] = float(share)
    return out


def _optional_radius(value):
    """`(ok, radius)` — a radius is absent, or a finite non-negative float."""
    if value is None:
        return True, None
    if not _finite(value) or value < 0.0:
        return False, None
    return True, float(value)


def validate_regime(raw, *, account_key, expected_fingerprint,
                    expected_revision, require_prediction_ready=True
                    ) -> "ValidatedRegime | RegimeRejection":
    """Validate one stored regime record. Never returns a partial record.

    The order of the checks is part of the contract: identity first, then
    shape, then the prediction gate. A regime written under other constants is
    reported as a fingerprint or revision mismatch even when its numbers are
    also unusable, because that is the cause a user can act on.

    `require_prediction_ready` is the section 1.1 gate. S1 marks a successor
    fit `detection-only` while it stays below the gate, and reserves
    `calibration.fitted` for a prediction-ready successor, so a regime
    carrying that qualification is refused for every PROJECTION consumer.
    Section 6's rate-change alert reads the same regime pair deliberately —
    detection is exactly what a detection-grade fit is for — and passes
    `False`. The gate makes the regime unusable, not unreadable.

    Support re-testing does not subsume this gate and the two must not be
    confused. `apply_regime`'s support test asks whether the consumer's
    population resembles the fit population; the gate asks whether the fit was
    ever good enough to predict with. A three-day fit sits well inside its own
    recorded radii, because a narrow population produces narrow radii.
    """
    if not isinstance(raw, dict):
        return RegimeRejection.MALFORMED
    if raw.get("fingerprint") != expected_fingerprint:
        return RegimeRejection.FINGERPRINT
    revision = raw.get("algorithmRevision")
    if isinstance(revision, bool) or not isinstance(revision, int) \
            or revision != expected_revision:
        return RegimeRejection.REVISION

    point = raw.get("unitsPerPoint")
    if not _finite(point) or point <= 0.0:
        return RegimeRejection.MALFORMED
    point = float(point)

    interval = raw.get("interval")
    if not isinstance(interval, dict):
        return RegimeRejection.MALFORMED
    lo = interval.get("lo")
    if not _finite(lo) or lo <= 0.0 or lo > point:
        return RegimeRejection.MALFORMED
    hi = interval.get("hi")
    if hi is not None:
        if not _finite(hi) or hi < point:
            return RegimeRejection.MALFORMED
        hi = float(hi)

    effective_from = _aware_instant(raw.get("effectiveFrom"))
    if effective_from is None:
        return RegimeRejection.MALFORMED
    as_of = _aware_instant(raw.get("asOf"))
    if as_of is None:
        return RegimeRejection.MALFORMED
    effective_until = None
    if raw.get("effectiveUntil") is not None:
        effective_until = _aware_instant(raw.get("effectiveUntil"))
        if effective_until is None:
            return RegimeRejection.MALFORMED

    family_shares = _shares(raw.get("familyShares") or {})
    class_shares = _shares(raw.get("classShares") or {})
    if family_shares is None or class_shares is None:
        return RegimeRejection.MALFORMED
    ok_family, family_radius = _optional_radius(raw.get("familyRadius"))
    ok_class, class_radius = _optional_radius(raw.get("classRadius"))
    if not ok_family or not ok_class:
        return RegimeRejection.MALFORMED

    status = raw.get("status")
    qualifications = raw.get("qualifications") or ()
    if not isinstance(qualifications, (list, tuple)):
        return RegimeRejection.MALFORMED
    marks = tuple(str(q) for q in qualifications)
    if require_prediction_ready and DETECTION_ONLY_QUALIFICATION in marks:
        return RegimeRejection.DETECTION_ONLY

    return ValidatedRegime(
        account_key=account_key,
        effective_from=effective_from,
        effective_until=effective_until,
        units_per_point=point,
        interval_lo=float(lo),
        interval_hi=hi,
        status=str(status) if status is not None else "",
        as_of=as_of,
        family_shares=family_shares,
        class_shares=class_shares,
        family_radius=family_radius,
        class_radius=class_radius,
        qualifications=marks,
    )


def population_units(entries) -> float:
    """Summed weighted quota units over the general-meter entries.

    The same filter `_cctally_quota_model.analyse_account` applies to the
    current week: an entry whose family does not drain the general weekly
    meter contributes nothing, and one whose cache-write split is unknown is
    skipped rather than priced at zero.
    """
    total = 0.0
    for entry in entries:
        if qm.family_participation(qm.normalize_family(entry.model)) \
                != "general":
            continue
        units = qm.weighted_units(entry)
        if units is None:
            continue
        total += units
    return total


def apply_regime(regime: ValidatedRegime, entries
                 ) -> "AppliedQuota | ApplyRejection":
    """Modelled consumption for `entries` under `regime`.

    Support is re-tested here rather than inherited from the regime's stored
    status, because that status describes S1's fit population and this one is
    the consumer's. The arithmetic afterwards reproduces the kernel's, so a
    consumer never re-derives the inversion by hand.
    """
    aggregated = qm.aggregate_composition(entries)
    if aggregated is None:
        return ApplyRejection.EMPTY_POPULATION
    family_shares, class_shares = aggregated
    if not qm.is_supported(family_shares, class_shares,
                           regime.family_shares, regime.family_radius,
                           regime.class_shares, regime.class_radius):
        return ApplyRejection.UNSUPPORTED_COMPOSITION

    units = population_units(entries)
    if not _finite(units) or units < 0.0:
        return ApplyRejection.EMPTY_POPULATION

    consumed = units / regime.units_per_point
    consumed_lo = (units / regime.interval_hi
                   if regime.interval_hi else 0.0)
    consumed_hi = (units / regime.interval_lo
                   if regime.interval_lo > 0.0 else None)
    return AppliedQuota(consumed_points=consumed, consumed_lo=consumed_lo,
                        consumed_hi=consumed_hi, units=units)
