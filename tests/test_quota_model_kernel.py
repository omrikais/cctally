"""#661 S1 — the pure quota-model kernel.

`bin/_lib_quota_model.py` holds every constant, type, predicate and the
statistics. It opens no database, renders nothing and reads no clock, so
every rule below is directly unit-testable without a store.

Each test names the kernel mutation it detects, so Task 12's mutation list
has something to point at.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import statistics

import pytest

# conftest puts bin/ on sys.path.
import _lib_quota_model as qm


# --------------------------------------------------------------------------
# Task 1 — constants payload, fingerprint, t-quantile table
# --------------------------------------------------------------------------
def test_fingerprint_matches_committed_literal():
    # Mutation: any constant value change without re-pinning the literal.
    assert qm.constants_fingerprint() == qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT


def test_fingerprint_is_canonical_sha256_of_the_payload():
    # Mutation: a non-canonical dump (sort_keys dropped, whitespace added).
    blob = json.dumps(
        qm.constants_payload(), sort_keys=True, separators=(",", ":")
    ).encode()
    assert qm.constants_fingerprint() == hashlib.sha256(blob).hexdigest()


def test_payload_carries_no_operator_budget():
    # Mutation: reintroducing BUDGET_CURRENT / WEEKLY_TO_5H into the kernel.
    text = json.dumps(qm.constants_payload())
    for banned in ("budget", "promo", "weeklyTo5h", "2418964", "1612643"):
        assert banned.lower() not in text.lower()


def test_counts_stay_ints_and_thresholds_stay_floats_in_the_payload():
    # `json.dumps(64)` is "64" and `json.dumps(64.0)` is "64.0", so tidying a
    # count to a float silently changes the fingerprint and invalidates every
    # persisted calibration. The fingerprint pin above would catch it as an
    # unexplained drift; this test names the reason.
    payload = qm.constants_payload()
    counts = {
        ("detector", "min_watch_days"), ("detector", "min_daily_gain"),
        ("detector", "max_auto_scan_days"), ("trust", "min_fit_days"),
        ("trust", "min_detect_baseline_days"),
        ("trust", "max_incomplete_baseline_days"),
    }
    thresholds = {
        ("eligibility", "fence_mads"), ("detector", "fence_mads"),
        ("detector", "alpha"), ("trust", "max_fit_width"),
        ("trust", "max_detect_baseline_width"),
        ("trust", "max_detect_watch_width"),
        ("trust", "max_incomplete_baseline_fraction"),
    }
    for table, key in counts:
        assert type(payload[table][key]) is int, (table, key)
    for table, key in thresholds:
        assert type(payload[table][key]) is float, (table, key)
    assert counts | thresholds == {
        (table, key) for table in ("eligibility", "detector", "trust")
        for key in payload[table]
    }


def test_token_class_weights_are_the_spec_values():
    # Mutation: any coefficient edited.
    assert qm.TOKEN_CLASS_WEIGHTS == {
        "fresh": 1.0, "output": 4.73, "cache_1h": 1.03, "cache_read": 0.0031,
    }


def test_every_token_class_coefficient_is_strictly_positive():
    # Positive coefficients make the weighted total strictly increasing in
    # every quantity, so on NON-NEGATIVE input a positive raw total implies a
    # positive weighted one. That is all they establish, and it is why the two
    # limbs of `build_daily_series`' guard read as redundant.
    #
    # They are not redundant, because neither premise the guards actually rest
    # on follows from the coefficients: that every entry's quantities are
    # non-negative, and that no class accumulator overflows. Section 39 pins
    # each of those with its own test in Part V below.
    assert all(w > 0.0 and math.isfinite(w)
               for w in qm.TOKEN_CLASS_WEIGHTS.values())


def test_no_per_family_multiplier_ships():
    # Mutation: restoring MODEL_WEIGHT_DEFAULT = 0.70 — or introducing any
    # other per-family or budget constant under a name a literal list did not
    # happen to enumerate. Checking `hasattr` against five spellings passed
    # for a reintroduced `MODEL_WEIGHTS` or `FAMILY_WEIGHT`, so this scans
    # every name the module exposes instead.
    banned_tokens = ("BUDGET", "PROMO", "WEEKLY_TO_5H", "MODEL_WEIGHT",
                     "FAMILY_WEIGHT", "PER_FAMILY", "MULTIPLIER")
    offenders = sorted(name for name in dir(qm)
                       if any(t in name.upper() for t in banned_tokens))
    assert offenders == []


@pytest.mark.parametrize("df,expected", [(1, 12.706), (2, 4.303), (3, 3.182),
                                         (5, 2.571), (10, 2.228), (30, 2.042)])
def test_t_quantile_table_matches_published_values(df, expected):
    # Mutation: a transcription error in the frozen table.
    assert qm.t_quantile_975(df) == pytest.approx(expected, abs=0.001)


def test_t_quantile_above_table_uses_normal():
    # Mutation: clamping to the df=100 entry instead of the normal limit.
    assert qm.t_quantile_975(101) == pytest.approx(
        statistics.NormalDist().inv_cdf(0.975), abs=1e-9
    )


def test_t_quantile_rejects_non_positive_df():
    # Mutation: silently returning the normal quantile at df <= 0.
    with pytest.raises(ValueError):
        qm.t_quantile_975(0)


# --------------------------------------------------------------------------
# Task 2 — enums, precedence, exit mapping, CalibrationEvidence
# --------------------------------------------------------------------------
def test_precedence_is_a_total_order_over_every_status():
    # Mutation: dropping a member from STATUS_PRECEDENCE.
    assert set(qm.STATUS_PRECEDENCE) == set(qm.CalibrationStatus)
    assert len(qm.STATUS_PRECEDENCE) == len(set(qm.STATUS_PRECEDENCE))


def test_precedence_order_is_the_spec_order():
    # Mutation: reordering two neighbours, which worst_status would hide
    # unless the order itself is pinned.
    assert [s.value for s in qm.STATUS_PRECEDENCE] == [
        "unavailable", "future", "stale", "token-split-unknown",
        "local-history-incomplete", "unsupported-model-mix",
        "unvalidated-coefficient-era", "insufficient-history",
        "fragmented-history", "unstable-fit", "ok",
    ]


def test_every_status_has_exactly_one_exit_code():
    assert set(qm.STATUS_EXIT) == set(qm.CalibrationStatus)


def test_exit_mapping_matches_the_contract():
    # Mutation: moving a status between the 3 and 4 families.
    E = qm.STATUS_EXIT
    S = qm.CalibrationStatus
    assert E[S.OK] == 0
    for s in (S.INSUFFICIENT_HISTORY, S.FRAGMENTED_HISTORY, S.UNSTABLE_FIT):
        assert E[s] == 4
    for s in (S.LOCAL_HISTORY_INCOMPLETE, S.UNSUPPORTED_MODEL_MIX,
              S.UNVALIDATED_COEFFICIENT_ERA, S.TOKEN_SPLIT_UNKNOWN,
              S.STALE, S.FUTURE, S.UNAVAILABLE):
        assert E[s] == 3
    assert 2 not in E.values()


def test_unavailable_outranks_every_other_status():
    for s in qm.CalibrationStatus:
        assert qm.worst_status([s, qm.CalibrationStatus.UNAVAILABLE]) is \
            qm.CalibrationStatus.UNAVAILABLE


def test_ok_loses_to_every_other_status():
    for s in qm.CalibrationStatus:
        if s is not qm.CalibrationStatus.OK:
            assert qm.worst_status([qm.CalibrationStatus.OK, s]) is s


def test_worst_status_of_nothing_is_ok():
    assert qm.worst_status([]) is qm.CalibrationStatus.OK


def test_withholding_causes_are_the_closed_set():
    assert {c.value for c in qm.WithholdingCause} == {
        "no-local-history", "sparse-local-history", "transition-day",
        "right-censored", "token-split-unknown", "unsupported-composition",
    }


def test_verdicts_are_the_closed_set():
    assert {v.value for v in qm.Verdict} == {
        "rate-change-detected", "no-rate-change", "withheld",
    }


def test_withheld_evidence_carries_a_code_and_no_value():
    ev = qm.evidence_withheld("no-local-history", {"days": 0})
    assert ev.value is None and ev.interval is None
    assert ev.code == "no-local-history"
    assert ev.state == "withheld"
    # Mutation: adding a confidence field. Part I never defined what one
    # would mean here, and an undefined confidence is worse than none.
    assert not hasattr(ev, "confidence")


def test_withheld_evidence_accepts_a_typed_cause_and_stores_its_value():
    ev = qm.evidence_withheld(qm.WithholdingCause.TRANSITION_DAY, {"days": 0})
    assert ev.code == "transition-day"
    # Mutation: storing the enum member itself. `isinstance` would pass on a
    # str enum, so the normalization has to be pinned on the exact type.
    assert type(ev.code) is str


def test_available_evidence_carries_no_code():
    ev = qm.evidence_available(
        1.0, qm.Interval(0.9, 1.1), qm.Support(5, 1), {"days": 5}
    )
    assert ev.code is None and ev.state == "available"
    assert ev.interval.lo == 0.9 and ev.interval.hi == 1.1
    assert ev.support.days == 5 and ev.support.segments == 1
    assert ev.qualifications == ()


# --------------------------------------------------------------------------
# Task 3 — model identity, participation, token weighting, the ceiling rule
# --------------------------------------------------------------------------
def _entry(**kw):
    base = dict(at=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
                model="claude-opus-5", fresh=0, output=0,
                cache_create_total=0, cache_1h=0, cache_read=0)
    base.update(kw)
    return qm.EntryRecord(**base)


def test_uppercase_provider_prefix_is_stripped():
    # Mutation: stripping the prefix before case-folding, which leaves
    # `ANTHROPIC/...` unstripped because the pricing helper matches only the
    # lowercase forms.
    assert qm.normalize_family("ANTHROPIC/Claude-Opus-5") == "claude-opus-5"
    assert qm.normalize_family("Anthropic.Claude-Opus-5") == "claude-opus-5"


def test_dated_suffix_is_removed_only_when_anchored():
    # Mutation: an unanchored or wrong-width date pattern.
    assert qm.normalize_family("claude-opus-5-20260725") == "claude-opus-5"
    assert qm.normalize_family("claude-opus-5-2026072") != "claude-opus-5"


def test_unknown_family_is_none_not_a_default_weight():
    # Mutation: restoring MODEL_WEIGHT_DEFAULT = 0.70 for anything unknown.
    assert qm.normalize_family("some-other-vendor-model") is None
    assert qm.normalize_family("") is None


def test_participation_of_an_unrecognized_family_is_unknown():
    assert qm.family_participation(None) == "unknown"
    assert qm.family_participation("some-other-vendor-model") == "unknown"
    assert qm.family_participation("claude-opus-5") == "general"


def test_mythos_drains_the_general_weekly_quota():
    # Section 26. Mutation: classifying Mythos "unknown", which withholds
    # every day containing any Mythos usage and gives a Mythos-primary user a
    # permanently withheld verdict.
    assert qm.family_participation("claude-mythos-5") == "general"
    assert qm.normalize_family("claude-mythos-preview") == "claude-mythos-5"


def test_mythos_participation_carries_its_operator_provenance():
    # Mutation: promoting Mythos to "general" without recording why, which is
    # the same unsourced default the classification catalogue exists to stop.
    note = qm.FAMILY_PROVENANCE["claude-mythos-5"]
    assert "operator-supplied" in note
    assert "dedicated" in note


def test_a_day_of_only_mythos_usage_is_not_withheld():
    # The observable consequence: before section 26 this day carried
    # UNSUPPORTED_COMPOSITION and never reached any fit.
    snaps = [_snap(1, 10.0), _snap(5, 20.0)]
    segs = qm.build_segments(snaps, [])
    e = qm.EntryRecord(at=DAY.replace(hour=3), model="claude-mythos-5",
                       fresh=1_000_000, output=0, cache_create_total=0,
                       cache_1h=0, cache_read=0)
    obs = qm.build_daily_series(segs, [e], now=DAY + dt.timedelta(days=2),
                                newest_entry_at=DAY + dt.timedelta(days=2))
    assert [o.cause for o in obs] == [None]
    assert obs[0].family_shares == {"claude-mythos-5": pytest.approx(1.0)}


def test_integer_percent_uses_the_epsilon_floor():
    # Mutation: bare int() or bare math.floor(). 0.57 * 100 is
    # 56.99999999999999, which bare flooring reads as 56.
    assert qm.integer_percent(57.99999999999999) == 58
    assert qm.integer_percent(57.4) == 57
    assert qm.integer_percent(0.57 * 100) == 57


def test_capped_reading_is_right_censored():
    # Mutation: returning 99.5, which invents a finite value the observation
    # does not supply.
    assert qm.true_percent_point(100) is None
    assert qm.true_percent_interval(100) == (99.0, None)


def test_low_readings_use_their_documented_midpoints():
    # Mutation: applying `shown - 0.5` uniformly, which gives -0.5 at 0.
    assert qm.true_percent_point(0) == 0.25
    assert qm.true_percent_point(1) == 0.75
    assert qm.true_percent_point(2) == 1.5
    assert qm.true_percent_point(99) == 98.5


def test_reading_intervals_are_the_documented_half_open_ranges():
    assert qm.true_percent_interval(0) == (0.0, 0.5)
    assert qm.true_percent_interval(1) == (0.5, 1.0)
    assert qm.true_percent_interval(2) == (1.0, 2.0)


def test_null_split_with_positive_cache_create_is_unknown():
    # Mutation: coalesce(cache_1h, 0), which reads "split unknown" as "zero
    # one-hour writes" and silently under-weights the day.
    assert qm.token_class_units(
        _entry(cache_create_total=10, cache_1h=None)
    ) is None


def test_null_split_with_zero_cache_create_is_zero_not_unknown():
    got = qm.token_class_units(_entry(cache_create_total=0, cache_1h=None))
    assert got is not None and got["cache_1h"] == 0.0


def test_five_minute_quantity_is_the_remainder_and_is_weighted_as_fresh():
    got = qm.token_class_units(_entry(cache_create_total=100, cache_1h=40))
    assert got["fresh"] == 60.0 and got["cache_1h"] == 40.0


def test_weighted_units_applies_each_class_coefficient():
    # Mutation: a dropped term or a swapped coefficient.
    e = _entry(fresh=1000, output=1000, cache_create_total=1000,
               cache_1h=1000, cache_read=1000)
    assert qm.weighted_units(e) == pytest.approx(
        1000 * 1.0 + 1000 * 4.73 + 1000 * 1.03 + 1000 * 0.0031
    )


def test_a_non_finite_token_quantity_is_an_unusable_split():
    # Section 27: `float('nan')` passes every `<= 0` guard and fails every
    # `>` guard, so it flowed straight through the weighting.
    assert qm.token_class_units(_entry(fresh=float("nan"))) is None
    assert qm.token_class_units(_entry(output=float("inf"))) is None
    assert qm.token_class_units(_entry(cache_read=float("-inf"))) is None
    assert qm.token_class_units(
        _entry(cache_create_total=float("nan"))) is None
    assert qm.token_class_units(
        _entry(cache_create_total=10, cache_1h=float("nan"))) is None
    assert qm.weighted_units(_entry(fresh=float("nan"))) is None


def test_weighted_units_is_none_when_the_split_is_unknown():
    assert qm.weighted_units(
        _entry(cache_create_total=10, cache_1h=None)
    ) is None


# --------------------------------------------------------------------------
# Task 4 — credit-aware segmentation and daily-observation construction
# --------------------------------------------------------------------------
UTC = dt.timezone.utc
DAY = dt.datetime(2026, 8, 1, tzinfo=UTC)


def _snap(hour, percent, week_start=DAY, source="api"):
    return qm.SnapshotRecord(DAY.replace(hour=hour), week_start, percent, source)


def test_week_anchor_rounds_to_the_nearest_hour():
    # Mutation: flooring to the hour, which separates 07:59 from its own week.
    A = dt.datetime(2026, 2, 5, 7, 59, tzinfo=UTC)
    B = dt.datetime(2026, 2, 5, 8, 28, tzinfo=UTC)
    expected = dt.datetime(2026, 2, 5, 8, 0, tzinfo=UTC)
    assert qm.canonical_week_anchor(A) == expected
    assert qm.canonical_week_anchor(B) == expected


def test_a_percentage_decrease_alone_never_forks_a_segment():
    # Mutation: restoring the research script's decrease-inferred fork. The
    # measured store holds 30 bare decreases against 10 authoritative credits.
    snaps = [_snap(h, p) for h, p in ((1, 10.0), (2, 20.0), (3, 5.0))]
    assert len(qm.build_segments(snaps, [])) == 1


def test_an_authoritative_reset_forks_and_reanchors():
    snaps = [_snap(h, p) for h, p in ((1, 10.0), (5, 20.0))]
    cr = [qm.CreditRecord(DAY.replace(hour=3), "reset")]
    segs = qm.build_segments(snaps, cr)
    assert len(segs) == 2 and segs[1].restarted is True
    assert segs[1].week_anchor == DAY.replace(hour=3)


def test_a_floor_credit_forks_without_reanchoring_the_week():
    # `record-credit` deliberately does not re-anchor, unlike a >=25pp
    # auto-credit. Mutation: treating both credit paths the same.
    #
    # The two rows carry DIFFERENT raw week-start spellings that canonicalize
    # to one anchor, and that anchor is neither the module default nor the
    # hour the credit instant rounds to. Built with the same default
    # `week_start` on both rows the assertion held whichever behaviour the
    # implementation had.
    jitter_a = dt.datetime(2026, 7, 28, 7, 59, tzinfo=UTC)
    jitter_b = dt.datetime(2026, 7, 28, 8, 28, tzinfo=UTC)
    expected = dt.datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
    credit_at = DAY.replace(hour=3)
    snaps = [qm.SnapshotRecord(DAY.replace(hour=1), jitter_a, 10.0, "api", 1),
             qm.SnapshotRecord(DAY.replace(hour=5), jitter_b, 20.0, "api", 2)]
    segs = qm.build_segments(snaps, [qm.CreditRecord(credit_at, "floor")])
    assert len(segs) == 2
    assert segs[1].restarted is False
    assert segs[0].week_anchor == expected
    assert segs[1].week_anchor == expected
    assert segs[1].week_anchor != qm.canonical_week_anchor(credit_at)
    assert expected != DAY


def test_the_snapshot_tie_break_uses_the_raw_percent():
    # Section 27: the kernel sorted by the FLOORED integer percent, so two
    # rows at one instant reading 10.2 and 10.7 both floored to 10, tied, and
    # fell through to rowid. Both floor to the same integer, so the resulting
    # order is the only thing that can distinguish the two keys — and the
    # rowids are arranged so the floored key would give the OPPOSITE order.
    at = DAY.replace(hour=1)
    low = qm.SnapshotRecord(at, DAY, 10.2, "api", 1)
    high = qm.SnapshotRecord(at, DAY, 10.7, "api", 2)
    segs = qm.build_segments([low, high], [])
    assert [r.percent for r in segs[0].rows] == [10.7, 10.2]
    assert qm.integer_percent(10.2) == qm.integer_percent(10.7) == 10


def test_the_snapshot_tie_break_falls_through_to_ascending_rowid():
    # The final total-order guarantee: equal instant AND equal percent.
    at = DAY.replace(hour=1)
    first = qm.SnapshotRecord(at, DAY, 10.5, "api", 7)
    second = qm.SnapshotRecord(at, DAY, 10.5, "api", 3)
    segs = qm.build_segments([first, second], [])
    assert [r.rowid for r in segs[0].rows] == [3, 7]


def test_a_reset_credit_straddling_the_rounding_boundary_forks_once():
    # Section 27. A reset anchors the successor from the credit instant while
    # the next row is compared against its OWN canonical anchor, so a credit
    # at 03:29 (rounding down to 03:00) with post-reset rows recording 03:31
    # (rounding up to 04:00) forked a spurious third segment that also lost
    # `restarted`. Section 2 measured a 29-minute spread of anchor spellings,
    # so straddling the boundary is realistic.
    old_week = dt.datetime(2026, 7, 25, 8, 0, tzinfo=UTC)
    new_week = dt.datetime(2026, 8, 1, 3, 31, tzinfo=UTC)
    credit_at = dt.datetime(2026, 8, 1, 3, 29, tzinfo=UTC)
    assert qm.canonical_week_anchor(new_week) != \
        qm.canonical_week_anchor(credit_at)
    rows = [qm.SnapshotRecord(dt.datetime(2026, 8, 1, 1, tzinfo=UTC),
                              old_week, 40.0, "api", 1)]
    rows += [qm.SnapshotRecord(dt.datetime(2026, 8, 1, h, tzinfo=UTC),
                               new_week, p, "api", n + 2)
             for n, (h, p) in enumerate(((4, 3.0), (5, 6.0), (6, 9.0)))]
    segs = qm.build_segments(rows, [qm.CreditRecord(credit_at, "reset")])
    assert len(segs) == 2
    assert segs[1].restarted is True
    assert segs[1].week_anchor == dt.datetime(2026, 8, 1, 3, tzinfo=UTC)
    assert len(segs[1].rows) == 3


def test_a_reset_followed_by_a_floor_between_two_rows_still_reanchors():
    # Section 27: the loop kept only the LAST crossed credit, so a reset
    # followed by a floor was filed as a floor and the week was not
    # re-anchored — which section 5 keeps deliberately distinct.
    rows = [_snap(1, 40.0), _snap(5, 6.0)]
    credits = [qm.CreditRecord(DAY.replace(hour=2), "reset"),
               qm.CreditRecord(DAY.replace(hour=3), "floor")]
    segs = qm.build_segments(rows, credits)
    assert len(segs) == 2
    assert segs[1].restarted is True
    assert segs[1].week_anchor == DAY.replace(hour=2)


def test_a_floor_followed_by_a_reset_reanchors_from_the_reset():
    # The mirror order, so the fix cannot be "always take the first credit".
    rows = [_snap(1, 40.0), _snap(5, 6.0)]
    credits = [qm.CreditRecord(DAY.replace(hour=2), "floor"),
               qm.CreditRecord(DAY.replace(hour=3), "reset")]
    segs = qm.build_segments(rows, credits)
    assert len(segs) == 2
    assert segs[1].restarted is True
    assert segs[1].week_anchor == DAY.replace(hour=3)


def test_two_floor_credits_between_two_rows_fork_once_without_reanchoring():
    rows = [_snap(1, 40.0), _snap(5, 6.0)]
    credits = [qm.CreditRecord(DAY.replace(hour=2), "floor"),
               qm.CreditRecord(DAY.replace(hour=3), "floor")]
    segs = qm.build_segments(rows, credits)
    assert len(segs) == 2
    assert segs[1].restarted is False
    assert segs[1].week_anchor == segs[0].week_anchor


def test_a_new_week_anchor_forks_a_segment():
    later = DAY + dt.timedelta(days=7)
    snaps = [_snap(1, 10.0), qm.SnapshotRecord(later, later, 3.0, "api")]
    assert len(qm.build_segments(snaps, [])) == 2


def test_a_date_crossing_a_segment_boundary_is_a_transition_day():
    snaps = [_snap(h, p) for h, p in ((1, 10.0), (2, 12.0), (5, 3.0), (6, 6.0))]
    cr = [qm.CreditRecord(DAY.replace(hour=4), "reset")]
    segs = qm.build_segments(snaps, cr)
    series = qm.build_daily_series(segs, [], now=DAY + dt.timedelta(days=2),
                                   newest_entry_at=DAY + dt.timedelta(days=2))
    same_date = [o for o in series if o.date == DAY.date()]
    # Mutation: emitting one row per (segment, date), which lets the
    # consecutive-run counter count one calendar day twice.
    assert len(same_date) == 1
    assert same_date[0].cause == qm.WithholdingCause.TRANSITION_DAY
    assert [o for o in series if o.date == DAY.date() and o.cause is None] == []


def test_an_incomplete_tail_day_is_local_history_incomplete():
    # Mutation: dropping the newest-entry condition, which makes a day whose
    # ingest has not finished look like a day the meter moved with no work.
    # The day carries REAL units, so the incomplete ingest tail is the only
    # thing that can withhold it — a zero-unit day would reach the same cause
    # by the other branch and leave the mutation invisible.
    snaps = [_snap(h, p) for h, p in ((1, 10.0), (20, 20.0))]
    segs = qm.build_segments(snaps, [])
    inside = qm.EntryRecord(at=DAY.replace(hour=3), model="claude-opus-5",
                            fresh=1_000_000, output=0, cache_create_total=0,
                            cache_1h=0, cache_read=0)
    series = qm.build_daily_series(segs, [inside],
                                   now=DAY + dt.timedelta(days=2),
                                   newest_entry_at=DAY.replace(hour=12))
    today = [o for o in series if o.date == DAY.date()]
    assert [o.cause for o in today] == [qm.WithholdingCause.NO_LOCAL_HISTORY]
    assert today[0].units == pytest.approx(1_000_000.0)


def test_a_day_the_clock_has_not_passed_is_incomplete():
    assert qm.day_is_complete(DAY.date(), now=DAY.replace(hour=23),
                              newest_entry_at=DAY + dt.timedelta(days=3)) is False
    assert qm.day_is_complete(DAY.date(), now=DAY + dt.timedelta(days=1),
                              newest_entry_at=DAY + dt.timedelta(days=1)) is True


def test_tokens_at_the_closing_instant_belong_to_the_next_observation():
    # Mutation: an inclusive [first, last] interval, which double-counts the
    # closing instant across two consecutive observations.
    close = DAY.replace(hour=5)
    snaps = [_snap(1, 10.0), qm.SnapshotRecord(close, DAY, 20.0, "api")]
    segs = qm.build_segments(snaps, [])
    at_close = qm.EntryRecord(at=close, model="claude-opus-5", fresh=1_000_000,
                              output=0, cache_create_total=0, cache_1h=0,
                              cache_read=0)
    series = qm.build_daily_series(segs, [at_close],
                                   now=DAY + dt.timedelta(days=2),
                                   newest_entry_at=DAY + dt.timedelta(days=2))
    assert [o for o in series if o.date == DAY.date()][0].units == 0.0


def test_an_entry_inside_the_interval_is_counted_with_its_shares():
    inside = DAY.replace(hour=3)
    snaps = [_snap(1, 10.0), _snap(5, 20.0)]
    segs = qm.build_segments(snaps, [])
    e = qm.EntryRecord(at=inside, model="claude-opus-5", fresh=1_000_000,
                       output=0, cache_create_total=0, cache_1h=0, cache_read=0)
    obs = [o for o in qm.build_daily_series(
        segs, [e], now=DAY + dt.timedelta(days=2),
        newest_entry_at=DAY + dt.timedelta(days=2)) if o.date == DAY.date()][0]
    assert obs.cause is None
    assert obs.units == pytest.approx(1_000_000.0)
    assert obs.family_shares == {"claude-opus-5": pytest.approx(1.0)}
    assert obs.class_shares["fresh"] == pytest.approx(1.0)
    # Mutation: the meter delta taken from raw readings rather than the
    # interval midpoints. Raw would be 10; corrected is 19.5 - 9.5.
    assert obs.meter_delta == pytest.approx(10.0)


def test_a_capped_reading_makes_the_day_right_censored():
    snaps = [_snap(1, 90.0), _snap(5, 100.0)]
    segs = qm.build_segments(snaps, [])
    series = qm.build_daily_series(segs, [], now=DAY + dt.timedelta(days=2),
                                   newest_entry_at=DAY + dt.timedelta(days=2))
    assert [o.cause for o in series] == [qm.WithholdingCause.RIGHT_CENSORED]


def test_an_unknown_token_split_inside_the_interval_withholds_the_day():
    snaps = [_snap(1, 10.0), _snap(5, 20.0)]
    segs = qm.build_segments(snaps, [])
    e = qm.EntryRecord(at=DAY.replace(hour=3), model="claude-opus-5",
                       fresh=1_000_000, output=0, cache_create_total=10,
                       cache_1h=None, cache_read=0)
    series = qm.build_daily_series(segs, [e], now=DAY + dt.timedelta(days=2),
                                   newest_entry_at=DAY + dt.timedelta(days=2))
    assert [o.cause for o in series] == [qm.WithholdingCause.TOKEN_SPLIT_UNKNOWN]


def test_an_unclassified_family_makes_the_composition_unsupported():
    snaps = [_snap(1, 10.0), _snap(5, 20.0)]
    segs = qm.build_segments(snaps, [])
    e = qm.EntryRecord(at=DAY.replace(hour=3), model="some-other-vendor-model",
                       fresh=1_000_000, output=0, cache_create_total=0,
                       cache_1h=0, cache_read=0)
    series = qm.build_daily_series(segs, [e], now=DAY + dt.timedelta(days=2),
                                   newest_entry_at=DAY + dt.timedelta(days=2))
    assert [o.cause for o in series] == \
        [qm.WithholdingCause.UNSUPPORTED_COMPOSITION]


def test_a_non_finite_entry_withholds_its_day_with_a_typed_cause():
    # Section 27: the day was emitted with NO withholding cause at all.
    snaps = [_snap(1, 10.0), _snap(5, 20.0)]
    segs = qm.build_segments(snaps, [])
    e = qm.EntryRecord(at=DAY.replace(hour=3), model="claude-opus-5",
                       fresh=float("nan"), output=0, cache_create_total=0,
                       cache_1h=0, cache_read=0)
    series = qm.build_daily_series(segs, [e], now=DAY + dt.timedelta(days=2),
                                   newest_entry_at=DAY + dt.timedelta(days=2))
    assert [o.cause for o in series] == \
        [qm.WithholdingCause.TOKEN_SPLIT_UNKNOWN]


def test_a_non_finite_day_never_lets_the_analysis_report_ok():
    series = _spread(10e6, 12)
    series[5] = _obs(10, float("nan"), 5)
    a = _analyse(series)
    assert a.status is not qm.CalibrationStatus.OK
    assert a.fitted.state == "withheld"
    assert a.fitted.value is None


def test_a_meter_gain_of_exactly_two_true_points_is_retained():
    # Section 5's minimum daily gain is two TRUE points. Readings of 10 and
    # 12 give midpoints 9.5 and 11.5, a delta of exactly 2.0, which binary
    # floating point represents exactly — so this pins the boundary at its
    # value rather than near it. A `<` to `<=` mutation drops the day.
    segs = qm.build_segments([_snap(1, 10.0), _snap(5, 12.0)], [])
    kept = qm.build_daily_series(segs, [], now=DAY + dt.timedelta(days=2),
                                 newest_entry_at=DAY + dt.timedelta(days=2))
    assert [o.date for o in kept] == [DAY.date()]
    assert kept[0].meter_delta == 2.0
    # One reading lower is 1.0 and is absent from the series entirely.
    below = qm.build_segments([_snap(1, 10.0), _snap(5, 11.0)], [])
    assert qm.build_daily_series(
        below, [], now=DAY + dt.timedelta(days=2),
        newest_entry_at=DAY + dt.timedelta(days=2)) == []


def test_a_day_the_meter_barely_moved_is_absent_from_the_series():
    # "Consecutive" means consecutive eligible days in the series, so a day
    # below the minimum gain must not occupy a slot in it.
    snaps = [_snap(1, 10.0), _snap(5, 11.0)]
    segs = qm.build_segments(snaps, [])
    assert qm.build_daily_series(segs, [], now=DAY + dt.timedelta(days=2),
                                 newest_entry_at=DAY + dt.timedelta(days=2)) == []


# --------------------------------------------------------------------------
# Task 5 — eligibility fence and the two composition support radii
# --------------------------------------------------------------------------
def _smad(xs):
    """Scaled MAD computed independently of the kernel."""
    med = statistics.median(xs)
    return 1.4826 * statistics.median([abs(x - med) for x in xs])


def test_scaled_mad_uses_the_1_4826_constant():
    # Mutation: dropping the scale factor, or using a standard deviation.
    assert qm.scaled_mad([1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.4826 * 1.0)


def test_zero_mad_makes_the_fence_undefined_rather_than_the_median():
    # Mutation: collapsing a zero scaled MAD to the median, which would flag
    # every above-median day.
    assert qm.eligibility_fence([1.0, 1.0, 1.0, 1.0, 1.0]) is None


def test_eligibility_fence_is_the_expm1_of_the_log1p_lower_fence():
    # Mutation: a sign flip, a wrong fence multiple, or a fence taken on the
    # raw units rather than on log1p.
    units = [1.0, 10.0, 100.0, 1000.0, 10000.0]
    xs = [math.log1p(u) for u in units]
    expected = math.expm1(statistics.median(xs) - 3.0 * _smad(xs))
    assert qm.eligibility_fence(units) == pytest.approx(expected)


def test_the_fence_population_excludes_non_positive_days():
    # Mutation: including zero-unit days, which drags log1p toward 0.
    positive = [1.0, 10.0, 100.0, 1000.0, 10000.0]
    xs = [math.log1p(u) for u in positive]
    expected = math.expm1(statistics.median(xs) - 3.0 * _smad(xs))
    assert qm.eligibility_fence([0.0] + positive) == pytest.approx(expected)


def test_a_fence_over_fewer_than_two_days_is_undefined():
    assert qm.eligibility_fence([5.0]) is None
    assert qm.eligibility_fence([]) is None


def test_componentwise_median_is_renormalised_to_a_distribution():
    # Mutation: dropping the renormalization. The componentwise medians here
    # are 0.4, 0.4, 0.4, which sum to 1.2 and are not a distribution.
    v = [{"a": 0.6, "b": 0.4, "c": 0.0},
         {"a": 0.4, "b": 0.0, "c": 0.6},
         {"a": 0.0, "b": 0.6, "c": 0.4}]
    c = qm.composition_centre(v)
    assert sum(c.values()) == pytest.approx(1.0)
    assert c["a"] == pytest.approx(1.0 / 3.0)


def test_all_zero_componentwise_median_has_no_centre():
    assert qm.composition_centre([{"a": 0.0}, {"a": 0.0}]) is None
    assert qm.composition_centre([]) is None


def test_total_variation_distance_is_half_the_absolute_difference():
    # Mutation: dropping the 0.5 factor, which doubles every distance and
    # makes the radius test reject supported days.
    assert qm.tv_distance({"a": 0.8, "b": 0.2}, {"a": 1.0}) == pytest.approx(0.2)
    assert qm.tv_distance({"a": 1.0}, {"a": 1.0}) == pytest.approx(0.0)


def test_support_radius_is_the_median_distance_plus_three_scaled_mads():
    centre = {"a": 1.0}
    vectors = [{"a": 1.0}, {"a": 0.8, "b": 0.2},
               {"a": 0.6, "b": 0.4}, {"a": 0.9, "b": 0.1}]
    d = [0.0, 0.2, 0.4, 0.1]
    assert qm.support_radius(vectors, centre) == pytest.approx(
        statistics.median(d) + 3.0 * _smad(d)
    )


def test_a_day_outside_either_radius_is_unsupported():
    # Mutation: requiring only the family radius. Removing per-family
    # multipliers left the token-class coefficients as the remaining
    # unvalidated assumption, so both axes are required.
    fam_c, cls_c = {"x": 1.0}, {"fresh": 1.0}
    assert qm.is_supported({"x": 0.0}, {"fresh": 1.0},
                           fam_c, 0.1, cls_c, 0.1) is False
    assert qm.is_supported({"x": 1.0}, {"fresh": 0.0, "output": 1.0},
                           fam_c, 0.1, cls_c, 0.1) is False


def test_a_day_inside_both_radii_is_supported():
    assert qm.is_supported({"x": 1.0}, {"fresh": 1.0},
                           {"x": 1.0}, 0.1, {"fresh": 1.0}, 0.1) is True


def test_an_undefined_centre_or_radius_makes_a_day_unsupported():
    # Mutation: treating a missing centre as "everything is supported".
    assert qm.is_supported({"x": 1.0}, {"fresh": 1.0},
                           None, 0.1, {"fresh": 1.0}, 0.1) is False
    assert qm.is_supported({"x": 1.0}, {"fresh": 1.0},
                           {"x": 1.0}, None, {"fresh": 1.0}, 0.1) is False


# --------------------------------------------------------------------------
# Task 6 — the fit, the exact rank-sum detector, Holm, classification
# --------------------------------------------------------------------------
NOW = dt.datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
OBS_EPOCH = dt.date(2026, 7, 1)


def _obs(meter_delta, units, index=0, *, segment_id=0, cause=None,
         family_shares=None, class_shares=None):
    """One DailyObservation. Successive `index` values are one day apart."""
    return qm.DailyObservation(
        date=OBS_EPOCH + dt.timedelta(days=index),
        segment_id=segment_id,
        meter_delta=float(meter_delta),
        units=float(units),
        class_shares={"fresh": 1.0} if class_shares is None else class_shares,
        family_shares=({"claude-opus-5": 1.0} if family_shares is None
                       else family_shares),
        cause=cause,
    )


def _spread(base_units, n=20, start=0, meter=10):
    """`n` days at roughly `base_units`, varied by 0-4% so the scaled MAD of
    the per-day statistic is non-zero. A perfectly flat baseline has a zero
    MAD, which makes the fence undefined by design."""
    return [_obs(meter, base_units + (i % 5) * (base_units / 100.0), start + i)
            for i in range(n)]


def test_exact_rank_sum_matches_a_hand_computed_case():
    # C(6,3) = 20 arrangements; only one puts the watch group at the top, so
    # P(W >= 15) = 0.05 and the two-sided p is 0.1.
    assert qm.rank_sum_p([1.0, 2.0, 3.0], [4.0, 5.0, 6.0]) == \
        pytest.approx(0.1, abs=1e-9)


def test_exact_rank_sum_handles_ties_with_mid_ranks():
    # Mutation: ordinal ranks instead of mid-ranks, which would manufacture a
    # difference out of six identical values.
    assert qm.rank_sum_p([1.0, 1.0, 1.0], [1.0, 1.0, 1.0]) == pytest.approx(1.0)


def test_exact_rank_sum_uses_mid_ranks_under_partial_ties():
    # Combined [1,2,2,2,3,3] gives ranks 1, 3, 3, 3, 5.5, 5.5. The watch group
    # sums to 28, which 3 of the 20 arrangements reach or exceed, so the
    # two-sided p is 0.3. Ordinal ranks would put the watch group at the
    # maximum and give 0.1 instead.
    #
    # This case pins the ORDINAL mutation only. Replacing the mid-rank average
    # with the tie group's MINIMUM rank also gives 0.3 here, so that mutation
    # survives this test; it is pinned separately in Part V below.
    assert qm.rank_sum_p([1.0, 2.0, 2.0], [2.0, 3.0, 3.0]) == \
        pytest.approx(0.3, abs=1e-9)


def test_exact_rank_sum_is_symmetric_in_direction():
    # Mutation: a one-sided test. The same separation in either direction must
    # give the same two-sided p.
    up = qm.rank_sum_p([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])
    down = qm.rank_sum_p([4.0, 5.0, 6.0], [1.0, 2.0, 3.0])
    assert up == pytest.approx(down)


def test_two_sided_p_never_exceeds_one():
    assert qm.rank_sum_p([1.0, 2.0], [1.5]) <= 1.0


def test_rank_sum_of_an_empty_group_is_one():
    assert qm.rank_sum_p([], [1.0]) == 1.0
    assert qm.rank_sum_p([1.0], []) == 1.0


def test_holm_is_monotone_and_bounded():
    got = qm.holm([0.001, 0.02, 0.5])
    assert got == sorted(got) and all(0.0 <= p <= 1.0 for p in got)
    # Mutation: Bonferroni (m * p at every rank) or a wrong step.
    assert got[0] == pytest.approx(0.003)
    assert got[1] == pytest.approx(0.04)
    assert got[2] == pytest.approx(0.5)


def test_holm_never_exceeds_one():
    assert qm.holm([0.4, 0.6]) == [pytest.approx(0.8), pytest.approx(0.8)]


def test_jackknife_uses_the_t_quantile_not_1_96():
    days = [_obs(8, 8e6, 0), _obs(13, 14e6, 1), _obs(17, 16e6, 2)]
    point, interval = qm.jackknife_fit(days)
    assert point == pytest.approx(38e6 / 38.0)
    reps = [(38e6 - 8e6) / 30.0, (38e6 - 14e6) / 25.0, (38e6 - 16e6) / 21.0]
    mean = sum(reps) / 3.0
    se = math.sqrt((2.0 / 3.0) * sum((v - mean) ** 2 for v in reps))
    half = (interval.hi - interval.lo) / 2.0
    # Mutation: the research script's hardcoded 1.96. At three days the
    # correct quantile is 4.303, which is materially wider.
    assert half == pytest.approx(qm.t_quantile_975(2) * se)
    assert half > 1.96 * se


def test_jackknife_lower_bound_is_floored_at_zero():
    days = [_obs(1, 1.0, 0), _obs(50, 1.0, 1), _obs(1, 90.0, 2)]
    point, interval = qm.jackknife_fit(days)
    assert interval.lo >= 0.0
    assert point == pytest.approx(92.0 / 52.0)


def test_a_non_finite_day_never_reaches_the_fit():
    # Section 27: a NaN unit count reached the fit and produced status=ok
    # with NaN values. Both axes and both non-finite values are covered,
    # because `nan > 0` is False while `inf > 0` is True — a guard written
    # only against NaN would leave infinity through.
    good = _obs(10, 10e6, 0)
    assert qm.jackknife_fit([good, _obs(10, float("nan"), 1)]) is None
    assert qm.jackknife_fit([good, _obs(10, float("inf"), 1)]) is None
    assert qm.jackknife_fit([good, _obs(float("nan"), 10e6, 1)]) is None
    assert qm.jackknife_fit([good, _obs(float("inf"), 10e6, 1)]) is None
    # The same two days with finite values do produce a fit, so the rejection
    # is what returns None and not the population size.
    assert qm.jackknife_fit([good, _obs(10, 12e6, 1)]) is not None


def test_a_non_finite_day_never_reaches_the_detector():
    # Positive infinity passes `units > 0.0`, so it entered the scan.
    series = _spread(10e6, 20) + [_obs(10, float("inf"), 20)] + \
        [_obs(10, 30e6, 21 + i) for i in range(3)]
    assert qm.detect_change(series).scanned_eligible_days == 23


def test_jackknife_needs_at_least_two_days():
    assert qm.jackknife_fit([_obs(10, 10e6, 0)]) is None
    assert qm.jackknife_fit([]) is None


def test_relative_width_is_the_span_over_the_point():
    assert qm.relative_width(qm.Interval(0.9, 1.1), 1.0) == pytest.approx(0.2)


def test_disjointness_is_strict():
    # Mutation: a non-strict comparison, which calls two touching intervals
    # disjoint and manufactures a rate-change verdict.
    assert qm.intervals_disjoint(qm.Interval(1.0, 2.0), qm.Interval(2.0, 3.0)) \
        is False
    assert qm.intervals_disjoint(qm.Interval(1.0, 2.0), qm.Interval(2.1, 3.0)) \
        is True


def test_a_downward_rate_change_is_detected():
    # The watch window's implied units-per-point is far ABOVE the baseline,
    # so the meter moves less per token and the metering rate went DOWN.
    series = _spread(10e6, 20) + [_obs(10, 30e6, 20 + i) for i in range(3)]
    result = qm.detect_change(series)
    assert result.qualified is True
    assert result.longest_run >= 3
    assert result.split_date == OBS_EPOCH + dt.timedelta(days=20)


def test_an_upward_rate_change_is_detected_too():
    # The mirror direction. Mutation: a one-sided fence, which stays green on
    # the test above and silently misses every rate INCREASE.
    series = _spread(30e6, 20) + [_obs(10, 10e6, 20 + i) for i in range(3)]
    result = qm.detect_change(series)
    assert result.qualified is True


def test_no_change_qualifies_on_a_homogeneous_series():
    result = qm.detect_change(_spread(10e6, 25))
    assert result.qualified is False
    assert result.split_date is None


def test_holm_family_is_every_admissible_split_not_only_qualifying_ones():
    # Mutation: building the family from the qualifying splits only, which
    # invalidates the multiplicity correction.
    series = [_obs(10, 10e6, i) for i in range(20)] + \
             [_obs(10, 25e6, 20 + i) for i in range(5)]
    result = qm.detect_change(series)
    admissible = (len(series) - qm.TRUST["min_detect_baseline_days"]
                  - qm.DETECTOR["min_watch_days"] + 1)
    assert result.holm_family_size == admissible


def test_a_series_too_short_to_split_has_an_empty_family():
    # The early return, which the no-qualifying-split test never reaches:
    # there `admissible` is non-empty and the winner-less path answers.
    result = qm.detect_change(_spread(10e6, 10))
    assert result.holm_family_size == 0 and result.qualified is False
    assert result.baseline_days == 0 and result.watch_days == 0
    assert result.scanned_eligible_days == 10


def test_the_override_bypasses_the_multiplicity_correction_only():
    series = _spread(10e6, 20) + [_obs(10, 30e6, 20 + i) for i in range(3)]
    forced = OBS_EPOCH + dt.timedelta(days=20)
    result = qm.detect_change(series, override_split=forced)
    assert result.holm_family_size == 1
    assert result.holm_p == pytest.approx(result.raw_p)
    assert result.split_date == forced


# --- the bounded scan horizon (section 24) ---------------------------------
def test_max_auto_scan_days_is_a_fingerprinted_public_constant():
    # Mutation: making the horizon a keyword argument, which lets a caller
    # silently diverge from it and stops a change invalidating persisted
    # calibrations through the fingerprint.
    assert qm.DETECTOR["max_auto_scan_days"] == 64
    assert qm.constants_payload()["detector"]["max_auto_scan_days"] == 64


def test_the_fit_population_bound_is_the_horizon_less_the_baseline_minimum():
    # Independent literals on both sides: 50 is asserted directly, and the
    # derivation is asserted separately, so neither is read back out of the
    # code under test.
    assert qm.max_fit_population_days() == 50
    assert (qm.DETECTOR["max_auto_scan_days"]
            - qm.TRUST["min_detect_baseline_days"]) == 50


def test_the_detector_reports_what_it_scanned_and_what_it_dropped():
    series = _spread(10e6, 100)
    result = qm.detect_change(series)
    assert result.input_eligible_days == 100
    assert result.scanned_eligible_days == 64
    assert result.truncated_eligible_days == 36
    assert result.history_truncated is True
    assert result.max_auto_scan_days == 64
    assert result.scan_start_date == OBS_EPOCH + dt.timedelta(days=36)
    # 64 - 14 - 3 + 1, computed inside the retained set.
    assert result.holm_family_size == 48


def test_the_retained_set_is_the_whole_rank_sum_population():
    # The discriminating case. Days 0-35 sit at a different implied rate from
    # days 36-99. Over the FULL population a split at index 36 is admissible
    # (36 baseline, 64 watch) and hugely significant, so an implementation
    # that merely restricted the candidate split dates while leaving the
    # population unbounded would still find it. Retaining only the most
    # recent 64 days puts that boundary at index 0, below the 14-day baseline
    # minimum, so it is not admissible at all.
    series = _spread(30e6, 36, 0) + _spread(10e6, 64, 36)
    result = qm.detect_change(series)
    assert result.scanned_eligible_days == 64
    assert result.input_eligible_days == 100
    assert result.qualified is False
    assert result.split_date is None


def test_no_qualifying_split_reports_zero_baseline_and_watch_days():
    # Mutation: reporting the whole population as `baseline_days`, which
    # implies a split that does not exist.
    result = qm.detect_change(_spread(10e6, 25))
    assert result.split_date is None
    assert result.baseline_days == 0 and result.watch_days == 0
    assert result.scanned_eligible_days == 25
    assert result.history_truncated is False


def test_the_override_split_is_exempt_from_the_horizon():
    # Section 24: the override evaluates one operator-selected split rather
    # than running a multiplicity-corrected scan, so it is not bounded.
    series = _spread(30e6, 14, 0) + _spread(10e6, 54, 14)
    forced = OBS_EPOCH + dt.timedelta(days=14)
    result = qm.detect_change(series, override_split=forced)
    assert result.scanned_eligible_days == 68
    assert result.truncated_eligible_days == 0
    assert result.history_truncated is False
    # Under a bounded scan the first four days would be dropped and the
    # baseline would be 10 days, not 14.
    assert result.baseline_days == 14 and result.watch_days == 54
    assert result.qualified is True


def test_the_no_change_fit_population_is_bounded_to_fifty_days():
    # Section 24: an unbounded fit population is a correctness problem, not
    # only a performance one — the cumulative all-history fit narrows to a
    # 10% interval while its point estimate climbs 2.6x and stays wrong.
    a = _analyse(_spread(10e6, 80))
    assert a.verdict is qm.Verdict.NO_RATE_CHANGE
    assert a.fitted.support.days == 50
    assert a.fitted.population["days"] == 50
    # The MOST RECENT 50, not the oldest. `considered` counts every row from
    # the first published day onward, so it is 50 when the window sits at the
    # end of an 80-day series and 80 when it sits at the start — the day
    # count alone cannot tell the two ends apart.
    assert a.fitted.population["considered"] == 50


def test_withheld_days_never_enter_the_detector():
    # Mutation: scanning the raw series including withheld rows, which lets a
    # transition day or a censored day occupy a split position.
    series = _spread(10e6, 20) + \
        [_obs(0, 0.0, 20, cause=qm.WithholdingCause.TRANSITION_DAY)] + \
        [_obs(10, 30e6, 21 + i) for i in range(3)]
    result = qm.detect_change(series)
    assert result.baseline_days + result.watch_days == 23
    assert result.scanned_eligible_days == 23


# --- eligibility fence application (the second pass) -----------------------
def test_a_day_below_the_fence_becomes_sparse_local_history():
    series = _spread(10e6, 5) + [_obs(10, 1.0, 5)]
    fenced = qm.apply_eligibility_fence(series, 1000.0)
    assert fenced[-1].cause == qm.WithholdingCause.SPARSE_LOCAL_HISTORY
    assert all(o.cause is None for o in fenced[:-1])


def test_a_day_exactly_at_the_eligibility_fence_is_eligible():
    # Section 5: a day AT or above the fence is eligible. The boundary is
    # exercised at its exact value, so a `<` to `<=` mutation in
    # `apply_eligibility_fence` fails here.
    fence = 1_000_000.0
    series = [_obs(10, fence, 0), _obs(10, fence - 1.0, 1),
              _obs(10, fence + 1.0, 2)]
    fenced = qm.apply_eligibility_fence(series, fence)
    assert fenced[0].cause is None
    assert fenced[1].cause is qm.WithholdingCause.SPARSE_LOCAL_HISTORY
    assert fenced[2].cause is None


def test_an_undefined_fence_marks_nothing_sparse():
    series = _spread(10e6, 5)
    assert [o.cause for o in qm.apply_eligibility_fence(series, None)] == \
        [None] * 5


# --- classification --------------------------------------------------------
def _classify(series, **kw):
    """`classify` over a series that is already its own published window.

    The fence is supplied rather than recomputed inside `classify`: section 37
    requires ONE eligibility floor, computed over the pre-watch prefix by the
    caller.
    """
    eligible = [o for o in series if o.cause is None]
    fit = qm.jackknife_fit(eligible)
    detector = qm.detect_change(series)
    args = dict(fingerprint_matches=True,
                newest_at=NOW - dt.timedelta(hours=1), now=NOW,
                fence=qm.eligibility_fence([o.units for o in eligible]))
    args.update(kw)
    return qm.classify(series, fit, detector, **args)


def test_a_healthy_series_classifies_ok():
    assert _classify(_spread(10e6, 10)) is qm.CalibrationStatus.OK


def test_too_few_eligible_days_is_insufficient_history():
    assert _classify(_spread(10e6, 4)) is \
        qm.CalibrationStatus.INSUFFICIENT_HISTORY


def test_a_fingerprint_mismatch_is_stale():
    assert _classify(_spread(10e6, 10), fingerprint_matches=False) is \
        qm.CalibrationStatus.STALE


def test_a_snapshot_ahead_of_the_clock_is_future():
    # Sixty seconds of tolerance, because that is clock skew rather than data.
    assert _classify(_spread(10e6, 10),
                     newest_at=NOW + dt.timedelta(seconds=30)) is \
        qm.CalibrationStatus.OK
    assert _classify(_spread(10e6, 10),
                     newest_at=NOW + dt.timedelta(seconds=90)) is \
        qm.CalibrationStatus.FUTURE


def test_an_unknown_token_split_anywhere_is_token_split_unknown():
    series = _spread(10e6, 10) + \
        [_obs(0, 0.0, 10, cause=qm.WithholdingCause.TOKEN_SPLIT_UNKNOWN)]
    assert _classify(series) is qm.CalibrationStatus.TOKEN_SPLIT_UNKNOWN


def test_an_unsupported_composition_is_unsupported_model_mix():
    series = _spread(10e6, 10) + \
        [_obs(0, 0.0, 10, cause=qm.WithholdingCause.UNSUPPORTED_COMPOSITION)]
    assert _classify(series) is qm.CalibrationStatus.UNSUPPORTED_MODEL_MIX


def test_too_much_incomplete_history_is_local_history_incomplete():
    series = _spread(10e6, 10) + [
        _obs(0, 0.0, 10, cause=qm.WithholdingCause.NO_LOCAL_HISTORY),
        _obs(0, 0.0, 11, cause=qm.WithholdingCause.NO_LOCAL_HISTORY),
    ]
    assert _classify(series) is qm.CalibrationStatus.LOCAL_HISTORY_INCOMPLETE


def test_sparse_days_do_not_consume_the_incomplete_history_budget():
    # Section 27: the budget in section 6 — one baseline day, at most 5%,
    # zero inside the decisive run — applies to `no-local-history` only. The
    # kernel counted both causes toward it, so a user whose days merely fall
    # below the eligibility fence was told the ingest tail does not cover
    # them. Three sparse days is well over both limbs of the budget, so the
    # assertion cannot pass because the population happened to stay inside.
    series = _spread(10e6, 10) + [
        _obs(0, 0.0, 10 + i,
             cause=qm.WithholdingCause.SPARSE_LOCAL_HISTORY)
        for i in range(3)
    ]
    assert _classify(series) is qm.CalibrationStatus.OK


def test_one_incomplete_day_inside_the_budget_stays_ok():
    series = _spread(10e6, 40) + \
        [_obs(0, 0.0, 40, cause=qm.WithholdingCause.NO_LOCAL_HISTORY)]
    assert _classify(series) is qm.CalibrationStatus.OK


def test_a_zero_mad_population_is_an_unstable_fit():
    # A perfectly flat population has no spread, so the eligibility fence is
    # undefined. Mutation: collapsing the fence to the median instead.
    assert _classify([_obs(10, 10e6, i) for i in range(10)]) is \
        qm.CalibrationStatus.UNSTABLE_FIT


def test_classify_reads_the_supplied_fence_rather_than_its_own():
    # Section 37: `classify` recomputed a SECOND fence over the fit days while
    # the fence that actually marks days sparse is computed over the prefix.
    # Section 5 defines one floor, so the same population must answer
    # differently only because the supplied fence differs.
    healthy = _spread(10e6, 10)
    assert _classify(healthy) is qm.CalibrationStatus.OK
    assert _classify(healthy, fence=None) is \
        qm.CalibrationStatus.UNSTABLE_FIT


def test_a_segment_disagreeing_with_the_whole_fit_is_fragmented():
    series = [_obs(10, 10e6, i, segment_id=0) for i in range(5)] + \
             [_obs(10, 30e6, 5 + i, segment_id=1) for i in range(5)]
    assert _classify(series) is qm.CalibrationStatus.FRAGMENTED_HISTORY


# --- composition ------------------------------------------------------------
WEEK_START = dt.datetime(2026, 8, 24, tzinfo=UTC)
WEEK_END = WEEK_START + dt.timedelta(days=7)


def _analyse(series, **kw):
    args = dict(now=NOW, newest_at=NOW - dt.timedelta(hours=1),
                observed_percent=40, current_week_units=40e6,
                current_week_start=WEEK_START, current_week_end=WEEK_END,
                forecast_population=())
    args.update(kw)
    return qm.analyse(series, **args)


def test_a_healthy_analysis_publishes_consumption_and_no_rate_change():
    a = _analyse(_spread(10e6, 12))
    assert a.status is qm.CalibrationStatus.OK
    assert a.verdict is qm.Verdict.NO_RATE_CHANGE
    assert a.fitted.state == "available"
    # Section 21: the command must publish current modelled consumption, its
    # headroom and its projection, each as evidence rather than a zero.
    for field in (a.consumption, a.headroom, a.projection):
        assert field.state == "available" and field.value is not None
    assert a.consumption.value == pytest.approx(40e6 / a.fitted.value)
    assert a.headroom.value == pytest.approx(100.0 - a.consumption.value)
    assert a.observed_percent == 40


def test_a_confirmed_change_reports_the_rate_change_verdict():
    # Section 23's named triple: the successor's ONLY shortfall is its day
    # count, so the status is thin while the verdict still reports the
    # detector, which satisfied its own independent and stricter gates.
    series = _spread(10e6, 20) + [_obs(10, 30e6, 20 + i) for i in range(3)]
    a = _analyse(series)
    assert a.status is qm.CalibrationStatus.INSUFFICIENT_HISTORY
    assert a.verdict is qm.Verdict.RATE_CHANGE_DETECTED
    assert a.exit_code == 1


def test_a_non_ok_status_always_withholds_and_never_says_no_rate_change():
    # The research script could print "no rate change detected" and exit 3
    # with nothing reconciling them. Mutation: reporting the detector's
    # verdict regardless of status.
    a = _analyse(_spread(10e6, 12), fingerprint_matches=False)
    assert a.status is qm.CalibrationStatus.STALE
    assert a.verdict is qm.Verdict.WITHHELD
    for field in (a.fitted, a.consumption, a.headroom, a.projection):
        assert field.state == "withheld"
        assert field.value is None and field.code is not None


def test_a_withheld_consumption_carries_a_cause_rather_than_zero():
    # Section 40 moved the ABSENT case from `no-local-history` to
    # `unavailable`; the shape of the withholding is what this test pins, and
    # the code itself is pinned below with its invalid twin.
    a = _analyse(_spread(10e6, 12), current_week_units=None)
    assert a.consumption.state == "withheld"
    assert a.consumption.value is None
    assert a.consumption.code == qm.CalibrationStatus.UNAVAILABLE.value


# --- the composition support test is APPLIED, not merely computed (§25) ---
_ATTACK_FAMILY = {"claude-opus-5": 1.0}


def _class_mix(fresh, output):
    return {"fresh": fresh, "output": output,
            "cache_1h": 0.01, "cache_read": 0.01}


def _token_class_attack():
    """20 baseline days at 90% fresh, then 3 watch days at 88% output.

    The family shares are IDENTICAL throughout, which is the whole point: a
    workload can hold its family mix perfectly constant while shifting its
    output-to-cache composition enough to move the effective scale, and the
    family radius alone cannot see it.

    The baseline class shares vary by up to two percentage points so the
    class radius is a real positive number rather than a degenerate zero.
    """
    baseline = [
        _obs(10, 10e6 + (i % 5) * 1e5, i,
             family_shares=dict(_ATTACK_FAMILY),
             class_shares=_class_mix(0.90 - (i % 5) * 0.005,
                                     0.08 + (i % 5) * 0.005))
        for i in range(20)
    ]
    watch = [
        _obs(10, 30e6, 20 + i,
             family_shares=dict(_ATTACK_FAMILY),
             class_shares=_class_mix(0.10, 0.88))
        for i in range(3)
    ]
    return baseline + watch


def test_the_attack_series_really_does_confirm_a_rate_change():
    # Guards the regression below against vacuity: the composition test must
    # be what withholds this verdict, not a detector that never found one.
    result = qm.detect_change(_token_class_attack())
    assert result.qualified is True
    assert result.split_date == OBS_EPOCH + dt.timedelta(days=20)


def test_the_attack_series_class_radius_is_not_degenerate():
    # The baseline class shares vary, so the radius is a real positive
    # number. A zero radius would make the rejection trivial.
    series = _token_class_attack()
    centre = qm.composition_centre([o.class_shares for o in series[:20]])
    radius = qm.support_radius([o.class_shares for o in series[:20]], centre)
    assert 0.0 < radius < 0.05
    assert qm.tv_distance(series[20].class_shares, centre) == \
        pytest.approx(0.79, abs=0.005)


def test_a_token_class_shift_at_constant_family_shares_is_withheld():
    # Section 25. `is_supported` was defined and never called, so the only
    # route to `unsupported-model-mix` was the per-day unknown-family branch
    # and this series produced status=ok, verdict=rate-change-detected at
    # exit 1 while the watch days sat 0.80 from the published class centre.
    a = _analyse(_token_class_attack())
    assert a.status is qm.CalibrationStatus.UNSUPPORTED_MODEL_MIX
    assert a.verdict is qm.Verdict.WITHHELD
    assert a.exit_code == 3


def test_a_family_shift_alone_is_also_withheld():
    # The mirror axis. Mutation: applying only the token-class radius.
    baseline = [_obs(10, 10e6 + (i % 5) * 1e5, i,
                     family_shares={"claude-opus-5": 0.98 - (i % 5) * 0.005,
                                    "claude-sonnet-5": 0.02 + (i % 5) * 0.005})
                for i in range(20)]
    watch = [_obs(10, 30e6, 20 + i,
                  family_shares={"claude-opus-5": 0.05,
                                 "claude-sonnet-5": 0.95})
             for i in range(3)]
    a = _analyse(baseline + watch)
    assert a.status is qm.CalibrationStatus.UNSUPPORTED_MODEL_MIX
    assert a.verdict is qm.Verdict.WITHHELD


def test_an_undefined_composition_centre_is_unsupported_model_mix():
    # Section 25: the shipped kernel published empty share vectors and
    # raised nothing when the componentwise median was all zeros.
    series = [_obs(10, 10e6 + (i % 5) * 1e5, i,
                   class_shares={"fresh": 0.0, "output": 0.0})
              for i in range(12)]
    a = _analyse(series)
    assert a.status is qm.CalibrationStatus.UNSUPPORTED_MODEL_MIX
    assert a.verdict is qm.Verdict.WITHHELD
    assert a.exit_code == 3


def test_a_consistent_composition_across_a_confirmed_change_stays_supported():
    # The over-triggering guard: applying the test must not withhold a
    # verdict whose composition never moved.
    series = _spread(10e6, 20) + [_obs(10, 30e6 + i * 1e5, 20 + i)
                                  for i in range(6)]
    a = _analyse(series)
    assert a.status is qm.CalibrationStatus.OK
    assert a.verdict is qm.Verdict.RATE_CHANGE_DETECTED


def test_headroom_lies_inside_its_own_interval_when_over_quota():
    # Section 27: the point value was unclamped while the lower bound was
    # floored at zero, so a user above 100% of quota got value = -96.56
    # against Interval(lo=0.0, hi=-94.75) — inverted, and not containing its
    # own point.
    a = _analyse(_spread(10e6, 12), current_week_units=2000e6)
    assert a.consumption.state == "available"
    assert a.consumption.value > 100.0
    h = a.headroom
    assert h.state == "available"
    assert h.interval.lo <= h.value <= h.interval.hi
    assert h.value == 0.0
    assert h.interval.lo == 0.0 and h.interval.hi == 0.0


@pytest.mark.parametrize("week_units", [1e3, 40e6, 102e6, 2000e6])
def test_headroom_always_contains_its_own_point(week_units):
    # Both sides of 100% consumption, and both far from it, so the invariant
    # is exercised where the clamp binds and where it does not.
    h = _analyse(_spread(10e6, 12), current_week_units=week_units).headroom
    assert h.interval.lo <= h.value <= h.interval.hi
    assert 0.0 <= h.interval.lo <= h.interval.hi <= 100.0


def test_entries_outside_every_observation_interval_are_reported():
    # Section 14 requires an `unattributedUnits` diagnostic; the kernel
    # silently ignored them. The day's token interval is the half-open
    # [01:00, 05:00), so the entry at exactly 05:00 is outside it.
    snaps = [_snap(1, 10.0), _snap(5, 20.0)]
    segs = qm.build_segments(snaps, [])

    def entry(hour):
        return qm.EntryRecord(at=DAY.replace(hour=hour), model="claude-opus-5",
                              fresh=1_000_000, output=0, cache_create_total=0,
                              cache_1h=0, cache_read=0)

    got = qm.unattributed_units(segs, [entry(0), entry(3), entry(5),
                                       entry(7)],
                                now=DAY + dt.timedelta(days=2))
    assert got["entries"] == 3
    assert got["units"] == pytest.approx(3_000_000.0)
    # And the one inside is the one the series actually counted.
    obs = qm.build_daily_series(segs, [entry(0), entry(3), entry(5),
                                       entry(7)],
                                now=DAY + dt.timedelta(days=2),
                                newest_entry_at=DAY + dt.timedelta(days=2))
    assert obs[0].units == pytest.approx(1_000_000.0)


def test_a_day_below_the_minimum_gain_attributes_none_of_its_tokens():
    # It is absent from the series, so its tokens belong to no observation.
    snaps = [_snap(1, 10.0), _snap(5, 11.0)]
    segs = qm.build_segments(snaps, [])
    e = qm.EntryRecord(at=DAY.replace(hour=3), model="claude-opus-5",
                       fresh=1_000_000, output=0, cache_create_total=0,
                       cache_1h=0, cache_read=0)
    assert qm.build_daily_series(segs, [e], now=DAY + dt.timedelta(days=2),
                                 newest_entry_at=DAY + dt.timedelta(days=2)) \
        == []
    assert qm.unattributed_units(
        segs, [e], now=DAY + dt.timedelta(days=2))["entries"] == 1


def test_no_family_is_classified_as_draining_only_a_dedicated_pool():
    # Section 36's eighth gap. No entry in the catalogue is "dedicated", so
    # the branch that handled one was unreachable and has been removed; this
    # pins the assumption, so classifying such a family forces the decision to
    # be retaken rather than silently filing its tokens as an unsupported mix.
    assert "dedicated" not in set(qm.FAMILY_PARTICIPATION.values())
    assert set(qm.FAMILY_PARTICIPATION.values()) == {"general"}


def test_an_unclassified_entry_is_counted_but_contributes_no_units():
    snaps = [_snap(1, 10.0), _snap(5, 20.0)]
    segs = qm.build_segments(snaps, [])
    e = qm.EntryRecord(at=DAY.replace(hour=9), model="some-other-vendor-model",
                       fresh=1_000_000, output=0, cache_create_total=0,
                       cache_1h=0, cache_read=0)
    got = qm.unattributed_units(segs, [e], now=DAY + dt.timedelta(days=2))
    assert got["entries"] == 1 and got["units"] == 0.0


def test_the_diagnostics_block_is_json_serializable_and_camel_case():
    # Section 27: the kernel stored `dataclasses.asdict(detector)`, whose
    # `split_date` is a `datetime.date` that `json.dumps` refuses, under
    # snake_case keys inside an otherwise camelCase dict.
    series = _spread(10e6, 20) + [_obs(10, 30e6, 20 + i) for i in range(3)]
    a = _analyse(series, unattributed={"units": 5.0, "entries": 2})
    decoded = json.loads(json.dumps(a.diagnostics))
    assert decoded["detector"]["splitDate"] == "2026-07-21"
    assert decoded["detector"]["scanStartDate"] == "2026-07-01"
    assert decoded["unattributedUnits"] == 5.0
    assert decoded["unattributedEntries"] == 2
    for key in decoded:
        assert "_" not in key, key
    for key in decoded["detector"]:
        assert "_" not in key, key


def test_the_diagnostics_carry_a_null_unattributed_figure_when_unsupplied():
    # Always present, so a glue that never wired the diagnostic is visible on
    # the wire rather than silently absent.
    a = _analyse(_spread(10e6, 12))
    assert a.diagnostics["unattributedUnits"] is None
    assert a.diagnostics["unattributedEntries"] is None


def test_the_analysis_publishes_baseline_and_current_share_vectors():
    # Section 27: the kernel published one pair, from the baseline, so a
    # reader could not see what actually moved. The two class vectors here
    # differ by 0.79 in total-variation distance, so an implementation that
    # aliased the second to the first cannot pass.
    a = _analyse(_token_class_attack())
    assert a.class_shares["fresh"] == pytest.approx(0.89, abs=0.005)
    assert a.current_class_shares["fresh"] == pytest.approx(0.10, abs=0.005)
    assert a.class_shares["output"] == pytest.approx(0.09, abs=0.005)
    assert a.current_class_shares["output"] == pytest.approx(0.88, abs=0.005)
    assert a.family_shares["claude-opus-5"] == pytest.approx(1.0)
    assert a.current_family_shares["claude-opus-5"] == pytest.approx(1.0)


def test_the_population_denominators_share_one_window():
    # `days` and `segments` counted the published days while `withheld`
    # counted the WHOLE series, so the three did not share a denominator.
    series = _spread(10e6, 10) + [
        _obs(0, 0.0, 10, cause=qm.WithholdingCause.RIGHT_CENSORED),
        _obs(0, 0.0, 11, cause=qm.WithholdingCause.TRANSITION_DAY),
    ]
    pop = _analyse(series).fitted.population
    assert pop["days"] == 10
    assert pop["withheld"] == 2
    assert pop["considered"] == 12
    assert pop["considered"] == pop["days"] + pop["withheld"]


def test_the_population_window_starts_at_the_split_after_a_change():
    series = _spread(10e6, 20) + [_obs(10, 30e6, 20 + i) for i in range(3)]
    a = _analyse(series)
    assert a.fitted.population["days"] == 3
    assert a.fitted.population["considered"] == 3
    assert a.baseline_fit.population["days"] == 20
    assert a.baseline_fit.population["considered"] == 20


def test_a_naive_datetime_is_rejected_rather_than_read_as_host_local():
    # `astimezone()` on a naive datetime interprets it in the HOST's zone, so
    # UTC date bucketing would silently depend on the runner's timezone.
    naive = dt.datetime(2026, 8, 1, 1, 0)
    segs = qm.build_segments([_snap(1, 10.0), _snap(5, 20.0)], [])
    later = DAY + dt.timedelta(days=2)
    entry = qm.EntryRecord(at=naive, model="claude-opus-5", fresh=1,
                           output=0, cache_create_total=0, cache_1h=0,
                           cache_read=0)
    with pytest.raises(ValueError):
        qm.canonical_week_anchor(naive)
    with pytest.raises(ValueError):
        qm.build_segments([qm.SnapshotRecord(naive, DAY, 10.0, "api")], [])
    with pytest.raises(ValueError):
        qm.build_segments([_snap(1, 10.0)],
                          [qm.CreditRecord(naive, "reset")])
    with pytest.raises(ValueError):
        qm.build_daily_series(segs, [], now=naive, newest_entry_at=later)
    with pytest.raises(ValueError):
        qm.build_daily_series(segs, [], now=later, newest_entry_at=naive)
    with pytest.raises(ValueError):
        qm.build_daily_series(segs, [entry], now=later,
                              newest_entry_at=later)
    with pytest.raises(ValueError):
        _analyse(_spread(10e6, 12), now=naive)
    with pytest.raises(ValueError):
        _analyse(_spread(10e6, 12), newest_at=naive)
    with pytest.raises(ValueError):
        _analyse(_spread(10e6, 12), current_week_start=naive)
    with pytest.raises(ValueError):
        _analyse(_spread(10e6, 12), current_week_end=naive)


def test_any_offset_is_accepted_because_instants_are_compared():
    # Aware is the requirement, not UTC. The measured store holds 9,749 rows
    # at +02:00 and 3,497 at +03:00.
    at = dt.datetime(2026, 8, 1, 3, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    assert qm.require_aware(at, "at") is at
    assert qm.canonical_week_anchor(at) == at.replace(minute=0)


def test_the_analysis_publishes_both_support_radii():
    a = _analyse(_spread(10e6, 12))
    assert a.family_radius is not None and a.class_radius is not None
    assert a.family_shares and a.class_shares


def test_a_missing_week_window_is_unavailable_not_no_local_history():
    # Section 27. Reporting `no-local-history` tells the user their token
    # history is missing when the actual cause is that the caller passed no
    # window. The units ARE present here, so the two causes are genuinely
    # distinguishable and the assertion cannot pass by the other route.
    a = _analyse(_spread(10e6, 12), current_week_start=None,
                 current_week_end=None)
    assert a.consumption.state == "available"
    assert a.projection.state == "withheld"
    assert a.projection.code == "unavailable"
    assert "week-window-unknown" in a.projection.qualifications


def test_a_degenerate_week_window_is_reported_as_invalid_not_unknown():
    a = _analyse(_spread(10e6, 12), now=WEEK_START,
                 newest_at=WEEK_START - dt.timedelta(hours=1))
    assert a.projection.code == "unavailable"
    assert "week-window-invalid" in a.projection.qualifications


def test_the_projection_scales_consumption_to_the_whole_week():
    a = _analyse(_spread(10e6, 12), now=WEEK_START + dt.timedelta(days=2),
                 newest_at=WEEK_START + dt.timedelta(days=2))
    assert a.projection.value == pytest.approx(a.consumption.value * 3.5)


def test_a_confirmed_change_publishes_the_successor_regime_not_the_blend():
    # A confirmed change closes one regime and opens a successor. Mutation:
    # fitting one value across the split, which blends a rate the provider
    # has already stopped using into the value the user predicts from.
    #
    # The successor is still what is published, but section 22 gates it: at
    # three watch days it is below the five-day prediction gate, so the four
    # published quantities are withheld and the successor's detection-grade
    # fit is published as `analysis.watch.fit` instead.
    series = _spread(10e6, 20) + [_obs(10, 30e6, 20 + i) for i in range(3)]
    a = _analyse(series)
    assert a.verdict is qm.Verdict.RATE_CHANGE_DETECTED
    assert a.diagnostics["baselineDays"] == 20
    assert a.diagnostics["currentRegimeDays"] == 3
    assert a.watch_fit.state == "available"
    assert a.watch_fit.value == pytest.approx(3e6)
    assert a.fitted.state == "withheld"


def test_a_gated_successor_withholds_the_four_published_quantities():
    # Section 22: between the three-day detection gate and the five-day
    # prediction gate there is a structural window in which a change is
    # confirmed but the new rate cannot yet be predicted from.
    series = _spread(10e6, 20) + [_obs(10, 30e6, 20 + i) for i in range(3)]
    a = _analyse(series)
    for field in (a.fitted, a.consumption, a.headroom, a.projection):
        assert field.state == "withheld"
        assert field.value is None
        assert field.code == "insufficient-history"
        assert "successor-regime" in field.qualifications
        # The successor population, not the baseline's and not the blend's.
        assert field.population["days"] == 3


def test_a_confirmed_change_publishes_both_regimes_as_evidence():
    # Section 22. Mutation: publishing only one side, which leaves the user
    # with no record of the rate they were metered at before the change.
    series = _spread(10e6, 20) + [_obs(10, 30e6, 20 + i) for i in range(3)]
    a = _analyse(series)
    assert a.baseline_fit.state == "available"
    # 20 days averaging 10.2e6 units against 10 meter points each.
    assert a.baseline_fit.value == pytest.approx(1.02e6)
    assert a.baseline_fit.support.days == 20
    assert "superseded" in a.baseline_fit.qualifications
    assert "split-at:2026-07-21" in a.baseline_fit.qualifications
    assert "detection-only" in a.watch_fit.qualifications


def test_a_mature_successor_reaches_ok_and_publishes_its_own_fit():
    # Six successor days clear the five-day prediction gate, so `fitted` is
    # available over the successor ALONE. Mutation: judging the baseline
    # while publishing the successor, which let a three-day fit reach
    # `available` and produced a prediction the spec's own gate refuses.
    series = _spread(10e6, 20) + [_obs(10, 30e6 + i * 1e5, 20 + i)
                                  for i in range(6)]
    a = _analyse(series)
    assert a.status is qm.CalibrationStatus.OK
    assert a.verdict is qm.Verdict.RATE_CHANGE_DETECTED
    assert a.exit_code == 1
    assert a.fitted.state == "available"
    assert a.fitted.support.days == 6
    assert a.fitted.value == pytest.approx(181.5e6 / 60.0)
    assert "successor-regime" in a.fitted.qualifications
    assert "detection-only" not in a.watch_fit.qualifications


def test_no_confirmed_split_withholds_both_regime_fits():
    a = _analyse(_spread(10e6, 12))
    for field in (a.baseline_fit, a.watch_fit):
        assert field.state == "withheld"
        assert "no-confirmed-split" in field.qualifications


def test_the_outcome_resolver_covers_every_allowed_triple():
    # Section 23: status and verdict are independent axes, so an exit code
    # read from the status alone is wrong.
    S, V = qm.CalibrationStatus, qm.Verdict
    blocking = (S.UNAVAILABLE, S.FUTURE, S.STALE, S.TOKEN_SPLIT_UNKNOWN,
                S.LOCAL_HISTORY_INCOMPLETE, S.UNSUPPORTED_MODEL_MIX,
                S.UNVALIDATED_COEFFICIENT_ERA)
    thin = (S.INSUFFICIENT_HISTORY, S.FRAGMENTED_HISTORY, S.UNSTABLE_FIT)
    for status in blocking:
        for detected in (False, True):
            assert qm.resolve_outcome(status, change_detected=detected) == \
                (V.WITHHELD, 3)
    for status in thin:
        assert qm.resolve_outcome(status, change_detected=False) == \
            (V.WITHHELD, 4)
    # Section 33 narrows section 23's exit-1 path to the ONE status it names.
    # `fragmented-history` and `unstable-fit` say the published successor fit
    # is untrustworthy or absent, which is how the review reached a genuinely
    # untrustworthy exit 1.
    assert qm.resolve_outcome(S.INSUFFICIENT_HISTORY, change_detected=True) \
        == (V.RATE_CHANGE_DETECTED, 1)
    for status in (S.FRAGMENTED_HISTORY, S.UNSTABLE_FIT):
        assert qm.resolve_outcome(status, change_detected=True) == \
            (V.WITHHELD, 4)
    assert qm.resolve_outcome(S.OK, change_detected=False) == \
        (V.NO_RATE_CHANGE, 0)
    assert qm.resolve_outcome(S.OK, change_detected=True) == \
        (V.RATE_CHANGE_DETECTED, 1)
    # Exhaustive: no status falls through the resolver unclassified.
    assert set(blocking) | set(thin) | {S.OK} == set(S)
    assert set(qm.VERDICT_BLOCKING_STATUSES) == set(blocking)


def test_evidence_codes_are_the_closed_union_of_both_enums():
    # Section 22 supersedes Part II's "code carries a withholding cause".
    # 6 causes + 10 non-ok statuses, sharing only `token-split-unknown`.
    expected = {c.value for c in qm.WithholdingCause} | {
        s.value for s in qm.CalibrationStatus
        if s is not qm.CalibrationStatus.OK
    }
    assert qm.EVIDENCE_CODES == expected
    assert len(qm.EVIDENCE_CODES) == 15
    assert "ok" not in qm.EVIDENCE_CODES


def test_a_code_outside_the_closed_union_is_rejected():
    # Mutation: accepting any string, which is how the shipped kernel came to
    # populate the field from two enums with nothing specifying which applied.
    with pytest.raises(ValueError):
        qm.evidence_withheld("not-a-real-code", {"days": 0})
    with pytest.raises(ValueError):
        qm.evidence_withheld(qm.CalibrationStatus.OK, {"days": 0})


# ==========================================================================
# Part IV — contracts closed after the remediation review gate
# ==========================================================================

# --- section 29: the current in-progress day is not an observation --------
def _meter_store(n_days, units_of, *, gain=4):
    """`n_days` consecutive UTC days from DAY, one segment, one entry a day.

    Each day moves the meter by `gain` true points, which keeps twenty-four
    days inside the hundred-point ceiling so no reading is right-censored, and
    one week anchor throughout means no date ever crosses a segment boundary.
    The first reading sits at 01:00 and the last at 23:00, so the entry at
    03:00 falls inside the half-open token interval.
    """
    snaps, entries = [], []
    for k in range(n_days):
        day = DAY + dt.timedelta(days=k)
        snaps.append(qm.SnapshotRecord(day.replace(hour=1), DAY,
                                       float(gain * k), "api"))
        snaps.append(qm.SnapshotRecord(day.replace(hour=23), DAY,
                                       float(gain * k + gain), "api"))
        entries.append(qm.EntryRecord(
            at=day.replace(hour=3), model="claude-opus-5",
            fresh=int(units_of(k)), output=0, cache_create_total=0,
            cache_1h=0, cache_read=0))
    return qm.build_segments(snaps, []), entries


def _step_units(k):
    """Twenty baseline days, then a threefold step — the live case's shape."""
    return 4.0e6 + (k % 5) * 4.0e4 if k < 20 else 12.0e6


