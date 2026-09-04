"""An in-place weekly credit ends one billing cycle and begins another
inside the same week, so every per-week surface must render it as two rows.

These tests cover the two places that collapse the pair back into one row
unless the segment identity is threaded through: `_aggregate_weekly`'s
bucket key (two segments share `start_date`, which used to be the key) and
`build_weekly_view`'s single global `as_of_utc` (which resolved both
segments to the same usage snapshot).
"""
import datetime as dt
import sqlite3

from conftest import load_script


def _usage_snapshot_conn(rows):
    """An in-memory `weekly_usage_snapshots` table holding `rows`.

    `rows` is a list of `(captured_at_utc, week_start_date, weekly_percent)`.
    """
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
            weekly_percent REAL
        )
    """)
    for captured, wsd, pct in rows:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, weekly_percent) "
            "VALUES (?, ?, ?)",
            (captured, wsd, pct),
        )
    conn.commit()
    return conn


def _segments(ns):
    """The two SubWeek segments of one credited week, as the applier emits
    them: same `start_date`, contiguous at the credit moment."""
    SubWeek = ns["SubWeek"]
    common = dict(
        start_date=dt.date(2026, 5, 9),
        end_date=dt.date(2026, 5, 15),
        source="snapshot",
    )
    pre = SubWeek(
        start_ts="2026-05-09T15:00:00+00:00",
        end_ts="2026-05-15T17:00:00+00:00",
        display_start_date=dt.date(2026, 5, 9),
        **common,
    )
    post = SubWeek(
        start_ts="2026-05-15T17:00:00+00:00",
        end_ts="2026-05-16T15:00:00+00:00",
        display_start_date=dt.date(2026, 5, 15),
        **common,
    )
    return pre, post


def _entry(ns, *, ts, cost):
    UsageEntry = ns["UsageEntry"]
    return UsageEntry(
        timestamp=dt.datetime.fromisoformat(ts),
        model="claude-opus-4-5-20251101",
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        cost_usd=cost,
        source_path="/fake/sess.jsonl",
    )


def test_aggregate_weekly_emits_two_buckets_for_two_segments_of_one_week():
    """Two SubWeeks sharing `start_date` must produce TWO buckets.

    `_aggregate_weekly` used to key buckets on `w.start_date.isoformat()`
    and `_aggregate_buckets` folds same-key entries into ONE accumulator,
    so the pair merged into a single row carrying both costs against one
    percent — worse than dropping the pre-credit row.
    """
    ns = load_script()
    aggregate = ns["_aggregate_weekly"]
    pre, post = _segments(ns)
    entries = [
        _entry(ns, ts="2026-05-13T12:00:00+00:00", cost=40.0),
        _entry(ns, ts="2026-05-15T18:30:00+00:00", cost=4.0),
    ]

    buckets = aggregate(entries, [pre, post], mode="display")

    assert len(buckets) == 2, [b.bucket for b in buckets]
    assert [b.bucket for b in buckets] == [pre.segment_key, post.segment_key]
    assert [round(b.cost_usd, 6) for b in buckets] == [40.0, 4.0]


def test_build_weekly_view_resolves_each_segment_to_its_own_percent():
    """Each segment must resolve to the usage snapshot current at ITS end.

    `build_weekly_view` passes one global `as_of_utc` to every row, so both
    segments used to read the same (latest) snapshot and render the same
    percent. The pre-credit segment must instead pick up the last snapshot
    captured before the credit.
    """
    ns = load_script()
    build = ns["build_weekly_view"]
    pre, post = _segments(ns)
    conn = _usage_snapshot_conn([
        ("2026-05-15T16:00:00Z", "2026-05-09", 67.0),   # pre-credit peak
        ("2026-05-15T19:00:00Z", "2026-05-09", 4.0),    # post-credit
    ])
    entries = [
        _entry(ns, ts="2026-05-13T12:00:00+00:00", cost=40.0),
        _entry(ns, ts="2026-05-15T18:30:00+00:00", cost=4.0),
    ]
    now = dt.datetime(2026, 5, 15, 20, 0, tzinfo=dt.timezone.utc)

    view = build(
        conn, entries, weeks=[pre, post], now_utc=now,
        as_of_utc="2026-05-15T20:00:00Z", mode="display",
    )

    # rows come back newest-first.
    assert len(view.rows) == 2, [r.week_start_at for r in view.rows]
    newest, oldest = view.rows
    assert oldest.week_start_at == pre.start_ts
    assert newest.week_start_at == post.start_ts
    assert oldest.used_pct == 67.0
    assert newest.used_pct == 4.0

    # Costs are per-segment and sum to the pair total with no double count.
    assert round(oldest.cost_usd, 6) == 40.0
    assert round(newest.cost_usd, 6) == 4.0
    assert round(view.total_cost_usd, 6) == 44.0


def test_build_weekly_view_keeps_the_global_as_of_for_an_uncredited_week():
    """A week with no sibling segment keeps reading the globally-latest
    snapshot at-or-before `as_of_utc`.

    The per-segment `as_of` bound applies only where a `start_date` is
    shared, mirroring `build_trend_view`'s `split_keys` handling. Applying
    it unconditionally would newly exclude a snapshot captured in the
    jitter window just past a normal week's `end_ts`.
    """
    ns = load_script()
    build = ns["build_weekly_view"]
    SubWeek = ns["SubWeek"]
    week = SubWeek(
        start_ts="2026-05-02T15:00:00+00:00",
        end_ts="2026-05-09T15:00:00+00:00",
        start_date=dt.date(2026, 5, 2),
        end_date=dt.date(2026, 5, 8),
        source="snapshot",
        display_start_date=dt.date(2026, 5, 2),
    )
    conn = _usage_snapshot_conn([
        ("2026-05-09T14:00:00Z", "2026-05-02", 80.0),
        # Captured 5 seconds after the week's end_ts but still stamped with
        # this week — the capture-jitter case.
        ("2026-05-09T15:00:05Z", "2026-05-02", 82.0),
    ])
    entries = [_entry(ns, ts="2026-05-05T12:00:00+00:00", cost=7.0)]
    now = dt.datetime(2026, 5, 16, 12, 0, tzinfo=dt.timezone.utc)

    view = build(
        conn, entries, weeks=[week], now_utc=now,
        as_of_utc="2026-05-16T12:00:00Z", mode="display",
    )

    assert len(view.rows) == 1
    assert view.rows[0].used_pct == 82.0


def test_build_weekly_view_keeps_the_global_as_of_for_the_last_segment():
    """The LAST segment of a credited week keeps the global `as_of_utc`.

    Only the pre-credit segment ends at the credit moment. The post-credit
    segment's `end_ts` IS the week's real end, so bounding it there rejects a
    snapshot captured a few seconds past the boundary and still stamped with
    this `week_start_date` — the same capture-jitter tolerance
    `test_build_weekly_view_keeps_the_global_as_of_for_an_uncredited_week`
    protects on every uncredited week. Bounding the last segment would drop
    the post-credit row's `Used %` to an earlier reading and raise `$/1%`
    with it.
    """
    ns = load_script()
    build = ns["build_weekly_view"]
    pre, post = _segments(ns)
    conn = _usage_snapshot_conn([
        ("2026-05-15T16:00:00Z", "2026-05-09", 67.0),   # pre-credit peak
        ("2026-05-16T14:00:00Z", "2026-05-09", 11.0),   # post-credit
        # Captured 7 seconds after the week's real end (post.end_ts) and
        # still stamped with this week — the capture-jitter case.
        ("2026-05-16T15:00:07Z", "2026-05-09", 13.0),
    ])
    entries = [
        _entry(ns, ts="2026-05-13T12:00:00+00:00", cost=40.0),
        _entry(ns, ts="2026-05-15T18:30:00+00:00", cost=4.0),
    ]
    now = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)

    view = build(
        conn, entries, weeks=[pre, post], now_utc=now,
        as_of_utc="2026-05-20T12:00:00Z", mode="display",
    )

    assert len(view.rows) == 2, [r.week_start_at for r in view.rows]
    newest, oldest = view.rows
    assert oldest.week_start_at == pre.start_ts
    assert newest.week_start_at == post.start_ts
    # The pre-credit segment is still bounded to the credit moment.
    assert oldest.used_pct == 67.0
    # The post-credit segment reads the jitter-window capture.
    assert newest.used_pct == 13.0


def test_weekly_share_artifact_labels_the_two_segments_distinctly(monkeypatch):
    """The share table has ONE label column, so the two segments of a
    credited week must not both render under the same date.

    The builder reads `display_start_date` — each segment's own user-facing
    start, the same field the terminal table's Week column renders. Reading
    `start_date` would print `2026-05-09` twice, and the artifact's stated
    period would still be right, so nothing else would reveal it.
    """
    import sys
    ns = load_script()
    cct = sys.modules["cctally"]
    pre, post = _segments(ns)
    BucketUsage = ns["BucketUsage"]
    WeeklyView = ns["WeeklyView"]

    def _bucket(sw, cost):
        return BucketUsage(
            bucket=sw.segment_key, input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0, total_tokens=0,
            cost_usd=cost, models=["m"], model_breakdowns=[],
        )

    # view.aggregated / view.overlay are newest-first.
    view = WeeklyView(
        rows=(), aggregated=(_bucket(post, 4.0), _bucket(pre, 40.0)),
        overlay=((12.0, 0.33), (71.0, 0.56)),
        total_cost_usd=44.0, total_tokens=0,
        period_start=dt.datetime(2026, 5, 9, tzinfo=dt.timezone.utc),
        period_end=dt.datetime(2026, 5, 16, tzinfo=dt.timezone.utc),
        display_tz_label="UTC",
    )
    snap = cct._build_weekly_snapshot(
        view,
        period_start=dt.datetime(2026, 5, 9, tzinfo=dt.timezone.utc),
        period_end=dt.datetime(2026, 5, 16, tzinfo=dt.timezone.utc),
        display_tz="UTC", version="9.9.9", breakdown_model=False,
        since_explicit=True, weeks=[pre, post],
    )

    labels = [r.cells["week"].text for r in snap.rows]
    assert labels == ["2026-05-09", "2026-05-15"], labels
