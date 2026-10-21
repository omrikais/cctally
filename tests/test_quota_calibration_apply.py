"""#661 S2 Task A2 — the apply adapter and its parity with `cctally quota`.

A stored `status: "ok"` is a fact about the population S1 fitted on, not about
the population a consumer is about to measure. `apply_regime` therefore
re-tests composition support against the consumer's own entries before it
converts anything, and reproduces S1's interval inversion rather than leaving
each consumer to re-derive it.

The parity test at the bottom pins the adapter's consumption number against
`cctally quota`'s own, on a shared fixture. Two independent paths to one
quantity diverge silently unless something binds them.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest

# conftest puts bin/ on sys.path.
import _lib_quota_calibration as qc
import _lib_quota_model as qm

UTC = dt.timezone.utc
ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The `01-steady-no-change` scenario's pinned clock, mirrored from its
#: committed `input.env` so the parity run measures the same instant the
#: golden does.
PARITY_SCENARIO = "01-steady-no-change"
PARITY_AS_OF = "2026-08-28T06:00:00Z"

_CLASS_SHARES_FRESH = {"fresh": 1.0, "output": 0.0,
                       "cache_1h": 0.0, "cache_read": 0.0}


def _validated(**over):
    base = dict(
        account_key=None,
        effective_from=dt.datetime(2026, 7, 25, tzinfo=UTC),
        effective_until=None,
        units_per_point=2_000_000.0,
        interval_lo=1_800_000.0,
        interval_hi=2_200_000.0,
        status="ok",
        as_of=dt.datetime(2026, 8, 28, tzinfo=UTC),
        family_shares={"claude-opus-5": 1.0},
        class_shares=dict(_CLASS_SHARES_FRESH),
        family_radius=0.02,
        class_radius=0.05,
        coefficient_era="opus-5",
        qualifications=(),
    )
    base.update(over)
    return qc.ValidatedRegime(**base)


#: The subscription week the `_entry` default instant (2026-08-26) sits in,
#: wholly inside the `opus-5` era.
_WEEK = (dt.datetime(2026, 8, 24, tzinfo=UTC),
         dt.datetime(2026, 8, 31, tzinfo=UTC))


def _entry(model="claude-opus-5", *, fresh=0, output=0, cache_create_total=0,
           cache_1h=0, cache_read=0, at=None):
    return qm.EntryRecord(
        at=at or dt.datetime(2026, 8, 26, 12, tzinfo=UTC), model=model,
        fresh=fresh, output=output, cache_create_total=cache_create_total,
        cache_1h=cache_1h, cache_read=cache_read)


def _entries_with_units(units):
    """A population of exactly `units` weighted units, all fresh Opus 5.

    `fresh` carries weight 1.0, so the token count IS the unit count and the
    expected arithmetic below stays readable.
    """
    return [_entry(fresh=int(units))]


def _entries_all_family(family):
    return [_entry(model=family, fresh=4_000_000)]


# --------------------------------------------------------------------------
# Support is re-tested against the consumer's own population
# --------------------------------------------------------------------------
def test_a2_unsupported_composition_is_rejected_even_when_status_is_ok():
    # A stored status of "ok" is a fact about S1's FIT population, not about
    # this consumer's. A population outside the recorded radii must reject.
    regime = _validated(status="ok", family_shares={"claude-opus-5": 1.0},
                        family_radius=0.02)
    entries = _entries_all_family("claude-haiku-4-5")
    assert qc.apply_regime(regime, entries, interval=_WEEK) is \
        qc.ApplyRejection.UNSUPPORTED_COMPOSITION


def test_a2_a_token_class_shift_inside_the_family_radius_is_still_caught():
    """The family radius alone is not enough: one family can hold its share
    at 1.0 while the output-to-fresh mix moves the effective scale."""
    regime = _validated(class_radius=0.05)
    entries = [_entry(fresh=1_000_000, output=1_000_000)]
    assert qc.apply_regime(regime, entries, interval=_WEEK) is \
        qc.ApplyRejection.UNSUPPORTED_COMPOSITION


def test_a2_an_absent_radius_fails_closed_rather_than_admitting_everything():
    regime = _validated(family_radius=None)
    assert qc.apply_regime(regime, _entries_with_units(4_000_000.0), interval=_WEEK) is \
        qc.ApplyRejection.UNSUPPORTED_COMPOSITION
    regime = _validated(class_radius=None)
    assert qc.apply_regime(regime, _entries_with_units(4_000_000.0), interval=_WEEK) is \
        qc.ApplyRejection.UNSUPPORTED_COMPOSITION


# --------------------------------------------------------------------------
# The arithmetic, including the inversion
# --------------------------------------------------------------------------
def test_a2_interval_inversion_flips_the_bounds():
    # points = units / units_per_point, so the LARGER budget yields the
    # SMALLER point estimate. A copied-through interval would invert the band.
    regime = _validated(units_per_point=2_000_000.0,
                        interval_lo=1_800_000.0, interval_hi=2_200_000.0)
    out = qc.apply_regime(regime, _entries_with_units(4_000_000.0), interval=_WEEK)
    assert out.consumed_points == pytest.approx(2.0)
    assert out.consumed_lo == pytest.approx(4_000_000.0 / 2_200_000.0)
    assert out.consumed_hi == pytest.approx(4_000_000.0 / 1_800_000.0)
    assert out.consumed_lo < out.consumed_points < out.consumed_hi
    assert out.units == pytest.approx(4_000_000.0)


def test_a2_an_unbounded_upper_budget_floors_the_lower_consumption_at_zero():
    """`interval_hi is None` means the budget is unbounded above, so modelled
    consumption is bounded below by zero rather than by infinity."""
    out = qc.apply_regime(_validated(interval_hi=None),
                          _entries_with_units(4_000_000.0), interval=_WEEK)
    assert out.consumed_lo == 0.0
    assert out.consumed_hi == pytest.approx(4_000_000.0 / 1_800_000.0)


def test_a2_the_inversion_matches_the_shipped_kernel_arithmetic():
    """The same numbers the kernel's own consumption branch produces."""
    units, point, lo, hi = 4_000_000.0, 2_000_000.0, 1_800_000.0, 2_200_000.0
    out = qc.apply_regime(
        _validated(units_per_point=point, interval_lo=lo, interval_hi=hi),
        _entries_with_units(units), interval=_WEEK)
    assert out.consumed_points == pytest.approx(units / point)
    assert out.consumed_lo == pytest.approx(units / hi)
    assert out.consumed_hi == pytest.approx(units / lo)