#: 10:08Z on the twenty-fourth day, which is the instant the maintainer's own
#: store was measured at: the meter had already moved and the day had not
#: ended.
_TODAY_NOW = (DAY + dt.timedelta(days=23)).replace(hour=10)
_TODAY_NEWEST = (DAY + dt.timedelta(days=23)).replace(hour=3)


def _analyse_store(n_days):
    segs, entries = _meter_store(n_days, _step_units)
    series = qm.build_daily_series(segs, entries, now=_TODAY_NOW,
                                   newest_entry_at=_TODAY_NEWEST)
    return series, qm.analyse(
        series, now=_TODAY_NOW, newest_at=_TODAY_NEWEST,
        forecast_population=(), current_week_units=40e6,
        current_week_start=DAY,
        current_week_end=DAY + dt.timedelta(days=28))


def test_an_in_progress_day_is_excluded_from_the_series_entirely():
    # Section 29. `day_is_complete` required the clock to have passed the
    # day's end, so the current day was never complete and entered the series
    # carrying `no-local-history`. "The day is not over yet" is a statement
    # about time and not about local history.
    series, _ = _analyse_store(24)
    dates = [o.date for o in series]
    assert (DAY + dt.timedelta(days=23)).date() not in dates
    assert dates[-1] == (DAY + dt.timedelta(days=22)).date()
    assert all(o.cause is None for o in series)


