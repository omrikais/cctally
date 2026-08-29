"""#661 S2 Tasks B3/B4 — `project` reports modelled quota (spec §5).

`Used %` was a project's share of the window's DOLLARS, scaled by the week's
meter reading. Cache reads are far cheaper per token than output under the
model's weights, so that proxy over-credits a cache-heavy project and
under-credits an output-heavy Opus one. It reports a cost share under a column
whose name says quota.

The replacement runs each joined entry through `weighted_units` BEFORE bucket
aggregation and divides by the regime's units-per-point. Weighting must
precede aggregation because `_accumulate_entry_into_bucket` drops the one-hour
cache-write split that `_JoinedClaudeEntry` carries, and `weighted_units`
needs it — weighting the existing aggregate buckets would silently mis-price
every cache-heavy project, the exact failure this finding exists to fix.

Support is tested over the whole account-week-regime population, not per
project, and a week with one unsupported or out-of-regime segment falls back
for the WHOLE week: partial-week mixtures are not produced.
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import pytest

from conftest import load_script

UTC = dt.timezone.utc
WEEK_START = dt.datetime(2026, 6, 1, tzinfo=UTC)
WEEK_END = WEEK_START + dt.timedelta(days=7)


def _mod():
    return load_script()["_cctally_project"]


def _qcg():
    return load_script()["_load_sibling"]("_cctally_quota_calibration")


def _regime(*, effective_from=None, effective_until=None,
            family_radius=1.0, class_radius=1.0):
    qcg = _qcg()
    return qcg.ValidatedRegime(
        account_key=None,
        effective_from=effective_from or dt.datetime(2026, 1, 1, tzinfo=UTC),
        effective_until=effective_until,
        units_per_point=2_400_000.0,
        interval_lo=2_300_000.0,
        interval_hi=2_500_000.0,
        status="ok",
        as_of=WEEK_END,
        family_shares={"opus": 1.0},
        class_shares={"fresh": 0.5, "output": 0.5},
        family_radius=family_radius,
        class_radius=class_radius,
    )


class _Entry:
    """The `_JoinedClaudeEntry` surface `_entry_quota_record` reads."""

    def __init__(self, *, model="claude-opus-4-5", input_tokens=1000,
                 output_tokens=1000, cache_creation_tokens=0,
                 cache_read_tokens=0, cache_1h_tokens=None,
                 at=None):
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_tokens = cache_creation_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_1h_tokens = cache_1h_tokens
        self.timestamp = at or (WEEK_START + dt.timedelta(hours=1))


# --------------------------------------------------------------------------
# Per-entry weighting
# --------------------------------------------------------------------------
def test_b3_weighting_precedes_bucket_aggregation():
    """`_accumulate_entry_into_bucket` drops the 1h cache-write split that
    `weighted_units` needs, so two entries identical in the aggregate must
    still weigh differently."""
    module = _mod()
    split = module._entry_quota_record(
        _Entry(input_tokens=0, output_tokens=0,
               cache_creation_tokens=1_000_000, cache_1h_tokens=1_000_000))
    unsplit = module._entry_quota_record(
        _Entry(input_tokens=0, output_tokens=0,
               cache_creation_tokens=1_000_000, cache_1h_tokens=0))
    assert split[1] is not None and unsplit[1] is not None
    assert split[1] != unsplit[1], (
        "the one-hour cache-write split does not change the weight, so "
        "weighting the aggregate bucket would have been equivalent")


def test_b3_an_entry_off_the_general_meter_weighs_nothing():
    """`population_units` skips a family whose participation is not
    `general`, so an attributed row must skip it too — otherwise the
    per-project parts stop summing to the population's own units.

    An unrecognized family resolves to `None`, whose participation is
    `unknown`, which is the reachable non-general case.
    """
    module = _mod()
    record, units = module._entry_quota_record(
        _Entry(model="some-model-nobody-classified"))
    assert record is None
    assert units is None


def test_b3_an_entry_with_an_unknown_cache_split_weighs_nothing():
    """`weighted_units` returns None rather than pricing an unknown split at
    zero, and this helper must pass that through rather than coercing."""
    module = _mod()
    _record, units = module._entry_quota_record(
        _Entry(input_tokens=0, output_tokens=0,
               cache_creation_tokens=1_000_000, cache_1h_tokens=None))
    assert units is None


# --------------------------------------------------------------------------
# Which weeks can be modelled
# --------------------------------------------------------------------------
def _records(module, entries):
    out = []
    for entry in entries:
        record, units = module._entry_quota_record(entry)
        if units is not None:
            out.append(record)
    return out


def test_b3_a_week_inside_a_supported_regime_is_modelled():
    module = _mod()
    regime = _regime()
    records = _records(module, [_Entry()])
    out = module.resolve_week_attribution(
        regime, records, week_start=WEEK_START, week_end=WEEK_END)
    assert out.basis == "modelled"
    assert out.cause is None
    assert out.units_per_point == pytest.approx(2_400_000.0)


def test_b3_no_regime_at_all_falls_back_to_the_cost_share():
    """The common case: no calibration, or one the validated reader refuses."""
    module = _mod()
    out = module.resolve_week_attribution(
        None, [], week_start=WEEK_START, week_end=WEEK_END)
    assert out.basis == "cost-share"
    assert out.cause == "calibration-absent"
    assert out.units_per_point is None


def test_b3_a_week_straddling_the_regime_boundary_falls_back_whole():
    """Spec §5.1: partial-week mixtures are not produced. The reader
    publishes only the OPEN regime, so the earlier segment has no rate at
    all and modelling half a week would publish an understated figure."""
    module = _mod()
    regime = _regime(effective_from=WEEK_START + dt.timedelta(days=3))
    out = module.resolve_week_attribution(
        regime, _records(module, [_Entry()]),
        week_start=WEEK_START, week_end=WEEK_END)
    assert out.basis == "cost-share"
    assert out.cause == "regime-boundary"


def test_b3_a_week_ending_after_a_closed_regime_falls_back_whole():
    module = _mod()
    regime = _regime(effective_until=WEEK_START + dt.timedelta(days=3))
    out = module.resolve_week_attribution(
        regime, _records(module, [_Entry()]),
        week_start=WEEK_START, week_end=WEEK_END)
    assert out.basis == "cost-share"
    assert out.cause == "regime-boundary"


def test_b3_one_unsupported_segment_falls_back_for_the_whole_week():
    """Support is re-tested against THIS population, not inherited from the
    regime's stored status."""
    module = _mod()
    regime = _regime(family_radius=0.001, class_radius=0.001)
    out = module.resolve_week_attribution(
        regime, _records(module, [_Entry(model="claude-sonnet-4-5")]),
        week_start=WEEK_START, week_end=WEEK_END)
    assert out.basis == "cost-share"
    assert out.cause == "unsupported-composition"


