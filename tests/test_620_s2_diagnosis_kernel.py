"""#620 S2 — the pure diagnosis kernel.

`bin/_lib_diagnosis.py` holds every type, constant, predicate and the
ranking. It opens no database, renders nothing and reads no clock, so every
classification rule below is directly unit-testable without a store.
"""
from __future__ import annotations

import pytest

# conftest puts bin/ on sys.path.
import _lib_diagnosis as k


def _coverage(*, usd_coverage=1.0, count_coverage=1.0, support_units=100,
              **extra):
    return k.PopulationCoverage(
        requested_start="2026-08-10T00:00:00Z",
        requested_end="2026-08-17T00:00:00Z",
        observed_start="2026-08-10T00:00:00Z",
        observed_end="2026-08-17T00:00:00Z",
        count_coverage=count_coverage,
        usd_coverage=usd_coverage,
        support_units=support_units,
        **extra,
    )


def _denominator(*, usd=100.0, withheld_code=None):
    """`usd` is an `EvidenceField`, so a denominator can be withheld.

    A definite dollar figure printed beside four withheld classes is the
    failure this shape exists to prevent.
    """
    field = (k.withheld(withheld_code, _coverage())
             if withheld_code is not None else k.available(usd, _coverage()))
    return k.Denominator(
        identity="totalExplainedRetainedCost",
        source="claude",
        account_key=None,
        window_start="2026-08-10T00:00:00Z",
        window_end="2026-08-17T00:00:00Z",
        population_digest="d" * 16,
        usd=field,
    )


def _subject(key, *, usd, entries=20, label=None, fallback=False):
    return k.SubjectFacts(
        subject_key=key,
        subject_label=label or key,
        observed_usd=usd,
        priced_entry_count=entries,
        is_fallback_pricing=fallback,
    )


def _classify(spec_index, subjects, denominator, **kw):
    return k.classify_class(
        k.CONTRIBUTOR_REGISTRY[spec_index],
        subjects=subjects,
        denominator=denominator,
        **kw,
    )


def _row(contributor_class, usd, key):
    spec = next(s for s in k.CONTRIBUTOR_REGISTRY if s.kind == contributor_class)
    cov = _coverage()
    return k.ContributorRow(
        contributor_class=contributor_class,
        subject_kind=spec.subject_kind,
        subject_key=key,
        subject_label=key,
        rank=None,
        observed_usd=k.EvidenceField(state="available", value=usd, population=cov),
        share=k.EvidenceField(state="available", value=usd / 100.0, population=cov),
        baseline=k.EvidenceField(
            state="withheld", value=None, population=cov,
            code=k.BaselineOutcome.BASELINE_INSUFFICIENT.value,
        ),
        confidence="high",
        is_fallback_pricing=False,
        next_step="cctally daily",
    )


def _result(contributor_class, verdict, code=None, rows=()):
    return k.ClassResult(
        contributor_class=contributor_class,
        verdict=verdict,
        code=code,
        rows=tuple(rows),
        population=_coverage(),
    )


def _clean(contributor_class):
    return _result(contributor_class, k.VerdictState.NO_CONTRIBUTOR.value)


# --- the registry -------------------------------------------------------

def test_the_registry_is_the_four_accounting_native_classes_in_fixed_order():
    """The four S2 classes keep their positions. #620 S3 APPENDS three
    conversation-derived classes after them, which is what keeps every S2
    tie-break where it was; `tests/test_620_s3_kernel.py` pins the full
    seven-entry order."""
    assert [s.kind for s in k.CONTRIBUTOR_REGISTRY][:4] == [
        "model_mix", "project_concentration",
        "session_concentration", "five_hour_bursts",
    ]


def test_every_spec_publishes_its_support_minima():
    # The four accounting-native classes. The three #620 S3 classes each
    # contribute ONE aggregate subject and take `min_distinct_subjects=1`.
    for spec in k.CONTRIBUTOR_REGISTRY[:4]:
        assert spec.min_distinct_subjects == 2
        assert spec.min_priced_entries == 20
        # A class whose next step names its own subject publishes NO generic
        # template, because only the code that built the subject knows how to
        # name it safely. A template that IS published must be a `cctally`
        # command.
        if spec.next_step_template is not None:
            assert spec.next_step_template.startswith("cctally ")


