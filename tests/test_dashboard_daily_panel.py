"""Tests for dashboard Daily-panel builder helpers."""
import datetime as dt

from conftest import load_script, redirect_paths


def test_daily_panel_row_fields():
    ns = load_script()
    cls = ns["DailyPanelRow"]
    row = cls(
        date="2026-04-26",
        label="04-26",
        cost_usd=8.40,
        is_today=True,
        intensity_bucket=5,
        models=[
            {"model": "claude-opus-4-5-20251101", "display": "opus-4-5",
             "chip": "opus", "cost_usd": 5.20, "cost_pct": 62.0},
        ],
    )
    assert row.date == "2026-04-26"
    assert row.is_today is True
    assert 0 <= row.intensity_bucket <= 5


def test_daily_panel_row_token_fields_present():
    """v2.3: DailyPanelRow exposes input/output/cache_creation/cache_read/total
    tokens plus cache_hit_pct so the dashboard modal can surface the same
    detail the CLI's `daily` command shows."""
    ns = load_script()
    cls = ns["DailyPanelRow"]
    row = cls(
        date="2026-04-26",
        label="04-26",
        cost_usd=4.37,
        is_today=True,
        intensity_bucket=5,
        models=[],
        input_tokens=412_000,
        output_tokens=38_400,
        cache_creation_tokens=1_200_000,
        cache_read_tokens=8_300_000,
        total_tokens=9_950_400,
        cache_hit_pct=87.3,
    )
    assert row.input_tokens == 412_000
    assert row.output_tokens == 38_400
    assert row.cache_creation_tokens == 1_200_000
    assert row.cache_read_tokens == 8_300_000
    assert row.total_tokens == 9_950_400
    assert row.cache_hit_pct == 87.3


def test_daily_panel_row_token_fields_default_to_zero():
    """Existing call sites that omit tokens (e.g. `_empty_dashboard_snapshot`,
    pre-v2.3 fixtures) must keep working — token fields default to 0,
    cache_hit_pct defaults to None."""
    ns = load_script()
    cls = ns["DailyPanelRow"]
    row = cls(
        date="2026-04-26", label="04-26", cost_usd=0.0,
        is_today=False, intensity_bucket=0, models=[],
    )
    assert row.input_tokens == 0
    assert row.output_tokens == 0
    assert row.cache_creation_tokens == 0
    assert row.cache_read_tokens == 0
    assert row.total_tokens == 0
    assert row.cache_hit_pct is None


def _make_daily_row(ns, **kwargs):
    """Helper to build a DailyPanelRow with defaults for fields not under test."""
    cls = ns["DailyPanelRow"]
    defaults = dict(
        date="2026-04-01", label="04-01", cost_usd=0.0,
        is_today=False, intensity_bucket=0, models=[],
    )
    defaults.update(kwargs)
    return cls(**defaults)


def test_compute_intensity_buckets_zero_days_get_bucket_0():
    ns = load_script()
    rows = [_make_daily_row(ns, cost_usd=0.0) for _ in range(5)]
    thresholds = ns["_compute_intensity_buckets"](rows)
    assert thresholds == []
    assert all(r.intensity_bucket == 0 for r in rows)


def test_compute_intensity_buckets_single_nonzero_clamps_to_bucket_1():
    """With exactly one non-zero day, that day must land in bucket 1
    (not 5) — verifies the min(bucket, 5) clamp on the upper end."""
    ns = load_script()
    rows = [
        _make_daily_row(ns, date="2026-04-01", cost_usd=0.0),
        _make_daily_row(ns, date="2026-04-02", cost_usd=5.0),
    ]
    ns["_compute_intensity_buckets"](rows)
    by_date = {r.date: r for r in rows}
    assert by_date["2026-04-01"].intensity_bucket == 0
    assert by_date["2026-04-02"].intensity_bucket == 1


def test_compute_intensity_buckets_quintile_distribution():
    """Five non-zero costs should map cleanly to buckets 1..5."""
    ns = load_script()
    costs = [0.5, 1.0, 2.0, 3.0, 5.0]  # five non-zero, sorted ascending
    rows = [_make_daily_row(ns, date=f"2026-04-0{i+1}", cost_usd=c)
            for i, c in enumerate(costs)]
    thresholds = ns["_compute_intensity_buckets"](rows)
    # Five thresholds, monotonically non-decreasing.
    assert len(thresholds) == 5
    assert thresholds == sorted(thresholds)
    # Smallest non-zero day → bucket 1; largest → bucket 5.
    by_cost = sorted(rows, key=lambda r: r.cost_usd)
    assert by_cost[0].intensity_bucket == 1
    assert by_cost[-1].intensity_bucket == 5


