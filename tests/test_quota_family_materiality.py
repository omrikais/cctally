"""#991 — the evidence-bounded family materiality test of the quota kernel.

`cctally quota` withholds its fit, consumption, headroom and projection when
the forecast population's composition is unsupported. Before #991 a family
shift was supported only inside the total-variation radius. The kernel now
also admits a shift whose worst-case effect on the forecast, bounded by the
meter evidence each era carries, stays within 5% (spec
docs/superpowers/specs/2026-10-23-991-family-participation-design.md, sections
4.1 to 4.3 and section 5 tests 1, 2 and 7).

Every test names the accuracy failure it detects: admitting a shift the
evidence does not bound publishes a forecast that may be wrong by more than
the materiality the rest of the support policy promises, and rejecting one it
does bound withholds a forecast the user could have had. The expected numbers
are worked by hand from the shipped bound, never recomputed through the
function under test.
"""
from __future__ import annotations

import dataclasses
import itertools
import math

import pytest

# conftest puts bin/ on sys.path.
import _lib_quota_model as qm

OPUS55 = qm.era_by_id("opus-5-5")
OPUS5 = qm.era_by_id("opus-5")
OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
UNAVAILABLE = qm.FamilyEffectUnavailable


def _era(*bounds):
    """The shipped `opus-5-5` era with its bounds replaced, for one test."""
    return dataclasses.replace(OPUS55, family_rate_bounds=tuple(
        qm.FamilyRateBound(family, low, high, "test-only")
        for family, low, high in bounds))


# --------------------------------------------------------------------------
# Section 5 test 1 — the bound arithmetic
# --------------------------------------------------------------------------
def test_the_shipped_era_evidence_is_the_pinned_bound():
    """A wrong shipped bound mis-admits or mis-rejects every Sonnet shift.

    The values are spec 4.1's: the reference is the era's own Opus family, the
    one bound is Sonnet 5.5 at [0.09, 0.61] with its evidence provenance, and
    the Opus 5 era and the unidentified families carry none.
    """
    assert OPUS55.reference_family == OPUS
    assert OPUS55.family_rate_bounds == (qm.FamilyRateBound(
        SONNET, 0.09, 0.61, "991-meter-evidence-2026-10-23"),)
    assert OPUS55.rate_bound(SONNET) == OPUS55.family_rate_bounds[0]
    for family in (HAIKU, "claude-sonnet-5", "claude-haiku-4-5", OPUS):
        assert OPUS55.rate_bound(family) is None
    assert OPUS5.reference_family == "claude-opus-5"
    assert OPUS5.family_rate_bounds == ()
    assert qm.MIX_SUPPORT["family_max_relative_effect"] == 0.05
    assert type(qm.MIX_SUPPORT["family_max_relative_effect"]) is float
    # A separate key: tying it to the token-class threshold would let a
    # change to one silently move the other.
    assert "token_class_max_relative_effect" in qm.MIX_SUPPORT
    assert qm.MIX_SUPPORT_POLICY_REVISION == 2


def test_a_sonnet_shift_is_bounded_by_the_shipped_evidence():
    """Pure Opus fit, 5% Sonnet forecast: E = 0.05 * 0.91 / 1 = 0.0455.

    d = max(|0.09 - 1|, |0.61 - 1|) = 0.91 and the denominator is q's own
    reference share times 1. A denominator taken over p, or a d dropping the
    low term (0.39), would move the result.
    """
    effect = qm.family_effect_bound({OPUS: 0.95, SONNET: 0.05}, {OPUS: 1.0},
                                    era=OPUS55)
    assert effect.unavailable is None
    assert effect.families == ()
    assert effect.bound == pytest.approx(0.0455, abs=1e-15)