def test_a_class_with_no_template_prefers_its_subject_own_next_step():
    """The five-hour class is the one whose next step names its subject.

    Both block builders set that override, so the override is the live path;
    the fallback below exists only so a hand-built subject cannot produce an
    empty `-> Run` line.
    """
    spec = k.spec_for("five_hour_bursts")
    assert spec.next_step_template is None
    subject = k.SubjectFacts(
        subject_key="claude|standard|2026-08-09T22:00:00Z",
        subject_label="2026-08-09T22:00:00Z",
        observed_usd=1.0, priced_entry_count=20,
        next_step="cctally five-hour-breakdown --block-start "
                  "2026-08-09T22:00:00Z",
    )
    context = {"source": "claude", "mode_flag": " -m calculate",
               "window_start_date": "2026-08-10",
               "window_end_date": "2026-08-17"}
    assert (k._render_next_step(spec, subject, context)
            == subject.next_step)

    bare = k.SubjectFacts(
        subject_key=subject.subject_key, subject_label=subject.subject_label,
        observed_usd=1.0, priced_entry_count=20,
    )
    fallback = k._render_next_step(spec, bare, context)
    assert fallback.startswith("cctally ")
    # The fallback names the window, never the subject key, so no store value
    # can reach a command line through it.
    assert subject.subject_key not in fallback


def test_the_versioned_constants_are_kernel_resident():
    assert k.DIAGNOSIS_CONTRACT_VERSION == 1
    assert k.DIAGNOSIS_CONTRIBUTOR_SHARE_FLOOR == 0.20
    assert k.DIAGNOSIS_TIE_EPSILON_USD == 1e-9
    assert k.CONFIDENCE_HIGH_MIN_SUPPORT == 20
    assert k.CONFIDENCE_MEDIUM_MIN_SUPPORT == 5
    assert k.CONFIDENCE_MEDIUM_MIN_COVERAGE == 0.80
    assert k.WITHHOLD_MIN_COVERAGE == 0.50
    assert k.WITHHOLD_MIN_SUPPORT == 2


def test_the_kernel_is_pure():
    """No store, no renderer, no clock."""
    import pathlib

    src = pathlib.Path(k.__file__).read_text()
    for banned in ("import sqlite3", "open(", "datetime.now", "utcnow", "print("):
        assert banned not in src, f"the kernel must not contain {banned!r}"


# --- classification -----------------------------------------------------

def test_share_exactly_on_the_floor_is_a_contributor():
    """The boundary is inclusive. A share of exactly 0.20 reports."""
    subjects = [_subject("top", usd=20.0)] + [
        _subject(f"m{i}", usd=16.0) for i in range(5)
    ]
    res = _classify(0, subjects, _denominator(usd=100.0))
    assert res.rows[0].share.value == pytest.approx(0.20)
    assert res.verdict == k.VerdictState.CONTRIBUTOR.value
    assert res.rows[0].subject_key == "top"


def test_a_share_just_below_the_floor_is_no_contributor():
    """The pair with the test above is what makes the boundary inclusive
    rather than merely satisfied."""
    subjects = [_subject("top", usd=19.99)] + [
        _subject(f"m{i}", usd=16.0) for i in range(5)
    ]
    res = _classify(0, subjects, _denominator(usd=100.0))
    assert res.verdict == k.VerdictState.NO_CONTRIBUTOR.value
    assert res.rows == ()


def test_top_share_below_the_floor_is_no_contributor_not_withheld():
    res = _classify(0, [_subject(f"m{i}", usd=10.0) for i in range(10)],
                    _denominator(usd=100.0))
    assert res.verdict == k.VerdictState.NO_CONTRIBUTOR.value
    assert res.code is None


def test_support_below_minimum_withholds_as_insufficient_population():
    res = _classify(0, [_subject("opus", usd=100.0)],   # 1 distinct model
                    _denominator(usd=100.0))
    assert res.verdict == k.VerdictState.WITHHELD.value
    assert res.code == k.WithheldCause.INSUFFICIENT_POPULATION.value