def test_compute_intensity_buckets_mixed_zero_and_nonzero():
    """Zero days get bucket 0 even when non-zero days exist."""
    ns = load_script()
    rows = [
        _make_daily_row(ns, date="2026-04-01", cost_usd=0.0),
        _make_daily_row(ns, date="2026-04-02", cost_usd=1.0),
        _make_daily_row(ns, date="2026-04-03", cost_usd=2.0),
        _make_daily_row(ns, date="2026-04-04", cost_usd=0.0),
        _make_daily_row(ns, date="2026-04-05", cost_usd=10.0),
    ]
    ns["_compute_intensity_buckets"](rows)
    by_date = {r.date: r for r in rows}
    assert by_date["2026-04-01"].intensity_bucket == 0
    assert by_date["2026-04-04"].intensity_bucket == 0
    assert by_date["2026-04-02"].intensity_bucket >= 1
    assert by_date["2026-04-05"].intensity_bucket >= 1


import sqlite3


def test_daily_panel_empty_db(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    rows = ns["_dashboard_build_daily_panel"](
        conn,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
        n=30,
    )
    assert rows == []


def test_daily_panel_seeded(tmp_path, monkeypatch):
    """Seed three days, assert ordering newest-first and is_today flag."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("TZ", "Etc/UTC")
    import time as _time
    _time.tzset()

    # Note on timestamp format: production ingest writes
    # `entry.timestamp.astimezone(utc).isoformat()` (→ "+00:00" suffix),
    # so seed rows match that format. Mixing Z-suffix here would be
    # silently truncated by SQLite's lex compare against the +00:00
    # range_end string passed by `get_entries`.
    cache = ns["open_cache_db"]()
    for i, ts in enumerate([
        "2026-04-25T12:00:00+00:00",
        "2026-04-23T12:00:00+00:00",
        "2026-04-20T12:00:00+00:00",
    ]):
        cache.execute(
            """INSERT INTO session_entries
            (source_path, line_offset, timestamp_utc, model,
             input_tokens, output_tokens, cache_create_tokens, cache_read_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"/x/f{i}.jsonl", 0, ts,
             "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
        )
    cache.commit()
    cache.close()
    conn = ns["open_db"]()

    rows = ns["_dashboard_build_daily_panel"](
        conn,
        now_utc=dt.datetime(2026, 4, 25, 18, 0, tzinfo=dt.timezone.utc),
        n=30,
    )
    # Panel materializes a contiguous 30-day window (newest-first), so empty
    # days are present as zero-cost rows alongside the seeded days.
    assert len(rows) == 30
    all_dates = [r.date for r in rows]
    assert all_dates[0] == "2026-04-25"
    assert all_dates[-1] == "2026-03-27"
    # Seeded days appear in newest-first order among the nonzero rows.
    nonzero_rows = [r for r in rows if r.cost_usd > 0]
    assert [r.date for r in nonzero_rows] == ["2026-04-25", "2026-04-23", "2026-04-20"]
    # Today marker.
    today_row = next(r for r in rows if r.date == "2026-04-25")
    assert today_row.is_today is True
    assert all(r.is_today is False for r in rows if r.date != "2026-04-25")
    # Label is "MM-DD".
    assert today_row.label == "04-25"
    # Bucketing has run (no row left at default 0 if it has cost).
    assert all(r.intensity_bucket >= 1 for r in nonzero_rows)


def test_daily_panel_caps_to_n(tmp_path, monkeypatch):
    """Seed 35 days; assert we get exactly 30."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("TZ", "Etc/UTC")
    import time as _time
    _time.tzset()

    # Use "+00:00" suffix (matches production ingest) so SQLite text
    # comparison against the +00:00 range_end string passed by
    # `get_entries` doesn't lex-truncate the boundary row. now_utc is
    # bumped one day past the newest seed so all 35 distinct calendar
    # dates fall inside the trailing window — confirming the cap=30
    # holds because the builder caps, not because of seed loss.
    cache = ns["open_cache_db"]()
    for d in range(1, 36):
        ts = (f"2026-04-{d:02d}T12:00:00+00:00" if d <= 30
              else f"2026-05-{d-30:02d}T12:00:00+00:00")
        cache.execute(
            """INSERT INTO session_entries
            (source_path, line_offset, timestamp_utc, model,
             input_tokens, output_tokens, cache_create_tokens, cache_read_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"/x/f{d}.jsonl", 0, ts,
             "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
        )
    cache.commit()
    cache.close()
    conn = ns["open_db"]()

    rows = ns["_dashboard_build_daily_panel"](
        conn,
        now_utc=dt.datetime(2026, 5, 6, 12, 0, tzinfo=dt.timezone.utc),
        n=30,
    )
    assert len(rows) == 30


def test_empty_dashboard_snapshot_has_daily_panel_default():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    assert snap.daily_panel == []


def test_tui_empty_snapshot_has_daily_panel_default():
    ns = load_script()
    snap = ns["_tui_empty_snapshot"](
        dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc)
    )
    assert snap.daily_panel == []


