"""#661 S2 Stage C review — two store-read relaxations that failed OPEN.

Both sites caught `sqlite3.Error` and continued with a degraded answer. The
class of error is what makes that wrong: `sqlite3.Error` covers `database is
locked`, `file is not a database`, `disk I/O error` and `no such column`, and
only ONE of the conditions each site reasoned about — a table that is not
there at all — is safe to treat as "nothing to read".

1. `_cctally_project.probe_provider_decoration` decides whether a merged read
   must withhold modelled quota (spec §5.3). Answering `False` under WAL lock
   contention on a genuinely decorated two-account store publishes a cost
   share under `Used %`, which is exactly what §5.3 forbids.

2. `_cctally_forecast._week_segment_boundaries` supplies the movement
   reducer's segment edges. Losing the `weekly_credit_floors` read collapses a
   credited week to one segment and sums it as if the credit had zeroed the
   meter — the arithmetic §4.1 calls unknowable. Ten lines away
   `_snapshot_columns` fails CLOSED for the same class of store defect.

A third test pins the cost bound the same review required: the comparability
probe reads one subscription week of `session_entries` per candidate, and it
must stop once the median has the four candidates it needs.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from conftest import load_script

UTC = dt.timezone.utc


# --------------------------------------------------------------------------
# 1. The decoration probe
# --------------------------------------------------------------------------
class _RaisingConn:
    """A connection whose `accounts` reads raise, catalogue reads apart."""

    def __init__(self, *, catalogue_ok=True, accounts_present=True):
        self._catalogue_ok = catalogue_ok
        self._accounts_present = accounts_present

    def execute(self, sql, params=()):
        if "table_info" in sql or "sqlite_master" in sql:
            if not self._catalogue_ok:
                raise sqlite3.OperationalError("database is locked")
            return _Rows([("0", "account_key")] if self._accounts_present
                         else [])
        raise sqlite3.OperationalError("database is locked")


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _project():
    return load_script()["_cctally_project"]


def test_a_locked_store_with_an_accounts_table_is_treated_as_decorated():
    """The fail-closed direction. A decorated two-account store under lock
    contention must not answer `False`, because the merged read would then
    publish a cost share under `Used %` (spec §5.3)."""
    module = _project()
    assert module.probe_provider_decoration(
        _RaisingConn(accounts_present=True), "claude") is True


def test_a_store_with_no_accounts_table_is_not_decorated():
    """The ONE relaxation the original rationale actually justified: a store
    with no `accounts` table cannot hold more than one real account."""
    module = _project()
    assert module.probe_provider_decoration(
        _RaisingConn(accounts_present=False), "claude") is False


def test_an_unreadable_catalogue_is_also_treated_as_decorated():
    module = _project()
    assert module.probe_provider_decoration(
        _RaisingConn(catalogue_ok=False), "claude") is True


def test_a_real_single_account_store_is_still_undecorated():
    """The non-vacuity twin: the probe must not answer `True` for everything."""
    module = _project()
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE accounts (account_key TEXT, provider TEXT,"
                 " email TEXT, label TEXT)")
    conn.execute("INSERT INTO accounts VALUES ('a1','claude','a@x','A')")
    try:
        assert module.probe_provider_decoration(conn, "claude") is False
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 2. The movement reducer's boundary read
# --------------------------------------------------------------------------
WEEK_START = dt.datetime(2026, 6, 1, tzinfo=UTC)
WEEK_END = WEEK_START + dt.timedelta(days=7)
CREDIT_AT = WEEK_START + dt.timedelta(hours=72)


def _forecast():
    return load_script()["_cctally_forecast"]


def _credited_week_conn(*, drop_credit_floors=False):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE week_reset_events ("
                 " effective_reset_at_utc TEXT, account_key TEXT)")
    if not drop_credit_floors:
        conn.execute("CREATE TABLE weekly_credit_floors ("
                     " week_start_date TEXT, effective_at_utc TEXT,"
                     " account_key TEXT)")
        conn.execute("INSERT INTO weekly_credit_floors VALUES (?,?,?)",
                     ("2026-06-01", CREDIT_AT.isoformat(), "acct"))
    return conn


def _credited_readings():
    payload = json.dumps({"kind": "record-credit", "from": 46.0, "to": 0.0,
                          "effective": CREDIT_AT.isoformat(
                              timespec="seconds")})
    return [
        ((WEEK_START + dt.timedelta(hours=24)).isoformat(), 46.0,
         "userscript", "{}"),
        (CREDIT_AT.isoformat(), 0.0, "record-credit", payload),
        ((WEEK_START + dt.timedelta(hours=120)).isoformat(), 31.0,
         "userscript", "{}"),
    ]


def test_a_credited_week_is_measured_when_the_boundary_read_succeeds():
    """The non-vacuity twin of the fail-closed test below: with the credit
    floor readable the week segments and measures 46 + 31 = 77."""
    module = _forecast()
    conn = _credited_week_conn()
    try:
        movement = module._realized_week_movement(
            conn, WEEK_START, WEEK_END, "2026-06-01", _credited_readings())
    finally:
        conn.close()
    assert movement.withheld_cause is None
    assert movement.points == pytest.approx(77.0)
    assert movement.segments == 2


def test_an_unreadable_credit_floor_table_withholds_the_week():
    """Fail CLOSED. Losing the credit rows collapses `edges` to the week
    start and sums the week as ONE segment — 46 alone, because the drop to 0
    contributes nothing — which is the "assume the credit zeroed the meter"
    arithmetic §4.1 calls unknowable."""
    module = _forecast()
    conn = _credited_week_conn()
    try:
        conn.execute("DROP TABLE weekly_credit_floors")
        conn.execute("CREATE VIEW weekly_credit_floors AS"
                     " SELECT 1 AS week_start_date")
        movement = module._realized_week_movement(
            conn, WEEK_START, WEEK_END, "2026-06-01", _credited_readings())
    finally:
        conn.close()
    assert movement.points is None
    assert movement.withheld_cause == "boundary-records-unreadable"


def test_an_unusable_week_key_withholds_rather_than_skipping_the_credit_leg():
    """The third route to the same wrong arithmetic (#661 S2 Stage C review).

    `weekly_credit_floors` is keyed by `week_start_date`, so a falsy key
    cannot select the week's credit rows. The leg used to be SKIPPED there,
    with no exception, so a credited week summed as one segment — the exact
    arithmetic the two tests above fail closed to prevent, reached by a
    different route. The table is present and the key is unusable, so the
    credit records are unreadable and the week is withheld.
    """
    module = _forecast()
    conn = _credited_week_conn()
    try:
        movement = module._realized_week_movement(
            conn, WEEK_START, WEEK_END, "", _credited_readings())
    finally:
        conn.close()
    assert movement.points is None
    assert movement.withheld_cause == "boundary-records-unreadable"


def test_an_unusable_week_key_is_harmless_when_the_table_is_absent():
    """The non-vacuity twin. A store with no `weekly_credit_floors` table
    records no credits at all, so an unusable key withholds nothing: there
    was never a credit row to miss."""
    module = _forecast()
    conn = _credited_week_conn(drop_credit_floors=True)
    try:
        movement = module._realized_week_movement(
            conn, WEEK_START, WEEK_END, "", [
                ((WEEK_START + dt.timedelta(hours=24)).isoformat(), 12.0,
                 "userscript", "{}"),
                ((WEEK_START + dt.timedelta(hours=120)).isoformat(), 30.0,
                 "userscript", "{}"),
            ])
    finally:
        conn.close()
    assert movement.withheld_cause is None
    assert movement.points == pytest.approx(30.0)


def test_an_absent_credit_floor_table_is_still_the_permitted_relaxation():
    """A store with no `weekly_credit_floors` table records no credits at
    all, which is the one condition the original rationale justified. The
    week measures as a single unsegmented run rather than raising."""
    module = _forecast()
    conn = _credited_week_conn(drop_credit_floors=True)
    try:
        movement = module._realized_week_movement(
            conn, WEEK_START, WEEK_END, "2026-06-01", [
                ((WEEK_START + dt.timedelta(hours=24)).isoformat(), 12.0,
                 "userscript", "{}"),
                ((WEEK_START + dt.timedelta(hours=120)).isoformat(), 30.0,
                 "userscript", "{}"),
            ])
    finally:
        conn.close()
    assert movement.withheld_cause is None
    assert movement.segments == 1
    assert movement.points == pytest.approx(30.0)


def test_a_reset_created_segment_names_the_reset_not_a_credit():
    """The cause must name the boundary kind that actually created the
    segment. A reset-created segment with no synthetic baseline was reported
    as `credit-baseline-absent`, which names the wrong record."""
    module = _forecast()
    conn = _credited_week_conn(drop_credit_floors=True)
    try:
        conn.execute("INSERT INTO week_reset_events VALUES (?,?)",
                     (CREDIT_AT.isoformat(), "acct"))
        movement = module._realized_week_movement(
            conn, WEEK_START, WEEK_END, "2026-06-01", [
                ((WEEK_START + dt.timedelta(hours=24)).isoformat(), 46.0,
                 "userscript", "{}"),
                ((WEEK_START + dt.timedelta(hours=120)).isoformat(), 31.0,
                 "userscript", "{}"),
            ])
    finally:
        conn.close()
    assert movement.points is None
    assert movement.withheld_cause == "reset-baseline-absent"


def test_an_unreadable_reset_table_withholds_the_week_too():
    module = _forecast()
    conn = _credited_week_conn(drop_credit_floors=True)
    try:
        conn.execute("DROP TABLE week_reset_events")
        conn.execute("CREATE VIEW week_reset_events AS SELECT 1 AS nope")
        movement = module._realized_week_movement(
            conn, WEEK_START, WEEK_END, "2026-06-01", _credited_readings())
    finally:
        conn.close()
    assert movement.points is None
    assert movement.withheld_cause == "boundary-records-unreadable"


# --------------------------------------------------------------------------
# 3. The candidate scan's bound
# --------------------------------------------------------------------------
NOW = dt.datetime(2026, 7, 6, tzinfo=UTC)
CURRENT_WEEK_START = dt.datetime(2026, 6, 29, tzinfo=UTC)
CANDIDATE_STARTS = [CURRENT_WEEK_START - dt.timedelta(days=7 * n)
                    for n in range(1, 13)]


def _candidate_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE weekly_usage_snapshots ("
        " week_start_date TEXT, week_start_at TEXT, week_end_at TEXT,"
        " captured_at_utc TEXT, weekly_percent REAL, source TEXT,"
        " payload_json TEXT)")
    conn.execute("CREATE TABLE weekly_credit_floors (week_start_date TEXT,"
                 " effective_at_utc TEXT)")
    conn.execute("CREATE TABLE week_reset_events "
                 "(effective_reset_at_utc TEXT)")
    for start in CANDIDATE_STARTS:
        end = start + dt.timedelta(days=7)
        for hours, pct in ((24, 20.0), (120, 40.0)):
            conn.execute(
                "INSERT INTO weekly_usage_snapshots VALUES (?,?,?,?,?,?,?)",
                (start.date().isoformat(), start.isoformat(), end.isoformat(),
                 (start + dt.timedelta(hours=hours)).isoformat(), pct,
                 "userscript", "{}"))
    return conn


def _make_regime(qcg, *, effective_from=dt.datetime(2026, 1, 1, tzinfo=UTC)):
    return qcg.ValidatedRegime(
        account_key=None,
        effective_from=effective_from,
        effective_until=None,
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


def _pin_regime(ns, monkeypatch, counter, *, unsupported=()):
    qcg = ns["_load_sibling"]("_cctally_quota_calibration")
    forecast = ns["_cctally_forecast"]
    regime = _make_regime(qcg)
    monkeypatch.setattr(
        qcg, "read_calibration_file",
        lambda **_kwargs: qcg.CalibrationRead(regime, None))

    def _records(start, _end, **_kwargs):
        counter.append(start)
        return [start]

    monkeypatch.setattr(forecast, "_week_entry_records", _records)
    refused = set(unsupported)
    monkeypatch.setattr(
        qcg, "apply_regime",
        lambda _regime, entries: (
            qcg.ApplyRejection.UNSUPPORTED_COMPOSITION
            if entries and entries[0] in refused
            else qcg.AppliedQuota(10.0, 9.0, 11.0, 1.0)))


def _run_selector(ns, conn):
    original = ns["_sum_cost_for_range"]
    ns["_sum_cost_for_range"] = (
        lambda _ws, _we, mode="auto", skip_sync=False, **_k: 40.0)
    try:
        return ns["_select_dollars_per_percent"](
            conn, NOW, CURRENT_WEEK_START, 5.0, 50.0, skip_sync=True)
    finally:
        ns["_sum_cost_for_range"] = original


def test_the_comparability_probe_stops_once_four_candidates_are_eligible(
        monkeypatch):
    """Each probe opens `cache.db`, scans one subscription week of
    `session_entries` and builds kernel records from the rows — a median
    16,923 rows and 28 ms over the twelve most recent completed weeks of the
    maintainer's August 2026 store snapshot, measured on an Apple M4 Max Mac
    Studio. The median consumes exactly four candidates, so twelve probes buy
    nothing. This test pins the STRUCTURAL bound, which is the portable half:
    the duration is one machine's and the probe COUNT is not."""
    ns = load_script()
    conn = _candidate_conn()
    counted: list = []
    _pin_regime(ns, monkeypatch, counted)
    try:
        _dpp, source = _run_selector(ns, conn)
    finally:
        conn.close()
    assert source == "trailing_4wk_median"
    assert len(counted) == 4, (
        "the comparability probe ran once per candidate week rather than "
        f"stopping at the four the median needs: {len(counted)} probes")


def test_the_probe_still_examines_every_candidate_it_needs(monkeypatch):
    """The non-vacuity twin: the early stop must be "four ELIGIBLE", never
    "four examined". Six recent weeks fail the support test, so ten probes
    are genuinely required to reach four comparable candidates."""
    ns = load_script()
    conn = _candidate_conn()
    counted: list = []
    _pin_regime(ns, monkeypatch, counted,
                unsupported=CANDIDATE_STARTS[:6])
    try:
        _dpp, source = _run_selector(ns, conn)
    finally:
        conn.close()
    assert source == "trailing_4wk_median_drifted"
    assert len(counted) == 10, (
        "six incomparable weeks precede the four eligible ones, so the loop "
        f"must examine ten candidates; it examined {len(counted)}")


def test_a_locked_cache_is_not_reported_as_a_regime_exclusion(monkeypatch):
    """`_dpp_candidate_comparability` returned `"excluded"` on ANY exception,
    so a locked `cache.db` silently dropped every candidate and collapsed the
    branch to `this_week_sparse` with no signal. The causes are distinct."""
    ns = load_script()
    forecast = ns["_cctally_forecast"]
    qcg = ns["_load_sibling"]("_cctally_quota_calibration")

    def _boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(forecast, "_week_entry_records", _boom)
    verdict = forecast._dpp_candidate_comparability(
        _make_regime(qcg), CANDIDATE_STARTS[0],
        CANDIDATE_STARTS[0] + dt.timedelta(days=7), account_key=None)
    assert verdict == "unreadable", (
        "a store-read failure must be distinguishable from a week on the far "
        f"side of a rate boundary; got {verdict!r}")


def test_a_week_outside_the_regime_is_still_excluded():
    ns = load_script()
    forecast = ns["_cctally_forecast"]
    qcg = ns["_load_sibling"]("_cctally_quota_calibration")
    verdict = forecast._dpp_candidate_comparability(
        _make_regime(qcg, effective_from=dt.datetime(2026, 7, 1, tzinfo=UTC)),
        CANDIDATE_STARTS[0], CANDIDATE_STARTS[0] + dt.timedelta(days=7),
        account_key=None)
    assert verdict == "excluded"


def test_an_unreadable_population_makes_the_restriction_inert_and_says_so(
        monkeypatch):
    """A locked `cache.db` fails for EVERY candidate, so dropping them all
    excludes every candidate rather than the incomparable ones — the exact
    failure `_dpp_candidate_regime`'s own docstring refuses. The restriction
    therefore becomes inert, and the rate says it was not verified."""
    ns = load_script()
    conn = _candidate_conn()
    forecast = ns["_cctally_forecast"]
    qcg = ns["_load_sibling"]("_cctally_quota_calibration")
    regime = _make_regime(qcg)
    monkeypatch.setattr(
        qcg, "read_calibration_file",
        lambda **_kwargs: qcg.CalibrationRead(regime, None))

    def _boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(forecast, "_week_entry_records", _boom)
    try:
        _dpp, source = _run_selector(ns, conn)
    finally:
        conn.close()
    assert source == "trailing_4wk_median_unverified", (
        "the branch collapsed to a fallback with no signal that the "
        f"comparability test never ran; got {source!r}")


def test_an_unreadable_probe_does_not_admit_a_pre_boundary_week(monkeypatch):
    """The rate-boundary test needs no store read, so it must keep running.

    `_dpp_candidate_comparability` returns `"excluded"` from a pure date
    comparison against the regime's own interval, BEFORE it touches
    `cache.db`. Skipping the whole probe once one candidate came back
    `"unreadable"` therefore admitted every later week unconditionally,
    including weeks on the far side of a metering-rate boundary — which is
    exactly what spec section 4.2 says the sparse fallback exists to avoid.

    Here the regime opens at the third-newest candidate, so three candidates
    are in regime and nine predate the boundary. Every probe is unreadable.
    Three eligible candidates is fewer than the four the median needs, so the
    selector must fall back rather than publish a median that reaches back
    across the boundary.
    """
    ns = load_script()
    conn = _candidate_conn()
    forecast = ns["_cctally_forecast"]
    qcg = ns["_load_sibling"]("_cctally_quota_calibration")
    regime = _make_regime(qcg, effective_from=CANDIDATE_STARTS[2])
    monkeypatch.setattr(
        qcg, "read_calibration_file",
        lambda **_kwargs: qcg.CalibrationRead(regime, None))

    def _boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(forecast, "_week_entry_records", _boom)
    try:
        _dpp, source = _run_selector(ns, conn)
    finally:
        conn.close()
    assert source == "this_week_sparse", (
        "one unreadable probe made the regime restriction inert for the "
        "rate-BOUNDARY test as well as for the population test, so nine "
        f"pre-boundary weeks entered the median; got {source!r}")


def test_the_boundary_test_costs_no_store_read_for_a_pre_boundary_week(
        monkeypatch):
    """The non-vacuity twin of the fix's cost argument.

    Honouring `"excluded"` after a first unreadable probe must not buy that
    correctness with a store read per remaining candidate. The boundary
    verdict is a date comparison against the regime's own interval, so a
    pre-boundary candidate reaches no population read at all; and the
    population probe itself stays inert once `unverified` is set, so the two
    in-regime candidates behind the failing one are not re-read either.
    """
    ns = load_script()
    conn = _candidate_conn()
    forecast = ns["_cctally_forecast"]
    qcg = ns["_load_sibling"]("_cctally_quota_calibration")
    regime = _make_regime(qcg, effective_from=CANDIDATE_STARTS[2])
    monkeypatch.setattr(
        qcg, "read_calibration_file",
        lambda **_kwargs: qcg.CalibrationRead(regime, None))
    probed: list = []

    def _boom(start, *_a, **_k):
        probed.append(start)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(forecast, "_week_entry_records", _boom)
    try:
        _run_selector(ns, conn)
    finally:
        conn.close()
    assert probed == [CANDIDATE_STARTS[0]], (
        "the nine pre-boundary weeks must be excluded on dates alone, and "
        "the population probe must stay inert once one read has failed; "
        f"probed {[str(p.date()) for p in probed]}")