def test_the_denominator_is_the_fit_reference_not_the_forecast():
    """The bound is relative to the FITTED blend, so its denominator is q.

    q = {opus 0.5, sonnet 0.5}, p = {opus 0.6, sonnet 0.4}:
    num = 0.1 * 0.91 = 0.091; den over q = 0.5 + 0.5 * 0.09 = 0.545, so
    E = 0.091 / 0.545. Over p it would be 0.6 + 0.4 * 0.09 = 0.636.
    """
    effect = qm.family_effect_bound({OPUS: 0.6, SONNET: 0.4},
                                    {OPUS: 0.5, SONNET: 0.5}, era=OPUS55)
    assert effect.bound == pytest.approx(0.091 / 0.545, rel=1e-12)
    assert effect.bound != pytest.approx(0.091 / 0.636, rel=1e-6)


def test_a_fit_weighted_reference_that_differs_from_the_median_centre_decides():
    """Measuring materiality against the median centre rejects a real admission.

    Five fit days: three pure-Opus days of 1 unit and two half-Sonnet days of
    10 units. The componentwise median centre is pure Opus, but the units the
    fit was taken over are q = {opus 13/23, sonnet 10/23}. A 45% Sonnet
    forecast is far outside the radius around the centre, within 5% of the
    fitted blend (E = |0.45 - 10/23| * 0.91 / (13/23 + 10/23 * 0.09) = 0.02291)
    and far outside it measured against the centre (0.45 * 0.91 = 0.4095).
    """
    centre = qm.composition_centre(
        [{OPUS: 1.0}] * 3 + [{OPUS: 0.5, SONNET: 0.5}] * 2)
    assert centre == {OPUS: 1.0, SONNET: 0.0}
    q = {OPUS: 13 / 23, SONNET: 10 / 23}
    p = {OPUS: 0.55, SONNET: 0.45}
    expected = abs(0.45 - 10 / 23) * 0.91 / (13 / 23 + (10 / 23) * 0.09)
    assert expected == pytest.approx(0.02291, abs=1e-5)

    def decide(reference):
        return qm.evaluate_prediction_support(
            p, {"fresh": 1.0}, family_centre=centre, family_radius=0.03,
            class_centre={"fresh": 1.0}, class_radius=0.05,
            fit_family_shares=reference, era=OPUS55)

    fitted = decide(q)
    assert fitted.family_effect.bound == pytest.approx(expected, rel=1e-12)
    assert fitted.family_within_radius is False
    assert fitted.supported is True
    assert fitted.family_admitted_by == "materiality"
    median = decide(centre)
    assert median.family_effect.bound == pytest.approx(0.4095, rel=1e-12)
    assert median.supported is False


def test_a_tiny_unevidenced_change_beside_a_bounded_one_is_unavailable():
    """Sonnet inside its bound plus a 0.1-point Haiku change is NOT admitted.

    Counting the unbounded Haiku change as zero would admit a shift no
    evidence bounds; the result is unavailable and names Haiku alone.
    """
    effect = qm.family_effect_bound(
        {OPUS: 0.948, SONNET: 0.05, HAIKU: 0.002},
        {OPUS: 0.999, HAIKU: 0.001}, era=OPUS55)
    assert effect.bound is None
    assert effect.unavailable is UNAVAILABLE.UNEVIDENCED_FAMILY_CHANGE
    assert effect.families == (HAIKU,)


def test_every_unevidenced_family_is_named_sorted():
    effect = qm.family_effect_bound(
        {OPUS: 0.94, SONNET: 0.05, HAIKU: 0.01},
        {OPUS: 0.98, "claude-sonnet-5": 0.01, "claude-haiku-4-5": 0.01},
        era=OPUS55)
    assert effect.unavailable is UNAVAILABLE.UNEVIDENCED_FAMILY_CHANGE
    assert effect.families == ("claude-haiku-4-5", HAIKU, "claude-sonnet-5")


def test_an_unchanged_unevidenced_family_contributes_nothing():
    """An unbounded family at an identical share is not a shift.

    q = {opus 0.9, haiku 0.1}, p = {opus 0.85, sonnet 0.05, haiku 0.1}:
    num = 0.05 * 0.91; den = 0.9 * 1 + 0.1 * 0 (its lower bound is zero).
    """
    effect = qm.family_effect_bound({OPUS: 0.85, SONNET: 0.05, HAIKU: 0.1},
                                    {OPUS: 0.9, HAIKU: 0.1}, era=OPUS55)
    assert effect.unavailable is None
    assert effect.bound == pytest.approx(0.05 * 0.91 / 0.9, rel=1e-12)


