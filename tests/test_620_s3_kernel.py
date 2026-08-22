"""#620 S3 — the contract extension in the pure diagnosis kernel.

Covers the four additive pieces Task 1 puts into `bin/_lib_diagnosis.py`: the
per-row evidence map, the evaluated-empty verdict and where its early return
sits, the two new withheld causes at their stated precedence positions, and
the registry's growth from four specs to seven.

The last test in the file is the S2 non-regression matrix (acceptance C2).
Its expectations were produced by running the same matrix against the
PRE-CHANGE kernel extracted from git and pasted in as literals; they are
never computed by calling the code under test, because a gate that clones its
result to build its baseline is an arithmetic identity.
"""
from __future__ import annotations

import itertools

import pytest

# conftest puts bin/ on sys.path.
import _lib_diagnosis as k


# --- shared builders ----------------------------------------------------

def _population(*, support_units=40, usd_coverage=1.0, count_coverage=1.0):
    return k.PopulationCoverage(
        requested_start="2026-08-10T00:00:00Z",
        requested_end="2026-08-17T00:00:00Z",
        observed_start="2026-08-10T00:00:00Z",
        observed_end="2026-08-17T00:00:00Z",
        count_coverage=count_coverage,
        usd_coverage=usd_coverage,
        support_units=support_units,
    )


def _blank_population():
    return k.PopulationCoverage(
        requested_start="2026-08-10T00:00:00Z",
        requested_end="2026-08-17T00:00:00Z",
    )


def _denominator_field(shape):
    pop = _population()
    if shape == "withheld":
        return k.withheld(k.WithheldCause.PRICING_UNAVAILABLE.value, pop)
    return k.available(0.0 if shape == "available_zero" else 100.0, pop)


def _denominator(shape="available_positive"):
    return k.Denominator(
        identity="totalExplainedRetainedCost",
        source="claude",
        account_key=None,
        window_start="2026-08-10T00:00:00Z",
        window_end="2026-08-17T00:00:00Z",
        population_digest="d" * 16,
        usd=_denominator_field(shape),
    )


def _available_denominator(usd):
    return k.Denominator(
        identity="totalExplainedRetainedCost",
        source="claude",
        account_key=None,
        window_start="2026-08-10T00:00:00Z",
        window_end="2026-08-17T00:00:00Z",
        population_digest="d" * 16,
        usd=k.available(usd, _population()),
    )


def _withheld_denominator(code):
    return k.Denominator(
        identity="totalExplainedRetainedCost",
        source="claude",
        account_key=None,
        window_start="2026-08-10T00:00:00Z",
        window_end="2026-08-17T00:00:00Z",
        population_digest="d" * 16,
        usd=k.withheld(code, _population()),
    )


def _subjects(shape):
    if shape == "empty":
        return []
    first = k.SubjectFacts(subject_key="a", subject_label="a",
                           observed_usd=50.0, priced_entry_count=40)
    if shape == "single":
        return [first]
    return [first, k.SubjectFacts(subject_key="b", subject_label="b",
                                  observed_usd=10.0, priced_entry_count=10)]


def _row(**overrides):
    kwargs = dict(
        contributor_class="model_mix", subject_kind="model",
        subject_key="k", subject_label="k", rank=None,
        observed_usd=k.available(1.0, _blank_population()),
        share=k.available(1.0, _blank_population()),
        baseline=k.withheld("baseline_insufficient", _blank_population()),
        confidence="high", is_fallback_pricing=False, next_step="x",
    )
    kwargs.update(overrides)
    return k.ContributorRow(**kwargs)


# --- 1.2 the evidence map -----------------------------------------------

def test_contributor_row_evidence_default_is_a_factory_not_a_shared_dict():
    """A bare `= {}` default is rejected by dataclasses at import time, and a
    shared mutable default would leak one row's evidence into the next."""
    a = _row()
    b = _row()
    assert a.evidence == {}
    assert a.evidence is not b.evidence


