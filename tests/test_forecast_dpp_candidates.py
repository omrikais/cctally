"""#661 S2 Task B2 — which prior weeks may enter the trailing median (§4.2).

Spec §4.2 restricts trailing-week candidates to the target week's own S1
metering regime, and — because §2.1 established that same-regime is necessary
but not sufficient — additionally requires each candidate to satisfy a stated
model-mix comparability condition. The condition is not invented here: it is
§1.1's apply adapter run over the candidate week's own population, so a week
whose composition has drifted outside the regime's recorded radii is the same
thing `cctally quota` would refuse to model.

Both clauses are gated on a trustworthy regime existing. The validated reader
publishes only the OPEN regime and refuses one S1 marked `detection-only`, so
on an install with no calibration — every fixture in this estate, and the
maintainer's own store today — there is no regime information at all and the
restriction is inert. Applying it anyway would exclude every candidate rather
than the incomparable ones, which would silently kill the trailing branch for
every user who has never run `cctally quota`.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from conftest import load_script

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 6, tzinfo=UTC)
CURRENT_WEEK_START = dt.datetime(2026, 6, 29, tzinfo=UTC)

#: The four prior weeks, oldest first. Costs are chosen so the credited week's
#: movement-versus-floored denominator MOVES the median rather than staying an
#: extreme the median steps over.
WEEKS = [
    (dt.datetime(2026, 6, 1, tzinfo=UTC), 154.0),
    (dt.datetime(2026, 6, 8, tzinfo=UTC), 40.0),
    (dt.datetime(2026, 6, 15, tzinfo=UTC), 120.0),
    (dt.datetime(2026, 6, 22, tzinfo=UTC), 200.0),
]
_COST_BY_START = {start.date(): cost for start, cost in WEEKS}


def _cost_fn(week_start, _week_end, mode="auto", skip_sync=False, **_kwargs):
    return _COST_BY_START[week_start.date()]


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE weekly_usage_snapshots ("
        " week_start_date TEXT, week_start_at TEXT, week_end_at TEXT,"
        " captured_at_utc TEXT, weekly_percent REAL, source TEXT,"
        " payload_json TEXT)")
    conn.execute(
        "CREATE TABLE weekly_credit_floors (week_start_date TEXT,"
        " effective_at_utc TEXT)")
    # #703 + #707 added the accounting facts the boundary reducer reads:
    # `week_start_date` selects a credit by the WEEK it names, and
    # `observed_at_utc` is the EXACT instant a segment boundary sits at. A
    # fixture without them makes every read fail closed.
    conn.execute(
        "CREATE TABLE week_reset_events (effective_reset_at_utc TEXT,"
        " old_week_end_at TEXT, new_week_end_at TEXT,"
        " week_start_date TEXT, observed_at_utc TEXT)")
    return conn


def _seed(conn, start, rows):
    """`rows` is `[(hours, percent[, credit_effective_hours])]`."""
    end = start + dt.timedelta(days=7)
    for row in rows:
        at = start + dt.timedelta(hours=row[0])
        if len(row) == 2:
            source, payload = "userscript", "{}"
        else:
            source = "record-credit"
            payload = json.dumps({
                "kind": "record-credit", "from": 46.0, "to": row[1],
                "effective": (start + dt.timedelta(hours=row[2])).isoformat(
                    timespec="seconds")})
        conn.execute(
            "INSERT INTO weekly_usage_snapshots VALUES (?,?,?,?,?,?,?)",
            (start.date().isoformat(), start.isoformat(), end.isoformat(),
             at.isoformat(), row[1], source, payload))


def _seed_plain(conn, starts):
    for start in starts:
        _seed(conn, start, [(24, 20.0), (120, 40.0)])


def _run(ns, conn, *, p_now=5.0, spent=50.0):
    original = ns["_sum_cost_for_range"]
    ns["_sum_cost_for_range"] = _cost_fn
    try:
        return ns["_select_dollars_per_percent"](
            conn, NOW, CURRENT_WEEK_START, p_now, spent, skip_sync=True)
    finally:
        ns["_sum_cost_for_range"] = original


# --------------------------------------------------------------------------
# The denominator itself
# --------------------------------------------------------------------------
def test_b2_the_median_uses_movement_not_the_floored_high_water_mark():
    """The regression the measurement found.

    Week A is credited: 46, a synthetic post-credit baseline of 0, then 31.
    Its realized movement is 77 and its cost is $154, so $2.00 per point.
    `_floored_week_max` would have returned 31 — the post-credit fragment —
    for $4.97 per point. With the other three weeks at $1, $3 and $5 the
    movement median is $2.50 and the floored median would be $3.98.
    """
    ns = load_script()
    conn = _conn()
    _seed(conn, WEEKS[0][0], [(24, 46.0), (73, 0.0, 72), (120, 31.0)])
    conn.execute("INSERT INTO weekly_credit_floors VALUES (?, ?)",
                 ("2026-06-01", "2026-06-04T00:00:00+00:00"))
    _seed_plain(conn, [start for start, _ in WEEKS[1:]])
    dpp, source = _run(ns, conn)
    assert source == "trailing_4wk_median"
    assert dpp == pytest.approx(2.5)
    conn.close()


def test_b2_a_withheld_week_is_excluded_from_the_median():
    """The same credited week WITHOUT a synthetic baseline is unknowable, so
    three candidates remain where the median needs four."""
    ns = load_script()
    conn = _conn()
    _seed(conn, WEEKS[0][0], [(24, 46.0), (120, 31.0)])
    conn.execute("INSERT INTO weekly_credit_floors VALUES (?, ?)",
                 ("2026-06-01", "2026-06-04T00:00:00+00:00"))
    _seed_plain(conn, [start for start, _ in WEEKS[1:]])
    dpp, source = _run(ns, conn)
    assert source == "this_week_sparse"
    assert dpp == pytest.approx(10.0)
    conn.close()


def test_b2_a_right_censored_week_is_excluded_from_the_median():
    ns = load_script()
    conn = _conn()
    _seed(conn, WEEKS[0][0], [(24, 46.0), (120, 100.0)])
    _seed_plain(conn, [start for start, _ in WEEKS[1:]])
    _dpp, source = _run(ns, conn)
    assert source == "this_week_sparse"
    conn.close()


def test_b2_a_week_below_one_point_of_movement_is_not_a_candidate():
    """The existing `>= 1.0` eligibility gate now reads movement."""
    ns = load_script()
    conn = _conn()
    _seed(conn, WEEKS[0][0], [(24, 0.4)])
    _seed_plain(conn, [start for start, _ in WEEKS[1:]])
    _dpp, source = _run(ns, conn)
    assert source == "this_week_sparse"
    conn.close()


# --------------------------------------------------------------------------
# The regime restriction
# --------------------------------------------------------------------------
def _regime(ns, *, effective_from, effective_until=None):
    qcg = ns["_load_sibling"]("_cctally_quota_calibration")
    return qcg.ValidatedRegime(
        account_key=None,
        effective_from=effective_from,
        effective_until=effective_until,
        units_per_point=2_400_000.0,
        interval_lo=2_300_000.0,
        interval_hi=2_500_000.0,
        status="ok",
        as_of=NOW,
        family_shares={"opus": 1.0},
        class_shares={"output": 1.0},
        family_radius=0.2,
        class_radius=0.2,
    )


def _pin_calibration(ns, monkeypatch, regime, *, unsupported_weeks=()):
    """Make the reader publish `regime` and the adapter refuse `unsupported`."""
    qcg = ns["_load_sibling"]("_cctally_quota_calibration")
    forecast = ns["_cctally_forecast"]
    monkeypatch.setattr(
        qcg, "read_calibration_file",
        lambda **_kwargs: qcg.CalibrationRead(regime, None))
    monkeypatch.setattr(
        forecast, "_week_entry_records",
        lambda start, _end, **_kwargs: [start])
    unsupported = {start for start in unsupported_weeks}
    monkeypatch.setattr(
        qcg, "apply_regime",
        lambda _regime, entries: (
            qcg.ApplyRejection.UNSUPPORTED_COMPOSITION
            if entries and entries[0] in unsupported
            else qcg.AppliedQuota(10.0, 9.0, 11.0, 1.0)))


def test_b2_no_trustworthy_regime_leaves_the_restriction_inert(monkeypatch):
    """The reader refuses on every install with no calibration, and on the
    maintainer's store today, whose open regime is `detection-only`."""
    ns = load_script()
    qcg = ns["_load_sibling"]("_cctally_quota_calibration")
    monkeypatch.setattr(
        qcg, "read_calibration_file",
        lambda **_kwargs: qcg.CalibrationRead(
            None, qcg.RegimeRejection.DETECTION_ONLY))
    conn = _conn()
    _seed_plain(conn, [start for start, _ in WEEKS])
    _dpp, source = _run(ns, conn)
    assert source == "trailing_4wk_median"
    conn.close()