def test_too_few_priced_entries_withholds_even_with_enough_subjects():
    res = _classify(0, [_subject("opus", usd=80.0, entries=5),
                        _subject("haiku", usd=20.0, entries=5)],
                    _denominator(usd=100.0))
    assert res.verdict == k.VerdictState.WITHHELD.value
    assert res.code == k.WithheldCause.INSUFFICIENT_POPULATION.value


def test_usd_coverage_below_the_withhold_floor_withholds():
    res = _classify(0, [_subject("opus", usd=80.0), _subject("haiku", usd=20.0)],
                    _denominator(usd=100.0),
                    population=_coverage(usd_coverage=0.49))
    assert res.verdict == k.VerdictState.WITHHELD.value
    assert res.code == k.WithheldCause.INSUFFICIENT_POPULATION.value


def test_a_preempting_cause_wins_over_the_population_test():
    """The adapter's typed causes are not re-derived by the kernel."""
    res = _classify(0, [], _denominator(usd=0.0),
                    preempting_cause=k.WithheldCause.PROVIDER_UNAVAILABLE.value)
    assert res.verdict == k.VerdictState.WITHHELD.value
    assert res.code == k.WithheldCause.PROVIDER_UNAVAILABLE.value


def test_ties_within_epsilon_all_report_ordered_by_subject_key():
    res = _classify(0, [_subject("zeta", usd=50.0), _subject("alpha", usd=50.0)],
                    _denominator(usd=100.0))
    assert [r.subject_key for r in res.rows[:2]] == ["alpha", "zeta"]
    assert res.verdict == k.VerdictState.CONTRIBUTOR.value


def test_a_share_on_the_floor_computed_as_a_ratio_of_sums_still_reports():
    """The boundary case that a literal `0.2` cannot detect.

    A subject's USD and the denominator are both folds over per-entry costs,
    so a subject genuinely at one fifth of the denominator can compute as
    0.19999999999999998. Both the existing kernel boundary test and the
    `boundary-on-floor` fixture use binary-exact values by design, so neither
    can see this: without float slack at the floor the class flips from a
    measured `contributor` to a healthy `no_contributor`, and the report then
    claims an answer the data does not support.
    """
    import math

    per_entry = 0.00007
    subject_usd = math.fsum([per_entry] * 20)
    total = math.fsum([per_entry] * 100)
    assert subject_usd / total < k.DIAGNOSIS_CONTRIBUTOR_SHARE_FLOOR

    subjects = [_subject("top", usd=subject_usd)] + [
        _subject(f"m{i}", usd=subject_usd) for i in range(4)
    ]
    res = _classify(0, subjects, _denominator(usd=total))
    assert res.verdict == k.VerdictState.CONTRIBUTOR.value


def test_a_withheld_denominator_withholds_every_class_with_its_own_cause():
    """A denominator is an `EvidenceField`, and a withheld one cannot be
    divided by. The class reports the denominator's cause rather than
    inventing a thinner one of its own."""
    res = _classify(
        0, [_subject("opus", usd=80.0), _subject("haiku", usd=20.0)],
        _denominator(withheld_code=k.WithheldCause.PRICING_UNAVAILABLE.value),
    )
    assert res.verdict == k.VerdictState.WITHHELD.value
    assert res.code == k.WithheldCause.PRICING_UNAVAILABLE.value
    assert res.rows == ()


def test_a_denominator_states_whether_it_has_a_value_at_all():
    assert _denominator(usd=12.5).usd_value == 12.5
    assert _denominator(usd=12.5).usd_is_available is True
    withheld_denominator = _denominator(
        withheld_code=k.WithheldCause.RETAINED_RANGE_MISMATCH.value
    )
    assert withheld_denominator.usd_is_available is False
    assert withheld_denominator.usd.value is None
    assert withheld_denominator.usd_value == 0.0


def test_a_zero_denominator_withholds_the_share_rather_than_dividing():
    res = _classify(0, [_subject("opus", usd=0.0), _subject("haiku", usd=0.0)],
                    _denominator(usd=0.0))
    assert res.verdict == k.VerdictState.WITHHELD.value