def test_an_ingest_incomplete_day_still_carries_no_local_history():
    # The other half of the split. The day HAS ended; only the ingest tail is
    # short, which is a real local-history defect and keeps its cause.
    segs, entries = _meter_store(3, lambda k: 4.0e6)
    ended = DAY + dt.timedelta(days=3)
    series = qm.build_daily_series(
        segs, entries, now=ended,
        newest_entry_at=(DAY + dt.timedelta(days=1)).replace(hour=12))
    causes = {o.date: o.cause for o in series}
    assert causes[DAY.date()] is None
    assert causes[(DAY + dt.timedelta(days=1)).date()] is \
        qm.WithholdingCause.NO_LOCAL_HISTORY
    assert causes[(DAY + dt.timedelta(days=2)).date()] is \
        qm.WithholdingCause.NO_LOCAL_HISTORY


def test_the_in_progress_day_never_withholds_a_confirmed_change():
    # The section 29 defect end to end, on two series differing by exactly one
    # unfinished day. With it the status became `local-history-incomplete`,
    # which outranks everything, and the verdict was withheld at exit 3 every
    # single day the command ran.
    _, with_today = _analyse_store(24)
    _, without_today = _analyse_store(23)
    assert without_today.verdict is qm.Verdict.RATE_CHANGE_DETECTED
    assert without_today.exit_code == 1
    assert with_today.status is without_today.status
    assert with_today.verdict is without_today.verdict
    assert with_today.exit_code == without_today.exit_code