@pytest.mark.parametrize("low,high,expected,admitted", [
    (0.09, 0.61, 0.0455, True),
    (0.0, 1.10, 0.05, True),
    (0.0, 2.10, 0.055, False),
])
def test_widening_a_bound_never_admits_what_a_narrower_one_rejected(
        low, high, expected, admitted):
    """Nested bounds give E = 0.0455, 0.05 and 0.055 for one shift.

    p = {opus 0.95, sonnet 0.05}, q = {opus 1.0}. [0.0, 1.10] gives d = 1.0 and
    E exactly 0.05, which equality admits; [0.0, 2.10] gives d = 1.1 and
    0.055, which is rejected. A `<` threshold rejects the equality case.
    """
    era = _era((SONNET, low, high))
    support = qm.evaluate_prediction_support(
        {OPUS: 0.95, SONNET: 0.05}, {"fresh": 1.0},
        family_centre={OPUS: 1.0}, family_radius=0.03,
        class_centre={"fresh": 1.0}, class_radius=0.05,
        fit_family_shares={OPUS: 1.0}, era=era)
    assert support.family_within_radius is False
    assert support.family_effect.bound == pytest.approx(expected, abs=1e-15)
    assert support.supported is admitted
    assert support.family_admitted_by == ("materiality" if admitted else None)


def test_the_bound_never_decreases_as_the_bound_widens():
    shift = ({OPUS: 0.95, SONNET: 0.05}, {OPUS: 1.0})
    bounds = [qm.family_effect_bound(*shift, era=_era((SONNET, lo, hi))).bound
              for lo, hi in ((0.09, 0.61), (0.0, 1.10), (0.0, 2.10))]
    assert bounds == sorted(bounds)
    assert bounds[1] == 0.05


def test_threshold_equality_is_exact_float_equality():
    """E computed as exactly 0.05 is admitted: the comparison is `<=`."""
    effect = qm.family_effect_bound({OPUS: 0.95, SONNET: 0.05}, {OPUS: 1.0},
                                    era=_era((SONNET, 0.0, 1.10)))
    assert effect.bound == qm.MIX_SUPPORT["family_max_relative_effect"]


def test_a_valid_zero_denominator_is_nonpositive_not_a_bound():
    """A fit entirely in a family whose lower rate bound is 0 bounds nothing.

    q = {sonnet 1.0} under a bound [0, 0.61]: the reference share is zero and
    Sonnet's lower bound is zero, so the denominator is exactly zero. Dividing
    would raise or publish infinity.
    """
    effect = qm.family_effect_bound({OPUS: 0.1, SONNET: 0.9}, {SONNET: 1.0},
                                    era=_era((SONNET, 0.0, 0.61)))
    assert effect.bound is None
    assert effect.unavailable is UNAVAILABLE.NONPOSITIVE_DENOMINATOR
    assert effect.families == ()