def test_fallback_pricing_survives_into_the_row():
    res = _classify(0, [_subject("opus", usd=80.0, fallback=True),
                        _subject("haiku", usd=20.0)],
                    _denominator(usd=100.0))
    assert res.rows[0].is_fallback_pricing is True


def test_a_subject_qualification_is_attached_once():
    """A qualification is one statement about the SUBJECT. Attaching the same
    tuple to the observed cost and to the share shipped
    `block_precedes_window` twice per row, which reads as two qualifications
    of two different figures."""
    subject = k.SubjectFacts(
        subject_key="claude|standard|2026-08-09T22:00:00Z",
        subject_label="2026-08-09T22:00:00Z",
        observed_usd=80.0, priced_entry_count=40,
        qualifications=("block_precedes_window",),
    )
    result = _classify(3, [subject, _subject("later", usd=20.0)],
                       _denominator(usd=100.0))
    row = result.rows[0]
    assert row.observed_usd.qualifications == ("block_precedes_window",)
    assert row.share.qualifications == ()


def test_every_reported_row_carries_a_next_step():
    res = _classify(0, [_subject("opus", usd=80.0), _subject("haiku", usd=20.0)],
                    _denominator(usd=100.0),
                    next_step_context={"window_start_date": "2026-08-10",
                                       "window_end_date": "2026-08-17",
                                       "source": "claude"})
    assert res.rows[0].next_step.startswith("cctally ")


# --- confidence ---------------------------------------------------------

def test_confidence_is_computed_on_usd_coverage_not_count_coverage():
    cov = _coverage(usd_coverage=0.40, count_coverage=1.0, support_units=100)
    assert k.assess_confidence(cov) == "low"


def test_complete_coverage_with_full_support_is_high():
    assert k.assess_confidence(_coverage(usd_coverage=1.0, support_units=20)) == "high"


def test_complete_coverage_with_thin_support_is_not_high():
    assert k.assess_confidence(_coverage(usd_coverage=1.0, support_units=19)) == "medium"


def test_medium_needs_both_its_coverage_and_its_support():
    assert k.assess_confidence(
        _coverage(usd_coverage=0.80, support_units=5)) == "medium"
    assert k.assess_confidence(
        _coverage(usd_coverage=0.80, support_units=4)) == "low"
    assert k.assess_confidence(
        _coverage(usd_coverage=0.79, support_units=100)) == "low"


def test_confidence_at_exactly_the_withholding_floor_is_low_not_absent():
    """`WITHHOLD_MIN_COVERAGE` is admitted, and what it is admitted AS matters.

    The nearest existing case used 0.49, which is on the other side of the
    boundary and so proves nothing about the boundary itself.
    """
    assert k.assess_confidence(
        _coverage(usd_coverage=k.WITHHOLD_MIN_COVERAGE)
    ) == "low"


def test_support_is_met_at_exactly_the_withholding_floor():
    spec = k.CONTRIBUTOR_REGISTRY[0]
    subjects = [_subject("opus", usd=80.0), _subject("haiku", usd=20.0)]
    on_floor = _coverage(usd_coverage=k.WITHHOLD_MIN_COVERAGE)
    below = _coverage(usd_coverage=k.WITHHOLD_MIN_COVERAGE - 0.01)
    assert k.support_shortfall(spec, subjects, on_floor) is None
    assert (k.support_shortfall(spec, subjects, below)
            == k.SUPPORT_SHORTFALL_USD_COVERAGE)