def test_daily_panel_populates_token_fields(tmp_path, monkeypatch):
    """Builder reads BucketUsage's token fields and threads them into
    DailyPanelRow. One known-good seed → assert exact values per row."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("TZ", "Etc/UTC")
    import time as _time
    _time.tzset()

    cache = ns["open_cache_db"]()
    # Two entries on 2026-04-25 → input=200, output=100, cc=2000, cr=10000.
    for offset, ts in enumerate([
        "2026-04-25T08:00:00+00:00",
        "2026-04-25T16:00:00+00:00",
    ]):
        cache.execute(
            """INSERT INTO session_entries
            (source_path, line_offset, timestamp_utc, model,
             input_tokens, output_tokens, cache_create_tokens, cache_read_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"/x/f.jsonl", offset, ts,
             "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
        )
    cache.commit()
    cache.close()
    conn = ns["open_db"]()

    rows = ns["_dashboard_build_daily_panel"](
        conn,
        now_utc=dt.datetime(2026, 4, 25, 18, 0, tzinfo=dt.timezone.utc),
        n=30,
    )
    today = next(r for r in rows if r.date == "2026-04-25")
    assert today.input_tokens == 200
    assert today.output_tokens == 100
    assert today.cache_creation_tokens == 2000
    assert today.cache_read_tokens == 10000
    assert today.total_tokens == 200 + 100 + 2000 + 10000
    # cache_hit_pct = 10000 / (200 + 2000 + 10000) * 100 = 81.967213...
    assert today.cache_hit_pct is not None
    assert abs(today.cache_hit_pct - (10000 / 12200 * 100)) < 1e-9


def test_daily_panel_zero_day_has_null_cache_hit_pct(tmp_path, monkeypatch):
    """Gap days (no entries) get all-zero tokens and cache_hit_pct=None.
    Avoids divide-by-zero and surfaces 'no data' cleanly to the modal tile."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("TZ", "Etc/UTC")
    import time as _time
    _time.tzset()

    cache = ns["open_cache_db"]()
    cache.execute(
        """INSERT INTO session_entries
        (source_path, line_offset, timestamp_utc, model,
         input_tokens, output_tokens, cache_create_tokens, cache_read_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("/x/f.jsonl", 0, "2026-04-25T12:00:00+00:00",
         "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
    )
    cache.commit()
    cache.close()
    conn = ns["open_db"]()

    rows = ns["_dashboard_build_daily_panel"](
        conn,
        now_utc=dt.datetime(2026, 4, 25, 23, 0, tzinfo=dt.timezone.utc),
        n=30,
    )
    gaps = [r for r in rows if r.date != "2026-04-25"]
    assert all(r.input_tokens == 0 for r in gaps)
    assert all(r.output_tokens == 0 for r in gaps)
    assert all(r.cache_creation_tokens == 0 for r in gaps)
    assert all(r.cache_read_tokens == 0 for r in gaps)
    assert all(r.total_tokens == 0 for r in gaps)
    assert all(r.cache_hit_pct is None for r in gaps)


def test_daily_panel_token_sum_invariant(tmp_path, monkeypatch):
    """input + output + cache_creation + cache_read == total_tokens
    on every non-zero day. Mirrors the four-component sum invariant
    enforced elsewhere in the codebase."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("TZ", "Etc/UTC")
    import time as _time
    _time.tzset()

    cache = ns["open_cache_db"]()
    for i, ts in enumerate([
        "2026-04-25T12:00:00+00:00",
        "2026-04-23T12:00:00+00:00",
        "2026-04-20T12:00:00+00:00",
    ]):
        cache.execute(
            """INSERT INTO session_entries
            (source_path, line_offset, timestamp_utc, model,
             input_tokens, output_tokens, cache_create_tokens, cache_read_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"/x/f{i}.jsonl", 0, ts,
             "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
        )
    cache.commit()
    cache.close()
    conn = ns["open_db"]()

    rows = ns["_dashboard_build_daily_panel"](
        conn,
        now_utc=dt.datetime(2026, 4, 25, 18, 0, tzinfo=dt.timezone.utc),
        n=30,
    )
    nonzero = [r for r in rows if r.cost_usd > 0]
    assert len(nonzero) == 3
    for r in nonzero:
        assert r.input_tokens + r.output_tokens + r.cache_creation_tokens + r.cache_read_tokens == r.total_tokens