# --- section 30: the health scan is bounded to the published window -------
def _long_series(cause, index, n=201):
    series = _spread(10e6, n)
    series[index] = _obs(0, 0.0, index, cause=cause)
    return series


@pytest.mark.parametrize("cause,status", [
    (qm.WithholdingCause.TOKEN_SPLIT_UNKNOWN,
     qm.CalibrationStatus.TOKEN_SPLIT_UNKNOWN),
    (qm.WithholdingCause.UNSUPPORTED_COMPOSITION,
     qm.CalibrationStatus.UNSUPPORTED_MODEL_MIX),
])
def test_a_bad_day_outside_the_published_window_decides_nothing(cause, status):
    # Section 30: one bad day anywhere in 201 days of retained history made
    # the verdict permanently unavailable. The same day INSIDE the window
    # still decides, so what changed is the bound and not the rule.
    outside = _analyse(_long_series(cause, 2))
    assert outside.status is qm.CalibrationStatus.OK
    assert outside.exit_code == 0
    inside = _analyse(_long_series(cause, 199))
    assert inside.status is status
    assert inside.exit_code == 3


def test_absent_days_outside_the_published_window_decide_nothing():
    early = _spread(10e6, 201)
    for i in (2, 4):
        early[i] = _obs(0, 0.0, i,
                        cause=qm.WithholdingCause.NO_LOCAL_HISTORY)
    assert _analyse(early).status is qm.CalibrationStatus.OK
    late = _spread(10e6, 201)
    for i in (198, 199):
        late[i] = _obs(0, 0.0, i,
                       cause=qm.WithholdingCause.NO_LOCAL_HISTORY)
    assert _analyse(late).status is \
        qm.CalibrationStatus.LOCAL_HISTORY_INCOMPLETE