def test_the_shortfall_names_which_minimum_was_missed():
    """`insufficient_population` is one cause for four different shortfalls,
    and the support count a surface prints beside it belongs to only one of
    them. Sixty entries under a single model is ample support and still
    withheld, and the cause alone reads as a contradiction."""
    spec = k.CONTRIBUTOR_REGISTRY[0]
    one_subject = [_subject("opus", usd=100.0)]
    two_subjects = [_subject("opus", usd=80.0), _subject("haiku", usd=20.0)]
    assert (k.support_shortfall(spec, one_subject, _coverage(support_units=60))
            == k.SUPPORT_SHORTFALL_DISTINCT_SUBJECTS)
    assert (k.support_shortfall(spec, two_subjects, _coverage(support_units=19))
            == k.SUPPORT_SHORTFALL_PRICED_ENTRIES)
    result = _classify(0, one_subject, _denominator(usd=100.0),
                       population=_coverage(support_units=60))
    assert result.code == k.WithheldCause.INSUFFICIENT_POPULATION.value
    assert result.support_shortfall == k.SUPPORT_SHORTFALL_DISTINCT_SUBJECTS


def test_the_share_floor_epsilon_is_the_one_the_verdict_uses():
    """The published constant must BE the slack the comparison applies, or a
    client recomputing the published rule disagrees with the server on the
    row the slack exists for."""
    top_usd = 19.999999999999996
    subjects = [_subject("opus", usd=top_usd),
                _subject("haiku", usd=19.0),
                _subject("sonnet", usd=19.0)]
    top_share = top_usd / 100.0
    assert top_share < k.DIAGNOSIS_CONTRIBUTOR_SHARE_FLOOR, (
        "the check below is vacuous unless the top share really is under the "
        "bare floor"
    )
    assert top_share >= (k.DIAGNOSIS_CONTRIBUTOR_SHARE_FLOOR
                         - k.DIAGNOSIS_SHARE_FLOOR_EPSILON)
    result = _classify(0, subjects, _denominator(usd=100.0))
    assert result.verdict == k.VerdictState.CONTRIBUTOR.value


def test_a_class_on_the_withholding_floor_still_reports_at_low_confidence():
    """The spec requires a low-confidence result above the floor to stay
    visible and labelled, not to be withheld."""
    res = _classify(0, [_subject("opus", usd=80.0), _subject("haiku", usd=20.0)],
                    _denominator(usd=100.0),
                    population=_coverage(
                        usd_coverage=k.WITHHOLD_MIN_COVERAGE))
    assert res.verdict == k.VerdictState.CONTRIBUTOR.value
    assert res.rows[0].confidence == "low"


def test_absent_usd_coverage_is_low_not_high():
    assert k.assess_confidence(_coverage(usd_coverage=None)) == "low"


# --- ranking ------------------------------------------------------------

def test_ranking_is_deterministic_across_classes():
    rows = k.rank_rows([_row("project_concentration", 5.0, "b"),
                        _row("model_mix", 5.0, "a"),
                        _row("model_mix", 9.0, "c")])
    assert [(r.contributor_class, r.subject_key) for r in rows] == [
        ("model_mix", "c"), ("model_mix", "a"), ("project_concentration", "b"),
    ]


def test_ranking_stamps_one_based_ranks():
    rows = k.rank_rows([_row("model_mix", 5.0, "a"), _row("model_mix", 9.0, "c")])
    assert [r.rank for r in rows] == [1, 2]


# --- the overall verdict ------------------------------------------------

def test_not_applicable_is_excluded_from_completeness():
    """Without this, a provider with a permanently unsupported class could
    never render healthy."""
    verdict, code = k.overall_verdict([
        _clean("model_mix"), _clean("project_concentration"),
        _clean("session_concentration"),
        _result("five_hour_bursts", k.VerdictState.NOT_APPLICABLE.value),
    ])
    assert verdict == "no_contributor_detected"
    assert code is None


def test_any_withheld_applicable_class_withholds_the_overall_verdict():
    verdict, code = k.overall_verdict([
        _clean("model_mix"), _clean("project_concentration"),
        _clean("session_concentration"),
        _result("five_hour_bursts", k.VerdictState.WITHHELD.value,
                code=k.WithheldCause.INSUFFICIENT_POPULATION.value),
    ])
    assert verdict == k.VerdictState.WITHHELD.value
    assert code == k.WithheldCause.INSUFFICIENT_POPULATION.value