def test_a2_empty_population_rejects_rather_than_returning_zero():
    assert qc.apply_regime(_validated(), [], interval=_WEEK) is \
        qc.ApplyRejection.EMPTY_POPULATION


def test_a2_a_population_of_only_unknown_splits_is_empty_not_zero():
    """A NULL one-hour column against a positive cache-write total is an
    unknown split, not zero one-hour writes. Such a population supports no
    comparison at all."""
    entries = [_entry(cache_create_total=1_000, cache_1h=None)]
    assert qc.apply_regime(_validated(), entries, interval=_WEEK) is \
        qc.ApplyRejection.EMPTY_POPULATION


def test_a2_units_exclude_a_family_that_does_not_drain_the_general_meter():
    """A supported population that also carries an unrecognized family must
    not have that family's tokens priced into the unit total."""
    supported = _entries_with_units(4_000_000.0)
    out_clean = qc.apply_regime(_validated(), supported, interval=_WEEK)
    with_unknown = supported + [_entry(model="some-unmodelled-thing",
                                       fresh=9_000_000)]
    out_mixed = qc.apply_regime(_validated(), with_unknown, interval=_WEEK)
    assert out_mixed.units == pytest.approx(out_clean.units)


def test_a2_cache_reads_are_weighted_far_below_fresh_input():
    """Cache reads carry 0.0031 against fresh input's 1.0. A cost-share proxy
    over-credits a cache-heavy population; the weighted units do not."""
    regime = _validated(class_shares={"fresh": 0.0, "output": 0.0,
                                      "cache_1h": 0.0, "cache_read": 1.0})
    out = qc.apply_regime(regime, [_entry(cache_read=4_000_000)],
                          interval=_WEEK)
    # The `_entry` default instant (2026-08-26) pins the Opus 5 meaning.
    assert out.units == pytest.approx(4_000_000.0 * 0.0031)
    # #978: in the `opus-5-5` era cache reads carry 0.0148, still far below
    # fresh input's 1.0.
    new_era = dt.datetime(2026, 9, 24, 12, tzinfo=UTC)
    out = qc.apply_regime(
        _validated(class_shares=dict(regime.class_shares),
                   coefficient_era="opus-5-5"),
        [_entry(cache_read=4_000_000, at=new_era)],
        interval=(dt.datetime(2026, 9, 23, tzinfo=UTC),
                  dt.datetime(2026, 9, 30, tzinfo=UTC)))
    assert out.units == pytest.approx(4_000_000.0 * 0.0148)