def test_the_eligibility_fence_is_bounded_to_the_scan_horizon():
    # Section 36's third gap. A fence computed over every retained day is a
    # floor on absolute daily volume taken across eras whose units-per-point
    # differ several-fold.
    series = [_obs(10, 1e5 + (i % 5) * 1e3, i) for i in range(140)] + \
             [_obs(10, 1e7 + (i % 5) * 1e5, 140 + i) for i in range(61)]
    horizon = int(qm.DETECTOR["max_auto_scan_days"])
    bounded = qm.eligibility_fence([o.units for o in series[-horizon:]])
    unbounded = qm.eligibility_fence([o.units for o in series])
    a = _analyse(series)
    assert a.diagnostics["eligibilityFence"] == pytest.approx(bounded)
    assert unbounded < bounded / 50.0
    # The observable consequence: every one of the 140 old days sits below the
    # bounded fence and above the unbounded one, so the two answers differ by
    # 140 withheld days rather than by a rounding step.
    assert a.diagnostics["withheldDays"] == 140


# --- section 31: the two rules are independent ----------------------------
def _sparse_run_series():
    """A confirmed change whose fourth watch day falls below the fence.

    Its units are below the fence computed over the twenty baseline days,
    while its implied units-per-point matches the watch group exactly, so the
    detector still confirms the same split and the sparse day is the only
    thing that can withhold the verdict.
    """
    return _spread(10e6, 20) + [_obs(10, 30e6, 20 + i) for i in range(3)] + \
        [_obs(3, 9.0e6, 23)]


