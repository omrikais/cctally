"""Tests for dashboard Blocks-panel builder helpers."""
import datetime as dt

from conftest import load_script, redirect_paths


def test_blocks_panel_row_fields():
    ns = load_script()
    cls = ns["BlocksPanelRow"]
    row = cls(
        start_at="2026-04-26T14:00:00+00:00",
        end_at="2026-04-26T19:00:00+00:00",
        anchor="recorded",
        is_active=True,
        cost_usd=4.21,
        models=[
            {"model": "claude-opus-4-5-20251101", "display": "opus-4-5",
             "chip": "opus", "cost_usd": 3.28, "cost_pct": 78.0},
        ],
        label="14:00 Apr 26",
    )
    assert row.start_at == "2026-04-26T14:00:00+00:00"
    assert row.anchor == "recorded"
    assert row.is_active is True
    assert row.cost_usd == 4.21
    assert row.label == "14:00 Apr 26"
    # Guard against accidental token-field additions: spec explicitly drops them.
    assert not hasattr(row, "input_tokens")
    assert not hasattr(row, "total_tokens")


import sqlite3


def test_blocks_panel_handles_empty_db(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    week_start = dt.datetime(2026, 4, 19, 0, 0, tzinfo=dt.timezone.utc)
    week_end = dt.datetime(2026, 4, 26, 0, 0, tzinfo=dt.timezone.utc)
    rows = ns["_dashboard_build_blocks_panel"](
        conn,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
        week_start_at=week_start,
        week_end_at=week_end,
    )
    assert rows == []


def test_blocks_panel_filters_to_window_and_excludes_gaps(tmp_path, monkeypatch):
    """Seed cost entries inside and outside the window; assert only the
    inside ones make it into the panel and that no `is_gap=True` rows leak."""
    import pathlib, sys
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))

    share = tmp_path / ".local" / "share" / "cctally"
    cache_path = share / "cache.db"

    # Seed via the cache.db schema directly (matches test_dashboard_period_builders).
    open_db = ns["open_db"]
    conn = open_db()
    cache = ns["open_cache_db"]()
    # Inside the window (2026-04-22 14:30 UTC).
    cache.execute(
        """INSERT INTO session_entries
        (source_path, line_offset, timestamp_utc, model,
         input_tokens, output_tokens, cache_create_tokens, cache_read_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("/x/file1.jsonl", 0, "2026-04-22T14:30:00Z",
         "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
    )
    # Outside the window (2026-04-15, before week_start).
    cache.execute(
        """INSERT INTO session_entries
        (source_path, line_offset, timestamp_utc, model,
         input_tokens, output_tokens, cache_create_tokens, cache_read_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("/x/file2.jsonl", 0, "2026-04-15T08:00:00Z",
         "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
    )
    cache.commit()
    cache.close()

    week_start = dt.datetime(2026, 4, 19, 0, 0, tzinfo=dt.timezone.utc)
    week_end = dt.datetime(2026, 4, 26, 0, 0, tzinfo=dt.timezone.utc)
    rows = ns["_dashboard_build_blocks_panel"](
        conn,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
        week_start_at=week_start,
        week_end_at=week_end,
    )
    # Exactly one block (no gap rows leak through, outside-window entry excluded).
    assert len(rows) == 1
    assert rows[0].cost_usd > 0
    # All rows must have anchor in the documented domain.
    assert all(r.anchor in ("recorded", "heuristic") for r in rows)


def test_blocks_panel_orders_newest_first(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache = ns["open_cache_db"]()
    # Two entries, 6 hours apart (different blocks).
    for i, ts in enumerate(["2026-04-22T08:30:00Z", "2026-04-22T15:30:00Z"]):
        cache.execute(
            """INSERT INTO session_entries
            (source_path, line_offset, timestamp_utc, model,
             input_tokens, output_tokens, cache_create_tokens, cache_read_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"/x/file{i}.jsonl", 0, ts,
             "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
        )
    cache.commit()
    cache.close()
    conn = ns["open_db"]()

    week_start = dt.datetime(2026, 4, 19, 0, 0, tzinfo=dt.timezone.utc)
    week_end = dt.datetime(2026, 4, 26, 0, 0, tzinfo=dt.timezone.utc)
    rows = ns["_dashboard_build_blocks_panel"](
        conn,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
        week_start_at=week_start,
        week_end_at=week_end,
    )
    assert len(rows) >= 2
    # Newest first: start_at descending.
    starts = [r.start_at for r in rows]
    assert starts == sorted(starts, reverse=True)


def test_blocks_panel_label_format(tmp_path, monkeypatch):
    """Label is 'HH:MM MMM DD' in local tz. Pin TZ=Etc/UTC per CLAUDE.md."""
    import os
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("TZ", "Etc/UTC")
    # Re-import time so TZ takes effect.
    import time as _time
    _time.tzset()

    cache = ns["open_cache_db"]()
    cache.execute(
        """INSERT INTO session_entries
        (source_path, line_offset, timestamp_utc, model,
         input_tokens, output_tokens, cache_create_tokens, cache_read_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("/x/file.jsonl", 0, "2026-04-22T14:30:00Z",
         "claude-opus-4-5-20251101", 100, 50, 1000, 5000),
    )
    cache.commit()
    cache.close()
    conn = ns["open_db"]()

    rows = ns["_dashboard_build_blocks_panel"](
        conn,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
        week_start_at=dt.datetime(2026, 4, 19, 0, 0, tzinfo=dt.timezone.utc),
        week_end_at=dt.datetime(2026, 4, 26, 0, 0, tzinfo=dt.timezone.utc),
    )
    assert rows
    # Format: "HH:MM MMM DD <TZ>" — e.g. "14:00 Apr 22 UTC". The trailing
    # tz token comes from format_display_dt(..., suffix=True), which
    # CLAUDE.md flags as the canonical render-time chokepoint for human
    # datetimes; the suffix here is fixed to "UTC" because the test pins
    # TZ=Etc/UTC above.
    import re
    assert re.match(
        r"^\d{2}:\d{2} [A-Z][a-z]{2} \d{2} UTC$", rows[0].label
    ), rows[0].label


def test_empty_dashboard_snapshot_has_blocks_panel_default():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    assert snap.blocks_panel == []


def test_tui_empty_snapshot_has_blocks_panel_default():
    ns = load_script()
    snap = ns["_tui_empty_snapshot"](
        dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc)
    )
    assert snap.blocks_panel == []
