"""Tests for dashboard period-builder helpers."""
import datetime as dt
import sqlite3

from conftest import load_script, redirect_paths


def test_chip_for_model_known_buckets():
    ns = load_script()
    cf = ns["_chip_for_model"]
    assert cf("claude-opus-4-5-20251101") == "opus"
    assert cf("claude-opus-4-6") == "opus"
    assert cf("claude-sonnet-4-5-20250929") == "sonnet"
    assert cf("claude-haiku-4-5-20251001") == "haiku"


def test_chip_for_model_unknown_falls_back_to_other():
    ns = load_script()
    cf = ns["_chip_for_model"]
    assert cf("claude-experimental-future") == "other"
    assert cf("") == "other"


def test_weekly_period_row_fields():
    ns = load_script()
    cls = ns["WeeklyPeriodRow"]
    row = cls(
        label="04-23",
        cost_usd=48.21,
        total_tokens=346_000_000,
        input_tokens=414_000,
        output_tokens=240_000,
        cache_creation_tokens=21_300_000,
        cache_read_tokens=324_000_000,
        used_pct=41.0,
        dollar_per_pct=1.18,
        delta_cost_pct=0.09,
        is_current=True,
        models=[{"model": "claude-opus-4-5-20251101", "display": "opus-4-5",
                 "chip": "opus", "cost_usd": 26.51, "cost_pct": 55.0}],
        week_start_at="2026-04-23T09:59:00+02:00",
        week_end_at="2026-04-30T09:59:00+02:00",
    )
    assert row.label == "04-23"
    assert row.is_current is True
    assert row.week_start_at == "2026-04-23T09:59:00+02:00"


def test_monthly_period_row_fields():
    ns = load_script()
    cls = ns["MonthlyPeriodRow"]
    row = cls(
        label="2026-04",
        cost_usd=182.50,
        total_tokens=1_000_000_000,
        input_tokens=2_000_000,
        output_tokens=500_000,
        cache_creation_tokens=92_000_000,
        cache_read_tokens=900_000_000,
        delta_cost_pct=0.02,
        is_current=True,
        models=[{"model": "claude-opus-4-5-20251101", "display": "opus-4-5",
                 "chip": "opus", "cost_usd": 110.0, "cost_pct": 60.0}],
    )
    assert row.label == "2026-04"
    # Monthly rows do not carry usage overlay or week boundaries.
    assert not hasattr(row, "used_pct")
    assert not hasattr(row, "week_start_at")