def test_a_contributor_beats_a_withheld_sibling_in_the_overall_verdict():
    """A measured contributor is a positive finding; it is not erased by an
    unmeasurable sibling class."""
    verdict, code = k.overall_verdict([
        _result("model_mix", k.VerdictState.CONTRIBUTOR.value,
                rows=[_row("model_mix", 9.0, "c")]),
        _result("project_concentration", k.VerdictState.WITHHELD.value,
                code=k.WithheldCause.STALE_EVIDENCE.value),
    ])
    assert verdict == "contributor_detected"
    assert code is None


def test_the_overall_verdict_discloses_how_many_classes_were_withheld():
    """`contributor_detected` claims only that a contributor was found, so it
    can stand beside a withheld sibling. That makes it an incomplete account,
    and every surface has to say how incomplete."""
    classes = [
        _result("model_mix", k.VerdictState.CONTRIBUTOR.value,
                rows=[_row("model_mix", 9.0, "c")]),
        _result("project_concentration", k.VerdictState.WITHHELD.value,
                code=k.WithheldCause.STALE_EVIDENCE.value),
        _clean("session_concentration"),
        _result("five_hour_bursts", k.VerdictState.NOT_APPLICABLE.value),
    ]
    result = k.build_provider_result(
        "claude", None, None, _denominator(usd=100.0), classes,
    )
    assert result.verdict == "contributor_detected"
    assert result.applicable_class_count == 3
    assert result.withheld_class_count == 1
    report = k.build_report("2026-08-17T00:00:00Z",
                            k.DiagnosisWindow("a", "b", "UTC"), [result])
    assert report.applicable_class_count == 3
    assert report.withheld_class_count == 1


def _withheld_provider(source, code):
    classes = [_result(spec.kind, k.VerdictState.WITHHELD.value, code=code)
               for spec in k.CONTRIBUTOR_REGISTRY]
    return k.build_provider_result(source, None, None,
                                   _denominator(withheld_code=code), classes)


def _healthy_provider(source):
    classes = [_clean(spec.kind) for spec in k.CONTRIBUTOR_REGISTRY]
    return k.build_provider_result(source, None, None,
                                   _denominator(usd=100.0), classes)


def _report(*results):
    return k.build_report("2026-08-17T00:00:00Z",
                          k.DiagnosisWindow("a", "b", "UTC"), list(results))


def test_an_unreadable_store_with_nothing_else_answering_is_terminal():
    """A store that could not be READ is an infrastructure failure, not an
    answer about data availability, so the surfaces report it as a failure."""
    report = _report(_withheld_provider(
        "claude", k.WithheldCause.PROVIDER_UNAVAILABLE.value))
    assert k.unreadable_store_is_terminal(report) is True


def test_another_provider_answering_makes_it_a_report_rather_than_a_failure():
    report = _report(
        _withheld_provider("codex", k.WithheldCause.PROVIDER_UNAVAILABLE.value),
        _healthy_provider("claude"),
    )
    assert k.unreadable_store_is_terminal(report) is False


@pytest.mark.parametrize("code", [
    k.WithheldCause.RETAINED_RANGE_MISMATCH.value,
    k.WithheldCause.STALE_EVIDENCE.value,
    k.WithheldCause.PRICING_UNAVAILABLE.value,
    k.WithheldCause.INSUFFICIENT_POPULATION.value,
])
def test_every_other_withheld_cause_is_an_answer_not_a_failure(code):
    """These four are statements about what the store holds. Reporting them as
    a failure would train a user to ignore the one that is a failure."""
    assert k.unreadable_store_is_terminal(_report(
        _withheld_provider("claude", code))) is False


def test_a_provider_that_measured_any_class_has_answered():
    """The predicate is keyed on whether a provider PRODUCED A MEASUREMENT,
    which is a class-level question. A provider whose overall verdict is
    `withheld` because one class could not be measured still measured the
    other three, and reading the overall verdict instead reported a complete
    answer as an infrastructure failure."""
    partial = k.build_provider_result(
        "claude", None, None, _denominator(usd=100.0),
        [_clean("model_mix"), _clean("project_concentration"),
         _clean("session_concentration"),
         _result("five_hour_bursts", k.VerdictState.WITHHELD.value,
                 code=k.WithheldCause.INSUFFICIENT_POPULATION.value)],
    )
    assert partial.verdict == k.OverallVerdict.WITHHELD.value
    report = _report(
        _withheld_provider("codex", k.WithheldCause.PROVIDER_UNAVAILABLE.value),
        partial,
    )
    assert k.unreadable_store_is_terminal(report) is False