# --------------------------------------------------------------------------
# Parity against the shipped command
# --------------------------------------------------------------------------
def _build_scenario(tmp_path, name):
    subprocess.run(
        [sys.executable, str(ROOT / "bin" / "build-quota-fixtures.py"),
         "--out", str(tmp_path), "--scenario", name],
        check=True, capture_output=True)
    return tmp_path / name


def _fixture_env(home):
    env = {k: v for k, v in os.environ.items()
           if k not in {"CODEX_HOME", "DO_NOT_TRACK",
                        "CCTALLY_DISABLE_TELEMETRY", "CCTALLY_DATA_DIR"}}
    env.update({
        "HOME": str(home), "NO_COLOR": "1", "TZ": "Etc/UTC", "COLUMNS": "120",
        "CCTALLY_AS_OF": PARITY_AS_OF,
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
        "CCTALLY_DISABLE_RETENTION_SWEEP": "1",
    })
    return env


def _run_quota_json(home):
    proc = subprocess.run([str(ROOT / "bin" / "cctally"), "quota", "--json"],
                          env=_fixture_env(home), capture_output=True,
                          text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def _regime_from_payload(payload):
    """A `ValidatedRegime` carrying exactly what the command published.

    The composition centre is the BASELINE one, matching `_decorate`'s
    `analysis.family_shares`, and the radii are the EFFECTIVE (floor-clamped)
    values the regime record stores.
    """
    fitted = payload["calibration"]["fitted"]
    composition = payload["composition"]
    return qc.ValidatedRegime(
        account_key=None,
        effective_from=dt.datetime.fromisoformat(
            payload["scope"]["analysisStart"]),
        effective_until=None,
        units_per_point=fitted["value"],
        interval_lo=fitted["interval"]["lo"],
        interval_hi=fitted["interval"]["hi"],
        status=payload["status"],
        as_of=dt.datetime.fromisoformat(payload["generatedAt"]),
        family_shares=composition["baseline"]["familyShares"],
        class_shares=composition["baseline"]["classShares"],
        family_radius=composition["familyRadiusEffective"],
        class_radius=composition["classRadiusEffective"],
        # The parity scenario is an Opus 5 era fixture (#978).
        coefficient_era="opus-5",
    )


def _entries_from_fixture(home, payload):
    """The command's own forecast population, read back out of `cache.db`.

    `currentWeek.start` is `CurrentWeek.units_start` and the horizon is
    `min(now, week.end)`, which is what `analyse_account` slices on.
    """
    start = dt.datetime.fromisoformat(payload["currentWeek"]["start"])
    end = dt.datetime.fromisoformat(payload["currentWeek"]["end"])
    now = dt.datetime.fromisoformat(payload["generatedAt"])
    horizon = min(now, end)
    db = home / ".local" / "share" / "cctally" / "cache.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT timestamp_utc, model, input_tokens, output_tokens,"
            " cache_create_tokens, cache_create_1h_tokens, cache_read_tokens"
            " FROM session_entries ORDER BY timestamp_utc, id").fetchall()
    finally:
        conn.close()
    entries = []
    for row in rows:
        at = dt.datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        at = at.astimezone(UTC)
        if not (start <= at < horizon):
            continue
        entries.append(qm.EntryRecord(
            at=at, model=str(row[1] or ""), fresh=row[2] or 0,
            output=row[3] or 0, cache_create_total=row[4] or 0,
            cache_1h=row[5], cache_read=row[6] or 0))
    return entries


