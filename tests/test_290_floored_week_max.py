"""#290: unit tests for the reset-aware floored-per-week-max reducer."""
import datetime as dt
import sqlite3

import pytest

from conftest import load_script, redirect_paths


_OPEN_CONNS: list = []


@pytest.fixture(autouse=True)
def _close_conns():
    """Close any in-memory conns opened via _conn_with_floor_tables so the suite
    stays ResourceWarning-clean (runs on pass and on failure)."""
    yield
    while _OPEN_CONNS:
        try:
            _OPEN_CONNS.pop().close()
        except Exception:
            pass


def _conn_with_floor_tables():
    """In-memory conn carrying just the two tables _reset_aware_floor reads."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE week_reset_events (effective_reset_at_utc TEXT)"
    )
    conn.execute(
        "CREATE TABLE weekly_credit_floors "
        "(week_start_date TEXT, effective_at_utc TEXT)"
    )
    _OPEN_CONNS.append(conn)
    return conn


def _helper():
    return load_script()["_floored_week_max"]


def test_uncredited_week_is_raw_max():
    conn = _conn_with_floor_tables()  # no floor rows
    rows = [
        ("k", "2026-06-01", "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
         "2026-06-02T00:00:00Z", 20.0),
        ("k", "2026-06-01", "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
         "2026-06-03T00:00:00Z", 46.0),
    ]
    assert _helper()(conn, rows) == {"k": 46.0}


def test_credit_floor_drops_pre_floor_captures():
    conn = _conn_with_floor_tables()
    conn.execute(
        "INSERT INTO weekly_credit_floors VALUES (?, ?)",
        ("2026-06-01", "2026-06-04T00:00:00Z"),
    )
    rows = [
        # pre-floor stale peak 46 -> dropped
        ("k", "2026-06-01", "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
         "2026-06-03T00:00:00Z", 46.0),
        # post-floor 31 -> kept
        ("k", "2026-06-01", "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
         "2026-06-05T00:00:00Z", 31.0),
    ]
    assert _helper()(conn, rows) == {"k": 31.0}


def test_mixed_null_bounds_do_not_suppress_reset_leg():
    """Two-pass canonicalization: a NULL-bound row first, then an anchored row
    for the SAME week that carries a reset event, must still floor."""
    conn = _conn_with_floor_tables()
    conn.execute(
        "INSERT INTO week_reset_events VALUES (?)", ("2026-06-04T00:00:00Z",)
    )
    rows = [
        # NULL bounds first (legacy) -> must not cache a reset-inert floor
        ("k", "2026-06-01", None, None,
         "2026-06-03T00:00:00Z", 46.0),                 # pre-floor
        # anchored row supplies canonical bounds for the reset leg
        ("k", "2026-06-01", "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
         "2026-06-05T00:00:00Z", 31.0),                 # post-floor
    ]
    assert _helper()(conn, rows) == {"k": 31.0}


def test_all_rows_pre_floor_week_absent():
    conn = _conn_with_floor_tables()
    conn.execute(
        "INSERT INTO weekly_credit_floors VALUES (?, ?)",
        ("2026-06-01", "2026-06-09T00:00:00Z"),  # floor after every capture
    )
    rows = [
        ("k", "2026-06-01", "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
         "2026-06-03T00:00:00Z", 46.0),
    ]
    assert _helper()(conn, rows) == {}


def test_malformed_captured_at_is_retained_under_active_floor():
    conn = _conn_with_floor_tables()
    conn.execute(
        "INSERT INTO weekly_credit_floors VALUES (?, ?)",
        ("2026-06-01", "2026-06-04T00:00:00Z"),
    )
    rows = [
        ("k", "2026-06-01", "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
         "not-a-timestamp", 31.0),  # unparseable cap -> retained
    ]
    assert _helper()(conn, rows) == {"k": 31.0}


def test_null_pct_skipped():
    conn = _conn_with_floor_tables()
    rows = [
        ("k", "2026-06-01", "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
         "2026-06-02T00:00:00Z", None),
    ]
    assert _helper()(conn, rows) == {}


# ── Task 3: forecast $/1% median flooring ─────────────────────────────


def _forecast_conn():
    """Minimal stats conn: _select_dollars_per_percent takes `conn` directly and
    only reads weekly_usage_snapshots + the two floor tables."""
    conn = _conn_with_floor_tables()
    conn.execute(
        "CREATE TABLE weekly_usage_snapshots ("
        " week_start_date TEXT, week_start_at TEXT, week_end_at TEXT,"
        " captured_at_utc TEXT, weekly_percent REAL)"
    )
    return conn


def _seed_week(conn, wsd, ws, we, caps):
    for cap, pct in caps:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots VALUES (?,?,?,?,?)",
            (wsd, ws, we, cap, pct),
        )


def _run_forecast_dpp(ns, conn, now, current_ws, cost_fn):
    """Call _select_dollars_per_percent with `_sum_cost_for_range` monkeypatched
    so the test needs no cache.db. ``ns`` IS the ``cctally`` module dict, and the
    forecast helper resolves the cost fn via ``c._sum_cost_for_range`` (i.e.
    ``sys.modules['cctally']._sum_cost_for_range``), so a setitem on ``ns`` is a
    setattr on that module. Returns (dpp, source)."""
    orig = ns["_sum_cost_for_range"]
    ns["_sum_cost_for_range"] = cost_fn
    try:
        return ns["_select_dollars_per_percent"](
            conn, now, current_ws, p_now=0.0, spent_usd=0.0, skip_sync=True
        )
    finally:
        ns["_sum_cost_for_range"] = orig


# Per-week costs chosen so the credited week's floored-vs-raw denominator MOVES
# the median (the plan's flat 40%/constant-cost example does not discriminate,
# because week A stays an extreme and the median of 4 averages the two middle
# values). With costs 62/40/120/200 over pcts 31(floored)/40/40/40 the floored
# $/1% values are [2.0, 1.0, 3.0, 5.0] -> median 2.5; the raw peak 46 makes
# week A 62/46 = 1.348 -> median 2.174. So dpp == 2.5 is reachable ONLY when the
# credited week is floored to 31.
_DISCRIMINATING_COST_BY_DATE = {
    dt.date(2026, 6, 1): 62.0,
    dt.date(2026, 6, 8): 40.0,
    dt.date(2026, 6, 15): 120.0,
    dt.date(2026, 6, 22): 200.0,
}


def _discriminating_cost_fn(ws, we, mode="auto", skip_sync=False):
    return _DISCRIMINATING_COST_BY_DATE[ws.date()]


def _seed_plain_bcd(conn):
    """Weeks B/C/D: one plain 40% snapshot each (shared by both forecast tests)."""
    _seed_week(conn, "2026-06-08", "2026-06-08T00:00:00Z", "2026-06-15T00:00:00Z",
               [("2026-06-09T00:00:00Z", 40.0)])
    _seed_week(conn, "2026-06-15", "2026-06-15T00:00:00Z", "2026-06-22T00:00:00Z",
               [("2026-06-16T00:00:00Z", 40.0)])
    _seed_week(conn, "2026-06-22", "2026-06-22T00:00:00Z", "2026-06-29T00:00:00Z",
               [("2026-06-23T00:00:00Z", 40.0)])


def test_forecast_median_uses_floored_denominator_for_credited_week():
    """A credited prior week's $/1% denominator must be its FLOORED peak (31),
    not the stale pre-credit peak (46) — asserted through the end median."""
    ns = load_script()
    conn = _forecast_conn()
    now = dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc)
    current_ws = dt.datetime(2026, 6, 29, tzinfo=dt.timezone.utc)
    # Week A (credited): pre-floor 46 @ 06-03 dropped, post-floor 31 @ 06-05
    # kept; credit floor effective at 06-04.
    _seed_week(conn, "2026-06-01", "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
               [("2026-06-03T00:00:00Z", 46.0), ("2026-06-05T00:00:00Z", 31.0)])
    conn.execute("INSERT INTO weekly_credit_floors VALUES (?, ?)",
                 ("2026-06-01", "2026-06-04T00:00:00Z"))
    _seed_plain_bcd(conn)
    dpp, source = _run_forecast_dpp(
        ns, conn, now, current_ws, _discriminating_cost_fn
    )
    assert source == "trailing_4wk_median"
    assert dpp == 2.5
    conn.close()


def test_forecast_median_credited_week_mixed_offset_spellings():
    """Mixed Z / +00:00 spellings of the credited week's start/end must coalesce
    into ONE week (parsed-instant keyed), so the floored median is unchanged
    (Codex P2 / spec §3.2 first-wins end coalescing)."""
    ns = load_script()
    conn = _forecast_conn()
    now = dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc)
    current_ws = dt.datetime(2026, 6, 29, tzinfo=dt.timezone.utc)
    # Same instant spelled two ways across week A's two snapshots.
    _seed_week(conn, "2026-06-01", "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
               [("2026-06-03T00:00:00Z", 46.0)])
    _seed_week(conn, "2026-06-01", "2026-06-01T00:00:00+00:00",
               "2026-06-08T00:00:00+00:00", [("2026-06-05T00:00:00Z", 31.0)])
    conn.execute("INSERT INTO weekly_credit_floors VALUES (?, ?)",
                 ("2026-06-01", "2026-06-04T00:00:00Z"))
    _seed_plain_bcd(conn)
    dpp, source = _run_forecast_dpp(
        ns, conn, now, current_ws, _discriminating_cost_fn
    )
    assert source == "trailing_4wk_median"
    assert dpp == 2.5
    conn.close()


# ── Task 4: diff multi-week average flooring ──────────────────────────


def _diff_seed_snapshot(conn, cap, wsd, wed, ws_at, we_at, pct):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (cap, wsd, wed, ws_at, we_at, pct, "test", "{}"),
    )


def test_diff_avg_floors_credited_week(tmp_path, monkeypatch):
    """The diff multi-week average branch must contribute a credited week's
    FLOORED peak (31), not the stale pre-credit peak (46). `_diff_resolve_used_pct`
    opens its own DB via open_db(), so seed the isolated stats.db (conftest
    APP_DIR isolation + open_db()) with two full weeks; week A is credited via a
    week_reset_events row effective mid-week."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    open_db = ns["open_db"]
    ParsedWindow = ns["ParsedWindow"]
    _diff_resolve_used_pct = ns["_diff_resolve_used_pct"]

    conn = open_db()
    try:
        # Week A: 2026-06-01..08, credited (reset effective 06-04).
        #   pre-floor 46 @ 06-03 (dropped), post-floor 31 @ 06-05 (kept).
        _diff_seed_snapshot(conn, "2026-06-03T00:00:00Z", "2026-06-01",
                            "2026-06-08", "2026-06-01T00:00:00Z",
                            "2026-06-08T00:00:00Z", 46.0)
        _diff_seed_snapshot(conn, "2026-06-05T00:00:00Z", "2026-06-01",
                            "2026-06-08", "2026-06-01T00:00:00Z",
                            "2026-06-08T00:00:00Z", 31.0)
        # Week B: 2026-06-08..15, plain peak 20.
        _diff_seed_snapshot(conn, "2026-06-09T00:00:00Z", "2026-06-08",
                            "2026-06-15", "2026-06-08T00:00:00Z",
                            "2026-06-15T00:00:00Z", 20.0)
        # Reset event marking week A's mid-week credit boundary (in-window).
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?,?,?,?)",
            ("2026-06-04T12:00:00Z", "2026-06-08T00:00:00Z",
             "2026-06-08T00:00:00Z", "2026-06-04T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    win = ParsedWindow(
        label="last-2w",
        start_utc=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc),
        end_utc=dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc),
        length_days=14.0, kind="explicit-range",
        week_aligned=False, full_weeks_count=2,
    )
    val, mode = _diff_resolve_used_pct(win)
    assert mode == "avg"
    # floored: (31 + 20) / 2 = 25.5 ; raw-peak bug would give (46+20)/2 = 33.0
    assert val == 25.5
