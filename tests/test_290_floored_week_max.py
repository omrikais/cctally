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
    # #703 + #707: the two boundary columns are part of production's shape, and
    # the boundary reducer reads them to tell a same-window CREDIT (both NULL)
    # from a RESET now that both kinds live in this one table. `week_start_date`
    # and `observed_at_utc` came with the same change: the floor selects a row
    # by the week it names and reads the EXACT observation instant rather than
    # the hour-floored display one.
    conn.execute(
        "CREATE TABLE week_reset_events (effective_reset_at_utc TEXT, "
        "old_week_end_at TEXT, new_week_end_at TEXT, week_start_date TEXT, "
        "observed_at_utc TEXT)"
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
        "INSERT INTO week_reset_events (effective_reset_at_utc) VALUES (?)",
        ("2026-06-04T00:00:00Z",)
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
    only reads weekly_usage_snapshots + the two floor tables.

    `source` and `payload_json` are present because #661 S2's movement reducer
    reads them to tell a synthetic post-credit baseline from an ordinary
    reading. Omitting them would ALSO withhold the credited week below, but
    for the wrong reason — a store that cannot express the distinction rather
    than a week that genuinely lacks the baseline.
    """
    conn = _conn_with_floor_tables()
    conn.execute(
        "CREATE TABLE weekly_usage_snapshots ("
        " week_start_date TEXT, week_start_at TEXT, week_end_at TEXT,"
        " captured_at_utc TEXT, weekly_percent REAL,"
        " source TEXT DEFAULT 'userscript', payload_json TEXT DEFAULT '{}')"
    )
    return conn


def _seed_week(conn, wsd, ws, we, caps):
    for cap, pct in caps:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots"
            " (week_start_date, week_start_at, week_end_at, captured_at_utc,"
            "  weekly_percent) VALUES (?,?,?,?,?)",
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


# Per-week costs chosen so week A's presence or absence MOVES the median.
# Weeks E/B/C/D each move 40 points, so at costs 160/40/120/200 they price at
# $4.00/$1.00/$3.00/$5.00 per point. Week A is credited and carries NO
# synthetic post-credit baseline, so #661 S2 §4.1 withholds it: the four
# survivors are E/B/C/D and the median is $3.50. Were week A admitted at its
# pre-credit peak of 46 for $62 it would price at $1.348, displacing E from
# the four most-recent weeks and giving $2.174 instead.
_DISCRIMINATING_COST_BY_DATE = {
    dt.date(2026, 5, 25): 160.0,
    dt.date(2026, 6, 1): 62.0,
    dt.date(2026, 6, 8): 40.0,
    dt.date(2026, 6, 15): 120.0,
    dt.date(2026, 6, 22): 200.0,
}

#: Which weeks the run actually priced. A withheld week is never costed, so
#: this is the direct observable of the withholding rather than an inference
#: from the median alone.
_COSTED_WEEKS: list = []


def _discriminating_cost_fn(ws, we, mode="auto", skip_sync=False, **kwargs):
    # **kwargs absorbs the #341 account_key= that _select_dollars_per_percent
    # now threads into _sum_cost_for_range (None on the merged forecast path).
    _COSTED_WEEKS.append(ws.date())
    return _DISCRIMINATING_COST_BY_DATE[ws.date()]


def _seed_plain_ebcd(conn):
    """Weeks E/B/C/D: one 20% and one 40% snapshot each, so each moves 40."""
    for start, end in (("2026-05-25", "2026-06-01"), ("2026-06-08", "2026-06-15"),
                       ("2026-06-15", "2026-06-22"), ("2026-06-22", "2026-06-29")):
        _seed_week(conn, start, f"{start}T00:00:00Z", f"{end}T00:00:00Z",
                   [(f"{start}T06:00:00Z", 20.0), (f"{start}T18:00:00Z", 40.0)])


def test_forecast_median_uses_floored_denominator_for_credited_week():
    """A credited prior week with no post-credit baseline is WITHHELD.

    #661 S2 reverses this expectation. The denominator was
    `_floored_week_max`'s post-credit reading of 31, which is a fragment of
    the week's cost-bearing consumption; spec §4.1 replaces it with realized
    meter movement, segmented at the recorded credit boundary. This fixture
    carries the pre-credit 46 and the post-credit 31 and no synthetic
    post-credit snapshot, so the week's movement is unknowable — 46 assumes
    the drop consumed nothing, 77 assumes the credit zeroed the meter — and
    the week leaves the median entirely.

    `_floored_week_max` itself is unchanged and its own tests above still
    pin it; the change is in the forecast consumer.
    """
    ns = load_script()
    conn = _forecast_conn()
    _COSTED_WEEKS.clear()
    now = dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc)
    current_ws = dt.datetime(2026, 6, 29, tzinfo=dt.timezone.utc)
    # Week A (credited): 46 @ 06-03, then 31 @ 06-05; credit effective 06-04.
    _seed_week(conn, "2026-06-01", "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
               [("2026-06-03T00:00:00Z", 46.0), ("2026-06-05T00:00:00Z", 31.0)])
    conn.execute("INSERT INTO weekly_credit_floors VALUES (?, ?)",
                 ("2026-06-01", "2026-06-04T00:00:00Z"))
    _seed_plain_ebcd(conn)
    dpp, source = _run_forecast_dpp(
        ns, conn, now, current_ws, _discriminating_cost_fn
    )
    assert source == "trailing_4wk_median"
    assert dt.date(2026, 6, 1) not in _COSTED_WEEKS, (
        "the withheld week was priced")
    assert sorted(_COSTED_WEEKS) == [dt.date(2026, 5, 25), dt.date(2026, 6, 8),
                                     dt.date(2026, 6, 15), dt.date(2026, 6, 22)]
    assert dpp == 3.5, (
        "REVERSED by #661 S2 §4.1: this test's NAME still says the floored "
        "denominator, and the assertion now says the opposite. The credited "
        "week is WITHHELD for want of a post-credit baseline and 2.5 was the "
        "floored-denominator expectation this session replaced")
    conn.close()


def test_forecast_median_credited_week_mixed_offset_spellings():
    """Mixed Z / +00:00 spellings of the credited week's start/end must coalesce
    into ONE week (parsed-instant keyed), which #661 S2 makes MORE observable
    rather than less: coalesced, week A holds both readings and is withheld
    for want of a post-credit baseline, so the median is E/B/C/D's $3.50. Two
    uncoalesced spellings would each hold one reading — the first entirely
    before the credit boundary, so it would price at $62/46 = $1.348 and
    displace week E, giving $2.174.

    The subject is unchanged (Codex P2 / spec §3.2 first-wins end coalescing);
    only the expected rate moves, and `_floored_week_max` is untouched.
    """
    ns = load_script()
    conn = _forecast_conn()
    _COSTED_WEEKS.clear()
    now = dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc)
    current_ws = dt.datetime(2026, 6, 29, tzinfo=dt.timezone.utc)
    # Same instant spelled two ways across week A's two snapshots.
    _seed_week(conn, "2026-06-01", "2026-06-01T00:00:00Z", "2026-06-08T00:00:00Z",
               [("2026-06-03T00:00:00Z", 46.0)])
    _seed_week(conn, "2026-06-01", "2026-06-01T00:00:00+00:00",
               "2026-06-08T00:00:00+00:00", [("2026-06-05T00:00:00Z", 31.0)])
    conn.execute("INSERT INTO weekly_credit_floors VALUES (?, ?)",
                 ("2026-06-01", "2026-06-04T00:00:00Z"))
    _seed_plain_ebcd(conn)
    dpp, source = _run_forecast_dpp(
        ns, conn, now, current_ws, _discriminating_cost_fn
    )
    assert source == "trailing_4wk_median"
    assert dt.date(2026, 6, 1) not in _COSTED_WEEKS
    assert dpp == 3.5, (
        "REVERSED by #661 S2 §4.1: the coalescing subject is unchanged, but "
        "the coalesced credited week is now WITHHELD rather than priced off "
        "the floored denominator; 2.5 was the old expectation")
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