def test_a2_parity_with_cctally_quota_on_a_shared_fixture(tmp_path):
    """The adapter must reproduce `cctally quota`'s own consumption number.

    Two independent paths to one quantity diverge silently unless pinned.
    """
    home = _build_scenario(tmp_path, PARITY_SCENARIO)
    payload = _run_quota_json(home)
    assert payload["currentWeek"]["consumption"]["state"] == "available", (
        "the parity fixture must publish a consumption figure, or this test "
        "asserts nothing")
    regime = _regime_from_payload(payload)
    entries = _entries_from_fixture(home, payload)
    assert entries, "the forecast population is empty; parity would be vacuous"

    week = (dt.datetime.fromisoformat(payload["currentWeek"]["start"]),
            dt.datetime.fromisoformat(payload["currentWeek"]["end"]))
    applied = qc.apply_regime(regime, entries, interval=week)
    assert isinstance(applied, qc.AppliedQuota), applied
    assert applied.consumed_points == pytest.approx(
        payload["currentWeek"]["consumption"]["value"], abs=1e-9)
    assert applied.consumed_lo == pytest.approx(
        payload["currentWeek"]["consumption"]["interval"]["lo"], abs=1e-9)
    assert applied.consumed_hi == pytest.approx(
        payload["currentWeek"]["consumption"]["interval"]["hi"], abs=1e-9)


def test_a2_parity_fixture_is_not_a_degenerate_one_point_week(tmp_path):
    """Guard the guard: a consumption of ~0 would let a broken adapter agree
    with the command by both returning nothing worth comparing."""
    home = _build_scenario(tmp_path, PARITY_SCENARIO)
    payload = _run_quota_json(home)
    value = payload["currentWeek"]["consumption"]["value"]
    assert value > 1.0, value
    interval = payload["currentWeek"]["consumption"]["interval"]
    assert interval["lo"] < value < interval["hi"]


# --------------------------------------------------------------------------
# #978 — a regime applies only inside its own coefficient era (spec 4.6)
# --------------------------------------------------------------------------
def _at(day, hour=12):
    return dt.datetime(2026, 9, day, hour, tzinfo=UTC)


def _era_days(*days, model="claude-opus-5-5"):
    return [_entry(model=model, fresh=1_000_000, at=_at(day)) for day in days]


def _era_regime(era_id, *, family="claude-opus-5-5", **over):
    base = dict(coefficient_era=era_id, family_shares={family: 1.0},
                family_radius=1.0, class_radius=1.0)
    base.update(over)
    return _validated(**base)


def test_apply_rejects_out_of_era_entry():
    regime = _era_regime("opus-5-5")
    interval = (_at(23, 0), _at(30, 0))
    in_era = _era_days(23, 24, 25, 26)
    # Non-vacuity: the in-era population alone applies.
    assert isinstance(qc.apply_regime(regime, in_era, interval=interval),
                      qc.AppliedQuota)
    stray = _era_days(21, model="claude-opus-5")
    assert qc.apply_regime(regime, in_era + stray, interval=interval) is \
        qc.ApplyRejection.UNVALIDATED_COEFFICIENT_ERA


def test_apply_rejects_interval_starting_before_era():
    # Every entry is in-era, yet modelling the whole week is forbidden.
    regime = _era_regime("opus-5-5")
    entries = _era_days(23, 24, 25, 26)
    assert qc.apply_regime(regime, entries,
                           interval=(_at(20, 0), _at(27, 0))) is \
        qc.ApplyRejection.UNVALIDATED_COEFFICIENT_ERA
    assert isinstance(qc.apply_regime(regime, entries,
                                      interval=(_at(23, 0), _at(30, 0))),
                      qc.AppliedQuota)


def test_apply_rejects_interval_ending_after_closed_era():
    regime = _era_regime("opus-5", family="claude-opus-5")
    entries = _era_days(19, 20, 21, model="claude-opus-5")
    assert qc.apply_regime(regime, entries,
                           interval=(_at(19, 0), _at(26, 0))) is \
        qc.ApplyRejection.UNVALIDATED_COEFFICIENT_ERA
    assert isinstance(qc.apply_regime(regime, entries,
                                      interval=(_at(15, 0), _at(22, 0))),
                      qc.AppliedQuota)