@pytest.mark.parametrize("p,q,bounds", [
    ({OPUS: 1.1, SONNET: -0.1}, {OPUS: 1.0}, ((SONNET, 0.09, 0.61),)),
    ({OPUS: 0.95, SONNET: 0.05}, {OPUS: -0.5, SONNET: 1.5},
     ((SONNET, 0.09, 0.61),)),
    ({OPUS: float("nan"), SONNET: 0.05}, {OPUS: 1.0}, ((SONNET, 0.09, 0.61),)),
    ({OPUS: 0.95, SONNET: 0.05}, {OPUS: float("inf")},
     ((SONNET, 0.09, 0.61),)),
    ({OPUS: 0.5, SONNET: 0.05}, {OPUS: 1.0}, ((SONNET, 0.09, 0.61),)),
    ({OPUS: 0.95, SONNET: 0.05}, {OPUS: 0.2}, ((SONNET, 0.09, 0.61),)),
    ({OPUS: 0.95, SONNET: 0.05}, {OPUS: 1.0}, ((SONNET, -0.1, 0.61),)),
    ({OPUS: 0.95, SONNET: 0.05}, {OPUS: 1.0}, ((SONNET, 0.09, float("inf")),)),
    ({OPUS: 0.95, SONNET: 0.05}, {OPUS: 1.0}, ((SONNET, 0.7, 0.61),)),
    ({OPUS: 0.95, SONNET: 0.05}, {OPUS: 1.0}, ((SONNET, float("nan"), 0.61),)),
    ([(OPUS, 1.0)], {OPUS: 1.0}, ((SONNET, 0.09, 0.61),)),
    ({OPUS: 0.95, SONNET: "0.05"}, {OPUS: 1.0}, ((SONNET, 0.09, 0.61),)),
    ({OPUS: 0.95, SONNET: 0.05}, {OPUS: 1e308, SONNET: 1e308},
     ((SONNET, 0.09, 0.61),)),
    ({OPUS: 1e308, SONNET: 1e308}, {OPUS: 1.0}, ((SONNET, 0.09, 0.61),)),
])
def test_malformed_shares_or_bounds_are_malformed_input(p, q, bounds):
    """Malformed input is refused before any denominator is evaluated.

    The second case sums to one with a negative share, whose denominator
    (-0.5 + 1.5 * 0.09) would also be nonpositive: validation must name the
    malformed input rather than be weakened to reach that later reason. The
    last two carry finite shares whose sum overflows, which must read as
    malformed input rather than raise `OverflowError` out of `math.fsum`.
    """
    effect = qm.family_effect_bound(p, q, era=_era(*bounds))
    assert effect.bound is None
    assert effect.unavailable is UNAVAILABLE.MALFORMED_INPUT


def test_a_missing_fit_reference_is_no_fit_reference():
    effect = qm.family_effect_bound({OPUS: 0.95, SONNET: 0.05}, None,
                                    era=OPUS55)
    assert effect.bound is None
    assert effect.unavailable is UNAVAILABLE.NO_FIT_REFERENCE


def test_an_absent_reference_family_with_a_positive_denominator_computes():
    """A fit with no Opus at all still bounds a change through Sonnet's low.

    q = {sonnet 1.0}, p = {sonnet 0.98, opus 0.02}: the Opus term carries
    d = 0; num = 0.02 * 0.91; den = 1.0 * 0.09.
    """
    effect = qm.family_effect_bound({SONNET: 0.98, OPUS: 0.02}, {SONNET: 1.0},
                                    era=OPUS55)
    assert effect.unavailable is None
    assert effect.bound == pytest.approx(0.02 * 0.91 / 0.09, rel=1e-12)


def test_shuffled_key_order_gives_an_identical_bound():
    """Every sum is a correctly rounded `fsum` over sorted keys.

    A plain `sum` over dict or set order moved a published radius by one ULP
    between processes (`tv_distance`'s history); the same must not happen to
    a value compared for equality against the threshold.
    """
    era = _era((SONNET, 0.09, 0.61), (HAIKU, 0.3, 1.7))
    p = {OPUS: 0.7, SONNET: 0.2, HAIKU: 0.1}
    q = {OPUS: 0.6, SONNET: 0.3, HAIKU: 0.1 + 1e-12}
    q[OPUS] = 1.0 - q[SONNET] - q[HAIKU]
    results = set()
    for p_order, q_order in itertools.product(
            itertools.permutations(p), itertools.permutations(q)):
        effect = qm.family_effect_bound({k: p[k] for k in p_order},
                                        {k: q[k] for k in q_order}, era=era)
        assert effect.unavailable is None and math.isfinite(effect.bound)
        results.add(effect.bound)
    assert len(results) == 1


