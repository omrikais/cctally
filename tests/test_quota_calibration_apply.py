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
        qualifications=(),
    )
    base.update(over)
    return qc.ValidatedRegime(**base)


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
    assert qc.apply_regime(regime, entries) is \
        qc.ApplyRejection.UNSUPPORTED_COMPOSITION


def test_a2_a_token_class_shift_inside_the_family_radius_is_still_caught():
    """The family radius alone is not enough: one family can hold its share
    at 1.0 while the output-to-fresh mix moves the effective scale."""
    regime = _validated(class_radius=0.05)
    entries = [_entry(fresh=1_000_000, output=1_000_000)]
    assert qc.apply_regime(regime, entries) is \
        qc.ApplyRejection.UNSUPPORTED_COMPOSITION


def test_a2_an_absent_radius_fails_closed_rather_than_admitting_everything():
    regime = _validated(family_radius=None)
    assert qc.apply_regime(regime, _entries_with_units(4_000_000.0)) is \
        qc.ApplyRejection.UNSUPPORTED_COMPOSITION
    regime = _validated(class_radius=None)
    assert qc.apply_regime(regime, _entries_with_units(4_000_000.0)) is \
        qc.ApplyRejection.UNSUPPORTED_COMPOSITION


# --------------------------------------------------------------------------
# The arithmetic, including the inversion
# --------------------------------------------------------------------------
def test_a2_interval_inversion_flips_the_bounds():
    # points = units / units_per_point, so the LARGER budget yields the
    # SMALLER point estimate. A copied-through interval would invert the band.
    regime = _validated(units_per_point=2_000_000.0,
                        interval_lo=1_800_000.0, interval_hi=2_200_000.0)
    out = qc.apply_regime(regime, _entries_with_units(4_000_000.0))
    assert out.consumed_points == pytest.approx(2.0)
    assert out.consumed_lo == pytest.approx(4_000_000.0 / 2_200_000.0)
    assert out.consumed_hi == pytest.approx(4_000_000.0 / 1_800_000.0)
    assert out.consumed_lo < out.consumed_points < out.consumed_hi
    assert out.units == pytest.approx(4_000_000.0)


def test_a2_an_unbounded_upper_budget_floors_the_lower_consumption_at_zero():
    """`interval_hi is None` means the budget is unbounded above, so modelled
    consumption is bounded below by zero rather than by infinity."""
    out = qc.apply_regime(_validated(interval_hi=None),
                          _entries_with_units(4_000_000.0))
    assert out.consumed_lo == 0.0
    assert out.consumed_hi == pytest.approx(4_000_000.0 / 1_800_000.0)


def test_a2_the_inversion_matches_the_shipped_kernel_arithmetic():
    """The same numbers the kernel's own consumption branch produces."""
    units, point, lo, hi = 4_000_000.0, 2_000_000.0, 1_800_000.0, 2_200_000.0
    out = qc.apply_regime(
        _validated(units_per_point=point, interval_lo=lo, interval_hi=hi),
        _entries_with_units(units))
    assert out.consumed_points == pytest.approx(units / point)
    assert out.consumed_lo == pytest.approx(units / hi)
    assert out.consumed_hi == pytest.approx(units / lo)


def test_a2_empty_population_rejects_rather_than_returning_zero():
    assert qc.apply_regime(_validated(), []) is \
        qc.ApplyRejection.EMPTY_POPULATION


def test_a2_a_population_of_only_unknown_splits_is_empty_not_zero():
    """A NULL one-hour column against a positive cache-write total is an
    unknown split, not zero one-hour writes. Such a population supports no
    comparison at all."""
    entries = [_entry(cache_create_total=1_000, cache_1h=None)]
    assert qc.apply_regime(_validated(), entries) is \
        qc.ApplyRejection.EMPTY_POPULATION


def test_a2_units_exclude_a_family_that_does_not_drain_the_general_meter():
    """A supported population that also carries an unrecognized family must
    not have that family's tokens priced into the unit total."""
    supported = _entries_with_units(4_000_000.0)
    out_clean = qc.apply_regime(_validated(), supported)
    with_unknown = supported + [_entry(model="some-unmodelled-thing",
                                       fresh=9_000_000)]
    out_mixed = qc.apply_regime(_validated(), with_unknown)
    assert out_mixed.units == pytest.approx(out_clean.units)


def test_a2_cache_reads_are_weighted_far_below_fresh_input():
    """Cache reads carry 0.0031 against fresh input's 1.0. A cost-share proxy
    over-credits a cache-heavy population; the weighted units do not."""
    regime = _validated(class_shares={"fresh": 0.0, "output": 0.0,
                                      "cache_1h": 0.0, "cache_read": 1.0})
    out = qc.apply_regime(regime, [_entry(cache_read=4_000_000)])
    assert out.units == pytest.approx(4_000_000.0 * 0.0031)


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

    applied = qc.apply_regime(regime, entries)
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