def test_b3_an_empty_week_population_falls_back_rather_than_modelling_zero():
    module = _mod()
    out = module.resolve_week_attribution(
        _regime(), [], week_start=WEEK_START, week_end=WEEK_END)
    assert out.basis == "cost-share"
    assert out.cause == "no-local-history"


def test_b3_the_attribution_causes_are_a_closed_set():
    module = _mod()
    assert set(module.ATTRIBUTION_CAUSES) == {
        "account-not-resolved", "calibration-absent", "regime-boundary",
        "unsupported-composition", "no-local-history"}


# --------------------------------------------------------------------------
# The account gate (spec §5.3)
# --------------------------------------------------------------------------
def test_b3_merged_account_withholds_modelled_quota_when_decorated():
    """`project` without `--account` is merged, and S1 publishes no valid
    merged calibration. Silently falling back to cost share there would keep
    publishing the very number F1 exists to remove, under a column the
    acceptance criterion says reports the correct account."""
    module = _mod()
    basis, cause = module.resolve_attribution_account_gate(
        account_key=None, decorated=True)
    assert basis == "withheld"
    assert cause == "account-not-resolved"


def test_b3_a_single_real_account_is_not_affected_by_the_gate():
    """R8: nothing decorates at one real account, so the merged path IS the
    account path and this affects only genuinely multi-account installs."""
    module = _mod()
    basis, cause = module.resolve_attribution_account_gate(
        account_key=None, decorated=False)
    assert basis is None and cause is None