def test_a_row_built_with_evidence_carries_an_immutable_mapping():
    """The adapter always passes evidence through `classify_class`, which
    wraps it, so the mapping a caller receives cannot be mutated through the
    row."""
    field = k.available(3, _blank_population())
    result = k.classify_class(
        _s3_spec(),
        [k.SubjectFacts(subject_key=k.SUBJECT_CACHE_CHURN,
                        subject_label="Prompt-cache churn",
                        observed_usd=50.0, priced_entry_count=40)],
        _available_denominator(100.0),
        population=_population(),
        evidence={"flaggedTurnCount": field},
    )
    row = result.rows[0]
    assert row.evidence["flaggedTurnCount"] is field
    with pytest.raises(TypeError):
        row.evidence["flaggedTurnCount"] = None


# --- 1.3 the evaluated-empty verdict ------------------------------------

def _s3_spec():
    return k.spec_for("cache_churn")


def test_evaluated_predicate_with_no_matches_is_no_contributor():
    """A predicate that was evaluated over an adequately covered population
    and matched nothing has MEASURED something: the healthy inverse."""
    result = k.classify_class(
        _s3_spec(), subjects=[], denominator=_available_denominator(100.0),
        population=_population(support_units=40, usd_coverage=1.0),
        predicate_evaluated=True,
    )
    assert result.verdict == "no_contributor"
    assert result.code is None
    assert result.rows == ()
    assert result.confidence == "high"


def test_evaluated_empty_still_withholds_when_the_denominator_is_unavailable():
    """The early return must sit AFTER the denominator guards. Returning it
    straight after the support check would call a class with a withheld
    denominator healthy, which is exactly what those guards exist to prevent.
    """
    result = k.classify_class(
        _s3_spec(), subjects=[],
        denominator=_withheld_denominator("pricing_unavailable"),
        population=_population(support_units=40, usd_coverage=1.0),
        predicate_evaluated=True,
    )
    assert result.verdict == "withheld"
    assert result.code == "pricing_unavailable"


def test_evaluated_empty_still_withholds_when_the_denominator_is_zero():
    result = k.classify_class(
        _s3_spec(), subjects=[], denominator=_available_denominator(0.0),
        population=_population(support_units=40, usd_coverage=1.0),
        predicate_evaluated=True,
    )
    assert result.verdict == "withheld"
    assert result.code == "insufficient_population"
    assert result.support_shortfall == k.SUPPORT_SHORTFALL_NO_PRICED_DOLLARS


def test_an_evaluated_empty_class_still_fails_the_priced_entry_minimum():
    """Only the distinct-subject minimum is waived. A class that looked at
    four entries and found nothing has not established a healthy window."""
    result = k.classify_class(
        _s3_spec(), subjects=[], denominator=_available_denominator(100.0),
        population=_population(support_units=4),
        predicate_evaluated=True,
    )
    assert result.verdict == "withheld"
    assert result.support_shortfall == k.SUPPORT_SHORTFALL_PRICED_ENTRIES


def test_an_unevaluated_empty_class_still_fails_the_distinct_subject_minimum():
    result = k.classify_class(
        _s3_spec(), subjects=[], denominator=_available_denominator(100.0),
        population=_population(support_units=40),
    )
    assert result.verdict == "withheld"
    assert result.support_shortfall == k.SUPPORT_SHORTFALL_DISTINCT_SUBJECTS


# --- 1.4 the two new causes ---------------------------------------------

def test_new_causes_sit_at_their_stated_precedence_positions():
    order = list(k.WITHHELD_CAUSE_PRECEDENCE)
    assert order.index("transcripts_not_visible") == \
        order.index("provider_unavailable") + 1
    assert order.index("stale_evidence") < order.index("signal_unavailable")
    assert order.index("signal_unavailable") < order.index(
        "insufficient_population")
    # Existing members keep their relative order.
    s2 = ["provider_unavailable", "retained_range_mismatch",
          "pricing_unavailable", "unattributed_evidence", "stale_evidence",
          "insufficient_population", "calculation_failed"]
    assert [c for c in order if c in s2] == s2