def test_a_sparse_day_inside_the_decisive_run_withholds_the_verdict():
    # Section 31, as section 38 re-sited it: the withholding is still here and
    # the split is still the same one. What the withholding IS — a blocking
    # reason rather than a borrowed status — is pinned in Part V below.
    series = _sparse_run_series()
    detector = qm.detect_change(series)
    assert detector.qualified is True
    assert detector.split_date == OBS_EPOCH + dt.timedelta(days=20)
    a = _analyse(series)
    assert a.verdict is qm.Verdict.WITHHELD
    assert a.exit_code == 3


def test_the_incomplete_history_budget_ignores_sparse_days():
    # The mirror rule, section 27. Three sparse days is over both limbs of the
    # budget, so the assertion cannot pass because the population happened to
    # stay inside it.
    series = _spread(10e6, 10) + [
        _obs(0, 0.0, 10 + i, cause=qm.WithholdingCause.SPARSE_LOCAL_HISTORY)
        for i in range(3)
    ]
    assert _analyse(series).status is qm.CalibrationStatus.OK


def test_the_absence_budget_count_limb_fires_on_its_own():
    # Section 36's fifth gap: the existing case used 2 absent days of 12
    # considered, which satisfies the count limb and the fraction limb at
    # once. Two of forty-two is 4.8%, inside the fraction limb, so only the
    # count limb can fire here.
    series = _spread(10e6, 40) + [
        _obs(0, 0.0, 40 + i, cause=qm.WithholdingCause.NO_LOCAL_HISTORY)
        for i in range(2)
    ]
    assert 2 / 42 < qm.TRUST["max_incomplete_baseline_fraction"]
    assert _analyse(series).status is \
        qm.CalibrationStatus.LOCAL_HISTORY_INCOMPLETE


def test_the_absence_budget_fraction_limb_fires_on_its_own():
    # One absent day is inside the count limb, so only the fraction limb —
    # one of eleven, 9.1% — can fire here.
    series = _spread(10e6, 10) + \
        [_obs(0, 0.0, 10, cause=qm.WithholdingCause.NO_LOCAL_HISTORY)]
    assert 1 <= qm.TRUST["max_incomplete_baseline_days"]
    assert _analyse(series).status is \
        qm.CalibrationStatus.LOCAL_HISTORY_INCOMPLETE


def test_the_decisive_run_limb_fires_on_its_own():
    # Section 36's first gap. Twenty-one successor days with one absent day
    # among them satisfies BOTH budget limbs — one day, 4.5% — so the
    # decisive-run limb is the only thing that can withhold this verdict.
    series = _spread(10e6, 20) + \
        [_obs(10, 30e6 + (i % 5) * 1e5, 20 + i) for i in range(21)] + \
        [_obs(0, 0.0, 41, cause=qm.WithholdingCause.NO_LOCAL_HISTORY)]
    a = _analyse(series)
    assert a.detector.qualified is True
    assert a.status is qm.CalibrationStatus.LOCAL_HISTORY_INCOMPLETE
    assert a.exit_code == 3
    # Drop the absent day and the same series reports the change at exit 1.
    assert _analyse(series[:-1]).exit_code == 1


# --- section 33: an unfittable population is `unstable-fit` ---------------
def test_a_population_that_cannot_be_fitted_is_an_unstable_fit():
    # Section 33: `fit is None` and a day-count shortfall are different
    # conditions. One day supports no jackknife at all.
    assert _classify([_obs(10, 10e6, 0)]) is qm.CalibrationStatus.UNSTABLE_FIT
    # Four days DO produce a fit and fall short of the five-day gate, which is
    # the other condition and keeps its own status.
    assert _classify(_spread(10e6, 4)) is \
        qm.CalibrationStatus.INSUFFICIENT_HISTORY


def test_a_confirmed_change_with_no_successor_fit_is_not_exit_one():
    # Section 33: the review reached a genuinely untrustworthy exit 1 through
    # the one status section 23 does name. The successor holds a non-finite
    # day, so no successor fit exists, and exit 1 must not be reachable.
    series = _spread(10e6, 20) + [_obs(10, 30e6, 20 + i) for i in range(3)] + \
        [_obs(10, float("nan"), 23)]
    a = _analyse(series)
    assert a.detector.qualified is True
    assert a.watch_fit.state == "withheld"
    assert a.status is qm.CalibrationStatus.UNSTABLE_FIT
    assert a.verdict is qm.Verdict.WITHHELD
    assert a.exit_code == 4


def test_the_undefined_radius_skip_is_not_a_mix_finding():
    # Section 36's second gap. A reference population too small to estimate a
    # spread is thin evidence, not a mix finding: inverting the skip would
    # move it from exit 4 to exit 3.
    a = _analyse([_obs(10, 10e6, 0)])
    assert a.class_radius is None and a.family_radius is None
    assert a.status is qm.CalibrationStatus.UNSTABLE_FIT
    assert a.exit_code == 4


# --- section 32: non-finite input at every numeric boundary ---------------
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5.0])
def test_a_non_finite_current_week_figure_withholds_its_evidence(bad):
    # Section 32. `nan` gave `consumption.value = nan` with `headroom.value =
    # 100.0` over the interval (100.0, 100.0) — the user was told they had
    # their full quota left. Infinity gave headroom 0.0 and a negative gave
    # 100.0.
    a = _analyse(_spread(10e6, 12), current_week_units=bad)
    for field in (a.consumption, a.headroom, a.projection):
        assert field.state == "withheld"
        assert field.value is None
        assert field.code == qm.CalibrationStatus.UNAVAILABLE.value
    # The fit itself is unaffected: only the dependent evidence is withheld.
    assert a.fitted.state == "available"