def test_apply_in_era_positive_control():
    # The three hand-worked 5.5 entries of the kernel's RED test 2b weigh
    # 1868 + 6640 + 3400 = 11908 units under the `opus-5-5` coefficients.
    entries = [
        _entry(model="claude-opus-5-5", fresh=1000, output=200,
               cache_read=50000, at=_at(24, 3)),
        _entry(model="claude-sonnet-5-5", fresh=4000, output=1000,
               cache_create_total=2000, cache_1h=500, at=_at(24, 4)),
        _entry(model="claude-haiku-5-5", output=3000, cache_1h=None,
               cache_read=100000, at=_at(24, 5)),
    ]
    regime = _era_regime("opus-5-5", units_per_point=2000.0,
                         interval_lo=1800.0, interval_hi=2200.0,
                         family_shares={"claude-opus-5-5": 0.157,
                                        "claude-sonnet-5-5": 0.558,
                                        "claude-haiku-5-5": 0.285})
    out = qc.apply_regime(regime, entries, interval=(_at(23, 0), _at(30, 0)))
    assert isinstance(out, qc.AppliedQuota), out
    assert out.units == pytest.approx(11908.0)
    assert out.consumed_points == pytest.approx(11908.0 / 2000.0)


def _write_era_calibration(app_dir, *, era_id, family, effective_from):
    import _lib_quota_model as kernel

    payload = {
        "schemaVersion": 1,
        "accounts": {"*": {"regimes": [{
            "effectiveFrom": effective_from.isoformat(),
            "effectiveUntil": None,
            "fingerprint": kernel.QUOTA_MODEL_CONSTANTS_FINGERPRINT,
            "algorithmRevision": kernel.QUOTA_MODEL_ALGORITHM_REVISION,
            "coefficientEra": era_id,
            "unitsPerPoint": 2_000_000.0,
            "interval": {"lo": 1_800_000.0, "hi": 2_200_000.0},
            "support": {"days": 26, "segments": 4},
            "status": "ok",
            "asOf": effective_from.isoformat(),
            "qualifications": [],
            "familyShares": {family: 1.0},
            "classShares": dict(_CLASS_SHARES_FRESH),
            "familyRadius": 0.02,
            "classRadius": 0.05,
        }]}},
    }
    path = app_dir / "quota-calibrations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _cross_era_store(tmp_path, monkeypatch, week_start):
    """A current week anchored at `week_start`, an `opus-5-5` regime and two
    in-era Opus 5.5 entries, built with the forecast suite's own helpers."""
    from conftest import load_isolated_cctally_module
    from test_projection_basis_selector import _seed_snapshots
    import _cctally_core

    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    conn = _cctally_core.open_db()
    cache = mod._load_sibling("_cctally_cache").open_cache_db()
    try:
        for index, at in enumerate((_at(23, 2), _at(23, 6))):
            cache.execute(
                "INSERT INTO session_entries(source_path, line_offset,"
                " timestamp_utc, model, input_tokens, output_tokens,"
                " cache_create_tokens, cache_create_1h_tokens,"
                " cache_read_tokens) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"/fx/978-{index}.jsonl", index, at.isoformat(),
                 "claude-opus-5-5", 20_000_000, 0, 0, 0, 0))
        cache.commit()
    finally:
        cache.close()
    now = _at(23, 12)
    hours = (now - week_start).total_seconds() / 3600.0
    _seed_snapshots(conn, [(0, 2.0), (hours - 12, 8.0), (hours - 4, 12.0),
                           (hours, 40.0)], week_start=week_start)
    # `effectiveFrom` precedes every week below so that project's own
    # regime-boundary check cannot mask the coefficient-era guard under test.
    _write_era_calibration(_cctally_core.APP_DIR, era_id="opus-5-5",
                           family="claude-opus-5-5",
                           effective_from=dt.datetime(2026, 9, 1,
                                                      tzinfo=UTC))
    monkeypatch.setattr(mod, "_sum_cost_for_range", lambda *a, **k: 20.0)
    return mod, conn, now