def test_the_s3_constants_and_codes_are_published_from_the_kernel():
    assert k.DIAGNOSIS_SHORT_CONVERSATION_MAX_HUMAN_TURNS == 3
    assert k.DIAGNOSIS_LARGE_CONTEXT_MIN_WINDOW_FRACTION == 0.80
    assert k.DIAGNOSIS_MIN_SUBAGENT_BUCKETS == 2
    assert k.DIAGNOSIS_SEED_SCAN_BUDGET_ROWS == 50_000
    assert k.DIAGNOSIS_CONVERSATION_NORMALIZE_BUDGET_ROWS == 2_000
    assert k.DIAGNOSIS_CODEX_EVENT_SCAN_BUDGET_ROWS == 1_048_576
    assert k.DIAGNOSIS_CODEX_EVENT_SCAN_PER_FILE_ROWS == 4_096
    assert k.GAP_UNKNOWN_CONTEXT_WINDOW == "unknown_context_window"
    assert k.GAP_SCAN_BUDGET_EXHAUSTED == "scan_budget_exhausted"
    assert k.SUBJECT_KIND_QUALIFYING_SET == "qualifying_set"
    assert k.SUBJECT_CACHE_CHURN == "qualifying-set/cache-churn"
    assert k.SUBJECT_SHORT_HIGH_CONTEXT == "qualifying-set/short-high-context"
    assert k.SUBJECT_SUBAGENT_FANOUT == "qualifying-set/subagent-fanout"
    assert k.QUALIFICATION_IDENTIFIABLE_SUBSET == \
        "identifiable_subset_unknown_completeness"
    assert k.QUALIFICATION_PARTIAL_ATTRIBUTION == "partial_attribution"
    assert k.QUALIFICATION_SESSION_LEVEL_CAPACITY == \
        "session_level_context_window"


# --- 1.5 the registry ----------------------------------------------------

def test_registry_grows_to_seven_by_appending():
    kinds = [s.kind for s in k.CONTRIBUTOR_REGISTRY]
    assert kinds == ["model_mix", "project_concentration",
                     "session_concentration", "five_hour_bursts",
                     "cache_churn", "short_high_context", "subagent_fanout"]
    # Appending is what keeps every S2 tie-break where it was.
    for i, kind in enumerate(kinds[:4]):
        assert k.REGISTRY_ORDER[kind] == i


def test_s3_specs_are_set_subject_shaped_and_carry_a_rule():
    for kind in ("cache_churn", "short_high_context", "subagent_fanout"):
        spec = k.spec_for(kind)
        assert spec.min_distinct_subjects == 1
        assert spec.min_priced_entries == 20
        assert spec.subject_kind == k.SUBJECT_KIND_QUALIFYING_SET
        assert spec.rule is not None and spec.rule.sentence
        assert isinstance(spec.rule.parameters, dict)


def test_s2_specs_have_no_rule_so_their_wire_shape_is_unchanged():
    for kind in ("model_mix", "project_concentration",
                 "session_concentration", "five_hour_bursts"):
        assert k.spec_for(kind).rule is None


def test_the_s3_rule_parameters_name_the_published_constants():
    short = k.spec_for("short_high_context").rule.parameters
    assert short["maxHumanTurns"] == \
        k.DIAGNOSIS_SHORT_CONVERSATION_MAX_HUMAN_TURNS
    assert short["minWindowFraction"] == \
        k.DIAGNOSIS_LARGE_CONTEXT_MIN_WINDOW_FRACTION
    assert k.spec_for("subagent_fanout").rule.parameters["minBuckets"] == \
        k.DIAGNOSIS_MIN_SUBAGENT_BUCKETS


# --- 6.5 the S2 non-regression matrix (acceptance C2) --------------------

SUBJECT_SETS = ["empty", "single", "multiple"]
DENOMINATORS = ["available_positive", "available_zero", "withheld"]