def test_unchanged_means_exactly_equal():
    """A 1e-15 change to an unbounded family's share is a change.

    A tolerance would admit an unevidenced family that moved by less than
    it; the rule is exact equality of the full-precision shares.
    """
    q = {OPUS: 0.99, HAIKU: 0.01}
    moved = {OPUS: 0.99 - 1e-15, HAIKU: 0.01 + 1e-15}
    assert moved[HAIKU] != q[HAIKU]
    effect = qm.family_effect_bound(moved, q, era=OPUS55)
    assert effect.unavailable is UNAVAILABLE.UNEVIDENCED_FAMILY_CHANGE
    assert effect.families == (HAIKU,)
    same = qm.family_effect_bound(dict(q), q, era=OPUS55)
    assert same.unavailable is None
    assert same.bound == 0.0


def test_the_bounds_never_reach_the_weighting():
    """Bounds decide support only; no multiplier ever touches units.

    Weighting the same entries under the era with and without its bounds
    gives identical units and shares. A bound applied as a multiplier would
    change the public blended-weekly contract.
    """
    import datetime as dt
    day = dt.datetime(2026, 9, 24, 12, tzinfo=dt.timezone.utc)
    entries = [qm.EntryRecord(at=day, model=model, fresh=1000, output=200,
                              cache_create_total=0, cache_1h=0,
                              cache_read=5000)
               for model in (OPUS, SONNET, HAIKU)]
    bare = dataclasses.replace(OPUS55, family_rate_bounds=())
    for entry in entries:
        assert qm.weigh_entry(entry, OPUS55) == qm.weigh_entry(entry, bare)
    assert qm.aggregate_composition(entries, era=OPUS55) == \
        qm.aggregate_composition(entries, era=bare)


# --------------------------------------------------------------------------
# Section 5 test 2 — the prediction-support evaluator
# --------------------------------------------------------------------------
def _evaluate(family, classes, *, fit=None, family_radius=0.03,
              class_radius=0.05, family_centre=None, class_centre=None):
    return qm.evaluate_prediction_support(
        family, classes,
        family_centre={OPUS: 1.0} if family_centre is None else family_centre,
        family_radius=family_radius,
        class_centre=({"fresh": 1.0} if class_centre is None
                      else class_centre),
        class_radius=class_radius,
        fit_family_shares={OPUS: 1.0} if fit is None else fit, era=OPUS55)


def test_a_class_rejection_holds_despite_a_family_admission():
    """The token-class axis is mandatory in both branches.

    A Sonnet shift the bound admits, combined with an output-heavy class mix,
    must stay unsupported; dropping the class gate from the materiality
    branch would publish it.
    """
    support = _evaluate({OPUS: 0.95, SONNET: 0.05},
                        {"fresh": 0.2, "output": 0.8})
    assert support.family_admitted_by == "materiality"
    assert support.class_within_radius is False
    assert support.class_distance == pytest.approx(0.8)
    assert support.supported is False


def test_a_class_rejection_holds_inside_the_family_radius_too():
    support = _evaluate({OPUS: 0.99, SONNET: 0.01},
                        {"fresh": 0.2, "output": 0.8})
    assert support.family_admitted_by == "radius"
    assert support.supported is False


def test_materiality_admits_a_family_shift_with_the_class_inside():
    support = _evaluate({OPUS: 0.95, SONNET: 0.05}, {"fresh": 1.0})
    assert support.family_distance == pytest.approx(0.05)
    assert support.family_within_radius is False
    assert support.family_effect.bound == pytest.approx(0.0455)
    assert support.family_admitted_by == "materiality"
    assert support.class_distance == 0.0
    assert support.class_within_radius is True
    assert support.supported is True


def test_inside_the_radius_admits_by_radius_and_still_reports_the_effect():
    """The radius branch is unchanged, and the effect is a diagnostic."""
    support = _evaluate({OPUS: 0.98, HAIKU: 0.02}, {"fresh": 1.0})
    assert support.family_within_radius is True
    assert support.family_admitted_by == "radius"
    assert support.supported is True
    assert support.family_effect.unavailable is \
        UNAVAILABLE.UNEVIDENCED_FAMILY_CHANGE


def test_an_over_limit_effect_outside_the_radius_is_unsupported():
    support = _evaluate({OPUS: 0.9, SONNET: 0.1}, {"fresh": 1.0})
    assert support.family_effect.bound == pytest.approx(0.091)
    assert support.family_admitted_by is None
    assert support.supported is False