def test_a_provider_whose_every_class_is_withheld_has_not_answered():
    """The other half of the same rule. Nothing was measured, so an unreadable
    sibling store is still the terminal condition."""
    report = _report(
        _withheld_provider("codex", k.WithheldCause.PROVIDER_UNAVAILABLE.value),
        _withheld_provider("claude",
                           k.WithheldCause.INSUFFICIENT_POPULATION.value),
    )
    assert k.unreadable_store_is_terminal(report) is True


def test_a_not_applicable_class_is_not_a_measurement():
    """`not_applicable` says the class does not apply to this provider, which
    is not an answer about the window either."""
    empty = k.build_provider_result(
        "claude", None, None,
        _denominator(withheld_code=k.WithheldCause.PROVIDER_UNAVAILABLE.value),
        [_result(spec.kind, k.VerdictState.NOT_APPLICABLE.value)
         for spec in k.CONTRIBUTOR_REGISTRY],
    )
    report = _report(
        _withheld_provider("codex", k.WithheldCause.PROVIDER_UNAVAILABLE.value),
        empty,
    )
    assert k.unreadable_store_is_terminal(report) is True


def test_the_withheld_code_follows_the_published_precedence():
    verdict, code = k.overall_verdict([
        _result("model_mix", k.VerdictState.WITHHELD.value,
                code=k.WithheldCause.INSUFFICIENT_POPULATION.value),
        _result("project_concentration", k.VerdictState.WITHHELD.value,
                code=k.WithheldCause.PROVIDER_UNAVAILABLE.value),
    ])
    assert verdict == k.VerdictState.WITHHELD.value
    assert code == k.WithheldCause.PROVIDER_UNAVAILABLE.value


# --- the three cause layers --------------------------------------------

def test_establishment_errors_are_not_withheld_causes():
    """The two enums must not overlap; a 503 reason is never an evidence field."""
    assert not ({c.value for c in k.EstablishmentError}
                & {c.value for c in k.WithheldCause})


def test_baseline_outcomes_are_their_own_axis():
    assert not ({c.value for c in k.BaselineOutcome}
                & {c.value for c in k.WithheldCause})
    assert not ({c.value for c in k.BaselineOutcome}
                & {c.value for c in k.EstablishmentError})


def test_the_withheld_precedence_covers_every_cause_exactly_once():
    assert list(k.WITHHELD_CAUSE_PRECEDENCE) == [c.value for c in k.WithheldCause]
    assert len(set(k.WITHHELD_CAUSE_PRECEDENCE)) == len(k.WITHHELD_CAUSE_PRECEDENCE)


def test_the_withheld_causes_are_the_spec_precedence_order():
    # #620 S3 INSERTS `transcripts_not_visible` and `signal_unavailable` at
    # their own precedence positions; `tests/test_620_s3_kernel.py` pins those
    # two. The S2 members keep their relative order, which is what this pins.
    assert [c for c in k.WITHHELD_CAUSE_PRECEDENCE
            if c not in ("transcripts_not_visible", "signal_unavailable")] == [
        "provider_unavailable", "retained_range_mismatch", "pricing_unavailable",
        "unattributed_evidence", "stale_evidence", "insufficient_population",
        "calculation_failed",
    ]


def test_establishment_failure_carries_its_code():
    exc = k.EstablishmentFailure(k.EstablishmentError.GENERATION_INCOHERENT.value,
                                 "components moved twice")
    assert exc.code == "generation_incoherent"


# --- no percentage-point attribution ------------------------------------

def test_the_kernel_names_no_percentage_point_quantity():
    """P1: the diagnosis must never project a weekly ratio onto a slice."""
    import pathlib

    src = pathlib.Path(k.__file__).read_text()
    for banned in ("percentage_point", "percentagePoints", "pct_points"):
        assert banned not in src