@pytest.mark.parametrize("week_start,crosses", [
    (dt.datetime(2026, 9, 20, tzinfo=UTC), True),
    # Non-vacuity: the same store anchored inside the era is calibrated.
    (dt.datetime(2026, 9, 22, 6, tzinfo=UTC), False),
])
def test_cross_era_rejection_reaches_each_consumer_fallback(
        tmp_path, monkeypatch, week_start, crosses):
    import _lib_forecast as fc

    mod, conn, now = _cross_era_store(tmp_path, monkeypatch, week_start)
    try:
        # Forecast: the corrected meter, with the typed calibration cause.
        inputs = mod._load_forecast_inputs(conn, now, skip_sync=True)
        selection = fc.select_projection_basis(inputs)
        if crosses:
            assert inputs.calibrated_projection_pct is None
            assert inputs.calibrated_withheld_code == \
                "unvalidated-coefficient-era"
            assert selection.basis is fc.ProjectionBasis.CORRECTED_METER
            assert inputs.calibrated_withheld_code in qm.EVIDENCE_CODES
        else:
            assert selection.basis is fc.ProjectionBasis.CALIBRATED
        # Projected alerts follow the forecast's selected basis exactly.
        projected = mod._weekly_pct_week_avg_projection(conn, now)
        assert projected is not None
        assert projected[0] == pytest.approx(selection.value, abs=1e-9)
    finally:
        conn.close()

    # Project: the whole subscription week falls back to cost share.
    project = mod._load_sibling("_cctally_project")
    read = mod._load_sibling("_cctally_quota_calibration") \
        .read_calibration_file(account_key=None)
    assert read.regime is not None and read.regime.coefficient_era == \
        "opus-5-5"

    class _Joined:
        model = "claude-opus-5-5"
        input_tokens = 20_000_000
        output_tokens = 0
        cache_creation_tokens = 0
        cache_read_tokens = 0
        cache_1h_tokens = 0
        timestamp = _at(23, 2)

    records = [project._entry_quota_record(_Joined())[0]]
    attribution = project.resolve_week_attribution(
        read.regime, records, week_start=week_start,
        week_end=week_start + dt.timedelta(days=7))
    if crosses:
        assert attribution.basis == "cost-share"
        assert attribution.cause == "unvalidated-coefficient-era"
    else:
        assert attribution.basis == "modelled"


# --------------------------------------------------------------------------
# #978 spec 4.2 invariant 5: population-level weighting refuses an entry
# outside its era rather than skipping it.
# --------------------------------------------------------------------------
def _era_entry(at, **over):
    base = dict(at=at, model="claude-opus-5", fresh=1000, output=100,
                cache_create_total=0, cache_1h=0, cache_read=0)
    base.update(over)
    return qm.EntryRecord(**base)


def test_population_units_refuses_an_out_of_era_entry():
    """A silent skip would divide a smaller population by the regime's
    budget and report it as the whole one; the kernel's own population
    functions raise, and so must this one."""
    era = qm.era_by_id("opus-5")
    inside = _era_entry(dt.datetime(2026, 8, 3, 12, tzinfo=UTC))
    outside = _era_entry(dt.datetime(2026, 9, 23, 12, tzinfo=UTC))
    with pytest.raises(qm.CoefficientEraMismatch):
        qc.population_units([inside, outside], era=era)


def test_population_units_still_skips_an_unknown_token_split():
    """The other `None` from weighting is unchanged: an in-era entry whose
    cache-write split is unknown contributes nothing."""
    era = qm.era_by_id("opus-5")
    inside = _era_entry(dt.datetime(2026, 8, 3, 12, tzinfo=UTC))
    split = _era_entry(dt.datetime(2026, 8, 3, 13, tzinfo=UTC),
                       cache_create_total=500, cache_1h=None)
    assert qm.weigh_entry(split, era).cause == qm.SPLIT_UNKNOWN
    assert qc.population_units([inside, split], era=era) == pytest.approx(
        qm.weighted_units(inside, era))