def test_b3_an_explicit_account_passes_the_gate_even_when_decorated():
    module = _mod()
    basis, cause = module.resolve_attribution_account_gate(
        account_key="acct-1", decorated=True)
    assert basis is None and cause is None


# --------------------------------------------------------------------------
# The residual (spec §5.2)
# --------------------------------------------------------------------------
def test_b3_the_residual_is_stateable_only_when_everything_aligns():
    module = _mod()
    assert module.residual_withholding_cause(
        account_resolved=True, whole_weeks=True, filtered=False,
        fallback_weeks=0) is None


@pytest.mark.parametrize("kwargs", [
    {"account_resolved": False},
    {"whole_weeks": False},
    {"filtered": True},
    {"fallback_weeks": 1},
])
def test_b3_the_residual_is_withheld_when_the_population_is_split(kwargs):
    """A residual computed against a partial population is not a residual."""
    module = _mod()
    base = dict(account_resolved=True, whole_weeks=True, filtered=False,
                fallback_weeks=0)
    base.update(kwargs)
    assert module.residual_withholding_cause(**base) == "population-misaligned"


def test_the_residual_causes_are_a_closed_set():
    """The analogue of `MOVEMENT_WITHHELD_CAUSES` for this module. A cause
    added without copy renders as its own code, which is readable but not
    the sentence a terminal user is owed."""
    module = _mod()
    assert set(module.RESIDUAL_CAUSES) == {
        "population-misaligned", "observed-absent", "no-modelled-weeks"}
    for code in module.RESIDUAL_CAUSES:
        assert module._cause_copy(code) != code, code


def test_an_absent_observed_side_is_not_reported_as_misalignment():
    """Spec section 5.2 names the absent observed side as a SEPARATE
    condition — "an absent observed side withholds on its own" — from a
    population the account, window or filters have split. Both withheld, and
    `population-misaligned` then told a terminal user, in a human sentence,
    that their window or filters were at fault when in fact one modelled
    week simply carried no meter snapshot."""
    module = _mod()
    assert module.residual_absence_cause(observed=None, modelled=12.0) == (
        "observed-absent")


def test_no_modelled_week_is_its_own_cause_too():
    """Both sides absent means the window modelled nothing, which is again
    not a misalignment: there is no population to be misaligned with."""
    module = _mod()
    assert module.residual_absence_cause(observed=None, modelled=None) == (
        "no-modelled-weeks")
    assert module.residual_absence_cause(
        observed=40.0, modelled=None) == "no-modelled-weeks"


def test_a_computable_residual_states_no_absence_cause():
    """The non-vacuity twin: the classifier must not name a cause for a
    residual that exists."""
    module = _mod()
    assert module.residual_absence_cause(observed=40.0, modelled=38.0) is None


def test_the_footer_names_the_absent_observed_side_in_its_own_words():
    module = _mod()
    text = _footer(module, {
        "modelledWeekPoints": 10.0, "visibleRowPoints": 10.0,
        "filteredOrUnmodelledPoints": 0.0,
        "observedMinusModelledPoints": None,
        "residualCause": "observed-absent",
    })
    assert "withheld" in text
    assert "does not align" not in text
    assert "snapshot" in text


def test_b3_the_residual_never_asserts_a_direction():
    """§2.2 measured the observed-minus-modelled difference with BOTH signs,
    so nothing may assume it has one."""
    module = _mod()
    assert module.observed_minus_modelled(observed=40.0, modelled=42.0) < 0
    assert module.observed_minus_modelled(observed=40.0, modelled=38.0) > 0
    assert module.observed_minus_modelled(observed=None, modelled=38.0) is None
    assert module.observed_minus_modelled(observed=40.0, modelled=None) is None