@pytest.mark.parametrize("missing", ["family_centre", "family_radius",
                                     "class_centre", "class_radius"])
def test_an_undefined_reference_is_unsupported_regardless_of_materiality(
        missing):
    """An undefined centre or radius never passes, even when E admits.

    This is today's `is_supported` result, so the thin-history and
    undefined-reference handling stay what they were.
    """
    kwargs = dict(family_centre={OPUS: 1.0}, family_radius=0.03,
                  class_centre={"fresh": 1.0}, class_radius=0.05)
    kwargs[missing] = None
    support = qm.evaluate_prediction_support(
        {OPUS: 0.95, SONNET: 0.05}, {"fresh": 1.0},
        fit_family_shares={OPUS: 1.0}, era=OPUS55, **kwargs)
    assert support.supported is False
    assert support.family_admitted_by is None
    assert support.family_distance is None
    assert support.class_distance is None
    assert support.family_effect.bound == pytest.approx(0.0455)


def test_is_supported_is_unchanged():
    """PRESERVATION (passes on the parent tree by design).

    `is_supported` stays the two-axis TV rule for the decisive watch days, so
    its answers over a grid of shares and radii match the rule as written
    before #991: inside both radii, both centres and radii defined.
    """
    centres = ({OPUS: 1.0}, {"fresh": 1.0})
    for sonnet, output, family_radius, class_radius in itertools.product(
            (0.0, 0.03, 0.05), (0.0, 0.05, 0.2), (None, 0.03), (None, 0.05)):
        family = {OPUS: 1.0 - sonnet, SONNET: sonnet}
        classes = {"fresh": 1.0 - output, "output": output}
        expected = (family_radius is not None and class_radius is not None
                    and qm.tv_distance(family, centres[0]) <= family_radius
                    and qm.tv_distance(classes, centres[1]) <= class_radius)
        assert qm.is_supported(family, classes, centres[0], family_radius,
                               centres[1], class_radius) is expected


# --------------------------------------------------------------------------
# Section 5 test 7 — the era table invariants and the fingerprint
# --------------------------------------------------------------------------
def test_the_shipped_era_table_satisfies_its_invariants():
    qm.validate_coefficient_eras(qm.COEFFICIENT_ERAS)


@pytest.mark.parametrize("change,needle", [
    (dict(reference_family="claude-unreleased-9"), "reference"),
    (dict(reference_family="claude-opus-5-5-20260922"), "reference"),
    (dict(family_rate_bounds=(qm.FamilyRateBound(
        OPUS, 0.5, 1.5, "x"),)), "reference"),
    (dict(family_rate_bounds=(qm.FamilyRateBound(
        "claude-unreleased-9", 0.5, 1.5, "x"),)), "general"),
    (dict(family_rate_bounds=(
        qm.FamilyRateBound(SONNET, 0.1, 0.6, "x"),
        qm.FamilyRateBound(SONNET, 0.2, 0.5, "y"))), "unique"),
    (dict(family_rate_bounds=(qm.FamilyRateBound(
        SONNET, -0.1, 0.6, "x"),)), "low"),
    (dict(family_rate_bounds=(qm.FamilyRateBound(
        SONNET, 0.7, 0.6, "x"),)), "low"),
    (dict(family_rate_bounds=(qm.FamilyRateBound(
        SONNET, 0.1, float("inf"), "x"),)), "finite"),
    (dict(family_rate_bounds=(qm.FamilyRateBound(
        SONNET, float("nan"), 0.6, "x"),)), "finite"),
    (dict(family_rate_bounds=(qm.FamilyRateBound(
        SONNET, 0.1, 0.6, ""),)), "provenance"),
])
def test_a_bad_era_table_raises_naming_the_invariant(change, needle):
    """A malformed era table must fail at import, never mis-decide support.

    A bound on the reference, a non-general or duplicate family, an inverted
    or non-finite interval or a bound with no provenance would each make the
    materiality branch admit on evidence that does not exist.
    """
    bad = (OPUS5, dataclasses.replace(OPUS55, **change))
    with pytest.raises(ValueError, match=needle):
        qm.validate_coefficient_eras(bad)