def test_a_non_finite_observed_percent_is_rejected_not_propagated():
    a = _analyse(_spread(10e6, 12), observed_percent=float("nan"))
    assert a.observed_percent is None
    assert "observedMinusModelled" not in a.diagnostics
    assert a.diagnostics["rejectedNumericInputs"] == ["observed_percent"]
    # Each rejected parameter is named, so the caller can tell which of them
    # was unusable rather than only that something was.
    both = _analyse(_spread(10e6, 12), observed_percent=float("nan"),
                    current_week_units=float("inf"))
    assert both.diagnostics["rejectedNumericInputs"] == \
        ["current_week_units", "observed_percent"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_the_diagnostics_never_carry_a_non_finite_float(bad):
    # `json.dumps` emits a bare `NaN`, which is not valid JSON. A strict dump
    # is the pin, because the permissive default would accept it silently. A
    # caller-supplied diagnostic is as capable of carrying one as the kernel.
    a = _analyse(_spread(10e6, 12), current_week_units=bad,
                 observed_percent=bad,
                 unattributed={"units": bad, "entries": 2},
                 diagnostics={"callerSupplied": bad,
                              "nested": {"deeper": [bad, 1.0]}})
    json.dumps(a.diagnostics, allow_nan=False)
    assert a.diagnostics["callerSupplied"] is None
    assert a.diagnostics["unattributedUnits"] is None
    assert a.diagnostics["nested"]["deeper"] == [None, 1.0]


def test_a_finite_negative_diagnostic_is_left_alone():
    # The sanitizer nulls NON-FINITE floats and nothing else; a negative
    # difference between the observed meter and the modelled consumption is an
    # ordinary and informative value.
    a = _analyse(_spread(10e6, 12), current_week_units=200e6,
                 observed_percent=1, diagnostics={"callerSupplied": -1.0})
    json.dumps(a.diagnostics, allow_nan=False)
    assert a.diagnostics["callerSupplied"] == -1.0
    assert a.diagnostics["observedMinusModelled"] < 0.0


# --- section 34: the composition support materiality floor ----------------
def test_a_one_percent_second_model_does_not_lock_a_user_out():
    # Section 34. A radius of median + 3 scaled MADs collapses to exactly zero
    # when the reference share vectors never vary, which is the ordinary case
    # for a single-model user, and any later day differing at all was then
    # unsupported.
    series = [_obs(10, 10e6 + (i % 5) * 1e5, i,
                   family_shares={"claude-opus-5": 1.0}) for i in range(20)] + \
             [_obs(10, 30e6 + i * 1e5, 20 + i,
                   family_shares={"claude-opus-5": 0.99,
                                  "claude-sonnet-5": 0.01}) for i in range(6)]
    a = _analyse(series)
    assert a.status is qm.CalibrationStatus.OK
    assert a.verdict is qm.Verdict.RATE_CHANGE_DETECTED
    assert a.exit_code == 1


def test_the_token_class_attack_is_still_caught_under_the_floor():
    # The floor must not admit the section 25 attack at total-variation
    # distance 0.79, which is what "a zero radius is not undefined and the
    # test is not skipped" exists to prevent.
    a = _analyse(_token_class_attack())
    assert a.status is qm.CalibrationStatus.UNSUPPORTED_MODEL_MIX
    assert a.exit_code == 3


# --- section 37: `analyse` sorts rather than assuming its caller did ------
def test_analyse_sorts_its_input_rather_than_assuming_the_caller_did():
    # `analyse` takes "most recent" positionally through `eligible[-n:]`, so
    # an unsorted caller silently fitted a different fifty days.
    import random
    ordered = _spread(10e6, 60)
    shuffled = list(ordered)
    random.Random(7).shuffle(shuffled)
    assert [o.date for o in shuffled] != [o.date for o in ordered]
    assert _analyse(shuffled).fitted.value == \
        pytest.approx(_analyse(ordered).fitted.value)


# --- section 34: the floor is derived, and the vectors are raw ------------
def test_token_class_shares_are_raw_quantities_not_weighted_units():
    # Section 34: the support vectors are RAW token-class quantity shares.
    # Weighting them first is why the materiality floor could not be applied
    # to them. A million fresh and a million output tokens are half the
    # quantity each; weighted they would be 0.1745 and 0.8255.
    segs = qm.build_segments([_snap(1, 10.0), _snap(5, 20.0)], [])
    e = qm.EntryRecord(at=DAY.replace(hour=3), model="claude-opus-5",
                       fresh=1_000_000, output=1_000_000,
                       cache_create_total=0, cache_1h=0, cache_read=0)
    obs = qm.build_daily_series(segs, [e], now=DAY + dt.timedelta(days=2),
                                newest_entry_at=DAY + dt.timedelta(days=2))[0]
    assert obs.class_shares["fresh"] == pytest.approx(0.5)
    assert obs.class_shares["output"] == pytest.approx(0.5)
    # The FAMILY vector stays a share of weighted units and still sums to one.
    # Normalizing it by the raw total instead gives 2.865 here.
    assert obs.family_shares == {"claude-opus-5": pytest.approx(1.0)}
    # The FITTED units stay weighted, which is the half of the change that
    # must not move.
    assert obs.units == pytest.approx(1_000_000.0 + 4.73 * 1_000_000.0)


def test_the_token_class_floor_is_derived_from_the_coefficients():
    # `0.05 * mu / 4.7269`, where mu is the centre's own weighted value and
    # 4.7269 = 4.73 - 0.0031 is the coefficient spread. Mutation: asserting a
    # constant instead, which would not move with the centre.
    spread = max(qm.TOKEN_CLASS_WEIGHTS.values()) - \
        min(qm.TOKEN_CLASS_WEIGHTS.values())
    assert spread == pytest.approx(4.7269)
    fresh_only = qm.token_class_floor({"fresh": 1.0})
    assert fresh_only == pytest.approx(0.05 * 1.0 / spread)
    assert fresh_only == pytest.approx(0.01058, abs=5e-5)
    # A centre carrying output is worth more per unit share, so its floor is
    # larger — the floor is centre-specific and not a single number.
    mixed = {"fresh": 0.5, "output": 0.5}
    assert qm.token_class_floor(mixed) == \
        pytest.approx(0.05 * (0.5 + 0.5 * 4.73) / spread)
    assert qm.token_class_floor(mixed) > fresh_only
    assert qm.token_class_floor(None) is None
    # A centre with no weighted value at all defines no floor.
    assert qm.token_class_floor({"fresh": 0.0}) is None
    assert qm.token_class_floor({}) is None


def test_the_effective_radius_is_the_larger_of_the_two():
    # A zero empirical radius is a real answer, not an undefined one.
    assert qm.effective_radius(0.0, 0.02) == 0.02
    assert qm.effective_radius(0.0694, 0.02) == pytest.approx(0.0694)
    assert qm.effective_radius(None, 0.02) is None
    assert qm.effective_radius(0.0694, None) == pytest.approx(0.0694)
    # A non-finite floor is no floor, never a radius that admits everything.
    assert qm.effective_radius(0.5, float("nan")) == pytest.approx(0.5)
    assert qm.effective_radius(0.5, float("inf")) == pytest.approx(0.5)


def test_the_mix_support_policy_is_fingerprinted():
    # Section 34: a persisted calibration must go stale when the policy moves.
    payload = qm.constants_payload()["mixSupport"]
    assert payload["family_tv_floor"] == 0.02
    assert payload["token_class_max_relative_effect"] == 0.05
    assert payload["vector"] == "raw-quantity-share"
    assert payload["effective_radius"] == "max(empirical, floor)"
    assert qm.QUOTA_MODEL_ALGORITHM_REVISION == 2


def test_the_published_radii_are_the_effective_ones():
    # The radius that decides is the one published, and the empirical value it
    # was floored from is a diagnostic rather than lost.
    a = _analyse(_spread(10e6, 12))
    assert a.family_radius == pytest.approx(0.02)
    assert a.class_radius == pytest.approx(qm.token_class_floor({"fresh": 1.0}))
    assert a.diagnostics["familyRadiusEmpirical"] == pytest.approx(0.0)
    assert a.diagnostics["classRadiusEmpirical"] == pytest.approx(0.0)


def test_the_family_floor_sits_below_the_measured_empirical_radius():
    # 0.02 is the smallest clean bound above the measured supported maximum of
    # 0.0156 and below the live empirical radius of 0.0694, so the measured
    # store's own outlier decision is unchanged by the floor.
    assert qm.effective_radius(0.0694, 0.02) == pytest.approx(0.0694)
    assert 0.0156 < qm.MIX_SUPPORT["family_tv_floor"] < 0.0694


# --- section 35: the forecast population is the current regime ------------
def _forecast_entry(day_index, *, fresh, output, model="claude-opus-5"):
    return qm.EntryRecord(
        at=dt.datetime.combine(OBS_EPOCH + dt.timedelta(days=day_index),
                               dt.time(12), tzinfo=UTC),
        model=model, fresh=fresh, output=output, cache_create_total=0,
        cache_1h=0, cache_read=0)


def _output_heavy(day_index):
    """One entry at 12% fresh and 88% output by RAW quantity.

    Its family share is unchanged, so only the token-class axis can reject it,
    and its distance from a fresh-only centre is 0.88 — far outside the
    0.01058 effective radius that centre carries.
    """
    return _forecast_entry(day_index, fresh=120_000, output=880_000)


def test_the_forecast_population_is_probed_against_the_fit_population():
    # Section 35, first regression. The decisive-watch path is held constant
    # and only the separately supplied forecast population varies.
    series = _spread(10e6, 12)
    clean = _analyse(series)
    assert clean.status is qm.CalibrationStatus.OK
    assert clean.verdict is qm.Verdict.NO_RATE_CHANGE
    assert clean.exit_code == 0
    probed = _analyse(series, forecast_population=[_output_heavy(11)])
    assert probed.status is qm.CalibrationStatus.UNSUPPORTED_MODEL_MIX
    assert probed.verdict is qm.Verdict.WITHHELD
    assert probed.exit_code == 3
    # The reference it was measured against is the fit population's centre.
    assert qm.tv_distance(qm.aggregate_composition([_output_heavy(11)])[1],
                          probed.class_shares) > probed.class_radius


def test_the_forecast_population_is_probed_against_the_baseline_after_a_change():
    # Section 35, second regression. The detector stays qualified throughout;
    # only the forecast population moves.
    series = _spread(10e6, 20) + [_obs(10, 30e6 + i * 1e5, 20 + i)
                                  for i in range(6)]
    clean = _analyse(series)
    assert clean.status is qm.CalibrationStatus.OK
    assert clean.verdict is qm.Verdict.RATE_CHANGE_DETECTED
    assert clean.exit_code == 1
    probed = _analyse(series, forecast_population=[_output_heavy(22)])
    assert probed.detector.qualified is True
    assert probed.status is qm.CalibrationStatus.UNSUPPORTED_MODEL_MIX
    assert probed.verdict is qm.Verdict.WITHHELD
    assert probed.exit_code == 3


def test_forecast_entries_before_the_successor_boundary_are_not_probed():
    # "Entries at or after the successor boundary" — a pre-split entry belongs
    # to the closed regime and says nothing about the current one.
    series = _spread(10e6, 20) + [_obs(10, 30e6 + i * 1e5, 20 + i)
                                  for i in range(6)]
    before = _analyse(series, forecast_population=[_output_heavy(5)])
    assert before.status is qm.CalibrationStatus.OK
    assert before.exit_code == 1
    assert before.diagnostics["forecastPopulationEntries"] == 0
    at_boundary = _analyse(series, forecast_population=[_output_heavy(20)])
    assert at_boundary.status is qm.CalibrationStatus.UNSUPPORTED_MODEL_MIX
    assert at_boundary.diagnostics["forecastPopulationEntries"] == 1


def test_a_supported_forecast_population_changes_nothing():
    # The over-triggering guard: a forecast population whose composition
    # matches the reference must leave the verdict alone.
    series = _spread(10e6, 12)
    supported = _analyse(
        series, forecast_population=[_forecast_entry(11, fresh=1_000_000,
                                                     output=0)])
    assert supported.status is qm.CalibrationStatus.OK
    assert supported.exit_code == 0
    assert supported.diagnostics["forecastPopulationEntries"] == 1


def test_the_forecast_aggregate_uses_raw_class_and_weighted_family_shares():
    entries = [_forecast_entry(1, fresh=1_000_000, output=1_000_000),
               _forecast_entry(2, fresh=1_000_000, output=0,
                               model="claude-sonnet-5")]
    families, classes = qm.aggregate_composition(entries)
    assert classes["fresh"] == pytest.approx(2 / 3)
    assert classes["output"] == pytest.approx(1 / 3)
    weighted_opus = 1_000_000 + 4.73 * 1_000_000
    total = weighted_opus + 1_000_000
    assert families["claude-opus-5"] == pytest.approx(weighted_opus / total)
    assert qm.aggregate_composition([]) is None
    # An unclassified family contributes nothing, so a population made only of
    # them supports no comparison.
    assert qm.aggregate_composition(
        [_forecast_entry(1, fresh=10, output=0, model="mystery-model")]) is None


# --- section 36: the remaining measured gaps ------------------------------
def test_the_headroom_clamp_binds_at_both_ends():
    # Section 36's seventh gap: the upper limb is unreachable on valid input,
    # because section 32 now rejects a negative week figure before it can
    # produce a consumption below zero. It is pinned directly instead.
    assert qm._clamp_percent(150.0) == 100.0
    assert qm._clamp_percent(-50.0) == 0.0
    assert qm._clamp_percent(42.5) == pytest.approx(42.5)


# --- section 37: the smaller corrections ----------------------------------
def test_the_in_progress_exclusion_is_reported_in_diagnostics():
    segs, _ = _meter_store(24, _step_units)
    excluded = qm.in_progress_dates(segs, now=_TODAY_NOW)
    assert excluded == ((DAY + dt.timedelta(days=23)).date(),)
    # A day the clock HAS passed is not in-progress, whatever the ingest tail.
    assert qm.in_progress_dates(segs, now=DAY + dt.timedelta(days=24)) == ()
    a = _analyse(_spread(10e6, 12), in_progress_excluded=excluded)
    assert a.diagnostics["inProgressDayExcluded"] == \
        [(DAY + dt.timedelta(days=23)).date().isoformat()]
    # Always published, so a glue that never wired it is visible on the wire.
    assert _analyse(_spread(10e6, 12)).diagnostics[
        "inProgressDayExcluded"] is None


def test_the_new_boundaries_reject_a_naive_datetime():
    # Section 37: `require_aware` guarded four entry points and not these.
    naive = dt.datetime(2026, 8, 1, 1, 0)
    later = DAY + dt.timedelta(days=2)
    segs = qm.build_segments([_snap(1, 10.0), _snap(5, 20.0)], [])
    entry = qm.EntryRecord(at=naive, model="claude-opus-5", fresh=1, output=0,
                           cache_create_total=0, cache_1h=0, cache_read=0)
    aware_entry = qm.EntryRecord(at=DAY.replace(hour=3), model="claude-opus-5",
                                 fresh=1, output=0, cache_create_total=0,
                                 cache_1h=0, cache_read=0)
    with pytest.raises(ValueError):
        qm.unattributed_units(segs, [entry], now=later)
    with pytest.raises(ValueError):
        qm.unattributed_units(segs, [aware_entry], now=naive)
    with pytest.raises(ValueError):
        qm.day_is_complete(DAY.date(), now=naive, newest_entry_at=later)
    with pytest.raises(ValueError):
        qm.day_is_complete(DAY.date(), now=later, newest_entry_at=naive)
    with pytest.raises(ValueError):
        qm.in_progress_dates(segs, now=naive)
    with pytest.raises(ValueError):
        _classify(_spread(10e6, 10), now=naive)
    with pytest.raises(ValueError):
        _classify(_spread(10e6, 10), newest_at=naive)
    with pytest.raises(ValueError):
        _analyse(_spread(10e6, 12), forecast_population=[entry])


def test_the_detector_table_is_annotated_honestly():
    # Section 37: `dict[str, float]` while four of the five entries are ints
    # whose int-ness is load-bearing for the fingerprint.
    assert "int" in qm.__annotations__["DETECTOR"]


def test_the_module_docstring_claims_no_re_export():
    # Section 37: the kernel is not re-exported on the `cctally` module.
    assert "re-export" not in qm.__doc__.lower()


# --- gaps the mutation probe measured -------------------------------------
def _successor_classify(series):
    """`classify` with the successor regime as the published population.

    Written out rather than routed through `_classify`, because that helper
    fits every eligible day: a fit spanning both regimes is wide enough to
    report `unstable-fit`, which outranks the status under test and would let
    the negative half of the boundary pass for the wrong reason.
    """
    split = OBS_EPOCH + dt.timedelta(days=20)
    watch = [o for o in series if o.cause is None and o.date >= split]
    return qm.classify(
        series, qm.jackknife_fit(watch), qm.detect_change(series),
        fingerprint_matches=True, newest_at=NOW - dt.timedelta(hours=1),
        now=NOW, fence=qm.eligibility_fence([o.units for o in series[:20]]),
        population=watch)


def test_an_absent_day_exactly_on_the_split_date_is_inside_the_run():
    # The decisive-run predicate is "at or after" the split date, and the
    # budget alone does not fire on either series — one absent day of
    # twenty-six considered is inside both limbs — so only the boundary
    # decides between them.
    series = _spread(10e6, 20) + [_obs(10, 30e6, 20 + i) for i in range(5)]
    assert qm.detect_change(series).split_date == \
        OBS_EPOCH + dt.timedelta(days=20)
    on_split = series + [_obs(0, 0.0, 20, segment_id=1,
                              cause=qm.WithholdingCause.NO_LOCAL_HISTORY)]
    assert _successor_classify(on_split) is \
        qm.CalibrationStatus.LOCAL_HISTORY_INCOMPLETE
    before_split = series + [_obs(0, 0.0, 19, segment_id=1,
                                  cause=qm.WithholdingCause.NO_LOCAL_HISTORY)]
    assert _successor_classify(before_split) is qm.CalibrationStatus.OK


def test_a_sparse_day_on_the_split_date_stays_inside_the_window():
    # The published window starts at the SPLIT date, not at the first day that
    # survived the fence. Taking it from the surviving days would push a
    # sparse day sitting on the split date outside the window that judges it,
    # which is the one place the two definitions differ.
    series = _spread(10e6, 20) + [_obs(3, 9.0e6, 20)] + \
        [_obs(10, 30e6, 21 + i) for i in range(3)]
    fence = qm.eligibility_fence([o.units for o in series[:20]])
    assert 0.0 < series[20].units < fence
    a = _analyse(series)
    assert a.detector.split_date == OBS_EPOCH + dt.timedelta(days=20)
    assert a.fitted.population["days"] == 3
    assert a.fitted.population["considered"] == 4
    # The consequence, in section 38's terms: the window taken from the split
    # date holds the sparse day and blocks the verdict, while a window taken
    # from the first SURVIVING day starts one day later and blocks nothing.
    fenced = qm.apply_eligibility_fence(series, fence)
    surviving = [o for o in fenced
                 if o.cause is None and o.date >= a.detector.split_date]
    assert qm.blocking_reasons(
        [o for o in fenced if o.date >= a.detector.split_date],
        a.detector) == (qm.BlockingReason.SPARSE_DAY_IN_DECISIVE_RUN,)
    assert qm.blocking_reasons(
        [o for o in fenced if o.date >= surviving[0].date], a.detector) == ()
    assert a.verdict is qm.Verdict.WITHHELD
    assert a.exit_code == 3
    assert "sparse-day-in-decisive-run" in a.fitted.qualifications


def test_a_population_with_no_reference_days_is_thin_not_unsupported():
    # An empty reference population supports no comparison, so it is thin
    # evidence at exit 4 and never a mix finding at exit 3.
    for series in ([], [_obs(0, 0.0, i,
                             cause=qm.WithholdingCause.RIGHT_CENSORED)
                        for i in range(6)]):
        a = _analyse(series)
        assert a.status is qm.CalibrationStatus.UNSTABLE_FIT
        assert a.exit_code == 4


def test_the_detector_answers_the_same_whatever_order_it_is_handed():
    # `detect_change` is a public entry point of its own and sorts its own
    # population; `analyse` sorting first does not excuse it.
    import random
    ordered = _spread(10e6, 30)
    shuffled = list(ordered)
    random.Random(3).shuffle(shuffled)
    assert [o.date for o in shuffled] != [o.date for o in ordered]
    assert qm.detect_change(shuffled) == qm.detect_change(ordered)


# --------------------------------------------------------------------------
# Part V — the third kernel review gate
# --------------------------------------------------------------------------
# --- section 38: a blocked verdict is not a status ------------------------
def test_blocking_reasons_are_a_closed_set():
    # A blocking reason is a third closed vocabulary, alongside the status and
    # the withholding cause. Borrowing a member of either of those to carry a
    # blocking reason is the defect section 38 names.
    assert {r.value for r in qm.BlockingReason} == \
        {"sparse-day-in-decisive-run"}
    assert not ({r.value for r in qm.BlockingReason}
                & {s.value for s in qm.CalibrationStatus})
    assert not ({r.value for r in qm.BlockingReason}
                & {c.value for c in qm.WithholdingCause})


def test_the_outcome_resolver_withholds_on_a_blocking_reason_alone():
    # Mutation: `resolve_outcome` ignoring its blocking argument. The status is
    # one section 23 names for exit 1, so only the blocking reason can move it.
    blocked = qm.resolve_outcome(
        qm.CalibrationStatus.INSUFFICIENT_HISTORY, change_detected=True,
        blocking=(qm.BlockingReason.SPARSE_DAY_IN_DECISIVE_RUN,))
    assert blocked == (qm.Verdict.WITHHELD, 3)
    assert qm.resolve_outcome(qm.CalibrationStatus.INSUFFICIENT_HISTORY,
                              change_detected=True, blocking=()) == \
        (qm.Verdict.RATE_CHANGE_DETECTED, 1)
    # A healthy population whose verdict is blocked still exits 3, not 0.
    assert qm.resolve_outcome(qm.CalibrationStatus.OK, change_detected=False,
                              blocking=(qm.BlockingReason
                                        .SPARSE_DAY_IN_DECISIVE_RUN,)) == \
        (qm.Verdict.WITHHELD, 3)


def test_a_sparse_day_in_the_decisive_run_is_a_blocking_reason_not_a_status():
    # Section 38. The kernel used to append `local-history-incomplete`, which
    # told a user whose days are fully ingested that their local token history
    # was incomplete. The verdict is still withheld at exit 3, but the status
    # now describes the published population's own health and the reason is
    # named in the qualifications.
    series = _sparse_run_series()
    fence = qm.eligibility_fence([o.units for o in series[:20]])
    assert 0.0 < series[23].units < fence
    a = _analyse(series)
    assert a.detector.qualified is True
    assert a.verdict is qm.Verdict.WITHHELD
    assert a.exit_code == 3
    assert a.status is not qm.CalibrationStatus.LOCAL_HISTORY_INCOMPLETE
    # Three successor days is what the published population actually suffers
    # from, and it is what the status reports.
    assert a.status is qm.CalibrationStatus.INSUFFICIENT_HISTORY
    for field in (a.fitted, a.consumption, a.headroom, a.projection):
        assert "sparse-day-in-decisive-run" in field.qualifications
    # Without that one day the same series reports the change, so the sparse
    # day is what withholds it and not the series' shape.
    assert _analyse(series[:23]).verdict is qm.Verdict.RATE_CHANGE_DETECTED


def test_the_sparse_run_rule_lives_outside_the_status_classifier():
    # Section 31, in the direction section 38 added: the blocking reason must
    # not be carried by a borrowed status. `classify` over the same window
    # reports the population's own health and nothing about the sparse day.
    series = _sparse_run_series()
    detector = qm.detect_change(series)
    fence = qm.eligibility_fence(
        [o.units for o in series if o.date < detector.split_date])
    fenced = qm.apply_eligibility_fence(series, fence)
    window = [o for o in fenced if o.date >= detector.split_date]
    published = [o for o in window if o.cause is None]
    assert any(o.cause is qm.WithholdingCause.SPARSE_LOCAL_HISTORY
               for o in window)
    status = qm.classify(window, qm.jackknife_fit(published), detector,
                         fingerprint_matches=True, now=NOW,
                         newest_at=NOW - dt.timedelta(hours=1), fence=fence,
                         population=published)
    assert status is qm.CalibrationStatus.INSUFFICIENT_HISTORY
    assert qm.blocking_reasons(window, detector) == \
        (qm.BlockingReason.SPARSE_DAY_IN_DECISIVE_RUN,)


def test_the_absence_rules_never_produce_a_blocking_reason():
    # The mirror of the rule above: the incomplete-history budget and the
    # absent-day-inside-the-run limb are STATUS findings, and neither may be
    # implemented as a blocking reason.
    series = _spread(10e6, 20) + \
        [_obs(10, 30e6 + (i % 5) * 1e5, 20 + i) for i in range(21)] + \
        [_obs(0, 0.0, 41, cause=qm.WithholdingCause.NO_LOCAL_HISTORY)]
    detector = qm.detect_change(series)
    assert detector.qualified is True
    window = [o for o in series if o.date >= detector.split_date]
    assert qm.blocking_reasons(window, detector) == ()
    a = _analyse(series)
    assert a.status is qm.CalibrationStatus.LOCAL_HISTORY_INCOMPLETE
    assert a.exit_code == 3


def test_no_confirmed_change_means_no_blocking_reason():
    # The decisive run only exists after a confirmed split, so a sparse day in
    # an unsplit series blocks nothing.
    series = _spread(10e6, 12) + [_obs(10, 1.0, 12)]
    fenced = qm.apply_eligibility_fence(series, 1000.0)
    assert fenced[-1].cause is qm.WithholdingCause.SPARSE_LOCAL_HISTORY
    assert qm.blocking_reasons(fenced, qm.detect_change(series)) == ()


# --- section 39: input validity is asserted, not assumed ------------------
_MALFORMED_DAY = dt.date(2026, 8, 20)


def _one_day_series(entries):
    """The daily series over one complete, fully ingested day.

    The meter moves ten true points across the day, so the day is attributed
    and its only remaining question is what the entries inside it amount to.
    """
    snaps = [qm.SnapshotRecord(
        dt.datetime(_MALFORMED_DAY.year, _MALFORMED_DAY.month,
                    _MALFORMED_DAY.day, hour, tzinfo=UTC),
        DAY, percent, "api") for hour, percent in ((1, 10.0), (23, 20.0))]
    return qm.build_daily_series(qm.build_segments(snaps, []), entries,
                                 now=NOW, newest_entry_at=NOW)


def _at_noon(**kw):
    return _entry(at=dt.datetime.combine(_MALFORMED_DAY, dt.time(12),
                                         tzinfo=UTC), **kw)


def test_a_one_hour_quantity_above_the_cache_write_total_is_malformed():
    # Section 39. The five-minute quantity is the REMAINDER, so a one-hour
    # part larger than the whole is malformed input. It used to survive into
    # the arithmetic as a negative fresh quantity: at
    # cache_create_total = 0 and cache_1h = 1000 the raw quantity total is
    # exactly 0.0 while the weighted total is positive, because the one-hour
    # class carries 1.03 against the fresh remainder's 1.0.
    bad = _entry(cache_create_total=0, cache_1h=1000)
    assert qm.token_class_units(bad) is None
    assert qm.weighted_units(bad) is None
    # The rejection is at the exact boundary. A remainder of -1 is already
    # malformed, and it is the smallest one that reproduces the defect: at
    # cache_create_total = 0 and cache_1h = 1 the raw total is 0.0 against a
    # weighted total of 0.03.
    assert qm.token_class_units(_entry(cache_create_total=0, cache_1h=1)) \
        is None
    # A remainder of exactly zero is well formed — the whole cache write was
    # one-hour — and must not be rejected with it.
    whole = _entry(cache_create_total=1000, cache_1h=1000)
    assert qm.token_class_units(whole) == {
        "fresh": 0.0, "output": 0.0, "cache_1h": 1000.0, "cache_read": 0.0}
    # It reaches the series as the typed cause section 22 already provides,
    # never as a day whose shares were computed from a negative quantity.
    series = _one_day_series([_at_noon(cache_create_total=0, cache_1h=1000)])
    assert [o.cause for o in series] == \
        [qm.WithholdingCause.TOKEN_SPLIT_UNKNOWN]


def test_a_negative_quantity_that_zeroes_the_raw_total_is_withheld():
    # Section 39. The `raw_total <= 0.0` guard rests on every quantity being
    # non-negative, which nothing in `EntryRecord` enforces. One fresh token
    # against one negative cache-read token leaves the raw total at exactly
    # 0.0 while the weighted total stays positive at 0.9969, so removing the
    # guard divides by zero instead of withholding the day.
    entry = _entry(fresh=1, cache_read=-1)
    classes = qm.token_class_units(entry)
    assert sum(classes.values()) == 0.0
    assert qm.weighted_units(entry) > 0.0
    series = _one_day_series([_at_noon(fresh=1, cache_read=-1)])
    assert [o.cause for o in series] == \
        [qm.WithholdingCause.NO_LOCAL_HISTORY]


def test_an_overflowing_class_accumulator_is_withheld():
    # Section 39. Two entries at cache_read = 1e308 overflow the RAW
    # accumulator to infinity while the weighted total stays finite at about
    # 6.2e305, because that class carries 0.0031. Without the
    # `_finite_quantity(raw_total)` guard every class share becomes NaN and
    # reaches the composition centre.
    entries = [_at_noon(cache_read=int(1e308)) for _ in range(2)]
    raw = sum(qm.token_class_units(e)["cache_read"] for e in entries)
    weighted = sum(qm.weighted_units(e) for e in entries)
    assert math.isinf(raw) and math.isfinite(weighted)
    assert [o.cause for o in _one_day_series(entries)] == \
        [qm.WithholdingCause.TOKEN_SPLIT_UNKNOWN]


def test_the_forecast_aggregate_asserts_the_same_two_premises():
    # Section 39's premises hold at BOTH numeric boundaries, not only inside
    # `build_daily_series`. `aggregate_composition` reduces the forecast
    # population — a REQUIRED parameter of `analyse` — over the same
    # quantities through the same accumulators, so the same two malformed
    # inputs reach it.
    #
    # One fresh token against one negative cache-read token leaves the raw
    # total at exactly 0.0 while the weighted total stays positive, so
    # dropping the `raw_total <= 0.0` limb divides by zero here exactly as it
    # does there.
    negative = [_entry(fresh=1, cache_read=-1)]
    assert qm.aggregate_composition(negative) is None
    # A probed population the kernel cannot aggregate fails CLOSED, so this
    # pins the guard's reachable consequence rather than a fail-open that a
    # renderer would report as a healthy run.
    assert _analyse(_spread(10e6, 12),
                    forecast_population=negative).status is \
        qm.CalibrationStatus.UNSUPPORTED_MODEL_MIX
    # Two entries at cache_read = 1e308 overflow the raw accumulator while the
    # weighted total stays finite. Dropping the `_finite_quantity(raw_total)`
    # limb does not merely publish a NaN share: `tv_distance` against a NaN
    # component compares false, so `is_supported` returns True against any
    # centre and the support test is defeated rather than tripped.
    overflowing = [_entry(cache_read=int(1e308)) for _ in range(2)]
    assert qm.aggregate_composition(overflowing) is None


# --- section 40: the interface change and the remaining pins --------------
def test_the_forecast_population_is_a_required_keyword():
    # Section 40. Its `()` default silently disabled section 35's probe and
    # returned `ok` at exit 0, with the only trace a zero count in
    # diagnostics. A parameter whose omission disables a spec-mandated safety
    # check must fail loudly.
    with pytest.raises(TypeError):
        qm.analyse(_spread(10e6, 12), now=NOW,
                   newest_at=NOW - dt.timedelta(hours=1))
    # The two diagnostics keep their `None` defaults, because publishing a
    # null is the stated design and is visible on the wire.
    a = qm.analyse(_spread(10e6, 12), now=NOW,
                   newest_at=NOW - dt.timedelta(hours=1),
                   forecast_population=())
    assert a.diagnostics["unattributedUnits"] is None
    assert a.diagnostics["inProgressDayExcluded"] is None


def test_the_mid_rank_convention_is_not_the_tie_groups_minimum_rank():
    # Section 21 names the mid-rank convention an acceptance item, and every
    # other tie case in this file survives its replacement by the tie group's
    # MINIMUM rank. These two do not.
    #
    # [1, 3, 3] against [4, 2]: the combined mid-ranks are 1, 3.5, 3.5, 5, 2,
    # the watch group sums to 7 of a total 15, and 4 of the 10 arrangements
    # reach it, so the two-sided p is 0.8. Minimum ranks make the tie group 3
    # instead of 3.5, the total 14, and only 3 arrangements reach the watch
    # sum, giving 0.6.
    assert qm.rank_sum_p([1.0, 3.0, 3.0], [4.0, 2.0]) == pytest.approx(0.8)
    # [3, 3, 3] against [4, 4, 1, 1]: the baseline is one tie group at mid-rank
    # 4, so its sum of 12 is exactly the mean of a 3-subset of 28 and the
    # two-sided p is 1.0. Minimum ranks move that group to 3, the sum to 9
    # against a mean of 9.857, and give 24/35.
    assert qm.rank_sum_p([3.0, 3.0, 3.0], [4.0, 4.0, 1.0, 1.0]) == \
        pytest.approx(1.0)


def test_a_week_that_already_ended_projects_no_less_than_it_consumed():
    # Section 40: the projection's `min(now, week_end)` clamp. Without it a
    # week that has already ended has an elapsed span longer than the week, so
    # the pace factor falls below one and the projected end-of-week
    # consumption prints BELOW the consumption printed above it.
    a = _analyse(_spread(10e6, 12), now=WEEK_END + dt.timedelta(days=1),
                 newest_at=WEEK_END + dt.timedelta(days=1))
    assert a.fitted.value == pytest.approx(1_017_500.0)
    assert a.consumption.value == pytest.approx(40e6 / 1_017_500.0)
    assert a.projection.value == pytest.approx(a.consumption.value)


def _downward_change_with_a_sparse_watch_day():
    """A confirmed DOWNWARD change whose fourth watch day is genuinely sparse.

    The baseline units are bimodal in `log1p` space — half at `UA` and half
    at `UA * e**0.5` — so admitting one more day into the fence population
    moves the median by a whole cluster rather than by a rank. The first watch
    day sits above both clusters, so admitting it raises the median to the
    upper cluster and LOWERS the fence, which is what lets the genuinely
    sparse fourth watch day pass.

    The rate change is downward: the watch days carry more units per day and
    three times the meter movement, so their implied units-per-point is a
    third of the baseline's while their units stay above the fence.
    """
    ua, gap, rate = 10e6, 0.5, 1.0e6
    series = []
    for i in range(14):
        units = ua if i % 2 == 0 else ua * math.exp(gap)
        series.append(_obs(units / (rate * (1.0 + (i % 5) * 0.004)), units, i))
    watch = ua * math.exp(1.1)
    for j in range(3):
        units = watch * (1.0 + j * 0.01)
        series.append(_obs(units / (rate / 3.0), units, 14 + j))
    sparse = ua * math.exp(-1.2)
    series.append(_obs(sparse / (rate / 3.0), sparse, 17))
    return series


def test_the_eligibility_fence_prefix_stops_before_the_split_date():
    # Section 40: `<` versus `<=` against the split date. Admitting the first
    # watch day into the fence population lowers the fence far enough to let a
    # genuinely sparse watch day through, and the verdict then reports a rate
    # change that rests on a day the fence excluded.
    series = _downward_change_with_a_sparse_watch_day()
    detector = qm.detect_change(series)
    assert detector.qualified is True
    assert detector.split_date == OBS_EPOCH + dt.timedelta(days=14)
    before = qm.eligibility_fence(
        [o.units for o in series if o.date < detector.split_date])
    including = qm.eligibility_fence(
        [o.units for o in series if o.date <= detector.split_date])
    assert including < before
    sparse_units = series[-1].units
    assert including < sparse_units < before
    a = _analyse(series)
    assert a.diagnostics["eligibilityFence"] == pytest.approx(before)
    assert a.verdict is qm.Verdict.WITHHELD
    assert a.exit_code == 3
    assert "sparse-day-in-decisive-run" in a.fitted.qualifications


def test_a_day_exactly_at_its_support_radius_is_supported():
    # Section 27 required this boundary class pinned and two of the three
    # were. `>` versus `>=`: a day AT the radius is inside it.
    fam_centre, cls_centre = {"x": 1.0}, {"fresh": 1.0}
    fam = {"x": 0.9, "y": 0.1}
    cls = {"fresh": 0.95, "output": 0.05}
    fam_r = qm.tv_distance(fam, fam_centre)
    cls_r = qm.tv_distance(cls, cls_centre)
    assert qm.is_supported(
        fam, cls, fam_centre, fam_r, cls_centre, 1.0) is True
    assert qm.is_supported(
        fam, cls, fam_centre, 1.0, cls_centre, cls_r) is True
    assert qm.is_supported(
        fam, cls, fam_centre, math.nextafter(fam_r, 0.0),
        cls_centre, 1.0) is False
    assert qm.is_supported(fam, cls, fam_centre, 1.0, cls_centre,
                           math.nextafter(cls_r, 0.0)) is False


_JITTERED_FRESH_SHARES = (
    0.90, 0.88, 0.91, 0.89, 0.92, 0.87, 0.90, 0.93, 0.88, 0.91,
    0.89, 0.90, 0.92, 0.86, 0.91, 0.88, 0.90, 0.94, 0.89, 0.72,
)


def _jittered_class_series():
    """Twenty healthy days whose fresh-to-output split varies day to day.

    Every other fixture in this file holds its share vectors constant, so
    every per-day distance is exactly zero and the no-per-day rule of section
    18 is unobservable. Here three of the twenty days lie outside the radius,
    which is what a radius of median plus three scaled MADs does on ordinary
    workloads.
    """
    return [_obs(10, 10e6 + (i % 5) * 1e5, i,
                 class_shares={"fresh": share,
                               "output": round(1.0 - share, 3),
                               "cache_1h": 0.0, "cache_read": 0.0})
            for i, share in enumerate(_JITTERED_FRESH_SHARES)]


def test_the_no_change_forecast_probe_is_not_applied_per_day():
    # Section 40. With no confirmed change the support test probes the
    # forecast population as ONE aggregate and never the published days
    # individually. A per-day rule there flips an ordinary workload to exit 3.
    series = _jittered_class_series()
    a = _analyse(series, forecast_population=())
    outside = [o for o in series
               if qm.tv_distance(o.class_shares, a.class_shares)
               > a.class_radius]
    assert len(outside) >= 3
    assert a.status is qm.CalibrationStatus.OK
    assert a.verdict is qm.Verdict.NO_RATE_CHANGE
    assert a.exit_code == 0


def test_the_composition_centres_come_from_the_reference_population():
    # Section 40. The centres and radii are measured from the regime that
    # ESTABLISHED the composition — the baseline after a confirmed change —
    # so feeding them from every eligible day blends the successor into the
    # centre it is being measured against. Twenty-one successor days against
    # twenty baseline days is enough to carry the componentwise median.
    series = _spread(10e6, 20) + [
        _obs(10, 30e6 + (i % 5) * 1e5, 20 + i,
             family_shares={"claude-sonnet-5": 1.0},
             class_shares={"output": 1.0})
        for i in range(21)]
    a = _analyse(series)
    assert a.detector.qualified is True
    assert a.family_shares == {"claude-opus-5": 1.0}
    assert a.class_shares == {"fresh": 1.0}
    assert a.current_family_shares == {"claude-sonnet-5": 1.0}
    assert a.current_class_shares == {"output": 1.0}
    # The blend the mutation would publish is the successor's own centre,
    # because the successor is the larger half.
    eligible = [o for o in series if o.cause is None]
    assert qm.composition_centre([o.family_shares for o in eligible]) == \
        {"claude-opus-5": 0.0, "claude-sonnet-5": 1.0}


def test_the_week_anchor_boundary_is_at_minute_thirty():
    # Section 40. The kernel keeps its OWN copy of the rounding rule
    # `_cctally_core._normalize_week_boundary_dt` implements. They agree
    # today, and nothing pinned them at the one value where they could part.
    import _cctally_core

    base = dt.datetime(2026, 2, 5, 7, 0, tzinfo=UTC)
    for minute, hour in ((29, 7), (30, 8), (31, 8)):
        value = base.replace(minute=minute)
        expected = base.replace(hour=hour, minute=0)
        assert qm.canonical_week_anchor(value) == expected, minute
        assert _cctally_core._normalize_week_boundary_dt(value) == expected


def test_a_non_finite_dict_key_never_reaches_json_dumps():
    # Section 40. `_finite_json` sanitized values but not keys, and
    # `json.dumps({float("nan"): 1})` emits `{"NaN": 1}`, which is not valid
    # JSON. A caller-supplied diagnostic is as capable of carrying one as this
    # kernel is.
    assert qm._finite_json({float("nan"): 1.0}) == {None: 1.0}
    assert qm._finite_json({float("inf"): {float("-inf"): 2.0}}) == \
        {None: {None: 2.0}}
    a = _analyse(_spread(10e6, 12),
                 diagnostics={float("nan"): 1.0, "ok": 2.0})
    assert "NaN" not in json.dumps(a.diagnostics)
    assert "Infinity" not in json.dumps(a.diagnostics)


def test_an_absent_current_week_figure_is_unavailable_not_no_local_history():
    # Section 40, on the principle section 27 already settled for the week
    # window: the caller supplying nothing is not the user's history being
    # missing. The invalid case already routed to `unavailable`; the absent
    # case joins it, under its own qualification.
    absent = _analyse(_spread(10e6, 12), current_week_units=None)
    assert absent.consumption.code == qm.CalibrationStatus.UNAVAILABLE.value
    assert "current-week-units-unknown" in absent.consumption.qualifications
    invalid = _analyse(_spread(10e6, 12), current_week_units=float("nan"))
    assert invalid.consumption.code == qm.CalibrationStatus.UNAVAILABLE.value
    assert "current-week-units-invalid" in invalid.consumption.qualifications
    # The two absences stay distinguishable.
    assert "current-week-units-invalid" not in \
        absent.consumption.qualifications


def test_a_forecast_population_the_kernel_cannot_aggregate_is_unsupported():
    # Mutation: dropping the `forecast_probed and forecast_shares is None`
    # limb of `_composition_unsupported`, which fails open.
    #
    # Section 40 made `forecast_population` required so the section 18 support
    # probe could not be silently disabled by omission. A population the
    # kernel cannot aggregate disables it just as completely: every entry from
    # an unrecognised family contributes no units, `aggregate_composition`
    # returns None, and the probe list stays empty. Section 25 already treats
    # an undefined REFERENCE centre as `unsupported-model-mix`; an undefined
    # FORECAST centre must fail the same way rather than the opposite way.
    #
    # This is the week a new Claude family ships, which is the scenario
    # section 26 already had to be patched for once.
    unknown = [_entry(model="claude-vega-9", fresh=1_000_000)] * 50
    assert qm.aggregate_composition(unknown) is None
    a = _analyse(_spread(10e6, 12), forecast_population=unknown)
    assert a.status is qm.CalibrationStatus.UNSUPPORTED_MODEL_MIX
    assert a.verdict is qm.Verdict.WITHHELD
    assert a.exit_code == 3
    # An EMPTY forecast population is a different thing: nothing was probed,
    # so there is nothing to fail on, and the run stays healthy.
    assert _analyse(_spread(10e6, 12),
                    forecast_population=()).status is qm.CalibrationStatus.OK


def test_the_blocking_reasons_are_published_as_a_typed_field():
    # Mutation: dropping `QuotaAnalysis.blocking`, which forces a renderer to
    # search `qualifications` -- a set carrying seven unrelated string
    # vocabularies -- and to stop matching when a second reason is added.
    a = _analyse(_sparse_run_series())
    assert a.verdict is qm.Verdict.WITHHELD
    assert a.blocking == (qm.BlockingReason.SPARSE_DAY_IN_DECISIVE_RUN,)
    # A healthy run publishes an empty tuple, never None, so a consumer may
    # iterate it unconditionally.
    assert _analyse(_spread(10e6, 12)).blocking == ()