# --------------------------------------------------------------------------
# The schema bump (spec §5.4)
# --------------------------------------------------------------------------
def _payload(module, **over):
    kwargs = dict(
        since=WEEK_START, until=WEEK_END, weeks_in_range=1,
        group_mode="git-root", rows=[], weeks_missing_snapshot=set(),
        warnings=[], include_breakdown=False, week_snapshots={},
        attribution_basis="cost-share", attribution_cause="calibration-absent",
        attribution_totals={
            "modelledWeekPoints": None,
            "visibleRowPoints": None,
            "filteredOrUnmodelledPoints": None,
            "observedMinusModelledPoints": None,
            "residualCause": "population-misaligned",
        },
    )
    kwargs.update(over)
    return module._project_json_payload(**kwargs)


def test_b4_project_json_declares_schema_version_2():
    """`attributedUsedPercent` keeps its spelling and changes its MEANING,
    from a cost share to modelled quota, which `docs/cli-contract.md`
    classifies as breaking. `costPerPercent`'s meaning moves with it."""
    module = _mod()
    payload = _payload(module)
    assert payload["schemaVersion"] == 2


def test_b4_the_payload_states_its_attribution_basis_and_cause():
    module = _mod()
    payload = _payload(module)
    assert payload["attribution"]["basis"] in {
        "modelled", "cost-share", "withheld"}
    assert payload["attribution"]["cause"] == "calibration-absent"
    assert "modelledWeekPoints" in payload["attribution"]["totals"]
    assert "observedMinusModelledPoints" in payload["attribution"]["totals"]
    assert payload["attribution"]["totals"]["residualCause"] == (
        "population-misaligned")


def test_b4_a_row_carries_its_own_basis():
    """A project can span weeks that resolved differently, so the basis is a
    row field and not only a payload one."""
    module = _mod()
    row = {
        "key": type("K", (), {"display_key": "alpha", "bucket_path": "/p/a",
                              "git_root": "/p/a"})(),
        "sessions": set(), "first_seen": WEEK_START, "last_seen": WEEK_END,
        "input": 1, "output": 1, "cache_write": 0, "cache_read": 0,
        "cost_usd": 1.0, "attributed_pct": 2.0, "cost_per_pct": 0.5,
        "models": {}, "attribution_basis": "modelled",
    }
    payload = _payload(module, rows=[row])
    assert payload["projects"][0]["attributionBasis"] == "modelled"
    assert payload["projects"][0]["attributedUsedPercent"] == 2.0


# --------------------------------------------------------------------------
# The §5.2 footer, and what "whole weeks" means
# --------------------------------------------------------------------------
def _footer(module, totals, *, basis="modelled", cause=None):
    return "\n".join(module.render_attribution_footer(
        totals, basis=basis, cause=cause))


def test_the_footer_states_all_four_quantities():
    """§5.2's quantities existed only in `project --json`; a terminal user
    got no reconciliation information at all."""
    module = _mod()
    text = _footer(module, {
        "modelledWeekPoints": 12.5,
        "visibleRowPoints": 11.0,
        "filteredOrUnmodelledPoints": 1.5,
        "observedMinusModelledPoints": 0.75,
        "residualCause": None,
    })
    assert "12.50 points across the modelled weeks" in text
    assert "11.00 in the rows listed" in text
    assert "1.50 filtered or unmodelled" in text
    assert "+0.75 points, as measured" in text


def test_the_footer_states_the_residuals_sign_and_asserts_no_direction():
    """§2.2 measured the difference with BOTH signs, so a footer asserting a
    direction would be false half the time, and §2.2 forbids naming it as
    off-machine usage at all."""
    module = _mod()
    base = {"modelledWeekPoints": 10.0, "visibleRowPoints": 10.0,
            "filteredOrUnmodelledPoints": 0.0, "residualCause": None}
    positive = _footer(module, dict(base, observedMinusModelledPoints=2.0))
    negative = _footer(module, dict(base, observedMinusModelledPoints=-2.0))
    assert "+2.00 points" in positive
    assert "-2.00 points" in negative
    for text in (positive, negative):
        assert "not an identification or an estimate of usage from another " \
               "machine" in text
        for forbidden in ("more than", "less than", "unaccounted",
                          "off-machine usage of", "missing"):
            assert forbidden not in text, (forbidden, text)