def test_the_payload_serializes_the_era_evidence():
    payload = qm.constants_payload()
    assert payload["mixSupportPolicyRevision"] == 2
    assert payload["mixSupport"]["family_max_relative_effect"] == 0.05
    eras = {e["id"]: e for e in payload["coefficientEras"]}
    assert eras["opus-5"]["referenceFamily"] == "claude-opus-5"
    assert eras["opus-5"]["familyRateBounds"] == []
    assert eras["opus-5-5"]["referenceFamily"] == OPUS
    assert eras["opus-5-5"]["familyRateBounds"] == [
        {"family": SONNET, "low": 0.09, "high": 0.61,
         "provenance": "991-meter-evidence-2026-10-23"}]
    assert qm.QUOTA_MODEL_ALGORITHM_REVISION == 5
    assert payload["algorithmRevision"] == 5


def _swap_open_era(monkeypatch, **change):
    monkeypatch.setattr(qm, "COEFFICIENT_ERAS", (
        OPUS5, dataclasses.replace(OPUS55, **change)))


@pytest.mark.parametrize("label,apply", [
    ("bound low", lambda mp: _swap_open_era(mp, family_rate_bounds=(
        qm.FamilyRateBound(SONNET, 0.08, 0.61,
                           "991-meter-evidence-2026-10-23"),))),
    ("bound high", lambda mp: _swap_open_era(mp, family_rate_bounds=(
        qm.FamilyRateBound(SONNET, 0.09, 0.62,
                           "991-meter-evidence-2026-10-23"),))),
    ("bound provenance", lambda mp: _swap_open_era(mp, family_rate_bounds=(
        qm.FamilyRateBound(SONNET, 0.09, 0.61, "another-run"),))),
    ("bounds removed", lambda mp: _swap_open_era(mp, family_rate_bounds=())),
    ("reference", lambda mp: _swap_open_era(mp, reference_family=SONNET,
                                            family_rate_bounds=())),
    ("threshold", lambda mp: mp.setitem(
        qm.MIX_SUPPORT, "family_max_relative_effect", 0.06)),
    ("policy revision", lambda mp: mp.setattr(
        qm, "MIX_SUPPORT_POLICY_REVISION", 3)),
])
def test_the_fingerprint_covers_the_family_support_policy(
        monkeypatch, label, apply):
    """A calibration fitted under another support policy must go stale.

    If a bound, the reference, the threshold or the policy revision changed
    without moving the fingerprint, a stored regime would be applied under a
    policy that did not admit its population.
    """
    before = qm.constants_fingerprint()
    assert before == qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT
    apply(monkeypatch)
    assert qm.constants_fingerprint() != before, label


def test_an_overflowing_stored_centre_is_outside_every_radius():
    """A TV centre whose finite shares overflow fails closed, never raises.

    Detects `tv_distance` raising `OverflowError` out of `math.fsum` when a
    stored regime's `familyShares` (validated by the looser `_shares`, which
    accepts any finite floats) holds values whose differences overflow: that
    exception escapes `apply_regime` into the forecast, project and alert
    consumers. An unmeasurable distance is outside every radius, so
    `is_supported` reports the population unsupported, and so does the
    evaluator when no fit reference lets the materiality branch decide.
    """
    centre = {OPUS: 1e308, SONNET: 1e308}
    p = {OPUS: 1.0}
    classes = {"fresh": 1.0}
    assert qm.tv_distance(p, centre) == math.inf
    assert qm.is_supported(p, classes, family_centre=centre,
                           family_radius=0.03, class_centre=classes,
                           class_radius=0.05) is False
    support = qm.evaluate_prediction_support(
        p, classes, family_centre=centre, family_radius=0.03,
        class_centre=classes, class_radius=0.05,
        fit_family_shares=None, era=OPUS55)
    assert support.supported is False
    assert support.family_within_radius is False
    assert support.family_distance == math.inf
