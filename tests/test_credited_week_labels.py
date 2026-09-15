"""Per-segment labels on a credited week (#750 S4 spec §3.2, §3.2a, §3.3).

`SubWeek` carries TWO date fields. `start_date` is the shared join key into
`weekly_usage_snapshots.week_start_date` and epic invariant 3 keeps it
identical across a credited week's segments. `display_start_date` is the
user-facing start date, which `_apply_reset_events_to_subweeks` moves per
segment (`bin/_lib_subscription_weeks.py`, and
`tests/test_subweek_display_dates.py` asserts it gives an in-place credit's
two segments `05-09` and `05-15`).

A CORRECTION to the spec is recorded here, because the tree does not match
what §3.2 says about CLI `weekly`. See
`test_cli_weekly_surfaces_already_label_each_segment_from_its_display_date`.
"""
import datetime as dt
import sqlite3

from conftest import load_script

WEEK_END = "2026-05-16T15:00:00+00:00"
CREDIT_CUT = "2026-05-15T17:00:00+00:00"


def _conn(rows, *, cuts=()):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE weekly_usage_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at_utc TEXT NOT NULL,
            week_start_date TEXT NOT NULL,
            week_end_date TEXT,
            week_start_at TEXT,
            week_end_at TEXT,
            weekly_percent REAL,
            -- #769 S11 (#824): `get_latest_usage_for_week` excludes held rows,
            -- so this hand-built table has to carry the column the real
            -- schema declares or every read here raises.
            weekly_observation_held INTEGER NOT NULL DEFAULT 0
                CHECK (weekly_observation_held IN (0, 1))
        )
    """)
    conn.execute("""
        CREATE TABLE week_reset_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at_utc TEXT NOT NULL,
            old_week_end_at TEXT NOT NULL,
            new_week_end_at TEXT NOT NULL,
            effective_reset_at_utc TEXT NOT NULL,
            observed_pre_credit_pct REAL,
            account_key TEXT
        )
    """)
    for captured, wsd, pct in rows:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, weekly_percent) VALUES (?,?,?)",
            (captured, wsd, pct),
        )
    for cut in cuts:
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct, account_key) "
            "VALUES (?,?,?,?,?,?)",
            (cut, cut, WEEK_END, cut, 67.0, "unattributed"),
        )
    conn.commit()
    return conn


def _segments(ns):
    """The two segments of one credited week, as the applier emits them:
    one `start_date`, two `display_start_date`s."""
    SubWeek = ns["SubWeek"]
    common = dict(start_date=dt.date(2026, 5, 9), end_date=dt.date(2026, 5, 15),
                  source="snapshot")
    pre = SubWeek(start_ts="2026-05-09T15:00:00+00:00", end_ts=CREDIT_CUT,
                  display_start_date=dt.date(2026, 5, 9), **common)
    post = SubWeek(start_ts=CREDIT_CUT, end_ts=WEEK_END,
                   display_start_date=dt.date(2026, 5, 15), **common)
    return pre, post


def _entry(ns, *, ts, cost):
    return ns["UsageEntry"](
        timestamp=dt.datetime.fromisoformat(ts),
        model="claude-opus-4-5-20251101",
        usage={"input_tokens": 100, "output_tokens": 50,
               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        cost_usd=cost, source_path="/fake/sess.jsonl",
    )


def _view(ns, weeks, conn, entries=None):
    if entries is None:
        entries = [_entry(ns, ts="2026-05-13T12:00:00+00:00", cost=40.0),
                   _entry(ns, ts="2026-05-15T18:30:00+00:00", cost=4.0)]
    return ns["build_weekly_view"](
        conn, entries,
        weeks=weeks,
        now_utc=dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.timezone.utc),
        as_of_utc="2026-05-16T12:00:00Z", mode="display",
    )


def test_weekly_view_labels_each_segment_from_its_own_display_date():
    """`WeeklyPeriodRow.label` is built from `start_date`, the shared join
    key, so both segments of a credited week carry one label.

    Every renderer in the tree that shows a week already reads
    `display_start_date` — `bin/_lib_render.py` for the terminal table,
    `bin/_cctally_share.py` for the weekly share artifact, and
    `bin/_cctally_dashboard.py` which OVERWRITES this very field with it
    before the envelope serializes it. Building the field correctly removes
    that overwrite's reason to exist and stops the raw view from carrying a
    label no consumer wants.
    """
    ns = load_script()
    pre, post = _segments(ns)
    conn = _conn([("2026-05-15T16:00:00Z", "2026-05-09", 67.0),
                  ("2026-05-15T19:00:00Z", "2026-05-09", 4.0)],
                 cuts=[CREDIT_CUT])
    view = _view(ns, [pre, post], conn)
    # rows are newest-first
    assert [r.label for r in view.rows] == ["05-15", "05-09"]
    conn.close()


def test_an_uncredited_weeks_label_is_unchanged():
    """`display_start_date == start_date` for a week no reset touched, so
    every existing label byte stays where it was."""
    ns = load_script()
    week = ns["SubWeek"](
        start_ts="2026-05-02T15:00:00+00:00",
        end_ts="2026-05-09T15:00:00+00:00",
        start_date=dt.date(2026, 5, 2), end_date=dt.date(2026, 5, 8),
        source="snapshot", display_start_date=dt.date(2026, 5, 2),
    )
    conn = _conn([("2026-05-09T14:00:00Z", "2026-05-02", 80.0)])
    view = _view(ns, [week], conn,
                 [_entry(ns, ts="2026-05-05T12:00:00+00:00", cost=7.0)])
    assert [r.label for r in view.rows] == ["05-02"]
    conn.close()


def test_cli_weekly_surfaces_already_label_each_segment_from_its_display_date():
    """CORRECTION to spec §3.2, recorded rather than silently diverged from.

    §3.2 states that CLI `weekly` labels from the shared `start_date` and so
    renders two identical rows on every credited week. That is false at this
    tree. `cmd_weekly` consumes `view.aggregated` / `view.overlay` and never
    `view.rows`, and both of its renderers already read `display_start_date`:
    the terminal table via `_render_weekly_table`, and `--json` via
    `_weekly_to_json`'s `displayWeek`. The defect §3.2 describes exists only
    on `WeeklyPeriodRow.label`, whose one consumer — the dashboard envelope —
    already receives an overwritten value.
    """
    ns = load_script()
    pre, post = _segments(ns)
    payload = ns["_weekly_to_json"](
        buckets=[
            ns["BucketUsage"](
                bucket=sw.segment_key, input_tokens=0, output_tokens=0,
                cache_creation_tokens=0, cache_read_tokens=0, total_tokens=0,
                cost_usd=cost, models=["m"], model_breakdowns=[],
            )
            for sw, cost in ((pre, 40.0), (post, 4.0))
        ],
        week_pct_overlay=[(67.0, 0.6), (4.0, 1.0)],
        weeks=[pre, post],
    )
    import json
    rows = json.loads(payload)["weekly"]
    assert [r["week"] for r in rows] == ["2026-05-09", "2026-05-09"], (
        "`week` is the shared billing-cycle join key and must not move"
    )
    assert [r["displayWeek"] for r in rows] == ["2026-05-09", "2026-05-15"], (
        "the user-facing date already distinguishes the two cycles"
    )


# --- §3.2a: the collision suffix -------------------------------------------


def _suffixes(ns, items, display_tz=None):
    return ns["apply_label_collision_suffixes"](items, display_tz=display_tz)


def test_an_ordinary_credited_week_gains_no_suffix():
    """Segments on different dates already read differently, so D5 leaves
    them alone. This is the common credited week, and its two panels stay
    byte-identical."""
    ns = load_script()
    items = [
        ("2026-05-09", "05-09",
         dt.datetime(2026, 5, 9, 15, tzinfo=dt.timezone.utc)),
        ("2026-05-09", "05-15",
         dt.datetime(2026, 5, 15, 17, tzinfo=dt.timezone.utc)),
    ]
    assert _suffixes(ns, items) == ["05-09", "05-15"]


def test_a_same_day_credited_week_suffixes_every_colliding_sibling():
    """Start and both cuts on one calendar day -> three rows, three
    suffixes. Written over cuts-plus-one segments rather than a literal
    pair."""
    ns = load_script()
    items = [
        ("2026-05-15", "05-15",
         dt.datetime(2026, 5, 15, 3, tzinfo=dt.timezone.utc)),
        ("2026-05-15", "05-15",
         dt.datetime(2026, 5, 15, 9, 30, tzinfo=dt.timezone.utc)),
        ("2026-05-15", "05-15",
         dt.datetime(2026, 5, 15, 18, tzinfo=dt.timezone.utc)),
    ]
    assert _suffixes(ns, items, display_tz="UTC") == [
        "05-15 03:00", "05-15 09:30", "05-15 18:00",
    ]


def test_two_cycles_one_year_apart_do_not_collide():
    """Both formats are year-free (`%m-%d` Weekly, `%b %d` Trend). Grouping
    across the whole rendered list would suffix two unrelated cycles.
    Grouping is confined to siblings of ONE canonical week."""
    ns = load_script()
    items = [
        ("2025-05-09", "May 09",
         dt.datetime(2025, 5, 9, 15, tzinfo=dt.timezone.utc)),
        ("2026-05-09", "May 09",
         dt.datetime(2026, 5, 9, 15, tzinfo=dt.timezone.utc)),
    ]
    assert _suffixes(ns, items, display_tz="UTC") == ["May 09", "May 09"]


def test_an_uncredited_week_is_byte_identical():
    ns = load_script()
    items = [
        ("2026-05-02", "05-02",
         dt.datetime(2026, 5, 2, 15, tzinfo=dt.timezone.utc)),
        ("2026-05-09", "05-09",
         dt.datetime(2026, 5, 9, 15, tzinfo=dt.timezone.utc)),
    ]
    assert _suffixes(ns, items) == ["05-02", "05-09"]


def test_the_suffix_is_rendered_in_the_display_timezone():
    """Rendered through `format_display_dt`, the display-timezone
    chokepoint, not through a bare `astimezone()`."""
    ns = load_script()
    items = [
        ("2026-05-15", "05-15",
         dt.datetime(2026, 5, 15, 3, tzinfo=dt.timezone.utc)),
        ("2026-05-15", "05-15",
         dt.datetime(2026, 5, 15, 9, tzinfo=dt.timezone.utc)),
    ]
    assert _suffixes(ns, items, display_tz="Asia/Jerusalem") == [
        "05-15 06:00", "05-15 12:00",
    ]


def test_the_weekly_view_suffixes_a_same_day_credited_week():
    """End to end through `build_weekly_view`: three segments whose display
    dates land on one calendar day come back distinctly labelled."""
    ns = load_script()
    SubWeek = ns["SubWeek"]
    common = dict(start_date=dt.date(2026, 5, 9), end_date=dt.date(2026, 5, 15),
                  source="snapshot")
    segs = [
        SubWeek(start_ts="2026-05-15T02:00:00+00:00",
                end_ts="2026-05-15T08:00:00+00:00",
                display_start_date=dt.date(2026, 5, 15), **common),
        SubWeek(start_ts="2026-05-15T08:00:00+00:00",
                end_ts="2026-05-15T14:00:00+00:00",
                display_start_date=dt.date(2026, 5, 15), **common),
        SubWeek(start_ts="2026-05-15T14:00:00+00:00", end_ts=WEEK_END,
                display_start_date=dt.date(2026, 5, 15), **common),
    ]
    conn = _conn([("2026-05-15T07:00:00Z", "2026-05-09", 30.0),
                  ("2026-05-15T13:00:00Z", "2026-05-09", 20.0),
                  ("2026-05-15T20:00:00Z", "2026-05-09", 10.0)],
                 cuts=["2026-05-15T08:00:00+00:00",
                       "2026-05-15T14:00:00+00:00"])
    view = ns["build_weekly_view"](
        conn,
        [_entry(ns, ts="2026-05-15T03:00:00+00:00", cost=3.0),
         _entry(ns, ts="2026-05-15T09:00:00+00:00", cost=2.0),
         _entry(ns, ts="2026-05-15T15:00:00+00:00", cost=1.0)],
        weeks=segs,
        now_utc=dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.timezone.utc),
        as_of_utc="2026-05-16T12:00:00Z", mode="display",
        display_tz=None,
    )
    labels = [r.label for r in reversed(view.rows)]
    assert len(set(labels)) == len(labels), labels
    assert all(label.startswith("05-15 ") for label in labels), labels
    conn.close()


# --- §3.3: report share needs its own collision path ------------------------


def _trend_row(ns, *, label, start_at, week_start_date, dpp):
    return ns["TuiTrendRow"](
        week_label=label, week_start_at=start_at, used_pct=50.0,
        dollars_per_percent=dpp, delta_dpp=None, spark_height=4,
        is_current=False, week_start_date=week_start_date,
        weekly_cost_usd=10.0,
    )


def _report_labels(ns, rows):
    view = ns["TrendView"](
        rows=tuple(rows), avg_dollars_per_pct=1.0,
        period_start=dt.datetime(2026, 5, 2, tzinfo=dt.timezone.utc),
        period_end=dt.datetime(2026, 5, 17, tzinfo=dt.timezone.utc),
        display_tz_label="UTC",
    )
    snap = ns["_build_report_snapshot"](
        view,
        period_start=dt.datetime(2026, 5, 2, tzinfo=dt.timezone.utc),
        period_end=dt.datetime(2026, 5, 17, tzinfo=dt.timezone.utc),
        display_tz="UTC", version="9.9.9",
    )
    return [r.cells["week"].text for r in snap.rows]


def test_report_share_disambiguates_a_credited_weeks_segments():
    """`_build_report_snapshot` ignores `week_label` and rebuilds an ISO date
    from `week_start_date`, which both segments share, so share output
    duplicates them. Switching the site to `r.week_label` would move every
    existing ISO date byte in every artifact, so share keeps the ISO base and
    gains its own collision path over it (§3.3)."""
    ns = load_script()
    labels = _report_labels(ns, [
        _trend_row(ns, label="May 09",
                   start_at=dt.datetime(2026, 5, 9, 15, tzinfo=dt.timezone.utc),
                   week_start_date=dt.date(2026, 5, 9), dpp=0.5),
        _trend_row(ns, label="May 15",
                   start_at=dt.datetime(2026, 5, 15, 17, tzinfo=dt.timezone.utc),
                   week_start_date=dt.date(2026, 5, 9), dpp=1.5),
    ])
    assert labels == ["2026-05-09 15:00", "2026-05-09 17:00"], labels


def test_report_share_bytes_are_unchanged_on_an_uncredited_week():
    ns = load_script()
    labels = _report_labels(ns, [
        _trend_row(ns, label="May 02",
                   start_at=dt.datetime(2026, 5, 2, 15, tzinfo=dt.timezone.utc),
                   week_start_date=dt.date(2026, 5, 2), dpp=0.5),
        _trend_row(ns, label="May 09",
                   start_at=dt.datetime(2026, 5, 9, 15, tzinfo=dt.timezone.utc),
                   week_start_date=dt.date(2026, 5, 9), dpp=1.5),
    ])
    assert labels == ["2026-05-02", "2026-05-09"], labels