def test_the_footer_withholds_the_residual_with_its_cause():
    module = _mod()
    text = _footer(module, {
        "modelledWeekPoints": 10.0, "visibleRowPoints": 8.0,
        "filteredOrUnmodelledPoints": 2.0,
        "observedMinusModelledPoints": None,
        "residualCause": "population-misaligned",
    })
    assert "withheld" in text
    assert "does not align" in text
    assert "points, as measured" not in text


def test_the_footer_states_the_run_level_withholding():
    module = _mod()
    text = _footer(module, {"residualCause": "population-misaligned"},
                   basis="withheld", cause="account-not-resolved")
    assert "modelled quota withheld" in text
    assert "--account" in text


@pytest.mark.parametrize("since,until,expected", [
    # The whole week, and the range that slices it.
    (WEEK_START, WEEK_END, True),
    (WEEK_START + dt.timedelta(days=2), WEEK_END, False),
    (WEEK_START, WEEK_END - dt.timedelta(days=2), False),
])
def test_whole_weeks_asks_whether_the_range_covers_whole_weeks(
        since, until, expected):
    """`whole_weeks` was `not weeks_missing_snapshot`, which asks whether
    every week has a SNAPSHOT — a different question that a three-day range
    over one fully-snapshotted week passed."""
    module = _mod()
    now = WEEK_END + dt.timedelta(days=1)
    assert module.range_covers_whole_weeks(
        since, until, [(WEEK_START, WEEK_END)], now=now) is expected


def test_the_open_week_is_covered_whole_by_a_range_running_to_now():
    """The meter reading and the local entries both stop at `now`, so
    neither side is clipped relative to the other."""
    module = _mod()
    now = WEEK_START + dt.timedelta(days=3)
    assert module.range_covers_whole_weeks(
        WEEK_START, now, [(WEEK_START, WEEK_END)], now=now) is True
    assert module.range_covers_whole_weeks(
        WEEK_START, now - dt.timedelta(days=1), [(WEEK_START, WEEK_END)],
        now=now) is False


def test_an_empty_bound_set_covers_no_whole_week():
    module = _mod()
    assert module.range_covers_whole_weeks(
        WEEK_START, WEEK_END, [], now=WEEK_END) is False


# --------------------------------------------------------------------------
# End to end: a partial range withholds the residual (spec §5.2)
# --------------------------------------------------------------------------
E2E_WEEK_START = dt.datetime(2026, 6, 1, tzinfo=UTC)
E2E_WEEK_END = E2E_WEEK_START + dt.timedelta(days=7)
E2E_AS_OF = "2026-06-09T12:00:00Z"