def test_dashboard_build_monthly_periods_handles_empty_db(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    rows = ns["_dashboard_build_monthly_periods"](
        conn,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
        n=12,
    )
    assert rows == []


def test_dashboard_build_weekly_periods_handles_empty_db(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    rows = ns["_dashboard_build_weekly_periods"](
        conn,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
        n=12,
    )
    assert rows == []


def test_dashboard_build_monthly_periods_emits_rows_when_seeded(tmp_path, monkeypatch):
    """Sanity end-to-end: seed one synthetic session entry, assert one row
    comes back with the right label and a non-zero cost."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    # Prime the cache DB so its schema (session_entries) exists, then
    # insert directly. Column names match the live schema in `open_cache_db`
    # (timestamp_utc / cache_create_tokens / line_offset).
    cache = ns["open_cache_db"]()
    cache.execute(
        """INSERT INTO session_entries
        (source_path, line_offset, timestamp_utc, model,
         input_tokens, output_tokens, cache_create_tokens, cache_read_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("/x/file1.jsonl", 0, "2026-04-15T12:00:00Z",
         "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
    )
    cache.commit()
    cache.close()
    rows = ns["_dashboard_build_monthly_periods"](
        conn,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
        n=12,
    )
    assert len(rows) >= 1
    apr = next((r for r in rows if r.label == "2026-04"), None)
    assert apr is not None
    assert apr.cost_usd > 0
    assert apr.is_current is True
    assert any(m["chip"] == "opus" for m in apr.models)
    # _short_model_name should strip the "claude-" prefix and the trailing
    # "-YYYYMMDD" date suffix.
    assert apr.models[0]["display"] == "opus-4-5"


def test_data_snapshot_has_period_fields_with_empty_defaults():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    assert snap.weekly_periods == []
    assert snap.monthly_periods == []


def test_weekly_now_pill_tracks_now_utc_not_stale_snapshot(tmp_path, monkeypatch):
    """Regression: when the latest `weekly_usage_snapshots` row is from an
    older subscription week (status line hasn't fired yet this week) but
    cost entries already exist for the current week, the `Now` pill must
    land on the row that contains `now_utc` — NOT the stale snapshot's
    week. Reviewer flagged P2."""
    import pathlib, sys
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    from _fixture_builders import (
        create_cache_db, seed_session_file, seed_session_entry,
        seed_weekly_usage_snapshot,
    )
    share = tmp_path / ".local" / "share" / "cctally"

    # Seed ONE snapshot for last week (2026-04-18 → 2026-04-25). This is
    # the "latest" by captured_at_utc, but it's stale — `now_utc` falls in
    # the NEXT subscription week (2026-04-25 → 2026-05-02).
    open_db = ns["open_db"]
    with open_db() as conn:
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc="2026-04-22T15:00:00Z",
            week_start_date="2026-04-18",
            week_end_date="2026-04-25",
            week_start_at="2026-04-18T00:00:00+00:00",
            week_end_at="2026-04-25T00:00:00+00:00",
            weekly_percent=42.0,
        )

    # Seed cost entries in BOTH weeks so each shows up in the panel.
    cache_path = share / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as cconn:
        seed_session_file(
            cconn, path="/fake/sess.jsonl",
            session_id="s1", project_path="/p",
        )
        # Last week's entry.
        seed_session_entry(
            cconn, source_path="/fake/sess.jsonl",
            line_offset=0,
            timestamp_utc="2026-04-22T12:00:00Z",
            model="claude-opus-4-5-20251101",
            input_tokens=100, output_tokens=50,
            cache_create=1000, cache_read=5000,
        )
        # THIS week's entry — same week as `now_utc`.
        seed_session_entry(
            cconn, source_path="/fake/sess.jsonl",
            line_offset=1,
            timestamp_utc="2026-04-26T08:00:00Z",
            model="claude-opus-4-5-20251101",
            input_tokens=100, output_tokens=50,
            cache_create=1000, cache_read=5000,
        )

    with open_db() as conn:
        rows = ns["_dashboard_build_weekly_periods"](
            conn,
            now_utc=dt.datetime(2026, 4, 26, 12, 0, tzinfo=dt.timezone.utc),
            n=12,
        )
    # The current-week row (start_date=2026-04-25) gets the Now pill;
    # the stale snapshot's week (2026-04-18) does not.
    cur = next((r for r in rows if r.label == "04-25"), None)
    stale = next((r for r in rows if r.label == "04-18"), None)
    assert cur is not None, "current-week row missing"
    assert stale is not None, "prior-week row missing"
    assert cur.is_current is True
    assert stale.is_current is False


def test_monthly_caps_to_n_drops_boundary_spillover(tmp_path, monkeypatch):
    """Regression: in tzs west of UTC, `range_start` (UTC midnight on the
    1st) lands in the previous local month, so `_aggregate_monthly` may
    emit an (n+1)th `'YYYY-MM'` bucket from the boundary window. The
    builder must cap to `n` BEFORE the delta loop so (a) the visible
    history matches the requested length and (b) the oldest visible row's
    delta is `None` rather than vs. a few-hour spillover bucket. Reviewer
    flagged P2."""
    import pathlib, sys, time
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    from _fixture_builders import (
        create_cache_db, seed_session_file, seed_session_entry,
    )
    share = tmp_path / ".local" / "share" / "cctally"

    # Force the local tz west of UTC so the boundary spillover triggers.
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()

    cache_path = share / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as cconn:
        seed_session_file(
            cconn, path="/fake/sess.jsonl",
            session_id="s1", project_path="/p",
        )
        # Boundary entry: 2025-05-01T01:00 UTC = 2025-04-30 18:00 PDT
        # (April 2025 local). This is the spillover bucket.
        seed_session_entry(
            cconn, source_path="/fake/sess.jsonl",
            line_offset=0,
            timestamp_utc="2025-05-01T01:00:00Z",
            model="claude-opus-4-5-20251101",
            input_tokens=100, output_tokens=50,
            cache_create=1000, cache_read=5000,
        )
        # 12 distinct months: 2025-05 through 2026-04 (one entry each at
        # mid-month so tz boundary jitter doesn't matter).
        offset = 1
        for ym in [
            "2025-05-15", "2025-06-15", "2025-07-15", "2025-08-15",
            "2025-09-15", "2025-10-15", "2025-11-15", "2025-12-15",
            "2026-01-15", "2026-02-15", "2026-03-15", "2026-04-15",
        ]:
            seed_session_entry(
                cconn, source_path="/fake/sess.jsonl",
                line_offset=offset,
                timestamp_utc=f"{ym}T20:00:00Z",
                model="claude-opus-4-5-20251101",
                input_tokens=100, output_tokens=50,
                cache_create=1000, cache_read=5000,
            )
            offset += 1

    open_db = ns["open_db"]
    with open_db() as conn:
        rows = ns["_dashboard_build_monthly_periods"](
            conn,
            now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
            n=12,
        )
    assert len(rows) == 12, f"expected 12 rows, got {len(rows)}: {[r.label for r in rows]}"
    # Newest-first: rows[0] is current month.
    assert rows[0].label == "2026-04"
    # Oldest visible row was bucket #12 in the original list — its prev
    # bucket (the boundary spillover) was dropped, so delta is None.
    assert rows[-1].delta_cost_pct is None
    # And the boundary spillover bucket (2025-04) is NOT in the visible set.
    assert all(r.label != "2025-04" for r in rows)


def test_dashboard_weekly_period_uses_display_date_after_reset(
    tmp_path, monkeypatch
):
    """End-to-end: seed a DB with two adjacent weeks + a reset event whose
    effective moment falls inside the post-reset SubWeek's API-derived
    backdated start. The dashboard's row label must reflect the effective
    reset date (04-13), not the API-derived backdated week_start_date
    (04-11)."""
    import datetime as dt
    import pathlib, sys
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    from _fixture_builders import (
        create_cache_db, seed_session_file, seed_session_entry,
        seed_weekly_usage_snapshot,
    )
    share = tmp_path / ".local" / "share" / "cctally"

    open_db = ns["open_db"]

    # Pre-reset week:  04-09 → 04-16 (60% used)
    # Post-reset week: API-derived start = 04-11 (backdated), end = 04-18
    # Reset event:     effective_reset_at_utc = 04-13T18:00Z
    # → post-reset SubWeek's display_start_date must be 04-13.
    with open_db() as conn:
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc="2026-04-11T12:00:00Z",
            week_start_date="2026-04-09",
            week_end_date="2026-04-16",
            week_start_at="2026-04-09T15:00:00+00:00",
            week_end_at="2026-04-16T15:00:00+00:00",
            weekly_percent=60.0,
        )
        seed_weekly_usage_snapshot(
            conn,
            captured_at_utc="2026-04-15T12:00:00Z",
            week_start_date="2026-04-11",
            week_end_date="2026-04-18",
            week_start_at="2026-04-11T15:00:00+00:00",
            week_end_at="2026-04-18T15:00:00+00:00",
            weekly_percent=25.0,
        )
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
            ("2026-04-13T18:01:00Z",
             "2026-04-16T15:00:00+00:00",
             "2026-04-18T15:00:00+00:00",
             "2026-04-13T18:00:00+00:00"),
        )
        conn.commit()

    # Seed at least one cost entry in EACH SubWeek so both buckets emit.
    # Pre-reset bucket window is 04-09T15:00Z → 04-13T18:00Z (after the
    # post-processor clamps end_ts). Post-reset bucket window is
    # 04-13T18:00Z → 04-18T15:00Z. Pick timestamps clearly inside each.
    cache_path = share / "cache.db"
    create_cache_db(cache_path)
    with sqlite3.connect(cache_path) as cconn:
        seed_session_file(
            cconn, path="/fake/sess.jsonl",
            session_id="s1", project_path="/p",
        )
        seed_session_entry(  # pre-reset week
            cconn, source_path="/fake/sess.jsonl",
            line_offset=0,
            timestamp_utc="2026-04-12T12:00:00Z",
            model="claude-opus-4-5-20251101",
            input_tokens=100, output_tokens=50,
            cache_create=1000, cache_read=5000,
        )
        seed_session_entry(  # post-reset week (after 04-13T18:00Z reset)
            cconn, source_path="/fake/sess.jsonl",
            line_offset=1,
            timestamp_utc="2026-04-15T12:00:00Z",
            model="claude-opus-4-5-20251101",
            input_tokens=100, output_tokens=50,
            cache_create=1000, cache_read=5000,
        )

    with open_db() as conn:
        builder = ns["_dashboard_build_weekly_periods"]
        now_utc = dt.datetime(2026, 4, 17, 12, 0, 0, tzinfo=dt.timezone.utc)
        rows = builder(conn, now_utc, n=4, skip_sync=True)

    labels = [r.label for r in rows]
    assert "04-13" in labels, f"expected 04-13 in {labels}"
    assert "04-11" not in labels, f"unexpected 04-11 in {labels}"