# Produced by running this exact matrix against the pre-change kernel at
# 123d48ad4 (`git show 123d48ad4:bin/_lib_diagnosis.py`) and pasting the
# values in. Never recomputed from the code under test.
EXPECTED_S2 = {
    ("model_mix", "empty", "available_positive"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("model_mix", "empty", "available_zero"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("model_mix", "empty", "withheld"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("model_mix", "single", "available_positive"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("model_mix", "single", "available_zero"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("model_mix", "single", "withheld"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("model_mix", "multiple", "available_positive"):
        ("contributor", None, None),
    ("model_mix", "multiple", "available_zero"):
        ("withheld", "insufficient_population", "no_priced_dollars"),
    ("model_mix", "multiple", "withheld"):
        ("withheld", "pricing_unavailable", None),
    ("project_concentration", "empty", "available_positive"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("project_concentration", "empty", "available_zero"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("project_concentration", "empty", "withheld"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("project_concentration", "single", "available_positive"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("project_concentration", "single", "available_zero"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("project_concentration", "single", "withheld"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("project_concentration", "multiple", "available_positive"):
        ("contributor", None, None),
    ("project_concentration", "multiple", "available_zero"):
        ("withheld", "insufficient_population", "no_priced_dollars"),
    ("project_concentration", "multiple", "withheld"):
        ("withheld", "pricing_unavailable", None),
    ("session_concentration", "empty", "available_positive"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("session_concentration", "empty", "available_zero"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("session_concentration", "empty", "withheld"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("session_concentration", "single", "available_positive"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("session_concentration", "single", "available_zero"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("session_concentration", "single", "withheld"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("session_concentration", "multiple", "available_positive"):
        ("contributor", None, None),
    ("session_concentration", "multiple", "available_zero"):
        ("withheld", "insufficient_population", "no_priced_dollars"),
    ("session_concentration", "multiple", "withheld"):
        ("withheld", "pricing_unavailable", None),
    ("five_hour_bursts", "empty", "available_positive"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("five_hour_bursts", "empty", "available_zero"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("five_hour_bursts", "empty", "withheld"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("five_hour_bursts", "single", "available_positive"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("five_hour_bursts", "single", "available_zero"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("five_hour_bursts", "single", "withheld"):
        ("withheld", "insufficient_population", "min_distinct_subjects"),
    ("five_hour_bursts", "multiple", "available_positive"):
        ("contributor", None, None),
    ("five_hour_bursts", "multiple", "available_zero"):
        ("withheld", "insufficient_population", "no_priced_dollars"),
    ("five_hour_bursts", "multiple", "withheld"):
        ("withheld", "pricing_unavailable", None),
}


@pytest.mark.parametrize(
    "spec_kind,subjects_shape,denominator_shape",
    [(s.kind, a, b) for s in k.CONTRIBUTOR_REGISTRY[:4]
     for a, b in itertools.product(SUBJECT_SETS, DENOMINATORS)],
)
def test_s2_classes_unchanged_without_the_flag(spec_kind, subjects_shape,
                                               denominator_shape):
    """Default `predicate_evaluated=False` must reproduce S2 exactly.

    Four classes x three subject shapes x three denominator shapes = 36 cells.
    """
    spec = k.spec_for(spec_kind)
    result = k.classify_class(
        spec, _subjects(subjects_shape), _denominator(denominator_shape),
        population=_population(support_units=40, usd_coverage=1.0),
    )
    assert (result.verdict, result.code, result.support_shortfall) == \
        EXPECTED_S2[(spec_kind, subjects_shape, denominator_shape)]


def test_the_matrix_covers_every_cell_it_claims_to():
    assert len(EXPECTED_S2) == 36
    assert set(EXPECTED_S2) == {
        (spec.kind, a, b) for spec in k.CONTRIBUTOR_REGISTRY[:4]
        for a, b in itertools.product(SUBJECT_SETS, DENOMINATORS)
    }