def _seed_e2e_store(app):
    """One fully-snapshotted, fully-modelled subscription week."""
    import _lib_quota_model as qm

    conn = app.open_db()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots("
            " captured_at_utc, week_start_date, week_end_date, week_start_at,"
            " week_end_at, weekly_percent, source, payload_json)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("2026-06-07T00:00:00Z", "2026-06-01", "2026-06-08",
             E2E_WEEK_START.isoformat().replace("+00:00", "Z"),
             E2E_WEEK_END.isoformat().replace("+00:00", "Z"),
             40.0, "fixture", "{}"))
        conn.commit()
    finally:
        conn.close()

    entries = []
    cache = app.open_cache_db()
    try:
        for index, day in enumerate((1, 3, 5)):
            at = E2E_WEEK_START + dt.timedelta(days=day)
            path = f"/fake/repos/alpha/e{index}.jsonl"
            cache.execute(
                "INSERT INTO session_files(path, size_bytes, mtime_ns,"
                " last_byte_offset, last_ingested_at, session_id,"
                " project_path) VALUES (?,?,?,?,?,?,?)",
                (path, 0, 0, 0, "2026-06-09T00:00:00Z", f"s{index}",
                 "/fake/repos/alpha"))
            cache.execute(
                "INSERT INTO session_entries(source_path, line_offset,"
                " timestamp_utc, model, msg_id, req_id, input_tokens,"
                " output_tokens, cache_create_tokens, cache_create_1h_tokens,"
                " cache_read_tokens, cost_usd_raw)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (path, 0, at.isoformat(), "claude-opus-4-5", f"m{index}",
                 f"r{index}", 200_000, 40_000, 0, 0, 0, None))
            entries.append(qm.EntryRecord(
                at=at, model="claude-opus-4-5", fresh=200_000, output=40_000,
                cache_create_total=0, cache_1h=0, cache_read=0))
        cache.commit()
    finally:
        cache.close()

    family_shares, class_shares = qm.aggregate_composition(entries)
    units = sum(qm.weighted_units(e) for e in entries)
    payload = {
        "schemaVersion": 1,
        "accounts": {"*": {"regimes": [{
            "effectiveFrom": (
                E2E_WEEK_START - dt.timedelta(days=30)).isoformat(),
            "effectiveUntil": None,
            "fingerprint": qm.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
            "algorithmRevision": qm.QUOTA_MODEL_ALGORITHM_REVISION,
            "unitsPerPoint": units / 30.0,
            "interval": {"lo": units / 30.0 * 0.95,
                         "hi": units / 30.0 * 1.05},
            "support": {"days": 26, "segments": 4},
            "status": "ok",
            "asOf": E2E_WEEK_START.isoformat(),
            "qualifications": [],
            "familyShares": dict(family_shares),
            "classShares": dict(class_shares),
            "familyRadius": 0.05,
            "classRadius": 0.05,
        }]}},
    }
    import _cctally_core
    path = _cctally_core.APP_DIR / "quota-calibrations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def e2e_app(monkeypatch, tmp_path):
    from conftest import redirect_paths

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CCTALLY_AS_OF", E2E_AS_OF)
    app = sys.modules["cctally"]
    _seed_e2e_store(app)
    return app


def _run_project(app, capsys, *extra):
    rc = app.main(["project", "--json", *extra])
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


def test_a_whole_week_range_states_the_residual(e2e_app, capsys):
    """The non-vacuity twin. Without it the partial-range test below would
    pass over a build whose residual was withheld for some other reason."""
    payload = _run_project(
        e2e_app, capsys, "--since", "2026-06-01", "--until", "2026-06-07")
    totals = payload["attribution"]["totals"]
    assert payload["attribution"]["basis"] == "modelled", payload[
        "attribution"]
    assert totals["residualCause"] is None, totals
    assert totals["observedMinusModelledPoints"] is not None, totals


def test_a_modelled_week_without_a_snapshot_names_the_absent_observed_side(
        e2e_app, capsys):
    """The end-to-end twin of `residual_absence_cause`.

    The population aligns — one resolved account, one whole subscription
    week, no filter and no fallback — and the residual is still absent,
    because the week carries no meter snapshot. Reporting that as
    `population-misaligned` told the user their window or filters were at
    fault, which is a false reason and, since the section 5.2 footer, one a
    terminal user reads as a sentence.
    """
    conn = e2e_app.open_db()
    try:
        conn.execute("DELETE FROM weekly_usage_snapshots")
        conn.commit()
    finally:
        conn.close()
    payload = _run_project(
        e2e_app, capsys, "--since", "2026-06-01", "--until", "2026-06-07")
    totals = payload["attribution"]["totals"]
    assert totals["observedMinusModelledPoints"] is None, totals
    assert totals["residualCause"] == "observed-absent", totals


def test_a_partial_range_withholds_the_residual(e2e_app, capsys):
    """`--since 2026-06-03 --until 2026-06-05` used to publish a whole-week
    residual beside a three-day range, because `whole_weeks` asked whether
    every week had a SNAPSHOT rather than whether the range covered the week
    whole. The meter reading is the week's; the modelled population is three
    days of it."""
    payload = _run_project(
        e2e_app, capsys, "--since", "2026-06-03", "--until", "2026-06-05")
    totals = payload["attribution"]["totals"]
    assert totals["residualCause"] == "population-misaligned", totals
    assert totals["observedMinusModelledPoints"] is None, totals