def test_b2_candidates_do_not_cross_a_metering_regime(monkeypatch):
    ns = load_script()
    conn = _conn()
    _seed_plain(conn, [start for start, _ in WEEKS])
    _pin_calibration(ns, monkeypatch,
                     _regime(ns, effective_from=dt.datetime(
                         2026, 6, 15, tzinfo=UTC)))
    _dpp, source = _run(ns, conn)
    assert source == "this_week_sparse"
    conn.close()


def test_b2_a_target_week_predating_the_open_regime_is_not_restricted(
        monkeypatch):
    """The reader publishes only the OPEN regime, so a target week inside a
    closed predecessor cannot be matched against anything. Restricting it
    would exclude every candidate rather than the incomparable ones."""
    ns = load_script()
    conn = _conn()
    _seed_plain(conn, [start for start, _ in WEEKS])
    _pin_calibration(ns, monkeypatch,
                     _regime(ns, effective_from=dt.datetime(
                         2026, 7, 20, tzinfo=UTC)))
    _dpp, source = _run(ns, conn)
    assert source == "trailing_4wk_median"
    conn.close()


def test_b2_an_incomparable_week_is_dropped_and_the_rate_says_so(monkeypatch):
    """Five in-regime weeks, one of which fails the support test.

    Four comparable weeks remain, so the median is still published — but on a
    population one week of which was dropped for drift, which the companion
    source field states rather than a new key (the #620 S1 D5 precedent).
    """
    ns = load_script()
    conn = _conn()
    extra = dt.datetime(2026, 5, 25, tzinfo=UTC)
    _COST_BY_START[extra.date()] = 80.0
    try:
        _seed_plain(conn, [extra] + [start for start, _ in WEEKS])
        _pin_calibration(
            ns, monkeypatch,
            _regime(ns, effective_from=dt.datetime(2026, 5, 1, tzinfo=UTC)),
            unsupported_weeks=[WEEKS[0][0]])
        dpp, source = _run(ns, conn)
        assert source == "trailing_4wk_median_drifted"
        # The four survivors are the three plain weeks plus the extra one, at
        # $1.00, $3.00, $5.00 and $2.00 per 40 points of movement.
        assert dpp == pytest.approx(2.5)
    finally:
        _COST_BY_START.pop(extra.date(), None)
    conn.close()


def test_b2_a_comparable_population_is_not_labelled_drifted(monkeypatch):
    """The non-vacuity twin of the test above: with the same wiring and no
    unsupported week, the label must be the plain one."""
    ns = load_script()
    conn = _conn()
    _seed_plain(conn, [start for start, _ in WEEKS])
    _pin_calibration(
        ns, monkeypatch,
        _regime(ns, effective_from=dt.datetime(2026, 5, 1, tzinfo=UTC)))
    _dpp, source = _run(ns, conn)
    assert source == "trailing_4wk_median"
    conn.close()


def test_b2_drift_alone_never_publishes_a_rate_from_under_four_weeks(
        monkeypatch):
    """Dropping a week for drift falls back rather than crossing the gate."""
    ns = load_script()
    conn = _conn()
    _seed_plain(conn, [start for start, _ in WEEKS])
    _pin_calibration(
        ns, monkeypatch,
        _regime(ns, effective_from=dt.datetime(2026, 5, 1, tzinfo=UTC)),
        unsupported_weeks=[WEEKS[0][0]])
    _dpp, source = _run(ns, conn)
    assert source == "this_week_sparse"
    conn.close()
